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


def test_forecast_explicit_zero_gap_policy_and_at_risk_status():
    daily = {date(2025, 6, d): 100.0 + (15 if d % 2 else -15) for d in range(1, 21) if d != 10}
    out = sc.forecast_spend(daily, assume_last_partial=False, budget_amount=3000,
                            missing_days_are_zero=True)
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
        budgets={"budgets": [_budget_row(sub, 12000)], "summary": {"over_budget": 0, "forecast_over": 0,
                                                          "total_amount": 12000}},
        commitments={"headline": {"coverage_pct": 80},
                     "inventory": [{"utilization_30d": 98}, {"utilization_7d": 96}]},
        waste={"headline": {"potential_monthly_savings": 200},
               "findings": [{"resourceId": "/r/a", "kind": "idle", "estMonthlySavingsUsd": 200}]},
        rightsizing=[{"resourceId": "/r/b", "action": "Resize", "estMonthlySavingsUsd": 100}],
        anomalies=[{"impact_usd": 500, "direction": "down"}],
        forecast={"available": True, "month_end_forecast": 10000, "run_rate_30d": 10000,
                  "context": _forecast_context(sub)},
        unit_costs=[{"change_pct": -3}, {"change_pct": -1}],
        managed_scopes=[f"{sub}/resourceGroups/app"],
        prior_forecast=9500,
        actual_spend=10000,
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
    assert cov["value"] is None


def test_scope_coverage_requires_arm_prefix_boundary():
    assert sc._scope_covered("/subscriptions/000/resourceGroups/app", ["/Subscriptions/000"])
    assert not sc._scope_covered("/subscriptions/0001", ["/subscriptions/000"])


def test_daily_costs_can_exclude_purchases():
    rows = [
        {"date": "2025-06-01", "cost": 10, "chargeType": "Usage"},
        {"date": "2025-06-01", "cost": 500, "chargeType": "Purchase"},
    ]
    assert sc.daily_costs(rows) == {date(2025, 6, 1): 510.0}
    assert sc.daily_costs(rows, exclude_purchases=True) == {date(2025, 6, 1): 10.0}


@pytest.mark.parametrize("prior,status", [(9500, "pass"), (8500, "warn"), (7000, "fail")])
def test_scorecard_forecast_accuracy_bands(prior, status):
    inputs = _healthy_inputs()
    inputs["prior_forecast"] = prior
    card = sc.kpi_scorecard(**inputs)
    kpi = next(k for k in card["kpis"] if k["id"] == "forecast_accuracy_pct")
    assert kpi["status"] == status
    assert kpi["detail"] == {"prior_forecast_usd": prior, "actual_usd": 10000.0}


def test_parse_forecast_record_finds_the_month_in_html():
    text = ("<td>finops-forecast-record:v1 month=2026-09 forecast=10.00 as_of=2026-08-31</td>"
            "<td>finops-forecast-record:v1 month=2026-10 forecast=3100.50 as_of=2026-09-30</td>")
    assert sc.parse_forecast_record(text, "2026-10") == {
        "month": "2026-10", "forecast": 3100.5, "as_of": "2026-09-30"}
    assert sc.parse_forecast_record(text, "2026-11") is None
    assert sc.parse_forecast_record(None, "2026-10") is None


def test_prior_forecast_inputs_prefers_record_then_reconstructs():
    daily = _series(61, start=date(2026, 8, 1), base=100.0)  # Aug + Sep, $100/day
    record = "finops-forecast-record:v1 month=2026-09 forecast=2700 as_of=2026-08-31"
    recorded = sc.prior_forecast_inputs(daily, "2026-09", record_text=record)
    assert recorded["source"] == "recorded"
    assert recorded["prior_forecast"] == 2700.0
    assert recorded["actual_spend"] == 3000.0
    assert recorded["warnings"] == []

    rebuilt = sc.prior_forecast_inputs(daily, "2026-09")
    assert rebuilt["source"] == "reconstructed"
    assert rebuilt["prior_forecast"] == pytest.approx(3000.0, abs=0.01)
    assert "reconstructed" in rebuilt["warnings"][0]

    card = sc.kpi_scorecard(prior_forecast=recorded["prior_forecast"],
                            actual_spend=recorded["actual_spend"],
                            prior_forecast_source=recorded["source"])
    kpi = next(k for k in card["kpis"] if k["id"] == "forecast_accuracy_pct")
    assert kpi["value"] == 10.0 and kpi["detail"]["source"] == "recorded"


