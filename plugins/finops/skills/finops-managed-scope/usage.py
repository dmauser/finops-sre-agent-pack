"""Deterministic preparation of projected Consumption UsageDetails pages."""

import glob
import json
import os
import re
from decimal import Decimal, InvalidOperation
from html import unescape
from urllib.parse import parse_qsl, urlsplit

from scope import filter_usage_details


_UNUSED_COMMITMENT_CHARGE_TYPES = {"unusedreservation", "unusedsavingsplan"}


def _text(value):
    return str(value or "").strip()


def _page_field(page, *names):
    for name in names:
        if name in page:
            return page[name]
    return None


def _lower_keys(mapping):
    return {str(key).casefold(): value for key, value in mapping.items()}


def _present(value):
    return value not in (None, "")


def resolve_resource_id(row):
    """Return the analysis resource id for one UsageDetails row.

    Modern billing puts the full ARM id in `instanceName` (resourceId is often null); legacy rows
    use `resourceId`. Prefer an ARM-shaped instanceName, then an ARM-shaped resourceId, then any
    non-empty value. Accepts flat projected rows or raw rows with a `properties` object.
    """

    if not isinstance(row, dict):
        return None
    top = _lower_keys(row)
    props = top.get("properties")
    sources = [top] + ([_lower_keys(props)] if isinstance(props, dict) else [])
    candidates = []
    for source in sources:
        for name in ("instancename", "resourceid"):
            value = _text(source.get(name))
            if value:
                candidates.append(value)
    for value in candidates:
        if value.casefold().startswith("/subscriptions/"):
            return value
    return candidates[0] if candidates else None


def costs_by_resource(rows):
    """Sum normalized `cost` per resolved resource id -> {resourceId: float}.

    Use on prepare_usage_details(...)["included_rows"] for a ~30-day window to build the
    `costs=` map that rightsize.py, waste.py, allocate.py, and commit.py expect.
    """

    totals = {}
    for row in rows or ():
        rid = resolve_resource_id(row)
        cost = _decimal_or_none(row.get("cost")) if isinstance(row, dict) else None
        if not rid or cost is None:
            continue
        totals[rid] = totals.get(rid, Decimal("0")) + cost
    return {rid: float(round(total, 2)) for rid, total in totals.items()}


def ingestion_gb_per_day(rows, days):
    """Average billable Log Analytics GB/day per workspace id -> {workspaceId: float}.

    Sums `quantity` for rows whose meterCategory is "Log Analytics" and whose unitOfMeasure
    mentions GB (e.g. "1 GB"), keyed by resolved resource id, divided by `days`. Requires the
    shared pull to project `meterCategory`, `quantity`, and `unitOfMeasure`.
    """

    if not days or days <= 0:
        raise ValueError("days must be a positive number of days in the window")
    totals = {}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        r = _lower_keys(row)
        if _text(r.get("metercategory")).casefold() != "log analytics":
            continue
        unit = _text(r.get("unitofmeasure")).casefold()
        # Ingestion is billed per GB; retention is per "GB/Month" and is not ingestion.
        if "gb" not in unit or "month" in unit:
            continue
        quantity = _decimal_or_none(r.get("quantity"))
        rid = resolve_resource_id(row)
        if quantity is None or not rid:
            continue
        # A unit like "100 GB" means quantity is in blocks of 100 GB.
        factor = Decimal("1")
        head = unit.split("gb")[0].strip()
        if head:
            factor = _decimal_or_none(head) or Decimal("1")
        totals[rid] = totals.get(rid, Decimal("0")) + quantity * factor
    return {rid: float(round(total / Decimal(str(days)), 3)) for rid, total in totals.items()}


def _first(mapping, *names):
    for name in names:
        value = mapping.get(name.casefold())
        if _present(value):
            return value
    return None


