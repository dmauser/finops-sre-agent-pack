"""Layer-1 unit tests for the cost anomaly detector (offline, deterministic, no Azure)."""

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

# Load detect.py directly from the skill folder (no package install needed).
_DETECT_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "finops"
    / "skills"
    / "finops-cost-anomaly-detection"
    / "detect.py"
)
_spec = importlib.util.spec_from_file_location("detect", _DETECT_PATH)
detect = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(detect)


TODAY = date(2026, 7, 14)


def _day(offset):
    return (TODAY - timedelta(days=offset)).isoformat()


def _series(meter, daily_cost, days, resource_id="/r/1", rg="rg1", start_offset=1):
    """Flat history of `daily_cost` for `days` days, oldest first, ending yesterday."""
    return [
        {
            "date": (TODAY - timedelta(days=off)).isoformat(),
            "cost": daily_cost,
            "meterCategory": meter,
            "resourceGroup": rg,
            "resourceId": resource_id,
            "tags": {},
        }
        for off in range(start_offset, start_offset + days)
    ]


def _point(meter, cost, offset=0, resource_id="/r/1", rg="rg1"):
    return {
        "date": (TODAY - timedelta(days=offset)).isoformat(),
        "cost": cost,
        "meterCategory": meter,
        "resourceGroup": rg,
        "resourceId": resource_id,
        "tags": {},
    }


def _by_value(anomalies, value):
    return [a for a in anomalies if a["value"] == value]


def test_known_spike_is_flagged_with_correct_delta():
    items = _series("Container Apps", 10.0, 28) + [_point("Container Apps", 100.0)]
    result = detect.detect_anomalies(
        items, today=TODAY, dimensions=("meterCategory",)
    )
    hits = _by_value(result, "Container Apps")
    assert len(hits) == 1
    a = hits[0]
    assert a["kind"] == "spike"
    assert "baseline" in a["triggers"]
    assert a["current_usd"] == 100.0
    assert a["baseline_mean_usd"] == 10.0
    assert a["dod_delta_usd"] == 90.0


def test_tiny_meter_noise_is_not_flagged():
    # $0.50 baseline -> $2.00 today: big % but below the $5 absolute floor.
    items = _series("Tiny Meter", 0.5, 28) + [_point("Tiny Meter", 2.0)]
    result = detect.detect_anomalies(
        items, today=TODAY, dimensions=("meterCategory",)
    )
    assert _by_value(result, "Tiny Meter") == []


def test_new_resource_is_new_spend_not_spike():
    # No baseline history at all, only today.
    items = [_point("Fresh Service", 40.0)]
    result = detect.detect_anomalies(
        items, today=TODAY, dimensions=("meterCategory",)
    )
    hits = _by_value(result, "Fresh Service")
    assert len(hits) == 1
    assert hits[0]["kind"] == "new_spend"
    assert hits[0]["triggers"] == ["new_spend"]


def test_flat_history_produces_no_anomalies():
    # Perfectly flat, including today -> zero false positives.
    items = _series("Steady", 25.0, 28) + [_point("Steady", 25.0)]
    result = detect.detect_anomalies(
        items, today=TODAY, dimensions=("meterCategory",)
    )
    assert result == []


def test_gradual_creep_caught_by_week_over_week():
    # Handcrafted so the trailing-baseline test misses but WoW fires.
    # Baseline days (1..28 ago) hover around ~13 with enough spread that
    # mean + 3*std is well above today's 16; but same-day-last-week was 10.
    totals = {}
    # 28 days of low-ish values that ramp gently, same-day-last-week = 10.
    values = [8, 9, 9, 10, 11, 12, 10, 12, 13, 14, 13, 15, 16, 14,
              15, 17, 18, 16, 17, 19, 20, 18, 19, 21, 22, 20, 21, 23]
    items = []
    for off, v in zip(range(1, 29), values):
        items.append(_point("Creeper", float(v), offset=off))
    # Force same-day-last-week (offset 7) to a clearly lower value.
    items = [i for i in items if i["date"] != _day(7)]
    items.append(_point("Creeper", 10.0, offset=7))
    # Today: only 16 -> below baseline threshold, but >= 1.5x last week's 10.
    items.append(_point("Creeper", 16.0, offset=0))

    result = detect.detect_anomalies(
        items, today=TODAY, dimensions=("meterCategory",)
    )
    hits = _by_value(result, "Creeper")
    assert len(hits) == 1
    assert "wow" in hits[0]["triggers"]
    assert "baseline" not in hits[0]["triggers"]