def test_prior_forecast_inputs_handles_missing_and_short_data():
    assert sc.prior_forecast_inputs({}, "2026-09")["actual_spend"] is None
    only_month = _series(20, start=date(2026, 9, 1))
    out = sc.prior_forecast_inputs(only_month, "2026-09")
    assert out["prior_forecast"] is None and out["actual_spend"] is None
    assert out["observed_actual_spend"] == 2000.0
    assert any("ends 2026-09-20" in w for w in out["warnings"])
    assert any("under 7 days" in w for w in out["warnings"])


def test_scorecard_benchmarks_and_next_iteration_focus():
    inputs = _healthy_inputs()
    inputs["anomalies"] = [{"impact_usd": 40, "direction": "up"}, {"impact_usd": 9, "direction": "down"}]
    inputs["advisor_cost_score"] = 72.5
    inputs["allocation"]["untagged_usd"] = 2000
    inputs["prior_forecast"] = 7000
    card = sc.kpi_scorecard(**inputs)
    bench = {b["id"]: b["value"] for b in card["benchmarks"]}
    assert bench == {"anomaly_count": 1, "anomaly_impact_usd": 40.0, "advisor_cost_score": 72.5}
    assert card["next_iteration_focus"][:2] == ["Allocation", "Forecasting"]
    assert len(card["next_iteration_focus"]) <= 3
    healthy = sc.kpi_scorecard(**_healthy_inputs())
    assert healthy["next_iteration_focus"] == []

def test_anomaly_kpi_infers_drop_direction_from_kind():
    anomalies = [
        {"kind": "drop", "impact_usd": 50.0},
        {"kind": "spike", "impact_usd": 10.0},
        {"kind": "spike", "direction": "down", "impact_usd": 99.0},
    ]
    result = sc.kpi_scorecard(allocation={"total_usd": 3000.0}, anomalies=anomalies)
    kpi = next(k for k in result["kpis"] if k["id"] == "anomaly_impact_pct")
    assert kpi["value"] == 10.0  # only the 10 USD spike counts against 100 USD/day
    counts = {b["id"]: b["value"] for b in result["benchmarks"]}
    assert counts["anomaly_count"] == 1


def test_route_to_owners_never_leaves_action_blank():
    findings = [
        {"resourceId": "/r/a", "kind": "review", "estMonthlySavingsUsd": 5.0},
        {"resourceId": "/r/b", "recommendedAction": "Resize to D2s_v5", "estMonthlySavingsUsd": 9.0},
        {"value": "rg-x", "kind": "drop", "impact_usd": 3.0},
    ]
    routed = sc.route_to_owners(findings)
    actions = {i["resourceId"]: i["action"] for i in routed["unrouted"]}
    assert actions["/r/b"] == "Resize to D2s_v5"
    assert actions["/r/a"].startswith("Review the finding")
    assert actions["rg-x"].startswith("Confirm the cost drop")


def test_anomaly_kpi_uses_period_days_or_run_rate():
    alloc = {"total_usd": 660.0}
    anomalies = [{"impact_usd": 30.0, "direction": "up"}]
    by_days = sc.kpi_scorecard(allocation=alloc, anomalies=anomalies, period_days=22)
    kpi = next(k for k in by_days["kpis"] if k["id"] == "anomaly_impact_pct")
    assert kpi["value"] == 100.0  # 30 / (660 / 22)
    by_rate = sc.kpi_scorecard(allocation=alloc, anomalies=anomalies,
                                      forecast={"available": True, "run_rate_30d": 900.0})
    kpi = next(k for k in by_rate["kpis"] if k["id"] == "anomaly_impact_pct")
    assert kpi["value"] == 100.0  # 30 / (900 / 30)


def _kpi(card, name):
    return next(k for k in card["kpis"] if k["id"] == name)


def _budget_row(scope, amount, *, currency="USD", filtered=False):
    return {"scope": scope, "amount": amount, "currency": currency, "time_grain": "Monthly",
            "category": "Cost", "filter": {"tags": {"name": "team"}} if filtered else {},
            "window": {"start": "2026-07-01", "end_exclusive": "2026-08-01"},
            "status": "on_track"}


