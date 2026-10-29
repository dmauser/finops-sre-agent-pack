# How to read the FinOps reports

This guide explains, in plain language, what each FinOps Live Report tells you, how its key numbers
are calculated, and what to do with them. Most reports show simple cost totals. A few compute a score
or apply a threshold; for those, the exact rules are listed so you can explain them to your team.

All reports are **read-only and advisory**. They recommend actions; they never change your resources.
Each report also shows a data-quality panel and a refresh time, so you can tell how complete and how
current the data is.

## Financial evidence and confidence

The provenance panel identifies **currency, ActualCost or AmortizedCost, managed scopes, requested
and observed dates, source generation, snapshot ID, code version and run ID** when a cost snapshot
is supplied. ActualCost is the invoice/cash view; AmortizedCost spreads commitment purchases across
benefiting usage. Never add or compare them as though they were the same measure.

Missing or incomplete source provenance marks a report **partial** with an explicit warning.
Budget Status uses an immutable snapshot of the original budget API objects rather than a usage
ledger. It shows the evaluation date and API retrieval timestamp; neither proves how current
Azure's asynchronously updated `currentSpend` is. Its billing-data freshness stays unknown.
Cost Overview's **Period spend** reports the observed date range and count; 29 observed days are
never labeled a complete 30-day total. Missing closed-month history remains unavailable, while
any current operational metrics must identify their distinct period.

One complete export generation is one ledger, not an increment to yesterday's cumulative export.
If export transport is unavailable, reports disclose the UsageDetails fallback and historical
limitations. A successful API response or dates on a few rows do **not** prove full-period coverage.
Unavailable inputs remain **unknown**, not zero; closed-month reports must not substitute this month.
Nonresource charges stay in reconciliation rather than disappearing from resource lists.
Threshold examples below use USD; monetary figures carry their actual currency. Different currencies
are separated or rejected, never silently relabeled or converted.

Supported savings, review candidates and contradicted estimates are different evidence classes.
None is permission to change infrastructure. See [production operations](PRODUCTION.md) for gates,
limitations and rollback.

## Contents

