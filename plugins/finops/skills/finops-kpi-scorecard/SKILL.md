---
name: finops-kpi-scorecard
description: Run the FinOps practice, read-only. Forecasts spend without inventing zero-cost days, compares only matched budget contexts, computes unit economics, and scores FinOps KPIs with health separated from evidence coverage. Deduplicates evidenced optimization actions and anomaly events; ambiguous overlap stays unknown. Legacy Crawl/Walk/Run bands are not an organizational maturity assessment. Uses bundled scorecard.py over sibling-skill results and routes findings to accountable owners. Use for monthly FinOps reviews, budget outlook, unit cost and owner action lists.
---

## When to use this skill

Use it to **quantify business value** and **manage the FinOps practice** (FinOps Framework domains),
on top of what the other skills already found:

- **Forecasting**: "will we land on budget this month?", "what will next month cost?"
- **Unit economics**: cost per 1,000 requests, per order, per active user, per GB processed, and
  whether it is improving while spend grows.
- **KPIs and governance checks**: allocation %, untagged spend, tag hygiene, budget coverage of
  every managed scope, budgets over, forecast vs budget, forecast accuracy, commitment coverage and
  utilization, addressable waste %, anomaly impact %, unit-cost change; with pass/warn/fail/unknown,
  **KPI health** and separate **evidence coverage**. Legacy crawl/walk/run health bands are not an
  organizational maturity assessment. Unscored benchmarks include anomaly count and impact when
  event overlap can be resolved, and the Azure Advisor Cost score.
- **Accountability**: one action list per owner/team from waste, rightsizing and anomaly findings.
- **Practice operations**: the next FinOps iteration's focus, assessment cadence, steering
  committee, onboarding, education and chargeback sequencing. See
  [microsoft-finops-practice.md](microsoft-finops-practice.md) and load it with
  `read_skill_file(skill_name="finops-kpi-scorecard", file_path="microsoft-finops-practice.md")`.

It is the analysis layer of the monthly FinOps review scheduled task. It never changes Azure.

## Required access

- **Cost Management Reader** (UsageDetails for the forecast and totals) and **Reader** on the
  managed scopes; the sibling skills list their own access.
- Optional: **Monitoring Reader** on Application Insights resources to read request counts for
  unit economics.
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

Pull UsageDetails independently for every effective scope with the canonical transport from
`finops-managed-scope`, run `prepare_usage_details(...)` (do not merge or scope-filter rows in
model reasoning), de-duplicate overlapping line items, and report included scopes, excluded
resources, unattributed cost rows, and any scope/API coverage gaps.

### Step 1 — Gather the inputs (sibling skills)

Run, or reuse from the same conversation turn, the results of:

| Input | Skill / function | Used for |
| --- | --- | --- |
| `allocation` | `finops-cost-allocation` → `allocate_costs(...)` | allocated %, untagged %, tag hygiene, totals |
| `budgets` | `finops-budget-governance` → `evaluate_budgets(...)` | budget coverage, overruns |
| `commitments` | `finops-commitment-advisor` → `analyze_commitments(...)` | coverage %, utilization % |
| `waste` | `finops-waste-finder` → `find_waste(...)` | addressable waste |
| `rightsizing` | `finops-rightsizing-advisor` → `recommend_rightsizing(...)` | addressable waste |
| `anomalies` | `finops-cost-anomaly-detection` → `detect_anomalies(...)` | anomaly impact |
| `rows` | prepared daily costs, trailing **60 days**, with explicit currency, scope and cost basis | forecast; use a budget-comparable series for budget checks, separate from an amortized optimization ledger |
| `prior_forecast`, `actual_spend` | `prior_forecast_inputs(daily, closed_month, record_text=...)` (see below) | forecast accuracy |
| `advisor_cost_score` | `GET .../providers/Microsoft.Advisor/advisorScore/Cost` (see the practice guide) | benchmark |

Every input is optional; a missing input makes its KPI `unknown` rather than failing. Say which
inputs were missing in the report.

Pass raw responses between steps through deterministic, run-isolated files
(see `finops-managed-scope` section 6), never shared scratch files or unbounded ToolOutputs scans.
Establish one unique `run_id` before collection and carry it through every load/save and report step:

