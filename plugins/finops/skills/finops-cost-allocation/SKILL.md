---
name: finops-cost-allocation
description: Showback / cost allocation for Azure, read-only. Joins per-resource monthly cost (modern Consumption UsageDetails) with resource tags (Resource Graph) to attribute spend to an owner dimension (team / env / service / costCenter / app / owner), then surfaces the spend that has no owner — an explicit unallocated bucket, a ranked untagged-resource list, and tag-hygiene flags — using the bundled allocate.py with the runtime's sandbox Python tool. Use for "who owns this bill", showback/chargeback prep, and finding untagged spend.
---

## When to use this skill

Use it when the user wants to **attribute Azure spend to an owner** (team, environment, service,
cost center, app, or owner) — a showback breakdown, chargeback preparation, or a hunt for **untagged
spend that has no owner**. It answers "who/what is this bill for?" and "how much spend is
unattributable?" It is read-only and recommends tagging actions only; it never changes tags.

For cost *spikes* and their cause use `finops-cost-anomaly-detection`; for rightsizing / idle waste
use `finops-rightsizing-advisor`.

## Required access

- **Cost Management Reader** on the subscription (UsageDetails returns no cost values without it; EA rows use billing-currency `cost`) and **Reader** for
  Resource Graph tags.
- Read-only `az` (`RunAzCliReadCommands`) and sandbox Python: use `ExecutePythonCode` when
  available, otherwise `RunInTerminal` with `python3`. No POST/write APIs are used.

## Scope

Attributes any resource that lands a cost line item in Consumption UsageDetails — the allocation is
**service-agnostic** (it works off cost + tags, not a hard-coded resource list). It is also
**tag-generic**: it groups by whatever tag keys exist in your data (a **tag inventory**) and treats
`team / env / service / costCenter / app / owner` as a *recommended* set to report coverage against —
not a hard-coded filter. Shared or untaggable cost (session pools, networking, shared clusters) is
**never force-allocated**: cost with no value for the requested dimension is kept in an explicit
**unallocated** bucket. A resource with **no tags at all** is listed as **untagged** for follow-up
(having *some* tag but not the requested dimension is unallocated, not untagged). Cross-charging
exports and management-group rollups are out of scope here until needed.

## Procedure

### Step 0 — Resolve the managed boundary

First load `finops-managed-scope` and follow its `scope.py` procedure to dynamically GET and validate
the current agent `managedResources`; never reuse cached scope. Managed scopes and expanded descendants
are the default boundary. Scheduled runs are strict/fail-closed with no override. An interactive named
outside-scope target requires disclosure and explicit confirmation in a subsequent turn before any
broader query. Broad RBAC never silently expands scope.

Pull UsageDetails independently for each effective scope where supported, with independent pagination
and completeness tracking. De-duplicate overlapping line items and filter attributable resource ids
against the boundary. Query Resource Graph once per unique effective subscription, always using
`--subscriptions`; if only specific RGs are managed in that subscription, query each unique RG with
the exact case-insensitive predicate and client-side ARM-prefix filter below. De-duplicate overlapping
results and retain only in-bound resources. Preserve cost rows with no usable resource/scope identity as `unattributed` rather than
assigning them. Report included scopes, excluded rows/resources, unattributed cost, unsupported scopes,
and partial/failed coverage.

### Step 1 — Pull per-resource monthly cost

Follow the canonical Consumption UsageDetails transport contract in `finops-managed-scope` over the
trailing ~30 days. This skill's projection must explicitly retain `tags`:

```
--query "{value: value[].{id: id, date: properties.date, cost: properties.costInUSD, costInBillingCurrency: properties.costInBillingCurrency, billingCost: properties.cost, billingCurrencyCode: properties.billingCurrencyCode, billingCurrency: properties.billingCurrency, chargeType: properties.chargeType, subscriptionId: properties.subscriptionId, resourceGroup: properties.resourceGroup, instanceName: properties.instanceName, resourceId: properties.resourceId, tags: tags}, nextLink: nextLink}"
```

After managed-scope filtering, aggregate `cost` by resource id into
`{resourceId: monthly_usd}` with case-insensitive ids. Resource Graph in Step 2 remains the source
for the current resource tag map used by `allocate_costs`.

Perform the page merge and managed-scope filtering with `finops-managed-scope`'s
`prepare_usage_details(...)`, then pass only `included_rows` into the allocation input transform.
Do not concatenate, de-duplicate, or scope-filter pages in model reasoning.

### Step 2 — Pull resource tags (Resource Graph)

```bash
az graph query \
  --subscriptions <EFFECTIVE_SUBSCRIPTION_ID> \
  -q "Resources | project id, tags" \
  --first 1000 \
  -o json
```

For an RG-only managed scope, use:

