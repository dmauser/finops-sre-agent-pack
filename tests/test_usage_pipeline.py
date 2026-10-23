"""Tests for deterministic UsageDetails page-chain preparation."""

import importlib.util
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
