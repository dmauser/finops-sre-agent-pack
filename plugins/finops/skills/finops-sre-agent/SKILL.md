---
name: finops-sre-agent
description: FinOps for Azure SRE Agent spend, read-only. Attributes Azure Agent Unit (AAU) cost per SRE Agent in the managed scope, splits fixed always-on flow (4 AAU/agent-hour until deletion) from token-metered active flow, breaks active flow down by source (chat threads, scheduled tasks, incidents, Operations Hub incident analysis), ranks the costliest threads, projects month-end use against each agent's monthly AAU limit, and flags deleted/stopped-but-billed, idle, and consolidation candidates, using the bundled sreagent.py with the runtime's sandbox Python tool. Use for "what do our SRE Agents cost", AAU breakdown, agent consolidation, and SRE Agent cost governance.
---

## When to use this skill

Use it when the user wants to understand **Azure SRE Agent spend** — "how much do our SRE Agents
cost", "why is this agent's bill high", "which scheduled task burns the most AAU", "are we near the
monthly agent-unit limit", or "should we consolidate agents". It answers *what each agent costs,
which flow and source drive it, and where to look first*. It is read-only and recommends actions
only; it never stops, deletes, or reconfigures an agent.

`finops-cost-optimization-report` and the Monthly Review also roll this skill's output up
(`summarize_optimization(sre_agents=...)`, `build_monthly_review_model({..., "sre_agents": ...})`).

For Azure OpenAI / Foundry model spend use `finops-for-ai`; for cost spikes use
`finops-cost-anomaly-detection`; for owner/team showback use `finops-cost-allocation`.

## How SRE Agent bills (why this skill exists)

