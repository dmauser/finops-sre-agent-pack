# KPI scorecard example

Run from the repository root. The inputs are offline fixtures shaped like the outputs of the sibling
skills (`allocate_costs`, `evaluate_budgets`, `find_waste`, `recommend_rightsizing`,
`detect_anomalies`) and prepared UsageDetails rows. Live runs use the real results for every
effective managed scope.

```python
import importlib.util
from datetime import date, timedelta
from pathlib import Path

path = Path("plugins/finops/skills/finops-kpi-scorecard/scorecard.py")
spec = importlib.util.spec_from_file_location("scorecard", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

# 46 days of AmortizedCost growing $1/day; the newest day is treated as partial.
start = date(2025, 5, 1)
rows = [{"date": (start + timedelta(days=i)).isoformat(), "cost": 100.0 + i} for i in range(46)]
forecast = module.forecast_spend(module.daily_costs(rows), budget_amount=4000)
assert forecast["month_end_forecast"] == 4365.0
assert forecast["budget_status"] == "forecast_over"

# Cost per 1,000 requests (units from App Insights or a business metric).
unit = module.unit_economics(4200, 2_100_000, unit_name="request", per=1000,
                             previous_cost=3900, previous_units=1_800_000)
assert unit["cost_per_unit"] == 2.0 and unit["trend"] == "improving"

sub = "/subscriptions/000"
card = module.kpi_scorecard(
    allocation={"dimension": "team", "total_usd": 4200, "unallocated_pct": 18.0,
                "untagged_usd": 420, "tag_hygiene": [{"cost_affected": 60}]},
    budgets={"budgets": [{"scope": sub}], "summary": {"over_budget": 0, "forecast_over": 1}},
    waste={"headline": {"potential_monthly_savings": 168}},
    rightsizing=[{"estMonthlySavingsUsd": 140}],
    anomalies=[{"impact_usd": 30, "direction": "up"}],
    forecast=forecast,
    unit_costs=unit,
    managed_scopes=[f"{sub}/resourceGroups/app", "/subscriptions/111"],
    as_of=date(2025, 6, 15),
)
assert card["maturity"] == "crawl"
assert card["uncovered_budget_scopes"] == ["/subscriptions/111"]

routing = module.route_to_owners(
    [{"resourceId": f"{sub}/resourceGroups/app/providers/Microsoft.Compute/disks/d1",
      "estMonthlySavingsUsd": 40, "tags": {"team": "payments"}},
     {"resourceId": f"{sub}/resourceGroups/old/providers/Microsoft.Network/publicIPAddresses/p1",
      "estMonthlySavings": 3.65}],
    source="waste",
)
assert routing["owners"][0]["owner"] == "payments"
assert routing["unrouted_usd"] == 3.65
```

Representative scorecard output (abridged):

```json
{
  "as_of": "2025-06-15",
  "score_pct": 22.2,
  "maturity": "crawl",
  "counts": {"pass": 2, "warn": 6, "fail": 1, "unknown": 2},
  "kpis": [
    {"id": "allocated_pct", "value": 82.0, "target": 90.0, "status": "warn"},
    {"id": "untagged_pct", "value": 10.0, "target": 5.0, "status": "warn"},
    {"id": "tag_hygiene_pct", "value": 1.4, "target": 2.0, "status": "pass"},
    {"id": "budget_coverage_pct", "value": 50.0, "target": 100.0, "status": "warn"},
    {"id": "budgets_over", "value": 1, "target": 0, "status": "warn"},
    {"id": "forecast_vs_budget_pct", "value": 109.1, "target": 100.0, "status": "warn"},
    {"id": "commitment_coverage_pct", "value": null, "target": 70.0, "status": "unknown"},
    {"id": "commitment_utilization_pct", "value": null, "target": 95.0, "status": "unknown"},
    {"id": "waste_pct", "value": 7.3, "target": 5.0, "status": "warn"},
    {"id": "anomaly_impact_pct", "value": 21.4, "target": 1.0, "status": "fail"},
    {"id": "unit_cost_change_pct", "value": -7.7, "target": 0.0, "status": "pass"}
  ],
  "uncovered_budget_scopes": ["/subscriptions/111"]
}
```
