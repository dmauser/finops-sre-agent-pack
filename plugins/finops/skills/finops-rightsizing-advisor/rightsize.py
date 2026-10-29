"""Rightsizing / idle-resource recommender for the FinOps SRE Agent pack
(skill: finops-rightsizing-advisor).

Pure, dependency-free logic so it is unit-testable offline. The agent gathers the
raw inputs via read-only `az` (Advisor cost recs, Resource Graph inventory, Azure
Monitor utilization, Consumption UsageDetails cost) and runs `recommend_rightsizing(...)`
with the runtime's sandbox Python tool. No Azure calls happen here.

The point of this module is to VALIDATE Advisor's suggestions and inventory heuristics
against real utilization and real cost before surfacing them, then rank everything by
estimated monthly savings. It never trusts a claim it cannot back with evidence:
Advisor recs with no utilization data are surfaced as `validated=None` ("unvalidated"),
and recs contradicted by high utilization are surfaced as `validated=False`.

Input shapes (all keyed/looked-up by resourceId, matched case-insensitively):

    resources    inventory rows from Resource Graph, one dict per resource:
        {
            "resourceId": "/subscriptions/.../virtualMachines/vm1",
            "type": "microsoft.compute/virtualmachines",   # lower-cased ARM type
            "sku": "Standard_D4s_v5",                        # optional
            "powerState": "stopped",                          # vm: running|stopped|deallocated
            "diskState": "Unattached",                        # disk: Attached|Unattached
            "numberOfSites": 0,                                # serverfarm: hosted site count
            "environmentId": "/subscriptions/.../managedEnvironments/env1",  # containerapp: parent ACA env
            "minReplicas": 1,                                  # containerapp: min replica count
            "readySessionInstances": 10,                       # sessionpool: pre-warmed (billed) sessions
            "tags": {"env": "prod"},                           # optional
        }

    utilization  {resourceId: {"cpu_p95": float, "cpu_avg": float,
                               "mem_p95": float|None, "sample_days": int,
                               "sample_coverage_pct": float}}
                 (percentages 0-100; from `az monitor metrics list`)
                 Coverage is the minimum completeness of the required metric series.
                 Alternatively supply sample_count and expected_sample_count.
                 CPU-only data is a candidate, never sufficient to validate an action.

    activity     {resourceId: {"requests_total": float, "sample_days": int}}
                 invocation traffic per resource. For an Azure Container App this is the
                 Azure Monitor `Requests` metric; for a dynamic session pool it is the
                 `SessionApiRequestCount` metric. Zero HTTP requests does not establish that
                 background workloads are unused. VM/App idle validation also requires a
                 workload_activity_total covering background work, plus CPU/memory and
                 activity sample coverage; session pools use their invocation metric.

    costs        {resourceId: monthly cost} (aggregated from UsageDetails).
                 Supply currency explicitly or through costs.currency. Unknown currency
                 remains unknown; USD-named output aliases are null unless currency is USD.

    advisor      list of Advisor cost recommendations:
        {
            "resourceId": "/subscriptions/.../virtualMachines/vm1",
            "problem": "Right-size or shut down underutilized virtual machines",
            "recommendation": "Resize to Standard_D2s_v5",   # optional free text
            "targetSku": "Standard_D2s_v5",                   # optional
            "savingsUsd": 42.10,                               # optional monthly USD
        }
"""

from __future__ import annotations

# Utilization thresholds (percent of capacity, evaluated on p95 CPU).
DEFAULT_CPU_IDLE_PCT = 5.0        # p95 CPU below this => effectively idle
DEFAULT_CPU_UNDERUTIL_PCT = 20.0  # p95 CPU below this => oversized (rightsize down)
DEFAULT_MIN_SAMPLE_DAYS = 7       # need at least this many days of metrics to trust util
DEFAULT_MIN_SAMPLE_COVERAGE_PCT = 90.0
DEFAULT_MEMORY_UNDERUTIL_PCT = 40.0
DEFAULT_MEMORY_IDLE_PCT = 20.0
DEFAULT_MIN_MONTHLY_SAVINGS_USD = 5.0
# Cost-led coverage sweep: any resource costing at least this much per month whose type has
# no idle rule (and that no other signal flagged) is surfaced as a "review" item so expensive
# resources are never silently dropped just because we lack a heuristic for their type.
DEFAULT_REVIEW_MIN_MONTHLY_USD = 20.0
# When we must estimate savings for a rightsize-down with no Advisor number, assume
# dropping one tier saves ~half. Conservative and clearly labelled as an estimate.
DEFAULT_RIGHTSIZE_SAVINGS_FRACTION = 0.5