def _forecast_context(scope="/subscriptions/s"):
    return {"scope": scope, "currency": "USD", "period_start": "2026-07-01",
            "period_end_exclusive": "2026-08-01", "filter": {}}


def test_forecast_budget_matches_parent_not_parent_plus_child():
    budgets = {"budgets": [_budget_row("/subscriptions/s", 1000),
                           _budget_row("/subscriptions/s/resourceGroups/rg", 500)],
               "summary": {"total_amount": 1500}}
    forecast = {"available": True, "month_end_forecast": 1200, "context": _forecast_context()}
    kpi = _kpi(sc.kpi_scorecard(budgets=budgets, forecast=forecast), "forecast_vs_budget_pct")
    assert kpi["value"] == 120
    assert kpi["status"] == "fail"
    assert kpi["detail"]["budget_amount"] == 1000


@pytest.mark.parametrize("change", ["currency", "window", "filter", "duplicate", "no_context"])
def test_forecast_budget_ambiguous_context_is_unknown(change):
    row = _budget_row("/subscriptions/s", 1000)
    rows = [row]
    forecast = {"available": True, "month_end_forecast": 1200, "context": _forecast_context()}
    if change == "currency":
        row["currency"] = "EUR"
    elif change == "window":
        row["window"]["end_exclusive"] = "2027-01-01"
    elif change == "filter":
        row["filter"] = {"tags": {"name": "team"}}
    elif change == "duplicate":
        rows.append(dict(row))
    else:
        forecast.pop("context")
    card = sc.kpi_scorecard(budgets={"budgets": rows, "summary": {"total_amount": 1500}},
                            forecast=forecast)
    kpi = _kpi(card, "forecast_vs_budget_pct")
    assert kpi["status"] == "unknown"
    assert kpi["detail"]["reason"]


def test_waste_and_rightsizing_deduplicate_same_action_not_all_resource_actions():
    findings = [{"resourceId": "/r/VM", "action": "Resize to D2", "estMonthlySavingsUsd": 20}]
    rightsizing = [
        {"resourceId": "/r/vm", "action": "Resize to D2", "estMonthlySavingsUsd": 20},
        {"resourceId": "/r/vm", "action": "Disable backup", "estMonthlySavingsUsd": 5},
    ]
    card = sc.kpi_scorecard(allocation={"total_usd": 1000}, rightsizing=rightsizing,
                            waste={"headline": {"potential_monthly_savings": 20},
                                   "findings": findings})
    kpi = _kpi(card, "waste_pct")
    assert kpi["value"] == 2.5
    assert kpi["detail"]["duplicates_removed"] == 1


def test_anomaly_event_deduplicates_cross_dimensions_not_distinct_dates():
    rows = [
        {"event_id": "a", "date": "2026-07-10", "dimension": "resourceId", "impact_usd": 10},
        {"event_id": "a", "date": "2026-07-10", "dimension": "resourceGroup", "impact_usd": 10},
        {"event_id": "a", "date": "2026-07-11", "dimension": "resourceGroup", "impact_usd": 10},
        {"event_id": "b", "date": "2026-07-10", "dimension": "resourceGroup", "impact_usd": 10},
    ]
    card = sc.kpi_scorecard(allocation={"total_usd": 3000}, anomalies=rows)
    assert _kpi(card, "anomaly_impact_pct")["value"] == 30
    assert next(b["value"] for b in card["benchmarks"] if b["id"] == "anomaly_count") == 3


def test_sparse_evidence_does_not_claim_run_maturity():
    card = sc.kpi_scorecard(commitments={"headline": {"coverage_pct": 90},
                                       "inventory": [{"utilization_30d": 99}]})
    assert card["kpi_health_pct"] == 100
    assert card["evidence_coverage_pct"] == 16.7
    assert card["maturity"] == "unknown"
    assert card["maturity_reason"]