```python
from usage import load_finops_input, save_finops_input

save_finops_input("advisor", advisor_response, run_id=run_id)
save_finops_input("inventory", inventory_response, run_id=run_id)
save_finops_input("budgets", budgets_response, run_id=run_id)
save_finops_input("tags", resource_tags, run_id=run_id)
save_finops_input("forecast-prepared", forecast_prepared, run_id=run_id)
resource_tags = load_finops_input("tags", run_id=run_id)
```

Files live under `/mnt/data/finops-runs/<run_id>/`. A missing input must remain a missing source;
never load a previous run's file as fallback. UsageDetails page reconstruction must retain the
same-run `after_timestamp=run_start`, exact thread ID and request URL prefix controls from
`finops-managed-scope`.

Match cost basis as well as currency, scope, filters and period when comparing a forecast with a
budget. Use a separate budget-comparable cost series when the optimization ledger has a different
basis. The context matcher checks the fields documented below; it does not prove that the caller
selected the correct underlying ledger or that collection was complete.

The forecast and month-over-month inputs need earlier months. For MOSP/MCA subscriptions the
UsageDetails date filter is ignored and only the current billing period is returned
(`finops-managed-scope` section 5): do not retry other filters; label the forecast and
month-over-month as partial with the requested vs obtained date range.

For **unit economics**, take business volume from the customer (orders, active users, GB processed)
or, for web workloads, from Application Insights request counts in a managed resource group:

```bash
az monitor app-insights metrics show --app <APP_INSIGHTS_NAME> \
  --resource-group <EFFECTIVE_RESOURCE_GROUP> --metrics requests/count \
  --aggregation sum --interval P30D --offset 30D -o json
```

Use the same period and the same scope for cost and units (e.g. the cost of the resource groups
behind that app). Never invent a unit metric; if none is available, leave `unit_costs` out.

### Step 2 — Compute (bundled scorecard.py, in-sandbox)

```
read_skill_file(skill_name="finops-kpi-scorecard", file_path="scorecard.py")
```

```python
from datetime import date, timedelta
from scorecard import (daily_costs, forecast_spend, unit_economics, kpi_scorecard,
                       route_to_owners, prior_forecast_inputs)
from usage import load_finops_input, save_finops_input

prepared = load_finops_input("forecast-prepared", run_id=run_id)
budgets = load_finops_input("budget-evaluation", default=None, run_id=run_id)
commitments = load_finops_input("commitment-analysis", default=None, run_id=run_id)
daily = daily_costs(prepared["included_rows"], currency=prepared["currency"])

# forecast_as_of is the verified last complete observation day, not blindly today's date.
last_complete = date.fromisoformat(forecast_as_of)
month_start = last_complete.replace(day=1)
next_start = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
context = {
    "scopes": forecast_scopes,  # exact, canonical, nonoverlapping scopes of this cost series
    "currency": prepared["currency"],
    "cost_basis": prepared["cost_metric"],  # preserve the actual series basis; never relabel it
    "filter": forecast_filter,  # {} only when this series is genuinely unfiltered
    "period_start": month_start.isoformat(),
    "period_end_exclusive": next_start.isoformat(),
}
forecast = forecast_spend(
    daily, as_of=last_complete, assume_last_partial=False,
    context=context, missing_days_are_zero=False,
)
# Let the scorecard select exact budget envelopes; never pass summary.total_amount.
accuracy = prior_forecast_inputs(daily, closed_month, record_text=previous_review_content)
unit = unit_economics(cost_30d, requests_30d, unit_name="request", per=1000,
                      previous_cost=cost_prev_30d, previous_units=requests_prev_30d)
card = kpi_scorecard(allocation=allocation, budgets=budgets, commitments=commitments,
                     waste=waste, rightsizing=rightsizing, anomalies=anomalies,
                     forecast=forecast, unit_costs=unit, managed_scopes=effective_scopes,
                     prior_forecast=accuracy["prior_forecast"],
                     actual_spend=accuracy["actual_spend"],
                     prior_forecast_source=accuracy["source"],
                     advisor_cost_score=advisor_cost_score,
                     period_days=days_of_usage_analyzed)  # anomaly KPI daily-spend denominator
# Use adjudicated, nonoverlapping USD actions for any additive owner savings total.
# If that selection is unavailable, route items without claiming a realizable aggregate.
owners = route_to_owners(supported_nonoverlapping_usd_actions,
                          tags=resource_tags, source="optimization")
save_finops_input("forecast", forecast, run_id=run_id)
save_finops_input("forecast-accuracy", accuracy, run_id=run_id)
save_finops_input("scorecard", card, run_id=run_id)
save_finops_input("owner-routing", owners, run_id=run_id)
```

