"""FinOps cost-allocation (showback) — pure, offline, deterministic (no Azure calls).

Given per-resource monthly cost (from Consumption UsageDetails) and per-resource tags
(from Resource Graph), attribute every dollar to an owner dimension and — the governance win —
surface the spend that **has no owner**, plus an inventory of the tags you actually use and how
they measure up against a recommended ownership taxonomy.

The skill is deliberately **tag-generic**: it groups by whatever tag keys exist in your data (a
tag inventory), and treats `team / env / service / costCenter / app / owner` as a *recommended*
set to report coverage against — not a hard-coded filter that defines "tagged".

Design decisions (see SKILL.md):
  * Shared / untaggable cost is NEVER force-allocated. Cost with no value for the requested
    dimension lands in an explicit "unallocated" bucket so the math stays honest and the gap
    is the actionable signal.
  * A resource with **no tags at all** is "untagged" (dimension-independent) and is listed for
    tagging, ranked by cost. (Having *some* tag but not the requested dimension is "unallocated",
    not "untagged".)
  * The **tag inventory** reports every tag key present, with the resource count and cost it
    covers — the "group by the ones we have" view.
  * **Recommended coverage** reports, for each recommended ownership key, whether it is present
    and what share of cost it covers, so gaps become an adopt-these-tags recommendation.
  * Tag values are grouped case-insensitively (trimmed + lower-cased); the most costly raw
    spelling is shown. Values that collapse to the same owner are flagged as tag-hygiene issues
    (e.g. "Prod" vs "production") because they otherwise split one owner's spend.

Input shapes (all keyed/looked-up by resourceId, matched case-insensitively):

    costs   {resourceId: monthly_usd}                     # aggregated from UsageDetails costInUSD
    tags    {resourceId: {"team": "payments", "env": "Prod", ...}}   # from Resource Graph

Output: a single dict (see allocate_costs) — a showback breakdown, an unallocated bucket, a
ranked untagged-resource list, a tag inventory, recommended-coverage, and tag-hygiene flags.
Feed it to a table / Live Report.

Optional extensions (all off by default, so results are unchanged when not used):
  * Tag inheritance (`inherited_tags`): fill a missing dimension tag from the resource group, then
    the subscription — mirroring Azure Cost Management tag inheritance (resource tags win).
  * Shared-cost split (`shared`): remove shared resources from direct allocation and redistribute
    their cost across owner groups (proportional / even / fixed) to show fully-loaded cost.
  * `month_over_month(...)`: per-owner and per-resource variance between two months.
"""

from collections import defaultdict

# Recommended ownership taxonomy — advisory only. Coverage against this set is reported; it does
# NOT define whether a resource is "tagged" (that is now "has any tag at all").
RECOMMENDED_TAG_KEYS = ("team", "env", "service", "costCenter", "app", "owner")
DEFAULT_DIMENSION = "team"


def _key(resource_id) -> str:
    """Case-insensitive resource-id key (ARM ids are case-insensitive)."""
    return str(resource_id or "").strip().rstrip("/").lower()


def _norm_val(value) -> str:
    """Normalize a tag value for grouping: trimmed + lower-cased. '' if empty."""
    return str(value or "").strip().lower()


def _tags_ci(tag_map):
    """Return a copy of a resource's tag dict with keys lower-cased for lookup."""
    return {str(k).strip().lower(): v for k, v in (tag_map or {}).items()}


def _lookup_tag(ci_tags, wanted_key):
    """Case-insensitive tag lookup; returns the raw value or None if absent/empty."""
    raw = ci_tags.get(str(wanted_key).strip().lower())
    return raw if _norm_val(raw) else None


def _scope_key(scope_id) -> str:
    """Normalize a scope id (subscription / resource group) for case-insensitive lookup."""
    return _key(scope_id).rstrip("/")


