# Cost allocation example

Run from the repository root. Resource IDs are matched case-insensitively.

These resource IDs are offline fixture data. Live UsageDetails rows are retrieved per dynamically
discovered managed scope, de-duplicated, and filtered before allocation; the final report also
discloses included, excluded, and unattributed cost coverage.

```python
import importlib.util
from pathlib import Path

path = Path("plugins/finops/skills/finops-cost-allocation/allocate.py")
spec = importlib.util.spec_from_file_location("allocate", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

vm = "/subscriptions/000/resourceGroups/prod/providers/Microsoft.Compute/virtualMachines/api-01"
cache = "/subscriptions/000/resourceGroups/prod/providers/Microsoft.Cache/Redis/cache-01"
logs = "/subscriptions/000/resourceGroups/shared/providers/Microsoft.Storage/storageAccounts/logs01"

costs = {vm: 140.0, cache: 60.0, logs: 50.0}
tags = {
    vm: {"team": "Payments", "env": "prod"},
    cache: {"team": "payments", "env": "prod"},
    logs: {"env": "prod"},
}

report = module.allocate_costs(costs, tags, dimension="team")

# Tag inheritance: logs01 inherits team=platform from its resource group.
inherited = module.allocate_costs(
    costs,
    tags,
    dimension="team",
    inherited_tags={"/subscriptions/000/resourceGroups/shared": {"team": "platform"}},
)
assert inherited["inheritance"]["allocated_via_inheritance_usd"] == 50.0

# Shared-cost split: logs01 is redistributed proportionally to direct cost.
split = module.allocate_costs(costs, tags, dimension="team", shared={"resource_ids": [logs]})
assert split["shared_costs"]["distribution"] == [{"owner": "Payments", "shared_usd": 50.0}]
assert split["groups"][0]["monthly_usd"] == 200.0  # direct (backward compatible)
assert split["groups"][0]["fully_loaded_usd"] == 250.0

# Month-over-month variance against a previous month.
mom = module.month_over_month(costs, {vm: 100.0, cache: 60.0}, dimension_tags=tags)
assert mom["delta_usd"] == 90.0
assert mom["movers"]["new"][0]["resourceId"] == logs
assert mom["movers"]["increased"][0]["delta_usd"] == 40.0
```

Representative output (abbreviated):

```json
{
  "dimension": "team",
  "total_usd": 250.0,
  "allocated_usd": 200.0,
  "unallocated_usd": 50.0,
  "unallocated_pct": 20.0,
  "groups": [{
    "owner": "Payments",
    "monthly_usd": 200.0,
    "pct": 80.0,
    "resource_count": 2,
    "inherited_usd": 0.0,
    "direct_usd": 200.0,
    "shared_usd": 0.0,
    "fully_loaded_usd": 200.0
  }],
  "unallocated": {
    "monthly_usd": 50.0,
    "pct": 20.0,
    "resource_count": 1,
    "resources": [{"resourceId": ".../storageAccounts/logs01", "monthly_usd": 50.0}]
  },
  "tag_inventory": [
    {"key": "env", "resource_count": 3, "cost_usd": 250.0, "pct": 100.0},
    {"key": "team", "resource_count": 2, "cost_usd": 200.0, "pct": 80.0}
  ],
  "missing_recommended": ["service", "costCenter", "app", "owner"],
  "tag_hygiene": [{
    "canonical": "Payments",
    "variants": {"Payments": 140.0, "payments": 60.0},
    "cost_affected": 200.0
  }]
}
```
