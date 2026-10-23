"""Layer-1 unit tests for the cost-optimization-report skill (offline, deterministic)."""

import importlib.util
from datetime import date
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "finops"
    / "skills"
    / "finops-cost-optimization-report"
    / "summarize.py"
)
_spec = importlib.util.spec_from_file_location("summarize", _PATH)
summarize = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(summarize)

summarize_optimization = summarize.summarize_optimization

AS_OF = date(2026, 7, 15)


def _anomaly(dimension, value, impact, *, kind="spike", current=100.0, baseline=40.0):
    return {
        "dimension": dimension, "value": value, "kind": kind,
        "current_usd": current, "baseline_mean_usd": baseline, "impact_usd": impact,
    }


def _finding(rid, savings, *, kind="idle", action="Delete idle resource", validated=True):
    return {
        "resourceId": rid, "kind": kind, "recommendedAction": action,
        "estMonthlySavingsUsd": savings, "validated": validated,
    }


def _budget(name, amount, current, forecast, status, currency="USD"):
    return {
        "name": name, "amount": amount, "current_spend": current,
        "forecast_spend": forecast, "currency": currency, "status": status,
    }


def _budgets_result(budgets):
    summary = {"over_budget": 0, "forecast_over": 0, "at_risk": 0, "on_track": 0}
    for b in budgets:
        key = {"over_budget": "over_budget", "forecast_over": "forecast_over",
               "at_risk": "at_risk", "on_track": "on_track"}[b["status"]]
        summary[key] += 1
    return {"budgets": budgets, "summary": summary, "no_budgets": not budgets}


def _allocation(total, untagged_usd, unallocated_usd, *, hygiene=None, untagged_resources=None):
    return {
        "total_usd": total, "unallocated_usd": unallocated_usd,
        "unallocated_pct": round(100.0 * unallocated_usd / total, 1) if total else 0.0,
        "untagged_usd": untagged_usd,
        "untagged_resources": untagged_resources or [],
        "tag_hygiene": hygiene or [],
    }


def test_all_empty_is_a_clean_report():
    out = summarize_optimization(as_of=AS_OF)
    assert out["priorities"] == []
    assert out["headline"]["potential_monthly_savings"] == 0.0
    assert out["headline"]["total_monthly_spend"] is None
    assert out["headline"]["anomaly_count"] == 0
    assert out["budgets"]["no_budgets"] is False  # None budgets -> empty, not "no_budgets defined"
    assert out["rightsizing"]["top"] == []


def test_rightsizing_rollup_totals_and_top():
    findings = [_finding("a", 300.0), _finding("b", 120.0), _finding("c", None)]
    out = summarize_optimization(rightsizing=findings, as_of=AS_OF)
    assert out["rightsizing"]["potential_monthly_savings"] == 420.0
    assert out["rightsizing"]["count"] == 3
    assert out["headline"]["potential_monthly_savings"] == 420.0


def test_anomaly_rollup_count_and_top_impact():
    anomalies = [_anomaly("service", "AOAI", 500.0), _anomaly("rg", "prod", 200.0)]
    out = summarize_optimization(anomalies=anomalies, as_of=AS_OF)
    assert out["anomalies"]["count"] == 2
    assert out["anomalies"]["top_impact_usd"] == 500.0
    assert out["headline"]["top_anomaly_impact_usd"] == 500.0


def test_budget_buckets_and_overrun_math():
    budgets = _budgets_result([
        _budget("over", 1000.0, 1200.0, 1300.0, "over_budget"),
        _budget("fcst", 1000.0, 400.0, 1150.0, "forecast_over"),
        _budget("risk", 1000.0, 850.0, 950.0, "at_risk"),
    ])
    out = summarize_optimization(budgets=budgets, as_of=AS_OF)
    assert [b["name"] for b in out["budgets"]["over"]] == ["over"]
    assert [b["name"] for b in out["budgets"]["forecast_over"]] == ["fcst"]
    assert [b["name"] for b in out["budgets"]["at_risk"]] == ["risk"]
    gates = {g["name"]: g["overrun_usd"] for g in out["governance"]["budget_gates"]}
    assert gates["over"] == 200.0        # 1200 - 1000 (actual over)
    assert gates["fcst"] == 150.0        # 1150 - 1000 (forecast over)
    assert out["headline"]["budgets_over"] == 1
    assert out["headline"]["budgets_forecast_over"] == 1
    assert out["headline"]["budgets_at_risk"] == 1


