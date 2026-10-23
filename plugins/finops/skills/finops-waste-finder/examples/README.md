# Waste finder example

Run from the repository root. The rows below are offline fixtures shaped like raw Azure Resource
Graph output. Live runs query every effective managed scope and attach monthly cost from prepared
UsageDetails rows.

```python
import importlib.util
from datetime import date
from pathlib import Path

path = Path("plugins/finops/skills/finops-waste-finder/waste.py")
spec = importlib.util.spec_from_file_location("waste", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

rg = "/subscriptions/000/resourceGroups/legacy/providers"
resources = [
    {"id": f"{rg}/Microsoft.Network/publicIPAddresses/old-pip",
     "type": "microsoft.network/publicipaddresses", "sku": {"name": "Standard"},
     "properties": {"ipAddress": "20.1.2.3"}},
    {"id": f"{rg}/Microsoft.Network/virtualNetworkGateways/vpn-gw",
     "type": "microsoft.network/virtualnetworkgateways",
     "properties": {"gatewayType": "Vpn"}},
    {"id": f"{rg}/Microsoft.Network/connections/other",
     "type": "microsoft.network/connections",
     "properties": {"virtualNetworkGateway1": {"id": f"{rg}/Microsoft.Network/virtualNetworkGateways/live-gw"}}},
    {"id": f"{rg}/Microsoft.Compute/snapshots/pre-migration",
     "type": "microsoft.compute/snapshots",
     "properties": {"timeCreated": "2024-01-15T00:00:00Z", "diskSizeGB": 512}},
]
costs = {
    f"{rg}/Microsoft.Network/publicIPAddresses/old-pip": 3.65,
    f"{rg}/Microsoft.Network/virtualNetworkGateways/vpn-gw": 138.70,
    f"{rg}/Microsoft.Compute/snapshots/pre-migration": 25.60,
}

result = module.find_waste(resources, costs=costs, currency="USD", as_of=date(2025, 6, 1))
assert result["headline"]["potential_monthly_savings"] == 167.95
assert [f["kind"] for f in result["findings"]] == ["idle", "stale", "orphan"]
```

Representative output (abridged):

```json
{
  "as_of": "2025-06-01",
  "currency": "USD",
  "headline": {
    "finding_count": 3,
    "actionable_count": 3,
    "potential_monthly_savings": 167.95,
    "unknown_cost_count": 0,
    "by_kind": {"idle": 1, "orphan": 1, "stale": 1}
  },
  "findings": [
    {"resourceId": "/subscriptions/000/resourceGroups/legacy/providers/Microsoft.Network/virtualNetworkGateways/vpn-gw",
     "kind": "idle", "reason": "Vpn gateway with no connections",
     "monthlyCost": 138.7, "estMonthlySavings": 138.7},
    {"resourceId": "/subscriptions/000/resourceGroups/legacy/providers/Microsoft.Compute/snapshots/pre-migration",
     "kind": "stale", "reason": "Snapshot older than 90 days",
     "monthlyCost": 25.6, "estMonthlySavings": 25.6},
    {"resourceId": "/subscriptions/000/resourceGroups/legacy/providers/Microsoft.Network/publicIPAddresses/old-pip",
     "kind": "orphan", "reason": "Unassociated public IP address",
     "monthlyCost": 3.65, "estMonthlySavings": 3.65}
  ],
  "warnings": []
}
```