_VM_TYPE = "microsoft.compute/virtualmachines"
_DISK_TYPE = "microsoft.compute/disks"
_PLAN_TYPE = "microsoft.web/serverfarms"
_ACA_ENV_TYPE = "microsoft.app/managedenvironments"
_ACA_APP_TYPE = "microsoft.app/containerapps"
_ACA_POOL_TYPE = "microsoft.app/sessionpools"


def _key(resource_id) -> str:
    """Case-insensitive join key (ARM resource ids are case-insensitive)."""
    return str(resource_id or "").strip().rstrip("/").casefold()


def _lookup(mapping: dict, resource_id):
    return mapping.get(_key(resource_id))


def _index_by_id(mapping) -> dict:
    """Normalize a {resourceId: value} dict to lower-cased keys."""
    return {_key(rid): val for rid, val in (mapping or {}).items()}


def _cost_index(mapping):
    """Cost rows are additive; metric dictionaries deliberately are not."""
    out = {}
    for rid, value in (mapping or {}).items():
        amount = _as_float(value)
        if amount is not None and _key(rid):
            out[_key(rid)] = out.get(_key(rid), 0.0) + amount
    return out


def _currency(value):
    return str(value or "").strip().upper() or None


class _Finding:
    """Mutable accumulator so multiple signals for one resource merge into one row."""

    def __init__(self, resource_id, resource_type, currency=None):
        self.resource_id = resource_id
        self.resource_type = resource_type
        self.currency = currency
        self.kind = None
        self.current_sku = None
        self.recommended_action = None
        self.current_monthly_usd = None
        self.est_monthly_savings_usd = None
        self.validated = None
        self.evidence = []
        self.sources = []

    def add_source(self, source):
        if source not in self.sources:
            self.sources.append(source)

    def to_dict(self) -> dict:
        return {
            "resourceId": self.resource_id,
            "resourceType": self.resource_type,
            "kind": self.kind,
            "currentSku": self.current_sku,
            "recommendedAction": self.recommended_action,
            "currency": self.currency,
            "currentMonthlyCost": _round(self.current_monthly_usd),
            "estMonthlySavings": _round(self.est_monthly_savings_usd),
            "currentMonthlyUsd": _round(self.current_monthly_usd) if self.currency == "USD" else None,
            "estMonthlySavingsUsd": _round(self.est_monthly_savings_usd) if self.currency == "USD" else None,
            "validated": self.validated,
            "validationStatus": ("supported" if self.validated is True else
                                 "contradicted" if self.validated is False else "candidate"),
            "evidence": list(self.evidence),
            "sources": list(self.sources),
        }


def _round(value):
    return round(value, 2) if isinstance(value, (int, float)) else value


