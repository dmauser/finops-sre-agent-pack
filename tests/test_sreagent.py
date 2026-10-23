"""Layer-1 unit tests for the finops-sre-agent skill (offline, deterministic)."""

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "plugins" / "finops" / "skills"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sre = _load("sreagent", SKILLS / "finops-sre-agent" / "sreagent.py")
models = _load("finops_models_sre", SKILLS / "finops-report-renderer" / "models.py")
render = _load("finops_render_sre", SKILLS / "finops-report-renderer" / "render.py")
summarize = _load("finops_summarize_sre", SKILLS / "finops-cost-optimization-report" / "summarize.py")

SUB = "/subscriptions/000/resourceGroups/sre/providers/Microsoft.App/agents/"
OPS, LAB, GONE, TRIAL = (SUB + n for n in ("ops-agent", "lab-agent", "gone-agent", "trial-agent"))


def _line(rid, day, aau, rate=0.1, **extra):
    row = {
        "date": day, "cost": round(aau * rate, 4), "quantity": aau, "resourceId": rid,
        "consumedService": "Microsoft.App", "meterName": "SRE Agent Unit",
    }
    row.update(extra)
    return row


def _usage(*rows):
    return {"value": [
        {"date": d, "source": s, "threadId": t, "value": v, "name": {"value": "AgentUnits"}}
        for d, s, t, v in rows
    ]}


def _hint_types(result):
    return {h["type"] for h in result["hints"]}


def test_is_sre_agent_line_matches_resource_id_or_meter():
    assert sre.is_sre_agent_line({"instanceName": OPS})
    assert sre.is_sre_agent_line({"consumedService": "microsoft.app", "meterName": "SRE Agent Unit"})
    assert not sre.is_sre_agent_line({"consumedService": "Microsoft.App", "meterName": "vCPU Duration"})
    assert not sre.is_sre_agent_line({"resourceId": "/subscriptions/0/providers/Microsoft.Compute/virtualMachines/v"})
    assert not sre.is_sre_agent_line(None)


def test_measured_split_derives_always_on_from_billed_minus_active():
    lines = [_line(OPS, "2026-07-30", 161.9), _line(OPS, "2026-07-31", 96.0)]
    lines.append({"consumedService": "Microsoft.Compute", "cost": 50.0, "date": "2026-07-31"})
    result = sre.analyze_sre_agent_costs(
        lines,
        [{"id": OPS, "name": "ops-agent", "powerState": "Running"}],
        {OPS: _usage(("2026-07-30", "Thread", "t1", 65.9), ("2026-06-01", "Thread", "old", 999.0))},
    )
    agent = result["by_agent"][0]
    assert agent["split"] == "measured"
    assert agent["billed_aau"] == 257.9
    assert agent["active_aau"] == 65.9
    assert agent["always_on_aau"] == 192.0
    assert result["total_usd"] == pytest.approx(25.79)
    assert result["always_on_usd"] + result["active_usd"] == pytest.approx(result["total_usd"], abs=0.02)
    assert result["by_source"] == [{
        "source": "thread", "label": "Chat / threads", "aau": 65.9, "usd": 6.59,
        "pct": 100.0, "thread_count": 1,
    }]
    assert result["usd_per_aau"] == 0.1
    assert result["always_on_monthly_usd_per_agent"] == 292.0
    assert result["window_days"] == 2


def test_active_is_capped_at_billed_and_estimated_split_without_usage():
    result = sre.analyze_sre_agent_costs(
        [_line(OPS, "2026-07-31", 50.0), _line(GONE, "2026-07-31", 120.0)],
        [{"id": OPS, "name": "ops-agent", "powerState": "Running"}],
        {OPS: _usage(("2026-07-31", "Thread", "t1", 80.0))},
    )
    rows = {r["agentName"]: r for r in result["by_agent"]}
    assert rows["ops-agent"]["active_aau"] == 50.0
    assert rows["ops-agent"]["always_on_aau"] == 0.0
    assert rows["gone-agent"]["split"] == "estimated"
    assert rows["gone-agent"]["status"] == "deleted"
    assert rows["gone-agent"]["always_on_aau"] == 96.0
    assert rows["gone-agent"]["active_aau"] == 24.0
    assert any("estimated" in w for w in result["warnings"])
    assert "deleted_agent_billed" in _hint_types(result)


def test_stopped_agent_still_paying_always_on_is_flagged():
    result = sre.analyze_sre_agent_costs(
        [_line(LAB, "2026-07-31", 96.0)],
        [{"id": LAB, "name": "lab-agent", "properties": {"powerState": "Stopped"}}],
        {LAB: _usage()},
    )
    hint = next(h for h in result["hints"] if h["type"] == "stopped_agent_billed")
    assert hint["target"] == "lab-agent"
    assert hint["monthly_usd"] == 292.0
    assert "idle_agent" not in _hint_types(result)


