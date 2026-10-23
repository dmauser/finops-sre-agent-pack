# FinOps Plugin

FinOps capabilities for Azure SRE Agent, delivered as **twelve skills + one agent + seven scheduled tasks** —
no changes to the SRE Agent product. The agent and every scheduled task use read-only Azure tools.
Budget planning can produce a governed shell script for a human to review, save, and run manually;
the agent never executes it.

## Managed scope boundary

`finops-managed-scope` is the shared guardrail for every analysis. **Every run** dynamically reads
the current `properties.knowledgeGraphConfiguration.managedResources` from `AGENT_RESOURCE_ID`;
the installer-time value is diagnostic only. Subscriptions, resource groups, and management groups
are supported. Changes to managed resources take effect on the next run without reinstalling the
plugin.

- **Scheduled runs are hard-bound and fail closed.** They accept no override. If the agent resource,
  managed-resource list, or required management-group expansion cannot be read and validated, the
  task stops without querying analysis data, sending email, or saving a Live Report.
- **Interactive runs default to managed scope.** A request wholly inside the boundary proceeds.
  An explicit request outside it requires the exact outside scopes to be displayed and explicitly
  confirmed by the user on a subsequent turn. That confirmation applies only to that request.
- **RBAC is not scope policy.** Broad or inherited Azure roles do not broaden the logical managed
  boundary, and installation does not revoke old broad grants.
- **Coverage is reported.** UsageDetails analyses retrieve each effective scope independently,
  de-duplicate and filter the merged rows, and report included, excluded, and unattributed counts
  and costs rather than hiding out-of-scope or unassignable spend.

The implementation is packaged entirely as skills, instructions, and scheduled-task policy. It
introduces no SRE Agent runtime changes.

## Skills

| Skill | What it does | Status |
|-------|--------------|--------|
| [`finops-cost-anomaly-detection`](skills/finops-cost-anomaly-detection/SKILL.md) | Detect cost spikes and correlate each to the deployment / change / PR that caused it | ✅ Wave 1 — validated live |
| [`finops-rightsizing-advisor`](skills/finops-rightsizing-advisor/SKILL.md) | Advisor cost recs + live utilization + inventory → ranked rightsizing / idle-cleanup recommendations (incl. idle Azure Container Apps environments, always-on apps, and warm session pools with no traffic, plus a cost-led sweep so no high-spend resource is missed), validated against real utilization and cost | ✅ Wave 1 — validated live |
| [`finops-waste-finder`](skills/finops-waste-finder/SKILL.md) | Find orphaned and idle non-compute waste the rightsizing advisor does not cover — unattached public IPs, NAT gateways with no subnets, dead private endpoints, empty load balancers and application gateways, unconnected VPN/ExpressRoute gateways, snapshots older than 90 days, orphaned NICs, and Log Analytics commitment-tier review — priced from amortized UsageDetails. Read-only. | ✅ Wave 5 — offline-tested |
| [`finops-commitment-advisor`](skills/finops-commitment-advisor/SKILL.md) | **Rate optimization** — reservation and savings-plan inventory with utilization, underutilized and expiring commitments, normalized monthly purchase recommendations (reservation **or** savings plan, never summed), on-demand vs committed coverage, and Azure Hybrid Benefit candidates for Windows VMs and SQL VMs. Read-only; recommends acting on usage optimization first. | ✅ Wave 5 — offline-tested |
| [`finops-cost-allocation`](skills/finops-cost-allocation/SKILL.md) | Join cost line items with tags → showback by any dimension; **tag-generic** (inventories the tag keys you actually use and reports coverage against a recommended `team/env/service/costCenter/app/owner` set rather than hard-coding it); explicit unallocated bucket + ranked untagged spend (no tags at all) + tag-hygiene flags | ✅ Wave 2 — offline-tested |
| [`finops-budget-governance`](skills/finops-budget-governance/SKILL.md) | Read native Azure budgets (`GET Microsoft.Consumption/budgets`) → evaluate each against amount + its own notification thresholds; Azure `forecastSpend` when present, else a client-side run-rate month-end forecast; ranks over / forecast-over / at-risk budgets and flags process gates. Handles the no-budgets-defined case. | ✅ Wave 2 — offline-tested |
| [`finops-budget-editor`](skills/finops-budget-editor/SKILL.md) | Advisory recommendations plus deterministic create/update plans for subscription, resource-group, and management-group budgets; Monthly/Quarterly/Annually; explicit or UsageDetails-derived amount; filter preservation and real-contact validation. Produces a governed human-run script with confirmation and read-back verification. | ✅ planning — offline-tested |
| [`finops-cost-optimization-report`](skills/finops-cost-optimization-report/SKILL.md) | **Executive rollup** — bundles the read-only analyses (anomalies, rightsizing, waste, commitments, cost allocation, budgets) into one headline, a single dollar-ranked priorities list (each item labelled by `impact_type` so savings, overruns, spikes, and governance dollars are never summed together), and per-section detail. Reuses existing signals only — no new data source. Read-only. | ✅ Wave 3 — offline-tested |
| [`finops-for-ai`](skills/finops-for-ai/SKILL.md) | Attribute Azure AI spend per resource / model / service family from the existing UsageDetails pull. Scopes by **`ConsumedService == Microsoft.CognitiveServices`** (captures both classic Azure OpenAI `kind=OpenAI` **and** Azure AI Foundry `kind=AIServices` accounts — see the AI resource taxonomy note below) **plus `Microsoft.MachineLearningServices`** (Foundry hub/project compute, managed online endpoints, fine-tuning), splits token/model meters from compute meters, ranks top drivers, and emits light read-only hints. | ✅ Wave 3 — offline-tested |
| [`finops-cost-vs-reliability`](skills/finops-cost-vs-reliability/SKILL.md) | Join monthly UsageDetails cost with alerts (primary reliability pain), Resource Health unavailable/degraded events, and Advisor HighAvailability recommendations → per-resource ranking, per-service rollup, high-pain/low-spend HA investment candidates, and high-spend/no-pain verify-before-cutting hints. Read-only weighted-count scoring. | ✅ Wave 4 — offline-tested |
| [`finops-managed-scope`](skills/finops-managed-scope/SKILL.md) | Dynamically resolve the agent's subscription, resource-group, and management-group managed resources; enforce scheduled and interactive scope policy; filter UsageDetails; and report included/excluded/unattributed coverage | ✅ foundational guardrail — offline-tested |
| [`finops-report-renderer`](skills/finops-report-renderer/SKILL.md) | Deterministically render the documented structured report model as escaped, CSP-compliant static HTML with metrics, warnings, tables, optional charts, and no Azure or connector calls | ✅ shared Live Report utility — offline-tested |

