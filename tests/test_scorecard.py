import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "plugins" / "finops" / "skills" / "finops-kpi-scorecard" / "scorecard.py"
_spec = importlib.util.spec_from_file_location("scorecard", PATH)
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)


def _series(days, start=date(2025, 5, 1), base=100.0, step=0.0):
    return {start + timedelta(days=i): base + step * i for i in range(days)}


# ---- daily_costs ---------------------------------------------------------------------------

def test_daily_costs_sums_by_day_and_optionally_drops_credits():
    rows = [
        {"date": "2025-06-01T00:00:00Z", "cost": 10},
        {"date": "2025-06-01", "cost": 5},
        {"date": "2025-06-02", "cost": -3},
        {"date": "2025-06-02", "cost": "bad"},
        {"cost": 99},
    ]
    assert sc.daily_costs(rows) == {date(2025, 6, 1): 15.0, date(2025, 6, 2): -3.0}
    assert sc.daily_costs(rows, include_credits=False) == {date(2025, 6, 1): 15.0}


# ---- forecast_spend ------------------------------------------------------------------------

def test_forecast_unavailable_without_history():
    out = sc.forecast_spend({})
    assert out["available"] is False


def test_forecast_flat_series_is_exact_and_on_track():
    daily = _series(61, start=date(2025, 5, 1))  # through 2025-06-30, last day dropped
    out = sc.forecast_spend(daily, budget_amount=4000)
    assert out["as_of"] == "2025-06-29"
    assert out["method"] == "linear_trend_weekday_seasonality"
    assert out["month_to_date"] == 2900.0
    assert out["month_end_forecast"] == pytest.approx(3000.0)
    assert out["next_month"] == "2025-07"
    assert out["next_month_forecast"] == pytest.approx(3100.0)
    assert out["run_rate_30d"] == pytest.approx(3000.0)
    assert out["confidence"] == "high"
    assert out["budget_status"] == "on_track"
    assert out["forecast_vs_budget_pct"] == 75.0


def test_forecast_trend_and_budget_overrun():
    daily = _series(46, step=1.0)
    out = sc.forecast_spend(daily, budget_amount=4000)
    assert out["daily_trend_slope"] == pytest.approx(1.0)
    assert out["month_end_forecast"] == 4365.0
    assert out["budget_status"] == "forecast_over"


def test_forecast_short_history_uses_trailing_average_and_warns():
    daily = _series(5, start=date(2025, 6, 1), base=50)
    out = sc.forecast_spend(daily, assume_last_partial=False)
    assert out["method"] == "trailing_average"
    assert out["confidence"] == "low"
    assert out["warnings"]
    assert out["month_end_forecast"] == pytest.approx(50.0 * 30)


def test_forecast_weekday_seasonality_and_band():
    start = date(2025, 4, 7)  # Monday
    daily = {start + timedelta(days=i): (40.0 if (start + timedelta(days=i)).weekday() >= 5 else 100.0)
             for i in range(56)}
    out = sc.forecast_spend(daily, as_of=start + timedelta(days=55))
    # Weekday pattern is learned, so the residual band stays tight.
    assert out["method"] == "linear_trend_weekday_seasonality"
    assert out["month_end_high"] - out["month_end_low"] < 0.05 * out["month_end_forecast"]


def test_forecast_fills_gaps_with_zero_and_at_risk_status():
    daily = {date(2025, 6, d): 100.0 + (15 if d % 2 else -15) for d in range(1, 21) if d != 10}
    out = sc.forecast_spend(daily, assume_last_partial=False, budget_amount=3000)
    assert out["history_days"] == 20
    assert out["budget_status"] in {"at_risk", "forecast_over", "on_track"}
    assert out["month_end_low"] <= out["month_end_forecast"] <= out["month_end_high"]


# ---- unit_economics ------------------------------------------------------------------------

def test_unit_economics_improving_healthy_growth():
    out = sc.unit_economics(4200, 2_100_000, unit_name="request", per=1000,
                            previous_cost=3900, previous_units=1_800_000)
    assert out["cost_per_unit"] == 2.0
    assert out["change_pct"] == -7.7
    assert out["trend"] == "improving"
    assert "healthy growth" in out["interpretation"]


def test_unit_economics_worsening_and_missing_units():
    worse = sc.unit_economics(1100, 100, previous_cost=1000, previous_units=100)
    assert worse["trend"] == "worsening" and worse["change_pct"] == 10.0
    missing = sc.unit_economics(100, 0)
    assert missing["cost_per_unit"] is None and missing["warnings"]


def test_unit_economics_by_owner_sorted_desc():
    out = sc.unit_economics(10, 10, by_owner={"a": {"cost": 10, "units": 10},
                                              "b": {"cost": 30, "units": 10},
                                              "c": {"cost": 5, "units": 0}})
    assert [r["owner"] for r in out["by_owner"]] == ["b", "a", "c"]


# ---- route_to_owners -----------------------------------------------------------------------