def test_idle_agents_and_consolidation_candidate():
    days = ("2026-07-29", "2026-07-30", "2026-07-31")
    lines = [_line(rid, d, 100.0) for d in days for rid in (OPS, LAB)]
    agents = [
        {"id": OPS, "name": "ops-agent", "powerState": "Running"},
        {"id": LAB, "name": "lab-agent", "powerState": "Running"},
    ]
    usage = {OPS: _usage(*((d, "Thread", "a", 4.0) for d in days)), LAB: _usage()}
    result = sre.analyze_sre_agent_costs(lines, agents, usage)
    types = [h["type"] for h in result["hints"]]
    assert types.count("idle_agent") == 2
    consolidation = next(h for h in result["hints"] if h["type"] == "consolidation_candidate")
    assert consolidation["target"] == "2 agents"
    assert consolidation["monthly_usd"] == 292.0


def test_limit_risk_projection_and_headroom():
    monthly = {
        OPS: {"value": [{"name": {"value": "TotalAgentUnits"}, "currentValue": 900.0, "limit": 1000.0}]},
        LAB: {"currentValue": 100.0, "limit": 10000.0},
    }
    result = sre.analyze_sre_agent_costs(
        [_line(OPS, "2026-07-10", 196.0), _line(LAB, "2026-07-10", 196.0)],
        [{"id": OPS, "name": "ops-agent"}, {"id": LAB, "name": "lab-agent"}],
        {OPS: _usage(("2026-07-10", "ScheduledTask", "s", 100.0)),
         LAB: _usage(("2026-07-10", "Incident", "i", 100.0))},
        monthly,
        as_of="2026-07-10",
    )
    rows = {r["agentName"]: r for r in result["by_agent"]}
    assert rows["ops-agent"]["projected_month_active_aau"] == 2790.0
    assert rows["ops-agent"]["projected_limit_pct"] == 279.0
    assert rows["lab-agent"]["projected_limit_pct"] == 3.1
    types = _hint_types(result)
    assert {"limit_risk", "limit_headroom_review", "scheduled_task_heavy", "incident_heavy"} <= types


def test_trial_waiver_and_premium_model_hints():
    lines = [_line(TRIAL, "2026-07-31", 1500.0)]
    agents = [{"id": TRIAL, "name": "trial-agent", "powerState": "Running",
               "defaultModel": {"provider": "Anthropic", "name": "claude-opus"}}]
    usage = {TRIAL: _usage(("2026-07-31", "Thread", "t", 1500.0))}
    result = sre.analyze_sre_agent_costs(lines, agents, usage)
    row = result["by_agent"][0]
    assert row["always_on_waived"] is True
    assert row["model"] == "Anthropic / claude-opus"
    assert {"always_on_waived_verify", "model_choice_verify"} <= _hint_types(result)
    assert "idle_agent" not in _hint_types(result)


def test_top_threads_skip_zero_guid_and_rank_by_aau():
    zero = "00000000-0000-0000-0000-000000000000"
    result = sre.analyze_sre_agent_costs(
        [_line(OPS, "2026-07-31", 200.0)],
        [{"id": OPS, "name": "ops-agent"}],
        {OPS: [
            {"date": "2026-07-31", "source": "IncidentAnalysis", "threadId": zero, "value": 30.0},
            {"date": "2026-07-31", "source": "Thread", "threadId": "small", "value": 5.0},
            {"date": "2026-07-31", "source": "ScheduledTask", "threadId": "big", "value": 60.0},
        ]},
    )
    assert [t["threadId"] for t in result["top_threads"]] == ["big", "small"]
    analysis = next(s for s in result["by_source"] if s["source"] == "incidentanalysis")
    assert analysis["thread_count"] == 0


def test_no_inventory_does_not_mark_billed_agents_deleted():
    result = sre.analyze_sre_agent_costs([_line(OPS, "2026-07-31", 96.0)])
    assert result["by_agent"][0]["status"] == "unknown"
    assert "deleted_agent_billed" not in _hint_types(result)
    assert result["inventory_count"] == 0


def test_empty_input_is_well_formed():
    result = sre.analyze_sre_agent_costs([])
    assert result["total_usd"] == 0
    assert result["by_agent"] == [] and result["hints"] == []
    assert result["window_start"] is None


def test_deterministic_and_json_serializable():
    args = (
        [_line(OPS, "2026-07-31", 150.0), _line(LAB, "2026-07-31", 96.0)],
        [{"id": LAB, "name": "lab-agent", "powerState": "Stopped"}],
        {OPS: _usage(("2026-07-31", "Thread", "t", 54.0))},
    )
    first = sre.analyze_sre_agent_costs(*args, as_of="2026-07-31")
    assert first == sre.analyze_sre_agent_costs(*args, as_of="2026-07-31")
    json.dumps(first)