For a separately selected scalar monthly budget, supply its independently established context:

```python
forecast = forecast_spend(
    daily, as_of=last_complete, assume_last_partial=False,
    context=context, budget_amount=selected_budget_amount,
    budget_context=selected_budget_context, missing_days_are_zero=False,
)
save_finops_input("forecast", forecast, run_id=run_id)
```

`selected_budget_context` uses the same keys as `context` and must describe the budget, not be
copied from the forecast just to force a match. Without `budget_context`, scalar-budget comparisons
are suppressed with `budget_comparison.reason`; no ratio or budget status is returned. Legacy
caller-asserted comparisons are not scored.

- `forecast_spend` fits a linear trend over the trailing window, adds day-of-week seasonality when
  at least 28 observations exist, and by default treats the newest day as potentially partial.
  An explicit `as_of` selects the anchor instead. It returns `month_end_forecast` with
  `month_end_low`/`month_end_high`, `next_month_forecast`, `run_rate_30d`, `confidence` and
  `context`. These are model estimates, not guaranteed ranges.
- **Missing dates stay missing.** `missing_days_are_zero=False` fits only observed history,
  including valid explicit zero values. `history_days` is the count actually fitted;
  `missing_history_days`, `history_complete`, `missing_mtd_days`, `data_complete`,
  `missing_day_policy` and `warnings` disclose gaps. Incomplete month-to-date data sets
  `month_to_date`, `month_end_forecast`, `month_end_low` and `month_end_high` to null while
  retaining `observed_month_to_date` and the low-confidence trend/next-month estimate.
  `available: true` means a model was fitted, not that every output or the export is complete.
- Set `missing_days_are_zero=True` **only** after independently verifying a complete export
  in which omitted dates genuinely mean zero spend. This is recorded as
  `missing_day_policy: "explicit_zero"`; a late, failed, truncated or short billing-data pull
  does not satisfy that requirement. `daily_costs` rejects mixed known/unknown currencies;
  group by currency or supply a verified denomination, never convert implicitly.
- **Forecast/budget matching:** `context` requires `scope` or `scopes`, `currency`, `cost_basis`, `filter`,
  `period_start` and `period_end_exclusive`. The helper fills the forecast month's boundaries
  and rejects contradictory supplied boundaries. `kpi_scorecard` selects one exact Monthly cost
  budget per nonoverlapping forecast scope with matching currency, cost basis, filter and window.
  `evaluate_budgets` identifies Cost budgets as `ActualCost`; missing, unknown or mismatched bases
  suppress the KPI, even if current totals happen to be similar. A parent
  budget is not the denominator for a child-only forecast; parent and child budgets are not
  added. Duplicate matches or missing context yield an unknown KPI with `detail.reason`.
  A valid comparison exposes `detail.budget_amount`, `matched_scopes` and `matched_budget_ids`.
  Missing month-end totals or `data_complete: false` also suppress the comparison.
- `kpi_scorecard` defaults follow common FinOps targets (allocated ≥ 90%, untagged ≤ 5%,
  every managed scope budgeted, commitment utilization ≥ 95%, waste ≤ 5% of spend). Override with
  `targets={"commitment_coverage_pct": {"target": 60}}` when the customer has agreed different
  targets, and say so. The commitment coverage and utilization targets come from FinOps Foundation
  practice; Microsoft's documentation does not publish numeric targets for them.
