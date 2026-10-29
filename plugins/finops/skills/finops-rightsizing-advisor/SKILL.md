---
name: finops-rightsizing-advisor
description: Recommend rightsizing and idle-resource cleanup for Azure, read-only. Combines Advisor recommendations, inventory, CPU/memory, workload activity, sample coverage, and currency-labelled costs. Separates supported recommendations, unvalidated candidates, and contradicted estimates using the bundled rightsize.py with the runtime's sandbox Python tool. Use for "what can we downsize or turn off", periodic savings reviews, and adding a cost-efficiency lens to capacity questions.
---

## When to use this skill

Use it when the user wants to **find underutilized or idle Azure resources and get concrete
resize / deallocate / delete recommendations** — an on-demand "what can we rightsize or shut down?",
a weekly savings review, or a cost-efficiency pass during a capacity discussion. This skill
**validates Advisor's suggestions against real utilization and real cost** before surfacing them and
ranks by estimated savings in the input currency. It recommends only; it never changes anything
(all steps are read-only, and a human approves any action).

For cost *spikes* and their root cause, use `finops-cost-anomaly-detection` instead.

## Required access

- **Reader** on the subscription/resource groups for Advisor, Resource Graph, and Azure Monitor
  metrics. **Cost Management Reader** to attach per-resource monthly cost (without it, savings are
  still estimated from Advisor where available but resources with no Advisor number show unknown cost).
- Read-only `az` (`RunAzCliReadCommands`) and sandbox Python: use `ExecutePythonCode` when
  available, otherwise `RunInTerminal` with `python3`. No POST/write APIs are used.

## Scope

The bundled heuristics cover VM utilization, unattached disks, stopped VMs, empty App Service
plans, and **Azure Container Apps idle candidates**: environments with no inventoried children
or no observed app HTTP requests, always-on container apps (`minReplicas>=1`) with no requests, and
**warm dynamic session pools** (`readySessionInstances>=1`) with no session traffic — a class that is
invisible to `az resource list` yet often tops the bill. A **cost-led coverage sweep** additionally
flags any high-spend resource whose type has no idle rule as `review`, so nothing expensive is
silently dropped. A low CPU reading or zero HTTP requests alone is **not proof of idle capacity**;
see the evidence gate below. **Pod- and namespace-level AKS rightsizing needs a Log Analytics Reader grant +
`api.loganalytics.io` scope on the agent identity** (Container Insights KQL is otherwise blocked) —
out of scope here until that grant lands.

## Procedure

### Step 0 — Resolve the managed boundary

First load `finops-managed-scope` and follow its `scope.py` procedure to dynamically GET and validate
the current agent `managedResources`. Do this before every FinOps request; do not reuse a prior list.
Use the validated managed scopes, expanded to their descendant subscriptions/resource groups/resources,
as the default and only scheduled boundary. Scheduled runs are fail-closed with no override. For an
interactive resource named outside that boundary, disclose the mismatch and wait for explicit
confirmation in a subsequent user turn before any broader Azure query. Broad RBAC never expands scope.

For each effective scope, run Advisor and Resource Graph with explicit subscription selection, and
retain only returned resource ids inside the expanded boundary. A full-subscription effective scope
is queried once. For an RG-only effective scope, use both the subscription id and exact resource-group
constraint shown below, then apply the normalized ARM-id client-side scope filter as defense in depth.
De-duplicate resources when effective scopes overlap. Pull metrics only for those retained resources.
Pull UsageDetails independently for every effective
scope where the API supports it, paginate each scope independently, de-duplicate overlapping line
items, and filter resource ids against the boundary as defense in depth. Report included scopes,
excluded rows/resources, unattributed cost rows, and any scope/API coverage gaps.

### Step 1 — Pull Azure Advisor cost recommendations

```bash
az advisor recommendation list \
  --subscription <EFFECTIVE_SUBSCRIPTION_ID> \
  --category Cost \
  -o json
```

