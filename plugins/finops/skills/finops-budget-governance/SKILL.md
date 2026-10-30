---
name: finops-budget-governance
description: Proactive budget governance for Azure, read-only. Reads native Azure Cost Management budgets (GET Microsoft.Consumption/budgets — amount, currentSpend, and forecastSpend when present) and evaluates each against its amount and its own notification thresholds; where Azure supplies no forecast it computes a linear run-rate month-end projection in-sandbox, so every budget gets a landing estimate. Surfaces budgets that are over, forecast-to-exceed, or at-risk, ranks them by severity, and flags the ones that warrant a process gate (a human decision before more is spent) — using the bundled budget.py with the runtime's sandbox Python tool. Use for "are we on budget", burn-rate / forecast checks, and month-end overrun early warning.
---

## When to use this skill

Use it when the user wants a **proactive read on Azure spend against defined budgets** — "are we on
budget this month", "will any budget blow past its cap", burn-rate / run-rate checks, or an early
warning before a month-end overrun. It reads the customer's own Azure Cost Management budgets and
tells them which are over, which are *forecast* to go over, and which need a decision now. It is
read-only: it reports and recommends, but never creates or edits a budget.

For cost *spikes* and their cause use `finops-cost-anomaly-detection`; for idle/oversized waste use
`finops-rightsizing-advisor`; for *who owns the spend* use `finops-cost-allocation`. Recommendations
and deterministic create/update plans with a human-run script are supported separately by
`finops-budget-editor`. Both skills remain agent-side read-only.

## Required access

- **Cost Management Reader** (or **Reader**) on the subscription — enough to `GET
  Microsoft.Consumption/budgets`. No write role is needed or used.
- Read-only `az` (`RunAzCliReadCommands`) and sandbox Python: use `ExecutePythonCode` when
  available, otherwise `RunInTerminal` with `python3`. No POST/PUT/write APIs are called.

## Scope

Evaluates every budget returned by the budgets GET at the requested scope (subscription or a resource
group). It is **service-agnostic** — it works off the budget objects Azure returns, not a hard-coded
resource list. A budget with **no budgets defined** is a first-class result: the skill reports "no
budgets defined" and points to `finops-budget-editor`, which can recommend a budget or generate a
reviewable create plan and human-run script, rather than failing silently. Azure's
own `forecastSpend` is preferred when present; when it is absent (it often is on the GET response)
the skill computes a **linear run-rate** month-end forecast in-sandbox and labels the source.
Multi-currency portfolios are out of scope. A configured management-group scope is queried directly
for budgets defined at that management group; those budgets are evaluated alongside budgets from the
expanded descendant effective scopes.

## Procedure

### Step 0 — Resolve the managed boundary

First load `finops-managed-scope` and follow its `scope.py` procedure to dynamically GET and validate
the current agent `managedResources`; never use a cached result. The validated managed scopes are the
default boundary. Scheduled runs are strict, fail closed on discovery/validation failure or an empty
list, and accept no override. In interactive work, a named outside-scope budget or target requires
disclosure and explicit confirmation in a subsequent turn before any broader Azure query. Broad RBAC
never silently expands the boundary.

Build budget query targets as the case-insensitive, canonical union of:

1. every configured management-group scope from the validated `managedResources` list, queried
   **directly** so management-group-level budgets are not lost; and
2. each effective scope in the expanded descendant set where the Budgets API is supported.

De-duplicate that union before querying and query each target exactly once, including when an
explicit/effective scope overlaps. Do not query descendant management groups merely because they were
discovered for expansion unless the descendant management group is itself configured in
`managedResources`. Keep scope
identity on every budget, reject responses outside the boundary, and report included scopes, excluded
responses, unsupported/failed scopes, and scopes with no budgets. Do not replace per-scope evaluation
with a single subscription-wide list.

### Step 1 — Read the budgets (read-only GET)

Pull the native budgets at the target scope. Subscription scope:

```bash
az rest --method get \
  --url "https://management.azure.com/subscriptions/<SUB_ID>/providers/Microsoft.Consumption/budgets?api-version=2023-05-01" \
  -o json
```

For every configured management group, query its budget collection directly:

```bash
az rest --method get \
  --url "https://management.azure.com/providers/Microsoft.Management/managementGroups/<MG_ID>/providers/Microsoft.Consumption/budgets?api-version=2023-05-01" \
  -o json
```

Also repeat the GET for each expanded descendant effective scope, subject to the de-duplicated query
targets above. For a resource-group budget, use
`.../subscriptions/<SUB_ID>/resourceGroups/<RG>/providers/Microsoft.Consumption/budgets`. The response
is `{"value": [ ... ]}`; pass the whole `value` array to Step 2. Each budget carries
`properties.amount`, `properties.timeGrain`, `properties.timePeriod`, `properties.currentSpend`
(`{amount, unit}` — spend so far in the current grain window), an optional `properties.forecastSpend`,
and `properties.notifications` (each with a percent `threshold` and a `thresholdType` of `Actual` or
`Forecasted`). **If `value` is empty, skip to Step 3 and report "no budgets defined".**

