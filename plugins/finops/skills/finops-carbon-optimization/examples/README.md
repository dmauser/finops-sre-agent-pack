# FinOps carbon optimization example

Run from the repository root. The responses below are offline fixture data. A live run saves the
JSON files written by the human-run script from `human_run_script(...)`.

```python
import importlib.util
from pathlib import Path

path = Path("plugins/finops/skills/finops-carbon-optimization/carbon.py")
spec = importlib.util.spec_from_file_location("carbon", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

scopes = ["/subscriptions/00000000-0000-0000-0000-000000000000"]
requests = module.standard_requests(scopes, "2026-10-23")
script = module.human_run_script(requests)

vm = "/subscriptions/00000000-0000-0000-0000-000000000000/resourcegroups/app/providers/microsoft.compute/virtualmachines/web-1"
responses = {
    "overall": {
        "subscriptionAccessDecisionList": [
            {"subscriptionId": "00000000-0000-0000-0000-000000000000", "decision": "Allowed"},
        ],
        "value": [{"dataType": "OverallSummaryData", "latestMonthEmissions": 120.0,
                   "previousMonthEmissions": 100.0}],
    },
    "top-resources": {"value": [
        {"dataType": "ResourceTopItemsSummaryData", "itemName": "web-1", "categoryType": "Resource",
         "resourceId": vm, "latestMonthEmissions": 80.0, "previousMonthEmissions": 60.0},
    ]},
}
result = module.analyze_carbon(responses, managed_scopes=scopes)
```

Representative output (abbreviated):

```json
{
  "unit": "kgCO2e",
  "overall": {"latest_kg": 120.0, "previous_kg": 100.0, "change_kg": 20.0, "change_pct": 20.0},
  "top_items": {"Resource": [{"name": "web-1", "latest_kg": 80.0, "change_pct": 33.33}]},
  "increases": [{"name": "web-1", "change_kg": 20.0, "change_pct": 33.33}],
  "hints": [
    {"type": "emissions_increase", "detail": "Emissions rose 20.0% month over month; check the largest increases."},
    {"type": "concentration", "detail": "The top 1 resources account for 66.7% of emissions; rightsizing and waste findings on them reduce both cost and carbon."}
  ]
}
```
