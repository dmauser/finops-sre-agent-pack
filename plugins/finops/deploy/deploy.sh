#!/usr/bin/env bash
# deploy.sh — automated, end-to-end onboarding of the FinOps SRE Agent.
#
# Reproduces a complete FinOps deployment in one command:
#   Phase 1 (ARM, main.bicep): resource group, user-assigned identity, Log Analytics,
#            Application Insights, the Microsoft.App/agents resource, SRE Agent Administrator
#            for you, and read-only subscription RBAC for the agent identity
#            (Reader, Monitoring Contributor, Log Analytics Reader, Cost Management Reader).
#   Phase 2 (agent data plane, ../install-api.sh): the `finops` plugin (14 skills, installed
#            from the GitHub URL with auto-update), the finops-investigator subagent, and the
#            scheduled task(s) that keep the Live Report(s) refreshed.
#
# It only prompts for agent name, region and resource group (Enter keeps the default).
# Re-running is safe: an existing agent is never re-provisioned; phase 2 upserts.
#
# Usage:
#   ./deploy.sh                                   # interactive
#   ./deploy.sh -y                                # accept defaults, no prompts
#   ./deploy.sh -n my-finops -l eastus2 -g rg-finops -y
#   ./deploy.sh --tasks all                       # all 8 scheduled tasks / 6 Live Reports
#   ./deploy.sh --dry-run                         # validate only, change nothing
#
# Environment overrides: SRE_AGENT_NAME, LOCATION, RESOURCE_GROUP, SUBSCRIPTION_ID, TASK_IDS,
# COST_OPT_CRON, ALERT_EMAIL, GITHUB_REPO, REPO_SLUG (fork owner/repo), GITHUB_PAT,
# GRANT_COMMITMENT_READERS=1, plus every install-api.sh variable.
#
# Requires: az (logged in, Owner or User Access Administrator + Contributor on the
# subscription), curl, python3. Run from WSL, macOS/Linux, or a local shell; Azure Cloud Shell
# cannot mint the SRE Agent data-plane token without 'az login --scope https://azuresre.dev/.default'.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd -P)"
INSTALLER="$PLUGIN_DIR/install-api.sh"
TEMPLATE="$SCRIPT_DIR/main.bicep"

# Defaults mirror the reference deployment.
DEFAULT_AGENT_NAME="sre-finops-demo"
DEFAULT_LOCATION="centralus"
DEFAULT_RESOURCE_GROUP="SRE-FinOps-Demo"
DEFAULT_TASK_IDS="cost-optimization-report"
TOKEN_RESOURCE="${TOKEN_RESOURCE:-https://azuresre.dev}"

SRE_AGENT_NAME="${SRE_AGENT_NAME:-}"
LOCATION="${LOCATION:-}"
RESOURCE_GROUP="${RESOURCE_GROUP:-}"
SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-}"
TASK_IDS="${TASK_IDS:-$DEFAULT_TASK_IDS}"
# Empty means "use the manifest defaultCron" (render_tasks.py treats "" as unset).
COST_OPT_CRON="${COST_OPT_CRON:-}"
ASSUME_YES="${ASSUME_YES:-0}"
DRY_RUN=0
FORCE_INFRA="${FORCE_INFRA:-0}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    -n|--name) SRE_AGENT_NAME="${2:?}"; shift 2;;
    -l|--location) LOCATION="${2:?}"; shift 2;;
    -g|--resource-group) RESOURCE_GROUP="${2:?}"; shift 2;;
    -s|--subscription) SUBSCRIPTION_ID="${2:?}"; shift 2;;
    --tasks) TASK_IDS="${2:?}"; shift 2;;
    -y|--yes) ASSUME_YES=1; shift;;
    --dry-run) DRY_RUN=1; shift;;
    -h|--help) usage;;
    *) die "Unknown argument: $1 (see --help)";;
  esac
done

# ---- Preflight ---------------------------------------------------------------
say "Preflight"
for tool in az curl python3; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool not found."
done
[ -f "$INSTALLER" ] || die "Installer not found: $INSTALLER"
[ -f "$TEMPLATE" ] || die "Template not found: $TEMPLATE"
TASKS_DIR="$PLUGIN_DIR/scheduled-tasks"
if command -v cygpath >/dev/null 2>&1; then
  # Git Bash: stop MSYS rewriting /subscriptions/... ARM IDs into Windows paths.
  export MSYS_NO_PATHCONV=1
  TEMPLATE="$(cygpath -w "$TEMPLATE")"
  TASKS_DIR="$(cygpath -w "$TASKS_DIR")"
