---
name: finops-commitment-advisor
description: Rate optimization for Azure, read-only. Inventories existing reservations and savings plans (utilization, expiry, auto-renew), flags underutilized and expiring commitments, normalizes reservation and savings plan purchase recommendations to estimated monthly savings (as alternatives, never summed), measures commitment coverage from amortized cost, and lists Azure Hybrid Benefit candidates, using the bundled commit.py with the runtime's sandbox Python tool. Use for "should we buy reservations or a savings plan", "are our reservations being used", "what expires soon", and quarterly rate-optimization reviews.
---

## When to use this skill

Use it for **rate optimization** — paying less for the usage you keep — as opposed to usage
optimization (using less, which `finops-rightsizing-advisor` and `finops-waste-finder` cover):

- "Should we buy reservations or a savings plan, and how much?"
- "Are our reservations / savings plans fully used?"
- "Which commitments expire soon without auto-renew?"
- "What share of our compute runs on commitment vs. pay-as-you-go?"
- "Where are we paying for Windows / SQL Server licenses we may already own?"

It recommends only. It never purchases, exchanges, renews, or changes a license setting; every
step is a read-only GET and a human approves any action.

**FinOps sequencing:** optimize usage first, then commit. Before recommending a purchase, run (or
reference a recent run of) `finops-rightsizing-advisor` and `finops-waste-finder`; committing to
capacity that is about to be deleted or downsized locks in waste. Say so in the report whenever
open rightsizing or waste findings exist for the same resources.

## Required access

- **Reader** on the managed subscriptions (purchase recommendations, Resource Graph).
- **Cost Management Reader** for `benefitRecommendations` and amortized UsageDetails.
- **Reservations Reader** (tenant-level role at `/providers/Microsoft.Capacity`) to list
  reservations, and **Savings plan Reader** (tenant-level at `/providers/Microsoft.BillingBenefits`,
  or Reader on the savings plan order) to list savings plans. Subscription RBAC does **not** grant
  these. Without them the inventory calls return empty/403; report that as a coverage gap instead
  of "no commitments" (see Step 1).
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

Run every recommendation and Resource Graph call for each effective scope with explicit
subscription selection. Reservations and savings plans are **billing-level** objects: list them
once, then pass the effective scopes to `analyze_commitments(managed_scopes=...)`, which keeps
Shared / management-group commitments (disclosed as shared context, not attributed) and drops
Single / resource-group-scoped commitments outside the boundary. Pull UsageDetails independently
for every effective scope, de-duplicate overlapping line items, and report included scopes,
excluded commitments/rows, unattributed cost rows, and any scope/API coverage gaps.

### Step 1 — Inventory existing commitments (read-only GETs)

```bash
az rest --method get \
  --url "https://management.azure.com/providers/Microsoft.Capacity/reservations?api-version=2022-11-01" \
  -o json

az rest --method get \
  --url "https://management.azure.com/providers/Microsoft.BillingBenefits/savingsPlans?api-version=2022-11-01" \
  -o json
```

Follow every `nextLink` and concatenate the `value` arrays. Each item carries
`properties.utilization.aggregates` (1/7/30-day percentages), `expiryDateTime`, `renew`, `term`,
and the applied scope. If a call returns 403 (or fails after one retry), add its kind
(`"reservation"` / `"savings_plan"`) to `inventory_unavailable` and continue — never report that as
"no commitments". `analyze_commitments` turns it into an explicit coverage-gap warning, while
coverage % and unused-commitment cost (Step 3, amortized cost — needs only Cost Management Reader)
and purchase recommendations (Step 2) are still produced. To close the gap, a tenant admin with
elevated access grants the agent identity (once, tenant-wide):

```bash
az role assignment create --assignee-object-id <AGENT_MI_OBJECT_ID> --assignee-principal-type ServicePrincipal \
  --role "Reservations Reader" --scope /providers/Microsoft.Capacity
az role assignment create --assignee-object-id <AGENT_MI_OBJECT_ID> --assignee-principal-type ServicePrincipal \
  --role "Savings plan Reader" --scope /providers/Microsoft.BillingBenefits
```

(`install-api.sh` does this when run with `GRANT_COMMITMENT_READERS=1 MI_OBJECT_ID=<id>`.)

### Step 2 — Pull purchase recommendations

Reservation recommendations, per effective subscription and per resource type that has spend
(start with `VirtualMachines`; add `SQLDatabases`, `CosmosDB`, `AppService`, `ManagedDisk`,
`RedisCache`, `PostgreSQL`, `MySQL`, `AzureDataExplorer`, `BlockBlob` when their meter categories
appear in the cost data):

```bash
az rest --method get \
  --url "https://management.azure.com/subscriptions/<EFFECTIVE_SUBSCRIPTION_ID>/providers/Microsoft.Consumption/reservationRecommendations?api-version=2023-05-01&%24filter=properties/scope eq 'Single' AND properties/lookBackPeriod eq 'Last30Days' AND properties/resourceType eq 'VirtualMachines'" \
  -o json
```

Savings plan (benefit) recommendations, per effective subscription (or per effective resource
group for an RG-only scope, by using that resource group id as the scope prefix):