```bash
az graph query \
  --subscriptions <EFFECTIVE_SUBSCRIPTION_ID> \
  -q "Resources | where resourceGroup =~ '<EFFECTIVE_RESOURCE_GROUP>' | project id, tags" \
  --first 1000 \
  -o json
```

Then keep only ids whose normalized ARM prefix is exactly
`/subscriptions/<effective_subscription_id>/resourcegroups/<effective_resource_group>/`.
Paginate with `--skip-token` until empty (Resource Graph caps at 1000 rows/page), retaining the same
`--subscriptions`, RG predicate, and client-side filter on every page. Flatten to
`{resourceId: {tagKey: tagVal}}`. Keep the raw tag values as-is — the ranker normalizes case and
flags variant spellings itself.

### Step 3 — Allocate (bundled allocate.py, in-sandbox)

Read the module and run it — do **not** re-implement the logic in the prompt:

```
read_skill_file(skill_name="finops-cost-allocation", file_path="allocate.py")
```

```python
from allocate import allocate_costs
result = allocate_costs(
    costs=costs,               # from Step 1  {resourceId: monthly_usd}
    tags=tags,                 # from Step 2  {resourceId: {tagKey: tagVal}}
    dimension="team",          # or env / service / costCenter / app / owner
)
```

`allocate_costs` handles all grouping and governance:

- **groups**: spend per owner value for the requested `dimension`, ranked by dollars, with `pct` of
  total and `resource_count`. Values are grouped case-insensitively; the most costly raw spelling is
  shown.
- **unallocated**: cost of resources with **no value for the requested dimension** — kept as its own
  bucket (never spread onto tagged owners) with `monthly_usd`, `pct`, and the top resources.
- **untagged_resources** / **untagged_usd**: resources with **no tags at all** — the tagging backlog,
  ranked by cost.
- **tag_inventory**: every tag key present in the data with the `resource_count` and `cost_usd` it
  covers, ranked by cost — the "group by the ones we have" view (includes non-recommended keys).
- **recommended_coverage** / **missing_recommended**: for each recommended ownership key
  (`team / env / service / costCenter / app / owner`, overridable via `recommended_keys`), whether it
  is `present` and the cost/resource share it covers; `missing_recommended` lists the ones with no
  usage — an adopt-these-tags recommendation.
- **tag_hygiene**: owner values that collapse to the same group under normalization (e.g. `Prod` vs
  `production`) with the cost each variant carries — these otherwise split one owner's spend.

Run it once per dimension the user cares about (e.g. `team` and `env`).

#### Optional: tag inheritance (`inherited_tags`)

Mirrors Azure Cost Management **tag inheritance**: when a resource lacks the dimension tag, use its
resource group's tag, then its subscription's tag; a tag on the resource itself always wins. Pull the
scope tags read-only (`az group list --query "[].{id:id, tags:tags}"` and
`az account show --query "{id:id, tags:tags}"` — or Resource Graph `ResourceContainers`) and pass
`{scopeId: {tagKey: tagVal}}` (ids matched case-insensitively):

```python
result = allocate_costs(costs, tags, dimension="team", inherited_tags={
    "/subscriptions/<SUB>/resourceGroups/<RG>": {"team": "payments"},
    "/subscriptions/<SUB>": {"team": "platform"},
})
```

Output gains `inherited_usd`, per-group `inherited_usd`, and
`inheritance: {enabled, allocated_via_inheritance_usd, resource_count}`. Inheritance only fills the
dimension grouping: `tag_inventory`, coverage, and `untagged_resources` still reflect each resource's
**own** tags (so the tagging backlog stays honest); untagged entries filled by inheritance carry
`inherited_owner` / `inherited_from` (`resourceGroup` | `subscription`). With `inherited_tags=None`
(default) results are unchanged. Report inherited spend as "allocated via inheritance" so owners know
it was not tagged at the resource.

#### Optional: shared-cost split (`shared`)

For platform cost everyone uses (hub networking, shared AKS, Log Analytics), identify shared
resources by id and/or tag and redistribute their cost across the allocated owner groups:

```python
result = allocate_costs(costs, tags, dimension="team", shared={
    "resource_ids": ["/subscriptions/<SUB>/resourceGroups/hub/providers/Microsoft.Network/azureFirewalls/fw"],
    "tag": {"key": "shared", "value": "true"},       # optional; omit "value" to match any value
    "method": "proportional",                        # | "even" | "fixed"
    "weights": {"payments": 0.6, "search": 0.4},     # only for "fixed"
})
```

- Shared resources are removed from direct allocation (they appear in neither `groups` nor
  `unallocated`). `proportional` (default) splits by each group's direct cost, `even` splits equally,
  `fixed` uses the given weights (normalized over owners that exist; others get nothing).
