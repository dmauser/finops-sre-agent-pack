# Running the FinOps practice — Microsoft guidance

Reference for `finops-kpi-scorecard` and the Monthly Review. It covers the parts of the Microsoft
FinOps documentation that are about *operating* the practice rather than analyzing cost data.
The pack stays read-only: every item here is something the agent **recommends** to the FinOps
team; it never configures Azure.

## FinOps iterations

- Work in iterations. In each one, pick a small set of capabilities (Microsoft suggests 3–5) and set
  1–3 measurable goals for each, then review progress at the end of the iteration.
- Assess each capability on five dimensions: **knowledge**, **process**, **metrics**, **adoption**
  and **automation**. Low scores on a dimension suggest the type of goal to set next.
- Teams that are new to FinOps can start with the "understand" capabilities (data ingestion,
  allocation, reporting, anomalies) before moving on to optimization and quantifying value.
- The scorecard's `next_iteration_focus` lists up to three capabilities with failing, then warning,
  KPIs. Use it as a starting point for choosing the next iteration's capabilities, not as the final
  choice. The business priorities still decide.

Source: https://learn.microsoft.com/en-us/cloud-computing/finops/conduct-iteration
Last reviewed: 2026-10-23

## Assessment cadence

- Microsoft's FinOps Review assessment is a questionnaire of about 35 questions that scores
  maturity across the FinOps Framework. Retake it every 1–3 months to track progress.
- The pack's crawl/walk/run indication comes from KPI pass rates. It complements the assessment;
  it does not replace it. Point the team to the assessment when they ask how mature they are.

Source: https://learn.microsoft.com/en-us/cloud-computing/finops/framework/manage/assessment
Last reviewed: 2026-10-23

## Practice operations

- Start with a virtual steering committee that meets weekly or monthly. It should include
  stakeholders from finance, engineering, business and leadership. Use the Monthly Review Live
  Report as its standing input.
- Define a RACI for FinOps activities, for example who owns tagging, budgets, commitment purchases
  and anomaly triage.
- Microsoft notes that organizations should consider a dedicated FinOps team as cloud spend
  approaches about $1M per year.
- Report the agreed KPIs in every recurring review and track the actions to closure. The scorecard
  `actions` and `route_to_owners` lists give you this.

Source: https://learn.microsoft.com/en-us/cloud-computing/finops/framework/manage/operations
Last reviewed: 2026-10-23

## Onboarding workloads

Before a new workload lands in a managed scope, check:

1. **Technical feasibility**: the architecture has been reviewed against the Well-Architected cost
   optimization pillar.
2. **Financial feasibility**: the workload's estimate fits the forecast and the remaining budget
   capacity of its scope. `forecast_spend(..., budget_amount=...)` shows the headroom.
3. **Allocation readiness**: the required tags (owner, cost center, environment) and a budget exist
   before deployment.
4. **Stakeholder introduction**: new workload owners get a short introduction to FinOps and to their
   Live Reports.

Source: https://learn.microsoft.com/en-us/cloud-computing/finops/framework/manage/onboarding
Last reviewed: 2026-10-23

## Education and enablement

- Keep training material in one central place, and set up a support channel where people can ask
  FinOps questions.
- Write content for nontechnical roles (finance, procurement, leadership) as well as engineers.
- The `finops-investigator` subagent can act as a first-line answerer in that channel.

Source: https://learn.microsoft.com/en-us/cloud-computing/finops/framework/manage/education
Last reviewed: 2026-10-23

## Invoicing and chargeback

- Introduce this in stages: **showback** (teams see their costs), then **allocation** (costs are
  assigned to teams reliably), then **chargeback** (costs are billed to teams). Do not start
  chargeback until allocation is trusted.
- If teams keep manual forecasts, give them separate budgets so variance is visible.
- Reconcile with the invoice using ActualCost; allocate using AmortizedCost (see
  `finops-cost-allocation`).

Source: https://learn.microsoft.com/en-us/cloud-computing/finops/framework/manage/invoicing-chargeback
Last reviewed: 2026-10-23

## Planning and estimating

These are out of scope for the agent at runtime, but recommend them when asked:

- Use the **Azure Pricing Calculator** to estimate new builds.
- Use an **Azure Migrate business case** for on-premises migrations.
- Use the Well-Architected **cost model** to tie cost to business value.
- Compare an estimate with actual spend after the first full month. `finops-cost-allocation` can
  give that month's cost for the workload's tags.

Sources: https://learn.microsoft.com/en-us/cloud-computing/finops/framework/quantify/planning ·
https://learn.microsoft.com/en-us/azure/well-architected/cost-optimization/cost-model
Last reviewed: 2026-10-23

## Benchmarking

- Microsoft recommends the Azure Advisor score, cost category, as a baseline benchmark, together
  with Well-Architected reviews.
- Pass `advisor_cost_score` to `kpi_scorecard` and it is shown under `benchmarks`. It is not scored
  because Microsoft publishes no target. Read it with a read-only GET:

  ```bash
  az rest --method get --url "https://management.azure.com/subscriptions/<SUB>/providers/Microsoft.Advisor/advisorScore/Cost?api-version=2023-01-01"
  ```

  Use `properties.lastRefreshedScore.score`, which the API documents as a percentage score. If the
  value is 1 or less, it is a fraction: multiply it by 100 before passing it to the scorecard.

Source: https://learn.microsoft.com/en-us/cloud-computing/finops/framework/quantify/benchmarking
Last reviewed: 2026-10-23

## Microsoft tools map

| Need | Microsoft tool | How this pack relates |
| --- | --- | --- |
| Cost data, views, budgets, anomaly alerts, exports | Microsoft Cost Management | Reads UsageDetails, budgets and scheduled actions (read-only) |
| Optimization recommendations, Advisor score | Azure Advisor | Reads recommendations and the Cost score |
| Emissions and carbon reduction | Azure Carbon optimization | `finops-carbon-optimization` (read-only) |
| Tag and SKU guardrails | Azure Policy | Recommends assignments; reads compliance state only |
| Estimates | Pricing Calculator, Azure Migrate business case | Recommended only |
| Data at scale (FOCUS, Power BI, ADX/Fabric) | FinOps toolkit: FinOps hubs, Power BI reports | Recommended past the UsageDetails scale limits |
| Optimization workbooks, scripted automation | FinOps toolkit: workbooks, Azure Optimization Engine, PowerShell module, Bicep registry | Recommended only |
| Incidents and change tickets | Azure Monitor ITSM connector, SRE Agent incident platforms | Route findings to owners |

Sources: https://learn.microsoft.com/en-us/cloud-computing/finops/framework/manage/tools-services ·
https://learn.microsoft.com/en-us/cloud-computing/finops/toolkit/finops-toolkit-overview ·
https://learn.microsoft.com/en-us/cloud-computing/finops/framework/manage/intersecting-disciplines
Last reviewed: 2026-10-23