For an RG-only managed scope, constrain the same command to that exact resource group:

```bash
az advisor recommendation list \
  --subscription <EFFECTIVE_SUBSCRIPTION_ID> \
  --resource-group <EFFECTIVE_RESOURCE_GROUP> \
  --category Cost \
  -o json
```

Run once per unique effective subscription, or once per unique effective RG when the subscription is
not fully managed. After either command, retain only recommendations whose normalized
`resourceMetadata.resourceId`/`impactedValue` is inside the corresponding effective scope.
`recommend_rightsizing` skips reservation and savings plan purchase recommendations (rate
optimization, handled by `finops-commitment-advisor`) so they never count as resize or waste.

Flatten each recommendation to the shape `rightsize.py` expects:
`{resourceId, problem, recommendation, targetSku, savingsUsd}` — `resourceId` from
`resourceMetadata.resourceId` (or the `impactedValue`), `problem`/`recommendation` from
`shortDescription`. Populate `savingsUsd` from `extendedProperties.savingsAmount`/`annualSavingsAmount`
only when its source currency is confirmed USD (convert a confirmed annual USD figure to monthly
by dividing by 12). Do not label an unverified-currency amount as USD. The core excludes an Advisor
USD estimate from a non-USD or unknown-currency cost calculation rather than converting it.

### Step 2 — Pull inventory (Resource Graph) for idle patterns Advisor misses

```bash
az graph query \
  --subscriptions <EFFECTIVE_SUBSCRIPTION_ID> \
  -q "Resources | where type in~ ('microsoft.compute/virtualmachines','microsoft.compute/disks','microsoft.web/serverfarms','microsoft.app/managedenvironments','microsoft.app/containerapps','microsoft.app/jobs','microsoft.app/sessionpools') | project id, type, sku=tostring(sku.name), properties, tags" \
  --first 1000 \
  -o json
```

For an RG-only managed scope, add the exact case-insensitive Kusto predicate before `project`:
`| where resourceGroup =~ '<EFFECTIVE_RESOURCE_GROUP>'`, while retaining
`--subscriptions <EFFECTIVE_SUBSCRIPTION_ID>`. After retrieval, also keep only ids whose normalized
ARM prefix is exactly
`/subscriptions/<effective_subscription_id>/resourcegroups/<effective_resource_group>/`.

On large subscriptions Resource Graph caps at 1000 rows per page — **paginate with `--skip-token`**
(from the response) until it's empty so you don't miss resources. Every page request must retain the
same `--subscriptions` value and, for RG-only scope, the same `resourceGroup =~` predicate and
client-side ARM-prefix filter.

Also pull every resource id that still exists in scope, with the same `--subscriptions`, RG predicate,
and pagination rules, and pass it as `existing_resource_ids` in Step 5:

```bash
az graph query --subscriptions <EFFECTIVE_SUBSCRIPTION_ID> -q "Resources | project id" --first 1000 -o json
```

UsageDetails still bills resources deleted earlier in the window. Without this list the cost-led
sweep lists them as "review" even though nothing is left to act on.

Flatten each row to `{resourceId, type, sku, powerState, diskState, numberOfSites, environmentId, minReplicas, readySessionInstances, childrenInventoryComplete, tags}`:

- **VM** `powerState` from `properties.extended.instanceView.powerState.code` (e.g.
  `PowerState/stopped` vs `PowerState/deallocated`). A **Stopped (not Deallocated)** VM still bills
  for compute.
- **Disk** `diskState` from `properties.diskState` (`Unattached` disks are cleanup candidates;
  the owner must still confirm retention and recovery requirements).
- **App Service plan** `numberOfSites` from `properties.numberOfSites`: only an explicit numeric
  zero establishes an empty plan. Preserve absent, invalid, or unavailable counts as unknown;
  never default them to zero.
