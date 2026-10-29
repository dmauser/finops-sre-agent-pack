"""FinOps budget-governance — pure, offline, deterministic (no Azure calls).

Given the budgets returned by the read-only `GET Microsoft.Consumption/budgets` API,
evaluate each one against its amount: how much is spent month-to-date, where it is
forecast to land, which of the budget's own notification thresholds are breached, and
whether spend warrants a process gate (a human review before more is spent).

Design decisions (see SKILL.md):
  * Read-only. This never creates or edits a budget (that is the separate, planned
    `budget-editor` write skill). It only reads and reports.
  * Azure's own `forecastSpend` is used when present, but it is frequently absent on the
    GET response. When it is missing we compute a **linear run-rate** forecast in-sandbox
    (spend-so-far extrapolated across the current time-grain window) and label the source,
    when current spend and an active calendar window are known.
  * The budget's own `notifications` (each with a percent `threshold` and a `thresholdType`
    of Actual or Forecasted) are the customer's declared governance intent — we evaluate
    those directly rather than inventing configured alerts. Analytical thresholds are separate.
  * Empty input is a first-class result: "no budgets defined" is itself the finding (with a
    pointer to create one), not an error.

Input shape (the `value[]` array straight from the budgets GET; only these fields are read):

    [
      {
        "name": "monthly-sub",
        "id": ".../providers/Microsoft.Consumption/budgets/monthly-sub",
        "properties": {
          "amount": 1000.0,
          "category": "Cost",
          "timeGrain": "Monthly",
          "timePeriod": {"startDate": "2026-01-01T00:00:00Z", "endDate": "2027-01-01T00:00:00Z"},
          "currentSpend": {"amount": 640.0, "unit": "USD"},
          "forecastSpend": {"amount": 1120.0, "unit": "USD"},   # often absent
          "notifications": {
            "actual_80": {"enabled": true, "threshold": 80, "thresholdType": "Actual", "operator": "GreaterThan"},
            "fcst_100":  {"enabled": true, "threshold": 100, "thresholdType": "Forecasted", "operator": "GreaterThan"}
          }
        }
      }
    ]

Output: a single dict (see evaluate_budgets) — a per-budget evaluation, a portfolio summary,
and a `gates` list of the budgets that need a human decision. Feed it to a table / report.
"""

from collections import defaultdict
from copy import deepcopy
from datetime import date
import math

AT_RISK_PCT = 80.0                   # portfolio "at risk" band

# Calendar months per time-grain window. Windows are anchored at timePeriod.startDate (Azure
# requires budgets to start on the first of a month) and use real day counts.
_GRAIN_MONTHS = {
    "monthly": 1,
    "billingmonth": 1,
    "quarterly": 3,
    "billingquarter": 3,
    "annually": 12,
    "billingannual": 12,
}


