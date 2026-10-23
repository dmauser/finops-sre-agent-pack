---
name: finops-waste-finder
description: Find orphaned, idle, and stale Azure resources that keep billing after their workload is gone, read-only. Covers unassociated public IPs, NAT gateways with no subnets, dead private endpoints, Standard load balancers and application gateways with empty backends, VPN/ExpressRoute gateways with no connections, old snapshots, detached NICs, and Log Analytics workspaces that need a commitment tier or retention review. Ranks findings by monthly cost using the bundled waste.py with the runtime's sandbox Python tool. Complements finops-rightsizing-advisor (no overlap). Use for "what are we paying for that nobody uses", cleanup sweeps, and post-migration/decommission hygiene.
---

## When to use this skill

Use it for **usage optimization by cleanup**: resources left behind after a migration, a
decommission, a failed deployment, or a proof of concept. These rarely show up in Azure Advisor
yet keep billing every hour (public IPs, NAT gateways, gateways, load balancers, application
gateways, snapshots).

It **does not** repeat `finops-rightsizing-advisor` (unattached managed disks, stopped-not-deallocated
VMs, empty App Service plans, idle Container Apps, Advisor cost recommendations), so savings from
the two skills can be added together. It recommends only; it never deletes or changes anything.

## Required access

- **Reader** on the managed subscriptions/resource groups (Resource Graph).
- **Cost Management Reader** to attach per-resource monthly cost (without it, findings are still
  listed but savings are unknown and sort last).
- Read-only `az` (`RunAzCliReadCommands`) and sandbox Python: use `ExecutePythonCode` when
  available, otherwise `RunInTerminal` with `python3`. No POST/write APIs are used.

## Procedure

### Step 0 — Resolve the managed boundary

First load `finops-managed-scope` and follow its `scope.py` procedure to dynamically GET and validate
the current agent `managedResources`. Do this before every FinOps request; do not reuse a prior list.
Use the validated managed scopes, expanded to their descendant subscriptions/resource groups/resources,
as the default and only scheduled boundary. Scheduled runs are fail-closed with no override. For an
interactive resource named outside that boundary, disclose the mismatch and wait for explicit
confirmation in a subsequent user turn before any broader Azure query. Broad RBAC never expands scope.

Run Resource Graph for each effective scope with explicit subscription selection and retain only
resource ids inside the expanded boundary. For an RG-only effective scope, use both the
subscription id and the exact resource-group constraint shown below, then apply the normalized
ARM-id client-side scope filter as defense in depth. Pull UsageDetails independently for every
effective scope, de-duplicate overlapping resources and line items, and report included scopes,
excluded resources, unattributed cost rows, and any scope/API coverage gaps.

### Step 1 — Pull candidate inventory (Resource Graph)

```bash
az graph query \
  --subscriptions <EFFECTIVE_SUBSCRIPTION_ID> \
  -q "Resources | where type in~ ('microsoft.network/publicipaddresses','microsoft.network/natgateways','microsoft.network/privateendpoints','microsoft.network/loadbalancers','microsoft.network/applicationgateways','microsoft.network/virtualnetworkgateways','microsoft.network/connections','microsoft.compute/snapshots','microsoft.network/networkinterfaces','microsoft.operationalinsights/workspaces') | project id, type, location, resourceGroup, sku, properties, tags" \
  --first 1000 \
  -o json
```

For an RG-only managed scope, add `| where resourceGroup =~ '<EFFECTIVE_RESOURCE_GROUP>'` before
`project`, keep `--subscriptions <EFFECTIVE_SUBSCRIPTION_ID>`, and keep only ids whose normalized
ARM prefix is exactly
`/subscriptions/<effective_subscription_id>/resourcegroups/<effective_resource_group>/`
(client-side). Paginate with `--skip-token` until empty, keeping the same scoping on every page.
Keep `microsoft.network/connections` rows in the list: they are how idle VPN/ExpressRoute gateways
are detected. Pass the raw rows (`data` arrays) to the analyzer unchanged.

### Step 2 — Pull per-resource monthly cost (recommended)

