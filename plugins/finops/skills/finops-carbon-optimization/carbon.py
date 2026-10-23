"""Azure carbon optimization helpers for the FinOps pack.

Pure functions only: no network, no Azure calls. The Carbon Optimization report API is a POST, which
the agent's read-only Azure CLI tool blocks, so this module:

* builds validated request bodies (`build_report_request`, `standard_requests`),
* renders a human-run, read-only query script (`human_run_script`), and
* analyzes the saved responses (`analyze_carbon`).

Emissions are in kgCO2e. API reference:
https://learn.microsoft.com/en-us/rest/api/carbon/carbon-service/query-carbon-emission-reports
"""

from __future__ import annotations

import json
import re
import shlex
from datetime import date

API_VERSION = "2025-04-01"
REPORT_URL = (
    "https://management.azure.com/providers/Microsoft.Carbon/carbonEmissionReports"
    f"?api-version={API_VERSION}"
)
DATE_RANGE_URL = (
    "https://management.azure.com/providers/Microsoft.Carbon/queryCarbonEmissionDataAvailableDateRange"
    f"?api-version={API_VERSION}"
)
UNIT = "kgCO2e"

REPORT_TYPES = (
    "OverallSummaryReport",
    "MonthlySummaryReport",
    "TopItemsSummaryReport",
    "TopItemsMonthlySummaryReport",
    "ItemDetailsReport",
)
SINGLE_MONTH_REPORTS = {"ItemDetailsReport", "TopItemsSummaryReport"}
CATEGORY_REPORTS = {"ItemDetailsReport", "TopItemsSummaryReport", "TopItemsMonthlySummaryReport"}
TOP_ITEM_REPORTS = {"TopItemsSummaryReport", "TopItemsMonthlySummaryReport"}
CATEGORY_TYPES = ("Resource", "ResourceGroup", "ResourceType", "Location", "Subscription")
ORDER_BY_COLUMNS = (
    "ItemName",
    "LatestMonthEmissions",
    "PreviousMonthEmissions",
    "MonthOverMonthEmissionsChangeRatio",
)
SORT_DIRECTIONS = ("Desc", "Asc")
EMISSION_SCOPES = ("Scope1", "Scope2", "Scope3")
MAX_SUBSCRIPTIONS = 100
MAX_PAGE_SIZE = 5000
DATA_READY_DAY = 19

_MONTH = re.compile(r"^\d{4}-\d{2}-01$")
_SUB_SCOPE = re.compile(r"^/subscriptions/([^/]+)(?:/resourcegroups/([^/]+))?/?$", re.IGNORECASE)
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _month(value, field):
    if isinstance(value, date):
        value = value.replace(day=1).isoformat()
    if not isinstance(value, str) or not _MONTH.match(value):
        raise ValueError(f"{field} must be the first day of a month as YYYY-MM-01")
    date.fromisoformat(value)
    return value


def _lower_list(values, field):
    if values is None:
        return None
    if isinstance(values, str):
        values = [values]
    out = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} entries must be non-empty strings")
        lowered = value.strip().lower()
        if lowered not in out:
            out.append(lowered)
    return out or None