- **Container Apps environment** (`microsoft.app/managedenvironments`) — set
  `childrenInventoryComplete=True` only after a complete, paginated inventory of apps, jobs, and
  session pools, with verified parent associations. This is a collector assertion, not an ARM
  property. Missing child rows alone leave an empty-environment finding unvalidated. Known
  job/session-pool children prevent the app-only idle heuristic from supporting deletion.
- **Container App** (`microsoft.app/containerapps`) `environmentId` from
  `properties.environmentId` (fall back to `properties.managedEnvironmentId`) and `minReplicas` from
  `properties.template.scale.minReplicas`. An app with `minReplicas>=1` is **always-on** (bills even
  with zero traffic).
- **Dynamic session pool** (`microsoft.app/sessionpools`) `readySessionInstances` from
  `properties.scaleConfiguration.readySessionInstances`. Pre-warmed sessions bill continuously, so a
  pool with `readySessionInstances>=1` and no session traffic is an idle candidate subject to the
  sample-coverage and owner-review gates below. **Important:** these do
  **not** appear in `az resource list -g <rg>` (only Resource Graph returns them), yet they are
  frequently the single largest line items on the bill.
- Preserve `environmentId` associations for jobs and session pools when known. If a parent
  relationship or an inventory page cannot be verified, do not assert complete child inventory.

### Step 3 — Pull utilization (Azure Monitor) for VM and Container App candidates

For each VM or Container App candidate, pull trailing CPU **and memory** so the recommendation can be
**validated against reality**. Metrics are not optional: do the Step 4 cost pull first when cost
access exists, then pull metrics in **descending monthly-cost order** and cover at least the
**top 15** VM / Container App / session-pool candidates (all of them if fewer) before ranking.
Candidates remain unvalidated whenever required evidence is unavailable, including within that
bound; report the count and exact missing signals. Metrics need only **Reader** or
**Monitoring Reader** on the resource; a 403 here is an RBAC gap to report,
not a reason to skip the remaining resources.

```bash
az monitor metrics list --resource <vm-resource-id> --metric "Percentage CPU" --interval PT1H --start-time <UTC-14d> --aggregation Average Maximum -o json
```

Reduce the aligned series to `{cpu_p95, cpu_avg, mem_p95, sample_days, sample_coverage_pct}`.
CPU and memory must be percentages of the relevant capacity, not raw cores/bytes. `cpu_p95` is the
95th percentile of the hourly Average series; `sample_days` counts distinct observed days, not the
requested window. `sample_coverage_pct` is the minimum completeness of the required metric series.
Alternatively supply `sample_count` and `expected_sample_count` for that same interval/window.
Missing buckets are unknown, **not zero**. If both coverage forms are supplied, the lower coverage
governs. Preserve missing memory as `None`.

The bundled policy requires at least **7 observed days and 90% sample coverage** before supporting
a utilization-based action. A one-tier-down candidate needs p95 CPU below **20%** and p95 memory
below **40%**. An idle action needs CPU below **5%**, memory below **20%**, and independently
measured zero workload activity with sufficient coverage. These are this pack's conservative
policy defaults, **not Azure service limits or Advisor's own thresholds**. Known contradictory
CPU, memory, or activity invalidates the proposed action even if other evidence is missing.
CPU-only data remains a candidate; insufficient observed days do not generate a utilization-only
finding. Advisor candidates without enough evidence remain unvalidated.

