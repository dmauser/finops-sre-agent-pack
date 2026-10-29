# Changelog

All notable changes to the **finops** plugin for Azure SRE Agent (`dmauser/finops-sre-agent-pack`) are
documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added

- **Auditable cost snapshots and native-export foundation**: run-isolated, write-once ledgers with
  currency, basis, requested/observed coverage, source hashes and report provenance; offline
  CSV/gzip manifest/partition validation and additive Hot ZRS export infrastructure. Export jobs
  stay inactive until network, SRE Agent transport and real-ledger reconciliation gates pass.
- **Production operations guide**: controlled upgrades, explicit source-SHA verification, rollback,
  permissions, coverage limitations, and incremental storage/AAU validation costs.

### Changed

- **Evidence before savings**: distinguish supported, candidate and contradicted estimates;
  require representative metrics and complete child inventory; deduplicate resource recommendations.
- **Monthly Review**: KPI health and evidence coverage replace the organizational-maturity claim.
  Incomplete history suppresses forecasts; comparison requires compatible budget context.
- **Safer installation defaults**: Cost Optimization only, plugin auto-update off, preserved
  existing task settings, explicit notification changes, and subscription Monitoring Reader
  instead of pack-owned Monitoring Contributor. Existing platform resource-group roles stay intact.

### Fixed

- **Financial integrity**: canonical resource identities, mixed-currency/invalid-cost rejection,
  nonresource reconciliation, billing-time allocation tags and distinct current-tag governance.
- **Budget and commitment semantics**: missing measurements remain unknown, overlapping budgets
  are not summed, notification rules stay distinct from analytical thresholds, and ineligible/
  uncertain usage does not inflate commitment eligibility.
- **Specialized evidence**: AI ancillary/unclassified meters are not assumed compute; unexplained
  SRE AAUs remain unreconciled; unavailable reliability sources cannot imply no risk.
- **Concurrent runs and portable generation**: isolated scratch and strict continuation boundaries
  prevent cross-run contamination; generated task references use UTF-8 consistently.

### Security

- **Read-only export access**: Entra-only container reads, disabled shared keys/anonymous access,
  explicit networking controls, and a guarded legacy monitoring-role migration with rollback.
- **Release gates remain explicit**: storage provisioning and offline tests do not establish live
  transport support or production readiness; no policy bypass or extra connector is added implicitly.

## [0.2.0] - 2026-10-29

### Added

- **Live Reports for anomalies, allocation, waste and commitments**: four renderer adapters and the
  `cost-allocation-report`, `waste-finder-report` and `commitment-report` scheduled tasks; Cost Anomaly
  Detection always saves `FinOps: Cost Anomalies` and emails only when anomalies are found.
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
- **Sample reports**: `docs/samples/generate_samples.py` builds all 12 Live Reports from fictional
  "Contoso" data through the real skill helpers and renderer. The README gallery now shows these
  samples (`docs/samples/*.html`, `docs/images/reports/*.png`) instead of live customer data.
- **How to read the reports**: new [`docs/REPORTS.md`](docs/REPORTS.md) explains each Live Report in
  plain language: the question it answers, how scores and thresholds are calculated (reliability
  score weights and risk bands, anomaly rules, rightsizing and commitment thresholds, KPI targets,
  maturity levels), and why it matters for FinOps, with public references. A new drift test,
  `tests/test_reports_doc.py`, keeps the documented numbers in sync with the skill code. Linked from
  the README and onboarding guide.
- **Cost estimates**: new [`docs/COST.md`](docs/COST.md) estimates active flow AAU and USD per run
  and per month for each Live Report and for all 12, measured on a lab agent. It explains the always-on
  versus active flow split, states clearly that figures are estimates and your consumption will differ,
  and shows how to enable only the reports you need. Linked from the README and onboarding guide.

### Changed

- The installer default `REPO_SLUG` and the marketplace owner now point to `dmauser/finops-sre-agent-pack`.
- Report tables show short resource names and resource groups instead of full ARM ids (anomalies,
  waste, rightsizing, optimization, Hybrid Benefit); allocation showback columns are labelled with the
  chosen tag dimension.
- **README refresh**: table of contents, architecture diagram, skill capability table, FinOps
  Framework coverage, Live Report catalog with schedules, a report gallery, example prompts, safety
  model, and permissions.
- **Install details**: the README lists the four resources `deploy.sh` creates, every role it
  grants (including the non-read Monitoring Contributor), optional settings, first-run and
  troubleshooting notes, and `az` commands that grant optional roles to the agent's managed
  identity. The GitHub Copilot install prompt now retries a failed clone, targets the managed
  identity, and restores the default `az` subscription.
- **Sub-page review**: skill counts updated to 15, a broken `#rbac` anchor fixed, the Carbon
  Optimization Reader role and a clone-failure troubleshooting row added, stale counts removed.

### Fixed

- **UsageDetails rows sharing an `id` were dropped as duplicates** (every report): Azure reuses one
  UsageDetails `id` across distinct rows (different dates, quantities, costs and tags), so de-duplicating
  by `id` removed about 5% of spend (live lab: $1,380.69 reported vs $1,452.35 in Cost Management) and
  zeroed whole Foundry projects in AI Spend. Row identity is now the full normalized row;
  `filter_usage_details(..., sources=...)` removes only overlap between separate retrievals, and
  `prepare_usage_details` never drops identical rows inside one chain.
- **Waste: unconnected VPN/ExpressRoute gateways missed**: gateways are now evaluated when connections
  were queried but none exist.
- **Concurrent scheduled tasks mixed usage pages**: `find_usage_tool_output_chains` never chains
  outputs from different thread ids (a live Allocation run reported $911 instead of $1,130).
- **Live Report accuracy** (found validating the new reports): Cost Overview merges resource-group
  casing and labels short periods; Rightsizing skips deleted resources and excludes Advisor
  reservation/savings plan recommendations from addressable waste; Commitments request single-scope
  benefit recommendations; Cost vs Reliability merges service casing and blanks pain per $1K below
  $10/month; threshold cells (`>=`/`<=`) stay on one line.
- **Cost Optimization schedule**: `deploy.sh` no longer forces `COST_OPT_CRON` to Mondays 09:00
  UTC. The task now uses the manifest default (Mondays 17:00 UTC) that the README documents.

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
- `install-api.sh` (`PLUGIN_SOURCE=direct`): rerunning on an agent with an existing direct install now
  refreshes the plugin to the latest commit instead of skipping it, so new skills are picked up.

- EA UsageDetails: cost is read in billing currency, so non-USD EA costs are no longer mislabelled.
- Optimization analyses use AmortizedCost, so reservation and savings-plan purchases no longer
  distort savings or anomalies.
- Budget evaluation respects Quarterly and Annually time grains when computing the period window.

## [0.1.0] - 2026-08-06

### Added

- Initial FinOps pack by [nirmash](https://github.com/nirmash/finops-sre-agent-pack): cost anomaly
  detection, rightsizing advisor (including Container Apps and session pools), cost allocation
  (showback), budget governance and budget editor, cost optimization report, FinOps for AI, and cost
  vs reliability skills; Cost Overview, Rightsizing Savings, Budget Status and Cost Optimization Live
  Reports; the `finops-investigator` agent; managed-scope enforcement; deterministic UsageDetails
  preparation; governed budget planning scripts.

[Unreleased]: https://github.com/dmauser/finops-sre-agent-pack/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/dmauser/finops-sre-agent-pack/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/dmauser/finops-sre-agent-pack/releases/tag/v0.1.0
