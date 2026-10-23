"""Cost anomaly detector for the FinOps SRE Agent pack (skill: finops-cost-anomaly-detection).

Pure, dependency-free logic so it is unit-testable offline. The agent reads this file
and runs `detect_anomalies(...)` with the runtime's sandbox Python tool after loading
Cost Management UsageDetails line items. No Azure calls happen here.

Line-item shape (one dict per UsageDetails row, already flattened by the skill):
    {
        "date": "2026-07-12",        # properties.date (day granularity)
        "cost": 172.89,               # properties.costInUSD
        "meterCategory": "Container Apps",
        "resourceGroup": "sre-agent-demo-2",
        "resourceId": "/subscriptions/.../containerApps/foo",
        "tags": {"env": "prod"}       # optional; dict, JSON string, or Azure '"k": "v"' string
    }

Negative-cost rows (refunds, credits, reservation refunds) are excluded from anomaly detection
so they cannot mask a spike or fabricate a drop; report them with `summarize_credits(...)`.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import date, timedelta

DEFAULT_DIMENSIONS = ("meterCategory", "resourceGroup", "resourceId")


def _parse_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _cost(item) -> float:
    try:
        return float(item.get("cost", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def parse_tags(raw) -> dict:
    """Normalize a UsageDetails tags value to a dict.

    Accepts a dict, a JSON object string ('{"env": "prod"}'), or the bare Azure format
    without braces ('"env": "prod","team": "x"'). Anything malformed yields {}.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    text = raw.strip()
    if not text:
        return {}
    if not text.startswith("{"):
        text = "{" + text + "}"
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tag_value(tags, key):
    """Case-insensitive tag lookup on a dict / JSON string / Azure bare-string tags value."""
    wanted = str(key).strip().lower()
    for k, v in parse_tags(tags).items():
        if str(k).strip().lower() == wanted:
            return v
    return None


def _dimension_value(item: dict, dimension: str):
    """Resolve a dimension value. `tag:<key>` reads from the item's tags bag (case-insensitive)."""
    if dimension.startswith("tag:"):
        return _tag_value(item.get("tags"), dimension[len("tag:"):])
    if dimension == "resourceId":
        # Modern billing carries the full ARM id in instanceName; prefer it when projected.
        instance = str(item.get("instanceName") or "").strip()
        if instance.lower().startswith("/subscriptions/"):
            return instance
    return item.get(dimension)


def resolve_today(dates, assume_last_partial: bool = True) -> date:
    """Pick the latest COMPLETE day.

    Cost Management billing lags 1-2 days, so the newest day is usually partial and
    must not be treated as a spike or drop. With `assume_last_partial`, the newest
    distinct day is dropped and the day before it is used as "today".
    """
    distinct = sorted({_parse_date(d) for d in dates})
    if not distinct:
        raise ValueError("no dates provided")
    if assume_last_partial and len(distinct) >= 2:
        return distinct[-2]
    return distinct[-1]


def _daily_totals(items, dimension, value) -> dict:
    totals: dict = defaultdict(float)
    for item in items:
        if _dimension_value(item, dimension) == value:
            totals[_parse_date(item["date"])] += _cost(item)
    return totals