def build_report_request(
    report_type,
    subscription_ids,
    start,
    end=None,
    *,
    scopes=EMISSION_SCOPES,
    category_type=None,
    top_items=None,
    order_by=None,
    sort_direction=None,
    page_size=None,
    skip_token=None,
    locations=None,
    resource_group_urls=None,
    resource_types=None,
):
    """Return a validated `carbonEmissionReports` request body.

    Subscription ids, locations, resource-group URLs and resource types are lowercased as the API
    requires. ItemDetails and TopItemsSummary reports cover exactly one month.
    """

    if report_type not in REPORT_TYPES:
        raise ValueError(f"report_type must be one of {', '.join(REPORT_TYPES)}")
    subs = _lower_list(subscription_ids, "subscription_ids")
    if not subs:
        raise ValueError("at least one subscription id is required")
    if len(subs) > MAX_SUBSCRIPTIONS:
        raise ValueError(f"at most {MAX_SUBSCRIPTIONS} subscriptions per request")
    start = _month(start, "start")
    end = _month(end or start, "end")
    if end < start:
        raise ValueError("end must not be before start")
    if report_type in SINGLE_MONTH_REPORTS and start != end:
        raise ValueError(f"{report_type} supports one month only; start must equal end")
    scope_list = list(dict.fromkeys(scopes or ()))
    if not scope_list or any(scope not in EMISSION_SCOPES for scope in scope_list):
        raise ValueError(f"scopes must be a non-empty subset of {', '.join(EMISSION_SCOPES)}")

    body = {
        "reportType": report_type,
        "subscriptionList": subs,
        "carbonScopeList": scope_list,
        "dateRange": {"start": start, "end": end},
    }
    if report_type in CATEGORY_REPORTS:
        if category_type not in CATEGORY_TYPES:
            raise ValueError(f"{report_type} needs category_type in {', '.join(CATEGORY_TYPES)}")
        body["categoryType"] = category_type
    elif category_type is not None:
        raise ValueError(f"{report_type} does not take category_type")
    if report_type in TOP_ITEM_REPORTS:
        if not isinstance(top_items, int) or isinstance(top_items, bool) or not 1 <= top_items <= 10:
            raise ValueError("top_items must be an integer between 1 and 10")
        body["topItems"] = top_items
    elif top_items is not None:
        raise ValueError(f"{report_type} does not take top_items")
    if report_type == "ItemDetailsReport":
        order_by = order_by or "LatestMonthEmissions"
        sort_direction = sort_direction or "Desc"
        page_size = page_size or 1000
        if order_by not in ORDER_BY_COLUMNS:
            raise ValueError(f"order_by must be one of {', '.join(ORDER_BY_COLUMNS)}")
        if sort_direction not in SORT_DIRECTIONS:
            raise ValueError("sort_direction must be Desc or Asc")
        if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be an integer between 1 and {MAX_PAGE_SIZE}")
        body.update(orderBy=order_by, sortDirection=sort_direction, pageSize=page_size)
        if skip_token:
            body["skipToken"] = skip_token
    elif any(value is not None for value in (order_by, sort_direction, page_size, skip_token)):
        raise ValueError("order_by, sort_direction, page_size and skip_token apply to ItemDetailsReport only")

    for key, values, field in (
        ("locationList", locations, "locations"),
        ("resourceGroupUrlList", resource_group_urls, "resource_group_urls"),
        ("resourceTypeList", resource_types, "resource_types"),
    ):
        lowered = _lower_list(values, field)
        if lowered:
            body[key] = lowered
    for url in body.get("resourceGroupUrlList", ()):
        if not re.match(r"^/subscriptions/[^/]+/resourcegroups/[^/]+$", url):
            raise ValueError("resource_group_urls must look like /subscriptions/<id>/resourcegroups/<name>")
    return body


def latest_complete_month(today):
    """Return the newest month whose data should be published (prior month after day 19)."""

    if isinstance(today, str):
        today = date.fromisoformat(today[:10])
    back = 1 if today.day >= DATA_READY_DAY else 2
    return _shift_month(date(today.year, today.month, 1), -back)


