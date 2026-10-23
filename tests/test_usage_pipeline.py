"""Tests for deterministic UsageDetails page-chain preparation."""

import importlib.util
import pathlib
import sys
from pathlib import Path

import pytest


SKILL = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "finops"
    / "skills"
    / "finops-managed-scope"
)
sys.path.insert(0, str(SKILL))
SPEC = importlib.util.spec_from_file_location("finops_usage", SKILL / "usage.py")
usage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(usage)

RG = "/subscriptions/sub/resourceGroups/managed"


def _row(row_id, resource_group, cost):
    return {
        "id": row_id,
        "properties": {
            "instanceName": (
                f"/subscriptions/sub/resourceGroups/{resource_group}"
                "/providers/Microsoft.Storage/storageAccounts/account"
            ),
            "costInUSD": cost,
        },
    }


def test_prepares_complete_chain_and_filters_after_merge():
    second = "https://management.azure.com/usage?skip=next&top=2"
    result = usage.prepare_usage_details(
        [
            {
                "requestUrl": "https://management.azure.com/usage?top=2",
                "value": [
                    _row("one", "managed", "1.25"),
                    _row("two", "outside", "8.75"),
                ],
                "nextLink": second.replace("&", "&amp;"),
            },
            {
                "request_url": second,
                "rows": [
                    _row("three", "MANAGED", "2.50"),
                    _row("one", "managed", "1.25"),
                ],
                "next_link": None,
            },
        ],
        [RG],
    )

    assert result["chain_complete"] is True
    assert result["partial"] is False
    assert result["page_count"] == 2
    assert result["page_row_counts"] == [2, 2]
    assert result["retrieved_row_count"] == 4
    assert result["included_count"] == 2
    assert result["excluded_count"] == 1
    assert result["duplicate_count"] == 1
    assert str(result["duplicate_cost"]) == "1.25"
    assert str(result["included_cost"]) == "3.75"
    assert str(result["excluded_cost"]) == "8.75"


def test_rejects_pages_from_a_restarted_chain():
    with pytest.raises(ValueError, match="does not continue"):
        usage.prepare_usage_details(
            [
                {
                    "request_url": "https://management.azure.com/usage?top=1",
                    "value": [_row("one", "managed", "1")],
                    "nextLink": "https://management.azure.com/usage?skip=next",
                },
                {
                    "request_url": "https://management.azure.com/usage?top=1",
                    "value": [_row("two", "managed", "2")],
                    "nextLink": None,
                },
            ],
            [RG],
        )


def test_incomplete_chain_fails_closed_by_default():
    pages = [
        {
            "request_url": "https://management.azure.com/usage?top=1",
            "value": [_row("one", "managed", "1")],
            "nextLink": "https://management.azure.com/usage?skip=next",
        }
    ]
    with pytest.raises(ValueError, match="incomplete"):
        usage.prepare_usage_details(pages, [RG])

    partial = usage.prepare_usage_details(pages, [RG], require_complete=False)
    assert partial["chain_complete"] is False
    assert partial["partial"] is True
    assert partial["remaining_next_link"].endswith("skip=next")

def _page(rows, url="https://management.azure.com/usage?metric=AmortizedCost&top=2"):
    return [{"request_url": url, "value": rows, "nextLink": None}]


def _ea_row(row_id, cost, currency="EUR", charge_type="Usage"):
    return {
        "id": row_id,
        "kind": "legacy",
        "properties": {
            "resourceId": (
                "/subscriptions/sub/resourceGroups/managed"
                "/providers/Microsoft.Storage/storageAccounts/account"
            ),
            "cost": cost,
            "billingCurrency": currency,
            "chargeType": charge_type,
        },
    }


def test_legacy_ea_rows_use_billing_currency_cost():
    result = usage.prepare_usage_details(
        _page([_ea_row("a", "10.50"), _ea_row("b", "4.50")]), [RG]
    )
    assert str(result["included_cost"]) == "15.00"
    assert result["currency"] == "EUR"
    assert result["cost_source"] == "billing_currency"
    assert result["missing_cost_count"] == 0
    assert result["cost_metric"] == "AmortizedCost"
    assert result["included_rows"][0]["cost"] == "10.50"