Use the canonical UsageDetails pull from `finops-managed-scope` (`metric=AmortizedCost`, trailing
~30 days, `--query "{value: value[].{id: id, date: properties.date, cost: properties.costInUSD, costInBillingCurrency: properties.costInBillingCurrency, billingCost: properties.cost, billingCurrencyCode: properties.billingCurrencyCode, billingCurrency: properties.billingCurrency, chargeType: properties.chargeType, meterCategory: properties.meterCategory, quantity: properties.quantity, unitOfMeasure: properties.unitOfMeasure,
subscriptionId: properties.subscriptionId, resourceGroup: properties.resourceGroup,
instanceName: properties.instanceName, resourceId: properties.resourceId}, nextLink: nextLink}"`), persist the
page envelopes, and run `prepare_usage_details(...)`. Build `costs = costs_by_resource(included_rows)`
(from `usage.py`; it resolves `instanceName` → `resourceId`); do not merge or scope-filter
UsageDetails in model reasoning. If the pull is partial, label savings "partial — cost pull truncated".

For Log Analytics workspaces, use `ingestion = ingestion_gb_per_day(included_rows, days=<window>)`:
it sums `quantity` of rows whose `meterCategory` is `Log Analytics` and whose unit is GB (retention
`GB/Month` excluded), per workspace id, divided by the window length.

### Step 3 — Classify (bundled waste.py, in-sandbox)

```
read_skill_file(skill_name="finops-waste-finder", file_path="waste.py")
```

```python
from waste import find_waste
result = find_waste(
    resources,                               # Step 1 raw ARG rows
    costs=costs,                             # Step 2 {resourceId: monthly cost}
    ingestion_gb_per_day=ingestion,          # Step 2 {workspaceId: GB/day}
    managed_scopes=effective_scopes,
    currency=prepared["currency"],
)
```

| Kind | Rule | Savings counted |
| --- | --- | --- |
| `orphan` | Public IP with no ipConfiguration/NAT gateway; NAT gateway with no subnets; private endpoint whose connections are all Disconnected/Rejected | Full monthly cost |
| `idle` | Standard load balancer with empty backend pools; running application gateway with no backend targets; VPN/ExpressRoute gateway with no connection and no point-to-site pool | Full monthly cost |
| `stale` | Snapshot older than `snapshot_age_days` (90) | Full monthly cost |
| `hygiene` | NIC attached to nothing | None (IP/clutter only) |
| `review` | Log Analytics `PerGB2018` at ≥100 GB/day (commitment tier candidate) or interactive retention > 90 days | Not estimated |

Findings are sorted by estimated monthly savings; unknown cost sorts last and is counted in
`headline.unknown_cost_count`.

### Step 4 — Report

Lead with `headline.potential_monthly_savings` (orphan + idle + stale only) and the count by kind.
Show a table: resource, type, kind, reason, monthly cost, estimated savings, recommended action,
evidence. Put `hygiene` and `review` rows in their own sections without savings claims. Before any
deletion is acted on, the owner must confirm the resource is not a DR standby, a reserved static
IP, or a retention-policy snapshot — say so. Report excluded resources and any warnings.

Recommend only — do not delete, stop, or modify resources. Read-only only. Do not use any
write/POST Azure operations.

### Step 5 — Save the Live Report

After reporting, refresh the shared Live Report `FinOps: Waste Cleanup` so the result is visible in Operations Hub. A scheduled task that already has ListReports/SaveReport steps owns this; follow it and do not save twice.

1. Save only when the run covered the full effective managed scope without a user scope override. Otherwise say the shared report was not refreshed and offer to save under a distinct name such as `FinOps: Waste Cleanup (<scope>)`. Skip when the user asks not to save.
2. Call ListReports; call GetReport and reuse the exact-name report's `reportId`, or create it if absent.
3. Load `finops-report-renderer`, its SKILL.md, exact `models.py`, and exact `render.py`. With `datetime.now(timezone.utc)`, call `build_waste_model(find_waste_result, ...)` with `partial`, warnings, and scope coverage; do not construct the model or HTML by hand. Run `write_report(model, "finops-waste.html")`.
4. SaveReport the static file with `allowedTools: []`, name `FinOps: Waste Cleanup`, and the reused `reportId`. Return the saved report id and version. A report-tool failure never suppresses the analysis result.
## Notes

- Add `finops-rightsizing-advisor` savings to this skill's savings for the total usage-optimization
  opportunity; the rule sets are disjoint by design.
- Tag the owner on each finding (`tags`) when present so cleanup can be routed (accountability).
- Microsoft recommends cleaning up unused resources as a first, low-risk optimization step. Pair it
  with an environment tag and auto-shutdown for non-production. The service-specific Advisor
  checks are listed in `finops-rightsizing-advisor` under "Microsoft workload optimization
  guidance". Source:
  https://learn.microsoft.com/en-us/cloud-computing/finops/framework/optimize/workloads.
  Last reviewed: 2026-10-23.
