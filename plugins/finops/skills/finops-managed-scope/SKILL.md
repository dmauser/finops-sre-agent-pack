---
name: finops-managed-scope
description: Read-only scope guard for FinOps skills. Dynamically reads the Azure SRE Agent managedResources configuration, canonicalizes subscription/resource-group/management-group scopes, expands management groups with caller-supplied descendant data, filters UsageDetails rows, reports exact coverage, and applies fail-closed scheduled or explicitly confirmed interactive policy using bundled dependency-free scope.py.
---

## Purpose

Use this foundational skill before a FinOps skill retrieves or reports Azure cost. It
prevents a scheduled task from silently querying beyond the Azure SRE Agent's current
managed resources and makes broader interactive requests visible to the user.
Broad Azure RBAC is not evidence that a scope is managed: managedResources remains the
policy boundary even when the agent identity can read more.

This skill is read-only. It introduces no runtime changes and uses no write tools. The
bundled `scope.py` is pure offline logic and makes **no Azure calls**. Keep the whole
skill directory together: `scope.py` loads its dependency-free `_scope_resolution.py`
sibling relative to `__file__`, so direct dynamic imports do not depend on `sys.path`.

## 1. Read the live managed scope every run

Do not cache, copy from the manifest, or infer scope from prior conversations. Read the
current agent resource dynamically:

```bash
az resource show --ids <AGENT_RESOURCE_ID> \
  --query properties.knowledgeGraphConfiguration.managedResources -o json
```

Pass the returned array to `resolve_managed_scopes`. Entries may be scope strings or
objects with `id`, `resourceId`, or `scope`. Only subscription, resource-group, and
management-group scope IDs are accepted. Duplicate casing is handled
case-insensitively and diagnosed.

If `AGENT_RESOURCE_ID` is unavailable, the read fails, the result is malformed/empty,
or required expansion cannot be completed, a scheduled run must stop **fail closed**.

## 2. Discover management-group descendants when needed

Management-group membership is not encoded in a resource ID. For each configured
management group, use read-only Azure CLI discovery to collect descendant subscriptions
and child management groups, following child groups until all descendant subscriptions
are known. For example:

```bash
az account management-group subscription show-sub-under-mg \
  --name <MANAGEMENT_GROUP_ID> -o json
```

That command returns direct subscriptions only. Build the complete recursive descendant
set using either:

- Azure Resource Graph subscription-container data and each subscription's
  `properties.managementGroupAncestorsChain`, selecting subscriptions whose ancestry
  contains the configured management group; or
- `az account management-group show --name <MANAGEMENT_GROUP_ID> --expand --recurse
  -o json`, recursively walking its returned child management groups and subscriptions.

The caller must supply **all descendant subscriptions**, not only direct children. Pass
the resulting mapping/records as `management_group_expansions`; `scope.py` handles
recursive expansion, overlap with explicit subscriptions/RGs, duplicate casing, and
cycles. It never performs discovery itself.

## 3. Resolve and enforce

Read and run the helper instead of reimplementing it:

```python
from scope import (
    decide_scope_policy,
    filter_usage_details,
    resolve_managed_scopes,
)

resolved = resolve_managed_scopes(
    managed_resources,
    management_group_expansions=mg_descendants,
)
decision = decide_scope_policy(
    requested_scopes,
    managed_resources,
    mode="scheduled",  # or "interactive"
    management_group_expansions=mg_descendants,
)
```

- **Scheduled:** strict and fail closed. Never continue for an outside scope, malformed
  or empty managed scope, or an unexpanded management group. Scheduled mode accepts no
  override; user confirmation cannot broaden it.
- **Interactive:** continue directly only when all requested scopes/resources are
  contained. A broader/outside request returns
  `outside_scope_confirmation_required`; show the exact `outside_scopes` and obtain an
  explicit user confirmation together with the returned `confirmation_key`. Re-evaluate
  in a subsequent turn with both `outside_scope_confirmed=True` and
  `confirmation_key=<DISPLAYED_KEY>`. Missing or mismatched keys remain confirmation
  required and must show the newly computed request/key. Silence, ambiguity, or a
  declined confirmation is denial.

The confirmation key binds approval to the exact displayed request. A key displayed for
request B cannot authorize request C. Confirmation does not alter managedResources or
become a standing permission.

## 4. Filter UsageDetails before analysis

