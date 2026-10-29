import gzip
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "finops" / "skills" / "finops-managed-scope"))
spec = importlib.util.spec_from_file_location("cost_exports", Path(sys.path[0]) / "exports.py")
exports = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exports)


def fixture(tmp_path, compressed=False):
    data = b"Date,CostInBillingCurrency,BillingCurrencyCode,SubscriptionId,ResourceId,Tags\n2026-09-01,1.005,EUR,sub,/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm,{}\n"
    if compressed:
        data = gzip.compress(data)
    (tmp_path / "part.csv").write_bytes(data)
    return {
        "blobCount": 1, "byteCount": len(data), "dataRowCount": 1,
        "exportConfig": {"type": "AmortizedCost", "dataVersion": "2021-01-01"},
        "deliveryConfig": {"fileFormat": "Csv", "compressionMode": "gzip" if compressed else "none"},
        "runInfo": {"runId": "generation-one"},
        "blobs": [{"blobName": "part.csv", "byteCount": len(data), "dataRowCount": 1}],
    }


def read(manifest, tmp_path):
    return exports.read_export_snapshot(
        manifest, tmp_path, ["/subscriptions/sub"], requested_start="2026-09-01",
        requested_end="2026-09-30", expected_basis="AmortizedCost",
        expected_version="2021-01-01", run_id="run", code_version="test",
        manifest_etag="revision-one",
    )


@pytest.mark.parametrize("compressed", [False, True])
def test_complete_native_export_reconciles(tmp_path, compressed):
    result = read(fixture(tmp_path, compressed), tmp_path)
    assert result["included_cost"] == "1.005"
    assert result["currency"] == "EUR"
    assert result["source"] == "cost-management-export"
    assert len(result["rows"]) == 1
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("mutation", ["count", "bytes", "rows", "basis", "version", "escape", "duplicate"])
def test_incomplete_or_wrong_generation_rejected(tmp_path, mutation):
    manifest = fixture(tmp_path)
    if mutation == "count":
        manifest["blobCount"] = 2
    elif mutation == "bytes":
        manifest["blobs"][0]["byteCount"] += 1
    elif mutation == "rows":
        manifest["dataRowCount"] = 2
    elif mutation == "basis":
        manifest["exportConfig"]["type"] = "ActualCost"
    elif mutation == "version":
        manifest["exportConfig"]["dataVersion"] = "unknown"
    elif mutation == "escape":
        manifest["blobs"][0]["blobName"] = "../part.csv"
    else:
        manifest["blobs"] *= 2
        manifest["blobCount"] = 2
    with pytest.raises(ValueError):
        read(manifest, tmp_path)


def test_mosa_schema_normalizes_without_assuming_usd():
    row = exports.normalize_export_row({
        "UsageDateTime": "2026-09-01", "PreTaxCost": "2.5", "Currency": "GBP",
        "SubscriptionGuid": "sub", "InstanceId": "/subscriptions/sub/resourceGroups/rg",
    })
    assert row["billingCurrencyCode"] == "GBP"
    assert row["costInBillingCurrency"] == "2.5"


def test_missing_partition_cannot_look_complete(tmp_path):
    manifest = fixture(tmp_path)
    (tmp_path / "part.csv").unlink()
    with pytest.raises(FileNotFoundError):
        read(manifest, tmp_path)