def test_governance_from_allocation_and_gates():
    hygiene = [{"canonical": "TeamA", "variants": {"TeamA": 90.0, "team-a": 60.0}, "cost_affected": 150.0}]
    alloc = _allocation(1000.0, untagged_usd=250.0, unallocated_usd=250.0,
                        hygiene=hygiene, untagged_resources=[{"resourceId": "x", "monthly_usd": 250.0}])
    out = summarize_optimization(allocation=alloc, as_of=AS_OF)
    gov = out["governance"]
    assert gov["untagged_usd"] == 250.0
    assert gov["unallocated_pct"] == 25.0
    assert gov["tag_hygiene"][0]["cost_affected"] == 150.0
    assert out["headline"]["untagged_usd"] == 250.0


def test_priorities_blended_ranked_and_labelled():
    findings = [_finding("save", 300.0)]
    anomalies = [_anomaly("service", "AOAI", 500.0)]
    budgets = _budgets_result([_budget("over", 1000.0, 1200.0, 1300.0, "over_budget")])
    alloc = _allocation(1000.0, untagged_usd=250.0, unallocated_usd=250.0)
    out = summarize_optimization(anomalies=anomalies, rightsizing=findings,
                                 allocation=alloc, budgets=budgets, as_of=AS_OF)
    prio = out["priorities"]
    # ranked by dollar impact desc: anomaly 500 > rightsizing 300 > untagged 250 > budget overrun 200
    assert [p["impact_type"] for p in prio] == ["spike", "savings", "governance", "overrun"]
    assert [p["impact_usd"] for p in prio] == [500.0, 300.0, 250.0, 200.0]
    assert [p["rank"] for p in prio] == [1, 2, 3, 4]
    # savings and overrun are distinct types, never merged into one number
    assert {p["category"] for p in prio} == {"anomaly", "rightsizing", "governance", "budget"}


def test_unknown_impact_items_kept_and_sorted_last():
    findings = [_finding("known", 300.0), _finding("unknown", None)]
    out = summarize_optimization(rightsizing=findings, as_of=AS_OF)
    prio = out["priorities"]
    assert prio[0]["impact_usd"] == 300.0
    assert prio[-1]["impact_usd"] is None
    assert len(prio) == 2


def test_missing_inputs_become_empty_sections():
    out = summarize_optimization(rightsizing=[_finding("a", 100.0)], as_of=AS_OF)
    assert out["anomalies"]["count"] == 0
    assert out["governance"]["untagged_usd"] == 0.0
    assert out["budgets"]["over"] == []
    assert out["headline"]["total_monthly_spend"] is None


def test_priorities_capped_at_ten():
    # 8 rightsizing (top-per-section cap) + 8 anomalies = 16 candidates -> priorities cap at 10.
    findings = [_finding(f"r{i}", float(1000 - i)) for i in range(8)]
    anomalies = [_anomaly("service", f"s{i}", float(100 - i)) for i in range(8)]
    out = summarize_optimization(rightsizing=findings, anomalies=anomalies, as_of=AS_OF)
    assert len(out["priorities"]) == 10
    assert out["priorities"][0]["impact_usd"] == 1000.0
    assert out["priorities"][0]["impact_type"] == "savings"


