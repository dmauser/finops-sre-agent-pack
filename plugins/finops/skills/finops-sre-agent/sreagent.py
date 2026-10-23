"""FinOps for Azure SRE Agent — attribute SRE Agent (AAU) spend per agent, flow, and source.

Pure, offline, deterministic (no Azure calls). The skill gathers four read-only inputs and this
module joins them so an owner can see WHAT each SRE Agent costs, WHY (always-on vs active flow,
and which thread sources drive active flow), and WHERE to look first.

Billing model (https://learn.microsoft.com/azure/sre-agent/pricing-billing): Azure SRE Agent bills
in Azure Agent Units (AAU) on one meter — UsageDetails `consumedService = Microsoft.App`,
`meterName = "SRE Agent Unit"`, `meterSubCategory = "Azure Agent Unit"`, with the agent's ARM id
(`.../providers/Microsoft.App/agents/<name>`) in `instanceName`. Two flows share that meter:

* **Always-on flow** — a fixed 4 AAU per agent-hour (96 AAU/day) from creation until the agent is
  **deleted**. Stopping does not end it. It is waived during the 30-day trial.
* **Active flow** — token-metered AAU for chats, incidents, scheduled tasks, and triggers. It is
  capped per agent by `monthlyAgentUnitLimit`; at the cap chat and actions stop until next month.

UsageDetails has only the blended daily quantity, so the split is derived from the agent's own
read-only ARM API: `GET <agentId>/dailyUsageDetails` returns per-thread active AAU per day with a
`source` (Thread, ScheduledTask, Incident, IncidentAnalysis, Other, Eval, ...). For each billed day
`always_on = billed AAU - active AAU` (floored at 0). When an agent has no dailyUsageDetails (for
example a deleted agent) the split falls back to an estimate (`min(billed, 96)` always-on) and is
labelled `estimated`.

Inputs (all read-only; flexible key casing):

* ``line_items`` — UsageDetails rows (``prepare_usage_details(...)["included_rows"]``). Non-SRE
  rows are ignored. Needs ``cost``, ``quantity``, ``date``, ``resourceId``/``instanceName``, and
  ``consumedService``/``meterName``.
* ``agents`` — optional Resource Graph rows for ``microsoft.app/agents`` (id, name, resourceGroup,
  location, powerState, runningState, monthlyAgentUnitLimit, defaultModel, tags).
* ``daily_usage`` — optional ``{agentId: dailyUsageDetails response or its value list}``.
* ``monthly_usage`` — optional ``{agentId: usages response or its TotalAgentUnits row}`` (MTD
  active AAU ``currentValue`` and ``limit``).
"""

import calendar
import re
from collections import defaultdict
from datetime import date, datetime

ALWAYS_ON_AAU_PER_HOUR = 4
ALWAYS_ON_AAU_PER_DAY = ALWAYS_ON_AAU_PER_HOUR * 24
HOURS_PER_MONTH = 730

_AGENT_SEGMENT = "/providers/microsoft.app/agents/"
_ZERO_THREAD = "00000000-0000-0000-0000-000000000000"

_SOURCE_LABELS = {
    "thread": "Chat / threads",
    "conversation": "Chat / threads",
    "scheduledtask": "Scheduled tasks",
    "incident": "Incidents",
    "incidentanalysis": "Incident analysis (Operations Hub)",
    "trigger": "Triggers",
    "eval": "Evaluations",
    "other": "Other",
}

_TOP_THREADS = 10

