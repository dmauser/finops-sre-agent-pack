---
name: finops-commitment-advisor
description: Rate optimization for Azure, read-only. Inventories reservations and savings plans, flags underutilized and expiring commitments, normalizes purchase recommendations to monthly savings as alternatives rather than additive savings, and measures cost-weighted coverage of a conservative evidenced eligible subset. Keeps unknown eligibility, excluded charges and currencies separate; lists Azure Hybrid Benefit candidates. Uses bundled commit.py with the runtime's sandbox Python tool. Use for "should we buy reservations or a savings plan", "are our reservations being used", "what expires soon", and quarterly rate-optimization reviews.
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
  --url "https://management.azure.com/subscriptions/<EFFECTIVE_SUBSCRIPTION_ID>/providers/Microsoft.CostManagement/benefitRecommendations?api-version=2023-11-01&%24filter=properties/lookBackPeriod eq 'Last30Days' AND properties/term eq 'P3Y' AND properties/scope eq 'Single'&%24expand=properties/allRecommendationDetails" \
  -o json
```

Repeat the benefit call with `properties/term eq 'P1Y'` so both terms are compared. Both are GET
APIs; do not use the Cost Management POST endpoints. Keep only rows whose `subscriptionId` (or
scope) is inside the effective scope. Always keep the `properties/scope eq 'Single'` filter: without
it the API also returns `Shared` recommendations sized on the whole billing account or enrollment,
which can exceed the subscription's spend by orders of magnitude. When `managed_scopes` is passed,
`commit.py` drops any `Shared` row that slips through and adds a warning.

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
from usage import load_finops_input, save_finops_input

# Reuse the unique run_id from managed-scope resolution.
inputs = dict(
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
save_finops_input("commitment-inputs", inputs, run_id=run_id)
result = analyze_commitments(**load_finops_input("commitment-inputs", run_id=run_id))
save_finops_input("commitment-analysis", result, run_id=run_id)
```

Inputs/results live only in `/mnt/data/finops-runs/<run_id>/`. Carry the same unique `run_id`
through collection, analysis and rendering. Do not recover missing inputs from another run or
reuse a shared scratch directory. Keep source failures in `inventory_unavailable` rather than
replacing a failed query with an apparently successful empty inventory.

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
- `coverage` reports **cost-weighted coverage of an evidenced eligible subset**, not the share
  of all services covered by commitments. Its denominator is `eligible_cost = covered_cost +
  on_demand_cost`; `coverage_pct` is null when that denominator is unavailable or zero.
- The helper's conservative policy counts rows with applied `Reservation`/`SavingsPlan` pricing
  as covered. For uncovered usage it includes `OnDemand` **Virtual Machines** compute meters,
  excluding identified license/software meters, or an explicit boolean `commitmentEligible`
  supplied from validated meter-level evidence. This is a local classification policy, **not**
  a complete service/SKU eligibility catalog or a promise that a purchase is available.
- Unknown pricing/eligibility remains in `unknown_eligibility_cost`. Service names such as
  Storage alone do not prove eligibility or ineligibility. Spot, explicit ineligibility and the
  helper's narrow excluded-meter/service classifications contribute to `ineligible_cost`.
  Negative adjustments remain in `excluded_adjustment_cost`; unused commitment charges remain
  separately in `unused_commitment_cost`. These buckets do not dilute the coverage denominator.
- Preserve `eligibility_basis`, `currency`, `by_meter_category` and `row_count` alongside the
  percentage. If currencies differ, `by_currency` holds separate results and aggregate monetary
  metrics/coverage are null with `reason`; unknown denomination is not silently treated as USD.
  Supply a verified `currency` for rows without a denomination. No currency conversion occurs.
- Hybrid Benefit cost mappings combine numeric values for case-insensitive resource-ID
  variants. Strings, booleans and nonfinite values are not monetary contributions; do not
  perform a lossy lowercasing dictionary rewrite before passing `costs`.

For example, with 50 of covered VM usage, 50 of eligible on-demand VM usage and 1,000 of
excluded service usage in the same currency, the eligible-subset coverage is **50%**, not
4.5%. Additional unknown-eligibility spend is disclosed separately, never counted as zero
coverage or silently assumed eligible.