def test_forecast_missing_days_are_unknown_not_fabricated_zero():
    daily = {date(2026, 7, 1): 100, date(2026, 7, 3): 100}
    out = sc.forecast_spend(daily, assume_last_partial=False)
    assert out["history_days"] == 2
    assert out["run_rate_30d"] == 3000
    assert out["data_complete"] is False
    assert out["month_end_forecast"] is None
    assert out["missing_mtd_days"] == ["2026-07-02"]


def test_forecast_explicit_complete_export_can_treat_omitted_days_as_zero():
    daily = {date(2026, 7, 1): 100, date(2026, 7, 3): 100}
    out = sc.forecast_spend(daily, assume_last_partial=False, missing_days_are_zero=True)
    assert out["history_days"] == 3
    assert out["data_complete"] is True
    assert out["month_end_forecast"] is not None
    assert out["missing_day_policy"] == "explicit_zero"


def test_forecast_before_history_returns_unavailable():
    out = sc.forecast_spend({"2026-07-10": 100}, as_of=date(2026, 7, 1))
    assert out["available"] is False


def test_closed_month_incomplete_actual_is_not_scored():
    daily = _series(29, start=date(2026, 9, 1))
    out = sc.prior_forecast_inputs(
        daily, "2026-09", record_text="finops-forecast-record:v1 month=2026-09 forecast=3000")
    assert out["actual_spend"] is None
    assert out["observed_actual_spend"] == 2900


def test_native_cross_dimension_anomalies_do_not_fake_distinct_events():
    rows = [{"dimension": dim, "value": value, "date": "2026-07-10", "kind": "spike",
             "impact_usd": 10}
            for dim, value in [("resourceId", "/r/a"), ("resourceGroup", "rg"),
                               ("meterCategory", "VM")]]
    card = sc.kpi_scorecard(allocation={"total_usd": 3000}, anomalies=rows)
    kpi = _kpi(card, "anomaly_impact_pct")
    assert kpi["status"] == "unknown"
    assert kpi["detail"]["observed_impact_lower_bound_usd"] == 10
    assert kpi["detail"]["observed_impact_upper_bound_usd"] == 30


def test_native_duplicate_observation_removed_but_separate_resources_kept():
    one = {"dimension": "resourceId", "value": "/r/a", "date": "2026-07-10",
           "kind": "spike", "impact_usd": 10}
    two = dict(one, value="/r/b")
    card = sc.kpi_scorecard(allocation={"total_usd": 3000}, anomalies=[one, one, two])
    assert _kpi(card, "anomaly_impact_pct")["value"] == 20


def test_waste_delete_and_resize_are_alternatives_not_additive():
    waste = {"findings": [{"resourceId": "/r/a", "kind": "idle", "currency": "USD",
                           "recommendedAction": "Deallocate VM", "estMonthlySavings": 100}]}
    right = [{"resourceId": "/r/A", "recommendedAction": "Resize VM", "currency": "USD",
              "estMonthlySavings": 50, "validated": True}]
    kpi = _kpi(sc.kpi_scorecard(allocation={"total_usd": 1000}, waste=waste,
                                rightsizing=right), "waste_pct")
    assert kpi["value"] == 10
    assert kpi["detail"]["alternative_actions_not_added"] == 1


@pytest.mark.parametrize("currency", ["EUR", None])
def test_generic_optimization_savings_require_matching_currency(currency):
    rows = [{"resourceId": "/r/a", "recommendedAction": "Resize", "estMonthlySavings": 20,
             "currency": currency, "validated": True}]
    kpi = _kpi(sc.kpi_scorecard(allocation={"total_usd": 1000}, rightsizing=rows), "waste_pct")
    assert kpi["status"] == "unknown"
    assert kpi["detail"]["reason"]


def test_candidate_and_contradicted_savings_not_counted_as_supported():
    rows = [{"resourceId": "/r/a", "recommendedAction": "Resize", "estMonthlySavingsUsd": 20,
             "validated": None},
            {"resourceId": "/r/b", "recommendedAction": "Resize", "estMonthlySavingsUsd": 40,
             "validated": False},
            {"resourceId": "/r/c", "recommendedAction": "Resize", "estMonthlySavingsUsd": 10,
             "validated": True}]
    kpi = _kpi(sc.kpi_scorecard(allocation={"total_usd": 1000}, rightsizing=rows), "waste_pct")
    assert kpi["value"] == 1
    assert kpi["detail"]["unsupported_findings_excluded"] == 2