def recommend_rightsizing(
    resources=None,
    utilization=None,
    costs=None,
    advisor=None,
    activity=None,
    *,
    cpu_idle_pct: float = DEFAULT_CPU_IDLE_PCT,
    cpu_underutil_pct: float = DEFAULT_CPU_UNDERUTIL_PCT,
    min_sample_days: int = DEFAULT_MIN_SAMPLE_DAYS,
    min_monthly_savings_usd: float = DEFAULT_MIN_MONTHLY_SAVINGS_USD,
    rightsize_savings_fraction: float = DEFAULT_RIGHTSIZE_SAVINGS_FRACTION,
    review_min_monthly_usd: float = DEFAULT_REVIEW_MIN_MONTHLY_USD,
    existing_resource_ids=None,
    currency=None,
):
    """Merge Advisor recs, inventory heuristics, utilization, and cost into a single
    ranked list of read-only rightsizing / idle-cleanup recommendations.

    `activity` includes requests_total, sample_days and sample_coverage_pct (or sample_count
    and expected_sample_count). For VMs/Container Apps, workload_activity_total must cover
    non-HTTP/background activity before an idle action can be supported. HTTP-only findings
    remain candidates. An empty environment additionally needs childrenInventoryComplete=True
    on its inventory row (including jobs and session pools) before deletion is supported.

    `currency` defaults to costs.currency, otherwise unknown. Generic monetary fields retain
    the original unit; USD aliases are populated only for USD. Conflicting explicit/metadata
    currencies raise ValueError; no exchange conversion occurs. Legacy *_usd threshold
    argument names are retained, but their values are in the selected input currency.

    `existing_resource_ids` is an optional iterable of every resource id that still exists in
    scope (a `Resources | project id` Resource Graph pull). When given, the cost-led review
    sweep skips ids that no longer exist: their cost is history, not a live opportunity.

    Returns a list of finding dicts sorted by estimated monthly savings (descending);
    findings whose savings can be quantified below `min_monthly_savings_usd` are dropped,
    while findings with unknown savings are kept (they cannot be ruled out) and sort last.
    """
    resources = resources or []
    util = _index_by_id(utilization)
    cost = _cost_index(costs)
    source_currency = _currency(getattr(costs, "currency", None))
    currency = _currency(currency)
    if currency and source_currency and currency != source_currency:
        raise ValueError("currency conflicts with costs.currency; no currency conversion is performed")
    currency = currency or source_currency
    activity = _index_by_id(activity)
    advisor = advisor or []
    existing = (
        None if existing_resource_ids is None
        else {_key(rid) for rid in existing_resource_ids if rid}
    )

    findings: dict = {}

    def finding_for(resource_id, resource_type=None):
        resource_type = resource_type or _type_from_id(resource_id)
        key = _key(resource_id)
        if key not in findings:
            findings[key] = _Finding(resource_id, resource_type, currency)
        elif resource_type and not findings[key].resource_type:
            findings[key].resource_type = resource_type
        return findings[key]

    # ---- 1. Inventory heuristics (no utilization required) -------------------
    for res in resources:
        rid = res.get("resourceId")
        if not rid:
            continue
        rtype = str(res.get("type") or "").lower()
        idle = _inventory_idle(res, rtype)
        if idle is None:
            continue
        kind, action, evidence = idle
        monthly = _lookup(cost, rid)
        f = finding_for(rid, rtype)
        f.kind = kind
        f.current_sku = res.get("sku")
        f.recommended_action = action
        f.current_monthly_usd = monthly
        f.est_monthly_savings_usd = monthly  # idle => the whole line item is recoverable
        f.validated = True                    # inventory state is a fact, not a guess
        f.evidence.append(evidence)
        f.add_source("resource-graph")

    # ---- 1b. Azure Container Apps idle detection ----------------------------
    # Unused ACA is pure waste: empty/no-traffic Container Apps environments, always-on
    # container apps (minReplicas>=1) with no requests, and dynamic session pools holding
    # warm readySessionInstances with no session activity (these often top the bill because
    # `az resource list` hides the sessionpools type, so they escape casual review).
    env_children: dict = {}
    for res in resources:
        if str(res.get("type") or "").lower() in {
            _ACA_APP_TYPE, _ACA_POOL_TYPE, "microsoft.app/jobs"
        }:
            env_id = res.get("environmentId")
            if env_id:
                env_children.setdefault(_key(env_id), []).append(res)

    for res in resources:
        rid = res.get("resourceId")
        if not rid or _key(rid) in findings:
            continue
        rtype = str(res.get("type") or "").lower()
        if rtype == _ACA_ENV_TYPE:
            idle = _aca_env_idle(res, env_children.get(_key(rid), []), activity, util,
                                 min_sample_days, cpu_idle_pct)
        elif rtype == _ACA_APP_TYPE:
            idle = _aca_app_idle(res, activity, util, min_sample_days, cpu_idle_pct)
        elif rtype == _ACA_POOL_TYPE:
            idle = _aca_sessionpool_idle(res, activity, min_sample_days)
        else:
            continue
        if idle is None:
            continue
        action, evidence, from_activity, validated = idle
        monthly = _lookup(cost, rid)
        f = finding_for(rid, rtype)
        f.kind = "idle"
        f.current_sku = res.get("sku")
        f.recommended_action = action
        f.current_monthly_usd = monthly
        f.est_monthly_savings_usd = monthly  # idle => the whole line item is recoverable
        f.validated = validated
        f.evidence.append(evidence)
        f.add_source("resource-graph")
        if from_activity:
            f.add_source("azure-monitor")

    # ---- 2. Utilization-driven idle / oversized -----------------------------
    for res in resources:
        rid = res.get("resourceId")
        if not rid or _key(rid) in findings:
            continue  # already flagged idle by inventory
        rtype = str(res.get("type") or "").lower()
        if rtype != _VM_TYPE:
            continue  # only VM compute is rightsized from CPU metrics here
        metrics = _lookup(util, rid)
        verdict = _utilization_verdict(metrics, cpu_idle_pct, cpu_underutil_pct, min_sample_days)
        if verdict is None:
            continue
        kind, evidence = verdict
        monthly = _lookup(cost, rid)
        f = finding_for(rid, rtype)
        f.kind = kind
        f.current_sku = res.get("sku")
        f.current_monthly_usd = monthly
        f.validated, validation_note = _validate_utilization(
            metrics, kind, _lookup(activity, rid), cpu_idle_pct, cpu_underutil_pct,
            min_sample_days)
        f.evidence.append(evidence)
        f.evidence.append(validation_note)
        f.add_source("azure-monitor")
        if kind == "idle":
            f.recommended_action = "Deallocate or delete (idle)."
            f.est_monthly_savings_usd = monthly
        else:  # oversized
            f.recommended_action = "Rightsize down one tier."
            f.est_monthly_savings_usd = (
                monthly * rightsize_savings_fraction if monthly is not None else None
            )

    # ---- 3. Advisor cost recs (validated against utilization) ---------------
    types_by_id = {_key(r.get("resourceId")): str(r.get("type") or "").lower() or None for r in resources}
    for rec in advisor:
        rid = rec.get("resourceId")
        if not rid or _is_rate_recommendation(rec):
            continue
        f = finding_for(rid, types_by_id.get(_key(rid)))
        f.add_source("advisor")
        if f.kind is None:
            f.kind = "advisor"
        if f.current_monthly_usd is None:
            f.current_monthly_usd = _lookup(cost, rid)

        target = rec.get("targetSku")
        if f.recommended_action is None:
            if target:
                f.recommended_action = f"Resize to {target}."
            else:
                f.recommended_action = rec.get("recommendation") or rec.get("problem") or "See Advisor cost recommendation."

        f.evidence.append("Azure Advisor: " + str(rec.get("problem") or rec.get("recommendation") or "cost recommendation"))

        metrics = _lookup(util, rid)
        action_text = str(f.recommended_action or "").casefold()
        idle_action = f.kind == "idle" or any(
            word in action_text for word in ("deallocat", "delete", "shutdown", "shut down", "stop", "retir"))
        validation = _validate_utilization(
            metrics, "idle" if idle_action else "oversized", _lookup(activity, rid),
            cpu_idle_pct, cpu_underutil_pct, min_sample_days)
        if validation is not None:
            validated, note = validation
            if validated is False or (f.validated is None and f.kind != "idle"):
                f.validated = validated
            f.evidence.append(note)

        advisor_usd = _as_float(rec.get("savingsUsd"))
        if advisor_usd is not None and f.currency is None and f.current_monthly_usd is None:
            f.currency = "USD"  # the input field explicitly identifies this amount as USD
        if advisor_usd is not None and f.currency != "USD":
            f.evidence.append("Advisor savingsUsd excluded: input costs are not confirmed USD; no conversion performed.")
            advisor_usd = None
        estimated = _advisor_savings_estimate(
            advisor_usd, f.current_monthly_usd, rightsize_savings_fraction
        )
        if estimated is not None and (
            f.est_monthly_savings_usd is None or estimated > f.est_monthly_savings_usd
        ):
            f.est_monthly_savings_usd = estimated

    # ---- 4. Cost-led coverage sweep -----------------------------------------
    # Guarantee no expensive resource is silently dropped just because we lack an idle rule
    # for its type. Any costly resource that no signal above evaluated is surfaced as a
    # "review" item (no savings claimed) so the report's coverage is complete by spend.
    _COVERED_TYPES = {
        _VM_TYPE, _DISK_TYPE, _PLAN_TYPE,
        _ACA_ENV_TYPE, _ACA_APP_TYPE, _ACA_POOL_TYPE,
    }
    original_ids = {_key(rid): rid for rid in (costs or {})}
    for key, monthly in cost.items():
        rid = original_ids[key]
        if _key(rid) in findings:
            continue  # already evaluated/flagged by a rule or Advisor
        if existing is not None and _key(rid) not in existing:
            continue  # deleted since it billed: nothing left to rightsize
        if not isinstance(monthly, (int, float)) or monthly < review_min_monthly_usd:
            continue
        rtype = _type_from_id(rid)
        if rtype in _COVERED_TYPES:
            continue  # a rule covers this type and chose not to flag it => evaluated, fine
        f = finding_for(rid, rtype)
        f.kind = "review"
        f.current_monthly_usd = monthly
        f.est_monthly_savings_usd = None  # unknown — we make no savings claim
        f.validated = None
        f.recommended_action = "High monthly spend with no automated idle rule for this resource type — review manually."
        f.evidence.append(
            f"{monthly:.2f} {currency or '(currency unknown)'}/mo; type '{rtype or 'unknown'}' "
            "has no idle heuristic yet and Advisor did not flag it."
        )
        f.add_source("cost-coverage")

    for f in findings.values():
        if f.kind == "idle" and f.resource_type in {_VM_TYPE, _ACA_APP_TYPE, _ACA_POOL_TYPE}:
            validated, note = _validate_utilization(
                _lookup(util, f.resource_id), "idle", _lookup(activity, f.resource_id),
                cpu_idle_pct, cpu_underutil_pct, min_sample_days)
            if validated is False:
                f.validated = False
                if note not in f.evidence:
                    f.evidence.append(note)
        if f.validated is False:
            f.recommended_action = "Do not act on this recommendation: contradictory utilization; review the workload."
        elif f.validated is None and f.kind != "review":
            f.recommended_action = "Review candidate; validate workload activity, memory and sample coverage before acting. " + (
                f.recommended_action or "")
    return _rank(findings.values(), min_monthly_savings_usd)


