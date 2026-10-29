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

# Complete synthetic daily costs on the same basis as the budget, growing $1/day.
# The newest of 46 observed days is treated as partial; prior days are explicitly present.
sub = "/subscriptions/000"
context = {
    "scope": sub,
    "currency": "USD",
    "filter": {},
    "period_start": "2025-06-01",
    "period_end_exclusive": "2025-07-01",
}
start = date(2025, 5, 1)
rows = [{"date": (start + timedelta(days=i)).isoformat(), "cost": 100.0 + i,
         "currency": "USD"} for i in range(46)]
daily = module.daily_costs(rows, currency="USD")
forecast = module.forecast_spend(
    daily, budget_amount=4000, context=context, budget_context=context,
    missing_days_are_zero=False,
)
assert forecast["month_end_forecast"] == 4365.0
assert forecast["budget_status"] == "forecast_over"
assert forecast["data_complete"] is True

# An omitted observation is unknown, not zero. Preserve the observed subtotal, not a fake total.
incomplete = dict(daily)
del incomplete[date(2025, 6, 5)]
partial = module.forecast_spend(incomplete, context=context)
assert partial["data_complete"] is False
assert partial["missing_mtd_days"] == ["2025-06-05"]
assert partial["month_end_forecast"] is None
assert partial["observed_month_to_date"] == 1790.0

# Cost per 1,000 requests (units from App Insights or a business metric).
unit = module.unit_economics(4200, 2_100_000, unit_name="request", per=1000,
                             previous_cost=3900, previous_units=1_800_000)
assert unit["cost_per_unit"] == 2.0 and unit["trend"] == "improving"

card = module.kpi_scorecard(
    allocation={"dimension": "team", "total_usd": 4200, "unallocated_pct": 18.0,
                "untagged_usd": 420, "tag_hygiene": [{"cost_affected": 60}]},
    budgets={"budgets": [{
        "id": f"{sub}/providers/Microsoft.Consumption/budgets/monthly",
        "scope": sub, "amount": 4000, "currency": "USD", "category": "Cost",
        "time_grain": "Monthly", "filter": {},
        "window": {"start": "2025-06-01", "end_exclusive": "2025-07-01"},
        "status": "forecast_over",
    }], "summary": {"over_budget": 0, "forecast_over": 1}},
    waste={"currency": "USD", "headline": {"potential_monthly_savings": 168},
           "findings": [{"resourceId": f"{sub}/resourceGroups/app/providers/Microsoft.Compute/disks/idle",
                         "kind": "orphan", "recommendedAction": "Delete verified unused disk",
                         "estMonthlySavings": 168, "currency": "USD"}]},
    rightsizing=[{"resourceId": f"{sub}/resourceGroups/app/providers/Microsoft.Compute/virtualMachines/vm",
                  "recommendedAction": "Resize to the validated target",
                  "estMonthlySavings": 140, "currency": "USD", "validated": True}],
    anomalies=[{"event_id": "synthetic-1", "date": "2025-06-14", "impact_usd": 30,
                "direction": "up", "currency": "USD"}],
    forecast=forecast,
    unit_costs=unit,
    managed_scopes=[f"{sub}/resourceGroups/app", "/subscriptions/111"],
    period_days=30,  # the separate allocation fixture covers a complete 30-day period
    as_of=date(2025, 6, 15),
)
assert card["kpi_health_pct"] == card["score_pct"] == 22.2
assert card["evidence_coverage_pct"] == 75.0
assert card["maturity"] == card["organizational_maturity"] == "unknown"
assert card["counts"] == {"pass": 2, "warn": 6, "fail": 1, "unknown": 3}
assert card["uncovered_budget_scopes"] == ["/subscriptions/111"]

routing = module.route_to_owners(
    [{"resourceId": f"{sub}/resourceGroups/app/providers/Microsoft.Compute/disks/d1",
      "estMonthlySavingsUsd": 40, "tags": {"team": "payments"}},
     {"resourceId": f"{sub}/resourceGroups/old/providers/Microsoft.Network/publicIPAddresses/p1",
      "estMonthlySavings": 3.65, "currency": "USD"}],
    source="waste",
)
assert routing["owners"][0]["owner"] == "payments"
assert routing["unrouted_usd"] == 3.65
```

Representative scorecard output (abridged). Commitment coverage, utilization and closed-period
forecast accuracy were not supplied: nine of twelve KPIs are known. The observed pass rate is not
an organizational maturity assessment; incomplete-month forecasts also remain unknown.

```json
{
  "as_of": "2025-06-15",
  "score_pct": 22.2,
  "kpi_health_pct": 22.2,
  "evidence_coverage_pct": 75.0,
  "maturity": "unknown",
  "organizational_maturity": "unknown",
  "maturity_reason": "Insufficient evidence: fewer than 80% of KPIs are known.",
  "counts": {"pass": 2, "warn": 6, "fail": 1, "unknown": 3},
  "kpis": [
    {"id": "allocated_pct", "value": 82.0, "target": 90.0, "status": "warn"},
    {"id": "untagged_pct", "value": 10.0, "target": 5.0, "status": "warn"},
    {"id": "tag_hygiene_pct", "value": 1.4, "target": 2.0, "status": "pass"},
    {"id": "budget_coverage_pct", "value": 50.0, "target": 100.0, "status": "warn"},
    {"id": "budgets_over", "value": 1, "target": 0, "status": "warn"},
    {"id": "forecast_vs_budget_pct", "value": 109.1, "target": 100.0, "status": "warn"},
    {"id": "forecast_accuracy_pct", "value": null, "target": 12.0, "status": "unknown"},
    {"id": "commitment_coverage_pct", "value": null, "target": 70.0, "status": "unknown"},
    {"id": "commitment_utilization_pct", "value": null, "target": 95.0, "status": "unknown"},
    {"id": "waste_pct", "value": 7.3, "target": 5.0, "status": "warn"},
    {"id": "anomaly_impact_pct", "value": 21.4, "target": 1.0, "status": "fail"},
    {"id": "unit_cost_change_pct", "value": -7.7, "target": 0.0, "status": "pass"}
  ],
  "uncovered_budget_scopes": ["/subscriptions/111"]
}
```
