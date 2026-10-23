---
name: finops-kpi-scorecard
description: Run the FinOps practice, read-only. Forecasts month-end and next-month spend with a confidence band, computes unit economics (cost per request/order/user), scores FinOps KPIs (allocation, tag and budget governance, forecast vs budget, commitment coverage/utilization, waste, anomaly impact, unit-cost trend) with a Crawl/Walk/Run indication, and routes every finding to its accountable owner tag. Uses the bundled scorecard.py with the runtime's sandbox Python tool over the results of the other FinOps skills. Use for the monthly FinOps review, "are we on track for budget", "what is our cost per transaction", "how mature is our FinOps", and "send each team its cleanup list".
---

## When to use this skill

Use it to **quantify business value** and **manage the FinOps practice** (FinOps Framework domains),
on top of what the other skills already found:

- **Forecasting**: "will we land on budget this month?", "what will next month cost?"
- **Unit economics**: cost per 1,000 requests, per order, per active user, per GB processed, and
  whether it is improving while spend grows.
- **KPIs and governance checks**: allocation %, untagged spend, tag hygiene, budget coverage of
  every managed scope, budgets over, forecast vs budget, commitment coverage and utilization,
  addressable waste %, anomaly impact %, unit-cost change; with pass/warn/fail and a maturity
  indication (crawl/walk/run).
- **Accountability**: one action list per owner/team from waste, rightsizing and anomaly findings.

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
| `rows` | prepared AmortizedCost UsageDetails, trailing **60 days** | forecast |

Every input is optional; a missing input makes its KPI `unknown` rather than failing. Say which
inputs were missing in the report.

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
from scorecard import daily_costs, forecast_spend, unit_economics, kpi_scorecard, route_to_owners

forecast = forecast_spend(daily_costs(prepared["included_rows"]),
                          budget_amount=<monthly budget for the scope, if one exists>)
unit = unit_economics(cost_30d, requests_30d, unit_name="request", per=1000,
                      previous_cost=cost_prev_30d, previous_units=requests_prev_30d)
card = kpi_scorecard(allocation=allocation, budgets=budgets, commitments=commitments,
                     waste=waste, rightsizing=rightsizing, anomalies=anomalies,
                     forecast=forecast, unit_costs=unit, managed_scopes=effective_scopes)
owners = route_to_owners(waste["findings"] + rightsizing, tags=resource_tags, source="optimization")
```

- `forecast_spend` fits a linear trend over the trailing window, adds day-of-week seasonality when
  at least 28 days exist, and drops the newest day as partial (Cost Management lags 1-2 days).
  It returns `month_end_forecast` with an 80% `month_end_low`/`month_end_high` band,
  `next_month_forecast`, `run_rate_30d`, `confidence` and, with a budget, `budget_status`
  (`on_track` / `at_risk` / `forecast_over`).
- `kpi_scorecard` defaults follow common FinOps targets (allocated ≥ 90%, untagged ≤ 5%,
  every managed scope budgeted, commitment utilization ≥ 95%, waste ≤ 5% of spend). Override with
  `targets={"commitment_coverage_pct": {"target": 60}}` when the customer has agreed different
  targets, and say so.
- `route_to_owners` checks the `owner`, `team`, `costCenter`, `service`, `app` tags (case-insensitive,
  first match wins). Unrouted findings are themselves a tag-governance action.

### Step 3 — Report

1. **Forecast**: month-to-date, month-end forecast with its band, budget status, next month,
   confidence and method. Never present the forecast as a single exact number.
2. **Scorecard**: one table row per KPI (value, target, status), the score and the maturity
   indication, and the ordered `actions` list (fail before warn).
3. **Governance checks**: `governance_checks` (tag policy and budget coverage) with
   `uncovered_budget_scopes` listed by name.
4. **Unit economics**: cost per unit, change vs previous period, and the interpretation. A rising
   total with a falling unit cost is healthy growth.
5. **Owner action lists**: one section per owner with total monthly savings and items; then the
   `unrouted` items as "needs an owner tag".

Report the excluded resources, missing inputs and warnings. Recommend only — read-only only.

## Operating the practice with SRE Agent

These patterns turn the scorecard into a running FinOps operating model. They use SRE Agent
features configured by the customer; this skill and `finops-investigator` stay read-only.

- **Monthly FinOps review**: the `monthly-finops-review` scheduled task runs this skill on the 1st of
  each month and posts the scorecard, forecast, month-over-month variance and owner action lists.
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
- KPI values are as good as their inputs: a scorecard built from a partial UsageDetails pull must be
  labeled partial.