Azure SRE Agent bills in **Azure Agent Units (AAU)** on one meter
([pricing and billing](https://learn.microsoft.com/azure/sre-agent/pricing-billing)): UsageDetails
`consumedService = Microsoft.App`, `meterCategory = "Foundry Tools"`,
`meterSubCategory = "Azure Agent Unit"`, `meterName = "SRE Agent Unit"`, with the agent's ARM id
(`.../providers/Microsoft.App/agents/<name>`) in `instanceName`. Two flows share that meter:

- **Always-on flow** — a fixed **4 AAU per agent-hour** (96 AAU/day, ~2,920 AAU/month) from creation
  until the agent is **deleted**. **Stopping an agent does not end it.** It is waived during the
  30-day trial.
- **Active flow** — token-metered AAU for chats, incident investigations, scheduled tasks, and
  triggers. It varies by model and is capped per agent by `monthlyAgentUnitLimit`; at the cap chat
  and actions stop until the next month. The always-on flow is not capped.

Cost data only has the **blended daily AAU** per agent, so it cannot tell always-on from active. The
agent's own read-only ARM API has per-thread active AAU per day with its **source**. This skill joins
both: for each billed day `always_on = min(max(billed AAU - active AAU, 0), 96)`. Excess billed
units become `unreconciled_aau` / `unreconciled_usd`; activity above same-day billing becomes
`unmatched_active_aau`. Billing and telemetry can arrive at different times. Never relabel that
difference as always-on usage or supported idle/consolidation savings.

Agents without per-thread data use a bounded estimate, labeled `estimated`, not a measured split.
Only reconciled measured low-use agents support consolidation hints. Source/thread costs are
rate-based estimates, not settled invoice amounts. Mixed billing currencies are rejected.

## Required access

- **Cost Management Reader** on the subscription (UsageDetails returns no cost values without it).
- **Reader** on the SRE Agent resources for Resource Graph and the agent `usages` /
  `dailyUsageDetails` GETs.
- Read-only `az` (`RunAzCliReadCommands`) and sandbox Python: use `ExecutePythonCode` when
  available, otherwise `RunInTerminal` with `python3`. Only GET requests are used.

## Procedure

### Step 0 — Resolve the managed boundary

First load `finops-managed-scope` and follow its `scope.py` procedure to dynamically GET and validate
the current agent `managedResources`; never use cached scope. Managed scopes and their expanded
descendants are the default boundary. Scheduled runs are strict/fail-closed with no override. An
interactive named resource outside the boundary requires disclosure and explicit confirmation in a
subsequent turn before any broader query. Broad RBAC never expands scope implicitly.

Pull UsageDetails independently for each effective scope where supported, with separate pagination
state and completeness tracking. De-duplicate overlapping line items, then filter resource ids and
attributable scope fields against the expanded boundary before SRE Agent classification. Run
Resource Graph once per unique effective subscription with `--subscriptions`; for RG-only managed
scopes, use the exact case-insensitive resource-group predicate and client-side ARM-prefix filter
below. Report included scopes, excluded rows, unattributed SRE Agent cost, unsupported scopes, and
partial/failed scope coverage.

### Step 1 — Pull SRE Agent cost line items

Follow the canonical Consumption UsageDetails transport contract in `finops-managed-scope`, once per
effective scope. Project the fields the classifier needs on top of the usual cost fields:

```
--query "{value: value[].{id: id, date: properties.date, cost: properties.costInUSD, costInBillingCurrency: properties.costInBillingCurrency, billingCost: properties.cost, billingCurrencyCode: properties.billingCurrencyCode, billingCurrency: properties.billingCurrency, chargeType: properties.chargeType, consumedService: properties.consumedService, meterCategory: properties.meterCategory, meterSubCategory: properties.meterSubCategory, meterName: properties.meterName, quantity: properties.quantity, subscriptionId: properties.subscriptionId, resourceGroup: properties.resourceGroup, instanceName: properties.instanceName, resourceId: properties.resourceId}, nextLink: nextLink}"
```

`quantity` is the billed AAU and is required for the always-on/active split. When a scheduled run
already pulled UsageDetails with these fields for another skill, reuse that pull — do not pull again.

Pass the persisted page envelopes through `finops-managed-scope`'s `prepare_usage_details(...)` and
use only `included_rows`. Keep rows where `sreagent.is_sre_agent_line(row)` is true; do not merge,
de-duplicate, or scope-filter pages in model reasoning. If no row matches, say there is no SRE Agent
spend in scope in one line and stop.

### Step 2 — Agent inventory (Resource Graph)

```bash
az graph query \
  --subscriptions <EFFECTIVE_SUBSCRIPTION_ID> \
  -q "Resources | where type =~ 'microsoft.app/agents' | project id, name, resourceGroup, location, tags, powerState = properties.powerState, runningState = properties.runningState, monthlyAgentUnitLimit = properties.monthlyAgentUnitLimit, defaultModel = properties.defaultModel" \
  --first 1000 \
  -o json
```

Run once per unique effective subscription. For an RG-only managed scope add
`| where resourceGroup =~ '<EFFECTIVE_RESOURCE_GROUP>'` before `project` and keep only ids whose
normalized ARM prefix is exactly
`/subscriptions/<effective_subscription_id>/resourcegroups/<effective_resource_group>/`
(client-side filter, on every paginated request). De-duplicate overlapping results.
An agent billed in Step 1 but missing here is reported as `deleted`.

### Step 3 — Per-agent active-flow detail (read-only ARM GETs)

For each in-scope agent id from Step 2 (at most 20; say so if more were skipped):

```bash
az rest --method get --url "https://management.azure.com<AGENT_ID>/dailyUsageDetails?api-version=2026-01-01" -o json
az rest --method get --url "https://management.azure.com<AGENT_ID>/usages?api-version=2026-01-01" -o json
```

- `dailyUsageDetails` returns `value[]` rows `{date, source, threadId, value}` (active AAU per
  thread per day, about the last 45 days) and may return `nextLink` — follow it with GET until
  absent. Date query parameters are ignored; `sreagent.py` filters to the billed window itself.
- `usages` returns `TotalAgentUnits` with `currentValue` (month-to-date active AAU) and `limit`.

Persist each response to a file keyed by agent id. A failed GET for one agent is a warning, not a
failure — that agent falls back to the `estimated` split.

### Step 4 — Analyze (bundled sreagent.py, in-sandbox)

Read the module and run it — do **not** re-implement the logic in the prompt:

```
read_skill_file(skill_name="finops-sre-agent", file_path="sreagent.py")
```

```python
from sreagent import analyze_sre_agent_costs, is_sre_agent_line
result = analyze_sre_agent_costs(
    line_items=[row for row in included_rows if is_sre_agent_line(row)],  # Step 1
    agents=agent_inventory,             # Step 2 rows
    daily_usage=daily_usage_by_agent,   # Step 3 {agentId: dailyUsageDetails response}
    monthly_usage=monthly_by_agent,     # Step 3 {agentId: usages response}
    as_of=today_utc,
)
```

`analyze_sre_agent_costs` returns:

- **total_usd**, **total_aau**, **always_on_usd**, **active_usd**, **always_on_pct**,
  **agent_count** (billed), **inventory_count**, **window_start/window_end/window_days**,
  **usd_per_aau**, and **always_on_monthly_usd_per_agent** (4 AAU × 730 h × rate).
- **by_agent** — per agent: status (`running`/`stopped`/`deleted`), model, billed USD/AAU, the
  always-on vs active AAU and USD split (`split = measured|estimated`), days billed, MTD active
  AAU, `limit_aau`, `projected_month_active_aau`, `projected_limit_pct`, and `always_on_waived`.
- **by_source** — measured active AAU and USD by source (Chat / threads, Scheduled tasks,
  Incidents, Incident analysis, Triggers, Evaluations, Other) with distinct thread counts.
- **top_threads** — the costliest threads (agent, thread id, source, AAU, USD, days, last active).
- **daily** — billed USD/AAU per day for the trend.
- **hints** — `deleted_agent_billed`, `stopped_agent_billed` (stopped agents still pay always-on
  until deleted), `idle_agent` (always-on paid, little active use), `consolidation_candidate`
  (several mostly-idle agents → one agent can manage multiple scopes), `limit_risk` (projected to
  hit the monthly limit → chat/actions stop), `limit_headroom_review` (limit far above use, so it
  is not a useful circuit breaker), `scheduled_task_heavy`, `incident_heavy`,
  `always_on_waived_verify` (trial waiver → budget for always-on when it ends), and
  `model_choice_verify` (premium default model with material active spend).
- **warnings** — mixed currency, estimated splits.

### Step 5 — Report

Produce: a **headline** (total SRE Agent spend, agents billed vs in inventory, always-on vs active
split, monthly always-on cost per agent); a **per-agent** table (status, billed, always-on, active,
MTD active vs limit with projected %); a **by-source** table; the **top threads**; and the **hints**
as a "where to look" list with their monthly USD. Thread ids map to the portal's
**Settings → Agent consumption** view, which also exports CSV. Label `estimated` splits, the
`always_on_waived_verify`, `model_choice_verify`, and `deleted_agent_billed` hints as **verify
first**. Never sum always-on and active dollars into one "savings" number — only deleting an agent
removes always-on flow; active flow is reduced by changing what the agent does. If cost was partial,
say so at the top.

When relevant, close with a short **governance** note (advice only — this skill changes nothing):

- **The monthly agent-unit limit is the circuit breaker** for active flow — set it to ~1.5–2× the
  expected month so a runaway task stops early but routine work is not cut off. It does not cap
  always-on flow.
- **Delete, don't stop**, agents that are no longer needed; consolidate low-use agents — one agent
  can manage several resource groups or subscriptions.
- **Scheduled tasks** — review cadence, merge overlapping tasks, reuse one data pull per run, and
  test prompts in chat before scheduling.
- **Incidents** — use incident response plans and filters so only actionable incidents start an
  investigation.
- **Budgets and anomaly alerts are smoke detectors** — they fire after spend; pair them with the
  monthly limit.

### Step 6 — Save the Live Report

After reporting, refresh the shared Live Report `FinOps: SRE Agent Spend` so the result is visible in Operations Hub. A scheduled task that already has ListReports/SaveReport steps owns this; follow it and do not save twice.

1. Save only when the run covered the full effective managed scope without a user scope override. Otherwise say the shared report was not refreshed and offer to save under a distinct name such as `FinOps: SRE Agent Spend (<scope>)`. Skip when the user asks not to save.
2. Call ListReports; call GetReport and reuse the exact-name report's `reportId`, or create it if absent.
3. Load `finops-report-renderer`, its SKILL.md, exact `models.py`, and exact `render.py`. With `datetime.now(timezone.utc)`, call `build_sre_agent_spend_model(analyze_sre_agent_costs_result, ...)` with `partial`, warnings, and scope coverage; do not construct the model or HTML by hand. Run `write_report(model, "finops-sre-agent-spend.html")`.
4. SaveReport the static file with `allowedTools: []`, name `FinOps: SRE Agent Spend`, and the reused `reportId`. Return the saved report id and version. A report-tool failure never suppresses the analysis result.
