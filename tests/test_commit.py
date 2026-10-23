"""Layer-1 unit tests for the commitment advisor (offline, deterministic, no Azure)."""

import importlib.util
from datetime import date
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "finops" / "skills" / "finops-commitment-advisor" / "commit.py"
)
_spec = importlib.util.spec_from_file_location("commit", _PATH)
commit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(commit)

analyze = commit.analyze_commitments

SUB = "/subscriptions/11111111-1111-1111-1111-111111111111"
OTHER = "/subscriptions/22222222-2222-2222-2222-222222222222"
TODAY = date(2025, 6, 1)


def _reservation(name, *, scope_type="Single", scopes=(SUB,), util30=95.0, util7=None,
                 expiry="2026-06-01T00:00:00Z", renew=True, state="Succeeded"):
    aggregates = []
    if util7 is not None:
        aggregates.append({"grain": 7, "grainUnit": "days", "value": util7})
    if util30 is not None:
        aggregates.append({"grain": 30, "grainUnit": "days", "value": util30})
    return {
        "id": f"/providers/microsoft.capacity/reservationOrders/o/reservations/{name}",
        "sku": {"name": "Standard_D2s_v5"},
        "properties": {
            "displayName": name, "reservedResourceType": "VirtualMachines", "term": "P1Y",
            "quantity": 2, "appliedScopeType": scope_type, "appliedScopes": list(scopes),
            "renew": renew, "expiryDateTime": expiry, "displayProvisioningState": state,
            "utilization": {"aggregates": aggregates},
        },
    }


def _ri_rec(term, net, *, sku="Standard_E4s_v5", on_demand=None, currency=None):
    savings = {"currency": currency, "value": net} if currency else net
    return {
        "location": "eastus",
        "properties": {
            "subscriptionId": SUB.split("/")[-1], "resourceType": "virtualmachines",
            "skuName": sku, "scope": "Single", "lookBackPeriod": "Last30Days", "term": term,
            "recommendedQuantity": 1, "netSavings": savings,
            "costWithNoReservedInstances": on_demand,
        },
    }


def _sp_rec(term, savings, hours=720, currency="USD"):
    return {
        "kind": "SavingsPlan",
        "properties": {
            "subscriptionId": SUB.split("/")[-1], "scope": "Single", "term": term,
            "lookBackPeriod": "Last30Days", "totalHours": hours, "currencyCode": currency,
            "recommendationDetails": {"savingsAmount": savings, "commitmentAmount": 2.0},
        },
    }


def test_reservation_net_savings_is_normalized_by_term_months():
    result = analyze(reservation_recommendations=[_ri_rec("P1Y", 1200.0), _ri_rec("P3Y", 7200.0)],
                     currency="USD", as_of=TODAY)
    [rec] = result["purchases"]
    assert rec["term"] == "P3Y"
    assert rec["est_monthly_savings"] == 200.0
    assert rec["alternatives"] == [{"term": "P1Y", "est_monthly_savings": 100.0}]
    assert result["headline"]["potential_monthly_savings"] == 200.0


def test_savings_plan_savings_is_normalized_by_lookback_hours():
    result = analyze(benefit_recommendations=[_sp_rec("P3Y", 300.0, hours=720)], as_of=TODAY)
    [rec] = result["purchases"]
    assert rec["est_monthly_savings"] == round(300.0 / 720 * 730, 2)
    assert rec["currency"] == "USD"


def test_savings_plan_falls_back_to_lookback_period_when_hours_missing():
    item = _sp_rec("P1Y", 70.0, hours=None)
    item["properties"]["lookBackPeriod"] = "Last7Days"
    [rec] = analyze(benefit_recommendations=[item], as_of=TODAY)["purchases"]
    assert rec["est_monthly_savings"] == round(70.0 / 168 * 730, 2)


def test_reservation_and_savings_plan_are_alternatives_not_summed():
    result = analyze(reservation_recommendations=[_ri_rec("P3Y", 3600.0)],
                     benefit_recommendations=[_sp_rec("P3Y", 50.0)],
                     currency="USD", as_of=TODAY)
    assert result["headline"]["potential_monthly_savings"] == 100.0
    assert result["headline"]["best_family"] == "reservation"
    assert any("alternatives" in w for w in result["warnings"])