def test_route_to_owners_uses_tag_priority_and_case_insensitive_ids():
    rid1 = "/subscriptions/0/resourceGroups/A/providers/x/y/one"
    rid2 = "/subscriptions/0/resourceGroups/A/providers/x/y/two"
    findings = [
        {"resourceId": rid1, "estMonthlySavingsUsd": 50},
        {"resourceId": rid2, "estMonthlySavings": 20, "tags": '"Team": "web", "app": "shop"'},
        {"resourceId": "/subscriptions/0/x/three", "impact_usd": 7},
    ]
    tags = {rid1.upper(): {"Owner": "alice@contoso.com", "team": "payments"}}
    out = sc.route_to_owners(findings, tags=tags, source="waste")
    assert [g["owner"] for g in out["owners"]] == ["alice@contoso.com", "web"]
    assert out["owners"][0]["owner_tag"] == "owner"
    assert out["owners"][1]["owner_tag"] == "team"
    assert out["owners"][0]["items"][0]["source"] == "waste"
    assert out["unrouted_usd"] == 7.0
    assert out["routed_pct"] == 90.9


# ---- kpi_scorecard -------------------------------------------------------------------------

def _healthy_inputs():
    sub = "/subscriptions/000"
    return dict(
        allocation={"dimension": "team", "total_usd": 10000, "unallocated_pct": 5.0,
                    "untagged_usd": 100, "tag_hygiene": [{"cost_affected": 50}]},
        budgets={"budgets": [{"scope": sub}], "summary": {"over_budget": 0, "forecast_over": 0,
                                                          "total_amount": 12000}},
        commitments={"headline": {"coverage_pct": 80},
                     "inventory": [{"utilization_30d": 98}, {"utilization_7d": 96}]},
        waste={"headline": {"potential_monthly_savings": 200}},
        rightsizing=[{"estMonthlySavingsUsd": 100}],
        anomalies=[{"impact_usd": 500, "direction": "down"}],
        forecast={"available": True, "month_end_forecast": 10000, "run_rate_30d": 10000},
        unit_costs=[{"change_pct": -3}, {"change_pct": -1}],
        managed_scopes=[f"{sub}/resourceGroups/app"],
        as_of=date(2025, 7, 1),
    )


def test_scorecard_all_pass_is_run_maturity():
    card = sc.kpi_scorecard(**_healthy_inputs())
    statuses = {k["id"]: k["status"] for k in card["kpis"]}
    assert set(statuses.values()) == {"pass"}, statuses
    assert card["maturity"] == "run" and card["score_pct"] == 100.0
    assert card["actions"] == []
    assert card["as_of"] == "2025-07-01"
    values = {k["id"]: k["value"] for k in card["kpis"]}
    assert values["commitment_utilization_pct"] == 97.0
    assert values["forecast_vs_budget_pct"] == 83.3
    assert values["anomaly_impact_pct"] == 0.0  # drops do not count against the KPI
    assert values["waste_pct"] == 3.0


def test_scorecard_no_inputs_is_unknown():
    card = sc.kpi_scorecard()
    assert card["maturity"] == "unknown"
    assert card["counts"]["unknown"] == len(card["kpis"]) == len(sc.DEFAULT_TARGETS)


def test_scorecard_governance_checks_and_uncovered_scopes():
    inputs = _healthy_inputs()
    inputs["managed_scopes"] = ["/subscriptions/000/resourceGroups/app", "/subscriptions/111"]
    inputs["allocation"]["untagged_usd"] = 2000
    card = sc.kpi_scorecard(**inputs)
    gov = {g["id"]: g for g in card["governance_checks"]}
    assert set(gov) == {"untagged_pct", "tag_hygiene_pct", "budget_coverage_pct"}
    assert gov["untagged_pct"]["status"] == "fail"
    assert gov["budget_coverage_pct"]["value"] == 50.0
    assert card["uncovered_budget_scopes"] == ["/subscriptions/111"]
    assert card["actions"][0]["status"] == "fail"
    assert card["maturity"] == "run" or card["maturity"] == "walk"


def test_scorecard_target_override_and_no_managed_scopes():
    inputs = _healthy_inputs()
    inputs["managed_scopes"] = None
    inputs["targets"] = {"commitment_coverage_pct": {"target": 90, "warn": 60}}
    card = sc.kpi_scorecard(**inputs)
    kpi = next(k for k in card["kpis"] if k["id"] == "commitment_coverage_pct")
    assert kpi["target"] == 90 and kpi["status"] == "warn" and kpi["action"]
    cov = next(k for k in card["kpis"] if k["id"] == "budget_coverage_pct")
    assert cov["value"] == 100.0


def test_scope_coverage_requires_arm_prefix_boundary():
    assert sc._scope_covered("/subscriptions/000/resourceGroups/app", ["/Subscriptions/000"])
    assert not sc._scope_covered("/subscriptions/0001", ["/subscriptions/000"])
