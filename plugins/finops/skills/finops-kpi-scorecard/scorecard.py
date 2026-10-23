"""FinOps forecast, unit economics, KPI scorecard and owner routing (skill: finops-kpi-scorecard).

Pure, dependency-free, deterministic logic (no Azure calls) that the agent runs in the sandbox
after the other FinOps skills have produced their results. It covers the FinOps Framework
"Quantify business value" and "Manage the FinOps practice" domains:

  * daily_costs / forecast_spend  month-end and next-month forecast with a confidence band
  * unit_economics                cost per business unit (request, order, user, GB...) and trend
  * kpi_scorecard                 pass/warn/fail FinOps KPIs, including tag and budget governance
                                  checks, with an overall Crawl/Walk/Run maturity indication
  * route_to_owners               group findings (waste, rightsizing, anomalies...) by the
                                  accountable owner tag so each team gets its own action list

Inputs are the outputs of the sibling skills (allocate_costs, find_waste, recommend_rightsizing,
analyze_commitments, evaluate_budgets, detect_anomalies) plus UsageDetails rows already prepared
and scope-filtered with finops-managed-scope `prepare_usage_details`. Every input is optional: a
missing source makes the related KPI "unknown" instead of failing the scorecard.
"""

from __future__ import annotations

import calendar
import html
import json
import math
import re
from collections import defaultdict
from datetime import date, timedelta

# FinOps Foundation-aligned default targets. Override any of them with `targets={...}`.
DEFAULT_TARGETS = {
    "allocated_pct": {"target": 90.0, "warn": 75.0, "higher_is_better": True},
    "untagged_pct": {"target": 5.0, "warn": 15.0, "higher_is_better": False},
    "tag_hygiene_pct": {"target": 2.0, "warn": 5.0, "higher_is_better": False},
    "budget_coverage_pct": {"target": 100.0, "warn": 50.0, "higher_is_better": True},
    "budgets_over": {"target": 0, "warn": 1, "higher_is_better": False},
    "forecast_vs_budget_pct": {"target": 100.0, "warn": 110.0, "higher_is_better": False},
    "commitment_coverage_pct": {"target": 70.0, "warn": 50.0, "higher_is_better": True},
    "commitment_utilization_pct": {"target": 95.0, "warn": 80.0, "higher_is_better": True},
    "waste_pct": {"target": 5.0, "warn": 10.0, "higher_is_better": False},
    "anomaly_impact_pct": {"target": 1.0, "warn": 5.0, "higher_is_better": False},
    "unit_cost_change_pct": {"target": 0.0, "warn": 10.0, "higher_is_better": False},
    # Microsoft FinOps forecasting guidance: variance under 12% is optimized, 12-20% acceptable.
    "forecast_accuracy_pct": {"target": 12.0, "warn": 20.0, "higher_is_better": False},
}

OWNER_TAG_KEYS = ("owner", "team", "costCenter", "service", "app")


def _parse_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _pct(part, whole):
    if not whole:
        return None
    return round(100.0 * part / whole, 1)


# --------------------------------------------------------------------------------------------
# Forecast
# --------------------------------------------------------------------------------------------

def daily_costs(rows, *, include_credits: bool = True, exclude_purchases: bool = False):
    """Sum prepared UsageDetails rows into {date: cost}. Rows without a date or cost are skipped.

    With include_credits=False negative rows (refunds, credits) are ignored, which is what a usage
    forecast wants; the default keeps them so totals reconcile with the invoice.
    With exclude_purchases=True rows whose chargeType is "Purchase" (reservation, savings plan and
    Marketplace purchases) are dropped, so one-time purchases do not skew a usage trend; forecast
    them separately, as Microsoft's forecasting guidance recommends.
    """
    totals = defaultdict(float)
    for row in rows or ():
        if not isinstance(row, dict) or not row.get("date"):
            continue
        if exclude_purchases and str(row.get("chargeType") or "").strip().casefold() == "purchase":
            continue
        cost = _num(row.get("cost"))
        if cost is None or (cost < 0 and not include_credits):
            continue
        totals[_parse_date(row["date"])] += cost
    return dict(sorted(totals.items()))


