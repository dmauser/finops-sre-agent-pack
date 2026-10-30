"""FinOps commitment (rate optimization) advisor — pure, offline, deterministic.

Takes raw, read-only Azure API responses and returns one normalized rate-optimization view:

  * inventory      existing reservations (Microsoft.Capacity) and savings plans
                   (Microsoft.BillingBenefits) with 1/7/30-day utilization and expiry
  * underutilized  commitments whose utilization is below the threshold (wasted spend)
  * expiring       commitments expiring within the window with auto-renew off (coverage cliff)
  * purchases      reservation (Consumption reservationRecommendations) and savings plan
                   (Cost Management benefitRecommendations) purchase recommendations,
                   normalized to an estimated MONTHLY savings figure
  * coverage       commitment coverage from amortized UsageDetails (pricingModel) plus the
                   unused reservation / savings plan charges
  * hybrid_benefit Azure Hybrid Benefit candidates (Windows VMs / SQL VMs paying for licenses)

Savings normalization (both verified against the API reference samples):
  * reservationRecommendations.netSavings is projected over the full reservation TERM
    (P1Y and P3Y rows for identical usage differ by exactly the term ratio), so
    monthly = netSavings / term_months.
  * benefitRecommendations.recommendationDetails.savingsAmount covers the LOOK-BACK window
    (`totalHours`), so monthly = savingsAmount / totalHours * 730.

Reservation and savings plan recommendations cover the same eligible usage. They are
ALTERNATIVES, never additive: the headline uses the larger of the two families, and within
a family only the best term per SKU/scope is counted.
"""

from datetime import date, datetime

HOURS_PER_MONTH = 730.0
_TERM_MONTHS = {"P1M": 1, "P1Y": 12, "P3Y": 36, "P5Y": 60}
_LOOKBACK_DAYS = {"last7days": 7, "last30days": 30, "last60days": 60}
_COMMITMENT_PRICING = {"reservation", "savingsplan"}
_UNUSED_CHARGE_TYPES = {"unusedreservation", "unusedsavingsplan"}
_WINDOWS_LICENSED = {"windows_server", "windows_client"}
_INVENTORY_ROLES = {
    "reservation": "Reservations Reader (/providers/Microsoft.Capacity)",
    "savings_plan": "Savings plan Reader (/providers/Microsoft.BillingBenefits)",
}


def _lower_keys(mapping):
    return {str(k).casefold(): v for k, v in (mapping or {}).items()}


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, dict):
        return _num(value.get("value"))
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _currency(value, default=None):
    if isinstance(value, dict) and value.get("currency"):
        return str(value["currency"]).upper()
    return default


def _text(value):
    return str(value or "").strip()


def _props(item):
    props = item.get("properties") if isinstance(item, dict) else None
    return props if isinstance(props, dict) else {}


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


def _norm_id(value):
    return _text(value).rstrip("/").casefold()


def _in_boundary(scope_ids, managed_scopes):
    """True when every id is inside the managed boundary (or no boundary was supplied)."""
    if managed_scopes is None:
        return True
    managed = [_norm_id(s) for s in managed_scopes if _text(s)]
    for scope in scope_ids:
        norm = _norm_id(scope)
        if not any(norm == m or norm.startswith(m + "/") for m in managed):
            return False
    return True


def _utilization(props):
    aggregates = ((props.get("utilization") or {}).get("aggregates")) or []
    result = {}
    for agg in aggregates:
        grain = _num(agg.get("grain"))
        value = _num(agg.get("value"))
        if grain is not None and value is not None:
            result[int(grain)] = round(value, 2)
    return result