## Governed budget planning

`finops-budget-editor` can plan one scope-wide budget create/update at a subscription, resource
group, or management group for `Monthly`, `Quarterly`, or `Annually`. It preserves existing filters
and settings on update, rejects filters on create, requires real notification contacts (default
Actual 80% + Forecasted 100%), and never emits a placeholder-bearing command or script. Amounts may
be explicit or derived from bounded UsageDetails ActualCost period totals plus headroom.

The output includes a shell-safe application script with exact target/body, an exact preflight GET,
an exact confirmation phrase, an atomic conditional PUT (`If-Match` eTag for update or
`If-None-Match=*` for create), and post-write GET comparison that exits nonzero on mismatch.
The human runs it under their own Azure CLI identity and existing Cost Management Contributor access.
The pack does not execute the script, expose a write tool, grant write RBAC, or plan delete, bulk,
filtered-create, or scheduled mutations.

## Roadmap & backlog

All planned **skills** are now built. Remaining cross-cutting work items not represented as a
skill:

| Item | What | Status |
|------|------|--------|
| **Usage examples per analysis skill** | Each of the eight analysis/planning skills has sample input, an offline invocation, and representative output under its `examples/` directory. The examples use placeholder resource IDs; live retrieval is always governed by `finops-managed-scope`. | ✅ complete |
| **Cost-pull recipe simplification** | Lead with bounded `$top`, minimal `--query` field projection, and complete `nextLink` pagination; lower `$top` on `413`, then use verified date-windowing only as a fallback because the `usageStart` filter is not reliably applied. | ✅ complete |
| **Make repo public** | Flip the repo to public and drop the install PAT once the pack is ready to share. | 🔜 planned |

Engineering wave order is complete: Wave 1 anomaly + rightsizing, Wave 2 allocation + budgets, Wave 3 executive/AI reports, and Wave 4 `finops-cost-vs-reliability` + Live Report are now built.

## Agent

The API installer also creates **`finops-investigator`**, a standalone autonomous, read-only agent
configured with the twelve FinOps skills and the built-in Live Report authoring skill. It is the
default execution target for all seven scheduled tasks. Budget planning may return a human-run
script, but the agent and scheduled tasks never execute it. Existing agents are not modified.