def _cost_fields(row):
    """Return (usd_cost, billing_cost, billing_currency, charge_type) for one row.

    Raw rows (with a `properties` object) follow the API schema: modern/MCA rows carry
    costInUSD + costInBillingCurrency + billingCurrencyCode, while legacy/EA rows carry only
    `cost` in the billing currency + billingCurrency. Projected rows use the canonical aliases
    `cost` (costInUSD), `billingCost`, `billingCurrency`, and `chargeType`.
    """

    top = _lower_keys(row)
    props = top.get("properties")
    if isinstance(props, dict):
        props = _lower_keys(props)
        usd = _first(props, "costInUSD")
        billing = _first(props, "costInBillingCurrency", "cost", "pretaxCost")
        currency = _first(props, "billingCurrencyCode", "billingCurrency")
        charge_type = _first(top, "chargeType") or _first(props, "chargeType")
    else:
        usd = _first(top, "cost", "costInUSD")
        billing = _first(top, "costInBillingCurrency", "billingCost")
        currency = _first(top, "billingCurrencyCode", "billingCurrency")
        charge_type = _first(top, "chargeType")
    return usd, billing, _text(currency).upper() or None, _text(charge_type).casefold()


def _decimal_or_none(value):
    if not _present(value) or isinstance(value, bool):
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _query_metric(request_url):
    for key, value in parse_qsl(urlsplit(request_url).query, keep_blank_values=True):
        if key.casefold() == "metric" and value.strip():
            return value.strip()
    # The UsageDetails API defaults to actual cost when no metric is supplied.
    return "ActualCost"


def normalize_usage_costs(rows):
    """Resolve one comparable `cost` per row and the currency it is expressed in.

    USD (`costInUSD`) is used when every costed row has it (MCA / MOSP). Legacy EA rows have
    no costInUSD, only `cost` in the billing currency; when any row lacks USD, every row uses
    its billing-currency cost instead so totals never mix currencies. More than one billing
    currency in that mode is rejected — analyze each billing account separately. Rows with
    no cost value at all are the real "missing Cost Management Reader / settling" signal and
    are counted in `missing_cost_count`.
    """

    fields = []
    for row in rows:
        fields.append(_cost_fields(row) if isinstance(row, dict) else (None, None, None, ""))

    needs_billing = any(
        usd is None and billing is not None for usd, billing, _, _ in fields
    )
    if needs_billing:
        currencies = sorted({currency for usd, billing, currency, _ in fields
                             if billing is not None and currency})
        if len(currencies) > 1:
            raise ValueError(
                "UsageDetails rows span multiple billing currencies "
                f"({', '.join(currencies)}) and lack costInUSD; analyze each billing "
                "account separately"
            )
        currency = currencies[0] if currencies else None
        cost_source = "billing_currency"
    else:
        currency = "USD"
        cost_source = "costInUSD"

    normalized = []
    missing = 0
    unused_count = 0
    unused_cost = Decimal("0")
    purchase_count = 0
    purchase_cost = Decimal("0")
    for row, (usd, billing, _, charge_type) in zip(rows, fields):
        if not isinstance(row, dict):
            normalized.append(row)
            continue
        value = billing if needs_billing else usd
        if needs_billing and value is None and usd is not None and currency == "USD":
            value = usd
        if value is None:
            missing += 1
        out = dict(row)
        out["cost"] = value
        out["currency"] = currency
        resolved_id = resolve_resource_id(out)
        if resolved_id:
            out["resourceId"] = resolved_id
        normalized.append(out)
        if charge_type in _UNUSED_COMMITMENT_CHARGE_TYPES:
            unused_count += 1
            unused_cost += _decimal_or_none(value) or Decimal("0")
        elif charge_type == "purchase":
            purchase_count += 1
            purchase_cost += _decimal_or_none(value) or Decimal("0")

    return normalized, {
        "currency": currency,
        "cost_source": cost_source,
        "missing_cost_count": missing,
        "unused_commitment_count": unused_count,
        "unused_commitment_cost": unused_cost,
        "purchase_count": purchase_count,
        "purchase_cost": purchase_cost,
    }


