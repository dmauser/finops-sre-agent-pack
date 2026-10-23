# Changelog

## Unreleased

### Documentation

- **README refresh**: table of contents, architecture diagram, skill capability table, FinOps
  Framework coverage, Live Report catalog with schedules, a screenshot gallery of 11 live reports
  (identifiers masked) in `docs/images/reports/`, example prompts, safety model, and permissions.

### Fixed

- **UsageDetails chain isolation**: `find_usage_tool_output_chains` gains `after_timestamp` (ignore
  `tmp/ToolOutputs` files from earlier scheduled-task executions) and `request_url_prefix` (never
  merge chains from other subscriptions or API versions). Production had merged stale pages into a
  28-page chain of mixed dates.
- **MOSP/MCA date filters**: `finops-managed-scope` documents that UsageDetails ignores every date
  filter for MOSP and MCA subscriptions (current billing period only; `billingPeriods` is empty).
  Detect it on the first page, stop retrying, filter client-side, and label results partial.
- **Deterministic inputs**: new `save_finops_input` / `load_finops_input` helpers write Advisor,
  inventory, budget, and tag responses to `tmp/finops-data/<name>.json`, replacing heuristic
  ToolOutputs scanning. The Monthly Review prompt and `finops-kpi-scorecard` use them.

### Added

- **Microsoft FinOps alignment** (audited against learn.microsoft.com/cloud-computing/finops,
  last reviewed 2026-10-23):
  - **`finops-carbon-optimization`** (15th skill): Azure Carbon Optimization emissions for the
    managed scope — trend, month-over-month change, top items, largest increases, and access gaps.
    The report API is a query POST, so `carbon.py` builds validated requests and a human-run
    read-only script, then analyzes the saved JSON.
  - `finops-kpi-scorecard`: forecast accuracy KPI (`prior_forecast`), `exclude_purchases`, Advisor
    cost score input, benchmarks, next-iteration focus, and `microsoft-finops-practice.md`
    (iterations, maturity, practice operations, onboarding, governance, chargeback).
  - `finops-cost-allocation`: `microsoft-allocation-guidance.md` (hierarchy, tag inheritance,
    allocation rules, audit-first tag policy).
  - Microsoft guidance sections in anomaly detection (native anomaly alerts, ARG change
    correlation), budget governance/editor, commitment advisor, rightsizing, waste finder, and
    managed scope (Exports and FinOps hubs at scale).
  - README "Microsoft FinOps alignment" domain map and complementary Microsoft tools.
  - **Forecast accuracy, automatically**: each Monthly Review writes a
    `finops-forecast-record:v1` line with next month's forecast into its Live Report; the next
    review reads it back with `prior_forecast_inputs` (source `recorded`), or rebuilds the forecast
    from the days before the closed month (source `reconstructed`) when no record is readable.
- **`finops-sre-agent`** (14th skill): FinOps for Azure SRE Agent. Attributes Azure Agent Unit (AAU)
  spend per agent, splits always-on flow (4 AAU/agent-hour until deletion) from active flow using
  each agent's read-only `dailyUsageDetails`, breaks active flow down by source (chat, scheduled
  tasks, incidents, incident analysis), ranks the costliest threads, projects month-end use against
  `monthlyAgentUnitLimit`, and flags deleted/stopped-but-billed, idle, consolidation, limit-risk,
  trial-waiver, and premium-model cases. New `build_sre_agent_spend_model` renderer adapter.
- **SRE Agent rollup**: `summarize_optimization(sre_agents=...)` adds SRE Agent spend, always-on share,
  and de-duplicated removable savings (idle/stopped agents *or* consolidation, never both) to the
  Cost Optimization headline and priorities; the Cost Optimization and Monthly Review Live Reports
  gain "SRE Agent spend" and "SRE Agent findings" sections.
- **SRE Agent Spend task** `sre-agent-spend-report` (9th task, Mondays 18:30 UTC): weekly
  `FinOps: SRE Agent Spend` Live Report.