def test_modern_amount_objects_carry_currency():
    result = analyze(reservation_recommendations=[_ri_rec("P1Y", 1200.0, currency="eur")],
                     as_of=TODAY)
    assert result["purchases"][0]["currency"] == "EUR"
    assert result["currency"] == "EUR"


def test_mixed_currencies_have_no_single_headline():
    result = analyze(reservation_recommendations=[_ri_rec("P1Y", 1200.0, currency="EUR")],
                     benefit_recommendations=[_sp_rec("P1Y", 100.0, currency="USD")],
                     as_of=TODAY)
    assert result["headline"]["potential_monthly_savings"] is None
    assert set(result["totals_by_currency"]) == {"EUR", "USD"}


def test_small_and_malformed_recommendations_are_dropped():
    bad = _ri_rec("P9Y", 1000.0)
    result = analyze(reservation_recommendations=[_ri_rec("P1Y", 12.0), bad],
                     currency="USD", as_of=TODAY)
    assert result["purchases"] == []
    assert any("skipped" in w for w in result["warnings"])


def test_underutilized_uses_30_day_then_7_day():
    result = analyze(reservations=[
        _reservation("low30", util30=50.0, util7=99.0),
        _reservation("low7", util30=None, util7=40.0),
        _reservation("fine", util30=97.0),
    ], as_of=TODAY)
    assert [c["name"] for c in result["underutilized"]] == ["low7", "low30"]
    assert result["underutilized"][1]["utilization"] == 50.0


def test_expiring_only_without_auto_renew_inside_window():
    result = analyze(reservations=[
        _reservation("soon", expiry="2025-06-20T00:00:00Z", renew=False),
        _reservation("renews", expiry="2025-06-20T00:00:00Z", renew=True),
        _reservation("later", expiry="2025-12-01T00:00:00Z", renew=False),
    ], as_of=TODAY)
    assert [c["name"] for c in result["expiring"]] == ["soon"]
    assert result["expiring"][0]["days_to_expiry"] == 19


def test_single_scope_outside_boundary_is_excluded_shared_is_kept():
    result = analyze(reservations=[
        _reservation("mine"),
        _reservation("theirs", scopes=(OTHER,)),
        _reservation("shared", scope_type="Shared", scopes=()),
    ], managed_scopes=[SUB], as_of=TODAY)
    names = [c["name"] for c in result["inventory"]]
    assert names == ["mine", "shared"]
    assert any("excluded" in w for w in result["warnings"])
    assert any("Shared" in w for w in result["warnings"])


def test_cancelled_commitments_are_not_inventoried():
    result = analyze(reservations=[_reservation("gone", state="Cancelled")], as_of=TODAY)
    assert result["inventory"] == []


def test_savings_plan_inventory_reads_hourly_commitment_and_scope_properties():
    plan = {
        "id": "/providers/Microsoft.BillingBenefits/savingsPlanOrders/o/savingsPlans/sp1",
        "sku": {"name": "Compute_Savings_Plan"},
        "properties": {
            "displayName": "sp1", "term": "P3Y", "appliedScopeType": "Single",
            "appliedScopeProperties": {"subscriptionId": SUB},
            "commitment": {"grain": "Hourly", "currencyCode": "usd", "amount": 4.5},
            "renew": False, "expiryDateTime": "2028-01-01T00:00:00Z",
            "displayProvisioningState": "Succeeded",
            "utilization": {"aggregates": [{"grain": 30, "value": 72.0}]},
        },
    }
    result = analyze(savings_plans=[plan], managed_scopes=[SUB], as_of=TODAY)
    [sp] = result["inventory"]
    assert sp["kind"] == "savings_plan"
    assert sp["hourly_commitment"] == 4.5
    assert sp["commitment_currency"] == "USD"
    assert result["underutilized"][0]["name"] == "sp1"


