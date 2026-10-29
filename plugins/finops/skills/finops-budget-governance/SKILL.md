---
name: finops-budget-governance
description: Proactive budget governance for Azure, read-only. Reads native Azure Cost Management budgets and evaluates their amount, spend, forecast and configured notification thresholds. Computes a labeled run-rate projection only when spend and an active calendar window are known; otherwise reports unknown. Preserves scope, filters, period and currency, never adds overlapping budget envelopes, and distinguishes analytical warnings from configured notifications. Uses bundled budget.py with the runtime's sandbox Python tool. Use for "are we on budget", burn-rate / forecast checks, and month-end overrun early warning.
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
the skill computes a **linear run-rate** forecast only when current spend and an active calendar
window are known, and labels the source. It evaluates currencies separately without converting or
combining them. A configured management-group scope is queried directly
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
from datetime import date
from usage import load_finops_input, save_finops_input

# Reuse the unique run_id established during managed-scope resolution.
# Persist every collected scope's original budget objects, with id/properties intact.
save_finops_input("budgets", budgets, run_id=run_id)
raw_budgets = load_finops_input("budgets", run_id=run_id)
result = evaluate_budgets(raw_budgets, as_of=date.fromisoformat(evaluation_day))
save_finops_input("budget-evaluation", result, run_id=run_id)
```

These files are isolated under `/mnt/data/finops-runs/<run_id>/`. Reuse the same `run_id` across
collection, analysis and rendering; do not load another execution's inputs or use a shared scratch
directory. A failed budget query is a coverage gap, not an empty successful response.

`evaluate_budgets` handles all math and classification:

- **budgets**: per-budget evaluation ranked by severity. Each entry retains `id`, `scope`,
  `category`, `filter`, `time_grain`, `time_period`, `notifications`, and the resolved
  `window: {"start": "YYYY-MM-DD", "end_exclusive": "YYYY-MM-DD"}`. Monetary fields are
  `amount`, `current_spend`, `forecast_spend`, `currency`, `current_currency` and
  `forecast_currency`; percentages are `pct_used` and `pct_forecast`. `forecast_source` is
  `azure`, `run-rate` or `unavailable`.
- **status** buckets: `over_budget` (already ≥ amount) → `forecast_over` (projected ≥ amount) →
  `at_risk` (≥ 80% used or an Actual threshold breached) → `on_track`, plus **`unknown`** when
  the evidence cannot establish a classification. A known actual/forecast breach can still be
  reported when another measurement is unavailable. `unknown_reasons` explains missing spend,
  amount, currency, incompatible currency, unsupported category or unresolved window.
- **Missing is not zero.** Missing/non-numeric `currentSpend.amount` remains `current_spend: null`;
  it cannot generate a zero run-rate or an on-track result. Do not format null amounts as zero or
  invent a currency. A numeric zero is different from a missing observation; disclose any known
  freshness concern separately.
- **Configured notifications versus analysis:** `notifications` preserves the complete original
  metadata. `breached_notifications` contains only enabled, configured rules evaluated with their
  supported operator and type, labeled `source: "configured"`; it is not evidence that an alert
  was delivered. `analytical_thresholds` uses `source: "analysis_only"` for independent spend/
  forecast checks. Never present these analytical checks as configured customer alerts.
- **summary**: `total_amount`, `total_current`, `total_forecast`, `currency`, status counts
  (including `unknown`) and `totals_by_currency`. Totals require disjoint, unfiltered cost-budget
  scopes with the same resolved window/time grain and known currency. Parent plus child budgets,
  duplicate scope envelopes, different periods and ambiguous filters are **not additive**:
  affected totals are null and `aggregation_reason` explains why. Each currency bucket is subject
  to the same checks; splitting by currency alone does not establish a valid total. Missing
  measurements also leave the corresponding total null.
- **Window limitations:** run-rate estimates use actual calendar day counts for the helper's
  supported Monthly/Quarterly/Annual windows, anchored at `timePeriod.startDate`. Inactive,
  malformed or unresolved windows remain unknown. The helper does not infer a calendar window
  for `BillingMonth`/other billing-period grains.
- **gates**: the budgets in `over_budget` / `forecast_over` — the ones that warrant a **process gate**
  (a human decision before more is spent), each with a one-line reason.
- **no_budgets**: `True` when nothing is defined — report it and recommend creating a budget.

### Step 3 — Report

Produce a **budget status table** (name, scope, amount, spent + `pct_used`, forecast + `pct_forecast`
+ source, status), with a totals row **only where aggregation is valid** and any **gated** budgets
called out at the top as action items (with their reason). Otherwise explain why a portfolio total
is unavailable and show the individual envelopes. Preserve filters, period and currency in the
table or its context. Show unknown values as unknown, with `unknown_reasons`.
List **configured breached notification thresholds** separately from **analytical warnings**. If a
forecast is `run-rate` (not Azure's), say so — it is an estimate. If a budget's `current_spend` is `0`
on a newly created budget, note it may be an unsynced value rather than real zero spend. If **no
budgets are defined**, say that plainly and point to `finops-budget-editor` for a recommendation or
a deterministic create plan and human-run script. If every budget is `on_track`, say so in one line.

When passing results to `finops-kpi-scorecard`, preserve the whole evaluation, not only
`summary.total_amount`. A subscription forecast of 1,200 compared with a subscription budget of
1,000 is 120%; a separate 500 resource-group budget must not turn it into 80%. The scorecard matches
exact scope, monthly window, filter and currency; missing or duplicate matches produce unknown.

### Step 4 — Save the Live Report

After reporting, refresh the shared Live Report `FinOps: Budget Status` so the result is visible in Operations Hub. A scheduled task that already has ListReports/SaveReport steps owns this; follow it and do not save twice.

1. Save only when the run covered the full effective managed scope without a user scope override. Otherwise say the shared report was not refreshed and offer to save under a distinct name such as `FinOps: Budget Status (<scope>)`. Skip when the user asks not to save.
2. Call ListReports; call GetReport and reuse the exact-name report's `reportId`, or create it if absent.
3. Reload the exact same-run budget API objects and preserve them with managed-scope `make_budget_snapshot(budgets, managed_scopes=effective_scopes, run_id=run_id, code_version=<installed source SHA>, as_of=<evaluation YYYY-MM-DD>, retrieved_at=<timezone-qualified source retrieval timestamp>, partial=<retrieval incomplete>)`. Persist with `save_cost_snapshot(budget_snapshot)`. This is a budget-source record, not a usage ledger; budget windows stay in the original objects and billing-data freshness remains unknown.
4. Load `finops-report-renderer`, its SKILL.md, exact `models.py`, and exact `render.py`. Reload the same-run evaluation with `load_finops_input("budget-evaluation", run_id=run_id)`. With `datetime.now(timezone.utc)`, call `build_budget_status_model(evaluate_budgets_result, cost_context=budget_snapshot, ...)` with `partial`, warnings, and scope coverage; do not construct the model or HTML by hand. Run `write_report(model, f"/mnt/data/finops-runs/{run_id}/finops-budget-status.html")`. Confirm the Data provenance section includes the source hash, installed code version and run.
5. SaveReport the static file with `allowedTools: []`, name `FinOps: Budget Status`, and the reused `reportId`. Return the saved report id and version. A report-tool failure never suppresses the analysis result.

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