def _shift_month(month, delta):
    index = month.year * 12 + month.month - 1 + delta
    return date(index // 12, index % 12 + 1, 1)


def scope_filters(effective_scopes):
    """Split managed ARM scopes into subscription ids and resource-group URLs for the request."""

    subscriptions, rg_urls, unsupported = [], [], []
    whole = set()
    for scope in effective_scopes or ():
        match = _SUB_SCOPE.match(str(scope).strip())
        if not match:
            unsupported.append(scope)
            continue
        sub = match.group(1).lower()
        if sub not in subscriptions:
            subscriptions.append(sub)
        if match.group(2):
            rg_urls.append(f"/subscriptions/{sub}/resourcegroups/{match.group(2).lower()}")
        else:
            whole.add(sub)
    rg_urls = [url for url in dict.fromkeys(rg_urls) if url.split("/")[2] not in whole]
    if rg_urls and whole:
        # One resource-group filter applies to every listed subscription, so mixed
        # whole-subscription and RG-only scopes must be queried separately.
        unsupported.append("mixed whole-subscription and resource-group scopes: query them separately")
    return {"subscriptions": subscriptions, "resource_group_urls": rg_urls, "unsupported": unsupported}


def standard_requests(effective_scopes, today, *, months=12, top_items=10):
    """Return the default request set for one managed boundary, keyed by output file name."""

    if not 1 <= months <= 12:
        raise ValueError("months must be between 1 and 12 (the service keeps 12 months)")
    filters = scope_filters(effective_scopes)
    if filters["unsupported"]:
        raise ValueError(f"unsupported scopes: {filters['unsupported']}")
    if not filters["subscriptions"]:
        raise ValueError("no subscription or resource-group scopes were supplied")
    latest = latest_complete_month(today)
    first = _shift_month(latest, -(months - 1))
    common = {"resource_group_urls": filters["resource_group_urls"] or None}
    subs = filters["subscriptions"]
    month = latest.isoformat()
    return {
        "overall": build_report_request("OverallSummaryReport", subs, month, **common),
        "monthly": build_report_request("MonthlySummaryReport", subs, first.isoformat(), month, **common),
        "top-resources": build_report_request(
            "TopItemsSummaryReport", subs, month, category_type="Resource", top_items=top_items, **common
        ),
        "top-resource-types": build_report_request(
            "TopItemsSummaryReport", subs, month, category_type="ResourceType", top_items=top_items, **common
        ),
        "top-locations": build_report_request(
            "TopItemsSummaryReport", subs, month, category_type="Location", top_items=top_items, **common
        ),
    }


def human_run_script(requests, out_dir="carbon-data"):
    """Render a bash script a human runs to save each report response as `<out_dir>/<name>.json`.

    The script issues only Carbon Optimization query POSTs, which read data and change nothing.
    ItemDetailsReport pages are followed with `skipToken` and saved as `<name>-<n>.json`.
    """

    if not requests:
        raise ValueError("requests must not be empty")
    lines = [
        "#!/usr/bin/env bash",
        "# Read-only Azure Carbon Optimization queries. Needs Carbon Optimization Reader or Reader.",
        "set -euo pipefail",
        f"OUT={shlex.quote(out_dir)}",
        'mkdir -p "$OUT"',
        f'az rest --method post --url {shlex.quote(DATE_RANGE_URL)} -o json > "$OUT/available-range.json"',
    ]
    quoted_url = shlex.quote(REPORT_URL)
    for name, body in requests.items():
        if not isinstance(name, str) or not _NAME.match(name):
            raise ValueError(f"invalid request name {name!r}")
        payload = json.dumps(body, separators=(",", ":"), sort_keys=True)
        if body.get("reportType") == "ItemDetailsReport":
            lines += [
                f"BODY={shlex.quote(payload)}",
                "PAGE=1",
                "while :; do",
                f'  az rest --method post --url {quoted_url} --body "$BODY" -o json > "$OUT/{name}-$PAGE.json"',
                f'  TOKEN=$(python3 -c \'import json,sys; print(json.load(open(sys.argv[1])).get("skipToken") or "")\' "$OUT/{name}-$PAGE.json")',
                '  [ -z "$TOKEN" ] && break',
                '  BODY=$(python3 -c \'import json,sys; b=json.loads(sys.argv[1]); b["skipToken"]=sys.argv[2]; print(json.dumps(b))\' "$BODY" "$TOKEN")',
                "  PAGE=$((PAGE + 1))",
                "done",
            ]
        else:
            lines.append(
                f'az rest --method post --url {quoted_url} --body {shlex.quote(payload)} -o json > "$OUT/{name}.json"'
            )
    lines.append('echo "Saved Carbon Optimization responses to $OUT"')
    return "\n".join(lines) + "\n"


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _change(latest, previous):
    change = None if latest is None or previous is None else round(latest - previous, 4)
    pct = None if change is None or not previous else round(change / previous * 100, 2)
    return change, pct


def _item_scope_id(row):
    rid = row.get("resourceId")
    if rid:
        return str(rid).lower()
    rg_url = row.get("resourceGroupUrl")
    if rg_url:
        return str(rg_url).lower()
    sub, rg = row.get("subscriptionId"), row.get("resourceGroup")
    if sub and rg:
        return f"/subscriptions/{sub}/resourcegroups/{rg}".lower()
    if sub and row.get("categoryType") == "Subscription":
        return f"/subscriptions/{sub}".lower()
    return None


def _in_scope(scope_id, managed):
    return any(scope_id == m or scope_id.startswith(m + "/") for m in managed)


def analyze_carbon(responses, *, managed_scopes=None, increase_threshold_pct=10.0, top_n=10):
    """Summarize saved Carbon Optimization responses (one dict, a list, or a name->dict mapping).

    Returns emissions in kgCO2e: overall totals, the monthly trend, top items, the largest
    increases, subscription access decisions, and advisory hints. Items with an ARM id outside
    `managed_scopes` are excluded; items with no id (Location, ResourceType) rely on the request
    filters and are counted as `unattributed_count`.
    """

    if isinstance(responses, dict) and ("value" in responses or "subscriptionAccessDecisionList" in responses):
        responses = [responses]
    elif isinstance(responses, dict):
        responses = list(responses.values())
    managed = [str(s).rstrip("/").lower() for s in managed_scopes or () if s]
    warnings, denied, allowed = [], {}, set()
    overall_latest = overall_previous = None
    monthly, items = {}, {}
    excluded = unattributed = 0

    for response in responses or ():
        if isinstance(response, str):
            response = json.loads(response)
        if not isinstance(response, dict):
            warnings.append("Skipped a response that is not a JSON object.")
            continue
        if response.get("skipToken"):
            warnings.append("A response has a skipToken: later ItemDetails pages are missing.")
        for decision in response.get("subscriptionAccessDecisionList") or ():
            sub = str(decision.get("subscriptionId", "")).lower()
            if decision.get("decision") == "Denied":
                denied[sub] = decision.get("denialReason") or "Denied"
            elif sub:
                allowed.add(sub)
        for row in response.get("value") or ():
            data_type = str(row.get("dataType", ""))
            latest = _num(row.get("latestMonthEmissions"))
            previous = _num(row.get("previousMonthEmissions"))
            if data_type == "OverallSummaryData":
                overall_latest = (overall_latest or 0.0) + (latest or 0.0)
                overall_previous = (overall_previous or 0.0) + (previous or 0.0)
            elif data_type == "MonthlySummaryData":
                key = str(row.get("date", ""))[:10]
                entry = monthly.setdefault(key, {"date": key, "kg": 0.0, "carbon_intensity": None})
                entry["kg"] = round(entry["kg"] + (latest or 0.0), 4)
                if _num(row.get("carbonIntensity")) is not None:
                    entry["carbon_intensity"] = _num(row.get("carbonIntensity"))
            elif "ItemDetails" in data_type or "TopItems" in data_type:
                scope_id = _item_scope_id(row)
                if managed and scope_id and not _in_scope(scope_id, managed):
                    excluded += 1
                    continue
                if managed and not scope_id:
                    unattributed += 1
                category = row.get("categoryType") or ("Resource" if row.get("resourceId") else None)
                key = (category, scope_id or str(row.get("itemName", "")).lower(), row.get("date"))
                change, pct = _change(latest, previous)
                items[key] = {
                    "name": row.get("itemName"),
                    "category": category,
                    "month": str(row.get("date"))[:10] if row.get("date") else None,
                    "resource_id": row.get("resourceId"),
                    "resource_group": row.get("resourceGroup"),
                    "subscription_id": row.get("subscriptionId"),
                    "resource_type": row.get("resourceType"),
                    "location": row.get("location"),
                    "latest_kg": latest,
                    "previous_kg": previous,
                    "change_kg": change,
                    "change_pct": pct,
                }
            elif data_type:
                warnings.append(f"Ignored unknown dataType {data_type}.")

    overall = None
    if overall_latest is not None:
        change, pct = _change(overall_latest, overall_previous)
        overall = {
            "latest_kg": round(overall_latest, 4),
            "previous_kg": round(overall_previous, 4),
            "change_kg": change,
            "change_pct": pct,
        }
    single_month = [item for item in items.values() if item["month"] is None]
    by_category = {}
    for item in single_month:
        by_category.setdefault(item["category"] or "Unknown", []).append(item)
    top = {
        category: sorted(rows, key=lambda r: -(r["latest_kg"] or 0.0))[:top_n]
        for category, rows in sorted(by_category.items())
    }
    increases = sorted(
        (
            item for item in single_month
            if (item["change_kg"] or 0) > 0 and item["change_pct"] is not None
            and item["change_pct"] >= increase_threshold_pct
        ),
        key=lambda r: -r["change_kg"],
    )[:top_n]

    hints = []
    if denied:
        hints.append({
            "type": "access_denied",
            "detail": "Grant Carbon Optimization Reader (or Reader) on these subscriptions to include them.",
            "subscriptions": sorted(denied),
        })
    if overall and overall["change_pct"] is not None and overall["change_pct"] >= increase_threshold_pct:
        hints.append({
            "type": "emissions_increase",
            "detail": f"Emissions rose {overall['change_pct']}% month over month; check the largest increases.",
        })
    resources = top.get("Resource") or []
    if overall and overall["latest_kg"] and resources:
        share = round(sum(r["latest_kg"] or 0 for r in resources) / overall["latest_kg"] * 100, 1)
        hints.append({
            "type": "concentration",
            "detail": f"The top {len(resources)} resources account for {share}% of emissions; "
                      "rightsizing and waste findings on them reduce both cost and carbon.",
        })
    if not (overall or monthly or items):
        warnings.append("No emissions rows were returned; check access and the available date range.")

    return {
        "unit": UNIT,
        "overall": overall,
        "monthly": [monthly[k] for k in sorted(monthly)],
        "top_items": top,
        "increases": increases,
        "access": {
            "allowed": sorted(allowed - set(denied)),
            "denied": [{"subscriptionId": sub, "reason": reason} for sub, reason in sorted(denied.items())],
        },
        "excluded_count": excluded,
        "unattributed_count": unattributed,
        "hints": hints,
        "warnings": list(dict.fromkeys(warnings)),
    }