def test_projected_ea_rows_use_billing_cost_alias():
    rows = [
        {"id": "a", "instanceName": RG + "/providers/x/y/z", "cost": None,
         "billingCost": 7, "billingCurrency": "GBP"},
    ]
    result = usage.prepare_usage_details(_page(rows), [RG])
    assert str(result["included_cost"]) == "7"
    assert result["currency"] == "GBP"


def test_mca_rows_keep_usd_cost():
    rows = [{"id": "a", "properties": {
        "instanceName": RG + "/providers/x/y/z",
        "costInUSD": 3, "costInBillingCurrency": 2.7, "billingCurrencyCode": "EUR"}}]
    result = usage.prepare_usage_details(_page(rows), [RG])
    assert str(result["included_cost"]) == "3"
    assert result["currency"] == "USD"
    assert result["cost_source"] == "costInUSD"


def test_mixed_billing_currencies_without_usd_are_rejected():
    with pytest.raises(ValueError, match="multiple billing currencies"):
        usage.prepare_usage_details(
            _page([_ea_row("a", 1, "EUR"), _ea_row("b", 1, "GBP")]), [RG]
        )


def test_rows_without_any_cost_are_counted_as_missing():
    result = usage.prepare_usage_details(
        _page([_row("a", "managed", None), _row("b", "managed", "2")]), [RG]
    )
    assert result["missing_cost_count"] == 1
    assert any("no cost value" in w for w in result["warnings"])


def test_unused_commitment_and_actual_cost_purchases_are_disclosed():
    rows = [
        _ea_row("a", "5", "USD", "UnusedReservation"),
        _ea_row("b", "100", "USD", "Purchase"),
        _ea_row("c", "1", "USD"),
    ]
    actual = usage.prepare_usage_details(
        _page(rows, "https://management.azure.com/usage?metric=ActualCost"), [RG]
    )
    assert actual["cost_metric"] == "ActualCost"
    assert actual["unused_commitment_count"] == 1
    assert str(actual["unused_commitment_cost"]) == "5"
    assert actual["purchase_count"] == 1
    assert any("metric=AmortizedCost" in w for w in actual["warnings"])

    amortized = usage.prepare_usage_details(_page(rows), [RG])
    assert not any("metric=AmortizedCost" in w for w in amortized["warnings"])


ARM = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1"
LAW = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.OperationalInsights/workspaces/law1"


def test_resolve_resource_id_prefers_arm_instance_name_over_resource_id():
    assert usage.resolve_resource_id({"instanceName": ARM, "resourceId": "/other"}) == ARM
    assert usage.resolve_resource_id({"instanceName": None, "resourceId": ARM}) == ARM
    assert usage.resolve_resource_id({"instanceName": "vm1", "resourceId": ARM}) == ARM
    assert usage.resolve_resource_id({"properties": {"instanceName": ARM}}) == ARM
    assert usage.resolve_resource_id({"instanceName": "vm1"}) == "vm1"
    assert usage.resolve_resource_id({}) is None


def test_costs_by_resource_sums_by_resolved_id():
    rows = [
        {"instanceName": ARM, "resourceId": None, "cost": 1.25},
        {"instanceName": None, "resourceId": ARM, "cost": "2.75"},
        {"instanceName": LAW, "cost": 3},
        {"instanceName": None, "resourceId": None, "cost": 9},
    ]
    assert usage.costs_by_resource(rows) == {ARM: 4.0, LAW: 3.0}


