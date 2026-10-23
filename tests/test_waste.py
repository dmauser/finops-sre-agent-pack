"""Layer-1 unit tests for the waste finder (offline, deterministic, no Azure)."""

import importlib.util
from datetime import date
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "finops" / "skills" / "finops-waste-finder" / "waste.py"
)
_spec = importlib.util.spec_from_file_location("waste", _PATH)
waste = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(waste)

find = waste.find_waste

SUB = "/subscriptions/s1"
RG = f"{SUB}/resourceGroups/rg/providers"
TODAY = date(2025, 6, 1)


def _row(kind, name, props=None, sku=None, tags=None):
    provider = {
        "microsoft.network/publicipaddresses": "Microsoft.Network/publicIPAddresses",
        "microsoft.network/natgateways": "Microsoft.Network/natGateways",
        "microsoft.network/privateendpoints": "Microsoft.Network/privateEndpoints",
        "microsoft.network/loadbalancers": "Microsoft.Network/loadBalancers",
        "microsoft.network/applicationgateways": "Microsoft.Network/applicationGateways",
        "microsoft.network/virtualnetworkgateways": "Microsoft.Network/virtualNetworkGateways",
        "microsoft.network/connections": "Microsoft.Network/connections",
        "microsoft.compute/snapshots": "Microsoft.Compute/snapshots",
        "microsoft.network/networkinterfaces": "Microsoft.Network/networkInterfaces",
        "microsoft.operationalinsights/workspaces": "Microsoft.OperationalInsights/workspaces",
        "microsoft.compute/disks": "Microsoft.Compute/disks",
    }[kind]
    row = {"id": f"{RG}/{provider}/{name}", "type": kind, "properties": props or {}}
    if sku is not None:
        row["sku"] = {"name": sku}
    if tags is not None:
        row["tags"] = tags
    return row


def _kinds(result):
    return {f["resourceId"].rsplit("/", 1)[-1]: f["kind"] for f in result["findings"]}


def test_public_ip_orphan_only_when_unassociated():
    rows = [
        _row("microsoft.network/publicipaddresses", "free", sku="Standard"),
        _row("microsoft.network/publicipaddresses", "used",
             {"ipConfiguration": {"id": "x"}}, sku="Standard"),
        _row("microsoft.network/publicipaddresses", "nat", {"natGateway": {"id": "n"}}),
        _row("microsoft.network/publicipaddresses", "basic", sku="Basic"),
    ]
    result = find(rows, as_of=TODAY)
    assert _kinds(result) == {"free": "orphan", "basic": "orphan"}
    basic = next(f for f in result["findings"] if f["resourceId"].endswith("basic"))
    assert any("retired" in e for e in basic["evidence"])


def test_nat_gateway_and_private_endpoint_orphans():
    def pe(name, *statuses):
        return _row("microsoft.network/privateendpoints", name, {
            "privateLinkServiceConnections": [
                {"properties": {"privateLinkServiceConnectionState": {"status": s}}}
                for s in statuses
            ]})

    rows = [
        _row("microsoft.network/natgateways", "nat-empty", {"subnets": []}),
        _row("microsoft.network/natgateways", "nat-used", {"subnets": [{"id": "s"}]}),
        pe("pe-dead", "Disconnected", "Rejected"),
        pe("pe-mixed", "Approved", "Disconnected"),
        pe("pe-ok", "Approved"),
    ]
    assert _kinds(find(rows, as_of=TODAY)) == {"nat-empty": "orphan", "pe-dead": "orphan"}


def test_load_balancer_and_app_gateway_idle_rules():
    empty_pool = [{"properties": {}}]
    full_pool = [{"properties": {"backendIPConfigurations": [{"id": "ip"}]}}]
    rows = [
        _row("microsoft.network/loadbalancers", "lb-empty",
             {"backendAddressPools": empty_pool}, sku="Standard"),
        _row("microsoft.network/loadbalancers", "lb-none", {}, sku="Standard"),
        _row("microsoft.network/loadbalancers", "lb-used",
             {"backendAddressPools": full_pool}, sku="Standard"),
        _row("microsoft.network/loadbalancers", "lb-basic", {}, sku="Basic"),
        _row("microsoft.network/applicationgateways", "agw-idle",
             {"operationalState": "Running", "backendAddressPools": empty_pool}),
        _row("microsoft.network/applicationgateways", "agw-stopped",
             {"operationalState": "Stopped", "backendAddressPools": empty_pool}),
        _row("microsoft.network/applicationgateways", "agw-used",
             {"operationalState": "Running", "backendAddressPools": [
                 {"properties": {"backendAddresses": [{"fqdn": "a"}]}}]}),
    ]
    assert _kinds(find(rows, as_of=TODAY)) == {
        "lb-empty": "idle", "lb-none": "idle", "agw-idle": "idle"}


