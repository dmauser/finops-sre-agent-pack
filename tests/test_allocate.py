"""Layer-1 unit tests for the cost-allocation (showback) skill (offline, deterministic)."""

import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "finops"
    / "skills"
    / "finops-cost-allocation"
    / "allocate.py"
)
_spec = importlib.util.spec_from_file_location("allocate", _PATH)
allocate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(allocate)

allocate_costs = allocate.allocate_costs

A = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/a"
B = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/b"
C = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Cache/Redis/c"
D = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.App/sessionPools/d"


def test_empty_inputs():
    out = allocate_costs()
    assert out["total_usd"] == 0.0
    assert out["groups"] == []
    assert out["unallocated_usd"] == 0.0


def test_group_by_team_and_totals():
    costs = {A: 100.0, B: 40.0, C: 60.0}
    tags = {A: {"team": "payments"}, B: {"team": "payments"}, C: {"team": "search"}}
    out = allocate_costs(costs, tags, dimension="team")
    assert out["total_usd"] == 200.0
    assert out["allocated_usd"] == 200.0
    assert out["unallocated_usd"] == 0.0
    g = {x["owner"]: x for x in out["groups"]}
    assert g["payments"]["monthly_usd"] == 140.0
    assert g["payments"]["resource_count"] == 2
    assert g["payments"]["pct"] == 70.0
    assert g["search"]["monthly_usd"] == 60.0
    # groups sorted by cost desc
    assert out["groups"][0]["owner"] == "payments"


def test_unallocated_bucket_not_force_allocated():
    costs = {A: 100.0, C: 331.0}
    tags = {A: {"team": "payments"}}  # C has no team tag
    out = allocate_costs(costs, tags, dimension="team")
    assert out["allocated_usd"] == 100.0
    assert out["unallocated_usd"] == 331.0
    assert out["unallocated_pct"] == 76.8
    assert out["unallocated"]["resource_count"] == 1
    assert out["unallocated"]["resources"][0]["resourceId"] == C
    # the single group is unchanged — shared cost is NOT spread onto payments
    assert out["groups"][0]["monthly_usd"] == 100.0


def test_untagged_means_no_tags_at_all():
    costs = {A: 10.0, B: 20.0, C: 30.0}
    # A has only a NON-recommended tag (foo); B has nothing; C has a recommended tag
    tags = {A: {"foo": "bar"}, C: {"owner": "nir"}}
    out = allocate_costs(costs, tags, dimension="team")
    untagged_ids = [r["resourceId"] for r in out["untagged_resources"]]
    assert untagged_ids == [B]  # only B has NO tags at all; A's non-recommended tag still counts
    assert out["untagged_usd"] == 20.0


def test_case_insensitive_tag_key_and_resource_id():
    costs = {A.upper(): 50.0}
    tags = {A: {"Team": "Payments"}}  # different id casing + capitalized key
    out = allocate_costs(costs, tags, dimension="team")
    assert out["unallocated_usd"] == 0.0
    assert out["groups"][0]["owner"] == "Payments"
    assert out["groups"][0]["monthly_usd"] == 50.0


def test_tag_hygiene_flags_variant_spellings():
    costs = {A: 100.0, B: 60.0, C: 10.0}
    tags = {A: {"team": "Prod"}, B: {"team": "prod"}, C: {"team": "PROD"}}
    out = allocate_costs(costs, tags, dimension="team")
    # all three collapse to one owner group
    assert len(out["groups"]) == 1
    assert out["groups"][0]["monthly_usd"] == 170.0
    assert out["groups"][0]["owner"] == "Prod"  # most costly raw spelling
    hy = out["tag_hygiene"]
    assert len(hy) == 1
    assert hy[0]["canonical"] == "Prod"
    assert hy[0]["cost_affected"] == 170.0
    assert set(hy[0]["variants"].keys()) == {"Prod", "prod", "PROD"}