def _parent_scopes(resource_id):
    """Return (resource_group_scope, subscription_scope) keys derived from an ARM resource id."""
    parts = [p for p in _key(resource_id).split("/") if p]
    sub = rg = None
    if len(parts) >= 2 and parts[0] == "subscriptions":
        sub = "/subscriptions/" + parts[1]
        if len(parts) >= 4 and parts[2] == "resourcegroups":
            rg = sub + "/resourcegroups/" + parts[3]
    return rg, sub


def _inherited_value(resource_id, inherited_by_scope, dim):
    """Look up `dim` on the resource group, then the subscription. Returns (raw, source) or (None, None)."""
    rg, sub = _parent_scopes(resource_id)
    for scope, source in ((rg, "resourceGroup"), (sub, "subscription")):
        if scope and scope in inherited_by_scope:
            raw = _lookup_tag(inherited_by_scope[scope], dim)
            if raw is not None:
                return raw, source
    return None, None


SHARED_METHODS = ("proportional", "even", "fixed")


def _is_shared(rid, ci_tags, shared_ids, shared_tag):
    if _key(rid) in shared_ids:
        return True
    if shared_tag:
        key, value = shared_tag
        raw = _lookup_tag(ci_tags, key)
        if raw is not None and (value is None or _norm_val(raw) == value):
            return True
    return False


def _split_shared(shared_total, group_cost, method, weights):
    """Distribute `shared_total` across owner groups. Returns ({norm: shared_usd}, undistributed)."""
    if shared_total == 0 or not group_cost:
        return {}, shared_total
    if method == "proportional":
        basis = {n: max(c, 0.0) for n, c in group_cost.items()}
        if sum(basis.values()) <= 0:
            basis = {n: 1.0 for n in group_cost}
    elif method == "even":
        basis = {n: 1.0 for n in group_cost}
    else:  # fixed
        wanted = {_norm_val(o): float(w) for o, w in (weights or {}).items()
                  if isinstance(w, (int, float)) and w > 0}
        basis = {n: wanted[n] for n in group_cost if n in wanted}
        if not basis:
            return {}, shared_total
    denom = sum(basis.values())
    return {n: shared_total * b / denom for n, b in basis.items()}, 0.0