def test_report_adapter_renders_static_html(tmp_path):
    result = sre.analyze_sre_agent_costs(
        [_line(OPS, "2026-07-30", 196.0), _line(LAB, "2026-07-31", 96.0)],
        [{"id": OPS, "name": "ops-agent", "powerState": "Running", "monthlyAgentUnitLimit": 10000},
         {"id": LAB, "name": "lab-agent", "powerState": "Stopped"}],
        {OPS: _usage(("2026-07-30", "ScheduledTask", "s", 100.0))},
        {OPS: {"currentValue": 100.0, "limit": 10000.0}},
        as_of="2026-07-31",
    )
    model = models.build_sre_agent_spend_model(
        result, refreshed_at="2026-08-01T00:00:00Z", scope_summary=["/subscriptions/000"]
    )
    assert model["title"] == "FinOps: SRE Agent Spend"
    assert model["chart"]["type"] == "line"
    assert model["chart"]["labels"] == ["2026-07-30", "2026-07-31"]
    sections = {s["title"]: s for s in model["sections"]}
    assert [r[0] for r in sections["By agent"]["rows"]] == ["ops-agent", "lab-agent"]
    actions = {r[0]: r[4] for r in sections["Where to look first"]["rows"]}
    assert actions["stopped_agent_billed"] == "review"
    assert model == models.build_sre_agent_spend_model(
        result, refreshed_at="2026-08-01T00:00:00Z", scope_summary=["/subscriptions/000"]
    )
    path = render.write_report(model, str(tmp_path / "sre.html"))
    html = Path(path).read_text(encoding="utf-8")
    assert "FinOps: SRE Agent Spend" in html and "lab-agent" in html


def test_report_adapter_marks_verify_hints_and_handles_empty():
    model = models.build_sre_agent_spend_model(
        sre.analyze_sre_agent_costs([]), refreshed_at="2026-08-01T00:00:00Z"
    )
    hints = next(s for s in model["sections"] if s["title"] == "Where to look first")
    assert hints["rows"] == []
    assert "No SRE Agent spend" in hints["emptyMessage"]
    for hint in ("always_on_waived_verify", "deleted_agent_billed", "model_choice_verify"):
        assert hint in models._VERIFY_HINTS


def _idle_pair():
    days = ("2026-07-29", "2026-07-30", "2026-07-31")
    lines = [_line(rid, d, 100.0) for d in days for rid in (OPS, LAB)]
    agents = [
        {"id": OPS, "name": "ops-agent", "powerState": "Running"},
        {"id": LAB, "name": "lab-agent", "powerState": "Running"},
    ]
    return sre.analyze_sre_agent_costs(lines, agents, {OPS: _usage(), LAB: _usage()})


def test_cost_optimization_rollup_dedupes_sre_savings_and_ranks_hints():
    sre_result = _idle_pair()
    out = summarize.summarize_optimization(sre_agents=sre_result, as_of=date(2026, 8, 1))
    head = out["headline"]
    assert head["sre_agent_spend_usd"] == pytest.approx(sre_result["total_usd"])
    # two idle agents (2 x 292) beat one consolidation (292); never both
    assert head["sre_agent_monthly_savings"] == pytest.approx(584.0)
    assert head["potential_monthly_savings"] == 0
    block = out["sre_agents"]
    assert block["agent_count"] == 2
    assert "ranked_hints" not in block
    sre_items = [p for p in out["priorities"] if p["category"] == "sre_agent"]
    assert sre_items and all(p["impact_type"] == "savings" for p in sre_items)
    json.dumps(out)


def test_cost_optimization_rollup_is_absent_without_sre_input():
    out = summarize.summarize_optimization(as_of=date(2026, 8, 1))
    assert "sre_agents" not in out
    assert "sre_agent_spend_usd" not in out["headline"]


def test_sre_rollup_renders_in_cost_optimization_and_monthly_models():
    sre_result = _idle_pair()
    out = summarize.summarize_optimization(sre_agents=sre_result, as_of=date(2026, 8, 1))
    ctx = {"refreshed_at": "2026-08-01T00:00:00Z"}
    model = models.build_cost_optimization_model(out, **ctx)
    titles = [s["title"] for s in model["sections"]]
    assert "SRE Agent spend" in titles and "SRE Agent findings" in titles
    assert any(m["label"] == "SRE Agent spend" for m in model["metrics"])

    monthly = models.build_monthly_review_model(
        {"scorecard": {}, "sre_agents": sre_result}, **ctx)
    assert "SRE Agent spend" in [s["title"] for s in monthly["sections"]]
    plain = models.build_monthly_review_model({"scorecard": {}}, **ctx)
    assert "SRE Agent spend" not in [s["title"] for s in plain["sections"]]
