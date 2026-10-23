"""Deterministic preparation of projected Consumption UsageDetails pages."""

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
        billing = _first(top, "billingCost", "costInBillingCurrency")
        currency = _first(top, "billingCurrency", "billingCurrencyCode")
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