def test_ingestion_gb_per_day_counts_only_log_analytics_gb_ingestion():
    rows = [
        {"instanceName": LAW, "meterCategory": "Log Analytics", "unitOfMeasure": "1 GB", "quantity": 30},
        {"instanceName": LAW, "meterCategory": "Log Analytics", "unitOfMeasure": "100 GB", "quantity": 0.3},
        {"instanceName": LAW, "meterCategory": "Log Analytics", "unitOfMeasure": "1 GB/Month", "quantity": 500},
        {"instanceName": LAW, "meterCategory": "Storage", "unitOfMeasure": "1 GB", "quantity": 500},
    ]
    assert usage.ingestion_gb_per_day(rows, days=30) == {LAW: 2.0}
    with pytest.raises(ValueError):
        usage.ingestion_gb_per_day(rows, days=0)


def test_projected_billing_cost_fields_without_or_operator():
    # EA-style projected row: no USD, both billing-cost candidates projected separately.
    rows = [{"id": "r1", "date": "2024-01-01", "cost": None,
             "costInBillingCurrency": 2.7, "billingCost": 9,
             "billingCurrencyCode": "EUR", "billingCurrency": "GBP",
             "instanceName": ARM, "resourceId": None}]
    normalized, meta = usage.normalize_usage_costs(rows)
    assert normalized[0]["resourceId"] == ARM
    assert float(normalized[0]["cost"]) == 2.7
    assert meta["currency"] == "EUR"
    assert meta["cost_source"] == "billing_currency"


def _save_tool_output(path, envelope, mtime):
    import json, os
    path.write_text(json.dumps({"cliExecutionResult": {"output": json.dumps(envelope)}}))
    os.utime(path, (mtime, mtime))


def test_tool_output_chain_is_rebuilt_without_per_page_saves(tmp_path):
    first = "https://management.azure.com/subscriptions/sub/providers/Microsoft.Consumption/usageDetails?api-version=2023-05-01&metric=AmortizedCost&%24top=1000"
    nxt = "https://management.azure.com/next?a=1&amp;skiptoken=2"
    row = {"id": "r1", "date": "2026-09-01", "cost": 1.0, "instanceName": RG + "/providers/x/y/z"}
    _save_tool_output(tmp_path / "t_RunAzCliReadCommands_a.json", {"nextLink": nxt, "value": [row]}, 100)
    (tmp_path / "t_RunAzCliReadCommands_err.json").write_text('{"cliExecutionResult": {"output": "timed out"}}')
    _save_tool_output(tmp_path / "t_RunAzCliReadCommands_other.json", {"value": [{"name": "alert"}], "nextLink": None}, 150)
    row2 = dict(row, id="r2")
    _save_tool_output(tmp_path / "t_RunAzCliReadCommands_b.json", {"nextLink": None, "value": [row2]}, 200)

    chains = usage.find_usage_tool_output_chains(str(tmp_path))
    assert [[pathlib.Path(p).name for p in c] for c in chains] == [
        ["t_RunAzCliReadCommands_a.json", "t_RunAzCliReadCommands_b.json"]
    ]
    pages = usage.load_usage_tool_output_pages(first, chains[0])
    assert pages[1]["request_url"] == "https://management.azure.com/next?a=1&skiptoken=2"
    result = usage.prepare_usage_details(pages, [RG])
    assert result["chain_complete"] and result["page_count"] == 2
    assert len(result["included_rows"]) == 2


def test_tool_output_loader_rejects_files_after_chain_end(tmp_path):
    a, b = tmp_path / "x_RunAzCliReadCommands_1.json", tmp_path / "x_RunAzCliReadCommands_2.json"
    _save_tool_output(a, {"nextLink": None, "value": []}, 1)
    _save_tool_output(b, {"nextLink": None, "value": []}, 2)
    with pytest.raises(ValueError):
        usage.load_usage_tool_output_pages("https://x", [str(a), str(b)])


def _save_with_command(path, envelope, mtime, url):
    import json, os
    path.write_text(json.dumps({
        "command": f'az rest --method get --url "{url}" --query "{{value: value, nextLink: nextLink}}"',
        "cliExecutionResult": {"output": json.dumps(envelope)},
    }))
    os.utime(path, (mtime, mtime))


