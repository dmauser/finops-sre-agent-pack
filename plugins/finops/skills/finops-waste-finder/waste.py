"""FinOps waste finder — pure, offline, deterministic.

Finds orphaned, idle, and stale Azure resources that keep billing (or cluttering) after the
workload they served is gone, from raw read-only Azure Resource Graph rows. It deliberately does
NOT repeat what `finops-rightsizing-advisor` already covers (unattached disks, stopped VMs, empty
App Service plans, idle Container Apps, Advisor cost recommendations), so the two can be summed.

Rules (resource type -> finding kind):
  public IP          no ipConfiguration and no natGateway                         orphan
  NAT gateway        no subnets                                                     orphan
  private endpoint   every connection Disconnected/Rejected                         orphan
  load balancer      Standard/Gateway SKU with no backend pool members              idle
  application gw     running, every backend pool empty                              idle
  VPN/ER gateway     no connection references it and no point-to-site pool          idle
  snapshot           older than `snapshot_age_days`                                 stale
  network interface  not attached to a VM / private endpoint / private link svc     hygiene
  Log Analytics      PerGB2018 at >= commitment-tier volume, or long retention     review

Savings: orphan/idle/stale findings save their full monthly cost when known (unknown cost is
kept and sorted last); hygiene saves nothing (clutter / IP exhaustion); review claims nothing.
"""

from datetime import date, datetime

_ORPHAN, _IDLE, _STALE, _HYGIENE, _REVIEW = "orphan", "idle", "stale", "hygiene", "review"
_DEAD_PE_STATES = {"disconnected", "rejected"}
_LA_COMMITMENT_TIER_GB = 100.0
_LA_FREE_RETENTION_DAYS = 90


def _text(value):
    return str(value or "").strip()


def _norm_id(value):
    return _text(value).rstrip("/").casefold()


def _props(row):
    props = row.get("properties") if isinstance(row, dict) else None
    return props if isinstance(props, dict) else {}


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _sku(row):
    sku = row.get("sku") or _props(row).get("sku")
    if isinstance(sku, dict):
        return _text(sku.get("name") or sku.get("tier"))
    return _text(sku)


def _parse_date(value):
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _in_boundary(rid, managed):
    if managed is None:
        return True
    norm = _norm_id(rid)
    return any(norm == m or norm.startswith(m + "/") for m in managed)


def _ref_id(value):
    return _norm_id(value.get("id")) if isinstance(value, dict) else ""


def _public_ip(row, _ctx):
    p = _props(row)
    if p.get("ipConfiguration") or p.get("natGateway"):
        return None
    evidence = ["No ipConfiguration or NAT gateway association."]
    if _sku(row).casefold() == "basic":
        evidence.append("Basic SKU public IPs are retired; migrate or delete.")
    return _ORPHAN, "Unassociated public IP address", "Delete the public IP (release it).", evidence


def _nat_gateway(row, _ctx):
    if _props(row).get("subnets"):
        return None
    return (_ORPHAN, "NAT gateway attached to no subnet",
            "Delete the NAT gateway or attach it to the subnet that needs outbound access.",
            ["properties.subnets is empty; NAT gateway bills hourly regardless."])


def _private_endpoint(row, _ctx):
    p = _props(row)
    conns = list(p.get("privateLinkServiceConnections") or []) + list(
        p.get("manualPrivateLinkServiceConnections") or [])
    if not conns:
        return None
    states = []
    for c in conns:
        state = ((c.get("properties") or {}).get("privateLinkServiceConnectionState") or {})
        states.append(_text(state.get("status")).casefold())
    if not states or not all(s in _DEAD_PE_STATES for s in states):
        return None
    return (_ORPHAN, "Private endpoint whose target connection is disconnected or rejected",
            "Delete the private endpoint (and its NIC/DNS record) or re-approve the connection.",
            [f"Connection status: {', '.join(sorted(set(states)))}."])


def _pool_empty(pool):
    pp = pool.get("properties") or {}
    return not (pp.get("backendIPConfigurations") or pp.get("loadBalancerBackendAddresses")
                or pp.get("backendAddresses"))


def _load_balancer(row, _ctx):
    if _sku(row).casefold() == "basic":
        return None
    pools = _props(row).get("backendAddressPools") or []
    if pools and not all(_pool_empty(pool) for pool in pools):
        return None
    detail = "no backend pools" if not pools else "every backend pool is empty"
    return (_IDLE, f"Standard load balancer with {detail}",
            "Delete the load balancer if no longer needed (rules and data processing still bill).",
            [f"{len(pools)} backend pool(s), 0 members."])