def _type_from_id(resource_id):
    """Parse the lower-cased ARM type (`namespace/type`) from a resource id, or None."""
    parts = str(resource_id or "").lower().split("/providers/")
    if len(parts) < 2:
        return None
    seg = parts[-1].split("/")
    return f"{seg[0]}/{seg[1]}" if len(seg) >= 2 else None


def _inventory_idle(res: dict, rtype: str):
    """Return (kind, action, evidence) for inventory-only idle patterns, else None."""
    if rtype == _DISK_TYPE and str(res.get("diskState") or "").lower() == "unattached":
        return ("idle", "Delete the unattached managed disk.", "Managed disk is Unattached.")

    if rtype == _VM_TYPE:
        power = str(res.get("powerState") or "").lower()
        # "stopped" (not "deallocated") still bills for compute — deallocate to stop it.
        if power == "stopped" or power.endswith("/stopped"):
            return (
                "idle",
                "Deallocate the VM (Stopped state still incurs compute charges).",
                "VM is Stopped but not Deallocated.",
            )

    if rtype == _PLAN_TYPE and _as_float(res.get("numberOfSites")) == 0:
        return (
            "idle",
            "Delete or scale down the empty App Service plan.",
            "App Service plan hosts 0 sites.",
        )

    return None


def _aca_activity_is_idle(act, min_sample_days):
    """True when the Requests signal has enough coverage AND shows zero traffic,
    False when it shows traffic, None when coverage is insufficient to judge."""
    if not act or _as_int(act.get("sample_days")) < min_sample_days:
        return None
    requests_total = _as_float(act.get("requests_total"))
    if requests_total is None or requests_total < 0:
        return None
    return requests_total == 0


