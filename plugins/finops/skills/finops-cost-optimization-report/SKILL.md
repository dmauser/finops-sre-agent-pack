---
name: finops-cost-optimization-report
description: Executive FinOps rollup for Azure — one report bundling cost anomalies, rightsizing and waste-cleanup savings, rate optimization (reservations, savings plans, Hybrid Benefit), Azure SRE Agent (AAU) spend, budget status, and governance (policy) findings. It orchestrates the existing read-only skills (finops-cost-anomaly-detection, finops-rightsizing-advisor, finops-waste-finder, finops-commitment-advisor, finops-sre-agent, finops-cost-allocation, finops-budget-governance), then rolls their outputs up with the bundled summarize.py using the runtime's sandbox Python tool into an executive headline, a single dollar-ranked priorities list, and per-section detail. Read-only — no writes; "policy findings" reuse existing tag-hygiene and budget-gate signals rather than a new data source. Use for a periodic FinOps review, a cost-optimization summary, or an executive "where should we act" briefing.
---

## When to use this skill

Use it when the user wants **one consolidated FinOps review** rather than a single analysis —
"give me the cost-optimization report", "what are our top savings/risk items this week", an executive
"where should we act" briefing, or a recurring FinOps summary. It bundles the pack's read-only
analyses into one prioritized picture: what we can save by using less (rightsizing, waste), what we
can save by paying less (commitments, Hybrid Benefit), what is spiking, what is about to blow a
budget, and what spend is ungoverned.

For a single dimension use the underlying skill directly: cost spikes →
`finops-cost-anomaly-detection`; idle/oversized resources → `finops-rightsizing-advisor`; orphaned
network/snapshot/logging waste → `finops-waste-finder`; reservations, savings plans, and licenses →
`finops-commitment-advisor`; Azure SRE Agent cost → `finops-sre-agent`; who owns the spend → `finops-cost-allocation`; budget status →
`finops-budget-governance`; right-sizing a budget → `finops-budget-editor`.

## Required access

- **Cost Management Reader** (or **Reader**) on the subscription — the same read-only access the
  underlying skills use. No write role is needed or used. Commitment inventory additionally needs
  **Reservations Reader** / **Savings plan Reader**; without them that subsection is reported as a
  coverage gap and the rest of the report still ships.
- Read-only `az` (`RunAzCliReadCommands`) and sandbox Python: use `ExecutePythonCode` when
  available, otherwise `RunInTerminal` with `python3`. **No POST/PUT or write APIs are called.**
- This report is purely a **rollup**: "policy findings" are the existing tag-hygiene / untagged-spend
  and budget-gate signals — it introduces no new data source of its own.

## Scope

Bundles the existing analyses at the requested scope (subscription or resource group). Each
underlying skill pulls and shapes its own data; this skill only aggregates their outputs, so it
inherits their scope and caveats (e.g. `currentSpend` can lag on a fresh budget; a `run-rate` budget
forecast is an estimate; rightsizing rows marked unvalidated need verification). Any analysis that
can't run or returns nothing becomes an **empty section**, not a failure — a partial report still
ships. Cross-currency portfolios are out of scope until needed.

## Procedure

### Step 0 — Resolve the managed boundary

First load `finops-managed-scope` and follow its `scope.py` procedure to dynamically GET and validate
the current agent `managedResources`; never reuse cached scope. Pass one immutable effective-scope set
for this request to every underlying skill. Scheduled runs are strict/fail-closed with no override.
An interactive named outside-scope target requires disclosure and explicit confirmation in a
subsequent turn before any broader query. Broad RBAC never silently expands scope.

For each effective scope or expanded descendant, every underlying analysis must query and enforce its own
defense-in-depth filtering. Shared UsageDetails may be reused only when it was independently paginated
per effective scope, de-duplicated, boundary-filtered, and annotated with per-scope completeness.
The report must disclose included scopes, excluded/unattributed data, unsupported scopes, and partial
coverage alongside the existing analysis caveats.

### Step 1 — Run the underlying analyses (read-only)

Run each existing skill and keep its structured output. Follow each skill's own SKILL.md for the data
pull and call — do **not** re-implement their logic here:

1. **finops-cost-anomaly-detection** → `detect_anomalies(line_items)` → a list of anomalies.
2. **finops-rightsizing-advisor** → `recommend_rightsizing(...)` → a list of findings.
3. **finops-waste-finder** → `find_waste(resources, costs=...)` → a waste dict.
4. **finops-commitment-advisor** → `analyze_commitments(...)` → a rate-optimization dict.
5. **finops-cost-allocation** → `allocate_costs(costs, tags, dimension=...)` → an allocation dict.
6. **finops-budget-governance** → `evaluate_budgets(budgets)` → a budget-status dict.
7. **finops-sre-agent** → `analyze_sre_agent_costs(sre_lines, agents, daily_usage, monthly_usage)` →
   an SRE Agent spend dict. Take `sre_lines` from the shared pull's included rows with
   `is_sre_agent_line`. The agent inventory and per-agent
   `dailyUsageDetails` / `usages` GETs follow that skill's Steps 2-3. If they fail, pass only
   the line items (the always-on/active split is then estimated).