- [Cost Overview](#cost-overview)
- [Cost Anomalies](#cost-anomalies)
- [Budget Status](#budget-status)
- [Rightsizing Savings](#rightsizing-savings)
- [Cost Allocation](#cost-allocation)
- [Waste Cleanup](#waste-cleanup)
- [Cost Optimization](#cost-optimization)
- [Commitments](#commitments)
- [AI Spend](#ai-spend)
- [SRE Agent Spend](#sre-agent-spend)
- [Cost vs Reliability](#cost-vs-reliability)
- [Monthly Review](#monthly-review)
- [References](#references)

## Cost Overview

**Question it answers:** Where is our money going?

Shows total spend for the period, the daily trend, and the top services and resource groups. These
are straight totals from Azure Cost Management; nothing is estimated.

**Why it matters:** you can't optimize what you can't see. This is the shared starting point for
engineering and finance.

## Cost Anomalies

**Question it answers:** Did anything cost more (or less) than expected yesterday?

For each service, resource group, and resource, the report compares the latest day to its history.

| Finding | Rule |
|---|---|
| **Spike** | The day is above the 28-day average plus 3 standard deviations, **or** at least 1.5× the same day last week, **and** the increase is at least $5 |
| **New spend** | Cost appears on something that had no cost history |
| **Drop** | The same rules in the other direction (below average minus 3 standard deviations, or 1.5× lower than last week, by at least $5) |
| **Sustained** | The last 3 days all moved by at least $5 in the same direction, so it looks like a step change (scale-out, new SKU, new workload) rather than a one-day burst |
| **Credits and refunds** | Negative cost rows, shown separately so they don't look like drops |

Results are ranked by **dollar impact**, not percentage, so a small resource doubling doesn't outrank
a large one growing 20%.

**Why it matters:** catching a spike the next day, instead of on the monthly invoice, limits how much
it costs.

## Budget Status

**Question it answers:** Are we going to stay within budget?

Reads each budget's identity, cost category, filter, currency, time grain, effective period and
configured notifications. A run-rate fallback applies only to a compatible current monthly window;
annual, expired or otherwise mismatched windows are not projected as monthly budgets.

| Status | Rule |
|---|---|
| **Over budget** | Spend has already reached the budget amount |
| **Forecast over** | Projected month-end spend reaches the budget amount |
| **At risk** | 80% or more of the budget is used, or one of the budget's own "actual" alert thresholds was crossed |
| **On track** | None of the above |
| **Unknown** | Spend, currency or applicable period is missing or incompatible; not a zero-spend success |

**Gated budgets** are the ones over or forecast over. They need a human decision before more is spent.
Overlapping subscription and resource-group budgets are shown separately, not summed into a portfolio
allowance. Analytical alert thresholds are not proof that Azure email/action-group delivery is configured;
check enabled actual versus forecast rules, operators, contacts and notification type.

**Why it matters:** budgets only help if someone acts before the money is gone.

## Rightsizing Savings

**Question it answers:** What can we downsize or turn off?

Combines Azure Advisor recommendations with real utilization and real cost.

| Term | Meaning |
|---|---|
| **Idle** | Candidate when VM p95 CPU is below 5% over at least 7 days of metrics; CPU alone is not sufficient evidence |
| **Oversized** | Candidate when VM p95 CPU is below 20% over at least 7 days of metrics; assess memory, disk, network and criticality |
| **Validated** | Sufficient representative utilization or complete inventory evidence supports further owner review, not automatic safety |
| **Verify first** | Not enough data to confirm; check with the owner before acting |
| **Estimated monthly savings** | Advisor's estimate when available; otherwise about 50% of current cost for a one-size-down change, or the full cost for idle resources |

Items below $5 a month in savings are left out.
Sparse samples, CPU-only readings and HTTP-only App Service evidence remain review candidates.
Contradictory memory evidence excludes the estimate from supported totals. Candidates and supported
amounts are shown separately; duplicate Advisor recommendations for one resource are not stacked.

**Why it matters:** rightsizing is one of the fastest savings, but cutting the wrong resource causes
incidents. The supported / verify-first split identifies what evidence still needs review.

## Cost Allocation

**Question it answers:** Which team owns this spend?

Groups cost by the best-populated ownership tag (for example `team`, `owner`, or `costCenter`).
Historical showback uses **each billing row's tags**, so changing today's owner cannot rewrite last
month. Current inventory tags form a separate governance view. Inheritance is applied to historical
rows only when its historical provenance is explicitly confirmed.

| Term | Meaning |
|---|---|
| **Allocated** | Cost with an owner tag value |
| **Unallocated** | Cost with no owner tag; this is the tagging backlog |
| **Via tag inheritance** | Cost where a historically attested tag came from the resource group or subscription; current tags alone do not prove historical ownership |
| **Fully loaded cost** | A team's direct cost plus its share of shared costs, when a split rule is supplied |
| **Tag hygiene** | One owner spelled several ways (for example `Payments` and `payments`), which splits their spend |

**Why it matters:** accountability. Teams act on cost they can see is theirs (showback first, then
chargeback).

## Waste Cleanup

**Question it answers:** What are we paying for that nobody uses?

| Type | Examples |
|---|---|
| **Orphan** | Public IPs not attached to anything, NAT gateways with no subnets, private endpoints whose connections are all disconnected or rejected |
| **Idle** | Standard load balancers or application gateways with empty backends, VPN or ExpressRoute gateways with no connections |
| **Stale** | Snapshots older than 90 days |

Savings are the resource's full current monthly cost. **Hygiene** items (detached NICs) and **review**
items (Log Analytics workspaces that may need a commitment tier or a retention review) carry no
claimed saving. Idle VMs, unattached disks, and empty App Service plans are covered by
[Rightsizing Savings](#rightsizing-savings), so nothing is counted twice.
Deletion-oriented findings require complete attachment/connection/child inventory. Missing fields are
not empty inventory. Age alone makes a snapshot a candidate, not proof it is disposable. Supported
savings and candidate estimates are separate, and duplicate resources are counted once.

**Why it matters:** waste is pure cost with no value, and it's usually the easiest money to recover.

## Cost Optimization

**Question it answers:** What should we do first?

An executive rollup of the other reports with one prioritized action list. Different savings types
(rightsizing, waste, commitments) are shown **separately and are not added together**, because some
overlap. For example, deleting a VM and buying a reservation for it can't both be counted.

**Why it matters:** one prioritized list makes it easy to decide and assign work.

## Commitments

**Question it answers:** Should we buy reservations or savings plans, and are the ones we have being used?

| Term | Meaning |
|---|---|
| **Commitment coverage** | Share of eligible usage cost covered by reservations or savings plans (amortized). On-demand spend is the opportunity |
| **Utilization** | How much of a commitment is actually used |
| **Underutilized** | 30-day utilization below 80% (7-day if 30-day isn't available) |
| **Expiring** | Ends within 60 days without auto-renew |
| **Potential monthly savings** | From Azure's purchase recommendations. Reservation and savings plan options are alternatives, so they're never added together |
| **Hybrid Benefit candidates** | Pay-as-you-go Windows or SQL licensing; enable only if you own eligible licenses |

Coverage excludes known-ineligible usage and negative credits/refunds from its eligible denominator.
Uncertain service/meter eligibility is reported separately. An arbitrary `OnDemand` row is not proof
that a reservation or savings plan can cover it. Eligibility and ownership must be verified before buying.

**Why it matters:** rate optimization lowers the price of what you already use without changing
workloads. Unused commitments are money already spent for nothing.

## AI Spend

**Question it answers:** What are our AI workloads costing?

Splits AI cost by resource, model, and meter, and separates **model-token spend** (pay per token) from
**compute spend** (hosted capacity and infrastructure).
Security, storage, network and support meters are ancillary. Unknown Machine Learning meters remain
unclassified, not automatically compute.

**Why it matters:** AI costs grow fast and are driven by usage patterns (prompt size, caching, model
choice) that engineering can change.

## SRE Agent Spend

**Question it answers:** What are our SRE Agents costing?

Splits Azure Agent Unit (AAU) cost per agent into **always-on flow** (a fixed 4 AAUs per agent-hour
while the agent exists) and **active flow** (tokens used while the agent works), and shows headroom
under the monthly AAU limit. See [Estimated cost of running the FinOps reports](COST.md).
The inferred always-on component is capped at 96 AAU per billed day. Excess unexplained billed units
remain **unreconciled**, not idle-agent savings. Raw activity cost is a rate-based estimate and may
not match the billing window because usage and invoices arrive at different times.

**Why it matters:** the tool that watches your spend should be accountable for its own.

## Cost vs Reliability

**Question it answers:** Where are we spending money and still having reliability problems, and where
is it safe to cut?

The **reliability score** is a "pain score" for each resource. It adds up points from three read-only
signals over the report period:

| Signal | Points |
|---|---|
| Azure Monitor alert, Sev0 | 8 |
| Azure Monitor alert, Sev1 | 5 |
| Azure Monitor alert, Sev2 | 3 |
| Azure Monitor alert, Sev3 | 1 |
| Azure Monitor alert, Sev4 | 0.5 |
| Azure Monitor alert, unknown severity | 2 |
| Resource Health event (Unavailable or Degraded) | 4 each |
| Advisor High Availability recommendation | 2 each |

**Higher means more pain**, not more reliable.

| Term | Meaning |
|---|---|
| **Risk band** | High: 10 or more. Medium: 5 to under 10. Low: above 0. None: 0 with complete sources; otherwise unknown |
| **Pain per $1K** | Score divided by monthly cost in thousands of dollars; finds cheap resources that cause a lot of trouble. Blank under $10 a month |
| **Invest list** | Score of 10 or more on a resource under $100 a month. Consider investing in resilience before cutting cost |
| **Verify-before-cutting list** | $1,000 or more a month with a score of 0 and complete source coverage. Check utilization and business criticality first |
| **Join coverage** | Share of costed resources whose signals could be matched by resource ID |

The score is a relative ranking within your environment, not an SLA or uptime metric. Advisor
recommendations show current configuration risk, not past incidents.

**Why it matters:** FinOps is about getting value from cloud spend, not just spending less. Seeing
reliability next to cost stops you cutting something that's already struggling (and causing an
outage), and shows where high spend with no issues is a safer place to optimize.

## Monthly Review

**Question it answers:** How is our FinOps practice doing, and who needs to do what?

Scores a set of FinOps KPIs as **pass**, **warn**, or **fail** against default targets:

| KPI | Pass | Warn |
|---|---|---|
| Cost allocated to an owner | 90% or more | 75% or more |
| Untagged spend | 5% or less | 15% or less |
| Tag hygiene issues | 2% or less | 5% or less |
| Managed scopes with a budget | 100% | 50% or more |
| Budgets over | 0 | 1 |
| Forecast vs budget | 100% or less | 110% or less |
| Commitment coverage | 70% or more | 50% or more |
| Commitment utilization | 95% or more | 80% or more |
| Waste as share of spend | 5% or less | 10% or less |
| Anomaly impact as share of spend | 1% or less | 5% or less |
| Unit cost change | 0% or less | 10% or less |
| Forecast accuracy (error) | Under 12% | 20% or less |

KPIs with no data are **unknown** and excluded from observed health, but reduce evidence coverage.
Budget comparisons require matching scope, filter, currency and period. Missing historical days
suppress a forecast unless a verified complete source establishes that those days truly had zero cost.
Service/resource/RG anomaly views cannot simply be summed without common event provenance.

| Term | Meaning |
|---|---|
| **KPI health** | Percentage of known KPIs that pass; can look high with very little evidence |
| **Evidence coverage** | Percentage of configured KPIs with usable evidence; always read alongside health |
| **Organizational maturity** | Not assessed. Crawl/Walk/Run requires a separate capability-based assessment, not a financial KPI percentage |
| **Forecast** | Linear trend with day-of-week seasonality and an 80% confidence band |
| **Next iteration focus** | Up to 3 capabilities with failing, then warning, KPIs to work on next |

Targets can be changed to match what your organization has agreed. Legacy helper `maturity` fields
are compatibility-only labels; Live Reports do not present them as organizational maturity.

**Why it matters:** FinOps is an iterative practice. A monthly scorecard shows progress, and routing
each finding to an owner turns the review into action.

## References

- [FinOps Foundation: FinOps principles](https://www.finops.org/framework/principles/)
- [FinOps Foundation: FinOps Framework capabilities](https://www.finops.org/framework/capabilities/)
- [Microsoft: FinOps documentation](https://learn.microsoft.com/cloud-computing/finops/)
- [Azure Well-Architected: Cost optimization tradeoffs](https://learn.microsoft.com/azure/well-architected/cost-optimization/tradeoffs)
- [Azure Monitor alerts overview (severity)](https://learn.microsoft.com/azure/azure-monitor/alerts/alerts-overview)
- [Azure Resource Health overview](https://learn.microsoft.com/azure/service-health/resource-health-overview)
- [Azure Advisor reliability recommendations](https://learn.microsoft.com/azure/advisor/advisor-high-availability-recommendations)
- [Azure Cost Management tag inheritance](https://learn.microsoft.com/azure/cost-management-billing/costs/enable-tag-inheritance)

The exact rules live in each skill's `SKILL.md` and Python helper under
[`plugins/finops/skills`](../plugins/finops/skills). If a default changes in code, the test
`tests/test_reports_doc.py` fails until this guide is updated.