def test_anomalies_ranked_by_absolute_dollars():
    big = _series("Big", 100.0, 28, resource_id="/r/big") + [
        _point("Big", 300.0, resource_id="/r/big")
    ]
    small = _series("Small", 5.0, 28, resource_id="/r/small") + [
        _point("Small", 60.0, resource_id="/r/small")
    ]
    result = detect.detect_anomalies(
        big + small, today=TODAY, dimensions=("meterCategory",)
    )
    # Small has a bigger %, Big has a bigger absolute impact -> Big ranks first.
    assert result[0]["value"] == "Big"
    assert result[0]["impact_usd"] > result[1]["impact_usd"]


def test_partial_last_day_is_excluded_by_resolve_today():
    # Newest distinct day is partial; resolve_today should pick the day before.
    dates = [_day(2), _day(1), _day(0)]
    resolved = detect.resolve_today(dates, assume_last_partial=True)
    assert resolved == TODAY - timedelta(days=1)


def test_partial_last_day_not_flagged_end_to_end():
    # A low partial value on the newest day must not register as a drop/spike,
    # and yesterday's real spike is what gets evaluated.
    items = _series("WithPartial", 10.0, 28, start_offset=2)  # days 2..29 ago
    items.append(_point("WithPartial", 100.0, offset=1))       # yesterday: real spike
    items.append(_point("WithPartial", 3.0, offset=0))         # today: partial, ignore
    result = detect.detect_anomalies(
        items, assume_last_partial=True, dimensions=("meterCategory",)
    )
    hits = _by_value(result, "WithPartial")
    assert len(hits) == 1
    assert hits[0]["date"] == (TODAY - timedelta(days=1)).isoformat()
    assert hits[0]["current_usd"] == 100.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_resource_id_dimension_prefers_arm_instance_name():
    arm = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1"
    assert detect._dimension_value({"instanceName": arm, "resourceId": None}, "resourceId") == arm.lower()
    assert detect._dimension_value({"instanceName": "vm1", "resourceId": arm}, "resourceId") == arm.lower()


# --- drops -------------------------------------------------------------------------------------


def test_drop_below_baseline_is_flagged_as_down():
    items = _series("Shrinker", 50.0, 28) + [_point("Shrinker", 10.0)]
    result = detect.detect_anomalies(items, today=TODAY, dimensions=("meterCategory",))
    hits = _by_value(result, "Shrinker")
    assert len(hits) == 1
    a = hits[0]
    assert a["kind"] == "drop"
    assert a["direction"] == "down"
    assert "baseline_drop" in a["triggers"]
    assert "wow_drop" in a["triggers"]
    assert a["dod_delta_usd"] == -40.0
    assert a["impact_usd"] == 40.0  # positive magnitude so impact ranking still works


def test_drop_to_zero_today_is_reported():
    # Steady spend, then nothing on the latest complete day (deleted / outage / broken pipeline).
    items = _series("Gone", 30.0, 28, resource_id="/r/gone")
    items.append(_point("Other", 1.0, resource_id="/r/other"))  # some row exists for today
    result = detect.detect_anomalies(items, today=TODAY, dimensions=("meterCategory", "resourceId"))
    hits = _by_value(result, "Gone") + _by_value(result, "/r/gone")
    assert {a["dimension"] for a in hits} == {"meterCategory", "resourceId"}
    for a in hits:
        assert a["kind"] == "drop"
        assert a["current_usd"] == 0.0
        assert a["impact_usd"] == 30.0


def test_detect_drops_false_keeps_spike_only_behaviour():
    items = _series("Shrinker", 50.0, 28) + [_point("Shrinker", 10.0)]
    items += _series("Gone", 30.0, 28, resource_id="/r/gone")
    result = detect.detect_anomalies(
        items, today=TODAY, dimensions=("meterCategory",), detect_drops=False
    )
    assert result == []


def test_small_drop_below_floor_not_flagged():
    items = _series("Tiny", 4.0, 28) + [_point("Tiny", 0.5)]
    assert detect.detect_anomalies(items, today=TODAY, dimensions=("meterCategory",)) == []