def _aca_app_idle(res, activity, utilization, min_sample_days, cpu_idle_pct):
    """Return (action, evidence, from_activity, validated) for an idle Container App, else None.

    Needs the Requests signal (from Azure Monitor) to claim "no traffic" — without it the
    app is left unvalidated rather than guessed. An always-on app (minReplicas>=1) with no
    traffic bills continuously and is the strongest signal.
    """
    is_idle = _aca_activity_is_idle(_lookup(activity, res.get("resourceId")), min_sample_days)
    if not is_idle:  # None (no coverage) or False (has traffic) => don't flag
        return None
    days = _as_int(_lookup(activity, res.get("resourceId")).get("sample_days"))
    min_replicas = _as_int(res.get("minReplicas"))
    validated, note = _validate_utilization(
        _lookup(utilization, res.get("resourceId")), "idle",
        _lookup(activity, res.get("resourceId")), cpu_idle_pct,
        DEFAULT_CPU_UNDERUTIL_PCT, min_sample_days)
    if min_replicas >= 1:
        return (
            "Consider deleting the Container App or setting minReplicas=0 after owner review (always-on).",
            f"0 HTTP requests over {days}d and minReplicas={min_replicas}. "
            f"HTTP requests exclude background work. {note}",
            True,
            validated,
        )
    return (
        "Consider retiring the Container App after confirming that no workload depends on it.",
        f"0 HTTP requests over {days}d; HTTP requests exclude background work. {note}",
        True,
        validated,
    )