```bash
az rest --method get \
  --url "https://management.azure.com/subscriptions/<EFFECTIVE_SUBSCRIPTION_ID>/providers/Microsoft.CostManagement/benefitRecommendations?api-version=2023-11-01&%24filter=properties/lookBackPeriod eq 'Last30Days' AND properties/term eq 'P3Y'&%24expand=properties/allRecommendationDetails" \
  -o json
```

Repeat the benefit call with `properties/term eq 'P1Y'` so both terms are compared. Both are GET
APIs; do not use the Cost Management POST endpoints. Keep only rows whose `subscriptionId` (or
scope) is inside the effective scope.

### Step 3 — Pull amortized cost for coverage (recommended)

Use the canonical UsageDetails pull from `finops-managed-scope` with `metric=AmortizedCost`
over the trailing ~30 days and add the fields this skill needs:
`--query "{value: value[].{id: id, date: properties.date, cost: properties.costInUSD, costInBillingCurrency: properties.costInBillingCurrency, billingCost: properties.cost, billingCurrencyCode: properties.billingCurrencyCode, billingCurrency: properties.billingCurrency, chargeType: properties.chargeType, pricingModel: properties.pricingModel, meterCategory: properties.meterCategory,
subscriptionId: properties.subscriptionId, resourceGroup: properties.resourceGroup,
instanceName: properties.instanceName, resourceId: properties.resourceId}, nextLink: nextLink}"`.
Persist the page envelopes and run `prepare_usage_details(...)`; pass its `included_rows` as
`usage_rows` and its `currency` as `currency`. Do not merge or scope-filter UsageDetails in
model reasoning. Amortized cost is required: in actual cost, commitment purchases appear as
one-time charges and usage covered by them shows as zero.

### Step 4 — Pull Hybrid Benefit candidates (Resource Graph)

```bash
az graph query \
  --subscriptions <EFFECTIVE_SUBSCRIPTION_ID> \
  -q "Resources | where type =~ 'microsoft.compute/virtualmachines' or type =~ 'microsoft.sqlvirtualmachine/sqlvirtualmachines' | project id, type, properties" \
  --first 1000 \
  -o json
```

For an RG-only managed scope, add `| where resourceGroup =~ '<EFFECTIVE_RESOURCE_GROUP>'` before
`project`, keep `--subscriptions <EFFECTIVE_SUBSCRIPTION_ID>`, and apply the normalized ARM-prefix
filter client-side. Paginate with `--skip-token`. Split rows into `vms` and `sql_vms` by type.

### Step 5 — Analyze (bundled commit.py, in-sandbox)

```
read_skill_file(skill_name="finops-commitment-advisor", file_path="commit.py")
```

```python
from commit import analyze_commitments
result = analyze_commitments(
    reservations=reservations,                 # Step 1
    savings_plans=savings_plans,               # Step 1
    reservation_recommendations=ri_recs,       # Step 2
    benefit_recommendations=sp_recs,           # Step 2
    usage_rows=prepared["included_rows"],      # Step 3 (optional)
    currency=prepared["currency"],
    vms=vms, sql_vms=sql_vms, costs=costs,     # Step 4 (+ per-resource monthly cost)
    managed_scopes=effective_scopes,
    inventory_unavailable=inventory_unavailable,  # Step 1 kinds that returned 403/error
)
```

`analyze_commitments` handles the normalization so the model does not do arithmetic:

- **Reservation `netSavings` covers the whole term** → monthly = netSavings / term months.
- **Savings plan `savingsAmount` covers the look-back window** → monthly = savingsAmount /
  `totalHours` × 730.
- Only the **best term per SKU/location/scope** is counted; the other terms are listed as
  `alternatives`.
- Reservation and savings plan recommendations target the same usage, so
  `headline.potential_monthly_savings` is the **larger family, never the sum**.
- `underutilized` uses 30-day utilization (falls back to 7-day) below `low_utilization_pct` (80).
- `expiring` lists commitments within `expiry_window_days` (60) with auto-renew off.
- `coverage` reports covered vs. on-demand amortized cost by meter category and the
  `UnusedReservation` / `UnusedSavingsPlan` charges (money already wasted).

### Step 6 — Report

Lead with the headline: commitment coverage %, unused commitment cost, the larger purchase family
and its estimated monthly savings, and counts of underutilized / expiring commitments. Then:

1. **Fix waste in what you own** — underutilized commitments (exchange, widen scope to Shared /
   management group) and the unused commitment charges.
2. **Avoid the coverage cliff** — expiring commitments with auto-renew off and their expiry date.
3. **Buy** — purchase recommendations, best term first, with `alternatives`, look-back period, and
   a note that recommendations assume the look-back usage continues.
4. **Licenses** — Hybrid Benefit candidates; entitlement must be verified.

State that reservation and savings plan options are alternatives, that shared commitments are
reported for context only, and any excluded commitments or inventory permission gaps. Recommend
only — do not purchase, exchange, renew, or change licensing.

Read-only only. Do not use any write/POST Azure operations.

## Notes

- Prefer savings plans for variable or migrating compute (flexible across SKU/region); prefer
  reservations for stable, specific SKUs where the extra discount is worth the lower flexibility.
- Recommendations use a 30-day look-back by default; seasonal workloads may need `Last60Days`.
- Thresholds (`low_utilization_pct`, `expiry_window_days`, `min_monthly_savings`) are keyword
  arguments to `analyze_commitments`.