def allocate_costs(
    costs=None,
    tags=None,
    *,
    dimension: str = DEFAULT_DIMENSION,
    recommended_keys=RECOMMENDED_TAG_KEYS,
    top_n_resources: int = 25,
    inherited_tags=None,
    inherited_tags_are_historical=False,
    shared=None,
    currency=None,
):
    """Attribute monthly cost to an owner dimension, keeping unallocated spend explicit.

    costs           {resourceId: monthly_usd}
    tags            {resourceId: {tagKey: tagVal}}
    dimension       the ownership tag key to group by (default "team")
    recommended_keys the advisory best-practice ownership keys to report coverage against; this
                    does NOT define "tagged" (a resource is untagged only if it has NO tags)
    top_n_resources cap on how many resources are listed inside the unallocated/untagged details
    inherited_tags  optional {scopeId: {tagKey: tagVal}} for resource groups
                    (/subscriptions/x/resourceGroups/rg) and subscriptions (/subscriptions/x). When a
                    resource lacks the dimension tag, the RG tag is used, then the subscription tag
                    (Azure Cost Management tag-inheritance semantics; resource tags always win).
                    Inheritance only fills the dimension grouping; tag inventory, coverage, and the
                    untagged list still reflect the resource's own tags.
    shared          optional {"resource_ids": [...], "tag": {"key", "value"}, "method":
                    "proportional"|"even"|"fixed", "weights": {owner: weight}}. Matching resources are
                    removed from direct allocation and their cost is redistributed across the owner
                    groups (never to unallocated). With no recipient groups (or no matching fixed
                    weights) it stays `undistributed_usd`.

    Returns a dict:
      {
        "dimension", "total_usd", "allocated_usd", "unallocated_usd", "unallocated_pct",
        "groups": [{"owner", "monthly_usd", "pct", "resource_count", "inherited_usd",
                    "direct_usd", "shared_usd", "fully_loaded_usd"}...] sorted desc,
                    # monthly_usd == direct_usd (backward compatible); fully_loaded adds shared
        "unallocated": {"monthly_usd", "pct", "resource_count", "resources": [{resourceId, monthly_usd}...]},
        "untagged_resources": [{resourceId, monthly_usd}...],   # NO tags at all
        "untagged_usd",
        "tag_inventory": [{"key", "resource_count", "cost_usd", "pct"}...],   # every tag key present, ranked
        "recommended_coverage": [{"key", "present", "resource_count", "cost_usd", "pct"}...],  # in recommended order
        "missing_recommended": [key...],   # recommended keys with no usage at all
        "tag_hygiene": [{"canonical", "variants": {raw: cost}, "cost_affected"}...],
        "inherited_usd",
        "inheritance": {"enabled", "allocated_via_inheritance_usd", "resource_count"},
        "shared_costs": {"total_usd", "method", "resources": [{resourceId, monthly_usd}...],
                         "distribution": [{"owner", "shared_usd"}...], "undistributed_usd"},
        "undistributed_usd",
      }

    `allocated_usd` is the fully-loaded allocated amount, so
    allocated_usd + unallocated_usd + undistributed_usd == total_usd (to rounding).
    Untagged entries gain "inherited_owner"/"inherited_from" when inheritance filled the dimension.
    """
    currency = currency or getattr(costs, "currency", None)
    nonresource_cost = getattr(costs, "nonresource_cost", 0.0)
    billing_rows = getattr(costs, "billing_rows", None)
    current_tags_available = tags is not None
    costs = costs if costs is not None else {}
    tags = tags or {}
    dim = str(dimension).strip()
    rec_keys = tuple(recommended_keys) or RECOMMENDED_TAG_KEYS

    # index raw tag maps case-insensitively by resource id (raw keys/values preserved for display)
    tags_by_id = {_key(rid): (t or {}) for rid, t in tags.items()}
    inherited_by_scope = {
        _scope_key(scope): _tags_ci(t) for scope, t in (
            inherited_tags or {} if billing_rows is None or inherited_tags_are_historical else {}
        ).items()
    }

    shared_cfg = shared or {}
    shared_ids = {_key(r) for r in (shared_cfg.get("resource_ids") or [])}
    shared_tag = None
    if shared_cfg.get("tag"):
        tag_key = str(shared_cfg["tag"].get("key") or "").strip()
        if tag_key:
            tag_val = shared_cfg["tag"].get("value")
            shared_tag = (tag_key, _norm_val(tag_val) if tag_val is not None else None)
    shared_method = str(shared_cfg.get("method") or "proportional").strip().lower()
    if shared and shared_method not in SHARED_METHODS:
        raise ValueError(f"shared method must be one of {SHARED_METHODS}, got {shared_method!r}")
    shared_cost = 0.0
    shared_resources = []
    group_inherited = defaultdict(float)
    inherited_cost = 0.0
    inherited_resources = set()

    group_cost = defaultdict(float)          # normalized owner value -> cost
    group_count = defaultdict(set)
    group_raw = defaultdict(lambda: defaultdict(float))  # norm -> {raw spelling: cost} (display + hygiene)
    key_cost = defaultdict(float)            # normalized tag key -> cost covered
    key_count = defaultdict(set)             # normalized tag key -> distinct resources
    key_raw = defaultdict(lambda: defaultdict(float))    # normalized tag key -> {raw key spelling: cost}
    unallocated_cost = nonresource_cost
    unallocated = []                          # resources with no value for `dim`
    untagged = []                             # resources with NO tags at all
    total = nonresource_cost

    entries = (
        [(row["resourceId"], float(row["cost"]), row["tags"]) for row in billing_rows]
        if billing_rows is not None else
        [(rid, monthly, tags_by_id.get(_key(rid), {})) for rid, monthly in costs.items()]
    )
    for rid, monthly, raw_tags in entries:
        if not isinstance(monthly, (int, float)):
            continue
        total += monthly
        ci = _tags_ci(raw_tags)

        # dimension grouping (shared resources are split later, not directly allocated)
        inherited_raw = inherited_from = None
        if shared and _is_shared(rid, ci, shared_ids, shared_tag):
            shared_cost += monthly
            shared_resources.append({"resourceId": rid, "monthly_usd": monthly})
        else:
            raw = _lookup_tag(ci, dim)
            if raw is None and inherited_by_scope:
                inherited_raw, inherited_from = _inherited_value(rid, inherited_by_scope, dim)
                raw = inherited_raw
            if raw is not None:
                norm = _norm_val(raw)
                group_cost[norm] += monthly
                group_count[norm].add(_key(rid))
                group_raw[norm][str(raw).strip()] += monthly
                if inherited_raw is not None:
                    group_inherited[norm] += monthly
                    inherited_cost += monthly
                    inherited_resources.add(_key(rid))
            else:
                unallocated_cost += monthly
                unallocated.append({"resourceId": rid, "monthly_usd": monthly})

        # tag inventory — count every tag key with a non-empty value on this resource
        present = False
        for raw_key, raw_val in raw_tags.items():
            if not _norm_val(raw_val):
                continue
            present = True
            kn = str(raw_key).strip().lower()
            key_cost[kn] += monthly
            key_count[kn].add(_key(rid))
            key_raw[kn][str(raw_key).strip()] += monthly

        # untagged = no tags at all (dimension-independent governance list)
        if not present:
            entry = {"resourceId": rid, "monthly_usd": monthly}
            if inherited_raw is not None:
                entry["inherited_owner"] = str(inherited_raw).strip()
                entry["inherited_from"] = inherited_from
            untagged.append(entry)

    shared_split, undistributed = _split_shared(
        shared_cost, group_cost, shared_method, shared_cfg.get("weights")
    )
    allocated_cost = total - unallocated_cost - undistributed

    groups = []
    for norm, cost in group_cost.items():
        display = max(group_raw[norm].items(), key=lambda kv: kv[1])[0]  # most-costly raw spelling
        groups.append({
            "owner": display,
            "monthly_usd": round(cost, 2),
            "pct": _pct(cost, total),
            "resource_count": len(group_count[norm]),
            "inherited_usd": round(group_inherited.get(norm, 0.0), 2),
            "direct_usd": round(cost, 2),
            "shared_usd": round(shared_split.get(norm, 0.0), 2),
            "fully_loaded_usd": round(cost + shared_split.get(norm, 0.0), 2),
        })
    groups.sort(key=lambda g: g["monthly_usd"], reverse=True)

    distribution = []
    for norm, amount in shared_split.items():
        display = max(group_raw[norm].items(), key=lambda kv: kv[1])[0]
        distribution.append({"owner": display, "shared_usd": round(amount, 2)})
    distribution.sort(key=lambda d: d["shared_usd"], reverse=True)
    def coalesce(rows):
        merged = {}
        for row in rows:
            key = _key(row["resourceId"])
            if key not in merged:
                merged[key] = {**row, "monthly_usd": 0.0}
            merged[key]["monthly_usd"] += row["monthly_usd"]
        return sorted(
            ({**row, "monthly_usd": round(row["monthly_usd"], 2)} for row in merged.values()),
            key=lambda row: (-row["monthly_usd"], _key(row["resourceId"])),
        )

    shared_resources = coalesce(shared_resources)
    unallocated = coalesce(unallocated)
    untagged = coalesce(untagged)
    untagged_usd = sum(r["monthly_usd"] for r in untagged)

    # tag inventory — every key present, ranked by cost covered
    tag_inventory = []
    for kn, cost in key_cost.items():
        display = max(key_raw[kn].items(), key=lambda kv: kv[1])[0]  # most-costly raw key spelling
        tag_inventory.append({
            "key": display,
            "resource_count": len(key_count[kn]),
            "cost_usd": round(cost, 2),
            "pct": _pct(cost, total),
        })
    tag_inventory.sort(key=lambda t: t["cost_usd"], reverse=True)

    # recommended coverage — for each recommended key, present? and how much cost it covers
    recommended_coverage = []
    missing_recommended = []
    for rk in rec_keys:
        kn = str(rk).strip().lower()
        present = kn in key_cost
        recommended_coverage.append({
            "key": rk,
            "present": present,
            "resource_count": len(key_count.get(kn, set())),
            "cost_usd": round(key_cost.get(kn, 0.0), 2),
            "pct": _pct(key_cost.get(kn, 0.0), total),
        })
        if not present:
            missing_recommended.append(rk)

    hygiene = []
    for norm, raws in group_raw.items():
        if len(raws) > 1:  # same owner spelled multiple ways -> splits their spend
            canonical = max(raws.items(), key=lambda kv: kv[1])[0]
            hygiene.append({
                "canonical": canonical,
                "variants": {k: round(v, 2) for k, v in sorted(raws.items(), key=lambda kv: kv[1], reverse=True)},
                "cost_affected": round(sum(raws.values()), 2),
            })
    hygiene.sort(key=lambda h: h["cost_affected"], reverse=True)

    return {
        "currency": currency,
        "allocation_basis": "billing_time" if billing_rows is not None else "current_inventory",
        "current_tag_governance": (
            allocate_costs(dict(costs), tags, dimension=dimension, currency=currency,
                           recommended_keys=recommended_keys, top_n_resources=top_n_resources)
            if billing_rows is not None and current_tags_available else None
        ),
        "warnings": (
            ["Current inherited tags were not applied to historical billing rows."]
            if billing_rows is not None and inherited_tags and not inherited_tags_are_historical else []
        ),
        "nonresource_cost": round(nonresource_cost, 2),
        "dimension": dim,
        "total_usd": round(total, 2),
        "allocated_usd": round(allocated_cost, 2),
        "unallocated_usd": round(unallocated_cost, 2),
        "unallocated_pct": _pct(unallocated_cost, total),
        "groups": groups,
        "unallocated": {
            "monthly_usd": round(unallocated_cost, 2),
            "pct": _pct(unallocated_cost, total),
            "resource_count": len(unallocated),
            "resources": unallocated[:top_n_resources],
        },
        "untagged_resources": untagged[:top_n_resources],
        "untagged_usd": round(untagged_usd, 2),
        "tag_inventory": tag_inventory,
        "recommended_coverage": recommended_coverage,
        "missing_recommended": missing_recommended,
        "tag_hygiene": hygiene,
        "inherited_usd": round(inherited_cost, 2),
        "inheritance": {
            "enabled": bool(inherited_by_scope),
            "allocated_via_inheritance_usd": round(inherited_cost, 2),
            "resource_count": len(inherited_resources),
        },
        "shared_costs": {
            "total_usd": round(shared_cost, 2),
            "method": shared_method if shared else None,
            "resources": shared_resources[:top_n_resources],
            "distribution": distribution,
            "undistributed_usd": round(undistributed, 2),
        },
        "undistributed_usd": round(undistributed, 2),
    }