def _commitment(item, kind, as_of, managed_scopes):
    props = _props(item)
    applied_type = _text(props.get("userFriendlyAppliedScopeType") or props.get("appliedScopeType"))
    scope_props = props.get("appliedScopeProperties") or {}
    applied_ids = [s for s in (props.get("appliedScopes") or []) if _text(s)]
    for key in ("resourceGroupId", "subscriptionId", "managementGroupId"):
        if _text(scope_props.get(key)):
            applied_ids = [scope_props[key]]
            break
    shared = applied_type.casefold() in {"shared", "managementgroup"} or not applied_ids
    if not shared and not _in_boundary(applied_ids, managed_scopes):
        return None
    expiry = _parse_date(props.get("expiryDateTime") or props.get("expiryDate"))
    util = _utilization(props)
    commitment = props.get("commitment") or {}
    hourly = None
    if _text(commitment.get("grain")).casefold() == "hourly":
        hourly = _num(commitment.get("amount"))
    return {
        "id": _text(item.get("id")),
        "kind": kind,
        "name": _text(props.get("displayName") or item.get("name")),
        "sku": _text((item.get("sku") or {}).get("name")),
        "resource_type": _text(props.get("reservedResourceType"))
        or ("SavingsPlan" if kind == "savings_plan" else ""),
        "term": _text(props.get("term")),
        "quantity": _num(props.get("quantity")),
        "hourly_commitment": hourly,
        "commitment_currency": _text(commitment.get("currencyCode")).upper() or None,
        "applied_scope_type": applied_type or ("Shared" if shared else "Single"),
        "applied_scopes": applied_ids,
        "shared_scope": shared,
        "state": _text(props.get("displayProvisioningState") or props.get("provisioningState")),
        "renew": bool(props.get("renew")),
        "expiry_date": expiry.isoformat() if expiry else None,
        "days_to_expiry": (expiry - as_of).days if expiry else None,
        "utilization_1d": util.get(1),
        "utilization_7d": util.get(7),
        "utilization_30d": util.get(30),
    }


def _active(c):
    return c["state"].casefold() not in {"cancelled", "canceled", "expired", "failed"}


def _ri_recommendation(item, default_currency):
    props = _props(item)
    term = _text(props.get("term")).upper()
    months = _TERM_MONTHS.get(term)
    net = _num(props.get("netSavings"))
    if months is None or net is None:
        return None
    on_demand = _num(props.get("costWithNoReservedInstances"))
    with_ri = _num(props.get("totalCostWithReservedInstances"))
    return {
        "family": "reservation",
        "resource_type": _text(props.get("resourceType")),
        "sku": _text(props.get("skuName") or props.get("normalizedSize")),
        "location": _text(item.get("location") or props.get("location")),
        "scope": _text(props.get("scope")),
        "subscription_id": _text(props.get("subscriptionId")),
        "term": term,
        "quantity": _num(props.get("recommendedQuantity")),
        "lookback": _text(props.get("lookBackPeriod")),
        "est_monthly_savings": round(net / months, 2),
        "savings_pct": round(100.0 * net / on_demand, 1) if on_demand else None,
        "est_monthly_cost_with_commitment": round(with_ri / months, 2) if with_ri else None,
        "hourly_commitment": None,
        "coverage_pct": None,
        "currency": _currency(props.get("netSavings"), default_currency),
    }


def _sp_recommendation(item, default_currency):
    props = _props(item)
    details = props.get("recommendationDetails") or {}
    savings = _num(details.get("savingsAmount"))
    hours = _num(props.get("totalHours"))
    if not hours:
        days = _LOOKBACK_DAYS.get(_text(props.get("lookBackPeriod")).casefold())
        hours = days * 24.0 if days else None
    if savings is None or not hours:
        return None
    total = _num(details.get("totalCost"))
    without = _num(props.get("costWithoutBenefit"))
    return {
        "family": "savings_plan",
        "resource_type": _text(props.get("armSkuName")) or "Compute_Savings_Plan",
        "sku": _text(props.get("armSkuName") or "Compute_Savings_Plan"),
        "location": "",
        "scope": _text(props.get("scope")),
        "subscription_id": _text(props.get("subscriptionId")),
        "term": _text(props.get("term")).upper(),
        "quantity": None,
        "lookback": _text(props.get("lookBackPeriod")),
        "est_monthly_savings": round(savings / hours * HOURS_PER_MONTH, 2),
        "savings_pct": round(100.0 * savings / without, 1) if without else None,
        "est_monthly_cost_with_commitment": round(total / hours * HOURS_PER_MONTH, 2)
        if total else None,
        "hourly_commitment": _num(details.get("commitmentAmount")),
        "coverage_pct": _num(details.get("coveragePercentage")),
        "currency": _text(props.get("currencyCode")).upper() or default_currency,
    }