def _detect_for_dimension(
    items, dimension, today, baseline_days, k, min_delta_usd, wow_ratio, detect_drops=True
):
    anomalies = []
    values = {
        _dimension_value(i, dimension)
        for i in items
        if _dimension_value(i, dimension) is not None
    }
    for value in values:
        totals = _daily_totals(items, dimension, value)
        current = totals.get(today, 0.0)
        if current <= 0 and not detect_drops:
            continue

        baseline = [
            totals.get(today - timedelta(days=offset), 0.0)
            for offset in range(1, baseline_days + 1)
        ]
        has_history = any(v > 0 for v in baseline)
        if current <= 0 and not has_history:
            continue
        mean = statistics.fmean(baseline) if baseline else 0.0
        std = statistics.pstdev(baseline) if len(baseline) > 1 else 0.0

        wow_prev = totals.get(today - timedelta(days=7))

        triggers = []
        direction = "up"
        if not has_history:
            # First time this dimension shows meaningful spend.
            if current >= min_delta_usd:
                triggers.append("new_spend")
        else:
            baseline_hit = current > mean + k * std and (current - mean) >= min_delta_usd
            wow_hit = (
                wow_prev is not None
                and wow_prev > 0
                and current >= wow_prev * wow_ratio
                and (current - wow_prev) >= min_delta_usd
            )
            if baseline_hit:
                triggers.append("baseline")
            if wow_hit:
                triggers.append("wow")
            if not triggers and detect_drops:
                # Drop: spend fell well below its trailing baseline (incl. to zero) or to
                # <= 1/wow_ratio of the same day last week -- an outage, deletion, broken
                # pipeline, or a savings win.
                drop_hit = current < mean - k * std and (mean - current) >= min_delta_usd
                wow_drop_hit = (
                    wow_prev is not None
                    and wow_prev > 0
                    and current <= wow_prev / wow_ratio
                    and (wow_prev - current) >= min_delta_usd
                )
                if drop_hit:
                    triggers.append("baseline_drop")
                if wow_drop_hit:
                    triggers.append("wow_drop")
                if triggers:
                    direction = "down"

        if not triggers:
            continue

        dod_delta = current - mean
        wow_delta = (current - wow_prev) if wow_prev is not None else None
        if direction == "down":
            kind = "drop"
            impact = max(-dod_delta, -wow_delta if wow_delta is not None else -dod_delta)
            sustained = all(
                (mean - totals.get(today - timedelta(days=off), 0.0)) >= min_delta_usd
                for off in range(3)
            )
        else:
            kind = "new_spend" if triggers == ["new_spend"] else "spike"
            impact = max(dod_delta, wow_delta if wow_delta is not None else dod_delta)
            sustained = all(
                (totals.get(today - timedelta(days=off), 0.0) - mean) >= min_delta_usd
                for off in range(3)
            )

        anomalies.append(
            {
                "dimension": dimension,
                "value": value,
                "date": today.isoformat(),
                "kind": kind,
                "direction": direction,
                "triggers": triggers,
                "current_usd": round(current, 2),
                "baseline_mean_usd": round(mean, 2),
                "baseline_std_usd": round(std, 2),
                "dod_delta_usd": round(dod_delta, 2),
                "dod_delta_pct": round((dod_delta / mean * 100) if mean > 0 else float("inf"), 1),
                "wow_delta_usd": round(wow_delta, 2) if wow_delta is not None else None,
                "impact_usd": round(impact, 2),
                "sustained": sustained,
            }
        )
    return anomalies


def detect_anomalies(
    line_items,
    *,
    today=None,
    assume_last_partial: bool = True,
    dimensions=DEFAULT_DIMENSIONS,
    baseline_days: int = 28,
    k: float = 3.0,
    min_delta_usd: float = 5.0,
    wow_ratio: float = 1.5,
    detect_drops: bool = True,
):
    """Detect cost anomalies across the given dimensions.

    Returns a list of anomaly dicts sorted by absolute dollar impact (descending).
    A spike fires when the current day exceeds the trailing baseline (mean + k*std)
    OR is at least `wow_ratio`x the same day last week -- in both cases only if the
    absolute change clears `min_delta_usd`. Dimensions with no prior history are
    labelled `new_spend` instead of `spike`.

    With `detect_drops` (default), a dimension with history whose current day falls
    below mean - k*std (trigger "baseline_drop") or to <= 1/`wow_ratio` of the same day
    last week (trigger "wow_drop") -- including falling to zero -- is reported with
    kind "drop". Every anomaly carries "direction" ("up"/"down"), a positive-magnitude
    "impact_usd", and "sustained" (the 3 most recent complete days all moved by at
    least `min_delta_usd` versus the baseline mean -- a step change, not a blip).

    Negative-cost rows (credits/refunds) are ignored here; use `summarize_credits`.
    """
    if not line_items:
        return []

    if today is None:
        today = resolve_today((i["date"] for i in line_items), assume_last_partial)
    else:
        today = _parse_date(today)

    usage_items = [i for i in line_items if _cost(i) >= 0]

    anomalies = []
    for dimension in dimensions:
        anomalies.extend(
            _detect_for_dimension(
                usage_items, dimension, today, baseline_days, k, min_delta_usd, wow_ratio,
                detect_drops,
            )
        )

    anomalies.sort(key=lambda a: a["impact_usd"], reverse=True)
    return anomalies