def _sum_by_key(costs):
    """{resourceId: usd} -> {normalized id: (display id, usd)}; non-numeric values are ignored."""
    out = {}
    for rid, usd in (costs or {}).items():
        if not isinstance(usd, (int, float)):
            continue
        k = _key(rid)
        display, prev = out.get(k, (rid, 0.0))
        out[k] = (display, prev + usd)
    return out


UNALLOCATED_OWNER = "(unallocated)"


def month_over_month(
    current_costs,
    previous_costs,
    *,
    dimension_tags=None,
    dimension: str = DEFAULT_DIMENSION,
    min_delta_usd: float = 10.0,
    top_n: int = 25,
):
    """Month-over-month cost variance by owner and by resource (pure, offline).

    current_costs / previous_costs  {resourceId: usd} for the two months (case-insensitive ids)
    dimension_tags                  optional {resourceId: {tagKey: tagVal}} for owner lookup
    dimension                       the ownership tag key (default "team"); missing -> "(unallocated)"
    min_delta_usd                   resource movers with |delta| below this are not listed
    top_n                           cap per mover list

    Returns a dict:
      {
        "dimension", "current_usd", "previous_usd", "delta_usd", "delta_pct" (None if previous is 0),
        "by_owner": [{"owner", "current_usd", "previous_usd", "delta_usd", "delta_pct"}...],
        "movers": {"new", "removed", "increased", "decreased": [{"resourceId", "owner",
                   "previous_usd", "current_usd", "delta_usd", "delta_pct"}...]},
        "bridge": {"new_usd", "removed_usd", "increased_usd", "decreased_usd", "below_threshold_usd"},
      }
    Owner and mover lists are sorted by absolute delta (desc). The bridge buckets cover every
    resource, so they sum to delta_usd. Price-vs-usage decomposition is out of scope.
    """
    cur = _sum_by_key(current_costs)
    prev = _sum_by_key(previous_costs)
    tags_by_id = {_key(rid): _tags_ci(t) for rid, t in (dimension_tags or {}).items()}
    dim = str(dimension).strip()

    owner_cur = defaultdict(float)
    owner_prev = defaultdict(float)
    owner_raw = defaultdict(lambda: defaultdict(float))
    movers = {"new": [], "removed": [], "increased": [], "decreased": []}
    bridge = {"new_usd": 0.0, "removed_usd": 0.0, "increased_usd": 0.0,
              "decreased_usd": 0.0, "below_threshold_usd": 0.0}

    for k in sorted(set(cur) | set(prev)):
        display = (cur.get(k) or prev.get(k))[0]
        c = cur[k][1] if k in cur else 0.0
        p = prev[k][1] if k in prev else 0.0
        delta = c - p
        raw = _lookup_tag(tags_by_id.get(k, {}), dim)
        owner = str(raw).strip() if raw is not None else UNALLOCATED_OWNER
        norm = _norm_val(raw) if raw is not None else None  # None = unallocated
        owner_cur[norm] += c
        owner_prev[norm] += p
        owner_raw[norm][owner] += max(c, p)

        if k not in prev:
            bucket = "new"
        elif k not in cur:
            bucket = "removed"
        elif delta > 0:
            bucket = "increased"
        else:
            bucket = "decreased"
        if abs(delta) < min_delta_usd or (bucket in ("increased", "decreased") and delta == 0):
            bridge["below_threshold_usd"] += delta
            continue
        bridge[bucket + "_usd"] += delta
        movers[bucket].append({
            "resourceId": display,
            "owner": owner,
            "previous_usd": round(p, 2),
            "current_usd": round(c, 2),
            "delta_usd": round(delta, 2),
            "delta_pct": round(100.0 * delta / p, 1) if p else None,
        })

    for bucket in movers:
        movers[bucket].sort(key=lambda m: abs(m["delta_usd"]), reverse=True)
        movers[bucket] = movers[bucket][:top_n]

    by_owner = []
    for norm in set(owner_cur) | set(owner_prev):
        c, p = owner_cur[norm], owner_prev[norm]
        display = max(owner_raw[norm].items(), key=lambda kv: kv[1])[0]
        by_owner.append({
            "owner": display,
            "current_usd": round(c, 2),
            "previous_usd": round(p, 2),
            "delta_usd": round(c - p, 2),
            "delta_pct": round(100.0 * (c - p) / p, 1) if p else None,
        })
    by_owner.sort(key=lambda o: (-abs(o["delta_usd"]), o["owner"]))

    cur_total = sum(v for _, v in cur.values())
    prev_total = sum(v for _, v in prev.values())
    return {
        "dimension": dim,
        "current_usd": round(cur_total, 2),
        "previous_usd": round(prev_total, 2),
        "delta_usd": round(cur_total - prev_total, 2),
        "delta_pct": round(100.0 * (cur_total - prev_total) / prev_total, 1) if prev_total else None,
        "by_owner": by_owner,
        "movers": movers,
        "bridge": {k: round(v, 2) for k, v in bridge.items()},
    }


def _pct(part, whole) -> float:
    """part/whole as a rounded percentage; 0.0 when whole is 0."""
    if not whole:
        return 0.0
    return round(100.0 * part / whole, 1)
