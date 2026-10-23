import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "plugins" / "finops" / "skills" / "finops-carbon-optimization" / "carbon.py"
spec = importlib.util.spec_from_file_location("carbon", PATH)
carbon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(carbon)

SUB = "AAAA0000-0000-0000-0000-000000000000"
SUB_L = SUB.lower()


def test_request_lowercases_and_sets_required_fields():
    body = carbon.build_report_request(
        "TopItemsSummaryReport", [SUB], "2026-09-01", category_type="Resource", top_items=5,
        locations=["East US"], resource_types=["Microsoft.Compute/virtualMachines"],
    )
    assert body == {
        "reportType": "TopItemsSummaryReport",
        "subscriptionList": [SUB_L],
        "carbonScopeList": ["Scope1", "Scope2", "Scope3"],
        "dateRange": {"start": "2026-09-01", "end": "2026-09-01"},
        "categoryType": "Resource",
        "topItems": 5,
        "locationList": ["east us"],
        "resourceTypeList": ["microsoft.compute/virtualmachines"],
    }


def test_item_details_defaults_and_paging_token():
    body = carbon.build_report_request(
        "ItemDetailsReport", [SUB], "2026-09-01", category_type="ResourceGroup", skip_token="abc",
    )
    assert body["orderBy"] == "LatestMonthEmissions"
    assert body["sortDirection"] == "Desc"
    assert body["pageSize"] == 1000
    assert body["skipToken"] == "abc"


@pytest.mark.parametrize("kwargs, message", [
    ({"report_type": "Bogus"}, "report_type"),
    ({"start": "2026-09-15"}, "first day"),
    ({"report_type": "ItemDetailsReport", "end": "2026-10-01", "category_type": "Resource"}, "one month"),
    ({"report_type": "TopItemsSummaryReport", "category_type": "Resource", "top_items": 11}, "top_items"),
    ({"report_type": "TopItemsSummaryReport", "top_items": 3}, "category_type"),
    ({"scopes": ["Scope4"]}, "scopes"),
    ({"page_size": 10}, "ItemDetailsReport only"),
    ({"subscription_ids": [f"s{i}" for i in range(101)]}, "at most 100"),
    ({"resource_group_urls": ["rg-only"]}, "resource_group_urls"),
])
def test_request_validation(kwargs, message):
    args = {"report_type": "OverallSummaryReport", "subscription_ids": [SUB], "start": "2026-09-01"}
    args.update(kwargs)
    report_type, subs, start = args.pop("report_type"), args.pop("subscription_ids"), args.pop("start")
    with pytest.raises(ValueError, match=message):
        carbon.build_report_request(report_type, subs, start, **args)


@pytest.mark.parametrize("today, expected", [
    ("2026-10-23", "2026-09-01"),
    ("2026-10-05", "2026-08-01"),
    ("2026-01-10", "2025-11-01"),
    ("2026-01-19", "2025-12-01"),
])
def test_latest_complete_month(today, expected):
    assert carbon.latest_complete_month(today).isoformat() == expected


def test_standard_requests_use_managed_rg_filters():
    scopes = [f"/subscriptions/{SUB}/resourceGroups/App"]
    reqs = carbon.standard_requests(scopes, "2026-10-23")
    assert set(reqs) == {"overall", "monthly", "top-resources", "top-resource-types", "top-locations"}
    assert reqs["monthly"]["dateRange"] == {"start": "2025-10-01", "end": "2026-09-01"}
    for body in reqs.values():
        assert body["subscriptionList"] == [SUB_L]
        assert body["resourceGroupUrlList"] == [f"/subscriptions/{SUB_L}/resourcegroups/app"]


def test_standard_requests_reject_mixed_and_unsupported_scopes():
    with pytest.raises(ValueError, match="mixed"):
        carbon.standard_requests(
            [f"/subscriptions/{SUB}", "/subscriptions/other/resourceGroups/x"], "2026-10-23"
        )
    with pytest.raises(ValueError, match="unsupported"):
        carbon.standard_requests(["/providers/Microsoft.Management/managementGroups/mg"], "2026-10-23")