Collect the actual supported metrics for the resource and normalize their units before calling
the helper. Container Apps' metric definitions distinguish raw CPU/memory from percentage metrics;
see [Microsoft Learn: Container Apps metrics](https://learn.microsoft.com/en-us/azure/container-apps/metrics).
Do not treat a CPU query as a memory measurement.

### Step 3b — Pull request activity (Azure Monitor) for Container Apps and session pools

For each container app, pull the trailing `Requests` metric to identify zero-request candidates.
Zero HTTP requests cannot establish that a background worker is idle. An app without usable
request coverage is not flagged by the app HTTP heuristic.

```bash
az monitor metrics list --resource <containerapp-resource-id> --metric "Requests" --interval P1D --start-time <UTC-14d> --aggregation Total -o json
```

For each **session pool**, pull `SessionApiRequestCount` (Total) — and optionally
`PoolExecutingPodCount` (Maximum, should be 0 when idle) — as its activity signal:

```bash
az monitor metrics list --resource <sessionpool-resource-id> --metric "SessionApiRequestCount" --interval P1D --start-time <UTC-14d> --aggregation Total -o json
```

Note: the session-pool metrics namespace is flaky — a call may return
`Microsoft.App/sessionPools is not a supported platform metric namespace`. **Retry once or twice.**
If it never succeeds, omit the pool from `activity`; the ranker still surfaces a warm pool as an
*unvalidated* candidate ("verify usage before reducing") rather than dropping the largest line items.

Sum actual Total observations into:

```python
activity = {
    resource_id: {
        "requests_total": observed_requests,
        "sample_days": observed_days,
        "sample_coverage_pct": observed_coverage,
        # VMs/Container Apps only; omit when no independent background-work signal exists:
        "workload_activity_total": observed_workload_activity,
    }
}
```

`workload_activity_total` is a normalized collector input, **not a platform metric name**. Derive it
from an appropriate independently observed workload signal that covers background/non-HTTP work;
never synthesize zero from `Requests == 0`. Its coverage must describe the same complete window.
VM/Container App idle actions need this activity evidence **and** the CPU/memory evidence from
Step 3. No HTTP traffic alone leaves app/environment findings as candidates. Jobs run in the same
environment as apps, so app HTTP traffic cannot establish their inactivity
([Microsoft Learn: Container Apps jobs](https://learn.microsoft.com/en-us/azure/container-apps/jobs)).

Session pools use their invocation signal instead: enough observed days plus the same sample
coverage gate are required to support zero-use findings. A warm pool with unavailable or sparse
usage metrics remains unvalidated. Pass the activity map unchanged to `activity=` below.

### Step 4 — Pull per-resource monthly cost (optional but recommended)

Reuse the `finops-cost-anomaly-detection` cost pull (modern Consumption UsageDetails GET, paginated) over
the trailing ~30 days (`metric=AmortizedCost`, so reserved/savings-plan-covered VMs show their
effective cost instead of $0) and aggregate the normalized `cost` from `prepare_usage_details`
`included_rows` by resource id into `{resourceId: monthly_cost}`.
In **modern billing the full ARM resource id is in `properties.instanceName`** (the `resourceId`
field is null) — project both fields separately and let the helpers resolve them:
`costs_by_resource(included_rows)` (from `finops-managed-scope/usage.py`) prefers `instanceName` and
falls back to `resourceId`. Ids are matched
case-insensitively. This is what lets the skill **rank by input-currency cost** and size idle waste. The Cost
Management Query/POST API is not needed.

Start with the complete trailing window using `%24top=1000`, project only the needed fields with
`--query "{value: value[].{id: id, date: properties.date, cost: properties.costInUSD, costInBillingCurrency: properties.costInBillingCurrency, billingCost: properties.cost, billingCurrencyCode: properties.billingCurrencyCode, billingCurrency: properties.billingCurrency, chargeType: properties.chargeType,
subscriptionId: properties.subscriptionId, resourceGroup: properties.resourceGroup,
instanceName: properties.instanceName, resourceId: properties.resourceId, tags: tags}, nextLink: nextLink}"`,
and paginate every `nextLink`. If a request returns `413 Request Too Large`, retry with `%24top=100`,
then `20`. Only if bounded pages still fail, use short half-open `usageStart` date slices as a
fallback; live runs showed that filter is not reliably applied, so verify returned dates and
de-duplicate the combined rows. `--query` is client-side and does not itself prevent a server 413,
but it keeps the retained sandbox payload small. If the pull still cannot complete, keep partial rows
but **label the savings totals "partial — cost pull truncated"** so the biggest line items aren't
undercounted silently.

Persist the page envelopes and run `finops-managed-scope`'s `prepare_usage_details(...)`. Aggregate
monthly cost only from `included_rows`; use its exact coverage and `partial` fields in the final
result. Do not merge or scope-filter UsageDetails in model reasoning.

Keep the dict-compatible `CostMap` returned by `costs_by_resource` intact so its `currency`
metadata survives. Canonically equivalent resource ids (case/trailing slash) are summed, including
in the cost-led review sweep. Utilization/activity dictionaries are indexed, **not added**.
Pass an explicit `currency=` only when verified; a conflict with `costs.currency` raises
`ValueError`. A plain dict without a verified currency stays unknown. No exchange-rate conversion
occurs, and non-resource/unattributed charges must not be assigned to invented resource ids.

### Step 5 — Rank (bundled rightsize.py, in-sandbox)

Read the module and run it — do **not** re-implement the logic in the prompt:

```
read_skill_file(skill_name="finops-rightsizing-advisor", file_path="rightsize.py")
```

```python
from rightsize import recommend_rightsizing
recs = recommend_rightsizing(
    resources=resources,       # from Step 2
    utilization=utilization,   # from Step 3  {resourceId: {...}}
    activity=activity,         # Step 3b: observed activity + sample coverage
    costs=costs,               # Step 4: CostMap; preserves the verified input currency
    advisor=advisor,           # from Step 1
    existing_resource_ids=existing_ids,  # from Step 2 `Resources | project id`
)   # returns findings ranked by generic estMonthlySavings desc
```

`recommend_rightsizing` handles all classification and validation:

- **kind** is `idle` (unattached disk, stopped-not-deallocated VM, empty App Service plan, empty or
  no-traffic Container Apps environment, always-on container app with no requests, warm dynamic
  session pool with no session traffic, or p95 CPU below `cpu_idle_pct`=5%), `oversized` (p95 CPU
  below `cpu_underutil_pct`=20%), `advisor` (an Advisor rec with no independent signal), or `review`
  (see cost-led sweep below).
- **validated / validationStatus**: `True` / `supported` when the required evidence supports a
  reviewable action; `None` / `candidate` when evidence is missing or insufficient; `False` /
  `contradicted` when known CPU, memory, or activity conflicts with the action. Contradictions
  override otherwise positive inventory/Advisor signals. Supported does not authorize execution.
- **currency / currentMonthlyCost / estMonthlySavings** are the canonical monetary fields.
  `currency=None` means unknown, not USD. `currentMonthlyUsd` and `estMonthlySavingsUsd` remain
  backward-compatible aliases only when currency is confirmed USD; otherwise they are `None`.
  An Advisor-only `savingsUsd` can establish USD for that estimate when no cost amount is present.
- **estMonthlySavings** retains full input-period cost for idle candidates, or a conservative 50%
  estimate for a one-tier downsize, merged with compatible Advisor estimates. These are potential
  estimates, not realized savings. Do not include candidate/contradicted estimates in a supported
  savings headline. The legacy argument `min_monthly_savings_usd` defaults to **5 input-currency
  units**, not an implicit USD conversion; unknown estimates are kept and sort last.
- **Cost-led coverage sweep** — after the rules run, any resource costing at least
  `review_min_monthly_usd` (default 20 input-currency units for the period) whose **type has no
  idle rule** and that **no other signal flagged** is surfaced as `kind="review"` (no savings
  claimed, `validated=None`). This guarantees no
  expensive line item is silently dropped just because we lack a heuristic for its type. Covered types
  that a rule evaluated but did not flag are **not** re-listed as review. When
  `existing_resource_ids` is passed, resources that no longer exist are skipped.
- Signals for the same `resourceId` (Advisor + inventory + utilization) merge into one finding, joined
  case-insensitively.

### Step 6 — Report

Produce a ranked table: resource (id + type), kind, current SKU, recommended action,
`currentMonthlyCost`, `estMonthlySavings`, **currency**, validation status, and evidence/sources.
Lead with **supported estimated savings only**; show candidate estimates separately. Contradicted
estimates are diagnostic only, never actionable savings. Do not rank contradicted rows as actions.
Use `summarize_optimization(rightsizing=recs)` for tested supported/candidate/contradicted rollups
and currency buckets rather than summing all rows. Never add different currencies or label an
unknown currency as dollars; show a mixed-currency scalar as unavailable and retain the buckets.
List `kind="review"` rows in a separate **"High spend, no idle rule yet — review"** section so the
report's coverage is complete by spend even where no automated recommendation exists.
Recommend only — do not perform any resize/deallocate/delete. If nothing clears the threshold, say so
in one line.

Read-only only. Do not use any write/POST Azure operations.

### Step 7 — Save the Live Report

After reporting, refresh the shared Live Report `FinOps: Rightsizing Savings` so the result is visible in Operations Hub. A scheduled task that already has ListReports/SaveReport steps owns this; follow it and do not save twice.

1. Save only when the run covered the full effective managed scope without a user scope override. Otherwise say the shared report was not refreshed and offer to save under a distinct name such as `FinOps: Rightsizing Savings (<scope>)`. Skip when the user asks not to save.
2. Call ListReports; call GetReport and reuse the exact-name report's `reportId`, or create it if absent.
3. Load `finops-report-renderer`, its SKILL.md, exact `models.py`, and exact `render.py`. With `datetime.now(timezone.utc)`, call `build_rightsizing_savings_model(rightsize_result, ...)` with `partial`, warnings, and scope coverage; do not construct the model or HTML by hand. Run `write_report(model, "finops-rightsizing-savings.html")`.
4. SaveReport the static file with `allowedTools: []`, name `FinOps: Rightsizing Savings`, and the reused `reportId`. Return the saved report id and version. A report-tool failure never suppresses the analysis result.

## Notes

- Storage-tier suggestions can arrive as Advisor candidates; reservation/savings-plan purchases
  are filtered out and belong to `finops-commitment-advisor`. Missing suitable evidence never
  becomes a supported recommendation.
- The 50% one-tier savings estimate and CPU thresholds are tunable via `recommend_rightsizing`
  keyword args if a subscription needs a different risk posture.
- Evidence/currency contract reviewed against `rightsize.py`; Container Apps metrics and shared
  app/job environment references above validated through Microsoft Learn on 2026-10-29.

## Microsoft workload optimization guidance

- **How Advisor flags a VM.** Advisor recommends shutdown or resize when CPU is at most 5% and
  network traffic is at most 7 MB on at least 4 of the last 7 days. It needs at least 14 days of
  data. A VM that Advisor does not flag may still be idle by this pack's rules, and the reverse is
  also true. Recommend that owners subscribe to Advisor alerts for new cost recommendations.
- **Non-production levers.** Recommend the following before resizing production:
  - tag the environment (for example `Env=dev`);
  - auto-shutdown outside working hours;
  - autoscale instead of fixed capacity;
  - serverless or auto-pause tiers;
  - Spot VMs for interruptible work;
  - avoiding cross-region data transfer.
- **Service-specific Advisor checks.** Advisor also covers:
  - Log Analytics: Basic logs and commitment tiers;
  - AKS: cost analysis, the vertical pod autoscaler, and Spot node pools;
  - Azure Data Explorer clusters;
  - Synapse auto-pause;
  - Premium disk snapshots that can move to Standard.

  These arrive as Step 1 `advisor` findings.
- **Complementary tools.** The FinOps toolkit Cost optimization workbook and the Azure Optimization
  Engine give a broader inventory view. Recommend them to customers who want a standing dashboard.

Sources: https://learn.microsoft.com/en-us/cloud-computing/finops/framework/optimize/workloads ·
https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/tutorial-acm-opt-recommendations
Last reviewed: 2026-10-23