- `forecast_accuracy_pct` is `|prior_forecast - actual| / actual`. It passes below 12% and warns up
  to 20%, following Microsoft's forecasting guidance (under 12% is optimized, 12–20% acceptable).
  Get the inputs with `prior_forecast_inputs(daily, "<closed YYYY-MM>", record_text=...)`:
  - **Recorded** (preferred): pass the previous `FinOps: Monthly Review` content from GetReport.
    Each review's renderer writes one line,
    `finops-forecast-record:v1 month=<next month> forecast=<USD> as_of=<date>`, with its forecast
    for the following month; `parse_forecast_record` reads it back.
  - **Reconstructed** (fallback): with no record, the forecast is rebuilt by running
    `forecast_spend` on the days before the closed month. Say that it tests the method, not the
    forecast published at the time.
  - `actual_spend` is null for an incomplete closed month, even if a forecast record exists.
    Preserve `observed_actual_spend`, `missing_actual_days` and `actual_complete` for disclosure.
    The same explicit-zero attestation is available through
    `prior_forecast_inputs(..., missing_days_are_zero=True)` only for verified complete exports.
  - Pass `prior_forecast_source=inputs["source"]`; the KPI is `unknown` when the forecast or
    complete actual is unavailable. Do not compare a recorded USD forecast with another currency.
- **Health is not evidence coverage:** `kpi_health_pct` is the pass rate among known KPIs;
  `score_pct` remains its compatibility alias. `evidence_coverage_pct` is the share of all KPIs
  with known results. Two passing KPIs and ten unknowns therefore mean 100% observed health but
  only 16.7% evidence coverage. Show both percentages and the status counts.
  `maturity` is suppressed to `unknown` below 80% evidence coverage. Above that threshold it is
  only a legacy KPI-health band; `organizational_maturity` remains `unknown` and
  `maturity_reason` explains the limitation. Never call this an organizational Crawl/Walk/Run
  assessment or treat missing inputs as passes.
- **Commitment coverage** uses the commitment advisor's conservative eligible denominator.
  Retain its full `coverage` detail and `eligibility_basis`; do not reinterpret it as all-service
  coverage or omit unknown-eligibility amounts.
- **Optimization overlap:** the waste KPI uses finding-level resource/action or recommendation
  identity, removes repeated actions and treats stop/delete versus resize options as nonadditive.
  Distinct supported actions are not automatically merged merely because their resource matches.
  Headline-only waste cannot be added to rightsizing without evidence resolving overlap.
  Generic savings need explicit USD currency to join USD spend; unsupported/candidate/
  contradicted rightsizing rows do not enter the supported numerator. Preserve
  `duplicates_removed`, `alternative_actions_not_added`, `unsupported_findings_excluded` and
  any `detail.reason`.
- **Anomaly overlap:** explicit `event_id`/`eventId` plus event dates/windows can identify the
  same event across dimensions; repeated identical observations are deduplicated. Equal
  monetary amounts alone do not prove identity. Native cross-dimensional observations without
  shared event provenance leave the KPI and event-count/impact benchmarks unknown, with
  `observed_impact_lower_bound_usd` / `observed_impact_upper_bound_usd` diagnostics. Do not call
  raw detection rows distinct incidents.
- `next_iteration_focus` lists up to 3 capabilities with failing, then warning, KPIs. Present them
  as candidates for the next FinOps iteration (see the practice guide).
- `route_to_owners` checks the `owner`, `team`, `costCenter`, `service`, `app` tags (case-insensitive,
  first match wins). Unrouted findings are themselves a tag-governance action.

### Step 3 — Report

1. **Forecast**: observed subtotal versus complete month-to-date, month-end forecast with its band
   when available, matched budget status, next month, currency, scope, period, confidence and
   method. Explain missing days and null totals; never replace them with zero or present the
   forecast as a single exact number.
2. **Scorecard**: one table row per KPI (value, target, status), **observed KPI health and evidence
   coverage side by side**, unknown reasons, the ordered `actions` list (fail before warn), `benchmarks`, and
   `next_iteration_focus`.
3. **Governance checks**: `governance_checks` (tag policy and budget coverage) with
   `uncovered_budget_scopes` listed by name.
4. **Unit economics**: cost per unit, change vs previous period, and the interpretation. A rising
   total with a falling unit cost is healthy growth.
5. **Owner action lists**: one section per owner and the `unrouted` items as "needs an owner tag".
   `route_to_owners` groups inputs; it is not the scorecard's deduplication or currency-validation
   engine. Route only same-currency, supported, nonoverlapping actions before presenting a
   realizable monthly savings total; keep candidate alternatives and anomaly impacts separate.

Report the excluded resources, missing inputs and warnings. Recommend only — read-only only.

### Step 4 — Save the Live Report