def test_coverage_from_amortized_rows():
    rows = [
        {"cost": 60.0, "chargeType": "Usage", "pricingModel": "Reservation",
         "meterCategory": "Virtual Machines"},
        {"cost": 40.0, "chargeType": "Usage", "pricingModel": "OnDemand",
         "meterCategory": "Virtual Machines"},
        {"cost": 100.0, "chargeType": "Usage", "pricingModel": "OnDemand",
         "meterCategory": "Storage"},
        {"cost": 15.0, "chargeType": "UnusedReservation", "pricingModel": "Reservation"},
        {"cost": 5.0, "chargeType": "Usage", "pricingModel": "Spot",
         "meterCategory": "Virtual Machines"},
    ]
    coverage = analyze(usage_rows=rows, as_of=TODAY)["coverage"]
    assert coverage["coverage_pct"] == 30.0
    assert coverage["unused_commitment_cost"] == 15.0
    vm = next(c for c in coverage["by_meter_category"] if c["meter_category"] == "Virtual Machines")
    assert vm["coverage_pct"] == 60.0


def test_hybrid_benefit_candidates():
    win = f"{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/win"
    ahb = f"{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/ahb"
    lin = f"{SUB}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/lin"
    sql = f"{SUB}/resourceGroups/rg/providers/Microsoft.SqlVirtualMachine/sqlVirtualMachines/sql"
    dev = f"{SUB}/resourceGroups/rg/providers/Microsoft.SqlVirtualMachine/sqlVirtualMachines/dev"
    outside = f"{OTHER}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/win2"

    def vm(rid, os_type, license_type=None):
        return {"id": rid, "properties": {"licenseType": license_type,
                                          "storageProfile": {"osDisk": {"osType": os_type}}}}

    result = analyze(
        vms=[vm(win, "Windows"), vm(ahb, "Windows", "Windows_Server"), vm(lin, "Linux"),
             vm(outside, "Windows")],
        sql_vms=[{"id": sql, "properties": {"sqlServerLicenseType": "PAYG",
                                            "sqlImageSku": "Enterprise"}},
                 {"id": dev, "properties": {"sqlServerLicenseType": "PAYG",
                                            "sqlImageSku": "Developer"}}],
        costs={sql.upper(): 900.0, win: 300.0},
        managed_scopes=[SUB], as_of=TODAY,
    )
    ids = [c["resourceId"] for c in result["hybrid_benefit"]]
    assert ids == [sql, win]
    assert result["hybrid_benefit"][0]["monthly_cost"] == 900.0
    assert any("entitlement" in w for w in result["warnings"])


def test_empty_input_is_stable():
    result = analyze(as_of=TODAY)
    assert result["headline"]["commitment_count"] == 0
    assert result["headline"]["potential_monthly_savings"] is None
    assert result["coverage"] is None
    assert result["warnings"] == []
    assert result["headline"]["inventory_unavailable"] == []


def test_inventory_unavailable_is_a_gap_not_zero_commitments():
    rows = [
        {"cost": 60.0, "chargeType": "Usage", "pricingModel": "SavingsPlan",
         "meterCategory": "Virtual Machines"},
        {"cost": 40.0, "chargeType": "Usage", "pricingModel": "OnDemand",
         "meterCategory": "Virtual Machines"},
        {"cost": 8.0, "chargeType": "UnusedSavingsPlan", "pricingModel": "SavingsPlan"},
    ]
    result = analyze(usage_rows=rows, as_of=TODAY,
                     inventory_unavailable=["savings_plan", "reservation", "bogus"])
    assert result["headline"]["inventory_unavailable"] == ["reservation", "savings_plan"]
    gap = next(w for w in result["warnings"] if w.startswith("Commitment inventory unavailable"))
    assert "Reservations Reader" in gap and "Savings plan Reader" in gap
    assert result["headline"]["coverage_pct"] == 60.0
    assert result["headline"]["unused_commitment_cost"] == 8.0


def test_shared_scope_benefit_recommendations_are_excluded_for_managed_scope():
    shared = {
        "kind": "SavingsPlan",
        "properties": {
            "scope": "Shared",
            "term": "P3Y",
            "lookBackPeriod": "Last30Days",
            "costWithoutBenefit": 2_000_000.0,
            "recommendationDetails": {"savingsAmount": 500_000.0, "commitmentAmount": 1000.0},
            "totalHours": 720,
        },
    }
    out = analyze(
        benefit_recommendations=[shared],
        managed_scopes=[SUB],
        as_of=TODAY,
    )
    assert out["purchases"] == []
    assert any("Shared-scope" in w for w in out["warnings"])
