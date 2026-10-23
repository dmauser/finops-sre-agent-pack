"""Deterministic preparation of projected Consumption UsageDetails pages."""

import glob
import json
import os
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


def _tool_output_envelope(path):
    """Return the projected `{value, nextLink}` envelope saved by RunAzCliReadCommands, or None."""

    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    for _ in range(3):
        if isinstance(data, dict) and isinstance(data.get("value"), list):
            return data
        if not isinstance(data, dict):
            return None
        inner = data.get("cliExecutionResult") or data
        output = inner.get("output") if isinstance(inner, dict) else None
        if not isinstance(output, str):
            return None
        try:
            data = json.loads(output)
        except ValueError:
            return None
    return None


def _looks_like_usage_page(envelope):
    if envelope is None or set(envelope) - {"value", "nextLink"}:
        return False
    rows = envelope["value"]
    if not rows:
        return "nextLink" in envelope
    first = rows[0]
    return isinstance(first, dict) and bool(_USAGE_ROW_KEYS & {k.casefold() for k in first})


def find_usage_tool_output_chains(directory="tmp/ToolOutputs", pattern="*RunAzCliReadCommands*.json"):
    """Group saved UsageDetails tool outputs into continuation chains, in fetch order.

    The SRE Agent runtime saves every RunAzCliReadCommands result under `tmp/ToolOutputs`. Files
    are ordered by modification time; non-UsageDetails outputs and failed/timed-out calls are
    skipped, and a page without a nextLink closes its chain. Returns a list of path lists. Pass
    each chain, with the exact URL of its first request, to `load_usage_tool_output_pages`.
    """

    paths = sorted(glob.glob(os.path.join(directory, pattern)), key=lambda p: (os.path.getmtime(p), p))
    chains, current = [], []
    for path in paths:
        envelope = _tool_output_envelope(path)
        if not _looks_like_usage_page(envelope):
            continue
        current.append(path)
        if not _text(envelope.get("nextLink")):
            chains.append(current)
            current = []
    if current:
        chains.append(current)
    return chains


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