def test_gateway_rule_needs_connections_inventory():
    gw_live = _row("microsoft.network/virtualnetworkgateways", "gw-live", {"gatewayType": "Vpn"})
    gw_dead = _row("microsoft.network/virtualnetworkgateways", "gw-dead",
                   {"gatewayType": "ExpressRoute"})
    gw_p2s = _row("microsoft.network/virtualnetworkgateways", "gw-p2s", {
        "gatewayType": "Vpn",
        "vpnClientConfiguration": {"vpnClientAddressPool": {"addressPrefixes": ["10.9.0.0/24"]}}})
    conn = _row("microsoft.network/connections", "c1",
                {"virtualNetworkGateway1": {"id": gw_live["id"].upper()}})

    skipped = find([gw_live, gw_dead], as_of=TODAY)
    assert skipped["findings"] == []
    assert any("connections" in w for w in skipped["warnings"])

    result = find([gw_live, gw_dead, gw_p2s, conn], as_of=TODAY)
    assert _kinds(result) == {"gw-dead": "idle"}
    assert "ExpressRoute" in result["findings"][0]["reason"]


def test_snapshot_age_threshold():
    rows = [
        _row("microsoft.compute/snapshots", "old", {"timeCreated": "2025-01-01T00:00:00Z"}),
        _row("microsoft.compute/snapshots", "new", {"timeCreated": "2025-05-01T00:00:00Z"}),
        _row("microsoft.compute/snapshots", "undated", {}),
    ]
    assert _kinds(find(rows, as_of=TODAY)) == {"old": "stale"}
    assert _kinds(find(rows, as_of=TODAY, snapshot_age_days=30)) == {
        "old": "stale", "new": "stale"}


def test_nic_hygiene_has_zero_savings_and_is_not_counted():
    rows = [
        _row("microsoft.network/networkinterfaces", "nic-free"),
        _row("microsoft.network/networkinterfaces", "nic-vm", {"virtualMachine": {"id": "v"}}),
        _row("microsoft.network/networkinterfaces", "nic-pe", {"privateEndpoint": {"id": "p"}}),
    ]
    result = find(rows, costs={rows[0]["id"]: 5.0}, as_of=TODAY)
    [f] = result["findings"]
    assert f["kind"] == "hygiene" and f["estMonthlySavings"] == 0.0
    assert result["headline"]["potential_monthly_savings"] == 0.0
    assert result["headline"]["actionable_count"] == 0


def test_log_analytics_review_rules():
    big = _row("microsoft.operationalinsights/workspaces", "big",
               {"sku": {"name": "PerGB2018"}, "retentionInDays": 30})
    long = _row("microsoft.operationalinsights/workspaces", "long",
                {"sku": {"name": "PerGB2018"}, "retentionInDays": 365})
    tiered = _row("microsoft.operationalinsights/workspaces", "tiered",
                  {"sku": {"name": "CapacityReservation"}, "retentionInDays": 30})
    result = find([big, long, tiered],
                  ingestion_gb_per_day={big["id"]: 140.0, long["id"]: 5.0, tiered["id"]: 400.0},
                  costs={big["id"]: 9000.0}, as_of=TODAY)
    assert _kinds(result) == {"big": "review", "long": "review"}
    for f in result["findings"]:
        assert f["estMonthlySavings"] is None
    assert result["headline"]["potential_monthly_savings"] == 0.0


def test_rightsizing_owned_types_are_ignored():
    rows = [_row("microsoft.compute/disks", "d1", {"diskState": "Unattached"})]
    assert find(rows, as_of=TODAY)["findings"] == []


def test_ranking_cost_join_boundary_and_dedup():
    a = _row("microsoft.network/publicipaddresses", "a", tags={"owner": "team-a"})
    b = _row("microsoft.network/natgateways", "b", {"subnets": []})
    c = _row("microsoft.compute/snapshots", "c", {"timeCreated": "2020-01-01"})
    outside = dict(_row("microsoft.network/natgateways", "x"),
                   id="/subscriptions/other/resourceGroups/rg/providers/"
                      "Microsoft.Network/natGateways/x")
    result = find([a, b, c, dict(a), outside],
                  costs={a["id"].upper(): 3.65, b["id"]: 32.85},
                  managed_scopes=[SUB], currency="USD", as_of=TODAY)
    order = [f["resourceId"].rsplit("/", 1)[-1] for f in result["findings"]]
    assert order == ["b", "a", "c"]
    assert result["headline"]["potential_monthly_savings"] == 36.5
    assert result["headline"]["unknown_cost_count"] == 1
    assert result["findings"][1]["tags"] == {"owner": "team-a"}
    assert any("excluded" in w for w in result["warnings"])
    assert any("no cost data" in w for w in result["warnings"])


def test_min_monthly_savings_filters_cheap_known_findings():
    a = _row("microsoft.network/publicipaddresses", "cheap")
    b = _row("microsoft.network/publicipaddresses", "unknown")
    result = find([a, b], costs={a["id"]: 1.0}, min_monthly_savings=2.0, as_of=TODAY)
    assert _kinds(result) == {"unknown": "orphan"}