After reporting, refresh the shared Live Report `FinOps: Monthly Review` so the result is visible in Operations Hub. A scheduled task that already has ListReports/SaveReport steps owns this; follow it and do not save twice.

1. Save only when the run covered the full effective managed scope without a user scope override. Otherwise say the shared report was not refreshed and offer to save under a distinct name such as `FinOps: Monthly Review (<scope>)`. Skip when the user asks not to save.
2. Call ListReports; call GetReport and reuse the exact-name report's `reportId`, or create it if absent.
3. Load `finops-report-renderer`, its SKILL.md, exact `models.py`, and exact `render.py`. Reload results with `load_finops_input("scorecard", run_id=run_id)` and `load_finops_input("forecast", run_id=run_id)`. With `datetime.now(timezone.utc)`, call `build_monthly_review_model({"month": ..., "scorecard": kpi_scorecard_result, "forecast": ..., "month_over_month": ..., "routing": ..., "unit_economics": ...}, cost_context=snapshot, ...)` with the immutable same-run source snapshot, `partial`, warnings, and scope coverage; preserve nested KPI details without serializing them to strings. Do not construct the model or HTML by hand. Run `write_report(model, f"/mnt/data/finops-runs/{run_id}/finops-monthly-review.html")`.
4. SaveReport the static file with `allowedTools: []`, name `FinOps: Monthly Review`, and the reused `reportId`. Return the saved report id and version. A report-tool failure never suppresses the analysis result.

## Operating the practice with SRE Agent

These patterns turn the scorecard into a running FinOps operating model. They use SRE Agent
features configured by the customer; this skill and `finops-investigator` stay read-only.

- **Monthly FinOps review**: the `monthly-finops-review` scheduled task runs this skill on the 1st of
  each month, saves the `FinOps: Monthly Review` Live Report, and posts the scorecard, forecast,
  month-over-month variance and owner action lists.
- **Alert-triggered investigations**: send Cost Management anomaly alerts, budget alerts (actual and
  forecast thresholds) and Azure Monitor alerts to an action group that the SRE Agent receives as
  incidents. Create an incident response plan that matches them (for example title contains
  "budget" or "anomaly") and hands them to the `finops-investigator` subagent with the prompt
  "investigate this cost alert: run finops-cost-anomaly-detection for the alert scope and window,
  explain the drivers and route the findings to owners".
- **Owner routing**: deliver each owner's list from `route_to_owners` through the customer's channel
  (email from the scheduled task, Teams, or a ticket per owner via an incident platform connector).
- **IaC remediation**: the fix for waste and rightsizing belongs in the customer's infrastructure
  code, not in a portal click. Connect the IaC repository to SRE Agent as a code source and ask the
  base agent (not `finops-investigator`) to open a pull request with the change, for example "open a
  PR in <repo> that changes the SKU of <resource> to <target> per the rightsizing finding". Owners
  review and merge through their normal change process.
- **Governance follow-up**: for failing tag KPIs, recommend Azure Policy `Require a tag` or
  `Inherit a tag from the resource group` assignments; for uncovered scopes, `finops-budget-editor`
  drafts the budget after explicit confirmation.

## Notes

- Forecasts are statistical, not commitments; with less than 14 days of history confidence is low.
- Exclude credits from a usage forecast with `daily_costs(rows, include_credits=False)` when the
  customer wants the usage trend rather than the invoice.
- Microsoft's forecasting guidance:
  - Exclude one-time purchases (`ChargeType=Purchase`) from the usage trend with
    `daily_costs(rows, exclude_purchases=True)`, and forecast them separately.
  - Use a look-back period at least as long as the forecast horizon. The 60-day default covers the
    month-end and next-month horizons; widen `history_days` for longer horizons.
  - Review forecasts monthly or quarterly.

  Source: https://learn.microsoft.com/en-us/cloud-computing/finops/framework/quantify/forecasting.
  Last reviewed: 2026-10-23.
- Unit economics: cost that cannot be mapped to a unit is shared overhead. Report it as overhead;
  do not spread it silently.
  Source: https://learn.microsoft.com/en-us/cloud-computing/finops/framework/quantify/unit-economics.
  Last reviewed: 2026-10-23.
- KPI values are as good as their inputs: a scorecard built from a partial UsageDetails pull must be
  labeled partial.