fi
az account show >/dev/null 2>&1 || die "Not logged in to Azure. Run 'az login' first."
if [ -n "$SUBSCRIPTION_ID" ]; then
  az account set --subscription "$SUBSCRIPTION_ID" || die "Cannot select subscription $SUBSCRIPTION_ID."
fi
SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
SUBSCRIPTION_NAME="$(az account show --query name -o tsv)"
ok "Subscription: $SUBSCRIPTION_NAME ($SUBSCRIPTION_ID)"

interactive=0
if [ "$ASSUME_YES" != "1" ] && [ -t 0 ]; then interactive=1; fi

ask() {  # ask VAR "Prompt" default
  local __var="$1" prompt="$2" default="$3" current="${!1}" reply=""
  if [ -n "$current" ]; then default="$current"; fi
  if [ "$interactive" = "1" ]; then
    read -r -p "  $prompt [$default]: " reply || true
  fi
  printf -v "$__var" '%s' "${reply:-$default}"
}

say "Deployment settings (press Enter to keep the default)"
ask SRE_AGENT_NAME "Agent name" "$DEFAULT_AGENT_NAME"
ask LOCATION "Region" "$DEFAULT_LOCATION"
ask RESOURCE_GROUP "Resource group" "$DEFAULT_RESOURCE_GROUP"

[[ "$SRE_AGENT_NAME" =~ ^[A-Za-z][-A-Za-z0-9]{0,30}[A-Za-z0-9]$ ]] || \
  die "Agent name must be 2-32 chars: letters, numbers, hyphens; start with a letter; end alphanumeric."
LOCATION="$(printf '%s' "$LOCATION" | tr '[:upper:]' '[:lower:]' | tr -d ' ')"
[[ "$LOCATION" =~ ^[a-z0-9]+$ ]] || die "Region must be an Azure region name such as eastus2."
[[ "$RESOURCE_GROUP" =~ ^[-A-Za-z0-9._()]{1,90}$ ]] && [[ "$RESOURCE_GROUP" != *. ]] || \
  die "Invalid resource group name: $RESOURCE_GROUP"

AGENT_RESOURCE_ID="/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.App/agents/$SRE_AGENT_NAME"
printf '  Agent        : %s\n  Region       : %s\n  Resource grp : %s\n  Tasks        : %s\n  Plugin source: https://github.com/%s/plugins/finops (auto-update)\n' \
  "$SRE_AGENT_NAME" "$LOCATION" "$RESOURCE_GROUP" "$TASK_IDS" "${REPO_SLUG:-dmauser/finops-sre-agent-pack}"

# Fail fast on a bad task selection before touching Azure.
TASK_IDS="$TASK_IDS" python3 - "$TASKS_DIR" <<'PY' || die "Invalid --tasks/TASK_IDS."
import sys
sys.path.insert(0, sys.argv[1])
import render_tasks
try:
    render_tasks.select_tasks(render_tasks.load_manifest())
except ValueError as exc:
    sys.exit(f"  {exc}")
PY

if [ "$interactive" = "1" ] && [ "$DRY_RUN" != "1" ]; then
  read -r -p "  Proceed? [Y/n]: " confirm || true
  case "${confirm:-Y}" in [Yy]*) ;; *) die "Cancelled.";; esac
fi

# ---- Deployer identity -------------------------------------------------------
case "$(az account show --query user.type -o tsv)" in
  servicePrincipal)
    DEPLOYER_TYPE="ServicePrincipal"
    DEPLOYER_OID="$(az ad sp show --id "$(az account show --query user.name -o tsv)" --query id -o tsv)";;
  *)
    DEPLOYER_TYPE="User"
    DEPLOYER_OID="$(az ad signed-in-user show --query id -o tsv)";;
esac
[ -n "$DEPLOYER_OID" ] || die "Could not resolve the deploying identity's object ID."
ok "Deployer: $DEPLOYER_TYPE $DEPLOYER_OID"

BICEP_PARAMS=(
  agentName="$SRE_AGENT_NAME"
  resourceGroupName="$RESOURCE_GROUP"
  location="$LOCATION"
  deployerObjectId="$DEPLOYER_OID"
  deployerPrincipalType="$DEPLOYER_TYPE"
)

agent_exists=0
if az resource show --ids "$AGENT_RESOURCE_ID" -o none 2>/dev/null; then agent_exists=1; fi