### Step 6 — Report

Lead with the headline: **eligible-subset commitment coverage %**, unused commitment cost, the larger purchase family
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

Always show the eligible denominator, excluded and unknown-eligibility amounts, currency and
`eligibility_basis` next to coverage. If a substantial share is unknown, explain that the reported
percentage describes only the evidenced subset. Do not label null coverage as 0%, turn a mixed-
currency total into a single amount, or present an unweighted all-service percentage as eligible
compute coverage.

Read-only only. Do not use any write/POST Azure operations.

### Step 7 — Save the Live Report

After reporting, refresh the shared Live Report `FinOps: Commitments` so the result is visible in Operations Hub. A scheduled task that already has ListReports/SaveReport steps owns this; follow it and do not save twice.

1. Save only when the run covered the full effective managed scope without a user scope override. Otherwise say the shared report was not refreshed and offer to save under a distinct name such as `FinOps: Commitments (<scope>)`. Skip when the user asks not to save.
2. Call ListReports; call GetReport and reuse the exact-name report's `reportId`, or create it if absent.
3. Load `finops-report-renderer`, its SKILL.md, exact `models.py`, and exact `render.py`. Reload the same-run result with `load_finops_input("commitment-analysis", run_id=run_id)`. With `datetime.now(timezone.utc)`, call `build_commitment_model(analyze_commitments_result, ...)` with `partial`, warnings, and scope coverage; do not construct the model or HTML by hand. Pass any 403 inventory kinds to `analyze_commitments(inventory_unavailable=...)` first so the report says `unknown`, not "no commitments". Run `write_report(model, f"/mnt/data/finops-runs/{run_id}/finops-commitments.html")`.
4. SaveReport the static file with `allowedTools: []`, name `FinOps: Commitments`, and the reused `reportId`. Return the saved report id and version. A report-tool failure never suppresses the analysis result.
## Notes

- Prefer savings plans for variable or migrating compute (flexible across SKU/region); prefer
  reservations for stable, specific SKUs where the extra discount is worth the lower flexibility.
- Recommendations use a 30-day look-back by default; seasonal workloads may need `Last60Days`.
- Thresholds (`low_utilization_pct`, `expiry_window_days`, `min_monthly_savings`) are keyword
  arguments to `analyze_commitments`.

## Microsoft rate optimization guidance

Use these when explaining recommendations. They are advice only; the agent never purchases,
exchanges, refunds or renews.

- **Start small, but start.** Begin with high-confidence commitments that cover stable usage, then
  expand. Don't delay commitments on usage that is already stable.
- **Reservations** save up to 72% compared with pay-as-you-go. They fit stable usage of a specific
  SKU in a specific region.
- **Savings plans** save up to 65% and fit usage that shifts across SKUs or regions. They are
  available only for EA, MCA and MPA billing accounts. They cannot be cancelled or refunded.
- **Savings plan for databases** covers SQL Database and SQL Managed Instance, PostgreSQL, MySQL,
  Cosmos DB, and SQL Server on Azure VMs or Arc. Mention it when database spend dominates.
- **Fixing under-utilization:**
  - widen the reservation scope (to shared or management group);
  - enable instance size flexibility;
  - exchange for a better-fitting reservation.

  Reservation refunds are limited to USD 50,000 in a rolling 12-month window.
- **Operate commitments:**
  - recommend utilization alerts;
  - recommend enabling auto-renew for commitments that are still needed;
  - recommend central purchasing and shared scope so benefits apply across subscriptions.
- **Azure Hybrid Benefit** applies to Windows Server, RHEL/SLES and SQL Server licenses. Dev/Test
  subscriptions already use dev/test pricing and are excluded. Entitlement must be verified.
- The 70% coverage and 95% utilization targets in `finops-kpi-scorecard` come from FinOps Foundation
  practice, not from Microsoft.

Sources: https://learn.microsoft.com/en-us/cloud-computing/finops/framework/optimize/rates ·
https://learn.microsoft.com/en-us/azure/cost-management-billing/savings-plan/savings-plan-compute-overview ·
https://learn.microsoft.com/en-us/azure/cost-management-billing/reservations/save-compute-costs-reservations
Last reviewed: 2026-10-23
