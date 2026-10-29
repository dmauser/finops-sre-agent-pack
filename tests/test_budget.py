"""Layer-1 unit tests for the budget-governance skill (offline, deterministic)."""

import importlib.util
from datetime import date
from pathlib import Path
import pytest

_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "finops"
    / "skills"
    / "finops-budget-governance"
    / "budget.py"
)
_spec = importlib.util.spec_from_file_location("budget", _PATH)
budget = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(budget)

evaluate_budgets = budget.evaluate_budgets

# Anchor every date-sensitive test so run-rate math is deterministic (day 15 of 31-day July).
AS_OF = date(2026, 7, 15)


def _budget(name, amount, current, *, forecast=None, notifications=None,
            time_grain="Monthly", scope_id=None):
    props = {
        "amount": amount,
        "category": "Cost",
        "timeGrain": time_grain,
        "currentSpend": {"amount": current, "unit": "USD"},
    }
    if forecast is not None:
        props["forecastSpend"] = {"amount": forecast, "unit": "USD"}
    if notifications is not None:
        props["notifications"] = notifications
    return {
        "name": name,
        "id": (scope_id or f"/subscriptions/s/providers/Microsoft.Consumption/budgets/{name}"),
        "properties": props,
    }


def test_empty_budgets_is_a_first_class_finding():
    out = evaluate_budgets([], as_of=AS_OF)
    assert out["no_budgets"] is True
    assert out["budget_count"] == 0
    assert out["budgets"] == []
    assert out["gates"] == []


def test_none_input_is_treated_as_empty():
    out = evaluate_budgets(as_of=AS_OF)
    assert out["no_budgets"] is True


def test_over_budget_status_and_gate():
    b = _budget("m", 1000.0, 1100.0)
    out = evaluate_budgets([b], as_of=AS_OF)
    e = out["budgets"][0]
    assert e["status"] == "over_budget"
    assert e["pct_used"] == 110.0
    assert out["summary"]["over_budget"] == 1
    assert out["gates"][0]["name"] == "m"
    assert "1100.0 USD spent vs 1000.0" in out["gates"][0]["reason"]


def test_native_forecast_preferred_over_run_rate():
    # currentSpend 400 at day 15/30 would run-rate to ~800, but Azure says 1300 -> forecast_over.
    b = _budget("m", 1000.0, 400.0, forecast=1300.0)
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["forecast_source"] == "azure"
    assert e["forecast_spend"] == 1300.0
    assert e["status"] == "forecast_over"
    assert e["pct_forecast"] == 130.0


def test_run_rate_fallback_when_forecast_absent():
    # No forecastSpend: day 15 of 31-day July, 600 spent -> 600 / (15/31) = 1240 -> forecast_over.
    b = _budget("m", 1000.0, 600.0)
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["forecast_source"] == "run-rate"
    assert e["forecast_spend"] == 1240.0
    assert e["status"] == "forecast_over"


def _with_period(b, start, end="2030-01-01T00:00:00Z"):
    b["properties"]["timePeriod"] = {"startDate": start, "endDate": end}
    return b


def test_run_rate_uses_real_february_length():
    b = _with_period(_budget("m", 1000.0, 140.0), "2026-01-01T00:00:00Z")
    e = evaluate_budgets([b], as_of=date(2026, 2, 14))["budgets"][0]
    assert e["forecast_spend"] == 280.0  # 14 of 28 days


def test_quarterly_recurring_budget_gets_run_rate_forecast():
    # Calendar Q3 2026 = Jul 1..Sep 30 (92 days); Aug 15 is day 46 -> half elapsed.
    b = _with_period(_budget("q", 3000.0, 1600.0, time_grain="Quarterly"),
                     "2026-01-01T00:00:00Z")
    e = evaluate_budgets([b], as_of=date(2026, 8, 15))["budgets"][0]
    assert e["forecast_source"] == "run-rate"
    assert e["forecast_spend"] == 3200.0
    assert e["status"] == "forecast_over"


def test_quarterly_window_anchored_at_start_month():
    # Starts in Feb -> windows Feb-Apr, May-Jul, Aug-Oct. Aug 1 is day 1 of 92.
    b = _with_period(_budget("q", 3000.0, 10.0, time_grain="Quarterly"),
                     "2026-02-01T00:00:00Z")
    e = evaluate_budgets([b], as_of=date(2026, 8, 1))["budgets"][0]
    assert e["forecast_spend"] == 920.0


def test_annual_recurring_budget_gets_run_rate_forecast():
    b = _with_period(_budget("y", 12000.0, 3650.0, time_grain="Annually"),
                     "2025-01-01T00:00:00Z")
    e = evaluate_budgets([b], as_of=date(2026, 4, 10))["budgets"][0]  # day 100 of 365
    assert e["forecast_source"] == "run-rate"
    assert e["forecast_spend"] == 13322.5


def test_budget_not_started_has_no_run_rate():
    b = _with_period(_budget("m", 1000.0, 0.0), "2026-09-01T00:00:00Z")
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["forecast_source"] == "unavailable"