def test_spikes_have_up_direction():
    items = _series("Container Apps", 10.0, 28) + [_point("Container Apps", 100.0)]
    a = detect.detect_anomalies(items, today=TODAY, dimensions=("meterCategory",))[0]
    assert a["direction"] == "up"
    new = detect.detect_anomalies(
        [_point("Fresh", 40.0)], today=TODAY, dimensions=("meterCategory",)
    )[0]
    assert new["direction"] == "up"


# --- credits / negative rows ---------------------------------------------------------------------


def test_negative_rows_do_not_mask_spike_or_create_drop():
    items = _series("Compute", 20.0, 28) + [_point("Compute", 200.0)]
    refund = _point("Compute", -180.0)
    refund["chargeType"] = "Refund"
    result = detect.detect_anomalies(items + [refund], today=TODAY, dimensions=("meterCategory",))
    hits = _by_value(result, "Compute")
    assert len(hits) == 1
    assert hits[0]["kind"] == "spike"
    assert hits[0]["current_usd"] == 200.0

    # A credit on a flat day must not produce a fake drop.
    flat = _series("Flat", 20.0, 28) + [_point("Flat", 20.0), _point("Flat", -20.0)]
    assert detect.detect_anomalies(flat, today=TODAY, dimensions=("meterCategory",)) == []


def test_summarize_credits_reports_negative_rows():
    items = _series("Compute", 20.0, 3)
    c1 = _point("Compute", -50.0, offset=0, resource_id="/r/a", rg="rg1")
    c1["chargeType"] = "Refund"
    c2 = _point("Reservations", -10.0, offset=1, resource_id="/r/b", rg="rg2")
    c2["chargeType"] = "Credit"
    out = detect.summarize_credits(items + [c1, c2], today=TODAY)
    assert out["today"] == TODAY.isoformat()
    assert out["total_credit_usd"] == 60.0
    assert out["row_count"] == 2
    assert out["today_credit_usd"] == 50.0
    assert out["by_day"] == [
        {"date": _day(1), "credit_usd": 10.0, "row_count": 1},
        {"date": _day(0), "credit_usd": 50.0, "row_count": 1},
    ]
    assert out["by_dimension"]["resourceGroup"][0] == {
        "value": "rg1", "credit_usd": 50.0, "row_count": 1
    }
    assert [c["chargeType"] for c in out["by_charge_type"]] == ["Refund", "Credit"]
    assert out["top_rows"][0]["cost"] == -50.0
    assert out["top_rows"][0]["resourceId"] == "/r/a"


def test_summarize_credits_empty():
    out = detect.summarize_credits([])
    assert out["total_credit_usd"] == 0.0
    assert out["row_count"] == 0
    assert out["today"] is None


# --- tags as strings -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {"Env": "prod"},
        '{"Env": "prod", "team": "x"}',
        '"Env": "prod","team": "x"',
        '  "env": "prod"  ',
    ],
)
def test_tag_dimension_handles_dict_and_string_formats(raw):
    assert detect._dimension_value({"tags": raw}, "tag:env") == "prod"
    assert detect._dimension_value({"tags": raw}, "tag:ENV") == "prod"


@pytest.mark.parametrize("raw", ['"env": prod', "not json", "[1,2]", "", None, 42])
def test_malformed_tags_yield_no_tags(raw):
    assert detect.parse_tags(raw) == {}
    assert detect._dimension_value({"tags": raw}, "tag:env") is None


def test_tag_string_dimension_end_to_end():
    items = _series("Svc", 10.0, 28) + [_point("Svc", 100.0)]
    for i in items:
        i["tags"] = '"team": "payments"'
    result = detect.detect_anomalies(items, today=TODAY, dimensions=("tag:team",))
    assert [a["value"] for a in result] == ["payments"]


# --- sustained -----------------------------------------------------------------------------------


def test_sustained_step_change_vs_one_day_blip():
    step = _series("Step", 10.0, 26, start_offset=3) + [
        _point("Step", 100.0, offset=2),
        _point("Step", 100.0, offset=1),
        _point("Step", 100.0, offset=0),
    ]
    a = _by_value(detect.detect_anomalies(step, today=TODAY, dimensions=("meterCategory",)), "Step")
    assert len(a) == 1 and a[0]["kind"] == "spike"
    assert a[0]["sustained"] is True

    blip = _series("Blip", 10.0, 28) + [_point("Blip", 100.0)]
    b = _by_value(detect.detect_anomalies(blip, today=TODAY, dimensions=("meterCategory",)), "Blip")
    assert b[0]["sustained"] is False