def prepare_usage_details(
    pages,
    managed_scopes,
    management_group_expansions=None,
    *,
    require_complete=True,
):
    """Validate one continuation chain, merge its rows, and apply managed scope.

    Each page is an object with:
      request_url / requestUrl  the URL used for that page;
      value / rows              the projected UsageDetails rows;
      nextLink / next_link      the continuation URL returned by Azure.

    The first page starts the chain. Every later request URL must exactly match the
    preceding decoded nextLink. Independently restarted page sequences cannot be
    mixed accidentally.
    """

    page_list = list(pages or ())
    if not page_list:
        raise ValueError("at least one UsageDetails page is required")

    merged_rows = []
    request_urls = []
    page_row_counts = []
    expected_request_url = None

    for index, page in enumerate(page_list):
        if not isinstance(page, dict):
            raise ValueError(f"UsageDetails page {index} must be an object")

        request_url = _text(_page_field(page, "request_url", "requestUrl"))
        if not request_url:
            raise ValueError(f"UsageDetails page {index} is missing request_url")
        request_url = unescape(request_url)

        if expected_request_url is not None and request_url != expected_request_url:
            raise ValueError(
                f"UsageDetails page {index} does not continue the preceding nextLink"
            )
        if request_url in request_urls:
            raise ValueError(f"UsageDetails page {index} repeats a request URL")

        rows = _page_field(page, "value", "rows")
        if not isinstance(rows, list):
            raise ValueError(f"UsageDetails page {index} value must be an array")

        next_link = _text(_page_field(page, "nextLink", "next_link"))
        expected_request_url = unescape(next_link) if next_link else None
        request_urls.append(request_url)
        page_row_counts.append(len(rows))
        merged_rows.extend(rows)

    chain_complete = expected_request_url is None
    if require_complete and not chain_complete:
        raise ValueError("UsageDetails page chain is incomplete; final nextLink was not fetched")

    cost_metric = _query_metric(request_urls[0])
    normalized_rows, cost_info = normalize_usage_costs(merged_rows)
    coverage = filter_usage_details(
        normalized_rows,
        managed_scopes,
        management_group_expansions=management_group_expansions,
    )
    warnings = []
    if cost_info["missing_cost_count"]:
        warnings.append(
            f"{cost_info['missing_cost_count']} UsageDetails rows had no cost value "
            "(check Cost Management Reader access or recently settling usage)"
        )
    if cost_info["cost_source"] == "billing_currency":
        warnings.append(
            "Costs are in billing currency "
            f"{cost_info['currency'] or 'unknown'} (legacy/EA rows have no costInUSD)"
        )
    if cost_info["unused_commitment_count"]:
        warnings.append(
            f"{cost_info['unused_commitment_count']} unused reservation/savings plan rows "
            f"totaling {cost_info['unused_commitment_cost']} {cost_info['currency'] or ''}"
            " are commitment waste and are not attributable to any resource".rstrip()
        )
    if cost_metric.casefold() != "amortizedcost" and cost_info["purchase_count"]:
        warnings.append(
            f"metric={cost_metric} includes {cost_info['purchase_count']} one-time purchase "
            f"rows totaling {cost_info['purchase_cost']} (reservations, savings plans, or "
            "Marketplace); use metric=AmortizedCost for showback, anomaly, and unit-cost analysis"
        )
    return {
        **coverage,
        **cost_info,
        "cost_metric": cost_metric,
        "warnings": warnings,
        "page_count": len(page_list),
        "page_row_counts": page_row_counts,
        "retrieved_row_count": len(merged_rows),
        "request_urls": request_urls,
        "chain_complete": chain_complete,
        "remaining_next_link": expected_request_url,
        "partial": not chain_complete,
    }


_USAGE_ROW_KEYS = {"date", "cost", "costinbillingcurrency", "billingcost"}


def _unwrap_tool_output(data):
    """Peel RunAzCliReadCommands wrappers (`cliExecutionResult.output` JSON strings) off a payload."""

    for _ in range(4):
        if not isinstance(data, dict) or isinstance(data.get("value"), list):
            return data
        inner = data.get("cliExecutionResult") or data
        output = inner.get("output") if isinstance(inner, dict) else None
        if not isinstance(output, str):
            return data
        try:
            data = json.loads(output)
        except ValueError:
            return None
    return data