USAGE_A = "https://management.azure.com/subscriptions/aaa/providers/Microsoft.Consumption/usageDetails"
USAGE_B = "https://management.azure.com/subscriptions/bbb/providers/Microsoft.Consumption/usageDetails"


def test_tool_output_chains_ignore_files_from_previous_runs(tmp_path):
    row = {"id": "r1", "date": "2026-09-01", "cost": 1.0}
    nxt_old = USAGE_A + "?api-version=2023-05-01&%24skiptoken=old"
    _save_tool_output(tmp_path / "o_RunAzCliReadCommands_1.json", {"nextLink": nxt_old, "value": [row]}, 100)
    _save_tool_output(tmp_path / "o_RunAzCliReadCommands_2.json", {"nextLink": nxt_old + "2", "value": [row]}, 110)
    _save_tool_output(tmp_path / "n_RunAzCliReadCommands_1.json", {"nextLink": None, "value": [row]}, 500)

    assert len(usage.find_usage_tool_output_chains(str(tmp_path))) == 1
    assert len(usage.find_usage_tool_output_chains(str(tmp_path))[0]) == 3  # old pages merged in
    chains = usage.find_usage_tool_output_chains(str(tmp_path), after_timestamp=400.0)
    assert [[pathlib.Path(p).name for p in c] for c in chains] == [["n_RunAzCliReadCommands_1.json"]]


def test_tool_output_chains_filter_by_request_url_prefix(tmp_path):
    row = {"id": "r1", "date": "2026-09-01", "cost": 1.0}
    first_a = USAGE_A + "?api-version=2023-05-01&metric=AmortizedCost&%24top=1000"
    next_a = USAGE_A + "?api-version=2023-05-01&amp;%24skiptoken=2"
    _save_with_command(tmp_path / "1_RunAzCliReadCommands.json", {"nextLink": next_a, "value": [row]}, 10, first_a)
    _save_with_command(tmp_path / "2_RunAzCliReadCommands.json", {"nextLink": None, "value": [row]}, 20, USAGE_B + "?x=1")
    # no command recorded: URL inferred from the previous page's nextLink
    _save_tool_output(tmp_path / "3_RunAzCliReadCommands.json", {"nextLink": None, "value": [row]}, 30)

    chains = usage.find_usage_tool_output_chains(
        str(tmp_path), request_url_prefix="/subscriptions/AAA/providers/Microsoft.Consumption/usageDetails")
    assert [[pathlib.Path(p).name for p in c] for c in chains] == [
        ["1_RunAzCliReadCommands.json", "3_RunAzCliReadCommands.json"]
    ]
    pages = usage.load_usage_tool_output_pages(first_a, chains[0])
    assert pages[1]["request_url"] == USAGE_A + "?api-version=2023-05-01&%24skiptoken=2"

    only_b = usage.find_usage_tool_output_chains(str(tmp_path), request_url_prefix=USAGE_B)
    assert [[pathlib.Path(p).name for p in c] for c in only_b] == [["2_RunAzCliReadCommands.json"]]


def test_save_and_load_finops_inputs_are_deterministic(tmp_path):
    import json
    out = tmp_path / "finops-data"
    src = tmp_path / "x_RunAzCliReadCommands.json"
    src.write_text(json.dumps({"cliExecutionResult": {"output": json.dumps({"data": [{"category": "Cost"}]})}}))

    usage.save_finops_input("advisor", source_path=str(src), directory=str(out))
    usage.save_finops_input("budgets", '{"value": [{"name": "b1"}]}', directory=str(out))
    usage.save_finops_input("tags", {"value": []}, directory=str(out))

    assert usage.load_finops_input("advisor", str(out)) == {"data": [{"category": "Cost"}]}
    assert usage.load_finops_input("budgets", str(out))["value"][0]["name"] == "b1"
    assert usage.load_finops_input("inventory", str(out), default=None) is None
    with pytest.raises(FileNotFoundError):
        usage.load_finops_input("inventory", str(out))
    with pytest.raises(ValueError):
        usage.save_finops_input("../evil", {}, directory=str(out))
    with pytest.raises(ValueError):
        usage.save_finops_input("advisor", {}, source_path=str(src), directory=str(out))
    bad = tmp_path / "bad_RunAzCliReadCommands.json"
    bad.write_text('{"cliExecutionResult": {"output": "timed out"}}')
    with pytest.raises(ValueError):
        usage.save_finops_input("tags", source_path=str(bad), directory=str(out))