> **Installed the plugin from the portal and `/agent` doesn't list FinOps?** That is expected: an
> SRE Agent plugin carries only skills (and MCP server requirements). Custom agents and scheduled
> tasks are never created by a plugin install. Either run `install-api.sh` (it detects the existing
> plugin and adds the agent and tasks), or create the agent from the portal YAML tab:
>
> 1. Render the YAML with your agent's resource ID (Cloud Shell has Python):
>    `python agents/render_agent_yaml.py --agent-resource-id <AGENT_RESOURCE_ID> > finops-investigator.yaml`
>    (or copy [`agents/finops-investigator.yaml`](agents/finops-investigator.yaml) and replace
>    `{{AGENT_RESOURCE_ID}}` with your agent's full ARM resource ID).
> 2. Open **Agent Canvas → Create → Custom agent → YAML**, paste the file, and select **Create**.
>    It sets the instructions, exactly the five read-only tools, and the 13 FinOps skills in one step.
> 3. Refresh the chat, type `/agent`, and select `finops-investigator`.
>
> The same YAML can be applied from an MCP client with the Azure MCP `sreagent_workflows_apply`
> command. To fill in the **Form** tab by hand instead:
>
> 1. Name it `finops-investigator`.
> 2. Paste `properties.instructions` from [`agents/finops-investigator.json`](agents/finops-investigator.json),
>    replacing `{{AGENT_RESOURCE_ID}}` with your agent's full ARM resource ID.
> 3. **Choose tools**: `RunAzCliReadCommands`, `ExecutePythonCode`, `ListReports`, `GetReport`,
>    `SaveReport`. Leaving tools empty inherits every global tool, including write tools.
> 4. **Choose skills**: every `finops-*` skill plus `live_report_authoring`.
> 5. Save, refresh the chat, then type `/agent` and select `finops-investigator`.
>
> A manually created agent does not get the scheduled tasks (Live Reports, email reviews) or RBAC;
> those still come from `install-api.sh`. **Without the scheduled tasks, Live Reports never
> refresh.** See [Manual scheduled tasks](#manual-scheduled-tasks) if you can't run the installer.
>
> Skills themselves never appear under `/`; the agent loads them automatically when a question
> matches, or when you name one ("Use finops-waste-finder on my subscription").

GitHub correlation and email delivery depend on connector tools already configured on the target
SRE Agent. Attach them during installation with `FINOPS_MCP_TOOLS` (comma-separated MCP tool
identifiers) and, when required, `FINOPS_CONNECTORS`. The investigator continues with Azure evidence
and reports the limitation when an optional connector is unavailable.

### Design note — AI resource taxonomy for `finops-for-ai` (#2)

Azure AI billing is easy to under-count, so `finops-for-ai` must **not** filter on `kind == OpenAI`
or a meter category literally named "Azure OpenAI" — that misses Foundry-hosted model spend. The
taxonomy:

| Resource | ResourceType | `kind` | ConsumedService | Cost driver |
|----------|--------------|--------|-----------------|-------------|
| Classic **Azure OpenAI** | `Microsoft.CognitiveServices/accounts` | `OpenAI` | `Microsoft.CognitiveServices` | token meters (input/output) |
| **Azure AI Foundry** (unified, formerly AI Services) | `Microsoft.CognitiveServices/accounts` | `AIServices` | `Microsoft.CognitiveServices` | token meters + other Cognitive Services meters |
| **Foundry hub / project** compute | `Microsoft.MachineLearningServices/*` (workspaces, online endpoints) | — | `Microsoft.MachineLearningServices` | managed compute / endpoint VMs, fine-tuning/training compute |

An OpenAI model (e.g. GPT-4o) deployed **inside** a Foundry `AIServices` account bills under
`Microsoft.CognitiveServices` with the **same** model/token meter names as a classic AOAI resource,
but the account's `kind` is `AIServices`, not `OpenAI`. So the skill should:

1. Scope AI model spend by **`ConsumedService == Microsoft.CognitiveServices`** (covers both `kind`s),
   then subdivide by `kind`, resource, deployment, and meter/model — never gate on `kind == OpenAI`.
2. **Also include `Microsoft.MachineLearningServices`** so Foundry hub compute, managed online
   endpoints, and fine-tuning/training compute aren't dropped.
3. Classify **token/model meters vs compute meters separately** — they are different cost drivers and
   warrant different optimization advice (model/tier/PTU choice vs idle-endpoint / right-size compute).

Reuses the existing read-only cost pull — no new data source, consistent with the rest of the pack.

## Prerequisites

These are Azure RBAC grants, not product changes. The supported installer enforces the first grant
and can optionally apply the second to the SRE Agent's managed identity.

| Grant | Needed for | Command |
|-------|-----------|---------|
| **Reader on the exact agent ARM resource** | Every run must read the current `knowledgeGraphConfiguration.managedResources` | Automatically granted and verified by `install-api.sh`; the installing identity needs role-assignment permission at `AGENT_RESOURCE_ID` |
| **Cost Management Reader on each UsageDetails transport scope** *(optional installer grant)* | Cost skills (UsageDetails returns no cost values without suitable access) | Set `MI_OBJECT_ID=<AGENT_MI_OBJECT_ID>` during installation. Subscription scopes remain subscription-scoped; RG scopes require the containing subscription because Consumption UsageDetails has no RG endpoint; management-group scopes remain management-group-scoped. |
| **Cost Management Contributor** *(write)* | Needed only by a human who chooses to run a generated budget application script. The pack, agent, and installer do not use or grant this role. | Grant out-of-band at the exact budget scope according to your governance process. |
| **Log Analytics Reader** + `api.loganalytics.io` scope | Pod/namespace-level AKS rightsizing (Container Insights KQL) | Grant the role on the workspace and allowlist the scope for the MI |
| **Reservations Reader** at `/providers/Microsoft.Capacity` + **Savings plan Reader** at `/providers/Microsoft.BillingBenefits` *(optional, tenant-level)* | Commitment inventory: utilization and expiry of existing reservations/savings plans. Without them reports show *"Commitment inventory unavailable"*; coverage %, unused-commitment cost and purchase recommendations still work. | `GRANT_COMMITMENT_READERS=1 MI_OBJECT_ID=<id> ./install-api.sh` (needs tenant-root User Access Administrator, i.e. a Global Admin with [elevated access](https://learn.microsoft.com/azure/role-based-access-control/elevate-access-global-admin); otherwise the installer prints the exact commands for an admin) |
| **Reader** (or Monitoring Reader) on managed resources | Azure Monitor utilization metrics that validate rightsizing/idle findings. Missing metrics leave findings *"verify first"*. | Usually already present via the managed-scope Reader assignment |

For an RG-managed boundary, the subscription-level Cost Management Reader grant is transport access
only. Every row is still filtered against the exact managed RG boundary, and included, excluded,
and unattributed coverage is disclosed. Existing inherited or broad role assignments likewise do
not broaden the FinOps logical boundary. Old broad assignments are not revoked automatically.

## Why read-only `az` is sufficient for cost

Actual cost comes from the **modern Consumption UsageDetails REST GET**
(`Microsoft.Consumption/usageDetails?api-version=2023-05-01&metric=AmortizedCost`, or `metric=ActualCost`
for budget cash-flow derivation), which passes the
read-only tool gate. The Cost Management **Query API is POST-based and stays blocked, but is not
required** — line items are aggregated / detected client-side in the sandbox. (Legacy
`az consumption usage list` returns null under MCA modern billing and is not used.)

## How to deploy with Azure SRE Agent

> **Onboarding guide:** [docs/ONBOARDING.md](../../docs/ONBOARDING.md) covers the one-command
> automated deployment ([`deploy/deploy.sh`](deploy/deploy.sh): agent, plugin, subagent, scheduled
> report, RBAC) and the equivalent manual portal steps.

Install this repository as a **plugin**, not as a code source. A plugin import loads the skills,
the bundled Python cores, the agent, and the scheduled tasks into SRE Agent; a code source is a
repository the agent reads to correlate incidents with changes.

1. **Install the pack** — use `install-api.sh` below (recommended: it also creates the
   `finops-investigator` agent, the scheduled tasks that produce the Live Reports, and RBAC). A
   portal install (**Builder → Plugins → Add marketplace** or *Install from URL*) imports **only the
   skills**: no agent appears under `/agent` and no Live Reports are produced until you also run the
   installer or create them by hand (see [Agent](#agent)).
2. **Set the managed scope** — add the subscriptions / resource groups the agent should analyze to
   its managed resources. Every skill enforces this boundary; scheduled runs fail closed without it.
3. **Grant read-only cost access** — Cost Management Reader (`MI_OBJECT_ID=...` lets the installer
   do it). For commitment inventory also grant Reservations Reader and Savings plan Reader at the
   tenant level (`GRANT_COMMITMENT_READERS=1`; see [RBAC](#rbac) — needs elevated access).
4. **Connect your IaC repository as a code source** (optional, recommended) — the customer's own
   Bicep/Terraform/app repository, not this one. It lets anomaly investigations cite the PR or
   deployment that caused a spike and lets the agent point at the IaC line to change.
5. **Adopt the FinOps cadence** — daily anomaly and budget checks (Inform), weekly cost-optimization
   review (Optimize: usage first, then rate), and a monthly showback with tag-coverage follow-up
   (Operate). Route each priority to its owner via the `team`/`owner` tags.

## Install everything (recommended)

This plugin ships as a **package**: one command installs the twelve skills, the standalone
`finops-investigator` agent, the eight proactive scheduled tasks (two email reviews + six Live
Reports), and (optionally) the RBAC grant. The tasks are all prefixed **`FinOps:`** and target
`finops-investigator` by default.

### API installer

[`install-api.sh`](install-api.sh) installs everything by calling the agent's own management API
directly (the same control-plane `srectl` uses) — so it needs only `az` (logged in), `curl`, and
`python3`. `AGENT_RESOURCE_ID` is required; endpoint-only installation cannot enforce dynamic
managed scope. No .NET build, no private NuGet feed. It (1) reads the agent endpoint, current
managed resources, and user-assigned identity from the ARM resource, (2) grants and verifies Reader
on that exact agent resource, (3) registers this repo as a plugin marketplace,
(4) installs the `finops` plugin (the server clones the repo and imports all skill dirs —
`SKILL.md` plus each bundled pure-Python core), (5) dry-run validates and upserts
`finops-investigator`, and (6) upserts the daily and weekly scheduled tasks. Re-running is safe.
The caller needs extended-agent and scheduled-task write permissions, plus scheduled-task delete
permission for the one-time migration of tasks previously targeting another agent, and permission
to create the Reader role assignment on the agent ARM resource.

```bash
# Your az-login identity must own ARM write on the agent (the resource owner does).
AGENT_RESOURCE_ID=/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.App/agents/<agent> \
  ./install-api.sh

# While this repo is private, pass a GitHub PAT so the server can clone it:
GITHUB_PAT=$(gh auth token) AGENT_RESOURCE_ID=<id> ./install-api.sh

# Also grant Cost Management Reader at every required UsageDetails transport scope:
MI_OBJECT_ID=<agent-mi-object-id> AGENT_RESOURCE_ID=<id> ./install-api.sh

# Also grant tenant-level Reservations Reader + Savings plan Reader (commitment inventory).
# Requires a Global Admin with elevated access; non-fatal if it fails.
GRANT_COMMITMENT_READERS=1 MI_OBJECT_ID=<agent-mi-object-id> AGENT_RESOURCE_ID=<id> ./install-api.sh
```

Configuration: required `AGENT_RESOURCE_ID`; optional `ENDPOINT` (consistency check only),
`PLUGIN_SOURCE=marketplace` (or `direct` to install from the GitHub URL with auto-update, like the
portal's *Install from URL*; `deploy/deploy.sh` uses `direct`), `PLUGIN_PATH=plugins/finops`,
`TASK_IDS=all` (comma-separated task ids from `scheduled-tasks/task-manifest.json` to install a subset),
`MARKETPLACE_NAME=finops-pack`, `PLUGIN_NAME=finops`, `REPO_SLUG=dmauser/finops-sre-agent-pack`,
`GITHUB_PAT`, `FINOPS_AGENT_NAME=finops-investigator`, `TASK_AGENT_NAME=finops-investigator`,
`FINOPS_MCP_TOOLS`, `FINOPS_CONNECTORS`, `TASK_NAME`,
`CRON="0 14 * * *"` (daily 14:00 UTC), `REPORT_TASK_NAME`, `REPORT_CRON="30 14 * * *"`
(daily Cost Overview Live Report, deliberately offset from anomaly detection),
`RIGHTSIZE_REPORT_TASK_NAME`, `RIGHTSIZE_REPORT_CRON="0 15 * * 1"`
(weekly Rightsizing Savings Live Report), `BUDGET_REPORT_TASK_NAME`, `BUDGET_REPORT_CRON="0 16 * * *"`
(daily Budget Status Live Report), `COST_OPT_TASK_NAME`, `COST_OPT_CRON="0 17 * * 1"`
(weekly Cost Optimization Live Report), `AI_REPORT_TASK_NAME`, `AI_REPORT_CRON="0 18 * * 1"`
(weekly AI Spend Live Report), `RELIABILITY_REPORT_TASK_NAME`, `RELIABILITY_REPORT_CRON="0 19 * * 1"`
(weekly Cost vs Reliability Live Report), `REPORT_NAME`, `RIGHTSIZE_REPORT_NAME`,
`BUDGET_REPORT_NAME`, `COST_OPT_NAME`, `AI_REPORT_NAME`, `RELIABILITY_REPORT_NAME`,
`MODEL_TIER` plus the per-task `ANOMALY_MODEL_TIER`, `REPORT_MODEL_TIER`,
`RIGHTSIZE_REPORT_MODEL_TIER`, `BUDGET_REPORT_MODEL_TIER`,
`COST_OPT_MODEL_TIER`, `AI_REPORT_MODEL_TIER`, and `RELIABILITY_REPORT_MODEL_TIER`,
`AGENT_NAME` (legacy alias for `TASK_AGENT_NAME`), `ALERT_EMAIL`, `GITHUB_REPO`, and
`MI_OBJECT_ID`, and `GRANT_COMMITMENT_READERS=0`. `RIGHTSIZE_TASK_NAME` remains accepted only to identify and retire a previously
installed standalone rightsizing-review task during migration.

All task tiers default to `ReasoningHeavy`. Keep that default for anomaly correlation, rightsizing,
cost optimization, and cost-versus-reliability. This repository has no verified lower-tier enum, so
it does not guess one for Cost Overview, Budget Status, or AI Spend. For an approved live A/B after
the target API's accepted enum is confirmed, set only `REPORT_MODEL_TIER`,
`BUDGET_REPORT_MODEL_TIER`, or `AI_REPORT_MODEL_TIER`, respectively, and rerun the deterministic
offline evaluation before comparing runtime, accuracy, and safety.

The canonical definitions for all seven active task names, descriptions, cron defaults, model-tier
variables, tags, report-name variables, and prompt assets live in
[`scheduled-tasks/task-manifest.json`](scheduled-tasks/task-manifest.json) and
[`scheduled-tasks/prompts/`](scheduled-tasks/prompts/). The stdlib-only
[`render_tasks.py`](scheduled-tasks/render_tasks.py) renders both installer API payloads and the
checked-in YAML references. Do not edit generated YAML directly:

```bash
python3 plugins/finops/scheduled-tasks/render_tasks.py write
python3 plugins/finops/scheduled-tasks/render_tasks.py check
```

`SUB_ID` is deprecated and ignored. Scheduled scope comes only from the current
`managedResources` value read through `AGENT_RESOURCE_ID`.

The installer detects the sandbox execution tool exposed by the target runtime. It prefers
`ExecutePythonCode` and safely falls back to `RunInTerminal` for bundled Python analysis; Azure
execution remains limited to `RunAzCliReadCommands`, and no Azure write tool is added.

Some V2 Agent Loop builds persist custom agents through the management API but do not resolve them
as interactive or scheduled home agents. Until that runtime regression is fixed, keep V2 enabled
and set `TASK_AGENT_NAME=<base-agent-name>` during installation so scheduled FinOps work runs on the
base agent; use the installed FinOps skills directly for interactive work. The strict managed-scope
task prompts still apply, but the base agent may expose additional tools, so this fallback does not
provide the dedicated custom agent's tool-level isolation.

`FINOPS_MCP_TOOLS` and `FINOPS_CONNECTORS` accept comma-separated connector integrations. They do
not modify the fixed core Azure/report tool list in the manifest. `FINOPS_EXTRA_TOOLS` is obsolete
and the installer rejects it when nonempty so an Azure write tool cannot be appended. Invalid
connector names fail the agent dry-run instead of silently falling back to another agent. Connector
integrations remain separate from the core tool list and should be limited to non-Azure-write
correlation or delivery capabilities; the agent instructions still prohibit Azure mutations.
Custom agent manifests are also checked: `properties.tools` must equal exactly
`RunAzCliReadCommands`, `ExecutePythonCode`, `ListReports`, `GetReport`, and `SaveReport` (order is
normalized). Missing or extra core tools stop installation with a read-only Azure safety error.

The scheduled-task update API cannot change an existing task's agent target. On the first upgrade
from an older installation, the installer therefore replaces each FinOps-owned task whose target is
not `TASK_AGENT_NAME`; it preserves a paused state when recreating it. Later runs update those tasks
in place as usual.

> Once this repo is public, drop `GITHUB_PAT` — the server clones it with the host's default GitHub
> identity.

What the API installer sets up:

| Component | What / where |
|-----------|--------------|
| **Agent** `finops-investigator` | standalone autonomous read-only FinOps agent with an explicit skill/tool allowlist; default target for all bundled tasks |
| **Skill** `finops-cost-anomaly-detection` | the whole skill dir `skills/finops-cost-anomaly-detection/` (SKILL.md + `detect.py`) |
| **Skill** `finops-rightsizing-advisor` | the whole skill dir `skills/finops-rightsizing-advisor/` (SKILL.md + `rightsize.py`) |
| **Skill** `finops-waste-finder` | the whole skill dir `skills/finops-waste-finder/` (SKILL.md + `waste.py`) |
| **Skill** `finops-commitment-advisor` | the whole skill dir `skills/finops-commitment-advisor/` (SKILL.md + `commit.py`) |
| **Skill** `finops-cost-allocation` | the whole skill dir `skills/finops-cost-allocation/` (SKILL.md + `allocate.py`) |
| **Skill** `finops-budget-governance` | the whole skill dir `skills/finops-budget-governance/` (SKILL.md + `budget.py`) |
| **Skill** `finops-budget-editor` | the whole skill dir `skills/finops-budget-editor/` (SKILL.md + `recommend.py`); deterministic recommendation/proposal builder and governed human-run application script generator |
| **Skill** `finops-cost-optimization-report` | the whole skill dir `skills/finops-cost-optimization-report/` (SKILL.md + `summarize.py`) |
| **Skill** `finops-for-ai` | the whole skill dir `skills/finops-for-ai/` (SKILL.md + `attribute.py`) |
| **Skill** `finops-cost-vs-reliability` | the whole skill dir `skills/finops-cost-vs-reliability/` (SKILL.md + `reliability.py`) |
| **Skill** `finops-managed-scope` | the whole skill dir `skills/finops-managed-scope/` (SKILL.md + dependency-free `scope.py`); live boundary resolution, fail-closed policy, filtering, and coverage reporting |
| **Skill** `finops-report-renderer` | the whole skill dir `skills/finops-report-renderer/` (SKILL.md + `render.py`); deterministic structured-model-to-static-HTML rendering for all six Live Reports |
| **Scheduled task** `FinOps: Cost Anomaly Detection (Daily)` | daily scan from [`scheduled-tasks/cost-anomaly-daily.yaml`](scheduled-tasks/cost-anomaly-daily.yaml) — alerts only on a spike |
| **Live Report** `FinOps: Cost Overview` (daily) | driven by [`scheduled-tasks/cost-overview-report-daily.yaml`](scheduled-tasks/cost-overview-report-daily.yaml) — a snapshot cost dashboard (total, daily trend, top services, top resource groups) in Operations Hub, re-versioned daily |
| **Live Report** `FinOps: Rightsizing Savings` (weekly) | driven by [`scheduled-tasks/rightsizing-savings-report-weekly.yaml`](scheduled-tasks/rightsizing-savings-report-weekly.yaml) — one shared rightsizing pull updates the snapshot dashboard and delivers the ranked review by email when configured |
| **Live Report** `FinOps: Budget Status` (daily) | driven by [`scheduled-tasks/budget-status-report-daily.yaml`](scheduled-tasks/budget-status-report-daily.yaml) — a snapshot budget-governance dashboard (spend vs amount, forecast, status, and gated budgets from `finops-budget-governance`) in Operations Hub, re-versioned daily |
| **Live Report** `FinOps: Cost Optimization` (weekly) | driven by [`scheduled-tasks/cost-optimization-report-weekly.yaml`](scheduled-tasks/cost-optimization-report-weekly.yaml) — the executive rollup dashboard (headline, a single dollar-ranked priorities list, and per-section detail across anomalies, rightsizing, waste, commitments, allocation, and budgets from `finops-cost-optimization-report`) in Operations Hub, re-versioned weekly |
| **Live Report** `FinOps: AI Spend` (weekly) | driven by [`scheduled-tasks/ai-spend-report-weekly.yaml`](scheduled-tasks/ai-spend-report-weekly.yaml) — an Azure AI cost dashboard (total AI spend, per-model + per-resource breakdowns, token-vs-compute split, top drivers, and read-only hints from `finops-for-ai`; covers Azure OpenAI + AI Foundry + ML) in Operations Hub, re-versioned weekly |
| **Live Report** `FinOps: Cost vs Reliability` (weekly) | driven by [`scheduled-tasks/cost-vs-reliability-report-weekly.yaml`](scheduled-tasks/cost-vs-reliability-report-weekly.yaml) — a cost-vs-reliability dashboard (spend + pain table, service rollup, HA investment candidates, verify-before-cutting candidates, and data-quality notes from `finops-cost-vs-reliability`) in Operations Hub, re-versioned weekly |
| **RBAC** | Reader on the exact agent ARM resource; optionally Cost Management Reader on each required UsageDetails transport scope when `MI_OBJECT_ID` is set |

The agent is upserted separately from the plugin installation because the runtime plugin importer
currently owns skills, not agent configurations. Uninstalling the plugin therefore does not delete
`finops-investigator`; remove that separately only when intentionally decommissioning it.

## Proactive monitoring

The bundled scheduled task first rediscovers and validates the current managed scope. Discovery
failure is fail closed: it performs no analysis query and sends no email. After successful scope
resolution, it runs the anomaly skill on a daily cron and **reports only when a spike is
detected** (otherwise it emits a single "no anomalies" line and stays quiet). On a detection it
correlates the spike to deployments and activity-log writes, adds GitHub correlation when
`GITHUB_REPO` is configured, and emails a ranked report with High importance when `ALERT_EMAIL` is
configured; otherwise the finding remains in the scheduled-task result. Edit the cron/agent/email in
the canonical task source or override it with the installer's environment variables. The generated
[`scheduled-tasks/cost-anomaly-daily.yaml`](scheduled-tasks/cost-anomaly-daily.yaml) is the reference
form. Manage it with `srectl scheduledtask list|pause|resume|get`.

## Live Reports (Operations Hub)

The pack also installs six **Live Reports** — self-contained HTML dashboards that appear in
**Operations Hub → Live Reports**:

- **`FinOps: Cost Overview`** (daily) — total spend, daily-spend trend, top services, and top
  resource groups.
- **`FinOps: Rightsizing Savings`** (weekly) — total potential monthly savings, a top-opportunities
  chart, and a ranked recommendations table from the `finops-rightsizing-advisor` analysis.
- **`FinOps: Budget Status`** (daily) — each budget's spend vs amount, forecast (Azure's or a
  run-rate estimate), status, breached thresholds, and any gated budgets, from the
  `finops-budget-governance` evaluation (budgets `GET` only — no cost pull).
- **`FinOps: Cost Optimization`** (weekly) — the executive rollup: a headline row, one dollar-ranked
  priorities list (each item labelled by `impact_type` so different kinds of dollars are never
  summed), and per-section detail across anomalies, rightsizing, waste, commitments, allocation, and budgets, from the
  `finops-cost-optimization-report` skill.
- **`FinOps: AI Spend`** (weekly) — Azure AI cost attributed per model, per resource, and per service
  family, with a token-vs-compute split, top cost drivers, and read-only optimization hints, from the
  `finops-for-ai` skill. Covers classic Azure OpenAI **and** Azure AI Foundry (`kind=AIServices`)
  accounts plus `Microsoft.MachineLearningServices` — see the AI resource taxonomy note above.
- **`FinOps: Cost vs Reliability`** (weekly) — monthly cost joined to weighted reliability pain from
  alerts (primary), Resource Health, and Advisor HighAvailability, with a spend + pain table, service
  rollup, HA investment candidates, verify-before-cutting candidates, and data-quality notes from the
  `finops-cost-vs-reliability` skill.

These are **snapshot** reports: each task runs deterministic analysis helpers, builds only the
documented `finops-report-renderer` model, calls `write_report`, and passes the generated static HTML
to `SaveReport` with `allowedTools=[]`. The prompts do not construct HTML, CSP, SRI, or Chart.js
markup. Each run finds the report by name and saves a **new version**, so the dashboard refreshes on
the task's cron (daily / weekly) rather than on every view. Azure cost data (`UsageDetails`) only
settles roughly daily, so daily/weekly refresh matches the data's freshness.

> **A Live Report only refreshes while its scheduled task exists and is Active.** Opening the
> dashboard does not re-run anything. A report generated once from chat, or one whose task is
> paused, deleted, or never installed (for example, a plugin-only install or a manually created
> agent), stays frozen at its **Refreshed** timestamp. To keep reports current, install the tasks
> with `install-api.sh`, or create them manually as described in
> [Manual scheduled tasks](#manual-scheduled-tasks). Check them with
> `srectl scheduledtask list`, or under **Scheduled tasks** in the portal.

> **Requires Live Reports enabled on the agent** (first-party + Operations Hub; feature flag
> `EnableLiveReports`). Where it isn't enabled, report saves do not refresh dashboards. The merged
> rightsizing task still completes its scheduled result/email delivery after a report-tool failure;
> anomaly delivery and all twelve skills are unaffected.

### Manual scheduled tasks

Use this when you installed the pack as a plugin (or created `finops-investigator` by hand) and
can't run `install-api.sh`. A plugin install never creates scheduled tasks, so without these steps
no Live Report or email review refreshes.

1. **Render the task definitions** with your values filled in (no placeholders remain):

   ```bash
   TASK_AGENT_NAME=finops-investigator \
   AGENT_RESOURCE_ID=/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.App/agents/<agent> \
   ALERT_EMAIL=you@contoso.com \
   python3 plugins/finops/scheduled-tasks/render_tasks.py api > finops-tasks.json
   ```

   `ALERT_EMAIL` and `GITHUB_REPO` are optional, and each `*_CRON` variable overrides a default
   schedule (see [API installer](#api-installer)). If your
   runtime can't use custom agents as a scheduled home agent (see the V2 note above), set
   `TASK_AGENT_NAME` to the base agent's name instead.

2. **Create one task per entry** in the portal (**Scheduled tasks → Create scheduled task**),
   copying each field:

   | Rendered field | Portal field | Value |
   |---|---|---|
   | `name` | **Task name** | e.g. `FinOps: Cost Optimization (Live Report, Weekly)` |
   | `agentPrompt` | **Instructions** | paste exactly, unedited |
   | `cronExpression` | **Schedule** → custom cron (UTC) | e.g. `0 17 * * 1` |
   | `agent` | **Response custom agent** | `finops-investigator` |
   | `agentMode` | **Agent autonomy level** | **Autonomous** (the portal defaults to Review) |
   | `description`, `modelTier` | description / model, where the form offers them | as rendered |

   Leave **Date range** open-ended and **Run limit** unset, or the report stops refreshing when
   either ends. Create all seven tasks, or at least the reports you use. Copy the instructions
   exactly: they carry the fail-closed managed-scope preamble, the report name that `SaveReport`
   versions, and the delivery rules. Keep the rendered task names too.

   > **Use Autonomous, not Review.** In Review mode the agent proposes actions and waits for a
   > person to approve them, so an unattended run can stop before `SaveReport` and the report goes
   > stale. These tasks are safe to run autonomously: the agent's tools allow Azure reads plus report
   > saves only.

   > **Created the task by asking the agent in chat?** That works (SRE Agent creates it for you), but
   > check the result under **Scheduled tasks** and edit it:
   > - Set **Agent autonomy level** to **Autonomous**; chat-created tasks default to Review.
   > - Set **Response custom agent** to `finops-investigator` so the run is limited to read-only tools.
   > - Replace free-form **Instructions** with the rendered `agentPrompt`. Hand-written prompts usually
   >   omit the strict managed-scope preamble, so a run could query outside the agent's scope instead
   >   of failing closed.
   > - Pinning a `reportId` is fine. The rendered prompts find the report by name instead, so they
   >   keep working if the report is recreated.
   >
   > To have the agent create it correctly, give it every value: *"Create a scheduled task named
   > `<name>`, cron `<cron>`, Autonomous, response custom agent `finops-investigator`, with exactly
   > these instructions: `<agentPrompt>`."*

3. **Grant RBAC.** The agent's **user-assigned** managed identity (shown in the agent's Identity
   blade, not the system-assigned one) needs Cost Management Reader on every managed scope, plus
   Reservations Reader and Savings plan Reader for the commitments section. Without these, reports
   save with warnings or empty sections.

4. **Verify the refresh.** Run each task once, or wait for its cron. Then open the report in
   Operations Hub and confirm the **Refreshed** timestamp advanced and a new version exists. A task's
   execution history shows the run: a scope error means `AGENT_RESOURCE_ID` or `managedResources` is
   wrong, and a pending approval means the task is still in Review mode.

When you upgrade the pack, re-render and update each task's prompt; stale prompts keep running the
old instructions.

### Runtime and API cost measurements

The canonical schedule now has zero same-minute collision pairs, down from two: Cost Overview moved
from 14:00 to 14:30 UTC, and the standalone Monday 15:00 rightsizing review was merged into the
Monday 15:00 Rightsizing Savings report. That merge reduces concurrent full weekly rightsizing
analyses from two to one while preserving the exact no-findings result and conditional Normal
importance email semantics. The merged prompt runs analysis before report operations and explicitly
continues result/email delivery if a report tool is unavailable or fails. The broader Cost
Optimization rollup remains deliberately separate at 17:00 because it correlates four domains.

For the scheduled-task installer path, steady-state scheduled-task API calls drop from 10 to 9
(one list, seven upserts, one verification list). The first migration run adds one deliberate
DELETE for the retired duplicate, returning that run to 10 calls. Static code-path measurement
reduces Python subprocesses for task rendering/planning/upsert lookup/final verification from 43 to
3 on an update, or from 51 to 10 on a fresh install where each POST response must still be parsed.
Generated scheduled-task YAML is 21,230 bytes (59.5% below the recorded 52,449-byte baseline), with
15,811 runtime agent-prompt bytes across the seven tasks.

## Install a skill only (manual)

If you only want an individual skill without installing the full package, use one of these methods.

### Option 1 — MCP `sre-agent-skills` tool (validated path)

This is what was used to install and validate `finops-cost-anomaly-detection` on a live agent. It uploads a
single self-contained skill blob (the detector is embedded inline in the skill body and written to
`detect.py` in the sandbox at runtime), so no separate reference-file upload is needed.

1. Point the `sre-agent` MCP server at your deployed agent (already authenticated via `az login`).
2. Call the `sre-agent-skills` tool with `action: create`, passing:
   - `name`: `finops-cost-anomaly-detection`
   - `description`: short summary
   - `content`: the SKILL.md body **with the contents of `detect.py` embedded inline** as a Python
     block the agent writes to `detect.py` before running it.
3. Verify with `sre-agent-skills` `action: list` — the skill should appear in the registry.

### Option 2 — `srectl` (directory-based, requires building Agent.Cli)

`srectl skill apply --name finops-cost-anomaly-detection` installs directly from this plugin's
`skills/<name>/` directory (carrying `SKILL.md` **and** the bundled `detect.py` reference file, so no
inlining is needed). This is the cleaner long-term path.

> **Note:** `srectl` must be built from `Agent.Cli` in the `sreagent-runtime` repo, which requires the
> private Antares Azure DevOps NuGet feed and .NET SDK `10.0.301`. If you don't have feed access, use
> Option 1.

### RBAC prerequisite

Before either manual skill install works end-to-end, grant **Reader** on the agent ARM resource and
appropriate **Cost Management Reader** access on each UsageDetails transport scope (see Prerequisites above) —
otherwise scope discovery fails or UsageDetails rows carry no cost values. (Enterprise Agreement rows never carry `costInUSD`; the pack uses their billing-currency `cost` instead.)

## Validation status

`finops-cost-anomaly-detection` was validated **end-to-end on a live subscription** (`93cba93f…`):

- Pulled 3,217 real Consumption UsageDetails line items (4 pages paginated, 13 days).
- Detector imported and ran clean on real data (0 anomalies — stable sub, expected).
- Backtest: injected a 5× synthetic spike on the top meter (Azure Container Apps) → flagged as
  `spike`, ranked #1 by impact (`current $227.11` / `baseline $17.71` / `Δ +$209.40`, +1182%).
- Correlation join queried deployments + activity log + GitHub PRs/commits for the spike window.

All 5 validation parts passed. The **daily proactive scheduled task** is also registered and Active
on the live agent (`FinOps: Cost Anomaly Detection (Daily)`, cron `0 14 * * *`), and the whole pack
has been installed end-to-end via [`install-api.sh`](install-api.sh) against a live agent.

`finops-rightsizing-advisor` was validated to the **same level** — 30 offline unit tests plus a live
end-to-end run on the same subscription (`93cba93f…`):

- Pulled **8 real Azure Advisor cost recs** (unattached disk, 3 empty App Service plans, 4 AKS).
- Pulled **live Resource Graph inventory** and **30 days of Consumption cost** (4 pages), aggregating
  spend by resource id — surfacing the `instanceName`/`resourceId` modern-billing detail below.
- Ran `recommend_rightsizing` on the real data: Advisor + inventory findings merged and ranked by
  estimated monthly savings, quantified rows first and unknown-savings rows last.
- Backtest: injected a synthetic idle VM (p95 CPU 1.5%) and an oversized VM (p95 CPU 11%) → classified
  `idle` (full-cost savings) and `oversized` (50% one-tier estimate), ranked idle first.
- **Azure Container Apps idle detection**: flags unused ACA environments (empty, or every app had
  zero `Requests` traffic over the window), always-on container apps (`minReplicas>=1`) with no
  hits, and **warm dynamic session pools** (`readySessionInstances>=1`) with zero `SessionApiRequestCount`
  — a class invisible to `az resource list` that was the subscription's #1/#2 spend ($331/mo each),
  validated live against real metrics.
- **Cost-led coverage sweep**: any resource costing ≥ $20/mo whose type has no idle rule (and that no
  other signal flagged) is surfaced as a `review` finding, so no expensive line item is ever silently
  dropped just because a heuristic doesn't exist for its type yet.

All 6 validation parts passed. The weekly `FinOps: Rightsizing Savings` task (cron `0 15 * * 1`)
now reuses that one analysis for both the Live Report and conditional ranked-review delivery.

> Modern-billing note found during validation: in Consumption UsageDetails the full ARM resource id
> is in `properties.instanceName` (the `resourceId` field is null), so cost is aggregated by
> `instanceName` (case-insensitive), falling back to `resourceId`.

## Test

The skills' logic is pure Python and offline-testable (`detect.py`, `rightsize.py`, `allocate.py`,
`budget.py`, `recommend.py`, `summarize.py`, `attribute.py`, `reliability.py`, `scope.py`,
`render.py`):

```bash
pip install -r requirements-dev.txt
pytest tests/          # 281 offline tests, including managed-scope and budget-script coverage
```