def _num(value):
    """Return value as float, or None if it isn't a real number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _spend_amount(block):
    """Read a {'amount': .., 'unit': ..} spend block; return (amount|None, unit|None)."""
    if not isinstance(block, dict):
        return None, None
    return _num(block.get("amount")), block.get("unit")


def _pct(part, whole):
    """part/whole as a rounded percentage; None when whole is missing/zero."""
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def _parse_date(text):
    """Parse an ISO-ish date ('YYYY-MM-DD' or full timestamp) to date; None on failure."""
    if not text:
        return None
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def _add_months(day, months):
    """Shift a first-of-month date by whole months."""
    total = day.year * 12 + (day.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def _grain_window(time_grain, time_period, as_of):
    """Return the (start, end_exclusive) budget reset window that contains `as_of`.

    Recurring windows are anchored at timePeriod.startDate (first of a month) and step by
    the grain's calendar months, so July has 31 days, Q1 has 90/91, and a quarterly budget
    starting in February resets Feb/May/Aug/Nov. Without a startDate the window aligns to the
    calendar (month, quarter, or year). A window is clipped to timePeriod.endDate. Returns
    None for unknown grains or when `as_of` is outside the budget's active period.
    """
    grain = str(time_grain or "").strip().lower()
    # Billing periods do not necessarily align with calendar months.
    if grain.startswith("billing"):
        return None
    months = _GRAIN_MONTHS.get(grain)
    if months is None:
        return None
    start = _parse_date((time_period or {}).get("startDate"))
    end = _parse_date((time_period or {}).get("endDate"))
    if any((time_period or {}).get(k) and parsed is None
           for k, parsed in (("startDate", start), ("endDate", end))):
        return None
    if start is not None:
        anchor = date(start.year, start.month, 1)
        if as_of < start:
            return None
    else:
        anchor = date(as_of.year, ((as_of.month - 1) // months) * months + 1, 1)
    elapsed_months = (as_of.year - anchor.year) * 12 + (as_of.month - anchor.month)
    window_start = _add_months(anchor, (elapsed_months // months) * months)
    window_end = _add_months(window_start, months)
    if start is not None and start > window_start:
        window_start = start
    if end is not None:
        if as_of >= end:
            return None
        window_end = min(window_end, end)
    return window_start, window_end


def _grain_fraction_elapsed(time_grain, time_period, as_of):
    """Fraction (0, 1] of the current time-grain window that has elapsed as of `as_of`.

    Counts `as_of` as an elapsed day and uses the real calendar length of the window.
    Returns None if the window can't be determined.
    """
    window = _grain_window(time_grain, time_period, as_of)
    if window is None:
        return None
    window_start, window_end = window
    days_in_window = (window_end - window_start).days
    if days_in_window <= 0:
        return None
    elapsed = (as_of - window_start).days + 1
    elapsed = max(1, min(elapsed, days_in_window))
    return elapsed / days_in_window


def _notifications(props):
    """Yield (name, threshold_pct, thresholdType_lower, enabled) for each notification."""
    for name, n in (props.get("notifications") or {}).items():
        if not isinstance(n, dict):
            continue
        threshold = _num(n.get("threshold"))
        if threshold is None:
            continue
        ttype = str(n.get("thresholdType") or "Actual").strip().lower()
        enabled = n.get("enabled", True) is not False
        yield name, threshold, ttype, enabled


def evaluate_budgets(budgets=None, *, as_of=None, mtd_spend=None):
    """Evaluate read-only Azure budgets against amount, forecast, and their own thresholds.

    budgets    the `value[]` list from GET Microsoft.Consumption/budgets (the full
               {"value": [...]} response or a single budget object is also accepted)
    as_of      date the evaluation is anchored to (defaults to today) — drives run-rate forecast
    mtd_spend  optional {budgetName: actual_month_to_date_in_budget_currency} from UsageDetails
               pull, used only to cross-check Azure's currentSpend and flag large discrepancies
               (caller must match scope/filter/currency; only monthly windows are cross-checked).

    Metadata is preserved in id, scope, category, filter, time_grain, time_period, window
    (start/end_exclusive), notifications, current_currency and forecast_currency. Unknown
    measurements stay None. Summary totals require disjoint ARM scopes, a common window and
    currency, and unfiltered cost budgets; otherwise aggregation_reason explains the null totals.

    Returns a dict:
      {
        "as_of", "budget_count",
        "budgets": [ {name, scope, amount, currency, current_spend, pct_used,
                      forecast_spend, forecast_source, pct_forecast, status,
                      breached_notifications: [{name, threshold, type}],
                      mtd_crosscheck: {agent_mtd, azure_current, delta, delta_pct} | None } ... ],
        "summary": {currency, total_amount, total_current, total_forecast,
                    totals_by_currency, over_budget, forecast_over, at_risk, on_track},
                   # total_* are None when budgets span currencies (see totals_by_currency)
        "gates": [ {name, reason, status} ... ],   # budgets needing a human decision
        "no_budgets": bool,                          # True + guidance when nothing is defined
      }
    """
    if isinstance(budgets, dict):
        budgets = budgets.get("value") if "value" in budgets else [budgets]
    budgets = budgets or []
    mtd_spend = mtd_spend or {}
    today = as_of or date.today()
    # index the MTD cross-check case-insensitively by budget name
    mtd_by_name = defaultdict(float)
    for key, value in mtd_spend.items():
        number = _num(value)
        if number is not None:
            mtd_by_name[str(key).strip().lower()] += number

    if not budgets:
        return {
            "as_of": today.isoformat(),
            "budget_count": 0,
            "budgets": [],
            "summary": {
                "currency": None,
                "total_amount": 0.0, "total_current": 0.0, "total_forecast": 0.0,
                "totals_by_currency": {},
                "over_budget": 0, "forecast_over": 0, "at_risk": 0, "on_track": 0,
            },
            "gates": [],
            "no_budgets": True,
        }

    evaluated = []
    tally = defaultdict(int)
    totals_by_currency = {}

    for b in budgets:
        props = b.get("properties") or {}
        name = b.get("name") or props.get("name") or "(unnamed)"
        amount = _num(props.get("amount"))
        current, current_currency = _spend_amount(props.get("currentSpend"))
        azure_forecast, forecast_currency = _spend_amount(props.get("forecastSpend"))
        currency = props.get("currency") or current_currency or forecast_currency
        currency = str(currency).strip().upper() if currency else None
        current_currency = str(current_currency).strip().upper() if current_currency else None
        forecast_currency = str(forecast_currency).strip().upper() if forecast_currency else None
        window = _grain_window(props.get("timeGrain"), props.get("timePeriod"), today)
        reasons = []
        if amount is None or amount < 0:
            reasons.append("valid budget amount unavailable")
        if current is None:
            reasons.append("current spend unavailable")
        if not currency:
            reasons.append("currency unavailable")
        if window is None:
            reasons.append("active calendar window unavailable")

        # Forecast: prefer Azure's, else linear run-rate extrapolation of current spend.
        if azure_forecast is not None:
            forecast, forecast_source = azure_forecast, "azure"
        else:
            frac = _grain_fraction_elapsed(props.get("timeGrain"), props.get("timePeriod"), today)
            if frac and current is not None:
                forecast, forecast_source = round(current / frac, 2), "run-rate"
                forecast_currency = current_currency
            else:
                forecast, forecast_source = None, "unavailable"

        comparable_current = current if currency and current_currency == currency and window else None
        comparable_forecast = forecast if currency and forecast_currency == currency and window else None
        if str(props.get("category") or "Cost").casefold() != "cost":
            comparable_current = comparable_forecast = None
            reasons.append("not a monetary cost budget")
        if current is not None and current_currency != currency:
            reasons.append("current-spend currency missing or mismatched")
        if forecast is not None and forecast_currency != currency:
            reasons.append("forecast currency missing or mismatched")
        pct_used = _pct(comparable_current, amount)
        pct_forecast = _pct(comparable_forecast, amount)

        # Compare unrounded ratios: display rounding must not trigger or suppress real alerts.
        breached = _breached_notifications(
            props, 100 * comparable_current / amount if amount and comparable_current is not None else None,
            100 * comparable_forecast / amount if amount and comparable_forecast is not None else None)
        status = _classify(amount, comparable_current, comparable_forecast, pct_used, breached)

        crosscheck = None
        if str(props.get("timeGrain")).casefold() == "monthly":
            crosscheck = _mtd_crosscheck(mtd_by_name.get(str(name).strip().lower()), comparable_current)

        evaluated.append({
            "name": name,
            "id": b.get("id"),
            "scope": _scope_of(b, props),
            "category": props.get("category") or "Cost",
            "cost_basis": "ActualCost" if str(props.get("category") or "Cost").casefold() == "cost" else None,
            "filter": deepcopy(props.get("filter") or {}),
            "time_grain": props.get("timeGrain"),
            "time_period": deepcopy(props.get("timePeriod") or {}),
            "window": {"start": window[0].isoformat(), "end_exclusive": window[1].isoformat()}
                      if window else None,
            "notifications": deepcopy(props.get("notifications") or {}),
            "amount": round(amount, 2) if amount is not None else None,
            "currency": currency,
            "current_currency": current_currency,
            "forecast_currency": forecast_currency,
            "current_spend": round(current, 2) if current is not None else None,
            "pct_used": pct_used,
            "forecast_spend": forecast,
            "forecast_source": forecast_source,
            "pct_forecast": pct_forecast,
            "status": status,
            "breached_notifications": breached,
            "analytical_thresholds": [
                {"type": kind, "threshold": 100.0, "source": "analysis_only"}
                for kind, pct in (("actual", pct_used), ("forecasted", pct_forecast))
                if pct is not None and pct >= 100
            ],
            "unknown_reasons": reasons,
            "mtd_crosscheck": crosscheck,
        })

        tally[status] += 1
    for currency in {e["currency"] or "UNKNOWN" for e in evaluated}:
        totals_by_currency[currency] = _safe_totals(
            [e for e in evaluated if (e["currency"] or "UNKNOWN") == currency])
    single = next(iter(totals_by_currency.values())) if len(totals_by_currency) == 1 else None

    evaluated.sort(key=lambda e: _severity(e["status"]), reverse=True)
    gates = [
        {"name": e["name"], "reason": _gate_reason(e), "status": e["status"]}
        for e in evaluated if e["status"] in ("over_budget", "forecast_over")
    ]

    return {
        "as_of": today.isoformat(),
        "budget_count": len(evaluated),
        "budgets": evaluated,
        "summary": {
            "currency": next(iter(totals_by_currency)) if single else None,
            "total_amount": single["total_amount"] if single else None,
            "total_current": single["total_current"] if single else None,
            "total_forecast": single["total_forecast"] if single else None,
            "totals_by_currency": dict(sorted(totals_by_currency.items())),
            "aggregation_reason": single["aggregation_reason"] if single else "multiple currencies",
            "over_budget": tally["over_budget"],
            "forecast_over": tally["forecast_over"],
            "at_risk": tally["at_risk"],
            "on_track": tally["on_track"],
            "unknown": tally["unknown"],
        },
        "gates": gates,
        "no_budgets": False,
    }


def _breached_notifications(props, pct_used, pct_forecast):
    """Which of the budget's own notification thresholds are currently breached.

    A Forecasted-type threshold with no available forecast is skipped (cannot evaluate).
    """
    declared = list(_notifications(props))

    breached = []
    for name, threshold, ttype, enabled in declared:
        if not enabled:
            continue
        measured = {"forecasted": pct_forecast, "actual": pct_used}.get(ttype)
        if measured is None:
            continue
        operator = str(props["notifications"][name].get("operator") or "GreaterThan").casefold()
        comparison = {"greaterthan": measured > threshold,
                      "greaterthanorequalto": measured >= threshold,
                      "equalto": measured == threshold}.get(operator, False)
        if comparison:
            breached.append({"name": name, "threshold": threshold, "type": ttype,
                             "source": "configured", "operator": operator})
    breached.sort(key=lambda x: x["threshold"], reverse=True)
    return breached


def _classify(amount, current, forecast, pct_used, breached):
    """Bucket a budget: over_budget > forecast_over > at_risk > on_track."""
    if amount is None or amount < 0:
        return "unknown"
    if current is not None and current >= amount:
        return "over_budget"
    if forecast is not None and forecast >= amount:
        return "forecast_over"
    if current is None or forecast is None:
        return "unknown"
    if pct_used is not None and pct_used >= AT_RISK_PCT:
        return "at_risk"
    if any(b["type"] == "actual" for b in breached):
        return "at_risk"
    return "on_track"


def _mtd_crosscheck(agent_mtd, azure_current):
    """Compare an independent month-to-date sum to Azure's currentSpend; None if not provided."""
    if agent_mtd is None or azure_current is None:
        return None
    delta = round(agent_mtd - azure_current, 2)
    return {
        "agent_mtd": round(agent_mtd, 2),
        "azure_current": round(azure_current, 2),
        "delta": delta,
        "delta_pct": _pct(abs(delta), azure_current),
    }