Consumption UsageDetails uses a **subscription-scoped transport endpoint**. For a
subscription effective scope, query that subscription directly. For one or more RG-only
effective scopes, query each distinct containing subscription once, paginate the complete
result, and then apply `filter_usage_details` to retain only rows inside the exact managed
resource groups. Never construct
`/resourceGroups/<rg>/providers/Microsoft.Consumption/usageDetails`; that endpoint is not
supported. Querying the containing subscription is transport only and does not broaden the
logical analysis boundary.

Use this canonical UsageDetails transport contract in every consuming FinOps skill:

- Use GET on `/subscriptions/<SUB_ID>/providers/Microsoft.Consumption/usageDetails` with
  `metric=AmortizedCost` for analysis (showback, anomaly, rightsizing, AI, reliability, cost
  overview) so reservation/savings plan purchases are spread across the resources that used them
  instead of appearing as one-time spikes with $0 covered usage. Use `metric=ActualCost` only when
  matching invoice/budget cash flow (for example `finops-budget-editor`). Start at `%24top=1000`;
  never use the POST Cost Management Query API.
  Note: `$` must be URL-encoded as `%24` in `az rest` URLs to avoid shell escaping issues with the
  agent's read-only CLI tool (e.g. `%24top=1000`, `%24filter=...`, `%24skiptoken=...`). Never write
  `\$top` or a bare `$top`.
- Project the response envelope as `{value: value[].{...}, nextLink: nextLink}`. Keep only the
  fields needed by scope enforcement and the analysis, alias `cost` once from
  `properties.costInUSD`, and always add `costInBillingCurrency: properties.costInBillingCurrency,
  billingCost: properties.cost`, `billingCurrencyCode: properties.billingCurrencyCode, billingCurrency: properties.billingCurrency`,
  and `chargeType: properties.chargeType`. Project both `instanceName: properties.instanceName,
  resourceId: properties.resourceId`. Include `subscriptionId` and
  `resourceGroup` whenever rows may need metadata-based attribution.
- **Never use `||` (or any other shell-like operator) in `--query`.** The read-only CLI tool rejects
  it with an uninformative `ValidationError`. Project the candidate fields separately, as above; the
  Python helpers resolve the fallbacks: `prepare_usage_details` prefers `costInBillingCurrency` over
  the legacy `billingCost` and `billingCurrencyCode` over `billingCurrency`, and sets each included
  row's `resourceId` to `instanceName` when that is a full ARM id (modern billing), falling back to
  `resourceId`.
- Legacy Enterprise Agreement rows have no `costInUSD`; their cost is `properties.cost` in the
  billing currency. `prepare_usage_details` normalizes every row's `cost` deterministically: USD
  when all rows carry `costInUSD`, otherwise billing currency for all rows (rejecting mixed billing
  currencies). Report its `currency`, `cost_source`, `cost_metric`, and `warnings`. A null
  `costInUSD` alone is **not** evidence of missing Cost Management Reader access — only
  `missing_cost_count` (rows with no cost value at all) is.
- If any request returns 413, retry that request with `%24top=100`, then `20`. If a request
  **times out** (exit code 124), do not drop straight to `%24top=100` (that multiplies the page
  count tenfold): retry with `%24top=500`, then `250`, and if it still times out split the window
  into calendar-month `usageStart` slices at `%24top=1000`, one chain per slice. Use verified,
  half-open `usageStart` date slices only if bounded pages still fail, and de-duplicate the
  combined rows with `filter_usage_details(..., sources=[slice label per row])` (never by `id`
  or `(resourceId, date, meter)` alone — distinct rows share those).
- If retrieval remains incomplete, retain the available rows only with an explicit partial-cost
  warning on every downstream total.
- **Finish the chain first.** In live runs, "partial" was almost always caused by the agent
  stopping pagination early (spending its tool-call/context budget on other API calls or on
  echoing page contents), not by an API failure. So: pull UsageDetails **once per run** and
  follow every `nextLink` until it is null **before** any other analysis call, without summarizing
  pages in chat; project the **union** of fields every
  consumer needs in that single pull (add `pricingModel`, `meterCategory`, `quantity`,
  `unitOfMeasure`, `tags` up front) so no
  skill re-pulls; and bound the window to what the analysis needs (trailing 30 days, or
  month-to-date for daily status). "Enough rows" is never a reason to stop — `partial` is allowed
  only after the 413 fallbacks above fail. When partial, report the pages retrieved and the
  `remaining_next_link` so an immediate follow-up in the same conversation can resume that chain.