def summarize_credits(
    line_items,
    today=None,
    *,
    assume_last_partial: bool = True,
    dimensions=DEFAULT_DIMENSIONS,
    top_n: int = 10,
):
    """Summarize negative-cost rows (credits, refunds, reservation refunds).

    `detect_anomalies` ignores these rows so they cannot hide a spike or fake a drop;
    call this alongside it and report credits separately. Credit amounts are returned
    as positive magnitudes (`credit_usd`); `top_rows` keep the raw negative `cost`.

    Returns a dict:
      {
        "today", "total_credit_usd", "row_count", "today_credit_usd",
        "by_day": [{"date", "credit_usd", "row_count"}...]            # oldest first
        "by_dimension": {dim: [{"value", "credit_usd", "row_count"}...]}  # largest first
        "by_charge_type": [{"chargeType", "credit_usd", "row_count"}...],
        "top_rows": [{"date", "cost", "chargeType", "meterCategory", "resourceGroup", "resourceId"}...],
      }
    """
    items = list(line_items or [])
    credits = [i for i in items if _cost(i) < 0]

    resolved = None
    if today is not None:
        resolved = _parse_date(today)
    elif items:
        resolved = resolve_today((i["date"] for i in items), assume_last_partial)

    by_day: dict = defaultdict(lambda: [0.0, 0])
    by_dim: dict = {d: defaultdict(lambda: [0.0, 0]) for d in dimensions}
    by_charge: dict = defaultdict(lambda: [0.0, 0])
    for item in credits:
        amount = -_cost(item)
        day = _parse_date(item["date"])
        by_day[day][0] += amount
        by_day[day][1] += 1
        for d in dimensions:
            value = _dimension_value(item, d)
            if value is not None:
                by_dim[d][value][0] += amount
                by_dim[d][value][1] += 1
        charge = item.get("chargeType") or "unknown"
        by_charge[charge][0] += amount
        by_charge[charge][1] += 1

    top = sorted(credits, key=_cost)[:top_n]
    return {
        "today": resolved.isoformat() if resolved else None,
        "total_credit_usd": round(sum(-_cost(i) for i in credits), 2),
        "row_count": len(credits),
        "today_credit_usd": round(by_day[resolved][0], 2) if resolved in by_day else 0.0,
        "by_day": [
            {"date": d.isoformat(), "credit_usd": round(v[0], 2), "row_count": v[1]}
            for d, v in sorted(by_day.items())
        ],
        "by_dimension": {
            d: sorted(
                (
                    {"value": val, "credit_usd": round(v[0], 2), "row_count": v[1]}
                    for val, v in vals.items()
                ),
                key=lambda r: r["credit_usd"],
                reverse=True,
            )
            for d, vals in by_dim.items()
        },
        "by_charge_type": sorted(
            (
                {"chargeType": c, "credit_usd": round(v[0], 2), "row_count": v[1]}
                for c, v in by_charge.items()
            ),
            key=lambda r: r["credit_usd"],
            reverse=True,
        ),
        "top_rows": [
            {
                "date": _parse_date(i["date"]).isoformat(),
                "cost": round(_cost(i), 2),
                "chargeType": i.get("chargeType"),
                "meterCategory": i.get("meterCategory"),
                "resourceGroup": i.get("resourceGroup"),
                "resourceId": _dimension_value(i, "resourceId"),
            }
            for i in top
        ],
    }