def _scope_of(budget, props):
    """Best-effort human scope: the budget's resource-group/subscription id path, else category."""
    bid = str(budget.get("id") or "")
    marker = "/providers/microsoft.consumption/budgets/"
    index = bid.casefold().find(marker)
    if index >= 0:
        return bid[:index].rstrip("/")
    return budget.get("scope") or props.get("scope")


def _safe_totals(entries):
    """Only add independent, same-window cost envelopes; filters cannot prove disjointness."""
    reason = None
    scopes = [str(e["scope"] or "").casefold().rstrip("/") for e in entries]
    if any(not s.startswith("/") for s in scopes):
        reason = "scope unavailable"
    elif any(a == b or a.startswith(b + "/") or b.startswith(a + "/")
             for i, a in enumerate(scopes) for b in scopes[i + 1:]):
        reason = "overlapping budget scopes"
    elif any(e["filter"] for e in entries):
        reason = "filtered budget envelopes cannot be safely aggregated"
    elif any(not e["window"] for e in entries):
        reason = "active calendar window unavailable"
    elif any(e["window"] != entries[0]["window"] or
             e["time_grain"] != entries[0]["time_grain"] for e in entries):
        reason = "different budget periods or time grains"
    elif any(not e["currency"] or str(e["category"]).casefold() != "cost" for e in entries):
        reason = "currency or cost category unavailable"
    out = {"aggregation_reason": reason, "scopes": [e["scope"] for e in entries],
           "window": entries[0]["window"] if not reason else None}
    for target, source in (("total_amount", "amount"), ("total_current", "current_spend"),
                           ("total_forecast", "forecast_spend")):
        valid = not reason and all(e[source] is not None for e in entries)
        currency_key = {"current_spend": "current_currency", "forecast_spend": "forecast_currency"}.get(source)
        if currency_key:
            valid = valid and all(e[currency_key] == e["currency"] for e in entries)
        out[target] = round(sum(e[source] for e in entries), 2) if valid else None
    return out


def _gate_reason(e):
    """One-line reason a budget is gated."""
    if e["status"] == "over_budget":
        return f"{e['current_spend']} {e['currency']} spent vs {e['amount']} budget ({e['pct_used']}%)"
    return f"forecast {e['forecast_spend']} {e['currency']} vs {e['amount']} budget ({e['pct_forecast']}%, {e['forecast_source']})"


_SEVERITY = {"over_budget": 3, "forecast_over": 2, "at_risk": 1, "on_track": 0}


def _severity(status):
    return _SEVERITY.get(status, 0)
