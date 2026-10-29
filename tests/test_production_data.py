"""Financial invariants shared by both ingestion modes."""

import importlib.util
import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "plugins" / "finops" / "skills"
sys.path.insert(0, str(SKILLS / "finops-managed-scope"))
import usage


def load(skill, name):
    spec = importlib.util.spec_from_file_location(name, SKILLS / skill / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


allocate = load("finops-cost-allocation", "allocate")
detect = load("finops-cost-anomaly-detection", "detect")
models = load("finops-report-renderer", "models")
attribute = load("finops-for-ai", "attribute")
sre = load("finops-sre-agent", "sreagent")
reliability = load("finops-cost-vs-reliability", "reliability")
RID = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm"


def test_cost_map_sums_case_variants_and_preserves_precision():
    costs = usage.costs_by_resource([
        {"resourceId": RID, "cost": "60.005", "currency": "EUR"},
        {"resourceId": RID.lower(), "cost": "40.005", "currency": "EUR"},
    ])
    assert len(costs) == 1
    assert sum(costs.values()) == pytest.approx(100.01)
    assert costs.currency == "EUR"


def test_nonresource_charges_reconcile_in_allocation():
    costs = usage.costs_by_resource([
        {"resourceId": RID, "cost": 100, "currency": "EUR"},
        {"cost": 25, "chargeType": "UnusedReservation", "currency": "EUR"},
    ])
    report = allocate.allocate_costs(costs)
    assert report["total_usd"] == 125
    assert report["nonresource_cost"] == 25
    assert report["currency"] == "EUR"
    assert report["allocated_usd"] + report["unallocated_usd"] + report["undistributed_usd"] == 125


def test_allocation_uses_each_billing_rows_tags_not_current_tags():
    costs = usage.costs_by_resource([
        {"resourceId": RID, "cost": 60, "currency": "USD", "tags": {"team": "original"}},
        {"resourceId": RID, "cost": 40, "currency": "USD", "tags": {"team": "new"}},
    ])
    result = allocate.allocate_costs(costs, tags={RID: {"team": "today"}})
    assert {g["owner"]: g["monthly_usd"] for g in result["groups"]} == {"original": 60, "new": 40}
    assert result["allocation_basis"] == "billing_time"


def test_terminal_page_cannot_be_followed_by_another_chain():
    with pytest.raises(ValueError, match="terminal"):
        usage.prepare_usage_details([
            {"request_url": "https://management.azure.com/usage", "value": []},
            {"request_url": "https://management.azure.com/restart", "value": []},
        ], ["/subscriptions/sub"])


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", True, "not-a-number"])
def test_present_invalid_cost_is_rejected(value):
    with pytest.raises(ValueError, match="cost"):
        usage.normalize_usage_costs([{"resourceId": RID, "cost": value}])


def test_anomaly_identity_is_case_insensitive():
    assert detect._dimension_value({"resourceId": RID}, "resourceId") == RID.lower()
    assert detect._dimension_value({"resourceGroup": "RG"}, "resourceGroup") == "rg"


def test_scratch_requires_run_identity_and_isolates_runs(tmp_path):
    with pytest.raises(ValueError, match="run_id"):
        usage.save_finops_input("budgets", {})
    usage.save_finops_input("budgets", {"run": "a"}, directory=tmp_path, run_id="a")
    usage.save_finops_input("budgets", {"run": "b"}, directory=tmp_path, run_id="b")
    assert usage.load_finops_input("budgets", directory=tmp_path, run_id="a") == {"run": "a"}
    assert usage.load_finops_input("budgets", directory=tmp_path, run_id="b") == {"run": "b"}


def test_mixed_cost_map_currencies_are_rejected():
    with pytest.raises(ValueError, match="currenc"):
        usage.costs_by_resource([
            {"resourceId": RID, "cost": 1, "currency": "USD"},
            {"resourceId": RID, "cost": 1, "currency": "EUR"},
        ])


def test_snapshot_is_reproducible_and_keeps_scope_period_currency():
    prepared = {
        "currency": "EUR", "cost_metric": "AmortizedCost", "chain_complete": True,
        "included_rows": [{"resourceId": RID, "date": "2026-09-10", "cost": "1.005", "currency": "EUR"}],
    }
    args = dict(managed_scopes=["/subscriptions/sub"], requested_start="2026-09-01",
                requested_end="2026-09-30", code_version="test-version")
    first = usage.make_cost_snapshot(prepared, run_id="a", **args)
    second = usage.make_cost_snapshot(prepared, run_id="b", **args)
    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["included_cost"] == "1.005"
    assert first["currency"] == "EUR"
    assert first["requested_period"]["end"] == "2026-09-30"


def test_renderer_propagates_currency_and_excludes_contradicted_savings():
    rows = [
        {"resourceId": RID, "currency": "EUR", "estMonthlySavingsUsd": 60, "validated": True},
        {"resourceId": RID + "2", "currency": "EUR", "estMonthlySavingsUsd": 40, "validated": False},
    ]
    report = models.build_rightsizing_savings_model(rows, refreshed_at="2026-10-29T00:00:00Z")
    assert report["metrics"][0]["value"] == "60.00 EUR"
    assert "(EUR)" in report["chart"]["label"]


def test_renderer_shows_snapshot_provenance_and_rejects_mismatch():
    context = {
        "currency": "EUR", "cost_basis": "AmortizedCost", "snapshot_id": "abc",
        "code_version": "v-test", "requested_period": {"start": "2026-09-01", "end": "2026-09-30"},
        "obtained_period": {"start": "2026-09-01", "end": "2026-09-29"}, "partial": True,
    }
    report = models.build_rightsizing_savings_model([], refreshed_at="2026-10-29T00:00:00Z", cost_context=context)
    assert report["partial"]
    assert any(section["title"] == "Data provenance" for section in report["sections"])
    with pytest.raises(ValueError, match="currency"):
        models.build_rightsizing_savings_model(
            [{"currency": "USD"}], refreshed_at="2026-10-29T00:00:00Z", cost_context=context,
        )


def test_ai_support_and_unclassified_ml_are_not_compute():
    assert attribute._meter_type("Machine Learning", {"meterName": "Defender for AI"}) == "ancillary"
    assert attribute._meter_type("Machine Learning", {"meterName": "Network endpoint bandwidth"}) == "ancillary"
    assert attribute._meter_type("Machine Learning", {"meterName": "Unknown charge"}) == "unclassified"


def test_sre_billing_remainder_is_not_all_always_on():
    rid = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.App/agents/agent"
    result = sre.analyze_sre_agent_costs(
        [{"resourceId": rid, "date": "2026-09-01", "quantity": 300, "cost": 30}],
        daily_usage={rid: {"value": [{"date": "2026-09-01", "value": 20}]}},
    )
    assert result["by_agent"][0]["always_on_aau"] <= 96
    assert result["unreconciled_usd"] == 18.4
    assert any("reconcil" in w for w in result["warnings"])


def test_failed_reliability_sources_do_not_claim_no_risk():
    result = reliability.analyze_cost_vs_reliability(
        [{"resourceId": RID, "date": "2026-09-01", "cost": 2000, "currency": "EUR"}],
    )
    assert result["by_resource"][0]["risk_band"] == "unknown"
    assert result["currency"] == "EUR"
    assert not any(h["type"] == "high_spend_no_pain" for h in result["hints"])
    assert all(row["risk_band"] == "unknown" for row in result["by_service"])


def test_historical_allocation_does_not_inherit_current_owner_and_coalesces_rows():
    costs = usage.costs_by_resource([
        {"resourceId": RID, "date": "2026-09-10", "cost": "0.004", "currency": "USD"}
        for _ in range(5)
    ])
    result = allocate.allocate_costs(
        costs, tags={RID: {"team": "today"}},
        inherited_tags={"/subscriptions/sub": {"team": "current-parent"}},
    )
    assert result["unallocated_usd"] == .02
    assert result["unallocated"]["resource_count"] == 1
    assert result["untagged_resources"][0]["monthly_usd"] == .02
    assert result["current_tag_governance"]["groups"][0]["owner"] == "today"
    assert result["warnings"]


def test_snapshot_requires_verified_period_coverage_and_is_write_once(tmp_path):
    prepared = {
        "currency": "USD", "cost_metric": "ActualCost",
        "included_rows": [{"date": "2026-09-10", "cost": "1.000000000000000001", "currency": "USD"}],
    }
    args = dict(managed_scopes=["/subscriptions/sub"], requested_start="2026-09-01",
                requested_end="2026-09-30", run_id="run", code_version="test")
    snapshot = usage.make_cost_snapshot(prepared, **args)
    assert snapshot["partial"]
    assert snapshot["nonresource_cost"] == snapshot["included_cost"] == "1.000000000000000001"
    prepared["coverage_period"] = {"start": "2026-09-01", "end": "2026-09-30"}
    snapshot = usage.make_cost_snapshot(prepared, **args)
    assert not snapshot["partial"]
    usage.save_cost_snapshot(snapshot, directory=tmp_path)
    with pytest.raises(FileExistsError):
        usage.save_cost_snapshot(snapshot, directory=tmp_path)
    snapshot["included_cost"] = "999"
    with pytest.raises(ValueError, match="hash"):
        usage.save_cost_snapshot(snapshot, directory=tmp_path)


def test_unknown_currency_is_not_silently_rendered_as_usd():
    model = models.build_rightsizing_savings_model(
        [{"resourceId": RID, "currency": None, "estMonthlySavings": 10, "validated": None}],
        refreshed_at="2026-10-29T00:00:00Z",
    )
    assert model["partial"]
    assert "unknown currency" in model["metrics"][0]["value"]
    assert "$" not in model["metrics"][0]["value"]


def budget_snapshot(budgets=None, **changes):
    args = dict(
        managed_scopes=["/subscriptions/sub"], run_id="budget-run", code_version="test-source",
        as_of="2026-10-29", retrieved_at="2026-10-29T08:30:00-05:00", partial=False,
    )
    args.update(changes)
    return usage.make_budget_snapshot(
        budgets if budgets is not None else [{
            "id": "/subscriptions/sub/providers/Microsoft.Consumption/budgets/example",
            "properties": {"currentSpend": {"amount": 12.34, "unit": "EUR"},
                           "timePeriod": {"startDate": "2026-10-01", "endDate": "2027-10-01"}},
        }], **args,
    )


def test_budget_snapshot_preserves_source_is_detached_and_write_once(tmp_path):
    snapshot = budget_snapshot()
    original = copy.deepcopy(snapshot["budgets"])
    second = budget_snapshot(original, run_id="another-run")
    assert snapshot["snapshot_id"] == second["snapshot_id"]
    original[0]["properties"]["currentSpend"]["amount"] = 999
    assert second["budgets"] == snapshot["budgets"]
    assert budget_snapshot(original)["snapshot_id"] != snapshot["snapshot_id"]
    assert snapshot["retrieved_at"] == "2026-10-29T13:30:00+00:00"
    assert snapshot["billing_data_as_of"] is None
    assert "requested_period" not in snapshot and "included_rows" not in snapshot
    path = usage.save_cost_snapshot(snapshot, directory=tmp_path)
    assert Path(path).name == "budget-snapshot.json"
    assert json.loads(Path(path).read_text()) == snapshot
    with pytest.raises(FileExistsError):
        usage.save_cost_snapshot(snapshot, directory=tmp_path)
    snapshot["budgets"][0]["properties"]["currentSpend"]["amount"] = 999
    with pytest.raises(ValueError, match="hash"):
        usage.save_cost_snapshot(snapshot, directory=tmp_path)


def test_budget_provenance_never_invents_usage_dates_or_billing_freshness():
    report = models.build_budget_status_model(
        {"budgets": [], "summary": {}, "gates": []},
        refreshed_at="2026-10-29T14:00:00Z", cost_context=budget_snapshot(),
    )
    fields = dict(next(s["rows"] for s in report["sections"] if s["title"] == "Data provenance"))
    assert fields["Source"] == "Microsoft.Consumption/budgets"
    assert fields["Cost basis / currency"] == "ActualCost / EUR"
    assert fields["Evaluation date"] == "2026-10-29"
    assert fields["Retrieved at"] == "2026-10-29T13:30:00+00:00"
    assert "Not supplied by the budget API" in fields["Billing-data freshness"]
    assert "Requested usage dates" not in fields and "Observed usage dates" not in fields
    assert "billing freshness unverified" in fields["Completeness"]


def test_budget_source_keeps_multiple_currencies_without_adding_them():
    snapshot = budget_snapshot([
        {"properties": {"currentSpend": {"amount": 10, "unit": "EUR"}}},
        {"properties": {"currentSpend": {"amount": 20, "unit": "USD"}}},
    ])
    assert snapshot["currency"] is None and snapshot["currencies"] == ["EUR", "USD"]
    assert "included_cost" not in snapshot
    report = models.build_budget_status_model(
        {"budgets": [], "summary": {}, "gates": []},
        refreshed_at="2026-10-29T14:00:00Z", cost_context=snapshot,
    )
    assert ["Cost basis / currency", "ActualCost / EUR, USD"] in report["sections"][-1]["rows"]


@pytest.mark.parametrize("changes", [
    {"retrieved_at": "2026-10-29T13:30:00"},
    {"retrieved_at": "invalid"}, {"as_of": "2026-02-30"},
    {"partial": None}, {"managed_scopes": []}, {"run_id": ""}, {"code_version": ""},
])
def test_budget_snapshot_rejects_unverifiable_metadata(changes):
    with pytest.raises(ValueError):
        budget_snapshot(**changes)


def test_budget_snapshot_discloses_missing_currency_and_incomplete_retrieval():
    assert budget_snapshot(partial=True)["partial"]
    snapshot = budget_snapshot([{"properties": {"currentSpend": {"amount": 1}}}])
    assert snapshot["partial"] and snapshot["warnings"] and snapshot["currency"] is None
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            budget_snapshot([{"properties": {"currentSpend": {"amount": bad, "unit": "USD"}}}])
    with pytest.raises(ValueError, match="currency"):
        budget_snapshot([{"properties": {"currentSpend": {"unit": "dollars"}}}])
    assert budget_snapshot([])["budgets"] == []


@pytest.mark.parametrize("context", [None, {}, {"source": "Microsoft.Consumption/budgets"}])
def test_missing_provenance_cannot_appear_complete(context):
    report = models.build_cost_overview_model(
        {"total_usd": 1}, refreshed_at="2026-10-29T14:00:00Z", cost_context=context,
    )
    assert report["partial"]
    assert any("provenance" in w for w in report["warnings"])
    if context is not None:
        assert ["Completeness", "Partial"] in report["sections"][-1]["rows"]


@pytest.mark.parametrize("field", [
    "source", "snapshot_id", "code_version", "run_id", "partial", "cost_basis", "as_of", "retrieved_at",
])
def test_budget_provenance_requires_all_source_metadata(field):
    context = budget_snapshot()
    context.pop(field)
    report = models.build_budget_status_model(
        {"budgets": [], "summary": {}, "gates": []},
        refreshed_at="2026-10-29T14:00:00Z", cost_context=context,
    )
    assert report["partial"]
    assert ["Completeness", "Partial"] in report["sections"][-1]["rows"]


@pytest.mark.parametrize("days", [0, 1, 28, 29, 30, 31])
def test_overview_never_infers_a_thirty_day_window(days):
    report = models.build_cost_overview_model({
        "total_usd": days,
        "daily": [{"date": f"2026-10-{day:02d}", "cost_usd": 1} for day in range(1, days + 1)],
    }, refreshed_at="2026-10-31T14:00:00Z")
    headline = report["metrics"][0]
    assert headline["label"] == "Period spend"
    assert (f"({days} observed days)" if days else "usage dates unavailable") in headline["detail"]


def test_monthly_provenance_keeps_missing_closed_month_explicit():
    snapshot = usage.make_cost_snapshot(
        {"currency": "USD", "cost_metric": "AmortizedCost", "included_rows": []},
        managed_scopes=["/subscriptions/sub"], run_id="monthly-run", code_version="test-source",
        requested_start="2026-09-01", requested_end="2026-09-30",
    )
    report = models.build_monthly_review_model(
        {"month": "2026-09", "scorecard": {}},
        refreshed_at="2026-10-29T14:00:00Z", cost_context=snapshot,
    )
    fields = dict(report["sections"][-1]["rows"])
    assert fields["Requested usage dates"] == "2026-09-01 to 2026-09-30"
    assert "2026-10" not in fields["Observed usage dates"]
    assert fields["Completeness"] == "Partial" and report["partial"]