def _linear_fit(points):
    """Ordinary least squares y = a + b*x. Returns (a, b, residual_std)."""
    n = len(points)
    if n == 1:
        return points[0][1], 0.0, 0.0
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in points)
    slope = (sum((x - mean_x) * (y - mean_y) for x, y in points) / sxx) if sxx else 0.0
    intercept = mean_y - slope * mean_x
    residuals = [y - (intercept + slope * x) for x, y in points]
    dof = max(n - 2, 1)
    std = math.sqrt(sum(r * r for r in residuals) / dof)
    return intercept, slope, std


def _weekday_factors(history, intercept, slope, origin):
    """Average ratio of actual to trend per weekday (needs 2+ samples), normalized to mean 1."""
    ratios = defaultdict(list)
    for day, value in history.items():
        trend = intercept + slope * (day - origin).days
        if trend > 0:
            ratios[day.weekday()].append(value / trend)
    factors = {}
    for weekday in range(7):
        samples = ratios.get(weekday, [])
        factors[weekday] = sum(samples) / len(samples) if len(samples) >= 2 else 1.0
    mean = sum(factors.values()) / 7
    return {k: (v / mean if mean else 1.0) for k, v in factors.items()}


def forecast_spend(
    daily,
    *,
    as_of=None,
    history_days: int = 60,
    assume_last_partial: bool = True,
    budget_amount=None,
    z: float = 1.2816,
):
    """Forecast month-end and next-month spend from daily cost.

    daily               {date|str: cost} (use daily_costs(rows) on AmortizedCost rows)
    as_of               last complete day to anchor on; defaults to the newest complete day
    history_days        trailing window used to fit the trend
    assume_last_partial drop the newest day (Cost Management data lags 1-2 days)
    budget_amount       optional monthly budget; adds forecast_vs_budget_pct and budget_status
    z                   band width; 1.2816 = 80% two-sided interval

    Method: linear trend (OLS) over the trailing window, with day-of-week seasonality when at least
    28 days exist; a flat trailing average below 7 days. The band is z * residual standard
    deviation * sqrt(days forecast). Month-end forecast = month-to-date actual + forecast of the
    remaining days of the month.
    """
    series = {_parse_date(k): float(v) for k, v in (daily or {}).items() if _num(v) is not None}
    if not series:
        return {"available": False, "reason": "no daily cost history", "warnings": []}

    days_sorted = sorted(series)
    if as_of is None:
        last = days_sorted[-2] if assume_last_partial and len(days_sorted) >= 2 else days_sorted[-1]
    else:
        last = _parse_date(as_of)
    first = max(last - timedelta(days=history_days - 1), days_sorted[0])
    # Fill gaps with zero inside the observed range: a missing day in UsageDetails means no spend.
    history = {}
    day = first
    while day <= last:
        history[day] = series.get(day, 0.0)
        day += timedelta(days=1)
    n = len(history)
    warnings = []
    if n < 14:
        warnings.append(f"only {n} days of history; forecast confidence is low")

    points = [((d - first).days, v) for d, v in history.items()]
    flat = {k: 1.0 for k in range(7)}
    if n >= 28:
        intercept, slope, std = _linear_fit(points)
        factors = _weekday_factors(history, intercept, slope, first)
        residuals = [v - (intercept + slope * (d - first).days) * factors[d.weekday()]
                     for d, v in history.items()]
        std = math.sqrt(sum(r * r for r in residuals) / max(n - 2, 1))
        method = "linear_trend_weekday_seasonality"
    elif n >= 7:
        intercept, slope, std = _linear_fit(points)
        factors = flat
        method = "linear_trend"
    else:
        mean = sum(history.values()) / n
        intercept, slope = mean, 0.0
        std = math.sqrt(sum((v - mean) ** 2 for v in history.values()) / max(n - 1, 1))
        factors = flat
        method = "trailing_average"

    def predict(target_day):
        x = (target_day - first).days
        return max(0.0, (intercept + slope * x) * factors[target_day.weekday()])

    month_start = last.replace(day=1)
    month_end = last.replace(day=calendar.monthrange(last.year, last.month)[1])
    mtd = sum(v for d, v in series.items() if month_start <= d <= last)
    remaining = [last + timedelta(days=i) for i in range(1, (month_end - last).days + 1)]
    month_forecast = mtd + sum(predict(d) for d in remaining)
    band = z * std * math.sqrt(len(remaining)) if remaining else 0.0

    next_start = month_end + timedelta(days=1)
    next_days = calendar.monthrange(next_start.year, next_start.month)[1]
    next_forecast = sum(predict(next_start + timedelta(days=i)) for i in range(next_days))
    next_band = z * std * math.sqrt(next_days)

    trailing = [history[d] for d in sorted(history)[-30:]]
    run_rate = sum(trailing) / len(trailing) * 30 if trailing else 0.0

    out = {
        "available": True,
        "as_of": last.isoformat(),
        "method": method,
        "history_days": n,
        "daily_trend_slope": round(slope, 4),
        "month": last.strftime("%Y-%m"),
        "month_to_date": round(mtd, 2),
        "days_remaining": len(remaining),
        "month_end_forecast": round(month_forecast, 2),
        "month_end_low": round(max(mtd, month_forecast - band), 2),
        "month_end_high": round(month_forecast + band, 2),
        "next_month": next_start.strftime("%Y-%m"),
        "next_month_forecast": round(next_forecast, 2),
        "next_month_low": round(max(0.0, next_forecast - next_band), 2),
        "next_month_high": round(next_forecast + next_band, 2),
        "run_rate_30d": round(run_rate, 2),
        "confidence": "low" if n < 14 else ("medium" if n < 45 else "high"),
        "warnings": warnings,
    }
    budget = _num(budget_amount)
    if budget:
        out["budget_amount"] = round(budget, 2)
        out["forecast_vs_budget_pct"] = round(100.0 * month_forecast / budget, 1)
        if month_forecast - band > budget:
            out["budget_status"] = "forecast_over"
        elif month_forecast + band > budget:
            out["budget_status"] = "at_risk"
        else:
            out["budget_status"] = "on_track"
    return out


