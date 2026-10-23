# Commitment advisor example

Run from the repository root. Inputs are offline fixtures shaped like the raw `value` items of
the read-only Reservations, Savings Plans, Consumption `reservationRecommendations`, and Cost
Management `benefitRecommendations` GET responses. Live runs pull them per effective scope
after resolving the managed boundary.

```python
import importlib.util
from datetime import date
from pathlib import Path

path = Path("plugins/finops/skills/finops-commitment-advisor/commit.py")
spec = importlib.util.spec_from_file_location("commit", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

sub = "/subscriptions/00000000-0000-0000-0000-000000000000"
reservations = [{
    "id": "/providers/microsoft.capacity/reservationOrders/o1/reservations/r1",
    "sku": {"name": "Standard_D4s_v5"},
    "properties": {
        "displayName": "d4s-eastus",
        "reservedResourceType": "VirtualMachines",
        "term": "P1Y",
        "quantity": 4,
        "appliedScopeType": "Single",
        "appliedScopes": [sub],
        "renew": False,
        "expiryDateTime": "2025-07-20T00:00:00Z",
        "displayProvisioningState": "Succeeded",
        "utilization": {"aggregates": [
            {"grain": 7, "grainUnit": "days", "value": 61.0, "valueUnit": "percentage"},
            {"grain": 30, "grainUnit": "days", "value": 58.5, "valueUnit": "percentage"},
        ]},
    },
}]
reservation_recommendations = [{
    "location": "westus",
    "properties": {
        "subscriptionId": "00000000-0000-0000-0000-000000000000",
        "resourceType": "virtualmachines", "skuName": "Standard_E8s_v5",
        "scope": "Single", "lookBackPeriod": "Last30Days", "recommendedQuantity": 2,
        "term": term, "netSavings": savings, "costWithNoReservedInstances": on_demand,
    },
} for term, savings, on_demand in (("P1Y", 3600.0, 10000.0), ("P3Y", 18000.0, 30000.0))]
benefit_recommendations = [{
    "kind": "SavingsPlan",
    "properties": {
        "subscriptionId": "00000000-0000-0000-0000-000000000000",
        "scope": "Single", "term": "P3Y", "lookBackPeriod": "Last30Days",
        "totalHours": 720, "currencyCode": "USD", "costWithoutBenefit": 2000.0,
        "recommendationDetails": {
            "savingsAmount": 400.0, "commitmentAmount": 1.5, "coveragePercentage": 80.0,
        },
    },
}]

result = module.analyze_commitments(
    reservations=reservations,
    reservation_recommendations=reservation_recommendations,
    benefit_recommendations=benefit_recommendations,
    managed_scopes=[sub],
    currency="USD",
    as_of=date(2025, 6, 1),
)
assert result["headline"]["potential_monthly_savings"] == 500.0
assert result["purchases"][0]["term"] == "P3Y"
assert result["underutilized"][0]["utilization"] == 58.5
assert result["expiring"][0]["days_to_expiry"] == 49
```

Representative output (abridged):

```json
{
  "as_of": "2025-06-01",
  "currency": "USD",
  "headline": {
    "commitment_count": 1,
    "underutilized_count": 1,
    "expiring_count": 1,
    "purchase_recommendation_count": 2,
    "potential_monthly_savings": 500.0,
    "best_family": "reservation",
    "coverage_pct": null,
    "unused_commitment_cost": null,
    "hybrid_benefit_candidates": 0
  },
  "purchases": [
    {"family": "reservation", "sku": "Standard_E8s_v5", "term": "P3Y",
     "est_monthly_savings": 500.0, "savings_pct": 60.0,
     "alternatives": [{"term": "P1Y", "est_monthly_savings": 300.0}]},
    {"family": "savings_plan", "sku": "Compute_Savings_Plan", "term": "P3Y",
     "est_monthly_savings": 405.56, "hourly_commitment": 1.5}
  ],
  "warnings": [
    "Reservation and savings plan recommendations cover the same usage; they are alternatives, so the headline uses the larger family, not the sum"
  ]
}
```