def _best_per_group(recs):
    """Keep the best term per SKU/scope; the other terms are alternatives, not extra savings."""
    groups = {}
    for rec in recs:
        key = (rec["family"], rec["resource_type"].casefold(), rec["sku"].casefold(),
               rec["location"].casefold(), rec["scope"].casefold(),
               rec["subscription_id"].casefold(), rec["currency"])
        groups.setdefault(key, []).append(rec)
    result = []
    for members in groups.values():
        members.sort(key=lambda r: (-r["est_monthly_savings"], r["term"]))
        best = dict(members[0])
        best["alternatives"] = [
            {"term": r["term"], "est_monthly_savings": r["est_monthly_savings"]}
            for r in members[1:]
        ]
        result.append(best)
    return result


def _coverage(rows):
    covered = on_demand = unused = 0.0
    by_category = {}
    for row in rows:
        r = _lower_keys(row)
        cost = _num(r.get("cost") if r.get("cost") is not None else r.get("costinbillingcurrency"))
        if cost is None:
            continue
        charge = _text(r.get("chargetype")).casefold()
        if charge in _UNUSED_CHARGE_TYPES:
            unused += cost
            continue
        if charge and charge != "usage":
            continue
        pricing = _text(r.get("pricingmodel")).casefold().replace(" ", "")
        category = _text(r.get("metercategory")) or "(unknown)"
        bucket = by_category.setdefault(category, {"covered": 0.0, "on_demand": 0.0})
        if pricing in _COMMITMENT_PRICING:
            covered += cost
            bucket["covered"] += cost
        elif pricing in {"ondemand", ""}:
            on_demand += cost
            bucket["on_demand"] += cost
    total = covered + on_demand
    categories = []
    for name, b in by_category.items():
        subtotal = b["covered"] + b["on_demand"]
        if subtotal <= 0:
            continue
        categories.append({
            "meter_category": name,
            "covered_cost": round(b["covered"], 2),
            "on_demand_cost": round(b["on_demand"], 2),
            "coverage_pct": round(100.0 * b["covered"] / subtotal, 1),
        })
    categories.sort(key=lambda c: (-c["on_demand_cost"], c["meter_category"].casefold()))
    return {
        "covered_cost": round(covered, 2),
        "on_demand_cost": round(on_demand, 2),
        "coverage_pct": round(100.0 * covered / total, 1) if total > 0 else None,
        "unused_commitment_cost": round(unused, 2),
        "by_meter_category": categories[:10],
        "row_count": len(rows),
    }


def _hybrid_benefit(vms, sql_vms, costs, managed_scopes):
    costs = {_norm_id(k): v for k, v in (costs or {}).items()}
    candidates = []
    for vm in vms or []:
        rid = _text(vm.get("id") or vm.get("resourceId"))
        if not rid or not _in_boundary([rid], managed_scopes):
            continue
        props = _props(vm)
        os_disk = (props.get("storageProfile") or {}).get("osDisk") or {}
        os_type = _text(os_disk.get("osType") or vm.get("osType"))
        license_type = _text(props.get("licenseType") or vm.get("licenseType"))
        if os_type.casefold() == "windows" and license_type.casefold() not in _WINDOWS_LICENSED:
            candidates.append({
                "resourceId": rid,
                "license": "Windows Server",
                "current_license_type": license_type or "(none - pay-as-you-go Windows license)",
                "monthly_cost": _num(costs.get(_norm_id(rid))),
                "action": "Enable Azure Hybrid Benefit (licenseType=Windows_Server) if eligible "
                          "Windows Server licenses with Software Assurance are available",
            })
    for sql in sql_vms or []:
        rid = _text(sql.get("id") or sql.get("resourceId"))
        if not rid or not _in_boundary([rid], managed_scopes):
            continue
        props = _props(sql)
        license_type = _text(props.get("sqlServerLicenseType") or sql.get("sqlServerLicenseType"))
        edition = _text(props.get("sqlImageSku") or sql.get("sqlImageSku"))
        if license_type.casefold() == "payg" and edition.casefold() not in {"developer", "express"}:
            candidates.append({
                "resourceId": rid,
                "license": f"SQL Server {edition}".strip(),
                "current_license_type": license_type,
                "monthly_cost": _num(costs.get(_norm_id(rid))),
                "action": "Switch sqlServerLicenseType to AHUB if eligible SQL Server licenses "
                          "with Software Assurance are available",
            })
    candidates.sort(key=lambda c: (c["monthly_cost"] is None, -(c["monthly_cost"] or 0.0),
                                   c["resourceId"].casefold()))
    return candidates