# --------------------------------------------------------------------------------------------
# Forecast accuracy inputs
# --------------------------------------------------------------------------------------------

FORECAST_RECORD_PREFIX = "finops-forecast-record:v1"
_RECORD_RE = re.compile(
    re.escape(FORECAST_RECORD_PREFIX)
    + r"\s+month=(\d{4}-\d{2})\s+forecast=(-?\d+(?:\.\d+)?)(?:\s+as_of=(\d{4}-\d{2}-\d{2}))?"
)


def parse_forecast_record(text, month):
    """Find the forecast recorded for `month` (YYYY-MM) in a previous Monthly Review.

    The Monthly Review Live Report carries one line
    `finops-forecast-record:v1 month=YYYY-MM forecast=<USD> as_of=YYYY-MM-DD` for the month after
    the one it reviewed. Pass the previous report's content (HTML or text, as GetReport returns it).
    Returns {"month", "forecast", "as_of"} or None. The last matching record wins.
    """
    if not isinstance(text, str) or not text:
        return None
    found = None
    for match in _RECORD_RE.finditer(html.unescape(text)):
        if match.group(1) == month:
            found = {"month": month, "forecast": float(match.group(2)), "as_of": match.group(3)}
    return found


def prior_forecast_inputs(daily, month, *, record_text=None):
    """Return prior_forecast and actual_spend for scoring forecast accuracy of a closed month.

    daily        {date|str: cost} covering the closed month and the month before it
    month        the closed month, YYYY-MM
    record_text  the previous Monthly Review content; its forecast record for `month` is the
                 forecast that was published at the time (source "recorded")

    Without a record, the forecast is reconstructed by running forecast_spend on the days before
    the month (source "reconstructed"). That measures the forecasting method, not a published
    forecast, and is labeled so.
    """
    series = {_parse_date(k): float(v) for k, v in (daily or {}).items() if _num(v) is not None}
    start = _parse_date(f"{month}-01")
    end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
    in_month = {d: v for d, v in series.items() if start <= d <= end}
    warnings = []
    out = {"month": month, "prior_forecast": None, "actual_spend": None, "source": None,
           "warnings": warnings}
    if not in_month:
        warnings.append(f"no cost data for {month}; forecast accuracy not scored")
        return out
    out["actual_spend"] = round(sum(in_month.values()), 2)
    if max(in_month) < end:
        warnings.append(f"cost data for {month} ends {max(in_month).isoformat()}; actual may be low")

    record = parse_forecast_record(record_text, month)
    if record:
        out.update(prior_forecast=round(record["forecast"], 2), source="recorded")
        return out

    before = {d: v for d, v in series.items() if d < start}
    if len(before) < 7:
        warnings.append(f"no forecast record and under 7 days before {month}; accuracy not scored")
        return out
    rebuilt = forecast_spend(before, as_of=start - timedelta(days=1), assume_last_partial=False)
    if rebuilt.get("available") and rebuilt.get("next_month") == month:
        out.update(prior_forecast=rebuilt["next_month_forecast"], source="reconstructed")
        warnings.append(
            f"no forecast record for {month}; reconstructed from {len(before)} days before it "
            "(tests the method, not the forecast published at the time)"
        )
    return out