Do not add a `properties/resourceGroup eq ...` filter to the subscription UsageDetails request.
The service may silently ignore that filter. Fetch the initial page once, persist it, and follow
only that response's `nextLink` chain to completion; do not mix pages from independently restarted
requests because ordering and newly settled usage can differ between calls. Decode `&amp;` in a
returned `nextLink`, then GET it as-is without re-adding initial query parameters. Apply the exact
case-insensitive RG boundary with `filter_usage_details` only after the complete chain is assembled.

**Page fast: one tool call per page.** `az` prints `nextLink` first in the output, and the runtime
already saves every `RunAzCliReadCommands` result under `tmp/ToolOutputs/`. So issue the next
`az rest` call immediately with the shown `nextLink` (decode `&amp;` to `&`). Do **not** run a
Python/terminal step between pages to save, count, or print a page, and do not loop `az` in a
subprocess (it has no Azure credentials). After the chain ends, rebuild every page with its exact
request URL in **one** Python call and let `usage.py` (code, not the model) validate continuity:

Before the first `az rest` call of the run, record the run start in a Python step
(`run_start = time.time()`, written to `tmp/finops-data/run_start.txt`). `tmp/ToolOutputs/`
persists across scheduled-task executions, so without it pages from earlier runs merge into the
current chain (a live run assembled a 28-page chain of mixed dates instead of 4 pages).

Scheduled tasks that run at the same time page the same UsageDetails URL into the same
directory, and their files interleave by time (a live run merged three concurrent tasks' pages
into 6/4/2-page chains and reported $911 instead of $1,130). The runtime names each saved output
`<thread-id>_RunAzCliReadCommands_<call-id>.json`, and the chain finder never chains files with
different thread-id prefixes. Also pass `thread_id`: the id is the prefix of the saved-output
path shown for this run's first `az rest` call.

```python
from usage import prepare_usage_details
from usage import find_usage_tool_output_chains, load_usage_tool_output_pages

chains = find_usage_tool_output_chains(
    "tmp/ToolOutputs",
    after_timestamp=run_start,  # ignore outputs from earlier executions
    thread_id=thread_id,  # ignore outputs from concurrent tasks
    request_url_prefix=f"/subscriptions/{sub_id}/providers/Microsoft.Consumption/usageDetails",
)
chain = chains[-1]  # newest chain for this subscription; one per scope/slice
usage_pages = load_usage_tool_output_pages(first_request_url, chain)
coverage = prepare_usage_details(
    usage_pages,
    managed_resources,
    management_group_expansions=mg_descendants,
)
```

`first_request_url` is the exact URL of the chain's first `az rest` call. If the final page's output
was not saved to `tmp/ToolOutputs` (small outputs may be shown inline only), append it as a
`{"request_url": <preceding decoded nextLink>, "value": [...], "nextLink": null}` envelope. Manually
persisted envelopes `{"request_url": "...", "value": [...], "nextLink": "..."}` remain valid input;
never use a placeholder `request_url`.

`prepare_usage_details` rejects restarted/mixed chains, requires the final `nextLink` to be
retrieved, merges all rows, normalizes each row's `cost`/`currency`, and calls
`filter_usage_details`. It also returns `unused_commitment_cost` (AmortizedCost
`UnusedReservation`/`UnusedSavingsPlan` rows — commitment waste that is not attributable to any
resource; report it separately, never spread it onto RGs) and `purchase_cost` (one-time purchases
in an ActualCost pull). Set `require_complete=False` only
after all bounded-page and date-slice fallbacks fail; the returned `partial` flag and
`remaining_next_link` must then be disclosed on every downstream total.

The filtering core prefers UsageDetails `instanceName` (the modern full ARM ID), falls back to
`resourceId`, then to resource-group/subscription metadata. It removes rows duplicated
by overlapping retrievals (never rows that merely share a UsageDetails `id` — Azure reuses
one `id` across distinct rows, and identical projected rows inside one continuation chain
are distinct charges), keeps subscription-level shared charges only when the subscription
itself is managed,
and separates included, excluded, and unattributed rows. Costs and totals are
`Decimal`; do not convert them to binary floats before reconciliation.

Report `included_count`, `excluded_count`, `unattributed_count`, `duplicate_count`,
their exact cost totals, `attribution_coverage_pct`, and deterministic diagnostics.
Never hide excluded or unattributed spend, and never assign an unattributed/shared
charge to an RG by guesswork.