# Active AAU in the window under this, on an agent that pays always-on, is "barely used".
_IDLE_ACTIVE_AAU = 50.0
# Each agent's active share under this % makes a multi-agent footprint a consolidation candidate.
_CONSOLIDATE_ACTIVE_PCT = 25.0
# Projected month active AAU at/above this % of monthlyAgentUnitLimit risks a hard stop.
_LIMIT_RISK_PCT = 80.0
# Projected month active AAU under this % of the limit means the limit is not a useful guardrail...
_LIMIT_HEADROOM_PCT = 10.0
# ...but only flag that when the unused headroom is worth at least this many AAU.
_LIMIT_HEADROOM_MIN_AAU = 5000.0
# Source share (of measured active AAU) that makes scheduled tasks / incidents the driver.
_SOURCE_HEAVY_PCT = 40.0
_SOURCE_HEAVY_MIN_AAU = 100.0
# Average always-on AAU/day under this on a measured agent means the always-on charge is waived.
_WAIVED_ALWAYS_ON_PER_DAY = 1.0
# Active-flow USD at/above this on a premium model is worth a model-choice check.
_PREMIUM_MODEL_MIN_USD = 100.0


def _lower_keys(mapping):
    return {str(k).casefold(): v for k, v in mapping.items()} if isinstance(mapping, dict) else {}


def _get(mapping, *names, default=None):
    lowered = _lower_keys(mapping)
    props = lowered.get("properties")
    for source in (lowered, _lower_keys(props) if isinstance(props, dict) else {}):
        for name in names:
            value = source.get(name.casefold())
            if value not in (None, ""):
                return value
    return default


def _num(value):
    if value in (None, "") or isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _day(value):
    if not value:
        return None
    text = str(value).strip()
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if match:
        return f"{match.group(3)}-{int(match.group(1)):02d}-{int(match.group(2)):02d}"
    return None


def _norm_id(value):
    return str(value or "").strip().rstrip("/").casefold()


def _short(resource_id):
    return str(resource_id or "").rstrip("/").rsplit("/", 1)[-1] or "(unknown)"


def _resource_group(resource_id):
    parts = str(resource_id or "").split("/")
    for index, part in enumerate(parts):
        if part.casefold() == "resourcegroups" and index + 1 < len(parts):
            return parts[index + 1]
    return ""


def _round(value, digits=2):
    return round(float(value), digits)


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else None


def is_sre_agent_line(row):
    """True when a UsageDetails row is Azure SRE Agent (AAU) spend."""

    if not isinstance(row, dict):
        return False
    rid = _norm_id(_get(row, "instanceName", "resourceId"))
    if _AGENT_SEGMENT in rid:
        return True
    service = str(_get(row, "consumedService", default="")).casefold()
    meter = str(_get(row, "meterName", default="")).casefold()
    return service == "microsoft.app" and "agent unit" in meter


def _usage_rows(value):
    if isinstance(value, dict):
        value = _get(value, "value", default=[])
    return [row for row in (value or []) if isinstance(row, dict)]


def _monthly_row(value):
    if isinstance(value, dict) and "value" in _lower_keys(value):
        rows = _usage_rows(value)
        for row in rows:
            name = _get(row, "name", default={})
            key = _get(name, "value", default="") if isinstance(name, dict) else str(name or "")
            if str(key).casefold() == "totalagentunits":
                return row
        return rows[0] if rows else {}
    return value if isinstance(value, dict) else {}


def _model_label(agent):
    model = _get(agent, "defaultModel", default=None)
    if isinstance(model, dict):
        name = _get(model, "name", default="")
        provider = _get(model, "provider", default="")
        return " / ".join(str(part) for part in (provider, name) if part)
    if model:
        return str(model)
    return " / ".join(
        str(part) for part in (_get(agent, "modelProvider"), _get(agent, "modelName")) if part
    )


def _agent_status(agent):
    if agent is None:
        return "deleted"
    states = {
        str(_get(agent, "powerState", default="")).casefold(),
        str(_get(agent, "runningState", default="")).casefold(),
    } - {""}
    if not states:
        return "unknown"
    if states <= {"running"}:
        return "running"
    if states & {"stopped", "stopping", "deallocated", "disabled", "suspended"}:
        return "stopped"
    return "/".join(sorted(states))


def _as_of_date(value, fallback):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    day = _day(value) or fallback
    return date.fromisoformat(day) if day else None


