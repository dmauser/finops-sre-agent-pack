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
- **`install-api.sh`** installs all 12 by default. Limit it with `TASK_IDS`, for example
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