def test_headline_only_waste_cannot_be_added_to_rightsizing():
    kpi = _kpi(sc.kpi_scorecard(
        allocation={"total_usd": 1000},
        waste={"currency": "USD", "headline": {"potential_monthly_savings": 100}},
        rightsizing=[{"resourceId": "/r/a", "action": "Resize", "estMonthlySavingsUsd": 20}]),
        "waste_pct")
    assert kpi["status"] == "unknown"
    assert "overlap" in kpi["detail"]["reason"] or "de-duplicated" in kpi["detail"]["reason"]


def test_forecast_budget_matches_disjoint_scope_envelopes():
    scopes = ["/subscriptions/a", "/subscriptions/b"]
    context = _forecast_context()
    context.pop("scope")
    context["scopes"] = scopes
    card = sc.kpi_scorecard(
        budgets={"budgets": [_budget_row(scopes[0], 1000), _budget_row(scopes[1], 500)]},
        forecast={"available": True, "month_end_forecast": 1200, "context": context})
    assert _kpi(card, "forecast_vs_budget_pct")["value"] == 80


def test_parent_budget_does_not_govern_child_only_forecast_denominator():
    card = sc.kpi_scorecard(
        budgets={"budgets": [_budget_row("/subscriptions/s", 1000)]},
        forecast={"available": True, "month_end_forecast": 300,
                  "context": _forecast_context("/subscriptions/s/resourceGroups/rg")})
    assert _kpi(card, "forecast_vs_budget_pct")["status"] == "unknown"


def test_filtered_budget_only_matches_exact_filtered_forecast():
    row = _budget_row("/subscriptions/s", 1000, filtered=True)
    context = _forecast_context()
    context["filter"] = row["filter"]
    card = sc.kpi_scorecard(budgets={"budgets": [row]},
                            forecast={"available": True, "month_end_forecast": 1200, "context": context})
    assert _kpi(card, "forecast_vs_budget_pct")["value"] == 120


def test_daily_costs_never_sum_different_currencies():
    with pytest.raises(ValueError, match="currency"):
        sc.daily_costs([{"date": "2026-07-01", "cost": 10, "currency": "USD"},
                        {"date": "2026-07-01", "cost": 20, "currency": "EUR"}])


def test_forecast_explicit_budget_currency_mismatch_suppresses_ratio():
    context = _forecast_context()
    out = sc.forecast_spend(_series(20, start=date(2026, 7, 1)), assume_last_partial=False,
                            budget_amount=1000, context=context,
                            budget_context=dict(context, currency="EUR"))
    assert "forecast_vs_budget_pct" not in out
    assert out["budget_comparison"]["reason"]


def test_forecast_history_excludes_missing_days_before_current_month():
    daily = _series(45, start=date(2026, 6, 1))
    del daily[date(2026, 6, 5)]
    out = sc.forecast_spend(daily, assume_last_partial=False)
    assert out["data_complete"] is True
    assert out["history_complete"] is False
    assert out["history_days"] == 44
    assert out["month_end_forecast"] == pytest.approx(3100)
    assert out["confidence"] == "low"


def test_closed_month_explicit_zero_omissions_are_documented():
    out = sc.prior_forecast_inputs(
        {"2026-09-01": 100}, "2026-09", missing_days_are_zero=True,
        record_text="finops-forecast-record:v1 month=2026-09 forecast=100")
    assert out["actual_spend"] == 100
    assert out["actual_complete"] is True
    assert len(out["missing_actual_days"]) == 29


def test_missing_allocation_measurements_do_not_invent_passes():
    card = sc.kpi_scorecard(allocation={"total_usd": 1000})
    for name in ("allocated_pct", "untagged_pct", "tag_hygiene_pct"):
        assert _kpi(card, name)["status"] == "unknown"
    assert card["evidence_coverage_pct"] == 0


def test_foreign_currency_forecast_is_not_a_usd_waste_denominator():
    card = sc.kpi_scorecard(
        rightsizing=[{"estMonthlySavingsUsd": 100}],
        forecast={"available": True, "run_rate_30d": 1000, "context": {"currency": "EUR"}})
    assert _kpi(card, "waste_pct")["status"] == "unknown"