if [ "$DRY_RUN" = "1" ]; then
  say "Dry run: validating the infrastructure template (no changes)"
  if [ "$agent_exists" = "1" ]; then
    ok "Agent already exists — phase 1 would be skipped; phase 2 would upsert plugin, subagent and tasks."
  else
    az deployment sub validate -l "$LOCATION" -f "$TEMPLATE" -p "${BICEP_PARAMS[@]}" -o none \
      || die "Template validation failed."
    ok "Template valid for a new agent '$SRE_AGENT_NAME' in $LOCATION"
  fi
  exit 0
fi

# ---- Phase 1: infrastructure ------------------------------------------------
say "Phase 1 — infrastructure"
az provider register --namespace Microsoft.App --wait -o none || die "Could not register Microsoft.App."
if [ "$agent_exists" = "1" ] && [ "$FORCE_INFRA" != "1" ]; then
  ok "Agent $SRE_AGENT_NAME already exists — keeping its identity and settings (FORCE_INFRA=1 to redeploy)."
else
  DEPLOYMENT_NAME="finops-sre-${SRE_AGENT_NAME}"
  az deployment sub create -n "$DEPLOYMENT_NAME" -l "$LOCATION" -f "$TEMPLATE" \
    -p "${BICEP_PARAMS[@]}" -o none || die "Infrastructure deployment failed (az deployment sub show -n $DEPLOYMENT_NAME)."
  ok "Deployed $DEPLOYMENT_NAME"
fi

say "Waiting for the agent to be ready"
ENDPOINT=""
for i in $(seq 1 60); do
  read -r state ENDPOINT < <(az resource show --ids "$AGENT_RESOURCE_ID" \
    --query "[properties.provisioningState, properties.agentEndpoint]" -o tsv 2>/dev/null | tr '\t\n' '  ') || true
  if [ "${state:-}" = "Succeeded" ] && [ -n "${ENDPOINT:-}" ]; then ok "Agent Succeeded: $ENDPOINT"; break; fi
  [ "$i" -eq 60 ] && die "Timed out waiting for the agent (state: ${state:-unknown})."
  printf '  … provisioningState=%s (%d/60)\n' "${state:-?}" "$i"; sleep 15
done

UAMI_ID="$(az resource show --ids "$AGENT_RESOURCE_ID" --query properties.actionConfiguration.identity -o tsv)"
[ -n "$UAMI_ID" ] || UAMI_ID="$(az resource show --ids "$AGENT_RESOURCE_ID" --query properties.knowledgeGraphConfiguration.identity -o tsv)"
MI_OBJECT_ID="$(az identity show --ids "$UAMI_ID" --query principalId -o tsv)" || die "Cannot resolve agent identity $UAMI_ID"
ok "Agent identity principal: $MI_OBJECT_ID"

# SRE Agent Administrator can take a few minutes to propagate to the data plane.
say "Waiting for data-plane access"
for i in $(seq 1 40); do
  TOKEN="$(az account get-access-token --resource "$TOKEN_RESOURCE" --query accessToken -o tsv 2>/dev/null)" \
    || die "Cannot mint a $TOKEN_RESOURCE token. In Cloud Shell run: az login --scope $TOKEN_RESOURCE/.default"
  code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$ENDPOINT/api/v2/plugins/installations" || true)"
  [ "$code" = "200" ] && { ok "Data plane reachable"; break; }
  [ "$i" -eq 40 ] && die "Data plane still returns HTTP $code. Confirm you hold SRE Agent Administrator on the agent."
  printf '  … HTTP %s (%d/40)\n' "$code" "$i"; sleep 15
done
unset TOKEN

# ---- Phase 2: plugin, subagent, scheduled tasks, RBAC -----------------------
say "Phase 2 — plugin, finops-investigator subagent, scheduled tasks"
# AGENT_NAME is an install-api.sh alias for the task target; never leak the SRE agent name into it.
env -u AGENT_NAME \
  AGENT_RESOURCE_ID="$AGENT_RESOURCE_ID" \
  MI_OBJECT_ID="$MI_OBJECT_ID" \
  PLUGIN_SOURCE="${PLUGIN_SOURCE:-direct}" \
  TASK_IDS="$TASK_IDS" \
  COST_OPT_CRON="$COST_OPT_CRON" \
  bash "$INSTALLER"

say "FinOps SRE Agent ready"
printf '  • Portal : https://portal.azure.com/#@/resource%s\n' "$AGENT_RESOURCE_ID"
printf '  • Agent  : https://sre.azure.com  (select %s)\n' "$SRE_AGENT_NAME"
printf '  • Next   : run a task once from Scheduled tasks > Run now to create the Live Report immediately.\n'