def test_summary_totals_single_currency():
    out = evaluate_budgets([_budget("a", 100.0, 10.0, forecast=20.0),
                            _budget("b", 200.0, 30.0, forecast=40.0, scope_id=(
                                "/subscriptions/other/providers/Microsoft.Consumption/budgets/b"))],
                           as_of=AS_OF)
    s = out["summary"]
    assert s["currency"] == "USD"
    assert (s["total_amount"], s["total_current"], s["total_forecast"]) == (300.0, 40.0, 60.0)


def test_summary_never_sums_across_currencies():
    eur = _budget("eur", 500.0, 50.0, forecast=100.0)
    eur["properties"]["currentSpend"]["unit"] = "EUR"
    out = evaluate_budgets([_budget("usd", 100.0, 10.0, forecast=20.0), eur], as_of=AS_OF)
    s = out["summary"]
    assert s["currency"] is None
    assert s["total_amount"] is None and s["total_current"] is None
    assert s["totals_by_currency"]["EUR"]["total_amount"] == 500.0
    assert s["totals_by_currency"]["USD"]["total_current"] == 10.0


def test_at_risk_band_by_actual_pct():
    # 85% spent, run-rate forecast still under budget on an already-late window -> at_risk.
    b = _budget("m", 1000.0, 850.0, forecast=950.0)
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["status"] == "at_risk"
    assert out_gate_names(evaluate_budgets([b], as_of=AS_OF)) == []  # at_risk does not gate


def test_on_track_low_spend():
    b = _budget("m", 1000.0, 100.0, forecast=300.0)
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["status"] == "on_track"


def test_budget_own_notification_thresholds_evaluated():
    notifications = {
        "actual_80": {"enabled": True, "threshold": 80, "thresholdType": "Actual"},
        "fcst_100": {"enabled": True, "threshold": 100, "thresholdType": "Forecasted"},
        "actual_50": {"enabled": True, "threshold": 50, "thresholdType": "Actual"},
    }
    b = _budget("m", 1000.0, 850.0, forecast=1200.0, notifications=notifications)
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    names = [n["name"] for n in e["breached_notifications"]]
    # 85% actual breaches actual_80 and actual_50; 120% forecast breaches fcst_100.
    assert set(names) == {"actual_80", "fcst_100", "actual_50"}
    # sorted by threshold descending
    assert e["breached_notifications"][0]["threshold"] == 100


def test_disabled_notification_ignored():
    notifications = {"off": {"enabled": False, "threshold": 10, "thresholdType": "Actual"}}
    b = _budget("m", 1000.0, 900.0, forecast=950.0, notifications=notifications)
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["breached_notifications"] == []


def test_mtd_crosscheck_flags_discrepancy():
    b = _budget("m", 1000.0, 500.0, forecast=900.0)
    out = evaluate_budgets([b], as_of=AS_OF, mtd_spend={"m": 650.0})
    cc = out["budgets"][0]["mtd_crosscheck"]
    assert cc["azure_current"] == 500.0
    assert cc["agent_mtd"] == 650.0
    assert cc["delta"] == 150.0
    assert cc["delta_pct"] == 30.0


def test_mtd_crosscheck_absent_when_not_provided():
    b = _budget("m", 1000.0, 500.0, forecast=900.0)
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["mtd_crosscheck"] is None


def test_budgets_sorted_by_severity():
    ok = _budget("ok", 1000.0, 100.0, forecast=200.0)
    over = _budget("over", 1000.0, 1200.0)
    fcst = _budget("fcst", 1000.0, 400.0, forecast=1100.0)
    out = evaluate_budgets([ok, fcst, over], as_of=AS_OF)
    assert [e["name"] for e in out["budgets"]] == ["over", "fcst", "ok"]
    assert len(out["gates"]) == 2


def test_non_numeric_amount_does_not_crash():
    b = _budget("m", None, 500.0)
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["amount"] is None
    assert e["pct_used"] is None
    assert e["status"] == "unknown"


def test_resource_group_scope_extracted():
    scope_id = ("/subscriptions/s/resourceGroups/rg1/providers/"
                "Microsoft.Consumption/budgets/m")
    b = _budget("m", 1000.0, 100.0, forecast=200.0, scope_id=scope_id)
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["scope"] == "/subscriptions/s/resourceGroups/rg1"


def out_gate_names(out):
    return [g["name"] for g in out["gates"]]

def test_evaluate_budgets_accepts_full_list_response():
    one = {"name": "sub-budget", "id": "/subscriptions/s/providers/Microsoft.Consumption/budgets/sub-budget",
           "properties": {"amount": 100, "timeGrain": "Monthly", "currentSpend": {"amount": 20, "unit": "USD"}}}
    from_list = evaluate_budgets([one], as_of=AS_OF)
    from_response = evaluate_budgets({"value": [one]}, as_of=AS_OF)
    assert from_response == from_list
    assert from_response["budget_count"] == 1
    assert evaluate_budgets(one, as_of=AS_OF)["budget_count"] == 1
    assert evaluate_budgets({"value": []}, as_of=AS_OF)["no_budgets"] is True