def analyze_sre_agent_costs(
    line_items,
    agents=None,
    daily_usage=None,
    monthly_usage=None,
    *,
    as_of=None,
):
    """Join billed AAU, agent inventory, and per-thread active AAU into one rollup."""

    inventory = {}
    for agent in agents or ():
        if isinstance(agent, dict):
            rid = _get(agent, "id", "resourceId")
            if rid:
                inventory[_norm_id(rid)] = agent

    billed = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))  # agent -> day -> [usd, aau]
    agent_ids = {}
    currencies = set()
    for row in line_items or ():
        if not is_sre_agent_line(row):
            continue
        rid = _get(row, "resourceId", "instanceName")
        key = _norm_id(rid)
        day = _day(_get(row, "date", "usageStart"))
        if not key or not day:
            continue
        agent_ids.setdefault(key, rid)
        slot = billed[key][day]
        slot[0] += _num(_get(row, "cost", "costInUSD"))
        slot[1] += _num(_get(row, "quantity"))
        currency = _get(row, "currency")
        if currency:
            currencies.add(str(currency))

    all_billed_days = sorted({day for days in billed.values() for day in days})
    window_start = all_billed_days[0] if all_billed_days else None
    window_end = all_billed_days[-1] if all_billed_days else None
    as_of_day = _as_of_date(as_of, window_end)

    def in_window(day):
        return window_start is None or window_start <= day <= window_end

    # Active AAU: restricted to the billed window so per-source totals reconcile with settled cost.
    active = defaultdict(lambda: defaultdict(float))            # agent -> day -> aau
    threads = defaultdict(lambda: {"aau": 0.0, "days": set()})  # (agent, thread, source)
    measured = set()
    for rid, payload in (daily_usage or {}).items():
        key = _norm_id(rid)
        if not key:
            continue
        agent_ids.setdefault(key, rid)
        measured.add(key)
        for row in _usage_rows(payload):
            day = _day(_get(row, "date"))
            value = _num(_get(row, "value"))
            if not day or value <= 0 or not in_window(day):
                continue
            active[key][day] += value
            source = str(_get(row, "source", default="Other"))
            thread = str(_get(row, "threadId", default="") or "")
            entry = threads[(key, thread, source)]
            entry["aau"] += value
            entry["days"].add(day)

    monthly = {
        _norm_id(rid): _monthly_row(payload) for rid, payload in (monthly_usage or {}).items()
    }

    warnings = []
    if len(currencies) > 1:
        warnings.append("SRE Agent rows span multiple currencies: " + ", ".join(sorted(currencies)))
    currency = next(iter(currencies)) if len(currencies) == 1 else "USD"

    by_agent = []
    daily_totals = defaultdict(lambda: [0.0, 0.0])
    for key in sorted(set(billed) | set(inventory) | measured):
        agent = inventory.get(key)
        rid = agent_ids.get(key) or _get(agent or {}, "id", default=key)
        days = billed.get(key, {})
        usd = sum(v[0] for v in days.values())
        aau = sum(v[1] for v in days.values())
        rate = usd / aau if aau else None
        split = "measured" if key in measured else "estimated"
        always_on_aau = 0.0
        active_aau = 0.0
        for day, (day_usd, day_aau) in days.items():
            daily_totals[day][0] += day_usd
            daily_totals[day][1] += day_aau
            if split == "measured":
                day_active = min(active[key].get(day, 0.0), day_aau)
            else:
                day_active = max(day_aau - ALWAYS_ON_AAU_PER_DAY, 0.0)
            active_aau += day_active
            always_on_aau += max(day_aau - day_active, 0.0)
        days_billed = len(days)

        month = monthly.get(key) or {}
        mtd_raw = _get(month, "currentValue")
        limit_raw = _get(month, "limit") or _get(agent or {}, "monthlyAgentUnitLimit")
        mtd_active = _num(mtd_raw) if mtd_raw is not None else None
        limit = _num(limit_raw) if limit_raw is not None else None
        projected = None
        if mtd_active is not None and as_of_day is not None:
            dim = calendar.monthrange(as_of_day.year, as_of_day.month)[1]
            projected = mtd_active / max(as_of_day.day, 1) * dim
        if agent is None and not inventory:
            status = "unknown"
        else:
            status = _agent_status(agent)
        by_agent.append({
            "resourceId": rid,
            "agentName": str(_get(agent or {}, "name", default=_short(rid))),
            "resourceGroup": str(_get(agent or {}, "resourceGroup", default=_resource_group(rid))),
            "location": str(_get(agent or {}, "location", default="")),
            "status": status,
            "model": _model_label(agent) if agent else "",
            "monthly_usd": _round(usd),
            "billed_aau": _round(aau, 1),
            "always_on_aau": _round(always_on_aau, 1),
            "active_aau": _round(active_aau, 1),
            "always_on_usd": _round(always_on_aau * rate) if rate is not None else None,
            "active_usd": _round(active_aau * rate) if rate is not None else None,
            "active_pct": _pct(active_aau, aau),
            "usd_per_aau": round(rate, 4) if rate is not None else None,
            "split": split,
            "days_billed": days_billed,
            "last_billed": max(days) if days else None,
            "mtd_active_aau": _round(mtd_active, 1) if mtd_active is not None else None,
            "limit_aau": _round(limit, 0) if limit is not None else None,
            "projected_month_active_aau": _round(projected, 1) if projected is not None else None,
            "projected_limit_pct": _pct(projected, limit) if projected is not None and limit else None,
            "always_on_waived": bool(
                split == "measured"
                and days_billed
                and always_on_aau / days_billed < _WAIVED_ALWAYS_ON_PER_DAY
            ),
        })
    by_agent.sort(
        key=lambda r: (-r["monthly_usd"], r["agentName"].casefold(), _norm_id(r["resourceId"]))
    )

    rates = {_norm_id(r["resourceId"]): r["usd_per_aau"] for r in by_agent}
    known_rates = sorted(r for r in rates.values() if r)
    total_usd = sum(r["monthly_usd"] for r in by_agent)
    total_aau = sum(r["billed_aau"] for r in by_agent)
    blended_rate = total_usd / total_aau if total_aau else (known_rates[0] if known_rates else None)

    sources = {}
    for (key, thread, source), entry in threads.items():
        rate = rates.get(key) or blended_rate
        slot = sources.setdefault(
            source.casefold(),
            {"label": _SOURCE_LABELS.get(source.casefold(), source), "aau": 0.0, "usd": 0.0,
             "threads": set()},
        )
        slot["aau"] += entry["aau"]
        slot["usd"] += entry["aau"] * rate if rate else 0.0
        if thread and thread != _ZERO_THREAD:
            slot["threads"].add((key, thread))
    total_active_measured = sum(s["aau"] for s in sources.values())
    by_source = sorted(
        (
            {
                "source": key,
                "label": slot["label"],
                "aau": _round(slot["aau"], 1),
                "usd": _round(slot["usd"]),
                "pct": _pct(slot["aau"], total_active_measured),
                "thread_count": len(slot["threads"]),
            }
            for key, slot in sources.items()
        ),
        key=lambda r: (-r["aau"], r["label"].casefold()),
    )

    names = {_norm_id(r["resourceId"]): r["agentName"] for r in by_agent}
    top_threads = []
    for (key, thread, source), entry in threads.items():
        if not thread or thread == _ZERO_THREAD:
            continue
        rate = rates.get(key) or blended_rate
        top_threads.append({
            "agentName": names.get(key, _short(key)),
            "threadId": thread,
            "source": _SOURCE_LABELS.get(source.casefold(), source),
            "aau": _round(entry["aau"], 1),
            "usd": _round(entry["aau"] * rate) if rate else None,
            "days": len(entry["days"]),
            "last_active": max(entry["days"]),
        })
    top_threads.sort(key=lambda r: (-r["aau"], r["agentName"].casefold(), r["threadId"]))
    top_threads = top_threads[:_TOP_THREADS]

    daily = [
        {"date": day, "usd": _round(v[0]), "aau": _round(v[1], 1)}
        for day, v in sorted(daily_totals.items())
    ]

    always_on_usd = sum(r["always_on_usd"] or 0.0 for r in by_agent)
    active_usd = sum(r["active_usd"] or 0.0 for r in by_agent)
    window_days = (
        (date.fromisoformat(window_end) - date.fromisoformat(window_start)).days + 1
        if window_start and window_end
        else 0
    )

    if any(r["split"] == "estimated" and r["billed_aau"] for r in by_agent):
        warnings.append(
            "Always-on vs active split is estimated (96 AAU/day always-on) for agents without "
            "dailyUsageDetails"
        )

    hints = _hints(by_agent, by_source, inventory_supplied=bool(inventory), as_of_day=as_of_day,
                   blended_rate=blended_rate)

    return {
        "total_usd": _round(total_usd),
        "total_aau": _round(total_aau, 1),
        "always_on_usd": _round(always_on_usd),
        "active_usd": _round(active_usd),
        "always_on_pct": _pct(always_on_usd, always_on_usd + active_usd),
        "currency": currency,
        "agent_count": sum(1 for r in by_agent if r["billed_aau"]),
        "inventory_count": len(inventory),
        "window_start": window_start,
        "window_end": window_end,
        "window_days": window_days,
        "as_of": as_of_day.isoformat() if as_of_day else None,
        "usd_per_aau": round(blended_rate, 4) if blended_rate else None,
        "always_on_monthly_usd_per_agent": (
            _round(ALWAYS_ON_AAU_PER_HOUR * HOURS_PER_MONTH * blended_rate) if blended_rate else None
        ),
        "by_agent": by_agent,
        "by_source": by_source,
        "top_threads": top_threads,
        "daily": daily,
        "hints": hints,
        "warnings": warnings,
    }