Retrieve analysis data for each effective scope independently, then pass rows to
`filter_usage_details(..., sources=[...])` with one label per retrieval (continuation chain
or date slice) so only cross-retrieval overlap is removed. Include excluded and unattributed
coverage in the final report; do not broaden a query merely because RBAC permits it.

## 5. MOSP and MCA subscriptions ignore UsageDetails date filters

For **Microsoft Online Services Program (MOSP / pay-as-you-go)** and **Microsoft Customer
Agreement (MCA)** subscriptions, the subscription-scoped Consumption UsageDetails API silently
ignores **every** date filter: `properties/usageStart`, `properties/usageEnd`,
`properties/usageDate`, and `properties/date` in `%24filter`, as well as other API versions. It
returns the **current open billing period only**, whatever range was requested. The
`billingPeriods` endpoint returns an empty `[]` for these subscription types, so
billing-period-scoped URLs (`.../billingPeriods/<name>/providers/Microsoft.Consumption/usageDetails`)
are not a fallback either. Production runs wasted ~10 tool calls retrying filter formats and API
versions.

- **Detect once, then stop.** After the first page of a chain, compare the row `date` values
  with the requested window. If the page contains dates outside the requested range, or none of
  the requested earlier months, the filter is being ignored. Do not retry other filter syntaxes,
  API versions, or `billingPeriods` URLs. Finish paging that chain normally.
- **Fallback.** Accept the current billing period's rows, filter them by `date` client-side after
  retrieval (half-open: `start <= date < end`), and run the analysis on what remains.
- **Label it partial.** Report the requested range against the range actually obtained (min/max
  `date` of the retained rows). Mark every total, forecast, and month-over-month comparison
  derived from the missing period as partial or `unknown`. Never fill the gap with estimates.
- Enterprise Agreement subscriptions honour the date filters and expose `billingPeriods`; this
  section does not apply to them.

**Cost data at scale (Microsoft guidance).** This pack reads UsageDetails because the agent's
tools are read-only and have no storage. Microsoft's guidance for larger estates is different:

- Use **Cost Management Exports** (scheduled to storage) for large or recurring datasets.
- Use the **Cost Details API** for small, on-demand reports.
- Move to Exports when a scope produces more than about 2 GB a month. One million rows is roughly
  1 GB.
- Query any one scope at most once a day; data refreshes about every four hours.
- Split large pulls by date range and child scope.
- Cache invoiced months, which no longer change.

Recommend Exports, or FinOps hubs (about USD 120 a month plus about USD 10 for each USD 1M of spend
monitored), when a scope repeatedly hits paging or throttling limits. Also recommend them when
the customer needs the history that section 5 cannot provide. Cost allocation rule reassignments
do not appear in UsageDetails; see `finops-cost-allocation`.
Source: https://learn.microsoft.com/en-us/azure/cost-management-billing/automate/usage-details-best-practices.
Last reviewed: 2026-10-23.

## 6. Pass every non-usage input through a deterministic file

Do not rebuild analysis inputs by scanning `tmp/ToolOutputs/` with heuristics (checking for
`category`, `type`, or `data` keys). Small responses are returned inline and never saved there, and
budget, Advisor, and tag responses look alike. Only UsageDetails pages use the chain detection
above.

Instead, right after each non-usage Azure call (or once after a batch of calls, before any
analysis), save the response under a fixed name with `usage.py`:

```python
from usage import save_finops_input, load_finops_input

# response saved by the runtime: pass the exact path it reported for that call
save_finops_input("advisor", source_path="tmp/ToolOutputs/<file reported for the Advisor call>.json")
# response shown inline: pass the JSON exactly as returned (string or parsed)
save_finops_input("budgets", budgets_json)
save_finops_input("inventory", inventory_json)   # Resource Graph inventory
save_finops_input("tags", tags_json)             # resource / tag data
# usage_data -> handled by find_usage_tool_output_chains / load_usage_tool_output_pages

# later, in the analysis script
advisor = load_finops_input("advisor", default=None)   # None when the call failed or was skipped
```

Files go to `tmp/finops-data/<name>.json` (`advisor.json`, `inventory.json`, `budgets.json`,
`tags.json`). Delete `tmp/finops-data/` at the start of a scheduled run so stale inputs never leak
in. A missing file means that source was unavailable; report it as a gap rather than guessing.