def test_no_hygiene_flag_when_consistent():
    costs = {A: 100.0, B: 60.0}
    tags = {A: {"team": "payments"}, B: {"team": "payments"}}
    out = allocate_costs(costs, tags, dimension="team")
    assert out["tag_hygiene"] == []


def test_dimension_selects_key():
    costs = {A: 100.0, B: 40.0}
    tags = {A: {"team": "pay", "env": "prod"}, B: {"team": "pay", "env": "dev"}}
    out = allocate_costs(costs, tags, dimension="env")
    owners = {x["owner"]: x["monthly_usd"] for x in out["groups"]}
    assert owners == {"prod": 100.0, "dev": 40.0}


def test_non_numeric_cost_ignored():
    costs = {A: 100.0, B: None, C: "oops"}
    tags = {A: {"team": "x"}}
    out = allocate_costs(costs, tags, dimension="team")
    assert out["total_usd"] == 100.0


def test_empty_inputs_have_new_tag_fields():
    out = allocate_costs()
    assert out["tag_inventory"] == []
    assert out["missing_recommended"] == list(allocate.RECOMMENDED_TAG_KEYS)
    assert all(c["present"] is False for c in out["recommended_coverage"])


def test_tag_inventory_lists_every_key_present_ranked_by_cost():
    costs = {A: 100.0, B: 40.0, C: 60.0}
    # A: team+env, B: env only, C: a non-recommended key
    tags = {A: {"team": "pay", "env": "prod"}, B: {"env": "dev"}, C: {"foo": "bar"}}
    out = allocate_costs(costs, tags, dimension="team")
    inv = {t["key"]: t for t in out["tag_inventory"]}
    assert inv["env"]["resource_count"] == 2
    assert inv["env"]["cost_usd"] == 140.0     # A(100) + B(40)
    assert inv["team"]["cost_usd"] == 100.0
    assert inv["foo"]["cost_usd"] == 60.0       # non-recommended keys still inventoried
    # ranked by cost desc: env(140) > team(100) > foo(60)
    assert [t["key"] for t in out["tag_inventory"]] == ["env", "team", "foo"]


def test_recommended_coverage_present_and_missing():
    costs = {A: 100.0, B: 100.0}
    tags = {A: {"team": "pay"}, B: {"env": "prod"}}
    out = allocate_costs(costs, tags, dimension="team")
    cov = {c["key"]: c for c in out["recommended_coverage"]}
    assert cov["team"]["present"] is True
    assert cov["team"]["cost_usd"] == 100.0
    assert cov["team"]["pct"] == 50.0
    assert cov["service"]["present"] is False
    assert cov["service"]["cost_usd"] == 0.0
    # recommended order is preserved
    assert [c["key"] for c in out["recommended_coverage"]] == list(allocate.RECOMMENDED_TAG_KEYS)
    assert "team" not in out["missing_recommended"]
    assert "service" in out["missing_recommended"]
    assert "costCenter" in out["missing_recommended"]


def test_recommended_keys_overridable():
    costs = {A: 100.0}
    tags = {A: {"project": "atlas"}}
    out = allocate_costs(costs, tags, dimension="project", recommended_keys=("project", "owner"))
    cov = {c["key"]: c for c in out["recommended_coverage"]}
    assert set(cov) == {"project", "owner"}
    assert cov["project"]["present"] is True
    assert out["missing_recommended"] == ["owner"]


# --- tag inheritance -----------------------------------------------------------------------------

SUB = "/subscriptions/s"
RG = "/subscriptions/s/resourceGroups/rg"
X = "/subscriptions/s/resourceGroups/other/providers/Microsoft.Web/sites/x"


