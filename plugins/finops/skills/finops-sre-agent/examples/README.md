# FinOps for SRE Agent example

Run from the repository root. The resource IDs below are offline fixture data. A live run pulls
UsageDetails for every dynamically discovered managed scope, the `microsoft.app/agents` inventory
from Resource Graph, and each agent's read-only `dailyUsageDetails` and `usages` GETs.

```python
import importlib.util
from pathlib import Path

path = Path("plugins/finops/skills/finops-sre-agent/sreagent.py")
spec = importlib.util.spec_from_file_location("sreagent", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

ops = "/subscriptions/000/resourceGroups/sre/providers/Microsoft.App/agents/ops-agent"
lab = "/subscriptions/000/resourceGroups/sre/providers/Microsoft.App/agents/lab-agent"
line_items = [
    {"date": day, "cost": aau * 0.1, "quantity": aau, "resourceId": rid,
     "consumedService": "Microsoft.App", "meterName": "SRE Agent Unit"}
    for day in ("2026-07-30", "2026-07-31")
    for rid, aau in ((ops, 196.0), (lab, 100.0))
]
agents = [
    {"id": ops, "name": "ops-agent", "resourceGroup": "sre", "powerState": "Running",
     "monthlyAgentUnitLimit": 10000},
    {"id": lab, "name": "lab-agent", "resourceGroup": "sre", "powerState": "Stopped"},
]
daily_usage = {
    ops: {"value": [
        {"date": "2026-07-30", "source": "ScheduledTask", "threadId": "t-1", "value": 80.0},
        {"date": "2026-07-31", "source": "ScheduledTask", "threadId": "t-1", "value": 80.0},
        {"date": "2026-07-31", "source": "Thread", "threadId": "t-2", "value": 40.0},
    ]},
    lab: {"value": [
        {"date": "2026-07-31", "source": "Thread", "threadId": "t-3", "value": 4.0},
    ]},
}
monthly_usage = {ops: {"value": [{"name": {"value": "TotalAgentUnits"},
                                  "currentValue": 6200.0, "limit": 10000.0}]}}

report = module.analyze_sre_agent_costs(
    line_items, agents, daily_usage, monthly_usage, as_of="2026-07-31",
)
```

Representative output (abbreviated):

```json
{
  "total_usd": 59.2,
  "always_on_usd": 38.8,
  "active_usd": 20.4,
  "always_on_pct": 65.5,
  "always_on_monthly_usd_per_agent": 292.0,
  "by_agent": [
    {"agentName": "ops-agent", "status": "running", "monthly_usd": 39.2, "always_on_usd": 19.2, "active_usd": 20.0, "split": "measured", "projected_limit_pct": 62.0},
    {"agentName": "lab-agent", "status": "stopped", "monthly_usd": 20.0, "always_on_usd": 19.6, "active_usd": 0.4, "split": "measured", "projected_limit_pct": null}
  ],
  "by_source": [
    {"label": "Scheduled tasks", "aau": 160.0, "usd": 16.0, "pct": 78.4},
    {"label": "Chat / threads", "aau": 44.0, "usd": 4.4, "pct": 21.6}
  ],
  "hints": [
    {"type": "stopped_agent_billed", "target": "lab-agent", "monthly_usd": 292.0},
    {"type": "scheduled_task_heavy", "target": "Scheduled tasks", "monthly_usd": 16.0}
  ]
}
```