# --------------------------------------------------------------------------------------------
# Unit economics
# --------------------------------------------------------------------------------------------

def unit_economics(cost, units, *, unit_name="unit", per: float = 1.0, previous_cost=None,
                   previous_units=None, by_owner=None):
    """Cost per business unit and its trend.

    cost            total cost for the period (same scope as `units`)
    units           business volume for the same period (requests, orders, active users, GB...)
    unit_name       label, e.g. "request" or "order"
    per             scale, e.g. 1000 to report cost per 1,000 requests
    previous_cost / previous_units   prior period, to report the change
    by_owner        optional {owner: {"cost": x, "units": y}} for per-product/team unit costs

    A falling unit cost while total cost rises is healthy growth; a rising unit cost is the signal
    to investigate (efficiency regression, over-provisioning, or a pricing change).
    """
    def ratio(c, u):
        c, u = _num(c), _num(u)
        if c is None or not u or u <= 0:
            return None
        return c / u * per

    current = ratio(cost, units)
    out = {
        "unit": unit_name,
        "per": per,
        "cost": round(_num(cost) or 0.0, 2),
        "units": _num(units),
        "cost_per_unit": round(current, 6) if current is not None else None,
        "previous_cost_per_unit": None,
        "change_pct": None,
        "trend": "unknown",
        "interpretation": None,
        "warnings": [],
    }
    if current is None:
        out["warnings"].append("units missing or zero; cost per unit cannot be computed")
    previous = ratio(previous_cost, previous_units)
    if previous is not None:
        out["previous_cost_per_unit"] = round(previous, 6)
        if current is not None and previous > 0:
            change = round(100.0 * (current - previous) / previous, 1)
            out["change_pct"] = change
            out["trend"] = "improving" if change < -1 else ("worsening" if change > 1 else "flat")
            if change > 1:
                out["interpretation"] = "unit cost rose; investigate efficiency or pricing changes"
            elif (_num(cost) or 0.0) > (_num(previous_cost) or 0.0):
                out["interpretation"] = (
                    "spend grew with volume while unit cost held or fell (healthy growth)"
                )
    if by_owner:
        rows = []
        for owner, data in by_owner.items():
            data = data or {}
            value = ratio(data.get("cost"), data.get("units"))
            rows.append({
                "owner": owner,
                "cost": round(_num(data.get("cost")) or 0.0, 2),
                "units": _num(data.get("units")),
                "cost_per_unit": round(value, 6) if value is not None else None,
            })
        rows.sort(key=lambda r: (r["cost_per_unit"] is None, -(r["cost_per_unit"] or 0.0)))
        out["by_owner"] = rows
    return out


