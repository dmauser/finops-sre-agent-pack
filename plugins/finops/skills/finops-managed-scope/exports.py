"""Offline native Cost Management CSV/gzip ingestion. No network or SDK dependency."""

import csv
import gzip
import hashlib
import io
import json
from datetime import datetime
from pathlib import Path, PurePosixPath

from scope import filter_usage_details
from usage import make_cost_snapshot, normalize_usage_costs


def _column(row, *names):
    normalized = {key.replace(" ", "").replace("_", "").casefold(): value for key, value in row.items()}
    return next((normalized[name.casefold()] for name in names
                 if normalized.get(name.casefold()) not in (None, "")), None)


def _usage_date(value):
    if not value:
        raise ValueError("export row has no usage date")
    for form in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value[:10], form).date().isoformat()
        except ValueError:
            continue
    raise ValueError("unsupported export usage date")


def normalize_export_row(row):
    cost = _column(row, "costinbillingcurrency", "pretaxcost", "cost")
    currency = _column(row, "billingcurrencycode", "billingcurrency", "currency")
    if cost is None or currency is None:
        raise ValueError("export row requires native cost and explicit billing currency")
    return {
        "date": _usage_date(_column(row, "date", "usagedatetime", "usagedate")),
        "resourceId": _column(row, "resourceid", "instanceid"),
        "subscriptionId": _column(row, "subscriptionid", "subscriptionguid"),
        "resourceGroup": _column(row, "resourcegroup", "resourcegroupname"),
        "costInBillingCurrency": cost,
        "billingCurrencyCode": currency,
        "tags": _column(row, "tags") or "{}",
        "consumedService": _column(row, "consumedservice"),
        "meterCategory": _column(row, "metercategory"),
        "meterSubCategory": _column(row, "metersubcategory"),
        "meterName": _column(row, "metername"),
        "quantity": _column(row, "quantity", "usagequantity"),
        "unitOfMeasure": _column(row, "unitofmeasure"),
        "pricingModel": _column(row, "pricingmodel"),
        "chargeType": _column(row, "chargetype"),
        "resourceLocation": _column(row, "resourcelocation"),
    }


def read_export_snapshot(manifest, directory, managed_scopes, *, requested_start,
                         requested_end, expected_basis, expected_version, run_id,
                         code_version, manifest_etag, management_group_expansions=None,
                         expected_export_id=None, source_period=None):
    """Consume exactly one complete generation; files retain their manifest blob paths."""
    config = manifest.get("exportConfig") or {}
    delivery = manifest.get("deliveryConfig") or {}
    run = manifest.get("runInfo") or {}
    if expected_export_id and str(config.get("resourceId", "")).casefold() != expected_export_id.casefold():
        raise ValueError("manifest export identity does not match configured source")
    if config.get("type") != expected_basis or config.get("dataVersion") != expected_version:
        raise ValueError("export cost basis or dataset version does not match the request")
    if delivery.get("fileFormat", "").casefold() != "csv":
        raise ValueError("only native CSV export files are supported")
    if not run.get("runId") or not manifest_etag:
        raise ValueError("export run identity and manifest ETag are required")
    partitions = manifest.get("blobs")
    if not isinstance(partitions, list) or not partitions or len(partitions) != manifest.get("blobCount"):
        raise ValueError("manifest partition count is missing or inconsistent")
    root = Path(directory).resolve()
    names = set()
    rows = []
    byte_count = 0
    revisions = {}
    for part in partitions:
        name = part.get("blobName", "")
        posix = PurePosixPath(name)
        if not name or posix.is_absolute() or ".." in posix.parts or "\\" in name or ":" in name:
            raise ValueError("invalid manifest blob path")
        if name in names:
            raise ValueError("duplicate manifest partition")
        names.add(name)
        path = root.joinpath(*posix.parts).resolve()
        if not path.is_relative_to(root):
            raise ValueError("manifest partition escapes the run directory")
        raw = path.read_bytes()
        if len(raw) != part.get("byteCount"):
            raise ValueError("partition byte count mismatch; download is incomplete or changed")
        revisions[name] = hashlib.sha256(raw).hexdigest()
        byte_count += len(raw)
        mode = delivery.get("compressionMode", "none").casefold()
        if mode not in ("gzip", "none"):
            raise ValueError("unsupported export compression")
        content = gzip.decompress(raw) if mode == "gzip" else raw
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline=""))
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("missing or duplicate CSV headers")
        current = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("malformed CSV row")
            current.append(normalize_export_row(row))
        if len(current) != part.get("dataRowCount"):
            raise ValueError("partition row count mismatch")
        rows.extend(current)
    if len(rows) != manifest.get("dataRowCount") or byte_count != manifest.get("byteCount"):
        raise ValueError("manifest totals do not reconcile to all partitions")
    normalized, info = normalize_usage_costs(rows)
    prepared = {
        **filter_usage_details(normalized, managed_scopes,
                               management_group_expansions=management_group_expansions,
                               sources=[0] * len(normalized)),
        **info, "cost_metric": expected_basis, "partial": False,
        "coverage_period": source_period,
    }
    return make_cost_snapshot(
        prepared, managed_scopes=managed_scopes,
        requested_start=requested_start, requested_end=requested_end, run_id=run_id,
        code_version=code_version, source="cost-management-export",
        source_version={
            "dataset": expected_version, "run_id": run["runId"],
            "manifest_etag": manifest_etag, "partitions_sha256": revisions,
            "manifest_sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
        },
    )