def test_missing_current_spend_is_unknown_not_zero():
    b = _budget("missing", 100, 0)
    del b["properties"]["currentSpend"]
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["current_spend"] is None
    assert e["forecast_spend"] is None
    assert e["currency"] is None
    assert e["status"] == "unknown"


def test_metadata_preserved_and_case_insensitive_scope():
    b = _with_period(_budget("m", 100, 20), "2026-01-01")
    b["id"] = "/subscriptions/S/providers/microsoft.consumption/budgets/m"
    b["properties"]["filter"] = {"tags": {"name": "team", "values": ["A"]}}
    b["properties"]["notifications"] = {
        "mail": {"threshold": 80, "operator": "GreaterThan", "contactEmails": ["owner@example.test"]}
    }
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["id"] == b["id"]
    assert e["scope"] == "/subscriptions/S"
    assert e["filter"] == b["properties"]["filter"]
    assert e["time_grain"] == "Monthly"
    assert e["time_period"] == b["properties"]["timePeriod"]
    assert e["window"] == {"start": "2026-07-01", "end_exclusive": "2026-08-01"}
    assert e["notifications"] == b["properties"]["notifications"]


def test_parent_and_child_budgets_are_not_additive():
    parent = _budget("sub", 1000, 100)
    child = _budget("rg", 500, 50, scope_id=(
        "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Consumption/budgets/rg"))
    summary = evaluate_budgets([parent, child], as_of=AS_OF)["summary"]
    assert summary["total_amount"] is None
    assert "overlap" in summary["aggregation_reason"]


def test_monthly_and_annual_budgets_are_not_additive():
    annual = _budget("annual", 1000, 100, time_grain="Annually",
                     scope_id="/subscriptions/other/providers/Microsoft.Consumption/budgets/annual")
    summary = evaluate_budgets([_budget("m", 100, 20), annual], as_of=AS_OF)["summary"]
    assert summary["total_amount"] is None
    assert summary["aggregation_reason"]


def test_analytic_fallback_is_not_a_configured_notification():
    e = evaluate_budgets([_budget("m", 100, 120)], as_of=AS_OF)["budgets"][0]
    assert e["breached_notifications"] == []
    assert e["analytical_thresholds"]


def test_notification_operator_respected_at_exact_threshold():
    notifications = {
        "strict": {"threshold": 80, "operator": "GreaterThan", "thresholdType": "Actual"},
        "inclusive": {"threshold": 80, "operator": "GreaterThanOrEqualTo", "thresholdType": "Actual"},
    }
    e = evaluate_budgets([_budget("m", 100, 80, forecast=90, notifications=notifications)],
                         as_of=AS_OF)["budgets"][0]
    assert [n["name"] for n in e["breached_notifications"]] == ["inclusive"]


def test_mismatched_forecast_currency_is_not_compared():
    b = _budget("m", 100, 10, forecast=200)
    b["properties"]["forecastSpend"]["unit"] = "EUR"
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["forecast_currency"] == "EUR"
    assert e["pct_forecast"] is None
    assert e["status"] == "unknown"


def test_expired_budget_mid_window_is_inactive():
    b = _with_period(_budget("m", 100, 50), "2026-01-01", "2026-07-10")
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["window"] is None
    assert e["status"] == "unknown"


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "100"])
def test_invalid_current_spend_is_not_zero(value):
    b = _budget("m", 100, value)
    e = evaluate_budgets([b], as_of=AS_OF)["budgets"][0]
    assert e["current_spend"] is None
    assert e["status"] == "unknown"


def test_currency_case_and_numeric_case_variant_crosschecks():
    b = _budget("m", 1000, 40)
    b["properties"]["currentSpend"]["unit"] = "usd"
    out = evaluate_budgets([b], as_of=AS_OF, mtd_spend={"M": 20, "m": 30, " m ": "999"})
    assert out["budgets"][0]["currency"] == "USD"
    assert out["budgets"][0]["mtd_crosscheck"]["agent_mtd"] == 50


def test_current_spend_missing_does_not_invent_complete_summary():
    b = _budget("m", 100, None, forecast=80)
    s = evaluate_budgets([b], as_of=AS_OF)["summary"]
    assert s["total_current"] is None
    assert s["unknown"] == 1


def test_actual_notification_compares_unrounded_ratio():
    b = _budget("m", 100, 80.04, forecast=90, notifications={
        "strict": {"threshold": 80, "operator": "GreaterThan", "thresholdType": "Actual"}})
    assert evaluate_budgets([b], as_of=AS_OF)["budgets"][0]["breached_notifications"]


def test_billing_month_does_not_guess_calendar_window():
    e = evaluate_budgets([_budget("m", 100, 10, time_grain="BillingMonth")],
                         as_of=AS_OF)["budgets"][0]
    assert e["window"] is None
    assert e["forecast_spend"] is None
    assert e["unknown_reasons"]


def test_usage_budget_is_not_monetary_cost_governance():
    b = _budget("m", 100, 10)
    b["properties"]["category"] = "Usage"
    assert evaluate_budgets([b], as_of=AS_OF)["budgets"][0]["status"] == "unknown"
