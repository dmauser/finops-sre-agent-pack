# Onboarding the FinOps SRE Agent

Two supported paths produce the same result: an Azure SRE Agent with the fifteen FinOps skills
(plugin), the read-only `finops-investigator` subagent, a scheduled task that keeps a Live Report
fresh, and read-only cost RBAC.

| | **Option A: Automated** | **Option B: Manual (portal)** |
|---|---|---|
| Effort | One command, ~10 minutes | ~30 minutes of portal steps |
| Creates the agent resource | Yes (Bicep) | You create it in the portal |
| Plugin, subagent, task, RBAC | Yes | Step by step below |
| Needs a shell | bash with `az`, `curl`, `python3` | No (Cloud Shell only for an optional render step) |
| Best for | Repeatable demos, new customers, CI | Locked-down tenants, learning the moving parts |

The paths can be mixed. For example, create the agent in the portal, then run the automated script
against it: the script detects the existing agent and adds only what is missing.

## What gets deployed

| Component | Detail |
|---|---|
| Resource group | Default `SRE-FinOps-Demo` |
| SRE Agent (`Microsoft.App/agents`) | Default `sre-finops-demo` in `centralus`. Access level Low, Review action mode, model *Automatic*, Stable channel, 10,000 monthly agent units, Limited sandbox egress |
| User-assigned managed identity | Used for actions and the knowledge graph. Managed resources = the subscription |
| Log Analytics + Application Insights | Agent telemetry |
| Agent identity RBAC (subscription) | Reader, Monitoring Contributor, Log Analytics Reader, **Cost Management Reader** |
| Agent identity RBAC (agent resource) | Reader, so every run can read the managed scope |
| Your RBAC | **SRE Agent Administrator** on the agent (data-plane access) |
| Plugin `finops` | Installed from `https://github.com/dmauser/finops-sre-agent-pack`, path `plugins/finops`, **auto-update on**, 14 skills |
| Subagent `finops-investigator` | Instructions, exactly five read-only tools, the FinOps skills plus `live_report_authoring` |
| Scheduled task | `FinOps: Cost Optimization (Live Report, Weekly)`, Mondays 09:00 UTC, **Autonomous**, response agent `finops-investigator` |
| Live Report | `FinOps: Cost Optimization` in Operations Hub. Created on the task's first run and refreshed on every run after that |

> **A Live Report refreshes only while its scheduled task exists and is Active.** A report saved
> from chat is a one-time snapshot. Both paths below create the task.

---

## Option A: Automated

Prerequisites:

- `az login` as an identity with **Owner**, or Contributor plus User Access Administrator, on the
  subscription.
- A bash shell with `az`, `curl` and `python3`: WSL, Linux, macOS, or Azure Cloud Shell. Git Bash on
  Windows is supported only for `--dry-run`.
- In Cloud Shell, run `az login --scope https://azuresre.dev/.default` first. Cloud Shell's built-in
  identity cannot get an SRE Agent data-plane token.

```bash
git clone https://github.com/dmauser/finops-sre-agent-pack
cd finops-sre-agent-pack
./plugins/finops/deploy/deploy.sh
```

The script prompts for three values. Press Enter to keep each default:

```text
  Agent name [sre-finops-demo]:
  Region [centralus]:
  Resource group [SRE-FinOps-Demo]:
```

It then runs two phases:

1. **Infrastructure.** [`main.bicep`](../plugins/finops/deploy/main.bicep) and
   [`agent.bicep`](../plugins/finops/deploy/agent.bicep) create the resource group, identity,
   monitoring, the agent, and the RBAC grants.
2. **Agent configuration.** [`install-api.sh`](../plugins/finops/install-api.sh) runs with
   `PLUGIN_SOURCE=direct`. It installs the plugin from the GitHub URL with auto-update on, upserts
   `finops-investigator`, and creates the scheduled task(s) as Autonomous on that subagent.

Re-running is safe. An existing agent is never re-provisioned, so its identity and settings are
kept, and phase 2 only upserts.

### Options

| Flag / variable | Default | Purpose |
|---|---|---|
| `-n` / `SRE_AGENT_NAME` | `sre-finops-demo` | Agent name |
| `-l` / `LOCATION` | `centralus` | Region; must be a region that supports SRE Agent (e.g. `eastus2`, `swedencentral`, `uksouth`, `australiaeast`) |
| `-g` / `RESOURCE_GROUP` | `SRE-FinOps-Demo` | Resource group, created if missing |
| `-s` / `SUBSCRIPTION_ID` | current `az` subscription | Target subscription |
| `--tasks` / `TASK_IDS` | `cost-optimization-report` | Comma-separated task ids, or `all` for all 12 tasks (each saves a Live Report; anomaly alerts and the monthly review also email) |
| `COST_OPT_CRON` | `0 9 * * 1` | Cost Optimization schedule (UTC) |
| `ALERT_EMAIL`, `GITHUB_REPO` | unset | Email delivery and change correlation for the email-review tasks |
| `GRANT_COMMITMENT_READERS=1` | off | Also grant tenant-level Reservations Reader and Savings plan Reader (needs elevated access) |
| `REPO_SLUG` | `dmauser/finops-sre-agent-pack` | Install the plugin from your fork |
| `GITHUB_PAT` | unset | Token for a private fork |
| `FORCE_INFRA=1` | off | Re-run Bicep even though the agent exists |
| `-y` | prompt | Accept defaults without prompting (CI) |
| `--dry-run` | | Validate inputs and the template; change nothing |