def _read_tool_output(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _tool_output_envelope(path):
    """Return the projected `{value, nextLink}` envelope saved by RunAzCliReadCommands, or None."""

    data = _unwrap_tool_output(_read_tool_output(path))
    if isinstance(data, dict) and isinstance(data.get("value"), list):
        return data
    return None


_URL_KEYS = {"request_url", "requesturl", "url", "uri"}
_COMMAND_KEYS = {"command", "commands", "clicommand", "cmd", "arguments", "args", "input"}
_URL_IN_COMMAND = re.compile(
    r"""--ur[il]\s+(?:"([^"]+)"|'([^']+)'|(\S+))|(https://\S+|/subscriptions/\S+)""", re.IGNORECASE
)


def _url_from_command(text):
    match = _URL_IN_COMMAND.search(text or "")
    if not match:
        return None
    return next(group for group in match.groups() if group).rstrip("\"',;")


def _tool_output_request_url(data, depth=0):
    """Find the request URL recorded alongside a saved tool output, never inside its `output` data."""

    if depth > 4:
        return None
    if isinstance(data, list):
        for item in data:
            found = _tool_output_request_url(item, depth + 1)
            if found:
                return found
        return None
    if not isinstance(data, dict):
        return None
    for key, value in data.items():
        folded = str(key).casefold()
        if folded in _URL_KEYS and isinstance(value, str) and value.strip():
            return value.strip()
        if folded in _COMMAND_KEYS:
            found = (_url_from_command(value) if isinstance(value, str)
                     else _tool_output_request_url(value, depth + 1))
            if found:
                return found
    for key, value in data.items():
        if str(key).casefold() not in {"output", "value", "nextlink"} and isinstance(value, (dict, list)):
            found = _tool_output_request_url(value, depth + 1)
            if found:
                return found
    return None


def _normalize_request_url(url):
    text = unescape(_text(url) or "")
    for host in ("https://management.azure.com", "http://management.azure.com"):
        if text.casefold().startswith(host):
            text = text[len(host):]
            break
    return text.casefold()


def _looks_like_usage_page(envelope):
    if envelope is None or set(envelope) - {"value", "nextLink"}:
        return False
    rows = envelope["value"]
    if not rows:
        return "nextLink" in envelope
    first = rows[0]
    return isinstance(first, dict) and bool(_USAGE_ROW_KEYS & {k.casefold() for k in first})


def find_usage_tool_output_chains(directory="tmp/ToolOutputs", pattern="*RunAzCliReadCommands*.json",
                                  *, after_timestamp=None, request_url_prefix=None):
    """Group saved UsageDetails tool outputs into continuation chains, in fetch order.

    The SRE Agent runtime saves every RunAzCliReadCommands result under `tmp/ToolOutputs`. Files
    are ordered by modification time; non-UsageDetails outputs and failed/timed-out calls are
    skipped, and a page without a nextLink closes its chain. A page whose saved request URL is not
    the open chain's nextLink starts a new chain, so an attempt abandoned after a timeout never
    absorbs the retry's pages. Returns a list of path lists; use the last chain whose final page
    has no nextLink. Pass each chain, with the exact URL of its first request, to
    `load_usage_tool_output_pages`.

    The directory persists across scheduled-task executions, so isolate the current run:

    after_timestamp     Unix epoch seconds (`time.time()` captured before the first `az rest`
                        call). Files modified before it are ignored. None keeps every file.
    request_url_prefix  Keep only pages whose request URL starts with this prefix (for example
                        `/subscriptions/<id>/providers/Microsoft.Consumption/usageDetails`), so
                        chains from other subscriptions or API versions never merge. The host
                        `https://management.azure.com`, `&amp;` encoding, and case are ignored. The
                        URL is read from the command saved with the output; when absent it is
                        inferred from the previous page's nextLink or the page's own nextLink. A
                        page whose URL cannot be determined is skipped. None keeps every page.
    """

    if after_timestamp is not None:
        after_timestamp = float(after_timestamp)
    prefix = _normalize_request_url(request_url_prefix) if request_url_prefix else None

    paths = sorted(glob.glob(os.path.join(directory, pattern)), key=lambda p: (os.path.getmtime(p), p))
    chains, current = [], []
    expected_url = None
    for path in paths:
        if after_timestamp is not None and os.path.getmtime(path) < after_timestamp:
            continue
        raw = _read_tool_output(path)
        envelope = _unwrap_tool_output(raw)
        if not isinstance(envelope, dict) or not isinstance(envelope.get("value"), list):
            continue
        if not _looks_like_usage_page(envelope):
            continue
        next_link = _text(envelope.get("nextLink"))
        saved_url = _tool_output_request_url(raw)
        if prefix is not None:
            url = saved_url or (expected_url if current else None) or next_link
            if not url or not _normalize_request_url(url).startswith(prefix):
                continue
        if (current and saved_url and expected_url
                and _normalize_request_url(saved_url) != _normalize_request_url(expected_url)):
            # Not the continuation of the open chain (e.g. a retry after a timed-out page):
            # close the abandoned chain and start a new one.
            chains.append(current)
            current, expected_url = [], None
        current.append(path)
        if not next_link:
            chains.append(current)
            current, expected_url = [], None
        else:
            expected_url = unescape(next_link)
    if current:
        chains.append(current)
    return chains


_INPUT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MISSING = object()


def save_finops_input(name, data=None, *, source_path=None, directory="tmp/finops-data"):
    """Persist one Azure response to `<directory>/<name>.json` for a later analysis step.

    Pass either the parsed/inline response as `data` (a JSON string is parsed), or the exact
    `tmp/ToolOutputs/...` path the runtime reported for that call as `source_path` (the
    RunAzCliReadCommands wrapper is removed). Use fixed names such as `advisor`, `inventory`,
    `budgets`, and `tags` so the analysis loads each input by name instead of scanning
    ToolOutputs heuristically. Returns the written path.
    """

    if not isinstance(name, str) or not _INPUT_NAME.match(name):
        raise ValueError(f"invalid input name {name!r}; use lowercase letters, digits, '-' or '_'")
    if (data is None) == (source_path is None):
        raise ValueError("pass exactly one of data or source_path")
    if source_path is not None:
        raw = _read_tool_output(source_path)
        if raw is None:
            raise ValueError(f"tool output is missing or not JSON: {source_path}")
        data = _unwrap_tool_output(raw)
        if data is None:
            raise ValueError(f"tool output holds no JSON response (failed call?): {source_path}")
    elif isinstance(data, str):
        data = json.loads(data)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{name}.json")
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, default=str)
    os.replace(temp, path)
    return path


