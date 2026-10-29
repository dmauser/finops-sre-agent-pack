# Estimated cost of running the FinOps reports

Each FinOps Live Report is a scheduled task. Every run uses **active flow** Azure Agent Units (AAUs) on
your SRE Agent. This page estimates the cost of each report and of all reports together, so you can
choose which ones to enable.

> [!IMPORTANT]
> **These figures are estimates only, not a price quote.** They come from a small number of runs in a
> single lab environment. Your consumption will differ, possibly by a large margin. Use them to compare
> reports and plan a budget, not as a forecast of your bill. To see your actual consumption, check
> **Settings > Agent consumption** in the SRE Agent portal, or run the **FinOps: SRE Agent Spend** report.

## Contents

- [How SRE Agent is billed](#how-sre-agent-is-billed)
- [Estimated cost per report](#estimated-cost-per-report)
- [Estimated monthly totals](#estimated-monthly-totals)
- [Enable only the reports you need](#enable-only-the-reports-you-need)
- [Keep costs predictable](#keep-costs-predictable)
- [What makes your cost different](#what-makes-your-cost-different)
- [How these estimates were measured](#how-these-estimates-were-measured)
- [Incremental cost of production hardening](#incremental-cost-of-production-hardening)
- [Approval-required connector cost scenario](#approval-required-connector-cost-scenario)

## How SRE Agent is billed

Azure SRE Agent bills in AAUs and has two parts
([Pricing and billing for Azure SRE Agent](https://learn.microsoft.com/azure/sre-agent/pricing-billing)):

| Component | How it's charged | Affected by this pack? |
|---|---|---|
| **Always-on flow** | 4 AAUs per agent-hour, from agent creation until the agent is deleted (about 2,920 AAUs a month) | No. You pay it with or without reports. |
| **Active flow** | AAUs for the LLM tokens the agent uses while it works, at rates set by the agent's model | Yes. Each report run adds active flow. |

The tables on this page cover **active flow only**. The USD amounts assume **$0.10 per AAU**. That is
the effective pay-as-you-go rate billed for the lab agents when we measured (cost ÷ quantity in Cost
Management). Your price can differ by agreement type, currency, region, and date. See
[Azure SRE Agent pricing](https://azure.microsoft.com/pricing/details/sre-agent/) and the
[Azure pricing calculator](https://azure.microsoft.com/pricing/calculator/) for current prices.

## Estimated cost per report

"AAU per run" is the median of the successful runs we measured. The range shows the lowest and highest
runs we measured. Runs per month assume about 30.4 runs for daily tasks, 4.35 for weekly tasks, and 1 for
monthly tasks.

| Live Report | Schedule | AAU per run (range) | Est. $ per run | Runs per month | Est. AAU per month | Est. $ per month |
|---|---|---|---|---|---|---|
| FinOps: Cost Anomalies | Daily | 38 (37–39) | $3.80 | 30.4 | 1,155 | $116 |
| FinOps: Cost Overview | Daily | 48 (36–60) | $4.80 | 30.4 | 1,459 | $146 |
| FinOps: Budget Status | Daily | 24 (24) | $2.40 | 30.4 | 730 | $73 |
| FinOps: Rightsizing Savings | Weekly | 57 (46–62) | $5.70 | 4.35 | 248 | $25 |
| FinOps: Cost Allocation | Weekly | 41 (31–44) | $4.10 | 4.35 | 178 | $18 |
| FinOps: Waste Cleanup | Weekly | 35 (30–39) | $3.50 | 4.35 | 152 | $15 |
| FinOps: Cost Optimization | Weekly | 59 (58–59) | $5.90 | 4.35 | 257 | $26 |
| FinOps: Commitments | Weekly | 36 (35–38) | $3.60 | 4.35 | 157 | $16 |
| FinOps: AI Spend | Weekly | 32 (32) | $3.20 | 4.35 | 139 | $14 |
| FinOps: SRE Agent Spend | Weekly | 40 (36–70) | $4.00 | 4.35 | 174 | $17 |
| FinOps: Cost vs Reliability | Weekly | 52 (49–56) | $5.20 | 4.35 | 226 | $23 |
| FinOps: Monthly Review | Monthly | 75 (55–92) | $7.50 | 1 | 75 | $8 |

Amounts are rounded, so the rows may not add up exactly to the totals below.

## Estimated monthly totals

| Scenario | Est. active AAU per month | Est. active $ per month |
|---|---|---|
| Default `deploy.sh` install (Cost Optimization only) | ~260 | ~$26 |
| All 8 weekly reports | ~1,530 | ~$153 |
| The 3 daily reports | ~3,340 | ~$334 |
| All 12 reports | ~4,950 | ~$495 |

The always-on flow (about 2,920 AAUs, or about $292 a month at $0.10 per AAU) is extra. You pay it for
any agent, whether or not it runs reports.

The **daily** reports account for about two-thirds of the total, because they run about 30 times a month.
Moving a daily report to a weekly schedule cuts its cost by about 85%.

## Enable only the reports you need

You don't have to run every report. Enable only the ones that answer questions your team acts on:

- **`deploy.sh`** installs only **Cost Optimization** by default. Add more with
  `--tasks cost-optimization-report,waste-finder-report`, or use `--tasks all` for every report.
- **`install-api.sh`** also defaults to Cost Optimization only. Select others with `TASK_IDS`, for example
  `TASK_IDS=cost-overview-report,budget-status-report AGENT_RESOURCE_ID=<id> ./plugins/finops/install-api.sh`.
- **After install**, pause or delete a task under **Scheduled tasks** in the SRE Agent portal. A paused
  task uses no active flow.
- **Change a schedule** with its `*_CRON` variable at install time (for example
  `REPORT_CRON="30 14 * * 1"` runs Cost Overview weekly). See
  [`task-manifest.json`](../plugins/finops/scheduled-tasks/task-manifest.json) for the variable names.

Task IDs are listed in [`task-manifest.json`](../plugins/finops/scheduled-tasks/task-manifest.json) and
the [onboarding guide](ONBOARDING.md).

## Keep costs predictable

- Set a **monthly active flow AAU limit** in **Settings > Agent consumption**. When the agent reaches the
  limit, it stops chat and actions until next month. Always-on billing continues.
- Run **FinOps: SRE Agent Spend** to track always-on versus active flow AAUs, the cost per agent, and the
  headroom left under the limit.
- Start with a few reports, check actual consumption after a week, then add more.
- Ad hoc chats with `finops-investigator` also use active flow and aren't included in these estimates.

## What makes your cost different

AAU consumption depends on the tokens each run uses, so your cost will vary with:

- **Environment size:** the number of subscriptions, resource groups, resources, budgets, and tags in
  the managed scope.
- **Data volume:** the number of Cost Management rows, Advisor recommendations, alerts, and anomalies a
  run reads and summarizes.
- **Model:** the agent's model provider and any model tier you set per task. AAU rates per token differ
  by model.
- **Findings:** runs that find more anomalies, waste, or recommendations do more follow-up work and use
  more tokens.
- **Retries and errors:** throttled or failed API calls can add tool calls to a run.
- **Pricing changes:** Microsoft sets AAU rates and prices and can change them.

## How these estimates were measured

| Item | Value |
|---|---|
| Measured | October 2026 |
| Environment | One lab SRE Agent (Anthropic model provider, default model tier) managing one subscription |
| Runs measured | 29 successful scheduled runs, 1 to 4 per report |
| AAU source | The agent's per-thread active flow usage, matched to each scheduled task's execution thread |
| Price source | Billed cost ÷ quantity for the "SRE Agent Unit" meter in Azure Cost Management |

The sample is small, and a larger or busier environment will use more AAUs per run. Treat these numbers
as a starting point and check your own consumption after the first runs.

## Incremental cost of production hardening

The optional native-export design reuses the existing agent and model. It adds one Central US
StorageV2 account using **Hot Standard_ZRS**, not another agent or always-on compute service.
Cost Management itself has no extra Azure-service fee; its storage destination is billed.

Public retail rates checked 2026-10-29, USD consumption, Central US, General Block Blob v2 Hot ZRS:

| Meter | Unit price |
|---|---:|
| Stored data, first tier | $0.023 / GB-month |
| Read operations | $0.004 / 10,000 |
| Write/list/create-container operations | $0.0625 / 10,000 |

Illustration: **10 GB total retained**, 100,000 reads and 10,000 writes per month costs
`10 × 0.023 + 10 × 0.004 + 1 × 0.0625 = $0.3325/month`.
**$1–$5/month is a small-lab planning allowance, not a cap or forecast.** It excludes transfer,
logs, taxes, extra services and larger retained datasets. Daily cumulative exports, 400-day closed
history, soft-deleted blobs and revisions all consume storage; do not confuse daily file size with
total retained capacity.

If active AAUs stayed unchanged, the old all-report lab scenario would move from about
**$787/month** ($292 always-on + $495 active) to about **$788–$792/month** with that storage allowance.
This excludes the workloads being analyzed. Reconciliation, richer evidence and retries can
increase active AAUs; exports may reduce paging but **no AAU saving is promised**.

One run of each of the 12 reports at the earlier median rates totals **$53.70**. Cost Overview and
Budget Status canaries add **$7.20**, giving **$60.90** for that validation sequence before transport
smoke tests, retries and reruns. This is separate from ongoing schedules.

Security-controlled networking can prevent direct export reads. A private endpoint, connector,
private DNS or additional compute is **not included or automatically deployed**; price and approve
that design separately. See [production gates](PRODUCTION.md).

Sources: [Azure Retail Prices API](https://prices.azure.com/api/retail/prices),
[Blob Storage pricing](https://azure.microsoft.com/pricing/details/storage/blobs/),
[Cost Management pricing](https://azure.microsoft.com/pricing/details/cost-management/).

## Approval-required connector cost scenario

The direct-export transport failed in the reference runtime. The
[alternative design](PRODUCTION.md#if-direct-transport-fails-approval-required-alternative) proposes
a narrowly scoped, read-only remote MCP service; **it is not deployed or included in the $1-$5
storage allowance**. A connector requires security/architecture approval and a working authentication
flow before implementation.

The following is a component-pricing scenario, **not a complete solution quote, forecast or cap**.
Public USD consumption rates were checked on 2026-10-29 using the Azure Retail Prices API:
Central US Container Apps Standard meters, global Private Link Standard first-tier meters, and
Azure DNS Private first-tier meters. Assume 730 hours/month, one 1-vCPU/2-GiB replica billed active
for the entire month, one storage private endpoint, one new private DNS zone, 10 GB of total
private-endpoint data processing, 1,000 DNS queries and 53,800 HTTP requests.

| Component | Rate and calculation | Illustrative monthly amount |
|---|---|---:|
| Container Apps active vCPU | $0.000024/vCPU-second x 1 x 2,628,000 seconds | $63.072 |
| Container Apps active memory | $0.000003/GiB-second x 2 x 2,628,000 seconds | $15.768 |
| HTTP requests | $0.40/million x 0.0538 million | $0.02152 |
| Storage private endpoint | $0.01/hour x 730 hours | $7.30 |
| Private Link data processing | $0.01/GB x 10 GB total ingress/egress | $0.10 |
| Private DNS zone | $0.50/zone-month x 1 | $0.50 |
| Private DNS queries | $0.40/million x 0.001 million | $0.0004 |
| **Listed components only** | Sum, rounded | **$86.76** |

Why show a full-month compute scenario? SRE Agent
[pings remote MCP connectors every 60 seconds](https://learn.microsoft.com/azure/sre-agent/mcp-connectors#connection-health-monitoring):
about 43,800 pings over 730 hours, plus the 10,000 application requests assumed above. These requests
can prevent scale-to-zero. This scenario deliberately does not assume idle billing or unused free
grants. Actual active/idle time, scaling, heartbeat behavior and shared subscription free-grant
consumption must be measured; multiple replicas or heavier workloads can cost more.

The subtotal **excludes** existing export storage/operations, SRE Agent AAUs, image registry,
logging, authentication gateway/hosting, NAT, bandwidth outside the stated data-processing
assumption, taxes, and any extra environment features. Container Apps Environment Private Endpoint
and Environment Management meters were each listed at $0.10/hour ($73 per 730-hour month) where
applicable; do not confuse them with the $0.01/hour **storage** private endpoint above. Confirm the
chosen environment's billable features before approval. A private endpoint on the MCP host does
not by itself solve the documented public-endpoint requirement for remote OAuth.

An existing approved hosting/authentication platform may avoid some new fixed charges, but its
capacity and internal charges are not assumed free. Reprice the selected design and obtain
approval rather than adding this scenario silently to the existing lab.

Sources: [Azure Retail Prices API](https://prices.azure.com/api/retail/prices),
[Container Apps pricing](https://azure.microsoft.com/pricing/details/container-apps/),
[Private Link pricing](https://azure.microsoft.com/pricing/details/private-link/),
[Azure DNS pricing](https://azure.microsoft.com/pricing/details/dns/).
