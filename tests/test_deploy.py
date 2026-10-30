import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FINOPS = ROOT / "plugins" / "finops"
DEPLOY = FINOPS / "deploy"
INSTALLER = FINOPS / "install-api.sh"
TASK_RENDERER = FINOPS / "scheduled-tasks" / "render_tasks.py"
ONBOARDING = ROOT / "docs" / "ONBOARDING.md"
AGENT_ID = "/subscriptions/s/resourceGroups/r/providers/Microsoft.App/agents/a"


def _renderer(monkeypatch):
    spec = importlib.util.spec_from_file_location("finops_task_renderer_deploy", TASK_RENDERER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("AGENT_RESOURCE_ID", AGENT_ID)
    monkeypatch.setenv("TASK_AGENT_NAME", "finops-investigator")
    return module


def test_select_tasks_defaults_to_all(monkeypatch):
    module = _renderer(monkeypatch)
    tasks = module.load_manifest()
    monkeypatch.delenv("TASK_IDS", raising=False)
    assert module.select_tasks(tasks) == tasks
    assert module.select_tasks(tasks, "all") == tasks
    assert module.select_tasks(tasks, "ALL") == tasks


def test_select_tasks_subset_and_unknown(monkeypatch):
    module = _renderer(monkeypatch)
    tasks = module.load_manifest()
    chosen = module.select_tasks(tasks, " cost-optimization-report , cost-anomaly ")
    assert {task["id"] for task in chosen} == {"cost-optimization-report", "cost-anomaly"}
    with pytest.raises(ValueError, match="Unknown TASK_IDS: nope"):
        module.select_tasks(tasks, "cost-anomaly,nope")


def test_install_plan_and_api_respect_task_ids(tmp_path, monkeypatch):
    module = _renderer(monkeypatch)
    monkeypatch.setenv("TASK_IDS", "cost-optimization-report")
    monkeypatch.setenv("COST_OPT_CRON", "0 9 * * 1")
    existing = tmp_path / "existing.json"
    existing.write_text(json.dumps({"value": []}))
    plan = tmp_path / "plan"

    module.write_install_plan(existing, plan)

    rows = (plan / "task-plan.tsv").read_text().splitlines()
    assert len(rows) == 1
    assert "FinOps: Cost Optimization (Live Report, Weekly)" in rows[0]
    assert (plan / "task-count.txt").read_text().strip() == "1"
    payloads = module.render_api_payloads()
    assert [p["name"] for p in payloads] == ["FinOps: Cost Optimization (Live Report, Weekly)"]
    assert payloads[0]["cronExpression"] == "0 9 * * 1"
    assert payloads[0]["agent"] == "finops-investigator"


def test_installer_supports_direct_plugin_install():
    script = INSTALLER.read_text()
    assert 'PLUGIN_SOURCE="${PLUGIN_SOURCE:-marketplace}"' in script
    assert "/api/v2/plugins/install-direct" in script
    assert "autoUpdate" in script
    assert "task-count.txt" in script
    assert 'TASK_IDS="${TASK_IDS:-all}"' in script


def test_deploy_script_defaults_and_handoff():
    script = (DEPLOY / "deploy.sh").read_text()
    assert 'DEFAULT_AGENT_NAME="sre-finops-demo"' in script
    assert 'DEFAULT_LOCATION="centralus"' in script
    assert 'DEFAULT_RESOURCE_GROUP="SRE-FinOps-Demo"' in script
    # The manifest defaultCron is the single schedule source; deploy.sh must not override it.
    assert 'DEFAULT_COST_OPT_CRON' not in script
    assert 'COST_OPT_CRON="${COST_OPT_CRON:-}"' in script
    # install-api.sh treats AGENT_NAME as a task-agent alias; it must not leak in.
    assert "env -u AGENT_NAME" in script
    assert 'PLUGIN_SOURCE="${PLUGIN_SOURCE:-direct}"' in script
    # Only the three documented prompts.
    assert len(re.findall(r"^ask [A-Z_]+ \"", script, re.MULTILINE)) == 3


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_deploy_scripts_parse():
    for path in (DEPLOY / "deploy.sh", INSTALLER):
        subprocess.run(["bash", "-n", str(path)], check=True)


def test_bicep_grants_read_only_finops_roles():
    main = (DEPLOY / "main.bicep").read_text()
    agent = (DEPLOY / "agent.bicep").read_text()
    for role in (
        "acdd72a7-3385-48ef-bd42-f606fba81ae7",  # Reader
        "72fafb9e-0641-4937-9268-a91bfd8191a3",  # Cost Management Reader
        "73c42c96-874c-492b-b04d-ab87d138a893",  # Log Analytics Reader
        "43d0d8ad-25c7-4714-9337-8ba259a9fe05",  # Monitoring Reader
    ):
        assert role in main
    assert "749f88d5-cbae-40b8-bcfc-e573ddc772fa" not in main  # No subscription monitoring writes
    assert "e79298df-d852-4c6d-84f9-5d13249d1e55" in agent  # SRE Agent Administrator
    assert "Microsoft.App/agents@" in agent
    for forbidden in ("8e3af657-a8ff-443c-a75c-2fe8c4bcb635", "b24988ac-6180-42a0-ab88-20f7382dd24c"):
        assert forbidden not in main + agent  # Owner / Contributor never granted


def test_onboarding_doc_covers_both_paths():
    doc = ONBOARDING.read_text(encoding="utf-8")
    assert "## Option A: Automated" in doc
    assert "## Option B: Manual (portal)" in doc
    assert "deploy/deploy.sh" in doc
    assert "Autonomous" in doc and "finops-investigator" in doc
    assert "Install from URL" in doc
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/ONBOARDING.md" in readme