def analyze_commitments(*, reservations=None, savings_plans=None,
                        reservation_recommendations=None, benefit_recommendations=None,
                        usage_rows=None, vms=None, sql_vms=None, costs=None,
                        managed_scopes=None, currency=None, as_of=None,
                        low_utilization_pct=80.0, expiry_window_days=60,
                        min_monthly_savings=10.0, inventory_unavailable=None):
    """Build the rate-optimization view from raw, read-only API responses.

    reservations                 `value` items from GET /providers/Microsoft.Capacity/reservations
    savings_plans                `value` items from GET /providers/Microsoft.BillingBenefits/savingsPlans
    reservation_recommendations  `value` items from GET .../Microsoft.Consumption/reservationRecommendations
    benefit_recommendations      `value` items from GET .../Microsoft.CostManagement/benefitRecommendations
    usage_rows                   prepare_usage_details(...)["included_rows"] from an AmortizedCost
                                 pull projected with pricingModel, meterCategory and chargeType
    vms / sql_vms                Resource Graph rows (id + properties) for Hybrid Benefit candidates
    costs                        {resourceId: monthly cost} to rank Hybrid Benefit candidates
    managed_scopes               effective ARM scopes; Single/RG-scoped commitments outside are dropped
    currency                     billing currency for amounts that do not carry one
    inventory_unavailable        kinds whose inventory GET failed (403/error), e.g.
                                 ["reservation", "savings_plan"]; reported as a coverage gap,
                                 never as "no commitments"
    """

    today = as_of or date.today()
    warnings = []
    unavailable = sorted({str(k) for k in (inventory_unavailable or [])
                          if str(k) in _INVENTORY_ROLES})
    if unavailable:
        roles = " / ".join(_INVENTORY_ROLES[k] for k in unavailable)
        warnings.append(
            f"Commitment inventory unavailable ({', '.join(k.replace('_', ' ') for k in unavailable)})"
            f" - grant {roles}; utilization and expiry were not evaluated for those commitments. "
            "Coverage and unused-commitment cost below come from amortized cost and remain valid."
        )

    inventory = []
    excluded = 0
    for kind, items in (("reservation", reservations), ("savings_plan", savings_plans)):
        for item in items or []:
            c = _commitment(item, kind, today, managed_scopes)
            if c is None:
                excluded += 1
            elif _active(c):
                inventory.append(c)
    if excluded:
        warnings.append(f"{excluded} single-scope commitment(s) outside the managed boundary "
                        "were excluded")
    if any(c["shared_scope"] for c in inventory):
        warnings.append("Shared/management-group commitments benefit usage beyond the managed "
                        "boundary; their utilization is reported for context, not attribution")
    inventory.sort(key=lambda c: (c["kind"], c["name"].casefold(), c["id"].casefold()))

    def _util(c):
        return c["utilization_30d"] if c["utilization_30d"] is not None else c["utilization_7d"]

    underutilized = []
    for c in inventory:
        util = _util(c)
        if util is None or util >= low_utilization_pct:
            continue
        action = ("Exchange, widen the applied scope (Shared/management group), or reduce "
                  "future purchases" if c["kind"] == "reservation" else
                  "Widen the applied scope or move eligible compute under the plan; do not "
                  "buy more until utilization recovers")
        underutilized.append(dict(c, utilization=util, action=action))
    underutilized.sort(key=lambda c: (c["utilization"], c["name"].casefold()))

    expiring = [
        dict(c, action="Decide renewal now: renew or replace to avoid falling back to "
                       "pay-as-you-go rates, or let it lapse deliberately")
        for c in inventory
        if c["days_to_expiry"] is not None and 0 <= c["days_to_expiry"] <= expiry_window_days
        and not c["renew"]
    ]
    expiring.sort(key=lambda c: (c["days_to_expiry"], c["name"].casefold()))

    raw_recs = []
    dropped = 0
    shared_recs = 0
    for parser, items in ((_ri_recommendation, reservation_recommendations),
                          (_sp_recommendation, benefit_recommendations)):
        for item in items or []:
            rec = parser(item, currency)
            if rec is None:
                dropped += 1
            elif managed_scopes is not None and rec["scope"].casefold() == "shared":
                shared_recs += 1
            else:
                raw_recs.append(rec)
    if dropped:
        warnings.append(f"{dropped} recommendation(s) lacked term or savings data and were skipped")
    if shared_recs:
        warnings.append(f"{shared_recs} Shared-scope recommendation(s) are sized on billing-account "
                        "usage beyond the managed boundary and were excluded; request "
                        "properties/scope eq 'Single' for scoped savings")
    purchases = [r for r in _best_per_group(raw_recs)
                 if r["est_monthly_savings"] >= min_monthly_savings]
    purchases.sort(key=lambda r: (-r["est_monthly_savings"], r["family"], r["sku"].casefold(),
                                  r["term"]))

    totals = {}
    for rec in purchases:
        fam = totals.setdefault(rec["currency"] or "UNKNOWN",
                                {"reservation": 0.0, "savings_plan": 0.0})
        fam[rec["family"]] += rec["est_monthly_savings"]
    currencies = sorted(totals)
    if len(currencies) > 1:
        warnings.append("Recommendations span multiple currencies; totals are per currency and "
                        "no single headline is computed")
    best_total = None
    best_family = None
    if len(currencies) == 1:
        fam = totals[currencies[0]]
        best_family = "reservation" if fam["reservation"] >= fam["savings_plan"] else "savings_plan"
        best_total = round(fam[best_family], 2)
    if {r["family"] for r in purchases} == {"reservation", "savings_plan"}:
        warnings.append("Reservation and savings plan recommendations cover the same usage; they "
                        "are alternatives, so the headline uses the larger family, not the sum")

    coverage = _coverage(usage_rows) if usage_rows is not None else None
    hub = _hybrid_benefit(vms, sql_vms, costs, managed_scopes)
    if hub:
        warnings.append("Azure Hybrid Benefit requires eligible licenses with Software Assurance "
                        "or qualifying subscriptions; verify entitlement before enabling")

    if currencies and len(currencies) == 1:
        out_currency = currencies[0]
    else:
        out_currency = currency
    return {
        "as_of": today.isoformat(),
        "currency": out_currency,
        "headline": {
            "commitment_count": len(inventory),
            "inventory_unavailable": unavailable,
            "underutilized_count": len(underutilized),
            "expiring_count": len(expiring),
            "purchase_recommendation_count": len(purchases),
            "potential_monthly_savings": best_total,
            "best_family": best_family,
            "coverage_pct": coverage["coverage_pct"] if coverage else None,
            "unused_commitment_cost": coverage["unused_commitment_cost"] if coverage else None,
            "hybrid_benefit_candidates": len(hub),
        },
        "totals_by_currency": {k: {f: round(v, 2) for f, v in fam.items()}
                               for k, fam in sorted(totals.items())},
        "inventory": inventory,
        "underutilized": underutilized,
        "expiring": expiring,
        "purchases": purchases,
        "coverage": coverage,
        "hybrid_benefit": hub,
        "warnings": warnings,
    }