def test_inheritance_none_is_unchanged():
    costs = {A: 100.0, C: 50.0}
    tags = {A: {"team": "pay"}}
    base = allocate_costs(costs, tags)
    assert base == allocate_costs(costs, tags, inherited_tags=None)
    assert base["inheritance"] == {
        "enabled": False, "allocated_via_inheritance_usd": 0.0, "resource_count": 0
    }
    assert base["inherited_usd"] == 0.0
    assert base["unallocated_usd"] == 50.0


def test_inheritance_rg_then_subscription_and_resource_wins():
    costs = {A: 100.0, B: 40.0, C: 60.0, X: 25.0}
    tags = {A: {"team": "own"}, B: {"env": "prod"}}   # C and X have no tags at all
    inherited = {
        RG.upper() + "/": {"Team": "rg-team"},          # case/trailing-slash insensitive
        SUB: {"team": "sub-team"},
    }
    out = allocate_costs(costs, tags, inherited_tags=inherited)
    g = {x["owner"]: x for x in out["groups"]}
    assert g["own"]["monthly_usd"] == 100.0 and g["own"]["inherited_usd"] == 0.0  # resource wins
    assert g["rg-team"]["monthly_usd"] == 100.0  # B + C from the RG
    assert g["rg-team"]["inherited_usd"] == 100.0
    assert g["sub-team"]["monthly_usd"] == 25.0  # X's RG has no tags -> subscription
    assert out["unallocated_usd"] == 0.0
    assert out["inherited_usd"] == 125.0
    assert out["inheritance"] == {
        "enabled": True, "allocated_via_inheritance_usd": 125.0, "resource_count": 3
    }
    # untagged list is still "no own tags", annotated with what inheritance filled
    un = {r["resourceId"]: r for r in out["untagged_resources"]}
    assert set(un) == {C, X}
    assert un[C]["inherited_owner"] == "rg-team" and un[C]["inherited_from"] == "resourceGroup"
    assert un[X]["inherited_owner"] == "sub-team" and un[X]["inherited_from"] == "subscription"
    # inventory still reflects own tags only
    assert {t["key"] for t in out["tag_inventory"]} == {"team", "env"}


# --- shared-cost split ---------------------------------------------------------------------------

def _shared_fixture():
    costs = {A: 300.0, B: 100.0, C: 80.0, D: 20.0}
    tags = {A: {"team": "pay"}, B: {"team": "search"}, C: {"shared": "TRUE"}}  # D unallocated
    return costs, tags


def _reconciles(out):
    total = sum(g["fully_loaded_usd"] for g in out["groups"])
    total += out["unallocated_usd"] + out["undistributed_usd"]
    return abs(total - out["total_usd"]) < 0.05


def test_shared_proportional_by_tag():
    costs, tags = _shared_fixture()
    out = allocate_costs(costs, tags, shared={"tag": {"key": "Shared", "value": "true"}})
    g = {x["owner"]: x for x in out["groups"]}
    assert g["pay"]["monthly_usd"] == g["pay"]["direct_usd"] == 300.0
    assert g["pay"]["shared_usd"] == 60.0 and g["pay"]["fully_loaded_usd"] == 360.0
    assert g["search"]["shared_usd"] == 20.0
    sc = out["shared_costs"]
    assert sc["total_usd"] == 80.0 and sc["method"] == "proportional"
    assert sc["resources"] == [{"resourceId": C, "monthly_usd": 80.0}]
    assert sc["distribution"] == [
        {"owner": "pay", "shared_usd": 60.0}, {"owner": "search", "shared_usd": 20.0}
    ]
    assert out["unallocated_usd"] == 20.0  # unallocated is never a recipient
    assert out["allocated_usd"] == 480.0
    assert _reconciles(out)


def test_shared_even_by_resource_id():
    costs, tags = _shared_fixture()
    out = allocate_costs(costs, tags, shared={"resource_ids": [C.upper()], "method": "even"})
    g = {x["owner"]: x for x in out["groups"]}
    assert g["pay"]["shared_usd"] == g["search"]["shared_usd"] == 40.0
    assert _reconciles(out)