def _app_gateway(row, _ctx):
    p = _props(row)
    if _text(p.get("operationalState")).casefold() == "stopped":
        return None
    pools = p.get("backendAddressPools") or []
    if pools and not all(_pool_empty(pool) for pool in pools):
        return None
    return (_IDLE, "Application gateway with no backend targets",
            "Stop or delete the application gateway; it bills per hour while running.",
            [f"{len(pools)} backend pool(s), 0 targets; operationalState "
             f"{_text(p.get('operationalState')) or 'unknown'}."])


def _vnet_gateway(row, ctx):
    if not ctx["connections_seen"]:
        return None
    p = _props(row)
    if _norm_id(row.get("id")) in ctx["connected_gateways"]:
        return None
    vpn_client = p.get("vpnClientConfiguration") or {}
    if ((vpn_client.get("vpnClientAddressPool") or {}).get("addressPrefixes")):
        return None
    gw_type = _text(p.get("gatewayType")) or "Virtual network"
    return (_IDLE, f"{gw_type} gateway with no connections",
            "Delete the gateway if the site-to-site / ExpressRoute link is decommissioned.",
            ["No microsoft.network/connections resource references this gateway and no "
             "point-to-site address pool is configured."])


def _snapshot(row, ctx):
    created = _parse_date(_props(row).get("timeCreated"))
    if created is None:
        return None
    age = (ctx["as_of"] - created).days
    if age < ctx["snapshot_age_days"]:
        return None
    size = _num(_props(row).get("diskSizeGB"))
    evidence = [f"Created {created.isoformat()} ({age} days ago)."]
    if size is not None:
        evidence.append(f"{size:g} GB.")
    return (_STALE, f"Snapshot older than {ctx['snapshot_age_days']} days",
            "Delete if not required by a backup/retention policy; otherwise move to "
            "Standard_LRS incremental snapshots or Azure Backup.", evidence)


def _nic(row, _ctx):
    p = _props(row)
    if p.get("virtualMachine") or p.get("privateEndpoint") or p.get("privateLinkService"):
        return None
    if _text(p.get("nicType")).casefold() == "elastic" and p.get("hostedWorkloads"):
        return None
    return (_HYGIENE, "Network interface attached to nothing",
            "Delete the NIC to free its private IP and reduce clutter (no direct cost).",
            ["No virtualMachine, privateEndpoint, or privateLinkService association."])


def _log_analytics(row, ctx):
    p = _props(row)
    sku = _text((p.get("sku") or {}).get("name") if isinstance(p.get("sku"), dict) else "")
    retention = _num(p.get("retentionInDays"))
    ingestion = ctx["ingestion"].get(_norm_id(row.get("id")))
    reasons, actions, evidence = [], [], []
    if sku.casefold() == "pergb2018" and ingestion is not None and \
            ingestion >= ctx["la_commitment_gb_per_day"]:
        reasons.append("pay-as-you-go ingestion at commitment-tier volume")
        actions.append(f"Evaluate a {int(ctx['la_commitment_gb_per_day'])} GB/day+ commitment tier")
        evidence.append(f"PerGB2018 SKU ingesting {ingestion:.1f} GB/day on average.")
    if retention is not None and retention > _LA_FREE_RETENTION_DAYS:
        reasons.append("interactive retention above 90 days")
        actions.append("Move older data to long-term (archive) retention or lower interactive "
                       "retention per table")
        evidence.append(f"retentionInDays={retention:g}.")
    if not reasons:
        return None
    return (_REVIEW, "Log Analytics workspace: " + "; ".join(reasons),
            "; ".join(actions) + ".", evidence)


_RULES = {
    "microsoft.network/publicipaddresses": _public_ip,
    "microsoft.network/natgateways": _nat_gateway,
    "microsoft.network/privateendpoints": _private_endpoint,
    "microsoft.network/loadbalancers": _load_balancer,
    "microsoft.network/applicationgateways": _app_gateway,
    "microsoft.network/virtualnetworkgateways": _vnet_gateway,
    "microsoft.compute/snapshots": _snapshot,
    "microsoft.network/networkinterfaces": _nic,
    "microsoft.operationalinsights/workspaces": _log_analytics,
}
SUPPORTED_TYPES = tuple(sorted(_RULES))


