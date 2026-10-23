# Changelog

## Unreleased

### Added

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

- EA UsageDetails: cost is read in billing currency, so non-USD EA costs are no longer mislabelled.
- Optimization analyses use AmortizedCost, so reservation and savings-plan purchases no longer
  distort savings or anomalies.
- Budget evaluation respects Quarterly and Annually time grains when computing the period window.

### Changed

- The installer default `REPO_SLUG` and the marketplace owner now point to `dmauser/finops-sre-agent-pack`.
