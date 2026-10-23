"""FinOps cost-optimization-report — pure, offline, deterministic (no Azure calls).

Rolls the existing read-only FinOps analyses up into ONE executive summary:

  * cost anomalies        (finops-cost-anomaly-detection -> detect_anomalies -> list)
  * rightsizing savings   (finops-rightsizing-advisor    -> recommend_rightsizing -> list)
  * cost allocation       (finops-cost-allocation        -> allocate_costs -> dict)
  * budget status         (finops-budget-governance      -> evaluate_budgets -> dict)
  * waste cleanup         (finops-waste-finder           -> find_waste -> dict, optional)
  * rate optimization     (finops-commitment-advisor     -> analyze_commitments -> dict, optional)
  * SRE Agent spend       (finops-sre-agent              -> analyze_sre_agent_costs -> dict, optional)

Usage optimization (rightsizing + waste, de-duplicated by resource id) and rate optimization
(commitments) are reported separately and never summed: buying commitments for capacity that
is about to be removed double-counts the same dollars.

This module does no data pulling of its own — the skill runs each existing core (each of
which pulls and shapes its own data), then hands their *already-computed* outputs here.
That keeps every analysis independently tested and keeps this a pure aggregation layer.

"Policy findings" here are governance signals derived from the existing outputs — not a new
data source: tag-hygiene + untagged/unallocated spend (from allocation) and budget gates
(from budget governance). No write, no new RBAC.

Output (see summarize_optimization): an executive `headline`, a blended, dollar-ranked
`priorities` list (each item labelled with an `impact_type` so savings, overruns, spikes and
governance exposure are never silently conflated), and per-section detail for the dashboard.
"""

from datetime import date

# How many rows each detail section and the blended priorities list keep.
_TOP_PRIORITIES = 10
_TOP_PER_SECTION = 8