# --------------------------------------------------------------------------------------------
# Owner routing
# --------------------------------------------------------------------------------------------

def _tags_dict(raw):
    """Accept a tag dict, a JSON object string, or Azure's bare '"k": "v", ...' string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        if not text.startswith("{"):
            text = "{" + text + "}"
        try:
            parsed = json.loads(text)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _owner_of(tags, owner_keys):
    ci = {str(k).strip().lower(): v for k, v in _tags_dict(tags).items()}
    for key in owner_keys:
        value = ci.get(str(key).strip().lower())
        if value is not None and str(value).strip():
            return str(key), str(value).strip()
    return None, None


def _finding_amount(finding):
    for key in ("estMonthlySavingsUsd", "estMonthlySavings", "impact_usd", "monthly_usd", "cost"):
        value = _num(finding.get(key))
        if value is not None:
            return value
    return None


def route_to_owners(findings, *, tags=None, owner_keys=OWNER_TAG_KEYS, source=None):
    """Group findings by accountable owner so each team gets one action list.

    findings    iterable of dicts with a resourceId (or `value` for anomalies) and an amount key
                (estMonthlySavingsUsd, estMonthlySavings, impact_usd, monthly_usd or cost)
    tags        {resourceId: {tag: value}}; a finding's own `tags` wins when present
    owner_keys  tag keys checked in order; the first non-empty value is the owner
    source      optional label stamped on each routed item (e.g. "waste")

    Findings without an owner land in `unrouted`, itself a governance action: tag them.
    """
    tag_index = {str(k).strip().lower(): v for k, v in (tags or {}).items()}
    groups = {}
    unrouted = []
    for finding in findings or ():
        if not isinstance(finding, dict):
            continue
        rid = finding.get("resourceId") or finding.get("value") or ""
        own_tags = finding.get("tags") or tag_index.get(str(rid).strip().lower())
        key, owner = _owner_of(own_tags, owner_keys)
        item = {
            "resourceId": rid,
            "source": source or finding.get("source") or finding.get("kind") or finding.get("category"),
            "amount_usd": _finding_amount(finding),
            "action": finding.get("action") or finding.get("recommendation") or finding.get("reason"),
        }
        if owner is None:
            unrouted.append(item)
            continue
        group = groups.setdefault(
            owner.lower(), {"owner": owner, "owner_tag": key, "total_usd": 0.0, "items": []}
        )
        group["items"].append(item)
        group["total_usd"] += item["amount_usd"] or 0.0

    def order(i):
        return (i["amount_usd"] is None, -(i["amount_usd"] or 0.0))

    owners = []
    for group in groups.values():
        group["items"].sort(key=order)
        group["total_usd"] = round(group["total_usd"], 2)
        group["count"] = len(group["items"])
        owners.append(group)
    owners.sort(key=lambda g: g["total_usd"], reverse=True)
    unrouted.sort(key=order)
    routed_total = sum(g["total_usd"] for g in owners)
    unrouted_total = round(sum(i["amount_usd"] or 0.0 for i in unrouted), 2)
    return {
        "owner_keys": list(owner_keys),
        "owners": owners,
        "unrouted": unrouted,
        "unrouted_usd": unrouted_total,
        "routed_pct": _pct(routed_total, routed_total + unrouted_total),
    }


# --------------------------------------------------------------------------------------------
# KPI scorecard
# --------------------------------------------------------------------------------------------

def _status(value, rule):
    if value is None:
        return "unknown"
    target, warn = rule["target"], rule["warn"]
    if rule["higher_is_better"]:
        if value >= target:
            return "pass"
        return "warn" if value >= warn else "fail"
    if value <= target:
        return "pass"
    return "warn" if value <= warn else "fail"


def _scope_covered(scope, budget_scopes):
    """True when a budget sits at the scope or at an ancestor of it (case-insensitive ARM ids)."""
    scope = str(scope or "").strip().lower().rstrip("/")
    for budget_scope in budget_scopes:
        candidate = str(budget_scope or "").strip().lower().rstrip("/")
        if candidate and (scope == candidate or scope.startswith(candidate + "/")):
            return True
    return False


def kpi_scorecard(
    *,
    allocation=None,
    budgets=None,
    commitments=None,
    waste=None,
    rightsizing=None,
    anomalies=None,
    forecast=None,
    unit_costs=None,
    managed_scopes=None,
    targets=None,
    as_of=None,
    prior_forecast=None,
    actual_spend=None,
    advisor_cost_score=None,
    prior_forecast_source=None,
):
    """Score the FinOps practice against KPI targets, including tag and budget governance checks.

    allocation     allocate_costs() result        -> allocated %, untagged %, tag hygiene
    budgets        evaluate_budgets() result      -> budget coverage of managed scopes, overruns
    commitments    analyze_commitments() result   -> coverage % and utilization %
    waste          find_waste() result            -> waste % of spend (with rightsizing)
    rightsizing    recommend_rightsizing() list
    anomalies      detect_anomalies() list        -> unexplained spike impact % of daily spend
    forecast       forecast_spend() result        -> month-end forecast vs budget
    unit_costs     unit_economics() result, or a list of them -> unit cost trend
    managed_scopes validated managed scopes; a scope with no budget at or above it fails the
                   budget-coverage governance check
    targets        overrides for DEFAULT_TARGETS, e.g. {"allocated_pct": {"target": 95}}
    prior_forecast the month-end forecast made for a closed period (e.g. last month's
                   month_end_forecast); with actual_spend it scores forecast accuracy
    actual_spend   the actual cost of that same closed period
    prior_forecast_source  "recorded" or "reconstructed" (see prior_forecast_inputs); shown in
                   the KPI detail
    advisor_cost_score  Azure Advisor Cost score (0-100) for the scope; reported as a benchmark,
                   not scored, because Microsoft publishes no target for it

    Each KPI: {id, name, capability, value, unit, target, higher_is_better, status, action,
    detail}. status is pass | warn | fail | unknown (source not provided). The maturity indication
    is "run" when >= 80% of known KPIs pass, "walk" when >= 50%, otherwise "crawl".
    `benchmarks` lists unscored context values (anomaly count and impact, Advisor cost score).
    `next_iteration_focus` lists up to 3 capabilities with failing, then warning, KPIs: the
    candidates for the next FinOps iteration (Microsoft recommends a small set per iteration).
    """
    rules = {k: dict(v) for k, v in DEFAULT_TARGETS.items()}
    for key, override in (targets or {}).items():
        if key in rules and isinstance(override, dict):
            rules[key].update(override)

    alloc = allocation or {}
    total = _num(alloc.get("total_usd"))
    if total is None and forecast and forecast.get("available"):
        total = _num(forecast.get("run_rate_30d"))
    kpis = []

    def add(kpi_id, name, capability, value, unit, action, detail=None):
        rule = rules[kpi_id]
        status = _status(value, rule)
        kpis.append({
            "id": kpi_id,
            "name": name,
            "capability": capability,
            "value": value,
            "unit": unit,
            "target": rule["target"],
            "higher_is_better": rule["higher_is_better"],
            "status": status,
            "action": action if status in ("warn", "fail") else None,
            "detail": detail,
        })

    # Allocation and tag governance
    allocated_pct = untagged_pct = hygiene_pct = None
    if allocation is not None:
        if alloc.get("unallocated_pct") is not None:
            allocated_pct = round(100.0 - float(alloc["unallocated_pct"]), 1)
        if total:
            untagged_pct = _pct(_num(alloc.get("untagged_usd")) or 0.0, total)
            hygiene = sum(_num(h.get("cost_affected")) or 0.0 for h in alloc.get("tag_hygiene") or [])
            hygiene_pct = _pct(hygiene, total)
    add("allocated_pct", f"Spend allocated by '{alloc.get('dimension', 'owner')}' tag", "Allocation",
        allocated_pct, "%",
        "Tag the top unallocated resources, or enable tag inheritance from resource groups and "
        "subscriptions in Cost Management.")
    add("untagged_pct", "Spend on resources with no tags", "Allocation (tag governance)",
        untagged_pct, "%",
        "Apply the recommended tags (team, env, costCenter, owner) and enforce them with Azure "
        "Policy (require, or inherit from the resource group).")
    add("tag_hygiene_pct", "Spend split by inconsistent tag values", "Allocation (tag governance)",
        hygiene_pct, "%",
        "Normalize tag values (e.g. 'Prod' vs 'production') and restrict allowed values with Azure Policy.")

    # Budget governance
    uncovered = []
    coverage = over = None
    if budgets is not None:
        budget_list = budgets.get("budgets") or []
        if managed_scopes:
            budget_scopes = [b.get("scope") for b in budget_list]
            uncovered = [s for s in managed_scopes if not _scope_covered(s, budget_scopes)]
            coverage = round(100.0 * (len(managed_scopes) - len(uncovered)) / len(managed_scopes), 1)
        else:
            coverage = 100.0 if budget_list else 0.0
        summary = budgets.get("summary") or {}
        over = (summary.get("over_budget") or 0) + (summary.get("forecast_over") or 0)
    add("budget_coverage_pct", "Managed scopes with a budget", "Budgeting (governance)", coverage, "%",
        "Create a budget with actual and forecast alerts for every uncovered scope "
        "(finops-budget-editor can draft it).",
        {"uncovered_scopes": uncovered} if uncovered else None)
    add("budgets_over", "Budgets over or forecast over", "Budgeting", over, "count",
        "Review the overrunning scopes with their owners; reduce spend or re-baseline the budget.")

    # Forecast vs budget
    fvb = None
    if forecast and forecast.get("available"):
        fvb = _num(forecast.get("forecast_vs_budget_pct"))
        if fvb is None and budgets is not None:
            amount = _num((budgets.get("summary") or {}).get("total_amount"))
            if amount:
                fvb = round(100.0 * forecast["month_end_forecast"] / amount, 1)
    add("forecast_vs_budget_pct", "Month-end forecast as % of budget", "Forecasting", fvb, "%",
        "Forecast exceeds budget: act on the top optimization priorities now or re-plan the budget.")

    accuracy = None
    prior, actual = _num(prior_forecast), _num(actual_spend)
    if prior is not None and actual:
        accuracy = round(100.0 * abs(prior - actual) / abs(actual), 1)
    accuracy_detail = None
    if accuracy is not None:
        accuracy_detail = {"prior_forecast_usd": prior, "actual_usd": actual}
        if prior_forecast_source:
            accuracy_detail["source"] = str(prior_forecast_source)
    add("forecast_accuracy_pct", "Forecast variance vs actual (closed period)", "Forecasting",
        accuracy, "%",
        "Forecast variance is high: exclude one-time purchases from the usage trend, match the "
        "look-back period to the forecast horizon, and record planned changes in the forecast.",
        accuracy_detail)

    # Rate optimization
    head = (commitments or {}).get("headline") or {}
    add("commitment_coverage_pct", "Eligible compute covered by commitments", "Rate optimization",
        _num(head.get("coverage_pct")), "%",
        "Review finops-commitment-advisor purchase recommendations after rightsizing.")
    utils = []
    for item in (commitments or {}).get("inventory") or []:
        value = _num(item.get("utilization_30d"))
        if value is None:
            value = _num(item.get("utilization_7d"))
        if value is not None:
            utils.append(value)
    add("commitment_utilization_pct", "Average commitment utilization (30d)", "Rate optimization",
        round(sum(utils) / len(utils), 1) if utils else None, "%",
        "Exchange or re-scope under-utilized reservations, or move eligible workloads onto them.")

    # Usage optimization (waste + rightsizing)
    waste_usd = None
    if waste is not None or rightsizing is not None:
        waste_usd = _num(((waste or {}).get("headline") or {}).get("potential_monthly_savings")) or 0.0
        waste_usd += sum(_num(r.get("estMonthlySavingsUsd")) or 0.0 for r in rightsizing or [])
    add("waste_pct", "Addressable waste as % of spend", "Workload optimization",
        _pct(waste_usd, total) if (waste_usd is not None and total) else None, "%",
        "Work the waste and rightsizing lists by owner (route_to_owners).",
        {"monthly_usd": round(waste_usd, 2)} if waste_usd is not None else None)

    # Anomaly management: only cost increases count against the KPI.
    impact = None
    if anomalies is not None:
        impact = sum(_num(a.get("impact_usd")) or 0.0 for a in anomalies
                     if a.get("direction", "up") == "up")
    daily_spend = (total / 30.0) if total else None
    add("anomaly_impact_pct", "Unexplained spike impact as % of daily spend", "Anomaly management",
        _pct(impact, daily_spend) if (impact is not None and daily_spend) else None, "%",
        "Investigate open spikes (finops-cost-anomaly-detection) and record the cause.")

    # Unit economics: the worst (highest) change across the supplied unit metrics.
    units = unit_costs if isinstance(unit_costs, list) else ([unit_costs] if unit_costs else [])
    changes = [_num(u.get("change_pct")) for u in units if _num(u.get("change_pct")) is not None]
    add("unit_cost_change_pct", "Cost per business unit, change vs previous period", "Unit economics",
        max(changes) if changes else None, "%",
        "Unit cost is rising: check whether efficiency regressed or prices changed.")

    known = [k for k in kpis if k["status"] != "unknown"]
    passed = sum(1 for k in known if k["status"] == "pass")
    score = round(100.0 * passed / len(known), 1) if known else None
    if score is None:
        maturity = "unknown"
    elif score >= 80:
        maturity = "run"
    elif score >= 50:
        maturity = "walk"
    else:
        maturity = "crawl"
    severity = {"fail": 0, "warn": 1}
    actions = [
        {"kpi": k["id"], "status": k["status"], "action": k["action"]}
        for k in sorted(kpis, key=lambda k: severity.get(k["status"], 2))
        if k["action"]
    ]
    focus = []
    for k in sorted(kpis, key=lambda k: severity.get(k["status"], 2)):
        capability = k["capability"].split(" (")[0]
        if k["status"] in severity and capability not in focus:
            focus.append(capability)
    benchmarks = []
    if anomalies is not None:
        ups = [a for a in anomalies if a.get("direction", "up") == "up"]
        benchmarks.append({"id": "anomaly_count", "name": "Cost anomalies detected (increases)",
                           "value": len(ups), "unit": "count"})
        benchmarks.append({"id": "anomaly_impact_usd", "name": "Cost impact of anomalies",
                           "value": round(impact or 0.0, 2), "unit": "USD"})
    score_value = _num(advisor_cost_score)
    if score_value is not None:
        benchmarks.append({"id": "advisor_cost_score", "name": "Azure Advisor Cost score",
                           "value": round(score_value, 1), "unit": "%"})
    if isinstance(as_of, date):
        as_of_text = as_of.isoformat()
    else:
        as_of_text = str(as_of) if as_of else date.today().isoformat()
    return {
        "as_of": as_of_text,
        "score_pct": score,
        "maturity": maturity,
        "counts": {s: sum(1 for k in kpis if k["status"] == s)
                   for s in ("pass", "warn", "fail", "unknown")},
        "kpis": kpis,
        "governance_checks": [
            {"id": k["id"], "name": k["name"], "status": k["status"], "value": k["value"],
             "target": k["target"], "detail": k["detail"]}
            for k in kpis if "governance" in k["capability"]
        ],
        "uncovered_budget_scopes": uncovered,
        "actions": actions,
        "next_iteration_focus": focus[:3],
        "benchmarks": benchmarks,
    }