The heavy/`413`-prone step is the shared **cost line-item pull** used by anomaly, allocation,
rightsizing, waste, and commitment coverage — pull it **once** (AmortizedCost, trailing 30 days,
projected with the union of fields every core needs: `pricingModel`, `meterCategory`, `meterName`, `consumedService`, `quantity`,
`unitOfMeasure`, `tags`) and
feed every core. If any one analysis fails or is out of
scope, keep going with the rest; `summarize.py` treats a missing input as an empty section. Do
**not** use `az rest --method post` or the Cost Management Query API — POST is blocked as a write.

**Execution order (avoids the three common data-quality gaps).** A weekly rollup makes many calls;
run them in this order so the expensive evidence is never the part that gets cut:

1. **Cost pull to completion.** Follow every UsageDetails `nextLink` until null and run
   `prepare_usage_details` **before** any other analysis call (see `finops-managed-scope`, "Finish
   the chain first"). Do not echo pages into chat. A `partial` result is acceptable only after the
   413 fallbacks fail — never because the run moved on.
2. **Utilization metrics for the top candidates.** Rank rightsizing/waste compute candidates
   (VMs, Container Apps, session pools) by monthly cost from step 1 and pull Azure Monitor metrics
   (`Percentage CPU`; `Requests`; `SessionApiRequestCount`) for at least the **top 15 by cost**
   before ranking. Only the remainder beyond that bound may be left unvalidated; say how many.
   Metrics need only **Reader** (or Monitoring Reader) on the managed scope.
3. **Commitment inventory.** Try both tenant-level GETs; on 403 pass
   `inventory_unavailable=[...]` to `analyze_commitments` and keep coverage/unused cost from step 1.
4. Advisor, Resource Graph, budgets, then the cores and `summarize.py`.

In the report's data-quality notes, state each gap with its fix: partial cost → pages retrieved and
`remaining_next_link`; unvalidated rightsizing → count beyond the metric bound or metric errors;
commitment inventory → the Reservations Reader / Savings plan Reader grant.

**Shared cost pull — exact shape.** One `az rest` GET per effective subscription (URL-encode `$` as
`%24`; never use `||` in `--query`, project candidate fields separately):

```bash
az rest --method get \
  --url "https://management.azure.com/subscriptions/<SUB_ID>/providers/Microsoft.Consumption/usageDetails?api-version=2023-05-01&metric=AmortizedCost&%24top=1000" \
  --query "{value: value[].{id: id, date: properties.date, cost: properties.costInUSD, costInBillingCurrency: properties.costInBillingCurrency, billingCost: properties.cost, billingCurrencyCode: properties.billingCurrencyCode, billingCurrency: properties.billingCurrency, chargeType: properties.chargeType, pricingModel: properties.pricingModel, meterCategory: properties.meterCategory, meterName: properties.meterName, consumedService: properties.consumedService, quantity: properties.quantity, unitOfMeasure: properties.unitOfMeasure, subscriptionId: properties.subscriptionId, resourceGroup: properties.resourceGroup, instanceName: properties.instanceName, resourceId: properties.resourceId, tags: tags}, nextLink: nextLink}" \
  -o json
```

Then derive every core's input in the sandbox from the same `prepared = prepare_usage_details(...)`
(the helpers resolve `instanceName` → `resourceId` and the billing-currency fallbacks):

```python
from usage import costs_by_resource, ingestion_gb_per_day
rows = prepared["included_rows"]
costs = costs_by_resource(rows)                    # {resourceId: cost} for rightsizing/waste/allocation/AHB
ingestion = ingestion_gb_per_day(rows, days=30)    # {workspaceId: GB/day}; Log Analytics + GB units only
```

`ingestion_gb_per_day` sums `quantity` for rows with `meterCategory == "Log Analytics"` whose
`unitOfMeasure` contains `GB` (retention `GB/Month` excluded; `100 GB` units scaled), keyed by
workspace id, divided by the window length. Pass it to `find_waste(..., ingestion_gb_per_day=ingestion)`.

**Allocation dimension.** Run `allocate_costs(costs, tags, dimension="team")` first. Then pick the
`recommended_coverage` entry with `present == True` and the highest `pct` (cost share); if it is not
`team`, rerun `allocate_costs` with that key. Name the chosen dimension and its coverage in the
report. If no recommended key is present, keep `team` (the report then shows 100% unallocated,
which is the correct governance finding).

**Commitment inventory 403.** Never fail the report. Prefer calling
`analyze_commitments(..., inventory_unavailable=[...])` so coverage, unused-commitment cost, and
purchase recommendations still populate. If the commitment skill cannot run at all, pass this
empty-but-explicit result to `summarize_optimization(commitments=...)` and continue:

```python
commitments_result = {
    "headline": {"potential_monthly_savings": None, "best_family": None,
                 "coverage_pct": None, "unused_commitment_cost": None,
                 "commitment_count": 0,
                 "inventory_unavailable": ["reservation", "savings_plan"]},
    "purchases": [], "underutilized": [], "expiring": [], "hybrid_benefit": [],
    "currency": prepared["currency"],
    "warnings": ["Commitment inventory unavailable — grant Reservations Reader / Savings plan Reader."],
}
```

### Step 2 — Roll up (bundled summarize.py, in-sandbox)

Read the module and run it — do **not** re-implement the rollup in the prompt:

```
read_skill_file(skill_name="finops-cost-optimization-report", file_path="summarize.py")
```

```python
from summarize import summarize_optimization
report = summarize_optimization(
    anomalies=anomalies_list,       # from detect_anomalies (or None)
    rightsizing=rightsizing_list,   # from recommend_rightsizing (or None)
    allocation=allocation_dict,     # from allocate_costs (or None)
    budgets=budgets_dict,           # from evaluate_budgets (or None)
    waste=waste_dict,               # from find_waste (or None)
    commitments=commitments_dict,   # from analyze_commitments (or None)
    sre_agents=sre_agent_dict,      # from analyze_sre_agent_costs (or None)
)
```

`summarize_optimization` produces the executive rollup:

- **headline**: `total_monthly_spend`, `potential_monthly_savings` (rightsizing only),
  `anomaly_count`, `top_anomaly_impact_usd`, budgets `over` / `forecast_over` / `at_risk` counts,
  `untagged_usd`, `unallocated_pct`. When waste is supplied: `waste_monthly_savings` and
  `usage_optimization_monthly_savings` (rightsizing + waste, de-duplicated by resource id). When
  commitments are supplied: `rate_optimization_monthly_savings`, `commitment_coverage_pct`,
  `unused_commitment_cost`. When SRE Agent spend is supplied: `sre_agent_spend_usd`,
  `sre_agent_always_on_pct`, and `sre_agent_monthly_savings` (idle/stopped agents or
  consolidation, whichever is larger, never both; kept separate from usage optimization).
- **priorities**: ONE dollar-ranked action list across all sources. Each item carries an
  **`impact_type`** — `savings` (rightsizing, waste, commitment purchase), `unused commitment`,
  `risk` (expiring commitment), `overrun` (budget gate), `spike` (anomaly), `governance`
  (untagged spend / tag hygiene), or `upcoming cost` (SRE Agent trial waiver ending). SRE Agent
  items use category `sre_agent` — so amounts are never silently conflated. Only the headline
  commitment family (reservation **or** savings plan) is ranked; the other is an alternative.
  Unknown-dollar items are kept and sort last.
- **rightsizing / waste / commitments / sre_agents / anomalies / budgets / governance**: the per-section detail
  for the dashboard.

### Step 3 — Report

Produce an **executive summary** dashboard: the **headline** metrics up top; the **top priorities**
table next (rank, category, impact, action) — this is the "where to act first" list; then a section
per analysis (rightsizing, waste cleanup, rate optimization, SRE Agent spend, anomalies, budget
status, governance/policy findings). Label each priority by its `impact_type` and **never sum savings,
overruns, spikes, and governance exposure into one number** — they are different kinds of dollars.
Report usage optimization (rightsizing + waste) and rate optimization (commitments) as separate
numbers: committing to capacity that is about to be removed double-counts the same dollars, so
recommend acting on usage first. Carry through the underlying caveats: mark `run-rate` budget
forecasts and unvalidated rightsizing rows as estimates/"verify first", note that commitment
recommendations assume look-back usage continues and Hybrid Benefit needs license entitlement, and
note an unsynced `currentSpend` where relevant. If an analysis returned nothing, show its section as
a clean empty-state. If every section is clean, say so in one line.

### Step 4 — Save the Live Report

After reporting, refresh the shared Live Report `FinOps: Cost Optimization` so the result is visible in Operations Hub. A scheduled task that already has ListReports/SaveReport steps owns this; follow it and do not save twice.

1. Save only when the run covered the full effective managed scope without a user scope override. Otherwise say the shared report was not refreshed and offer to save under a distinct name such as `FinOps: Cost Optimization (<scope>)`. Skip when the user asks not to save.
2. Call ListReports; call GetReport and reuse the exact-name report's `reportId`, or create it if absent.
3. Load `finops-report-renderer`, its SKILL.md, exact `models.py`, and exact `render.py`. With `datetime.now(timezone.utc)`, call `build_cost_optimization_model(summarize_result, ...)` with `partial`, warnings, and scope coverage; do not construct the model or HTML by hand. Run `write_report(model, "finops-cost-optimization.html")`.
4. SaveReport the static file with `allowedTools: []`, name `FinOps: Cost Optimization`, and the reused `reportId`. Return the saved report id and version. A report-tool failure never suppresses the analysis result.