def _hints(by_agent, by_source, *, inventory_supplied, as_of_day, blended_rate):
    hints = []
    always_on_month_aau = ALWAYS_ON_AAU_PER_HOUR * HOURS_PER_MONTH

    def monthly_always_on(row):
        rate = row["usd_per_aau"] or blended_rate
        return _round(always_on_month_aau * rate) if rate else None

    for row in by_agent:
        name = row["agentName"]
        if inventory_supplied and row["status"] == "deleted" and row["billed_aau"]:
            recent = bool(
                as_of_day is not None
                and row["last_billed"]
                and (as_of_day - date.fromisoformat(row["last_billed"])).days <= 2
            )
            hints.append({
                "type": "deleted_agent_billed",
                "target": name,
                "detail": (
                    f"Not in the agent inventory but billed {row['billed_aau']} AAU in the window "
                    f"(last billed {row['last_billed']}). "
                    + (
                        "Charges are recent; confirm the agent is really deleted and not in a "
                        "subscription outside the managed scope."
                        if recent
                        else "Expected tail after deletion; confirm no new charges appear."
                    )
                ),
                "monthly_usd": row["monthly_usd"],
            })
        if row["status"] == "stopped" and row["always_on_aau"]:
            hints.append({
                "type": "stopped_agent_billed",
                "target": name,
                "detail": (
                    "Agent is stopped but still pays always-on flow (4 AAU/hour until deletion). "
                    "Delete it if it is no longer needed; stopping does not end billing."
                ),
                "monthly_usd": monthly_always_on(row),
            })
        elif (
            row["status"] in ("running", "unknown")
            and row["split"] == "measured"
            and row["always_on_aau"]
            and row["active_aau"] < _IDLE_ACTIVE_AAU
        ):
            hints.append({
                "type": "idle_agent",
                "target": name,
                "detail": (
                    f"Only {row['active_aau']} active AAU over {row['days_billed']} billed days while "
                    "always-on flow is charged. Consolidate its scope onto another agent or delete it."
                ),
                "monthly_usd": monthly_always_on(row),
            })
        if row["always_on_waived"]:
            hints.append({
                "type": "always_on_waived_verify",
                "target": name,
                "detail": (
                    "No always-on charge is billed yet (likely the 30-day trial waiver). Budget for "
                    "4 AAU/hour per agent once it ends."
                ),
                "monthly_usd": monthly_always_on(row),
            })
        pct = row["projected_limit_pct"]
        if pct is not None and pct >= _LIMIT_RISK_PCT:
            hints.append({
                "type": "limit_risk",
                "target": name,
                "detail": (
                    f"Projected month active flow {row['projected_month_active_aau']} AAU is {pct}% "
                    f"of the {row['limit_aau']} AAU monthly limit. At the limit chat and actions "
                    "stop until next month; raise the limit or reduce scheduled/incident load."
                ),
                "monthly_usd": None,
            })
        elif (
            pct is not None
            and pct < _LIMIT_HEADROOM_PCT
            and row["limit_aau"]
            and row["limit_aau"] - (row["projected_month_active_aau"] or 0.0)
            >= _LIMIT_HEADROOM_MIN_AAU
        ):
            hints.append({
                "type": "limit_headroom_review",
                "target": name,
                "detail": (
                    f"Monthly limit {row['limit_aau']} AAU is far above projected use "
                    f"({row['projected_month_active_aau']} AAU, {pct}%). The limit is the spend "
                    "circuit breaker; consider ~1.5-2x expected use."
                ),
                "monthly_usd": None,
            })
        model = (row["model"] or "").casefold()
        if "opus" in model and (row["active_usd"] or 0.0) >= _PREMIUM_MODEL_MIN_USD:
            hints.append({
                "type": "model_choice_verify",
                "target": name,
                "detail": (
                    f"Default model {row['model']} is a premium tier. Test routine tasks on a "
                    "lower-cost model before switching; per-token AAU rates differ several-fold."
                ),
                "monthly_usd": row["active_usd"],
            })

    paying = [r for r in by_agent if r["always_on_aau"] and r["status"] != "deleted"]
    low_use = sorted(
        (r for r in paying if (r["active_pct"] or 0.0) < _CONSOLIDATE_ACTIVE_PCT),
        key=lambda r: (-(r["active_aau"] or 0.0), r["agentName"].casefold()),
    )
    if len(paying) >= 2 and len(low_use) >= 2:
        saving = sum(monthly_always_on(r) or 0.0 for r in low_use[1:])
        hints.append({
            "type": "consolidation_candidate",
            "target": f"{len(low_use)} agents",
            "detail": (
                "Mostly always-on flow with little active use: "
                + ", ".join(sorted(r["agentName"] for r in low_use))
                + ". One agent can manage multiple scopes; each removed agent saves its "
                "always-on flow."
            ),
            "monthly_usd": _round(saving) if saving else None,
        })

    for row in by_source:
        if row["aau"] < _SOURCE_HEAVY_MIN_AAU or (row["pct"] or 0.0) < _SOURCE_HEAVY_PCT:
            continue
        if row["source"] == "scheduledtask":
            hints.append({
                "type": "scheduled_task_heavy",
                "target": row["label"],
                "detail": (
                    f"Scheduled tasks drive {row['pct']}% of active AAU. Review cadence, merge "
                    "overlapping tasks, reuse one data pull per run, and test prompts in chat first."
                ),
                "monthly_usd": row["usd"],
            })
        elif row["source"] in ("incident", "incidentanalysis"):
            hints.append({
                "type": "incident_heavy",
                "target": row["label"],
                "detail": (
                    f"Incident handling drives {row['pct']}% of active AAU. Use incident response "
                    "plans and filters so only actionable incidents start an investigation."
                ),
                "monthly_usd": row["usd"],
            })

    hints.sort(
        key=lambda h: (h["monthly_usd"] is None, -(h["monthly_usd"] or 0.0), h["type"], h["target"])
    )
    return hints
