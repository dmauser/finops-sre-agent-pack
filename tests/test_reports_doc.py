"""Keep docs/REPORTS.md in sync with the thresholds and weights the skills actually use."""

import importlib.util
import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "plugins" / "finops" / "skills"
DOC = (ROOT / "docs" / "REPORTS.md").read_text(encoding="utf-8")


def _load(skill, module):
    path = SKILLS / skill / f"{module}.py"
    spec = importlib.util.spec_from_file_location(f"_reports_doc_{module}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fmt(value):
    value = float(value)
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _default(func, name):
    return inspect.signature(func).parameters[name].default


def test_reliability_weights_match_doc():
    rel = _load("finops-cost-vs-reliability", "reliability")
    for sev, weight in rel.SEV_WEIGHTS.items():
        sev_label = sev if str(sev).lower().startswith("sev") else f"Sev{sev}"
        assert f"Azure Monitor alert, {sev_label} | {_fmt(weight)} |" in DOC, sev
    assert f"unknown severity | {_fmt(rel.UNKNOWN_SEVERITY_WEIGHT)} |" in DOC
    assert f"(Unavailable or Degraded) | {_fmt(rel.HEALTH_EVENT_WEIGHT)} each |" in DOC
    assert f"High Availability recommendation | {_fmt(rel.ADVISOR_HA_WEIGHT)} each |" in DOC


def test_reliability_bands_and_thresholds_match_doc():
    rel = _load("finops-cost-vs-reliability", "reliability")
    assert f"High: {_fmt(rel.RISK_HIGH)} or more" in DOC
    assert f"Medium: {_fmt(rel.RISK_MEDIUM)} to under {_fmt(rel.RISK_HIGH)}" in DOC
    assert f"Blank under ${_fmt(rel._MIN_PAIN_COST)} a month" in DOC
    assert f"Score of {_fmt(rel.RISK_HIGH)} or more on a resource under ${_fmt(rel.LOW_SPEND_USD)} a month" in DOC
    assert f"${rel.HIGH_SPEND_USD:,.0f} or more a month with a score of 0" in DOC


def test_anomaly_defaults_match_doc():
    det = _load("finops-cost-anomaly-detection", "detect").detect_anomalies
    line = next(l for l in DOC.splitlines() if l.startswith("| **Spike** |"))
    assert f"{_fmt(_default(det, 'baseline_days'))}-day average" in line
    assert f"plus {_fmt(_default(det, 'k'))} standard deviations" in line
    assert f"{_fmt(_default(det, 'wow_ratio'))}×" in line
    assert f"at least ${_fmt(_default(det, 'min_delta_usd'))}" in line


def test_rightsizing_defaults_match_doc():
    rs = _load("finops-rightsizing-advisor", "rightsize")
    days = _fmt(rs.DEFAULT_MIN_SAMPLE_DAYS)
    assert f"below {_fmt(rs.DEFAULT_CPU_IDLE_PCT)}% over at least {days} days" in DOC
    assert f"below {_fmt(rs.DEFAULT_CPU_UNDERUTIL_PCT)}% over at least {days} days" in DOC
    assert f"about {_fmt(rs.DEFAULT_RIGHTSIZE_SAVINGS_FRACTION * 100)}% of current cost" in DOC
    assert f"Items below ${_fmt(rs.DEFAULT_MIN_MONTHLY_SAVINGS_USD)} a month" in DOC


def test_commitment_and_waste_defaults_match_doc():
    analyze = _load("finops-commitment-advisor", "commit").analyze_commitments
    assert f"utilization below {_fmt(_default(analyze, 'low_utilization_pct'))}%" in DOC
    assert f"Ends within {_fmt(_default(analyze, 'expiry_window_days'))} days" in DOC
    find_waste = _load("finops-waste-finder", "waste").find_waste
    assert f"Snapshots older than {_fmt(_default(find_waste, 'snapshot_age_days'))} days" in DOC


def test_sre_agent_always_on_rate_matches_doc():
    sre = _load("finops-sre-agent", "sreagent")
    assert f"a fixed {_fmt(sre.ALWAYS_ON_AAU_PER_HOUR)} AAUs per agent-hour" in DOC


KPI_ROWS = {
    "allocated_pct": "Cost allocated to an owner",
    "untagged_pct": "Untagged spend",
    "tag_hygiene_pct": "Tag hygiene issues",
    "budget_coverage_pct": "Managed scopes with a budget",
    "budgets_over": "Budgets over",
    "forecast_vs_budget_pct": "Forecast vs budget",
    "commitment_coverage_pct": "Commitment coverage",
    "commitment_utilization_pct": "Commitment utilization",
    "waste_pct": "Waste as share of spend",
    "anomaly_impact_pct": "Anomaly impact as share of spend",
    "unit_cost_change_pct": "Unit cost change",
    "forecast_accuracy_pct": "Forecast accuracy (error)",
}


def test_kpi_targets_match_doc():
    targets = _load("finops-kpi-scorecard", "scorecard").DEFAULT_TARGETS
    assert set(targets) == set(KPI_ROWS), "new KPI: add a row to docs/REPORTS.md and KPI_ROWS"
    for key, label in KPI_ROWS.items():
        row = next((l for l in DOC.splitlines() if l.startswith(f"| {label} |")), None)
        assert row, f"missing KPI row: {label}"
        cells = [c.strip() for c in row.strip("|").split("|")]
        nums = [re.findall(r"\d+(?:\.\d+)?", c) for c in cells[1:3]]
        assert nums == [[_fmt(targets[key]["target"])], [_fmt(targets[key]["warn"])]], label


def test_maturity_thresholds_match_doc():
    src = inspect.getsource(_load("finops-kpi-scorecard", "scorecard"))
    run, walk = (int(m) for m in re.findall(r"elif score >= (\d+):", src)[:2])
    assert f"**Run** at {run}% or more, **Walk** at {walk}% or more" in DOC