- **Faster UsageDetails paging**: one `az rest` call per page with no Python step between pages;
  new `find_usage_tool_output_chains` / `load_usage_tool_output_pages` rebuild the chain from the
  runtime's saved tool outputs in one step. Timeouts fall back to `%24top=500`/`250` (not `100`), and
  the monthly review pulls the two closed months once and reuses them for every step.
- **Monthly Review Live Report**: `monthly-finops-review` now also saves the `FinOps: Monthly Review`
  Live Report (new `build_monthly_review_model` adapter) while keeping email delivery. The six
  report skills now save their Live Report when run from chat for the full managed scope.
- `docs/ONBOARDING.md`: *Updating an existing deployment* (automated rerun and portal steps).

- **`finops-kpi-scorecard`** (13th skill): month-end and next-month forecast (linear trend with
  weekday seasonality, 80% band, budget status), unit economics (cost per request/order/user and
  trend), a FinOps KPI scorecard with tag and budget governance checks and a crawl/walk/run
  indication, and `route_to_owners` per-owner action lists.
- **Monthly review task** `monthly-finops-review` (8th task, 15:00 UTC on the 1st): scorecard,
  governance checks, forecast, month-over-month variance and owner action lists, emailed when
  `ALERT_EMAIL` is set.
- **Anomaly detection**: cost drops (`kind: drop`), `direction` and `sustained` flags, credits
  excluded from detection and summarized by `summarize_credits`, tag strings parsed.
- **Cost allocation**: tag inheritance (`inherited_tags`), shared-cost split (`shared=`, fully
  loaded cost per owner), `month_over_month` variance with a bridge, and FOCUS/Cost Details export
  mapping guidance.
- Operating-model docs: alert-triggered investigations, owner routing, governance checks and IaC
  pull-request remediation.

- **Automated onboarding**: `plugins/finops/deploy/deploy.sh` with Bicep (`main.bicep`,
  `agent.bicep`) creates the SRE Agent, identity, monitoring and read-only RBAC (including Cost
  Management Reader). It then installs the plugin from the GitHub URL with auto-update, creates
  `finops-investigator`, and schedules the weekly Cost Optimization Live Report. It prompts only
  for agent name, region and resource group.
- `docs/ONBOARDING.md`: side-by-side automated and manual (portal) onboarding guides.
- `install-api.sh`: `PLUGIN_SOURCE=direct` (install-direct, idempotent) and `TASK_IDS` to install a
  subset of scheduled tasks; `render_tasks.py api` honours `TASK_IDS`.

- **`finops-waste-finder`**: finds orphaned and idle non-compute resources: unattached public IPs,
  NAT gateways with no subnets, dead private endpoints, empty load balancers and application
  gateways, unconnected VPN/ExpressRoute gateways, snapshots older than 90 days, orphaned NICs, and
  Log Analytics commitment-tier candidates. Savings are priced from amortized UsageDetails.
- **`finops-commitment-advisor`**: rate optimization. Covers reservation and savings-plan
  utilization, underused and expiring commitments, and purchase recommendations normalized to
  monthly. Reservation and savings-plan options are shown as alternatives and never summed. Also
  reports on-demand vs committed coverage and Azure Hybrid Benefit candidates.
- The cost-optimization report and weekly Live Report now include waste and commitment sections.
  Usage optimization (rightsizing + waste, de-duplicated) is reported separately from rate
  optimization.
- A README section on deploying the pack with SRE Agent: install it as a plugin, set the managed
  scope, grant RBAC, connect the customer's IaC repo as a code source, and set up a FinOps cadence.

### Fixed

- `install-api.sh` (`PLUGIN_SOURCE=direct`): rerunning on an agent with an existing direct install now
  refreshes the plugin to the latest commit instead of skipping it, so new skills are picked up.

- EA UsageDetails: cost is read in billing currency, so non-USD EA costs are no longer mislabelled.
- Optimization analyses use AmortizedCost, so reservation and savings-plan purchases no longer
  distort savings or anomalies.
- Budget evaluation respects Quarterly and Annually time grains when computing the period window.

### Changed

- The installer default `REPO_SLUG` and the marketplace owner now point to `dmauser/finops-sre-agent-pack`.