Task ids: `cost-anomaly`, `cost-overview-report`, `rightsizing-savings-report`,
`budget-status-report`, `cost-optimization-report`, `ai-spend-report`,
`sre-agent-spend-report`, `cost-vs-reliability-report`, `cost-allocation-report`,
`waste-finder-report`, `commitment-report`, `monthly-finops-review`.

```bash
# Everything, non-interactive, in another region
./plugins/finops/deploy/deploy.sh -y -n contoso-finops -l eastus2 -g rg-finops --tasks all

# Validate only
./plugins/finops/deploy/deploy.sh --dry-run
```

**Already have an agent?** Point the script at it with `-n` and `-g`. Phase 1 is skipped, and the
plugin, subagent, tasks and RBAC are added.

After the script finishes, select **Run now** on the task to produce the first report instead of
waiting for Monday (see [Verify](#6-verify)).

---

## Option B: Manual (portal)

### 1. Create the agent

1. In the Azure portal, search for **Azure SRE Agent** and select **Create**.
2. Choose the subscription, then set:
   - Resource group: `SRE-FinOps-Demo`
   - Agent name: `sre-finops-demo`
   - Region: `centralus`
   - Application Insights: **Create new**
3. Select **Create** and wait for **Succeeded** (about 2–5 minutes). The portal creates the
   user-assigned identity and makes you SRE Agent Administrator.
4. Open the agent at [sre.azure.com](https://sre.azure.com). In its **managed resources** settings,
   add the subscription or resource groups to analyze. Every FinOps skill is bounded by this list,
   and scheduled runs fail closed without it.

### 2. Grant read-only cost access

The roles below go to the agent's **user-assigned** managed identity, named `<agent>-<suffix>` on
the agent's Identity blade. The system-assigned identity is not used for this.

1. In **Subscriptions → your subscription → Access control (IAM) → Add role assignment**, assign
   **Cost Management Reader** to that identity.
2. On the agent resource's **Access control (IAM)**, assign **Reader** to the same identity, so each
   run can read the managed scope.
3. Optional: ask a Global Administrator with elevated access to grant **Reservations Reader** at
   `/providers/Microsoft.Capacity` and **Savings plan Reader** at
   `/providers/Microsoft.BillingBenefits`. These enable the commitment inventory section.

### 3. Install the plugin (skills)

In sre.azure.com, go to **Builder → Plugins → Install from URL**:

| Field | Value |
|---|---|
| Source URL | `https://github.com/dmauser/finops-sre-agent-pack` (or your fork) |
| Path in repo | `plugins/finops` |
| Authentication | Host default for the public repo, or a PAT for a private fork |
| Auto-update | **On**, so skills follow `main` |

Wait for the success message, which reports 14 skills.

> A plugin carries **only skills**. It does not create the subagent or the scheduled task, so
> continue with steps 4 and 5.

### 4. Create the `finops-investigator` subagent

1. Render the YAML with your agent's resource ID, for example in Cloud Shell:

   ```bash
   python3 plugins/finops/agents/render_agent_yaml.py \
     --agent-resource-id /subscriptions/<sub>/resourceGroups/SRE-FinOps-Demo/providers/Microsoft.App/agents/sre-finops-demo \
     > finops-investigator.yaml
   ```

   Or copy [`agents/finops-investigator.yaml`](../plugins/finops/agents/finops-investigator.yaml) and
   replace `{{AGENT_RESOURCE_ID}}` yourself.
2. In **Agent Canvas → Create → Custom agent → YAML**, paste the file and select **Create**.
3. Confirm that **Tools** lists exactly `RunAzCliReadCommands`, `ExecutePythonCode`, `ListReports`,
   `GetReport`, `SaveReport`. An empty tool list inherits every tool, including write tools.

To fill in the **Form** tab field by field instead, see
[Agent](../plugins/finops/README.md#agent).

### 5. Create the scheduled task that refreshes the report

1. Render the exact task definition:

   ```bash
   TASK_AGENT_NAME=finops-investigator \
   TASK_IDS=cost-optimization-report \
   COST_OPT_CRON="0 9 * * 1" \
   AGENT_RESOURCE_ID=/subscriptions/<sub>/resourceGroups/SRE-FinOps-Demo/providers/Microsoft.App/agents/sre-finops-demo \
   python3 plugins/finops/scheduled-tasks/render_tasks.py api > finops-tasks.json
   ```

2. In **Scheduled tasks → Create scheduled task**, copy the rendered fields:

   | Portal field | Value |
   |---|---|
   | Task name | `FinOps: Cost Optimization (Live Report, Weekly)` |
   | Instructions | the rendered `agentPrompt`, pasted unedited |
   | Schedule | custom cron `0 9 * * 1` (UTC) |
   | Response custom agent | `finops-investigator` |
   | Agent autonomy level | **Autonomous**. The portal defaults to Review, which stalls unattended runs |
   | Date range / Run limit | leave unset |

3. Add other report tasks the same way if you want them. See
   [Manual scheduled tasks](../plugins/finops/README.md#manual-scheduled-tasks) for every field, and
   for how to fix a task that was created from chat.

### 6. Verify

1. In **Scheduled tasks**, select **Run now** on the task.
2. When the run finishes, open **Operations Hub → Live Reports → FinOps: Cost Optimization** and
   check that **Refreshed** shows the current time.
3. In chat, type `/agent`, select `finops-investigator`, and ask
   *"Use finops-waste-finder on my subscription"*.

---

## After onboarding: run the practice

Both paths install the reports. To run FinOps as a monthly operating rhythm, also:

- Install the monthly review task: `deploy.sh --tasks cost-optimization-report,monthly-finops-review`,
  or create `FinOps: Monthly Review (KPI Scorecard)` by hand from
  [`monthly-finops-review.yaml`](../plugins/finops/scheduled-tasks/monthly-finops-review.yaml).
  Set `ALERT_EMAIL` to email it.
- Ask for the scorecard on demand: *"Use finops-kpi-scorecard: forecast this month, score our FinOps
  KPIs and give each team its action list"*.
- Route budget and anomaly alerts to the agent with an incident response plan, and connect your IaC
  repository as a code source for remediation pull requests. See
  [Operating model](../plugins/finops/README.md#operating-model).

---

## Updating an existing deployment

When `main` gains skills, subagent changes or tasks, bring an agent up to date the same way you
onboarded it.

**Automated.** Rerun the deploy script against the existing agent. It skips the infrastructure,
refreshes the plugin to the latest commit, upserts `finops-investigator`, and upserts the tasks you
list (existing tasks with the same name are updated in place):

```bash
./plugins/finops/deploy/deploy.sh -n sre-finops-demo -g SRE-FinOps-Demo \
  --tasks cost-optimization-report,monthly-finops-review
```

**Manual (portal).**

1. **Skills**: with auto-update on, the plugin follows `main`. To pull immediately, reinstall the
   plugin from the same URL; the installation is refreshed in place and reports the new skill
   count.
2. **Subagent**: open `finops-investigator`, paste the current instructions from
   [`finops-investigator.yaml`](../plugins/finops/agents/finops-investigator.yaml) (replace the
   agent resource ID placeholder), and add any new skill (for example `finops-kpi-scorecard`) to
   its allowed skills.
3. **Tasks**: create any new task (for example the monthly review) from its YAML in
   `plugins/finops/scheduled-tasks/`.
4. **RBAC**: confirm the agent identity still holds **Cost Management Reader** on the subscription.

> A task you created by hand under a different name is not replaced by the installer. Pause or
> delete it so two tasks do not refresh the same Live Report.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `deploy.sh`: *data plane still returns HTTP 403* | SRE Agent Administrator can take a few minutes to propagate. Re-run the script, or check the role on the agent's IAM blade. |
| Cannot get an `https://azuresre.dev` token | In Cloud Shell, run `az login --scope https://azuresre.dev/.default`, or use WSL or a local shell. |
| Region rejected | Pick a region that supports SRE Agent: `az provider show -n Microsoft.App --query "resourceTypes[?resourceType=='agents'].locations"`. |
| Report never refreshes | The task is missing, paused, in **Review** mode, or has a run limit or end date. See step 5. |
| *Commitment inventory unavailable* | Grant Reservations Reader and Savings plan Reader (step 2, optional). |
| Cost sections empty | The user-assigned identity lacks Cost Management Reader, or managed resources are empty. |
| `finops-investigator` missing from `/agent` in chat | The chat loads the agent list only once, when it opens, and caches it. Hard-refresh the page (Ctrl+F5) and start a **New chat**. Type `/agent`, choose **Agent**, and then pick `finops-investigator`. The `/agent` command is hidden in threads that are locked to an agent, such as scheduled-task, incident, or handler threads. To confirm the agent exists, check **Builder > Agent Canvas**. |