def find_waste(resources, *, costs=None, ingestion_gb_per_day=None, managed_scopes=None,
               currency=None, as_of=None, snapshot_age_days=90,
               la_commitment_gb_per_day=_LA_COMMITMENT_TIER_GB, min_monthly_savings=0.0,
               connections_queried=False):
    """Classify raw Resource Graph rows into waste findings.

    resources            ARG rows with id, type, sku, properties (include
                         microsoft.network/connections rows so gateways can be checked)
    connections_queried  True when the Resource Graph query included microsoft.network/connections,
                         so zero connection rows means no gateway has a connection
    costs                {resourceId: monthly cost} from prepared UsageDetails (case-insensitive)
    ingestion_gb_per_day {workspaceId: average billable GB/day} for Log Analytics workspaces
    managed_scopes       effective ARM scopes; rows outside are dropped defensively
    """

    today = as_of or date.today()
    managed = None if managed_scopes is None else [
        _norm_id(s) for s in managed_scopes if _text(s)]
    cost_map = {_norm_id(k): _num(v) for k, v in (costs or {}).items()}
    rows = [r for r in resources or [] if isinstance(r, dict) and _text(r.get("id"))]

    connected, connections_seen = set(), bool(connections_queried)
    for r in rows:
        if _text(r.get("type")).casefold() == "microsoft.network/connections":
            connections_seen = True
            p = _props(r)
            for key in ("virtualNetworkGateway1", "virtualNetworkGateway2"):
                if _ref_id(p.get(key)):
                    connected.add(_ref_id(p.get(key)))

    ctx = {
        "as_of": today,
        "snapshot_age_days": snapshot_age_days,
        "connected_gateways": connected,
        "connections_seen": connections_seen,
        "ingestion": {_norm_id(k): _num(v) for k, v in (ingestion_gb_per_day or {}).items()},
        "la_commitment_gb_per_day": la_commitment_gb_per_day,
    }

    findings, excluded, seen = [], 0, set()
    for r in rows:
        rid = _text(r.get("id"))
        key = _norm_id(rid)
        if key in seen:
            continue
        seen.add(key)
        if not _in_boundary(rid, managed):
            excluded += 1
            continue
        rule = _RULES.get(_text(r.get("type")).casefold())
        if rule is None:
            continue
        hit = rule(r, ctx)
        if hit is None:
            continue
        kind, reason, action, evidence = hit
        monthly = cost_map.get(key)
        if kind in (_ORPHAN, _IDLE, _STALE):
            savings = monthly
        elif kind == _HYGIENE:
            savings = 0.0
        else:
            savings = None
        if savings is not None and kind != _HYGIENE and savings < min_monthly_savings:
            continue
        findings.append({
            "resourceId": rid,
            "resourceType": _text(r.get("type")).casefold(),
            "kind": kind,
            "reason": reason,
            "recommendedAction": action,
            "monthlyCost": round(monthly, 2) if monthly is not None else None,
            "estMonthlySavings": round(savings, 2) if savings is not None else None,
            "evidence": evidence,
            "tags": r.get("tags") if isinstance(r.get("tags"), dict) else {},
        })

    findings.sort(key=lambda f: (f["estMonthlySavings"] is None,
                                 -(f["estMonthlySavings"] or 0.0),
                                 f["kind"], f["resourceId"].casefold()))

    by_kind = {}
    for f in findings:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
    actionable = [f for f in findings if f["kind"] in (_ORPHAN, _IDLE, _STALE)]
    warnings = []
    if excluded:
        warnings.append(f"{excluded} resource(s) outside the managed boundary were excluded")
    unknown = sum(1 for f in actionable if f["estMonthlySavings"] is None)
    if unknown:
        warnings.append(f"{unknown} actionable finding(s) have no cost data; savings are understated")
    if any(_text(r.get("type")).casefold() == "microsoft.network/virtualnetworkgateways"
           for r in rows) and not connections_seen:
        warnings.append("Gateway rule skipped: include microsoft.network/connections in the "
                        "Resource Graph query to evaluate VPN/ExpressRoute gateways")

    return {
        "as_of": today.isoformat(),
        "currency": currency,
        "headline": {
            "finding_count": len(findings),
            "actionable_count": len(actionable),
            "potential_monthly_savings": round(sum(
                f["estMonthlySavings"] or 0.0 for f in actionable), 2),
            "unknown_cost_count": unknown,
            "by_kind": dict(sorted(by_kind.items())),
        },
        "findings": findings,
        "warnings": warnings,
    }