- Each group gains `direct_usd`, `shared_usd`, `fully_loaded_usd`. **`monthly_usd` stays the
  direct amount** for backward compatibility — use `fully_loaded_usd` for chargeback.
- `shared_costs: {total_usd, method, resources, distribution: [{owner, shared_usd}], undistributed_usd}`.
  The unallocated bucket is never a recipient. If there are no recipient groups (or no `fixed`
  weight matches), the cost stays in `undistributed_usd` (also top-level).
- `allocated_usd` is the fully-loaded allocated amount, so
  `sum(fully_loaded_usd) + unallocated_usd + undistributed_usd == total_usd` (to rounding).

#### Optional: month-over-month variance (`month_over_month`)

Pull two monthly windows (same Step 1 procedure, each aggregated to `{resourceId: usd}`) and run:

```python
from allocate import month_over_month
mom = month_over_month(current_costs, previous_costs, dimension_tags=tags, dimension="team",
                       min_delta_usd=10.0, top_n=25)
```

Returns `current_usd`, `previous_usd`, `delta_usd`, `delta_pct`, `by_owner` variance (same
case-insensitive tag lookup; missing owner -> `(unallocated)`), and `movers` split into `new`
(only in current), `removed` (only in previous), `increased`, `decreased` — each with
`previous_usd`, `current_usd`, `delta_usd`, `delta_pct`, sorted by absolute delta and filtered by
`min_delta_usd`. `bridge` buckets (`new_usd`, `removed_usd`, `increased_usd`, `decreased_usd`,
`below_threshold_usd`) cover every resource and sum to `delta_usd` — use it for variance commentary.
Splitting a mover into **price vs usage** effects is out of scope (it needs quantity and unit price
per meter); state that when asked.

### Step 4 — Report

Produce, for the chosen dimension: a **showback table** (owner, monthly $, % of total, resource
count) with the **total** and the **unallocated** row called out explicitly; a **tag inventory**
(every key in use, with cost + resource coverage); a **recommended-coverage** section (which of
`team / env / service / costCenter / app / owner` are present vs missing, with an adopt-the-missing
recommendation); an **untagged-spend** section (total $ + top resources to tag — resources with no
tags at all); and **tag-hygiene** warnings. When inheritance is used, call out the
"allocated via inheritance" amount; when a shared split is used, show direct, shared, and
fully-loaded columns plus any undistributed amount. Recommend tagging actions only — never modify tags. If
cost was partial (Step 1 truncation), say so at the top. If everything is allocated and there is no
untagged spend, say so in one line.

## Data source: UsageDetails today, FOCUS exports at scale

This pack uses the **Consumption UsageDetails** API (read-only GET) today. For large estates —
Enterprise Agreement / Microsoft Customer Agreement billing scopes, many subscriptions, or row
counts where paging UsageDetails is slow or hits 413s — the recommended source is a **Cost Management
export** in the **FOCUS** format (FOCUS 1.0 cost and usage dataset) delivered on a schedule to a
storage account. Aggregate the export rows to the same `{resourceId: usd}` / `{resourceId: tags}`
inputs and the module works unchanged.

| FOCUS 1.0 column | Field this skill expects | Notes |
|------------------|--------------------------|-------|
| `EffectiveCost` | amortized cost (UsageDetails `metric=AmortizedCost` `cost`) | Preferred for showback; spreads reservation / savings-plan purchases |
| `BilledCost` | actual cost (`metric=ActualCost`) | Matches the invoice |
| `ResourceId` | `resourceId` (key of `costs` / `tags`) | Matched case-insensitively |
| `Tags` | `tags` | JSON object; parse to a dict |
| `ServiceName` | `meterCategory`-style service grouping | Approximate equivalent, not identical values |
| `ChargePeriodStart` | `date` | Day granularity for daily exports |
| `ChargeCategory` = `Credit` / `Adjustment` | credits / refunds (negative rows) | Keep out of allocation totals or report separately, as `summarize_credits` does for anomalies |
| `BillingCurrency` | `billingCurrencyCode` | FOCUS costs are in billing currency |

Guidance (not something the current read-only tool set does automatically):

- Creating the export is a one-time admin action in Cost Management (Exports → "Cost and usage (FOCUS)" dataset)
  by someone with export rights; the agent never creates it.
- Reading the export requires **Storage Blob Data Reader** on the container for the agent's managed
  identity, and the runtime must be able to reach the storage endpoint (allowed egress / private
  endpoint, or a read-only SAS supplied by the operator). The current `RunAzCliReadCommands` tool set
  may need that access configured first.
- Keep managed-scope filtering: filter export rows by `ResourceId` / subscription against the
  effective scope exactly as for UsageDetails.