def test_shared_fixed_weights():
    costs, tags = _shared_fixture()
    out = allocate_costs(
        costs, tags,
        shared={"resource_ids": [C], "method": "fixed", "weights": {"Pay": 1, "search": 3, "ghost": 5}},
    )
    g = {x["owner"]: x for x in out["groups"]}
    assert g["pay"]["shared_usd"] == 20.0 and g["search"]["shared_usd"] == 60.0
    assert out["undistributed_usd"] == 0.0
    assert _reconciles(out)


def test_shared_without_recipients_is_undistributed():
    costs = {C: 80.0, D: 20.0}
    out = allocate_costs(costs, {}, shared={"resource_ids": [C]})
    assert out["groups"] == []
    assert out["undistributed_usd"] == 80.0
    assert out["shared_costs"]["undistributed_usd"] == 80.0
    assert out["unallocated_usd"] == 20.0
    assert out["allocated_usd"] == 0.0
    assert _reconciles(out)


def test_shared_fixed_with_no_matching_weights_is_undistributed():
    costs, tags = _shared_fixture()
    out = allocate_costs(costs, tags, shared={"resource_ids": [C], "method": "fixed", "weights": {"x": 1}})
    assert out["undistributed_usd"] == 80.0
    assert _reconciles(out)


def test_shared_invalid_method_raises():
    import pytest
    with pytest.raises(ValueError):
        allocate_costs({A: 1.0}, {}, shared={"resource_ids": [A], "method": "bogus"})


def test_groups_without_shared_have_fully_loaded_equal_direct():
    out = allocate_costs({A: 10.0}, {A: {"team": "t"}})
    g = out["groups"][0]
    assert g["direct_usd"] == g["fully_loaded_usd"] == g["monthly_usd"] == 10.0
    assert g["shared_usd"] == 0.0
    assert out["shared_costs"]["method"] is None and out["undistributed_usd"] == 0.0


# --- month over month ----------------------------------------------------------------------------

def test_month_over_month_movers_and_owners():
    prev = {A: 100.0, B: 50.0, C: 40.0, D: 30.0}
    cur = {A.upper(): 160.0, B: 20.0, C: 45.0, X: 70.0}   # D removed, X new, C below threshold
    tags = {A: {"team": "pay"}, B: {"Team": "search"}, X: {"team": "pay"}}
    out = allocate.month_over_month(cur, prev, dimension_tags=tags)
    assert out["previous_usd"] == 220.0 and out["current_usd"] == 295.0
    assert out["delta_usd"] == 75.0 and out["delta_pct"] == 34.1
    m = out["movers"]
    assert [r["resourceId"] for r in m["new"]] == [X] and m["new"][0]["delta_pct"] is None
    assert [r["resourceId"] for r in m["removed"]] == [D] and m["removed"][0]["delta_usd"] == -30.0
    assert m["increased"][0]["delta_usd"] == 60.0 and m["increased"][0]["owner"] == "pay"
    assert m["decreased"][0]["resourceId"] == B and m["decreased"][0]["delta_pct"] == -60.0
    b = out["bridge"]
    assert b == {"new_usd": 70.0, "removed_usd": -30.0, "increased_usd": 60.0,
                 "decreased_usd": -30.0, "below_threshold_usd": 5.0}
    assert round(sum(b.values()), 2) == out["delta_usd"]
    owners = {o["owner"]: o for o in out["by_owner"]}
    assert owners["pay"]["delta_usd"] == 130.0
    assert owners["search"]["delta_usd"] == -30.0
    assert owners["(unallocated)"]["delta_usd"] == -25.0
    assert out["by_owner"][0]["owner"] == "pay"  # sorted by absolute delta


def test_month_over_month_empty_previous():
    out = allocate.month_over_month({A: 50.0}, {})
    assert out["delta_pct"] is None
    assert out["movers"]["new"][0]["delta_usd"] == 50.0