def load_finops_input(name, directory="tmp/finops-data", default=_MISSING):
    """Load an input written by `save_finops_input`; return `default` (when given) if absent."""

    if not isinstance(name, str) or not _INPUT_NAME.match(name):
        raise ValueError(f"invalid input name {name!r}")
    path = os.path.join(directory, f"{name}.json")
    if not os.path.exists(path):
        if default is _MISSING:
            raise FileNotFoundError(path)
        return default
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def load_usage_tool_output_pages(initial_url, paths):
    """Rebuild page envelopes for `prepare_usage_details` from saved tool outputs.

    `paths` are the saved RunAzCliReadCommands outputs of one chain in fetch order and
    `initial_url` is the exact URL of the first request. Each later page's `request_url` is the
    preceding page's decoded nextLink, so the pages need no per-page save step.
    """

    pages = []
    request_url = _text(initial_url)
    if not request_url:
        raise ValueError("initial_url is required")
    for index, path in enumerate(paths):
        envelope = _tool_output_envelope(path)
        if envelope is None:
            raise ValueError(f"tool output {index} is not a UsageDetails page: {path}")
        next_link = _text(envelope.get("nextLink"))
        pages.append({"request_url": request_url, "value": envelope["value"], "nextLink": next_link})
        if not next_link:
            if index != len(paths) - 1:
                raise ValueError(f"tool output {index} ends the chain but more files follow: {path}")
            break
        request_url = unescape(next_link)
    return pages