def test_waste_and_commitments_are_opt_in_and_never_summed():
    rs_vm = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Network/publicIPAddresses/dup"
    rightsizing = [{"resourceId": rs_vm, "kind": "idle", "recommendedAction": "Delete",
                    "estMonthlySavingsUsd": 50.0, "validated": True}]
    waste = {
        "currency": "USD",
        "findings": [
            {"resourceId": rs_vm.upper(), "kind": "orphan", "reason": "dup",
             "recommendedAction": "Delete", "estMonthlySavings": 50.0},
            {"resourceId": "/x/natGateways/nat1", "kind": "orphan", "reason": "NAT idle",
             "recommendedAction": "Delete", "estMonthlySavings": 32.85},
            {"resourceId": "/x/networkInterfaces/nic1", "kind": "hygiene", "reason": "NIC",
             "recommendedAction": "Delete", "estMonthlySavings": 0.0},
        ],
    }
    commitments = {
        "currency": "USD",
        "headline": {"potential_monthly_savings": 400.0, "best_family": "reservation",
                     "coverage_pct": 35.0, "unused_commitment_cost": 120.0},
        "purchases": [
            {"family": "reservation", "term": "P3Y", "sku": "D4s_v5",
             "est_monthly_savings": 400.0, "alternatives": [{"term": "P1Y"}]},
            {"family": "savings_plan", "term": "P3Y", "sku": "Compute_Savings_Plan",
             "est_monthly_savings": 350.0, "alternatives": []},
        ],
        "underutilized": [{"name": "ri1", "utilization": 40.0}],
        "expiring": [{"name": "ri2", "days_to_expiry": 12}],
    }

    base = summarize_optimization(rightsizing=rightsizing, as_of=AS_OF)
    assert "waste" not in base and "commitments" not in base
    assert "waste_monthly_savings" not in base["headline"]

    out = summarize_optimization(rightsizing=rightsizing, waste=waste,
                                 commitments=commitments, as_of=AS_OF)
    h = out["headline"]
    assert h["potential_monthly_savings"] == 50.0
    assert h["waste_monthly_savings"] == 32.85
    assert h["usage_optimization_monthly_savings"] == 82.85
    assert h["rate_optimization_monthly_savings"] == 400.0
    assert h["commitment_coverage_pct"] == 35.0
    assert out["waste"]["deduplicated"] == 1
    assert out["waste"]["count"] == 2

    titles = [p["title"] for p in out["priorities"]]
    assert titles[0] == "Buy P3Y reservation — D4s_v5"
    assert not any(t.startswith("Buy") and "savings plan" in t for t in titles)
    assert any(p["impact_type"] == "unused commitment" for p in out["priorities"])
    assert any(p["impact_type"] == "risk" and "12 days" in p["title"]
               for p in out["priorities"])
    assert not any(p["category"] == "waste" and "NIC" in p["title"]
                   for p in out["priorities"])

def test_drop_anomalies_are_labelled_as_drops():
    drop = _anomaly("resourceGroup", "rg-old", 28.0, kind="drop", current=0.0, baseline=10.0)
    out = summarize_optimization(anomalies=[drop], as_of=AS_OF)
    item = next(p for p in out["priorities"] if p["category"] == "anomaly")
    assert item["impact_type"] == "drop"
    assert item["action"].startswith("Confirm the drop is expected")


def test_governance_detail_distinguishes_untagged_from_unallocated():
    alloc = {"dimension": "team", "total_usd": 100.0, "untagged_usd": 35.0,
             "unallocated_usd": 100.0, "unallocated_pct": 100.0}
    out = summarize_optimization(allocation=alloc, as_of=AS_OF)
    item = next(p for p in out["priorities"] if p["category"] == "governance")
    assert item["detail"] == "no tags at all; separately 100.0% of spend lacks the 'team' tag"


def test_spend_period_is_reported_in_headline():
    out = summarize_optimization(allocation={"total_usd": 10.0},
                                 spend_period=("2026-10-01", "2026-10-22T00:00:00"), as_of=AS_OF)
    assert out["headline"]["spend_period"] == {"from": "2026-10-01", "to": "2026-10-22"}
    assert "spend_period" not in summarize_optimization(as_of=AS_OF)["headline"]