def _num(value):
    """Return value as float, or None if it isn't a real number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _short_id(resource_id):
    """Last path segment of a resource id, for compact titles."""
    if not resource_id:
        return "(unknown)"
    return str(resource_id).rstrip("/").split("/")[-1]


def _rightsizing_rollup(findings):
    """Total quantified savings, count, and the top findings from recommend_rightsizing()."""
    findings = findings or []
    total = 0.0
    for f in findings:
        savings = _num(f.get("estMonthlySavingsUsd"))
        if savings:
            total += savings
    top = findings[:_TOP_PER_SECTION]  # already sorted by savings desc by the core
    return {
        "potential_monthly_savings": round(total, 2),
        "count": len(findings),
        "top": top,
    }


def _anomaly_rollup(anomalies):
    """Count, largest impact, and the top anomalies from detect_anomalies()."""
    anomalies = anomalies or []
    top_impact = _num(anomalies[0].get("impact_usd")) if anomalies else None
    return {
        "count": len(anomalies),
        "top_impact_usd": round(top_impact, 2) if top_impact is not None else None,
        "top": anomalies[:_TOP_PER_SECTION],  # already sorted by impact desc by the core
    }


def _budget_overrun_usd(b):
    """Dollar overage for a gated budget: spend-over-amount, else forecast-over-amount."""
    amount = _num(b.get("amount"))
    if amount is None:
        return None
    if b.get("status") == "over_budget":
        current = _num(b.get("current_spend"))
        return round(current - amount, 2) if current is not None else None
    if b.get("status") == "forecast_over":
        forecast = _num(b.get("forecast_spend"))
        return round(forecast - amount, 2) if forecast is not None else None
    return None


def _budget_rollup(budgets_result):
    """Split evaluated budgets into over / forecast_over / at_risk buckets for the report."""
    result = budgets_result or {}
    evaluated = result.get("budgets") or []
    buckets = {"over": [], "forecast_over": [], "at_risk": []}
    for b in evaluated:
        status = b.get("status")
        if status == "over_budget":
            buckets["over"].append(b)
        elif status == "forecast_over":
            buckets["forecast_over"].append(b)
        elif status == "at_risk":
            buckets["at_risk"].append(b)
    return {
        "no_budgets": bool(result.get("no_budgets")),
        "summary": result.get("summary") or {},
        "over": buckets["over"],
        "forecast_over": buckets["forecast_over"],
        "at_risk": buckets["at_risk"],
    }


def _governance_rollup(allocation, budget_roll):
    """Governance/policy findings from existing signals: tagging + budget gates."""
    alloc = allocation or {}
    gates = list(budget_roll["over"]) + list(budget_roll["forecast_over"])
    return {
        "dimension": alloc.get("dimension"),
        "untagged_usd": _num(alloc.get("untagged_usd")) or 0.0,
        "unallocated_usd": _num(alloc.get("unallocated_usd")) or 0.0,
        "unallocated_pct": alloc.get("unallocated_pct"),
        "untagged_top": (alloc.get("untagged_resources") or [])[:_TOP_PER_SECTION],
        "tag_hygiene": (alloc.get("tag_hygiene") or [])[:_TOP_PER_SECTION],
        "budget_gates": [
            {"name": b.get("name"), "status": b.get("status"),
             "overrun_usd": _budget_overrun_usd(b), "currency": b.get("currency")}
            for b in gates
        ],
    }


_WASTE_ACTIONABLE = ("orphan", "idle", "stale")


def _waste_rollup(waste, rightsizing_findings):
    """Waste findings not already reported by rightsizing (joined case-insensitively)."""
    waste = waste or {}
    taken = {str(f.get("resourceId") or "").rstrip("/").casefold()
             for f in rightsizing_findings or []}
    all_findings = waste.get("findings") or []
    findings = [f for f in all_findings
                if str(f.get("resourceId") or "").rstrip("/").casefold() not in taken]
    total = 0.0
    by_kind = {}
    for f in findings:
        by_kind[f.get("kind")] = by_kind.get(f.get("kind"), 0) + 1
        savings = _num(f.get("estMonthlySavings"))
        if f.get("kind") in _WASTE_ACTIONABLE and savings:
            total += savings
    return {
        "potential_monthly_savings": round(total, 2),
        "count": len(findings),
        "deduplicated": len(all_findings) - len(findings),
        "by_kind": dict(sorted((str(k), v) for k, v in by_kind.items())),
        "currency": waste.get("currency"),
        "top": findings[:_TOP_PER_SECTION],  # already sorted by savings desc by the core
    }


def _commitment_rollup(commitments):
    """Rate-optimization headline and the top rows of each commitment list."""
    c = commitments or {}
    headline = c.get("headline") or {}
    return {
        "potential_monthly_savings": _num(headline.get("potential_monthly_savings")),
        "best_family": headline.get("best_family"),
        "currency": c.get("currency"),
        "coverage_pct": _num(headline.get("coverage_pct")),
        "unused_commitment_cost": _num(headline.get("unused_commitment_cost")),
        "commitment_count": headline.get("commitment_count") or 0,
        "inventory_unavailable": list(headline.get("inventory_unavailable") or []),
        "purchases": (c.get("purchases") or [])[:_TOP_PER_SECTION],
        "underutilized": (c.get("underutilized") or [])[:_TOP_PER_SECTION],
        "expiring": (c.get("expiring") or [])[:_TOP_PER_SECTION],
        "hybrid_benefit": (c.get("hybrid_benefit") or [])[:_TOP_PER_SECTION],
        "warnings": list(c.get("warnings") or []),
    }


_FAMILY_LABEL = {"reservation": "reservation", "savings_plan": "savings plan"}


def _waste_priorities(waste_roll):
    items = []
    for f in waste_roll["top"]:
        if f.get("kind") not in _WASTE_ACTIONABLE:
            continue
        items.append({
            "category": "waste",
            "impact_type": "savings",
            "impact_usd": _num(f.get("estMonthlySavings")),
            "title": f"{f.get('reason') or 'Waste'} — {_short_id(f.get('resourceId'))}",
            "detail": f.get("kind"),
            "action": f.get("recommendedAction"),
            "validated": None,
        })
    return items


def _rate_priorities(rate_roll):
    items = []
    best = rate_roll["best_family"]
    for p in rate_roll["purchases"]:
        if p.get("family") != best:
            continue  # alternatives cover the same usage; rank only the headline family
        family = _FAMILY_LABEL.get(p.get("family"), p.get("family"))
        items.append({
            "category": "rate",
            "impact_type": "savings",
            "impact_usd": _num(p.get("est_monthly_savings")),
            "title": f"Buy {p.get('term')} {family} — {p.get('sku')}",
            "detail": f"{p.get('lookback') or 'look-back'} usage; "
                      f"{len(p.get('alternatives') or [])} alternative term(s)",
            "action": "Purchase after usage optimization; confirm the usage is steady",
            "validated": None,
        })
    unused = rate_roll["unused_commitment_cost"]
    if unused:
        items.append({
            "category": "rate",
            "impact_type": "unused commitment",
            "impact_usd": round(unused, 2),
            "title": "Unused reservation / savings plan charges",
            "detail": f"{len(rate_roll['underutilized'])} underutilized commitment(s)",
            "action": "Exchange, re-scope (Shared/management group), or move eligible usage",
            "validated": None,
        })
    for e in rate_roll["expiring"]:
        items.append({
            "category": "rate",
            "impact_type": "risk",
            "impact_usd": None,
            "title": f"Commitment expires in {e.get('days_to_expiry')} days — {e.get('name')}",
            "detail": "auto-renew off",
            "action": "Decide renewal before expiry to avoid pay-as-you-go rates",
            "validated": None,
        })
    return items


_SRE_AGENT_HINTS = {
    # hint type: (impact_type, title, action)
    "idle_agent": ("savings", "Idle SRE Agent",
                   "Consolidate its scope onto another agent or delete it"),
    "stopped_agent_billed": ("savings", "Stopped SRE Agent still billed",
                             "Delete it if no longer needed; stopping does not end billing"),
    "consolidation_candidate": ("savings", "Consolidate SRE Agents",
                                "Move scopes onto one agent and delete the rest"),
    "deleted_agent_billed": ("risk", "Deleted SRE Agent still billed",
                             "Confirm the agent is deleted and charges stop"),
    "limit_risk": ("risk", "SRE Agent near its monthly AAU limit",
                   "Raise the limit or reduce scheduled/incident load"),
    "always_on_waived_verify": ("upcoming cost", "SRE Agent trial waiver ends",
                                "Budget for always-on flow once the trial ends"),
}
_SRE_PER_AGENT_SAVINGS = ("idle_agent", "stopped_agent_billed")


def _sre_agent_rollup(sre):
    """Azure SRE Agent (AAU) spend from analyze_sre_agent_costs(), with de-duplicated savings.

    Per-agent savings (idle / stopped-but-billed) and the consolidation hint describe the same
    always-on dollars, so only the larger of the two is counted and ranked.
    """
    s = sre or {}
    hints = [h for h in (s.get("hints") or []) if isinstance(h, dict)]
    per_agent = [h for h in hints if h.get("type") in _SRE_PER_AGENT_SAVINGS]
    consolidation = [h for h in hints if h.get("type") == "consolidation_candidate"]
    per_agent_total = sum(_num(h.get("monthly_usd")) or 0.0 for h in per_agent)
    consolidation_total = sum(_num(h.get("monthly_usd")) or 0.0 for h in consolidation)
    if consolidation_total > per_agent_total:
        savings_hints, savings = consolidation, consolidation_total
    else:
        savings_hints, savings = per_agent, per_agent_total
    ranked = savings_hints + [
        h for h in hints
        if _SRE_AGENT_HINTS.get(h.get("type"), ("",))[0] in ("risk", "upcoming cost")
    ]
    return {
        "total_usd": round(_num(s.get("total_usd")) or 0.0, 2),
        "always_on_usd": round(_num(s.get("always_on_usd")) or 0.0, 2),
        "active_usd": round(_num(s.get("active_usd")) or 0.0, 2),
        "always_on_pct": _num(s.get("always_on_pct")),
        "agent_count": s.get("agent_count") or 0,
        "currency": s.get("currency"),
        "window_start": s.get("window_start"),
        "window_end": s.get("window_end"),
        "potential_monthly_savings": round(savings, 2),
        "by_agent": (s.get("by_agent") or [])[:_TOP_PER_SECTION],
        "hints": hints[:_TOP_PER_SECTION],
        "ranked_hints": ranked,
        "warnings": list(s.get("warnings") or []),
    }


def _sre_agent_priorities(sre_roll):
    items = []
    for h in sre_roll["ranked_hints"]:
        impact_type, title, action = _SRE_AGENT_HINTS[h.get("type")]
        items.append({
            "category": "sre_agent",
            "impact_type": impact_type,
            "impact_usd": _num(h.get("monthly_usd")),
            "title": f"{title} — {h.get('target')}",
            "detail": h.get("detail"),
            "action": action,
            "validated": None,
        })
    return items


def _priorities(rightsizing, anomalies, budget_roll, governance, extra=()):
    """One blended, dollar-ranked action list. Each item is labelled with an impact_type so
    savings, overruns, spikes, and governance exposure are comparable but never conflated."""
    items = list(extra)

    for f in rightsizing["top"]:
        items.append({
            "category": "rightsizing",
            "impact_type": "savings",
            "impact_usd": _num(f.get("estMonthlySavingsUsd")),
            "title": f"{f.get('recommendedAction') or 'Rightsize'} — {_short_id(f.get('resourceId'))}",
            "detail": f.get("kind"),
            "action": f.get("recommendedAction"),
            "validated": f.get("validated"),
        })

    for b in budget_roll["over"] + budget_roll["forecast_over"]:
        items.append({
            "category": "budget",
            "impact_type": "overrun",
            "impact_usd": _budget_overrun_usd(b),
            "title": f"Budget {b.get('name')} — {b.get('status')}",
            "detail": f"{b.get('current_spend')}/{b.get('amount')} {b.get('currency')} "
                      f"(forecast {b.get('forecast_spend')})",
            "action": "Review and adjust the budget or the spend driving it",
            "validated": None,
        })

    for a in anomalies["top"]:
        kind = str(a.get("kind") or "spike")
        direction = str(a.get("direction") or "").strip().lower()
        is_drop = direction == "down" or (not direction and kind.lower() == "drop")
        items.append({
            "category": "anomaly",
            "impact_type": "drop" if is_drop else "spike",
            "impact_usd": _num(a.get("impact_usd")),
            "title": f"{kind}: {a.get('dimension')}={a.get('value')}",
            "detail": f"current {a.get('current_usd')} vs baseline {a.get('baseline_mean_usd')}",
            "action": ("Confirm the drop is expected (decommission, outage, or data lag)"
                       if is_drop else "Investigate the spike and correlate to a change"),
            "validated": None,
        })

    if governance["untagged_usd"] > 0:
        items.append({
            "category": "governance",
            "impact_type": "governance",
            "impact_usd": round(governance["untagged_usd"], 2),
            "title": f"Untagged spend — {len(governance['untagged_top'])}+ resources",
            "detail": (f"no tags at all; separately {governance['unallocated_pct']}% of spend "
                       f"lacks the '{governance.get('dimension') or 'owner'}' tag")
                      if governance["unallocated_pct"] is not None else "no tags at all",
            "action": "Tag resources so spend is attributable",
            "validated": None,
        })
    for h in governance["tag_hygiene"]:
        items.append({
            "category": "governance",
            "impact_type": "governance",
            "impact_usd": _num(h.get("cost_affected")),
            "title": f"Tag hygiene — '{h.get('canonical')}' split across {len(h.get('variants') or {})} spellings",
            "detail": "spend divided across inconsistent tag values",
            "action": "Consolidate the tag values",
            "validated": None,
        })

    # Rank by dollar impact; unknown-impact items sort last but are retained.
    items.sort(key=lambda i: (i["impact_usd"] is not None, i["impact_usd"] or 0.0), reverse=True)
    for rank, item in enumerate(items[:_TOP_PRIORITIES], start=1):
        item["rank"] = rank
    return items[:_TOP_PRIORITIES]


def summarize_optimization(*, anomalies=None, rightsizing=None, allocation=None,
                           budgets=None, waste=None, commitments=None, sre_agents=None,
                           as_of=None, spend_period=None):
    """Roll the FinOps analyses up into one executive optimization summary.

    anomalies    the list returned by detect_anomalies()          (or None)
    rightsizing  the list returned by recommend_rightsizing()     (or None)
    allocation   the dict returned by allocate_costs()            (or None)
    budgets      the dict returned by evaluate_budgets()          (or None)
    waste        the dict returned by find_waste()                (or None)
    commitments  the dict returned by analyze_commitments()       (or None)
    sre_agents   the dict returned by analyze_sre_agent_costs()   (or None)
    as_of        date the report is anchored to (defaults to today)
    spend_period optional (first_date, last_date) or {"from", "to"} of the usage rows behind
                 total_monthly_spend; emitted as headline.spend_period so the report states it

    Every input is optional; a missing analysis becomes an empty section rather than an error.
    `waste` and `commitments` add their own sections and headline keys only when provided:
    headline.waste_monthly_savings, usage_optimization_monthly_savings (rightsizing + waste),
    rate_optimization_monthly_savings, commitment_coverage_pct, unused_commitment_cost.
    `sre_agents` adds an `sre_agents` section and headline.sre_agent_spend_usd,
    sre_agent_always_on_pct, sre_agent_monthly_savings (kept separate from usage optimization).
    headline.potential_monthly_savings stays rightsizing-only for backward compatibility.

    Returns a dict:
      {
        "as_of",
        "headline": {total_monthly_spend, potential_monthly_savings, anomaly_count,
                     top_anomaly_impact_usd, budgets_over, budgets_forecast_over,
                     budgets_at_risk, untagged_usd, unallocated_pct},
        "priorities": [ {rank, category, impact_type, impact_usd, title, detail, action, validated} ],
        "rightsizing": {potential_monthly_savings, count, top:[...]},
        "anomalies": {count, top_impact_usd, top:[...]},
        "budgets": {no_budgets, summary, over:[...], forecast_over:[...], at_risk:[...]},
        "governance": {untagged_usd, unallocated_usd, unallocated_pct, untagged_top:[...],
                       tag_hygiene:[...], budget_gates:[...]},
      }
    """
    today = as_of or date.today()
    alloc = allocation or {}

    rightsizing_roll = _rightsizing_rollup(rightsizing)
    anomaly_roll = _anomaly_rollup(anomalies)
    budget_roll = _budget_rollup(budgets)
    governance_roll = _governance_rollup(alloc, budget_roll)
    waste_roll = _waste_rollup(waste, rightsizing) if waste is not None else None
    rate_roll = _commitment_rollup(commitments) if commitments is not None else None
    sre_roll = _sre_agent_rollup(sre_agents) if sre_agents is not None else None
    extra = []
    if waste_roll is not None:
        extra += _waste_priorities(waste_roll)
    if rate_roll is not None:
        extra += _rate_priorities(rate_roll)
    if sre_roll is not None:
        extra += _sre_agent_priorities(sre_roll)
        del sre_roll["ranked_hints"]
    priorities = _priorities(rightsizing_roll, anomaly_roll, budget_roll, governance_roll, extra)

    summary = budget_roll["summary"]
    headline = {
        "total_monthly_spend": _num(alloc.get("total_usd")),
        "potential_monthly_savings": rightsizing_roll["potential_monthly_savings"],
        "anomaly_count": anomaly_roll["count"],
        "top_anomaly_impact_usd": anomaly_roll["top_impact_usd"],
        "budgets_over": summary.get("over_budget", 0),
        "budgets_forecast_over": summary.get("forecast_over", 0),
        "budgets_at_risk": summary.get("at_risk", 0),
        "untagged_usd": round(governance_roll["untagged_usd"], 2),
        "unallocated_pct": governance_roll["unallocated_pct"],
    }
    if spend_period:
        start, end = ((spend_period.get("from"), spend_period.get("to"))
                      if isinstance(spend_period, dict) else tuple(spend_period))
        headline["spend_period"] = {"from": str(start)[:10], "to": str(end)[:10]}
    if waste_roll is not None:
        headline["waste_monthly_savings"] = waste_roll["potential_monthly_savings"]
        headline["usage_optimization_monthly_savings"] = round(
            rightsizing_roll["potential_monthly_savings"]
            + waste_roll["potential_monthly_savings"], 2)
    if rate_roll is not None:
        headline["rate_optimization_monthly_savings"] = rate_roll["potential_monthly_savings"]
        headline["commitment_coverage_pct"] = rate_roll["coverage_pct"]
        headline["unused_commitment_cost"] = rate_roll["unused_commitment_cost"]
    if sre_roll is not None:
        headline["sre_agent_spend_usd"] = sre_roll["total_usd"]
        headline["sre_agent_always_on_pct"] = sre_roll["always_on_pct"]
        headline["sre_agent_monthly_savings"] = sre_roll["potential_monthly_savings"]

    out = {
        "as_of": today.isoformat(),
        "headline": headline,
        "priorities": priorities,
        "rightsizing": rightsizing_roll,
        "anomalies": anomaly_roll,
        "budgets": budget_roll,
        "governance": governance_roll,
    }
    if waste_roll is not None:
        out["waste"] = waste_roll
    if rate_roll is not None:
        out["commitments"] = rate_roll
    if sre_roll is not None:
        out["sre_agents"] = sre_roll
    return out