def test_tool_output_chains_split_abandoned_attempt_from_retry(tmp_path):
    row = {"id": "r1", "date": "2026-10-01", "cost": 1.0, "instanceName": "/subscriptions/aaa/resourceGroups/g/providers/x/y/z"}
    first = USAGE_A + "?api-version=2023-05-01&%24top=500"
    _save_with_command(tmp_path / "1_RunAzCliReadCommands.json",
                       {"nextLink": USAGE_A + "?skiptoken=old", "value": [row]}, 10, first)
    (tmp_path / "2_RunAzCliReadCommands.json").write_text('{"cliExecutionResult": {"output": "timed out"}}')
    _save_with_command(tmp_path / "3_RunAzCliReadCommands.json",
                       {"nextLink": USAGE_A + "?skiptoken=p2", "value": [row]}, 30, first)
    _save_with_command(tmp_path / "4_RunAzCliReadCommands.json",
                       {"nextLink": None, "value": [row]}, 40, USAGE_A + "?skiptoken=p2")

    chains = usage.find_usage_tool_output_chains(str(tmp_path), request_url_prefix=USAGE_A)
    assert [[pathlib.Path(p).name for p in c] for c in chains] == [
        ["1_RunAzCliReadCommands.json"],
        ["3_RunAzCliReadCommands.json", "4_RunAzCliReadCommands.json"],
    ]
    pages = usage.load_usage_tool_output_pages(first, chains[-1])
    assert usage.prepare_usage_details(pages, ["/subscriptions/aaa"])["chain_complete"]


def test_tool_output_chains_separate_concurrent_threads(tmp_path):
    ta, tb = "2c3229cc-a873-44fa-8e60-aad5cc118dbf", "cd4c5191-c68f-498c-a1b7-c681057aa266"
    row = {"id": "r1", "date": "2026-10-01", "cost": 1.0}
    nxt = USAGE_A + "?api-version=2023-11-01&%24skiptoken="
    # Two scheduled tasks page the same URL at the same time; their files interleave by mtime.
    _save_tool_output(tmp_path / f"{ta}_RunAzCliReadCommands_1.json", {"nextLink": nxt + "a1", "value": [row]}, 100)
    _save_tool_output(tmp_path / f"{tb}_RunAzCliReadCommands_1.json", {"nextLink": nxt + "b1", "value": [row]}, 101)
    _save_tool_output(tmp_path / f"{ta}_RunAzCliReadCommands_2.json", {"nextLink": nxt + "a2", "value": [row]}, 102)
    _save_tool_output(tmp_path / f"{tb}_RunAzCliReadCommands_2.json", {"nextLink": None, "value": [row]}, 103)
    _save_tool_output(tmp_path / f"{ta}_RunAzCliReadCommands_3.json", {"nextLink": None, "value": [row]}, 104)

    names = [[pathlib.Path(p).name.split("_")[0][:2] + pathlib.Path(p).stem[-1] for p in c]
             for c in usage.find_usage_tool_output_chains(str(tmp_path), request_url_prefix=USAGE_A)]
    assert names == [["cd1", "cd2"], ["2c1", "2c2", "2c3"]]

    only_a = usage.find_usage_tool_output_chains(str(tmp_path), thread_id=ta.upper())
    assert [len(c) for c in only_a] == [3]
    pages = usage.load_usage_tool_output_pages(USAGE_A + "?api-version=2023-11-01", only_a[0])
    assert usage.prepare_usage_details(pages, ["/subscriptions/aaa"])["chain_complete"]