def _aca_env_idle(res, children, activity, utilization, min_sample_days, cpu_idle_pct):
    """Return (action, evidence, from_activity, validated) for an idle Container Apps environment.

    Apps alone do not exhaust environment dependencies: jobs and session pools also matter.
    No app rows is only an empty-inventory candidate unless collection was complete.
    """
    if not children:
        return (
            "Delete the empty Container Apps environment.",
            "No environment children in supplied inventory; complete app/job/session-pool inventory "
            "and owner review are required.",
            False,
            True if res.get("childrenInventoryComplete") is True else None,
        )
    validations = []
    for app in children:
        if str(app.get("type") or "").lower() != _ACA_APP_TYPE:
            return None  # job/session-pool use cannot be inferred from app HTTP requests
        if _aca_activity_is_idle(_lookup(activity, app.get("resourceId")), min_sample_days) is not True:
            return None  # some app has traffic, or we can't confirm it doesn't
        validation, _ = _validate_utilization(
            _lookup(utilization, app.get("resourceId")), "idle",
            _lookup(activity, app.get("resourceId")), cpu_idle_pct,
            DEFAULT_CPU_UNDERUTIL_PCT, min_sample_days)
        validations.append(validation)
    validated = (False if False in validations else
                 True if all(v is True for v in validations) and
                 res.get("childrenInventoryComplete") is True else None)
    return (
        "Consider retiring the Container Apps environment after reviewing all apps, jobs and session pools.",
        f"All {len(children)} supplied app(s) had 0 HTTP requests; this does not exclude background work. "
        "Complete child inventory, CPU/memory, workload activity and sample coverage are required.",
        True,
        validated,
    )


def _aca_sessionpool_idle(res, activity, min_sample_days):
    """Return (action, evidence, from_activity, validated) for an idle dynamic session pool, else None.

    A dynamic session pool keeps `readySessionInstances` pre-warmed containers that bill
    continuously — real cost only exists when that count is >= 1. `activity["requests_total"]`
    carries the pool's SessionApiRequestCount (session invocations): zero over the window means
    nobody used the pool, so the whole warm pool is recoverable. When the pool has ready
    instances but the usage metric is unavailable, it is surfaced as UNVALIDATED (verify usage)
    rather than dropped — these pools are frequently the largest untracked line items.
    """
    ready = _as_int(res.get("readySessionInstances"))
    if ready < 1:
        return None  # no warm instances => no idle cost to reclaim
    is_idle = _aca_activity_is_idle(_lookup(activity, res.get("resourceId")), min_sample_days)
    if is_idle is False:
        return None  # the pool is in use
    action = f"Set readySessionInstances=0 or delete the session pool ({ready} warm sessions billing while idle)."
    if is_idle is True:
        act = _lookup(activity, res.get("resourceId"))
        days = _as_int(act.get("sample_days"))
        covered = _has_coverage(act, min_sample_days)
        return (action, f"0 session requests over {days}d with {ready} ready sessions. "
                + ("Sample coverage confirmed." if covered else "Sample coverage unconfirmed; validate before acting."),
                True, True if covered else None)
    return (action, f"{ready} ready sessions configured; no usage metric available — verify before reducing.", False, None)


def _utilization_verdict(metrics, cpu_idle_pct, cpu_underutil_pct, min_sample_days):
    """Classify a VM from its utilization metrics, or None if insufficient/healthy."""
    if not metrics or _as_int(metrics.get("sample_days")) < min_sample_days:
        return None
    cpu_p95 = _as_float(metrics.get("cpu_p95"))
    if cpu_p95 is None:
        return None
    sample_days = _as_int(metrics.get("sample_days"))
    if cpu_p95 < cpu_idle_pct:
        return ("idle", f"p95 CPU {cpu_p95:.1f}% over {sample_days}d (idle).")
    if cpu_p95 < cpu_underutil_pct:
        return ("oversized", f"p95 CPU {cpu_p95:.1f}% over {sample_days}d (underutilized).")
    return None


