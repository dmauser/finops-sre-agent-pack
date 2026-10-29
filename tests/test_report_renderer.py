"""Deterministic static FinOps report renderer tests."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


PATH = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "finops"
    / "skills"
    / "finops-report-renderer"
    / "render.py"
)
SPEC = importlib.util.spec_from_file_location("finops_report_renderer", PATH)
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)

MODELS_PATH = PATH.with_name("models.py")
MODELS_SPEC = importlib.util.spec_from_file_location("finops_report_models", MODELS_PATH)
models = importlib.util.module_from_spec(MODELS_SPEC)
MODELS_SPEC.loader.exec_module(models)

REFRESHED_AT = "2026-08-07T01:30:00Z"
SCOPE = "/subscriptions/sub/resourceGroups/managed"
EVIL = '<script>alert("owned")</script>'


def _model():
    return {
        "title": "FinOps: Cost Overview",
        "description": "Daily snapshot <safe>",
        "refreshedAt": "2026-08-07T01:30:00Z",
        "scopeSummary": ["/subscriptions/sub/resourceGroups/managed"],
        "partial": True,
        "warnings": ["Billing is still settling."],
        "metrics": [
            {
                "label": "30-day spend",
                "value": "$123.45",
                "detail": "ActualCost",
                "tone": "warning",
            }
        ],
        "chart": {
            "type": "line",
            "label": "Daily cost",
            "labels": ["Aug 1", "Aug 2"],
            "values": [12.3, 14.8],
        },
        "sections": [
            {
                "title": "Top services",
                "description": "Ranked by cost.",
                "columns": ["Service", "Cost"],
                "rows": [["Storage<script>", "$80.00"]],
            }
        ],
    }


def test_render_is_deterministic_secure_and_static():
    first = renderer.render_report(_model())
    second = renderer.render_report(_model())

    assert first == second
    assert "Content-Security-Policy" in first
    assert "nonce=\"{REPORT_NONCE}\"" in first
    assert "sha384-vsrfeLOOY6KuIYKDlmVH5UiBmgIdB1oEf7p01YgWHuqmOHfZr374+odEv96n9tNC" in first
    assert "window.sreagent" not in first
    assert "innerHTML" not in first
    assert "Storage&lt;script&gt;" in first
    assert "Daily snapshot &lt;safe&gt;" in first
    assert "Billing is still settling." in first


def test_render_handles_empty_sections_and_no_chart():
    model = {
        "title": "Empty",
        "refreshedAt": "2026-08-07T01:30:00Z",
        "sections": [
            {
                "title": "Rows",
                "columns": ["Name"],
                "rows": [],
                "emptyMessage": "Nothing found.",
            }
        ],
    }
    output = renderer.render_report(model)

    assert "Nothing found." in output
    assert "chart.umd.min.js" not in output
    assert "No headline metrics were produced." in output


def test_render_rejects_invalid_shapes():
    with pytest.raises(ValueError, match="title is required"):
        renderer.render_report({"refreshedAt": "now"})
    with pytest.raises(ValueError, match="equal length"):
        renderer.render_report(
            {
                "title": "Bad",
                "refreshedAt": "now",
                "chart": {"label": "x", "labels": ["a"], "values": []},
            }
        )


def test_write_report_creates_file(tmp_path):
    path = tmp_path / "nested" / "report.html"
    assert renderer.write_report(_model(), path) == str(path)
    assert path.read_text().startswith("<!doctype html>")


def _context():
    return {
        "refreshed_at": REFRESHED_AT,
        "scope_summary": [SCOPE],
        "partial": True,
        "warnings": ["Scope z failed.", "Billing is settling."],
        "scope_coverage": [
            {
                "scope": SCOPE,
                "status": "partial",
                "included": 4,
                "excluded": 2,
                "unattributed": 1,
                "cost_usd": 30.03,
                "partial": True,
                "detail": EVIL,
            }
        ],
    }


def _overview():
    return {
        "total_usd": 30.03,
        "daily": [
            {"date": "2026-08-02", "cost_usd": 20.02},
            {"date": "2026-08-01", "cost_usd": 10.01},
        ],
        "by_service": [
            {"service": EVIL, "cost_usd": 15.015},
            {"service": "Storage", "cost_usd": 15.015},
        ],
        "by_resource_group": [
            {"resource_group": "rg-b", "cost_usd": 10},
            {"resource_group": "rg-a", "cost_usd": 20.03},
        ],
    }


def _rightsizing():
    return [
        {
            "resourceId": f"{SCOPE}/providers/Microsoft.Compute/virtualMachines/{EVIL}",
            "resourceType": "microsoft.compute/virtualmachines",
            "kind": "oversized",
            "currentSku": "D4",
            "recommendedAction": "Resize",
            "currentMonthlyUsd": 1.0,
            "estMonthlySavingsUsd": 0.1,
            "validated": True,
            "evidence": ["p95 CPU 4%"],
            "sources": ["azure-monitor"],
        },
        {
            "resourceId": f"{SCOPE}/providers/Microsoft.Compute/virtualMachines/vm-b",
            "resourceType": "microsoft.compute/virtualmachines",
            "kind": "advisor",
            "currentSku": "D2",
            "recommendedAction": "Verify and resize",
            "currentMonthlyUsd": 2.0,
            "estMonthlySavingsUsd": 0.2,
            "validated": None,
            "evidence": ["Advisor only"],
            "sources": ["advisor"],
        },
        {
            "resourceId": f"{SCOPE}/providers/Microsoft.Storage/storageAccounts/review",
            "resourceType": "microsoft.storage/storageaccounts",
            "kind": "review",
            "currentSku": "",
            "recommendedAction": "Review cost",
            "currentMonthlyUsd": 100,
            "estMonthlySavingsUsd": None,
            "validated": None,
            "evidence": [],
            "sources": ["cost"],
        },
    ]


def _budgets():
    return {
        "as_of": "2026-08-06",
        "budget_count": 2,
        "budgets": [
            {
                "name": "safe",
                "scope": SCOPE,
                "amount": 1000,
                "currency": "USD",
                "current_spend": 0,
                "pct_used": 0,
                "forecast_spend": 0,
                "forecast_source": "run-rate",
                "pct_forecast": 0,
                "status": "on_track",
                "breached_notifications": [],
                "mtd_crosscheck": None,
            },
            {
                "name": EVIL,
                "scope": SCOPE,
                "amount": 100,
                "currency": "USD",
                "current_spend": 110,
                "pct_used": 110,
                "forecast_spend": 150,
                "forecast_source": "azure",
                "pct_forecast": 150,
                "status": "over_budget",
                "breached_notifications": [
                    {"name": "actual_100", "threshold": 100, "type": "actual"}
                ],
                "mtd_crosscheck": None,
            },
        ],
        "summary": {
            "total_amount": 1100,
            "total_current": 110,
            "total_forecast": 150,
            "over_budget": 1,
            "forecast_over": 0,
            "at_risk": 0,
            "on_track": 1,
        },
        "gates": [{"name": EVIL, "reason": "current spend is over", "status": "over_budget"}],
        "no_budgets": False,
    }


def _optimization():
    finding = _rightsizing()[1]
    return {
        "as_of": "2026-08-06",
        "headline": {
            "total_monthly_spend": 500,
            "potential_monthly_savings": 0.2,
            "anomaly_count": 1,
            "top_anomaly_impact_usd": 90,
            "budgets_over": 0,
            "budgets_forecast_over": 1,
            "budgets_at_risk": 0,
            "untagged_usd": 40,
            "unallocated_pct": 8,
        },
        "priorities": [
            {
                "rank": 2,
                "category": "rightsizing",
                "impact_type": "savings",
                "impact_usd": 0.2,
                "title": "Resize vm-b",
                "detail": "advisor",
                "action": "Verify and resize",
                "validated": None,
            },
            {
                "rank": 1,
                "category": "budget",
                "impact_type": "overrun",
                "impact_usd": 200,
                "title": EVIL,
                "detail": "forecast over",
                "action": "Review",
                "validated": None,
            },
        ],
        "rightsizing": {
            "potential_monthly_savings": 0.2,
            "count": 1,
            "top": [finding],
        },
        "anomalies": {
            "count": 1,
            "top_impact_usd": 90,
            "top": [
                {
                    "kind": "spike",
                    "dimension": "service",
                    "value": "Compute",
                    "current_usd": 100,
                    "baseline_mean_usd": 10,
                    "impact_usd": 90,
                }
            ],
        },
        "budgets": {
            "no_budgets": False,
            "summary": {},
            "over": [],
            "forecast_over": [
                {
                    "name": "prod",
                    "status": "forecast_over",
                    "currency": "USD",
                    "current_spend": 800,
                    "amount": 1000,
                    "forecast_spend": 1200,
                    "forecast_source": "run-rate",
                }
            ],
            "at_risk": [],
        },
        "governance": {
            "untagged_usd": 40,
            "unallocated_usd": 40,
            "unallocated_pct": 8,
            "untagged_top": [{"resourceId": EVIL, "monthly_usd": 40}],
            "tag_hygiene": [
                {
                    "canonical": "prod",
                    "variants": {"Prod": 20, "prod": 10},
                    "cost_affected": 30,
                }
            ],
            "budget_gates": [
                {
                    "name": "prod",
                    "status": "forecast_over",
                    "overrun_usd": 200,
                    "currency": "USD",
                }
            ],
        },
    }


def _ai_spend():
    return {
        "as_of": "2026-08-06",
        "total_ai_usd": 30.03,
        "resource_count": 2,
        "model_count": 2,
        "by_service_family": [
            {"service_family": "Machine Learning", "monthly_usd": 10, "pct": 33.3},
            {
                "service_family": "Cognitive Services / OpenAI",
                "monthly_usd": 20.03,
                "pct": 66.7,
            },
        ],
        "by_meter_type": [
            {"meter_type": "compute", "monthly_usd": 0.2, "pct": 0.7},
            {"meter_type": "model_token", "monthly_usd": 0.1, "pct": 0.3},
            {"meter_type": "model_token", "monthly_usd": 19.73, "pct": 65.7},
            {"meter_type": "other_cognitive", "monthly_usd": 10, "pct": 33.3},
        ],
        "by_resource": [
            {
                "resourceId": f"{SCOPE}/providers/Microsoft.CognitiveServices/accounts/a",
                "resourceName": EVIL,
                "kind": "AIServices",
                "service_family": "Cognitive Services / OpenAI",
                "monthly_usd": 20.03,
                "pct": 66.7,
                "top_model": "gpt-4o",
            }
        ],
        "by_model": [
            {"model": "gpt-4o-mini", "monthly_usd": 5, "pct": 25.2, "resource_count": 1},
            {"model": "gpt-4o", "monthly_usd": 14.83, "pct": 74.8, "resource_count": 1},
        ],
        "top_drivers": [
            {
                "resourceName": EVIL,
                "service_family": "Cognitive Services / OpenAI",
                "model": "gpt-4o",
                "meter_type": "model_token",
                "monthly_usd": 14.83,
            }
        ],
        "hints": [
            {
                "type": "commitment_opportunity",
                "target": EVIL,
                "detail": "Evaluate PTU.",
                "monthly_usd": 14.83,
            }
        ],
    }


def _reliability():
    return {
        "as_of": "2026-08-06",
        "total_usd": 1200,
        "resource_count": 2,
        "reliability_signal_count": 4,
        "coverage": {
            "cost_resource_count": 2,
            "reliability_resource_count": 2,
            "joined_resource_count": 1,
            "unmatched_reliability_count": 1,
            "subscription_level_event_count": 1,
        },
        "by_resource": [
            {
                "resourceId": f"{SCOPE}/providers/Microsoft.Compute/virtualMachines/a",
                "resourceName": EVIL,
                "resourceGroup": "managed",
                "service": "Compute",
                "monthly_usd": 1200,
                "pct": 100,
                "alert_count": 2,
                "sev_counts": {"Sev0": 0, "Sev1": 1, "Sev2": 1, "Sev3": 0, "Sev4": 0},
                "sev0": 0,
                "sev1": 1,
                "sev2": 1,
                "sev3": 0,
                "sev4": 0,
                "health_event_count": 1,
                "advisor_reliability_count": 1,
                "reliability_score": 14,
                "pain_per_1000_usd": 11.67,
                "risk_band": "high",
                "primary_signal": "alert",
            }
        ],
        "by_service": [
            {
                "service": "Compute",
                "monthly_usd": 1200,
                "resource_count": 1,
                "alert_count": 2,
                "health_event_count": 1,
                "advisor_reliability_count": 1,
                "reliability_score": 14,
                "pct": 100,
                "risk_band": "high",
            }
        ],
        "top_drivers": [],
        "hints": [
            {
                "type": "high_incident_low_spend",
                "target": "api",
                "detail": "Invest in resilience.",
                "monthly_usd": 50,
                "reliability_score": 12,
            },
            {
                "type": "high_spend_no_pain",
                "target": "batch",
                "detail": "Verify before rightsizing.",
                "monthly_usd": 2000,
                "reliability_score": 0,
            },
        ],
        "unmatched_reliability": [
            {
                "resourceId": f"{SCOPE}/providers/Microsoft.Web/sites/missing",
                "resourceName": "missing",
                "signal_type": "alert",
                "severity": "Sev2",
                "score": 3,
                "date": "2026-08-05",
                "detail": EVIL,
            }
        ],
        "data_quality": {
            "cost_partial": True,
            "sources_used": {
                "usage_details": True,
                "alerts": True,
                "resource_health": False,
                "advisor_highavailability": True,
            },
            "limitations": ["Alerts are weighted counts.", EVIL],
        },
    }


def _monthly():
    return {
        "month": "2026-09",
        "scorecard": {
            "score_pct": 40.0,
            "maturity": "crawl",
            "kpis": [
                {"id": "allocated_pct", "name": "Spend allocated", "capability": "Allocation",
                 "value": 80.0, "unit": "%", "target": 90, "higher_is_better": True,
                 "status": "warn", "action": EVIL, "detail": None},
                {"id": "budget_coverage_pct", "name": "Managed scopes with a budget",
                 "capability": "Budgeting (governance)", "value": 50.0, "unit": "%",
                 "target": 100, "higher_is_better": True, "status": "fail",
                 "action": "Create budgets.",
                 "detail": {"uncovered_scopes": ["/subscriptions/b", "/subscriptions/a"]}},
                {"id": "untagged_pct", "name": "Untagged spend",
                 "capability": "Allocation (tag governance)", "value": 2.0, "unit": "%",
                 "target": 5, "higher_is_better": False, "status": "pass", "action": None,
                 "detail": None},
                {"id": "budgets_over", "name": "Budgets over", "capability": "Budgeting",
                 "value": 0, "unit": "count", "target": 0, "higher_is_better": False,
                 "status": "pass", "action": None, "detail": None},
                {"id": "unit_cost_change_pct", "name": "Unit cost change",
                 "capability": "Unit economics", "value": None, "unit": "%", "target": 0,
                 "higher_is_better": False, "status": "unknown", "action": None,
                 "detail": None},
            ],
            "actions": [
                {"kpi": "allocated_pct", "status": "warn", "action": EVIL},
                {"kpi": "budget_coverage_pct", "status": "fail", "action": "Create budgets."},
            ],
        },
        "forecast": {
            "available": True, "month": "2026-09", "next_month": "2026-10",
            "month_to_date": 2900.0, "days_remaining": 1,
            "month_end_forecast": 3000.0, "month_end_low": 2950.0, "month_end_high": 3050.0,
            "next_month_forecast": 3100.0, "next_month_low": 2800.0, "next_month_high": 3400.0,
            "run_rate_30d": 3000.0, "method": "linear_trend", "confidence": "high",
            "budget_amount": 2800.0, "forecast_vs_budget_pct": 107.1,
            "budget_status": "forecast_over", "warnings": [],
        },
        "month_over_month": {
            "current_usd": 3000.0, "previous_usd": 2500.0, "delta_usd": 500.0, "delta_pct": 20.0,
            "by_owner": [
                {"owner": "web", "previous_usd": 1000, "current_usd": 1300, "delta_usd": 300,
                 "delta_pct": 30.0},
                {"owner": EVIL, "previous_usd": 1500, "current_usd": 1700, "delta_usd": 200,
                 "delta_pct": 13.3},
            ],
            "movers": {
                "new": [{"resourceId": "/s/r/new", "owner": "web", "previous_usd": 0,
                         "current_usd": 200, "delta_usd": 200, "delta_pct": None}],
                "increased": [{"resourceId": "/s/r/up", "owner": EVIL, "previous_usd": 100,
                               "current_usd": 300, "delta_usd": 200, "delta_pct": 200.0}],
                "decreased": [],
                "removed": [{"resourceId": "/s/r/gone", "owner": "web", "previous_usd": 50,
                             "current_usd": 0, "delta_usd": -50, "delta_pct": -100.0}],
            },
        },
        "routing": {
            "owners": [
                {"owner": "web", "total_usd": 60.0, "items": [
                    {"resourceId": "/s/r/a", "source": "waste", "amount_usd": 40.0,
                     "action": "Delete disk"},
                    {"resourceId": "/s/r/b", "source": "rightsizing", "amount_usd": 20.0,
                     "action": EVIL},
                ]},
            ],
            "unrouted": [{"resourceId": "/s/r/z", "source": "waste", "amount_usd": None,
                          "action": None}],
            "unrouted_usd": 0.0,
            "routed_pct": 100.0,
        },
        "unit_economics": {"unit": "request", "per": 1000, "cost_per_unit": 3.0,
                           "previous_cost_per_unit": 3.1, "change_pct": -3.2,
                           "trend": "improving", "interpretation": None, "warnings": []},
    }


def _digest(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _contains_exact(value, wanted):
    if isinstance(value, dict):
        return any(_contains_exact(item, wanted) for item in value.values())
    if isinstance(value, list):
        return any(_contains_exact(item, wanted) for item in value)
    return value == wanted


def _reverse_lists(value):
    copied = copy.deepcopy(value)
    if isinstance(copied, list):
        return list(reversed(copied))
    for key, item in copied.items():
        if isinstance(item, list):
            copied[key] = list(reversed(item))
        elif isinstance(item, dict):
            for child_key, child in item.items():
                if isinstance(child, list):
                    item[child_key] = list(reversed(child))
    return copied


def test_cost_overview_adapter_preserves_partial_scope_and_sorts():
    model = models.build_cost_overview_model(_overview(), **_context())

    assert model["partial"] is True
    assert model["refreshedAt"] == REFRESHED_AT
    assert model["scopeSummary"] == [SCOPE]
    assert model["chart"]["labels"] == ["2026-08-01", "2026-08-02"]
    assert model["sections"][0]["rows"] == [
        [EVIL, "$15.02"],
        ["Storage", "$15.02"],
    ]
    assert model["sections"][-1]["title"] == "Scope coverage"


def test_cost_overview_merges_resource_group_casing_and_labels_short_period():
    overview = _overview()
    overview["by_resource_group"] = [
        {"resource_group": "LAB-VWAN", "cost_usd": 1},
        {"resource_group": "lab-vwan", "cost_usd": 5},
        {"resource_group": "Lab-Vwan", "cost_usd": 1},
        {"resource_group": "rg-a", "cost_usd": 6.5},
        {"resource_group": "", "cost_usd": 0.5},
    ]
    model = models.build_cost_overview_model(overview, **_context())
    assert model["sections"][1]["rows"] == [
        ["lab-vwan", "$7.00"],
        ["rg-a", "$6.50"],
        ["(unattributed)", "$0.50"],
    ]
    assert model["metrics"][0]["label"] == "Period spend"
    assert "2026-08-01 to 2026-08-02 (2 observed days)" in model["metrics"][0]["detail"]

    overview["daily"] = [
        {"date": f"2026-08-{day:02d}", "cost_usd": 1} for day in range(1, 31)
    ]
    assert models.build_cost_overview_model(overview, **_context())["metrics"][0]["label"] == "Period spend"


def test_rightsizing_adapter_uses_decimal_boundaries_and_verify_labels():
    model = models.build_rightsizing_savings_model(_rightsizing(), **_context())

    assert model["metrics"][0]["value"] == "$0.30"
    assert model["metrics"][1]["value"] == "2"
    assert model["metrics"][3]["value"] == "1"
    assert model["chart"]["values"] == [0.2, 0.1]
    review = model["sections"][1]
    assert review["title"] == "High spend, no idle rule yet — review"
    assert review["rows"][0][2] == "$100.00"
    assert all(row[2] != "review" for row in model["sections"][0]["rows"])
    assert model["sections"][0]["rows"][0][7] == "verify first"


def test_budget_adapter_gates_first_and_labels_estimates():
    model = models.build_budget_status_model(_budgets(), **_context())

    assert model["sections"][0]["rows"][0][0] == "over_budget"
    assert model["sections"][1]["rows"][0][0] == EVIL
    assert "Run-rate forecasts are estimates" in " ".join(model["warnings"])
    assert "asynchronously" in " ".join(model["warnings"])
    assert model["chart"]["label"].endswith("(100% = budget limit)")


def test_cost_optimization_adapter_keeps_impact_types_separate():
    model = models.build_cost_optimization_model(_optimization(), **_context())

    priorities = model["sections"][0]["rows"]
    assert priorities[0][2] == "overrun"
    assert priorities[1][2] == "savings"
    assert model["metrics"][1]["value"] == "$0.20"
    assert model["metrics"][4]["value"] == "$40.00"
    assert priorities[1][-1] == "verify first"
    assert "Run-rate budget forecasts are estimates." in model["warnings"]


def test_ai_spend_adapter_keeps_token_and_compute_separate():
    model = models.build_ai_spend_model(_ai_spend(), **_context())

    assert model["metrics"][3]["value"] == "$19.83"
    assert model["metrics"][4]["value"] == "$0.20"
    assert model["chart"]["labels"] == ["gpt-4o", "gpt-4o-mini"]
    assert model["sections"][-3]["rows"][0][-1] == "$14.83"
    assert model["sections"][-2]["rows"][0][-1] == "verify first"


def test_cost_vs_reliability_adapter_preserves_quality_and_signal_fields():
    model = models.build_cost_vs_reliability_model(_reliability(), **_context())

    assert model["partial"] is True
    assert model["metrics"][3]["value"] == "50.0%"
    spend_row = model["sections"][0]["rows"][0]
    assert spend_row[4:8] == ["2", "0/1/1/0/0", "1", "1"]
    assert spend_row[8:] == ["14", "11.67", "high", "alert"]
    assert model["sections"][2]["rows"][0][0] == "high_incident_low_spend"
    assert model["sections"][3]["rows"][0][0] == "high_spend_no_pain"
    assert any(row[0] == "limitation" for row in model["sections"][5]["rows"])
    assert "complete incident system" in " ".join(model["warnings"])


@pytest.mark.parametrize(
    ("builder", "empty"),
    [
        (
            models.build_cost_overview_model,
            {"total_usd": 0, "daily": [], "by_service": [], "by_resource_group": []},
        ),
        (models.build_rightsizing_savings_model, []),
        (
            models.build_budget_status_model,
            {
                "budgets": [],
                "summary": {
                    "total_amount": 0,
                    "total_current": 0,
                    "total_forecast": 0,
                    "over_budget": 0,
                    "forecast_over": 0,
                    "at_risk": 0,
                    "on_track": 0,
                },
                "gates": [],
                "no_budgets": True,
            },
        ),
        (
            models.build_cost_optimization_model,
            {
                "headline": {},
                "priorities": [],
                "rightsizing": {},
                "anomalies": {},
                "budgets": {},
                "governance": {},
            },
        ),
        (
            models.build_ai_spend_model,
            {
                "total_ai_usd": 0,
                "resource_count": 0,
                "model_count": 0,
                "by_service_family": [],
                "by_meter_type": [],
                "by_resource": [],
                "by_model": [],
                "top_drivers": [],
                "hints": [],
            },
        ),
        (
            models.build_cost_vs_reliability_model,
            {
                "total_usd": 0,
                "resource_count": 0,
                "reliability_signal_count": 0,
                "coverage": {},
                "by_resource": [],
                "by_service": [],
                "hints": [],
                "unmatched_reliability": [],
                "data_quality": {},
            },
        ),
        (models.build_monthly_review_model, {"scorecard": {}}),
    ],
)
def test_all_adapters_render_empty_states(builder, empty):
    model = builder(empty, refreshed_at=REFRESHED_AT)
    output = renderer.render_report(model)

    assert output.startswith("<!doctype html>")
    assert "No data points for this period." in output
    assert "class=\"empty\"" in output


@pytest.mark.parametrize(
    ("builder", "fixture", "model_hash", "html_hash"),
    [
        (
            models.build_cost_overview_model,
            _overview,
            "011d37b5366d916a268a1143dd98e24f1830d6aece10b594e4e19a630a96a860",
            "0e0b22a6aa3d2e9f7b73fd07cc4c555cadfa78cfb519e992171cddc455bf50b0",
        ),
        (
            models.build_rightsizing_savings_model,
            _rightsizing,
            "d2c936e6efbfd598cb69b00344648b470bc0974e3b26475aaa5463e2ad598ad5",
            "862371ba132084badf3bab80e7f1c22fa40cf483da597d4ff7a5c7d660244bd3",
        ),
        (
            models.build_budget_status_model,
            _budgets,
            "6ab8e5cb5973920ddd218c0ccefee42d99d80bf88a5f4ffc0a11e62a55d95712",
            "c0c2c522a55d3e3ca1b9a9108e660b3f4068d6e56c0be9ba5d329f173c0a75e0",
        ),
        (
            models.build_cost_optimization_model,
            _optimization,
            "cef81797578790d3fd847be1a1533bdb95a8e4e43b1545aa2e23892548025867",
            "cde0b21317057e23b6ad33801d82e63d17ad76b224f60210b08b327c981a779f",
        ),
        (
            models.build_ai_spend_model,
            _ai_spend,
            "286cf0b030ca6dbb67a46e5b81092e5d301a1dfb5956a9ce5bc213744fb053fa",
            "0838dd47f6ec3ab2becfb5c6eb6e25bdf782122a40d2f714c827f008cfe40049",
        ),
        (
            models.build_cost_vs_reliability_model,
            _reliability,
            "b805021220ffa04a66d6d731c3cd2ab4023f45047b4199a04447e91cb6dd0e24",
            "45c2ece3ddcc520705e26c13694643d66e76a472499d263704f0b967041eb13c",
        ),
        (
            models.build_monthly_review_model,
            _monthly,
            "7fa02703c731bb9a9316203f44b4e4c458fa9373ee8d9e44a45a38a5a2c1395f",
            "46157df9c8d3f21477a400a5a38381d8ceddeea63a0f4d96d5547d55aa5287ec",
        ),
    ],
)
def test_adapter_models_and_html_have_stable_hashes(
    builder, fixture, model_hash, html_hash
):
    original = builder(fixture(), **_context())
    reordered = builder(_reverse_lists(fixture()), **_context())
    rendered = renderer.render_report(original)

    assert _digest(original) == _digest(reordered) == model_hash
    assert hashlib.sha256(rendered.encode()).hexdigest() == html_hash
    assert hashlib.sha256(renderer.render_report(reordered).encode()).hexdigest() == html_hash


@pytest.mark.parametrize(
    ("builder", "fixture"),
    [
        (models.build_cost_overview_model, _overview),
        (models.build_rightsizing_savings_model, _rightsizing),
        (models.build_budget_status_model, _budgets),
        (models.build_cost_optimization_model, _optimization),
        (models.build_ai_spend_model, _ai_spend),
        (models.build_cost_vs_reliability_model, _reliability),
        (models.build_monthly_review_model, _monthly),
    ],
)
def test_malicious_strings_are_unescaped_in_models_and_escaped_at_render(builder, fixture):
    model = builder(fixture(), **_context())
    output = renderer.render_report(model)

    assert _contains_exact(model, EVIL)
    assert EVIL not in output
    assert "&lt;script&gt;alert(&quot;owned&quot;)&lt;/script&gt;" in output


def test_adapters_require_supplied_refresh_and_reject_unsafe_numbers():
    with pytest.raises(ValueError, match="refreshed_at is required"):
        models.build_cost_overview_model({"total_usd": 0}, refreshed_at="")
    with pytest.raises(ValueError, match="finite number"):
        models.build_cost_overview_model(
            {"total_usd": "1.00"}, refreshed_at=REFRESHED_AT
        )
    with pytest.raises(ValueError, match="finite number"):
        models.build_ai_spend_model(
            {
                "total_ai_usd": float("nan"),
                "resource_count": 0,
                "model_count": 0,
            },
            refreshed_at=REFRESHED_AT,
        )


def test_model_adapters_have_no_clock_or_network_access():
    source = MODELS_PATH.read_text()

    assert "datetime" not in source
    assert "date.today" not in source
    assert "socket" not in source
    assert "requests" not in source
    assert "urllib" not in source


def test_skill_documents_all_adapter_apis():
    skill = MODELS_PATH.with_name("SKILL.md").read_text()

    for name in models.__all__:
        assert name in skill
    assert "refreshed_at" in skill
    assert "scope_coverage" in skill
    assert "write_report(model, output_path)" in skill

def test_budget_status_does_not_sum_mixed_currencies():
    result = {
        "budgets": [
            {"name": "usd", "scope": "/subscriptions/a", "currency": "USD", "amount": 100,
             "current_spend": 10, "pct_used": 10, "forecast_spend": 20, "pct_forecast": 20,
             "forecast_source": "azure", "status": "on_track", "breached_notifications": []},
            {"name": "eur", "scope": "/subscriptions/b", "currency": "EUR", "amount": 500,
             "current_spend": 50, "pct_used": 10, "forecast_spend": 100, "pct_forecast": 20,
             "forecast_source": "azure", "status": "on_track", "breached_notifications": []},
        ],
        "summary": {"currency": None, "total_amount": None, "total_current": None,
                    "total_forecast": None, "on_track": 2},
        "gates": [],
    }
    model = models.build_budget_status_model(result, refreshed_at="2026-08-07T01:30:00Z")
    metrics = {m["label"]: m for m in model["metrics"]}
    assert metrics["Portfolio budget"]["value"] == "—"
    assert metrics["Portfolio budget"]["detail"] == "Mixed currencies"
    assert any("more than one currency" in w for w in model["warnings"])


def test_cost_optimization_adapter_adds_waste_and_commitment_sections_only_when_present():
    base = models.build_cost_optimization_model(_optimization(), **_context())
    result = copy.deepcopy(_optimization())
    result["waste"] = {
        "currency": "EUR",
        "potential_monthly_savings": 32.85,
        "top": [{"resourceId": EVIL, "kind": "orphan", "reason": "NAT gateway attached to no subnet",
                 "monthlyCost": 32.85, "estMonthlySavings": 32.85,
                 "recommendedAction": "Delete"}],
    }
    result["commitments"] = {
        "currency": "USD",
        "potential_monthly_savings": 400.0,
        "best_family": "reservation",
        "coverage_pct": 35.0,
        "unused_commitment_cost": 120.0,
        "purchases": [
            {"family": "reservation", "term": "P3Y", "sku": "D4s_v5",
             "est_monthly_savings": 400.0},
            {"family": "savings_plan", "term": "P3Y", "sku": "Compute_Savings_Plan",
             "est_monthly_savings": 350.0},
        ],
        "underutilized": [{"kind": "reservation", "name": "ri1", "utilization": 40.0,
                           "action": "Exchange"}],
        "expiring": [{"kind": "savings_plan", "name": "sp1", "days_to_expiry": 12}],
        "hybrid_benefit": [{"resourceId": "/vm/win", "license": "Windows Server",
                            "monthly_cost": None}],
    }
    model = models.build_cost_optimization_model(result, **_context())

    assert model["metrics"][: len(base["metrics"])] == base["metrics"]
    labels = [m["label"] for m in model["metrics"][len(base["metrics"]):]]
    assert labels == ["Waste cleanup savings", "Rate optimization savings", "Commitment coverage"]
    assert model["metrics"][len(base["metrics"])]["value"] == "32.85 EUR"
    titles = [s["title"] for s in model["sections"]]
    assert titles.index("Waste cleanup") < titles.index("Rate optimization (commitments)")
    assert titles[-1] == base["sections"][-1]["title"]
    rate = next(s for s in model["sections"] if s["title"].startswith("Rate optimization"))
    assert [r[0] for r in rate["rows"]] == [
        "buy (headline)", "buy (alternative)", "underutilized", "expiring", "hybrid benefit"]
    assert rate["rows"][2][3] == "40.0%"
    html = renderer.render_report(model)
    assert EVIL not in html
    assert "Rate optimization (commitments)" in html

def test_ai_spend_adapter_renders_optional_splits_and_new_verify_hints():
    result = _ai_spend()
    result["by_token_direction"] = [
        {"token_direction": "input", "monthly_usd": 15, "pct": 75.6},
    ]
    result["by_deployment_type"] = [
        {"deployment_type": "global", "monthly_usd": 19.83, "pct": 100},
    ]
    result["by_project"] = [{"project": "support-bot", "monthly_usd": 20.03, "pct": 100}]
    result["hints"].append(
        {"type": "prompt_caching_verify", "target": "input", "detail": "Check cache.", "monthly_usd": 15}
    )
    model = models.build_ai_spend_model(result, **_context())

    split = model["sections"][0]["rows"]
    assert ["token direction", "input", "$15.00", "75.6%"] in split
    assert ["deployment type", "global", "$19.83", "100.0%"] in split
    assert ["Foundry project", "support-bot", "$20.03", "100.0%"] in split
    hint_rows = [row for section in model["sections"] for row in section["rows"]
                 if row and row[0] == "prompt_caching_verify"]
    assert hint_rows[0][-1] == "verify first"


def test_monthly_review_adapter_orders_kpis_owners_and_flags_missing_inputs():
    model = models.build_monthly_review_model(_monthly(), **_context())

    assert model["title"] == "FinOps: Monthly Review"
    assert [m["label"] for m in model["metrics"]] == [
        "KPI health", "KPIs passing", "Month-end forecast",
        "Month-over-month change", "Owner-routed findings",
    ]
    assert model["metrics"][1]["value"] == "2 of 4"
    assert model["metrics"][2]["tone"] == "critical"
    sections = {s["title"]: s for s in model["sections"]}
    kpi_rows = sections["KPI scorecard"]["rows"]
    assert [r[4] for r in kpi_rows] == ["fail", "warn", "unknown", "pass", "pass"]
    assert kpi_rows[0][3] == "≥ 100.0%"
    governance = sections["Governance checks"]["rows"]
    assert governance[0][4] == "uncovered_scopes: /subscriptions/a, /subscriptions/b"
    assert sections["Owner actions"]["rows"][-1][0] == "needs an owner tag"
    assert sections["Top resource movers"]["rows"][-1][0] == "removed"
    assert model["chart"]["labels"] == ["web", EVIL]

    bare = models.build_monthly_review_model({"scorecard": {}}, refreshed_at=REFRESHED_AT)
    assert "Forecast was not supplied." in bare["warnings"]
    assert "Month-over-month variance was not supplied." in bare["warnings"]
    assert "Next FinOps iteration" not in [s["title"] for s in bare["sections"]]

    practice = models.build_monthly_review_model(
        {"scorecard": {"next_iteration_focus": ["Allocation", "Forecasting"],
                       "benchmarks": [{"id": "anomaly_impact_usd", "name": "Impact",
                                       "value": 40, "unit": "USD"},
                                      {"id": "anomaly_count", "name": "Count",
                                       "value": 1, "unit": "count"}]}},
        refreshed_at=REFRESHED_AT)
    sections = {s["title"]: s for s in practice["sections"]}
    assert sections["Next FinOps iteration"]["rows"] == [["1", "Allocation"], ["2", "Forecasting"]]
    assert sections["Benchmarks"]["rows"][0] == ["Impact", "$40.00"]
    assert "Forecast record" not in sections


def test_monthly_review_renders_nested_commitment_kpi_without_input_rewrite():
    review = _monthly()
    detail = {"by_meter_category": [
        {"category": EVIL, "coverage_pct": 12.5, "eligible": True},
        {"category": "Virtual Machines", "coverage_pct": None, "notes": ["unknown", "partial"]},
    ]}
    review["scorecard"]["kpis"][0]["detail"] = detail
    model = models.build_monthly_review_model(review, **_context())
    output = renderer.render_report(model)
    assert "by_meter_category:" in output and "coverage_pct: 12.5" in output
    assert "eligible: yes" in output and "notes: partial, unknown" in output
    assert EVIL not in output
    assert review["scorecard"]["kpis"][0]["detail"] == detail
    detail["by_meter_category"].reverse()
    assert renderer.render_report(models.build_monthly_review_model(review, **_context())) == output


def test_monthly_review_forecast_record_round_trips_through_scorecard_parser():
    review = _monthly()
    review["forecast"]["as_of"] = "2026-09-30"
    model = models.build_monthly_review_model(review, **_context())
    record = {s["title"]: s for s in model["sections"]}["Forecast record"]["rows"]
    assert record == [["finops-forecast-record:v1 month=2026-10 forecast=3100.00 as_of=2026-09-30"]]

    spec = importlib.util.spec_from_file_location(
        "scorecard_rt", PATH.parents[1] / "finops-kpi-scorecard" / "scorecard.py")
    scorecard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scorecard)
    assert scorecard.FORECAST_RECORD_PREFIX == models.FORECAST_RECORD_PREFIX
    parsed = scorecard.parse_forecast_record(renderer.render_report(model), "2026-10")
    assert parsed == {"month": "2026-10", "forecast": 3100.0, "as_of": "2026-09-30"}

    review["forecast"]["as_of"] = "not-a-date"
    bad = models.build_monthly_review_model(review, **_context())
    assert "Forecast record" not in [s["title"] for s in bad["sections"]]


@pytest.mark.parametrize("basis", [None, "ActualCost", "AmortizedCost"])
def test_monthly_review_labels_forecast_with_its_own_basis(basis):
    review = _monthly()
    review["forecast"]["context"] = {"cost_basis": basis}
    model = models.build_monthly_review_model(review, **_context())
    section = next(s for s in model["sections"] if s["title"] == "Forecast")
    assert section["description"].endswith(f"; {basis or 'cost basis not supplied'}.")


def test_refreshed_time_is_human_readable_and_chart_titled():
    model = _model()
    model["refreshedAt"] = "2026-10-23T03:47:30.919138+00:00"
    model["chart"] = {"type": "line", "label": "Daily cost (USD)", "labels": [], "values": []}
    output = renderer.render_report(model)
    assert "Last refreshed: 2026-10-23 03:47 UTC" in output
    assert "<h2>Daily cost (USD)</h2>" in output
    assert "No data points for this period." in output
    assert "@media (max-width:640px)" in output
    model["refreshedAt"] = "today"
    assert "Last refreshed: today" in renderer.render_report(model)


def test_table_cells_keep_numbers_whole_and_wrap_long_ids():
    assert renderer._cell("$508.05") == '<td class="num">$508.05</td>'
    assert renderer._cell("98.1%") == '<td class="num">98.1%</td>'
    rid = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.App/agents/a"
    assert renderer._cell(rid).startswith('<td class="long">')
    assert renderer._cell("governance") == "<td>governance</td>"


def test_threshold_cells_stay_on_one_line():
    assert renderer._cell("\u2265 100.0%").startswith('<td class="num">')



def _skill(name, folder, filename):
    path = PATH.parents[1] / folder / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_alloc = _skill("report_allocate", "finops-cost-allocation", "allocate.py")
_waste = _skill("report_waste", "finops-waste-finder", "waste.py")
_commit = _skill("report_commit", "finops-commitment-advisor", "commit.py")
_detect = _skill("report_detect", "finops-cost-anomaly-detection", "detect.py")
_AS_OF = __import__("datetime").date(2026, 8, 1)
_RG = "/subscriptions/sub/resourceGroups/managed/providers"


def _allocation():
    costs = {f"{_RG}/Microsoft.Compute/virtualMachines/vm{i}": 10.0 * i for i in range(1, 6)}
    tags = {
        f"{_RG}/Microsoft.Compute/virtualMachines/vm1": {"team": EVIL},
        f"{_RG}/Microsoft.Compute/virtualMachines/vm2": {"Team": "platform"},
        f"{_RG}/Microsoft.Compute/virtualMachines/vm3": {"team": "Platform"},
    }
    result = _alloc.allocate_costs(costs, tags, currency="USD")
    result["month_over_month"] = _alloc.month_over_month(
        costs, {k: v / 2 for k, v in costs.items()}, dimension_tags=tags
    )
    return result


def _waste_result():
    rows = [
        {
            "id": f"{_RG}/Microsoft.Network/publicIPAddresses/{EVIL}",
            "type": "microsoft.network/publicipaddresses",
            "sku": {"name": "Standard"},
            "properties": {},
        },
        {
            "id": f"{_RG}/Microsoft.Compute/disks/d1",
            "type": "microsoft.compute/disks",
            "sku": {"name": "Premium_LRS"},
            "properties": {"diskState": "Unattached", "diskSizeGB": 128},
        },
    ]
    return _waste.find_waste(rows, costs={rows[0]["id"]: 3.65}, as_of=_AS_OF, currency="USD")


def _commitments():
    rec = {
        "location": "eastus",
        "properties": {
            "subscriptionId": "sub",
            "resourceType": "virtualmachines",
            "skuName": EVIL,
            "scope": "Single",
            "lookBackPeriod": "Last30Days",
            "term": "P3Y",
            "recommendedQuantity": 1,
            "netSavings": 3600.0,
        },
    }
    return _commit.analyze_commitments(
        reservation_recommendations=[rec],
        currency="USD",
        as_of=_AS_OF,
        inventory_unavailable=["reservation", "savings_plan"],
    )


def _anomalies():
    start = _AS_OF - __import__("datetime").timedelta(days=31)
    items = []
    for offset in range(32):
        day = (start + __import__("datetime").timedelta(days=offset)).isoformat()
        items.append(
            {
                "date": day,
                "cost": 10.0 + (90.0 if offset == 30 else 0.0),
                "meterCategory": EVIL,
                "resourceGroup": "managed",
                "resourceId": f"{_RG}/Microsoft.Compute/virtualMachines/vm1",
            }
        )
    items.append(
        {
            "date": items[30]["date"],
            "cost": -5.0,
            "meterCategory": "Credit",
            "resourceGroup": "managed",
            "resourceId": "",
        }
    )
    daily = {}
    for item in items:
        if item["cost"] > 0:
            daily[item["date"]] = daily.get(item["date"], 0.0) + item["cost"]
    return {
        "date": items[30]["date"],
        "anomalies": _detect.detect_anomalies(items),
        "credits": _detect.summarize_credits(items),
        "daily": [{"date": d, "cost_usd": v} for d, v in sorted(daily.items())],
    }


NEW_ADAPTERS = [
    (models.build_cost_allocation_model, _allocation, "FinOps: Cost Allocation"),
    (models.build_waste_model, _waste_result, "FinOps: Waste Cleanup"),
    (models.build_commitment_model, _commitments, "FinOps: Commitments"),
    (models.build_cost_anomaly_model, _anomalies, "FinOps: Cost Anomalies"),
]


@pytest.mark.parametrize(("builder", "fixture", "title"), NEW_ADAPTERS)
def test_new_adapters_are_deterministic_escaped_and_titled(builder, fixture, title):
    original = builder(fixture(), **_context())
    reordered = builder(_reverse_lists(fixture()), **_context())
    output = renderer.render_report(original)

    assert original["title"] == title
    assert _digest(original) == _digest(reordered)
    assert output == renderer.render_report(reordered)
    assert output.startswith("<!doctype html>")
    assert _contains_exact(original, EVIL)
    assert EVIL not in output
    assert "&lt;script&gt;" in output


@pytest.mark.parametrize(
    ("builder", "empty"),
    [
        (models.build_cost_allocation_model, lambda: _alloc.allocate_costs({}, {})),
        (models.build_waste_model, lambda: _waste.find_waste([], as_of=_AS_OF)),
        (models.build_commitment_model, lambda: _commit.analyze_commitments(as_of=_AS_OF)),
        (models.build_cost_anomaly_model, lambda: []),
    ],
)
def test_new_adapters_render_empty_states(builder, empty):
    model = builder(empty(), refreshed_at=REFRESHED_AT)
    output = renderer.render_report(model)

    assert output.startswith("<!doctype html>")
    assert 'class="empty"' in output


def test_commitment_model_flags_unavailable_inventory_as_partial():
    model = models.build_commitment_model(_commitments(), refreshed_at=REFRESHED_AT)
    metrics = {m["label"]: m["value"] for m in model["metrics"]}

    assert model["partial"] is True
    assert metrics["Active commitments"] == "unknown"
    assert any("inventory unavailable" in w for w in model["warnings"])


def test_allocation_model_keeps_unallocated_and_total_rows():
    model = models.build_cost_allocation_model(_allocation(), refreshed_at=REFRESHED_AT)
    showback = model["sections"][0]["rows"]
    labels = [row[0] for row in showback]

    assert labels[-1] == "Total"
    assert any("unallocated" in label.lower() for label in labels)
    assert showback[-1][3] == "$150.00"


def test_anomaly_model_reports_spike_and_credit_separately():
    model = models.build_cost_anomaly_model(_anomalies(), refreshed_at=REFRESHED_AT)
    metrics = {m["label"]: m["value"] for m in model["metrics"]}

    assert metrics["Spikes and new spend"] != "0"
    assert metrics["Credits and refunds"] == "$5.00"
    assert model["chart"]["type"] == "line"
    assert len(model["chart"]["labels"]) == 32


def test_scope_coverage_accepts_filter_result_and_waste_shows_short_names():
    result = _waste_result()
    coverage = [{
        "scope": "/subscriptions/s",
        "status": "complete",
        "included_count": 10,
        "excluded_count": 0,
        "unattributed_count": 1,
        "included_cost_usd": 1131.5,
        "attribution_coverage_pct": 99.82,
        "included": [{}] * 10,
    }]
    model = models.build_waste_model(result, **{**_context(), "scope_coverage": coverage})
    section = next(s for s in model["sections"] if s["title"] == "Scope coverage")
    assert section["columns"][5:7] == ["Cost", "Attribution"]
    assert section["rows"][0][2:7] == ["10", "0", "1", "$1,131.50", "99.8%"]
    findings = model["sections"][0]
    assert findings["columns"][:2] == ["Resource", "Resource group"]
    assert all("/" not in row[0] for row in findings["rows"])


def test_display_resource_shortens_arm_ids_only():
    rid = "/subscriptions/s/resourceGroups/rg-a/providers/Microsoft.App/agents/agent-1"
    assert models._display_resource(rid) == "agent-1 (rg-a)"
    assert models._display_resource("Virtual WAN") == "Virtual WAN"


def test_case_canonicalizer_prefers_common_mixed_case_spelling():
    name = models._case_canonicalizer(["Microsoft.Compute", "microsoft.compute", "Microsoft.Compute", "RG-A", "rg-a"])
    assert name("MICROSOFT.COMPUTE") == "Microsoft.Compute"
    assert name("RG-A") == "rg-a"  # tie: deterministic spelling
    assert name("other") == "other"