def test_rg_scope_under_whole_subscription_collapses():
    filters = carbon.scope_filters([f"/subscriptions/{SUB}", f"/subscriptions/{SUB}/resourceGroups/x"])
    assert filters == {"subscriptions": [SUB_L], "resource_group_urls": [], "unsupported": []}


def test_human_run_script_is_post_query_only_and_valid_bash(tmp_path):
    reqs = carbon.standard_requests([f"/subscriptions/{SUB}"], "2026-10-23")
    reqs["details"] = carbon.build_report_request(
        "ItemDetailsReport", [SUB], "2026-09-01", category_type="Resource"
    )
    script = carbon.human_run_script(reqs)
    assert "set -euo pipefail" in script
    for line in script.splitlines():
        if "az " in line:
            assert "az rest --method post" in line
            assert "Microsoft.Carbon/" in line
    assert "skipToken" in script and "details-$PAGE.json" in script
    path = tmp_path / "carbon.sh"
    path.write_text(script, newline="\n")
    bash = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, check=False)
    assert bash.returncode == 0, bash.stderr
    with pytest.raises(ValueError):
        carbon.human_run_script({"bad name": {}})


def test_analyze_carbon_summarizes_and_filters_scope():
    in_vm = f"/subscriptions/{SUB_L}/resourcegroups/app/providers/microsoft.compute/virtualmachines/a"
    out_vm = "/subscriptions/other/resourcegroups/x/providers/microsoft.compute/virtualmachines/b"
    responses = {
        "overall": json.dumps({
            "subscriptionAccessDecisionList": [
                {"subscriptionId": SUB, "decision": "Allowed"},
                {"subscriptionId": "DENIED", "decision": "Denied", "denialReason": "No access"},
            ],
            "value": [{"dataType": "OverallSummaryData", "latestMonthEmissions": 110,
                       "previousMonthEmissions": 100}],
        }),
        "monthly": {"value": [
            {"dataType": "MonthlySummaryData", "date": "2026-09-01", "latestMonthEmissions": 110,
             "carbonIntensity": 2.5},
            {"dataType": "MonthlySummaryData", "date": "2026-08-01", "latestMonthEmissions": 100},
        ]},
        "top": {"value": [
            {"dataType": "ResourceTopItemsSummaryData", "itemName": "a", "categoryType": "Resource",
             "resourceId": in_vm, "latestMonthEmissions": 60, "previousMonthEmissions": 40},
            {"dataType": "ResourceTopItemsSummaryData", "itemName": "b", "categoryType": "Resource",
             "resourceId": out_vm, "latestMonthEmissions": 500, "previousMonthEmissions": 1},
            {"dataType": "TopItemsSummaryData", "itemName": "east us", "categoryType": "Location",
             "latestMonthEmissions": 110, "previousMonthEmissions": 0},
        ]},
    }
    result = carbon.analyze_carbon(responses, managed_scopes=[f"/subscriptions/{SUB}"])
    assert result["unit"] == "kgCO2e"
    assert result["overall"] == {"latest_kg": 110.0, "previous_kg": 100.0, "change_kg": 10.0, "change_pct": 10.0}
    assert [m["date"] for m in result["monthly"]] == ["2026-08-01", "2026-09-01"]
    assert result["monthly"][1]["carbon_intensity"] == 2.5
    assert [i["name"] for i in result["top_items"]["Resource"]] == ["a"]
    assert result["top_items"]["Location"][0]["change_pct"] is None
    assert [i["name"] for i in result["increases"]] == ["a"]
    assert result["excluded_count"] == 1
    assert result["unattributed_count"] == 1
    assert result["access"] == {
        "allowed": [SUB_L], "denied": [{"subscriptionId": "denied", "reason": "No access"}],
    }
    assert {h["type"] for h in result["hints"]} == {"access_denied", "emissions_increase", "concentration"}


def test_analyze_carbon_warns_on_empty_and_partial_pages():
    result = carbon.analyze_carbon([{"value": [], "skipToken": "next"}])
    assert result["overall"] is None
    assert any("skipToken" in w for w in result["warnings"])
    assert any("No emissions rows" in w for w in result["warnings"])