def _has_coverage(metrics, min_sample_days):
    """A long time range alone is not evidence that metrics were actually collected."""
    if not isinstance(metrics, dict) or _as_int(metrics.get("sample_days")) < min_sample_days:
        return False
    coverage = _as_float(metrics.get("sample_coverage_pct"))
    count = _as_float(metrics.get("sample_count"))
    expected = _as_float(metrics.get("expected_sample_count"))
    if count is not None or expected is not None:
        if count is None or expected is None or not 0 < count <= expected:
            return False
        count_coverage = 100.0 * count / expected
        coverage = min(coverage, count_coverage) if coverage is not None else count_coverage
    return coverage is not None and DEFAULT_MIN_SAMPLE_COVERAGE_PCT <= coverage <= 100


def _validate_utilization(metrics, kind, activity, cpu_idle_pct, cpu_underutil_pct,
                          min_sample_days):
    """Contradiction wins even when other required evidence is missing or sparse."""
    metrics = metrics if isinstance(metrics, dict) else {}
    activity = activity if isinstance(activity, dict) else {}
    cpu = _as_float(metrics.get("cpu_p95"))
    memory = _as_float(metrics.get("mem_p95"))
    cpu_limit = cpu_idle_pct if kind == "idle" else cpu_underutil_pct
    memory_limit = DEFAULT_MEMORY_IDLE_PCT if kind == "idle" else DEFAULT_MEMORY_UNDERUTIL_PCT
    workload = _as_float(activity.get("workload_activity_total"))
    requests = _as_float(activity.get("requests_total"))
    contradictions = []
    if cpu is not None and 0 <= cpu <= 100 and cpu >= cpu_limit:
        contradictions.append(f"p95 CPU {cpu:.1f}% (>= {cpu_limit:g}%)")
    if memory is not None and 0 <= memory <= 100 and memory >= memory_limit:
        contradictions.append(f"p95 memory {memory:.1f}% (>= {memory_limit:g}%)")
    if kind == "idle" and (workload is not None and workload > 0 or requests is not None and requests > 0):
        contradictions.append("observed workload activity")
    if contradictions:
        return False, "Utilization contradicts the recommendation: " + "; ".join(contradictions) + "."
    if cpu is None or memory is None or not (0 <= cpu <= 100 and 0 <= memory <= 100):
        return None, "CPU/memory metrics missing or invalid — validate before acting."
    if not _has_coverage(metrics, min_sample_days):
        return None, "CPU/memory sample coverage insufficient or unknown — validate before acting."
    if kind == "idle" and (workload != 0 or not _has_coverage(activity, min_sample_days)):
        return None, "Workload/background activity or its sample coverage unconfirmed — validate before acting."
    return True, (f"Utilization supports review: p95 CPU {cpu:.1f}%, memory {memory:.1f}%; "
                  f">= {min_sample_days}d with >= {DEFAULT_MIN_SAMPLE_COVERAGE_PCT:g}% sample coverage.")


_RATE_TERMS = ("savings plan", "savings_plan", "savingsplan", "reserved instance", "reservation",
               "reserved capacity")


def _is_rate_recommendation(rec):
    """True for Advisor reservation / savings plan purchases: rate optimization, not waste.

    They belong to finops-commitment-advisor; counting them here would mislabel a purchase as
    a resize and add it to addressable waste.
    """
    text = " ".join(str(rec.get(k) or "") for k in
                    ("problem", "recommendation", "targetSku", "impactedField")).lower()
    if any(term in text for term in _RATE_TERMS):
        return True
    rid = str(rec.get("resourceId") or "").strip().strip("/").split("/")
    return len(rid) <= 2 and (not rid[0] or rid[0].lower() == "subscriptions")


def _advisor_savings_estimate(advisor_savings, monthly, fraction):
    if advisor_savings is not None:
        return advisor_savings
    if monthly is not None:
        return monthly * fraction
    return None


def _rank(findings, min_monthly_savings_usd):
    result = []
    for f in findings:
        d = f.to_dict()
        savings = d["estMonthlySavings"]
        if isinstance(savings, (int, float)) and savings < min_monthly_savings_usd:
            continue  # quantified and too small to bother
        result.append(d)

    # Known savings first (desc); unknown-savings findings last.
    def sort_key(d):
        savings = d["estMonthlySavings"]
        known = isinstance(savings, (int, float))
        return (0 if known else 1, -(savings if known else 0))

    result.sort(key=sort_key)
    return result


def _as_float(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


def _as_int(value):
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