> **`currentSpend` freshness — do not reconstruct spend with a cost pull.** Azure computes each
> budget's `currentSpend` asynchronously: a **newly created** budget reads `currentSpend: 0` for hours
> until the Cost Management pipeline populates it. Treat a `0` on a just-created budget as "not yet
> synced", not as real zero spend. Do **not** try to reconstruct month-to-date spend with a
> UsageDetails pull — that heavy, `413`-prone query is unnecessary for budget status, and the clean
> Cost Management aggregate query is a `POST`, which the agent's read-only tooling blocks. Rely on
> Azure's `currentSpend` once it syncs.

### Step 2 — Evaluate (bundled budget.py, in-sandbox)

Read the module and run it — do **not** re-implement the logic in the prompt:

```
read_skill_file(skill_name="finops-budget-governance", file_path="budget.py")
```

```python
from budget import evaluate_budgets
result = evaluate_budgets(budgets)   # the value[] array from Step 1
```

`evaluate_budgets` handles all math and classification:

- **budgets**: per-budget evaluation ranked by severity — `current_spend`, `pct_used`,
  `forecast_spend` + `forecast_source` (`azure` when Azure supplied it, else `run-rate`), `pct_forecast`,
  `status`, and the budget's own **breached_notifications**.
- **status** buckets: `over_budget` (already ≥ amount) → `forecast_over` (projected ≥ amount) →
  `at_risk` (≥ 80% used or an Actual threshold breached) → `on_track`.
- **summary**: portfolio totals (`total_amount`, `total_current`, `total_forecast`, `currency`) and a
  count of each status. Totals are never summed across currencies: when budgets use more than one
  currency the `total_*` fields are `null` and `totals_by_currency` carries one total per currency.
  Run-rate forecasts use the real calendar length of the budget's current Monthly/Quarterly/Annual
  reset window, anchored at `timePeriod.startDate`.
- **gates**: the budgets in `over_budget` / `forecast_over` — the ones that warrant a **process gate**
  (a human decision before more is spent), each with a one-line reason.
- **no_budgets**: `True` when nothing is defined — report it and recommend creating a budget.

### Step 3 — Report

Produce a **budget status table** (name, scope, amount, spent + `pct_used`, forecast + `pct_forecast`
+ source, status), with the **portfolio totals** row and any **gated** budgets called out at the top as
the action items (with their reason). List each budget's **breached notification thresholds**. If a
forecast is `run-rate` (not Azure's), say so — it is an estimate. If a budget's `current_spend` is `0`
on a newly created budget, note it may be an unsynced value rather than real zero spend. If **no
budgets are defined**, say that plainly and point to `finops-budget-editor` for a recommendation or
a deterministic create plan and human-run script. If every budget is `on_track`, say so in one line.

### Step 4 — Save the Live Report

After reporting, refresh the shared Live Report `FinOps: Budget Status` so the result is visible in Operations Hub. A scheduled task that already has ListReports/SaveReport steps owns this; follow it and do not save twice.

1. Save only when the run covered the full effective managed scope without a user scope override. Otherwise say the shared report was not refreshed and offer to save under a distinct name such as `FinOps: Budget Status (<scope>)`. Skip when the user asks not to save.
2. Call ListReports; call GetReport and reuse the exact-name report's `reportId`, or create it if absent.
3. Load `finops-report-renderer`, its SKILL.md, exact `models.py`, and exact `render.py`. With `datetime.now(timezone.utc)`, call `build_budget_status_model(evaluate_budgets_result, ...)` with `partial`, warnings, and scope coverage; do not construct the model or HTML by hand. Run `write_report(model, "finops-budget-status.html")`.
4. SaveReport the static file with `allowedTools: []`, name `FinOps: Budget Status`, and the reused `reportId`. Return the saved report id and version. A report-tool failure never suppresses the analysis result.

## How Azure evaluates budgets (Microsoft guidance)

Use these when explaining a budget's status or recommending changes:

- **What a budget counts.** Budgets evaluate ActualCost, which includes one-time purchases such as
  reservations, savings plans and Marketplace. A purchase month can therefore breach a usage budget.
  To budget consumption only, recommend a budget filtered to `Publisher type = Azure` and
  `Charge type = Usage`. A human creates it in the portal; `finops-budget-editor` only plans
  scope-wide creates.
- **When data arrives.** Cost data refreshes every 8–24 hours, and budgets are evaluated about once
  every 24 hours. Alerts are not real time.
- **New subscriptions.** A budget on a new subscription may not have data for up to 48 hours. This
  is why a new budget can show `current_spend: 0`.
- **Management groups.** Budgets on management groups do not support subscriptions with more than
  one billing currency.
- **Thresholds and actions.** A budget supports up to five notification thresholds, each between
  0.01% and 1000% of the amount. Action groups work only on subscription and resource-group budgets.
  Recommend both an **actual** and a **forecasted** threshold.
- **Planning horizon.** Start with any budget and alert rather than none, base the amount on prior
  months, and add **quarterly or annual** budgets for spend that is planned on those horizons.
  `finops-budget-editor` plans all three time grains.

Sources: https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/tutorial-acm-create-budgets ·
https://learn.microsoft.com/en-us/cloud-computing/finops/framework/quantify/budgeting
Last reviewed: 2026-10-23
