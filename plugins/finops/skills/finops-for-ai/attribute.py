"""FinOps for AI — attribute Azure AI spend per resource / model / service family.

Pure, offline, deterministic (no Azure calls). Given Cost Management UsageDetails line
items already filtered to the AI service families, it classifies every dollar and rolls it
up so an owner can see WHERE the AI money goes and WHICH cost driver to look at first.

Why this exists (see SKILL.md + the pack README's "AI resource taxonomy" note): Azure AI
spend is easy to under-count. Classic **Azure OpenAI** accounts are
`Microsoft.CognitiveServices/accounts` with `kind=OpenAI`; **Azure AI Foundry** (the unified
resource, formerly AI Services) is the SAME resource type with `kind=AIServices`. An OpenAI
model deployed INSIDE a Foundry account bills under `Microsoft.CognitiveServices` with the
same token meter names but `kind=AIServices` — so filtering on `kind==OpenAI` or a meter
category literally named "Azure OpenAI" DROPS Foundry-hosted model spend. This module keys off
`consumedService` (both kinds roll up under `Microsoft.CognitiveServices`) and never gates on
kind. It also covers `Microsoft.MachineLearningServices` (Foundry hub/project compute, managed
online endpoints, fine-tuning) so ML compute isn't dropped.

Line-item shape (one dict per UsageDetails row, already flattened by the skill; the skill's
`--query` projection must add consumedService + meterSubCategory + meterName on top of the
usual cost projection):

    {
        "cost": 12.34,                                   # properties.costInUSD
        "consumedService": "Microsoft.CognitiveServices",
        "meterCategory": "Azure OpenAI",
        "meterSubCategory": "gpt-4o",                    # often carries the model
        "meterName": "gpt-4o-0513 Inp glbl Tokens",
        "resourceId": "/subscriptions/.../accounts/myaoai",
        "date": "2026-07-15",                            # optional; used only for as_of
    }

Optional `resource_kinds {resourceId: kind}` (from a Resource Graph GET) lets the rollup label
each resource OpenAI vs AIServices vs the ML kind; omit it and the label is just left off.
An optional per-row `tags` object (top-level UsageDetail `tags`) enables Foundry project
chargeback: Foundry automatically applies a `project` tag to project usage (preview, models sold
by Azure only).

Microsoft does not publish a meter-name -> token-type mapping, so the token-direction
(input / cached input / cache write / output) and deployment-type (Global / Data Zone /
Regional / Batch / Flex / Provisioned / Priority) splits are best-effort meter-name parsing.
Unrecognised meters stay "unspecified", and hints built on them are labelled verify-first.

Output: a single dict (see attribute_ai_costs) — total AI spend, breakdowns by service family,
meter type (model-token vs compute vs other-cognitive), resource, model, token direction,
deployment type, and Foundry project, the top blended cost drivers, and light read-only
optimization hints. Feed it to a table / Live Report.
"""

import re
from collections import defaultdict

# consumedService -> the service family we report under. Keys matched case-insensitively.
_SERVICE_FAMILY = {
    "microsoft.cognitiveservices": "Cognitive Services / OpenAI",
    "microsoft.machinelearningservices": "Machine Learning",
}

# meter-type buckets
_MODEL_TOKEN = "model_token"        # LLM/embedding token usage (the AOAI/Foundry model spend)
_COMPUTE = "compute"                # managed compute / endpoints / training (mostly ML)
_OTHER_COGNITIVE = "other_cognitive"  # Speech/Vision/Language/etc. transaction meters

# substrings (lower-cased) that mark a line as token/model usage
_TOKEN_HINTS = ("token", "tokens")
# substrings that mark a line as compute (fine-tuned model hosting bills hourly like compute)
_COMPUTE_HINTS = ("compute", "vcpu", "vm ", "instance", "endpoint", "training", "inference hour",
                  "core hour", "hosting")
# meter-name suffixes that trail the model name, stripped when parsing the model
_MODEL_SUFFIX_MARKERS = (" inp", " outp", " input", " output", " cached", " cchd", " token", " glbl",
                         " regional", " data zone", " dz", " batch", " flex")

# token-direction buckets (model_token lines only)
_INPUT = "input"
_CACHED_INPUT = "cached_input"
_CACHE_WRITE = "cache_write"
_OUTPUT = "output"
_UNSPECIFIED = "unspecified"

# meter-name words (lower-cased, split on non-alphanumerics) for token direction
_CACHE_WORDS = {"cached", "cache", "cchd", "cch"}
_WRITE_WORDS = {"write", "writes", "wr", "wrt"}
_INPUT_WORDS = {"inp", "input", "inpt", "prompt"}
_OUTPUT_WORDS = {"outp", "output", "outpt", "completion"}

# deployment-type words, checked in this order (Global Batch meters can also carry "glbl")
_DEPLOYMENT_WORDS = (
    ("batch", {"batch"}),
    ("flex", {"flex"}),
    ("provisioned", {"ptu", "provisioned", "prvsnd"}),
    ("priority", {"priority"}),
    ("data_zone", {"dz", "datazone"}),
    ("global", {"glbl", "global"}),
    ("regional", {"regional", "regnl", "rgnl"}),
)
_ASYNC_DEPLOYMENTS = {"batch", "flex"}

_TOP_DRIVERS = 10
_TOP_RESOURCES = 25
_TOP_MODELS = 25

# a single model taking >= this share of model-token spend is flagged for review
_CONCENTRATION_PCT = 60.0
# steady model-token spend on one resource at/above this monthly $ is flagged for a PTU/commitment look
_COMMITMENT_USD = 1000.0
# more than this many distinct AI resources each under _SPRAWL_EACH_USD is flagged as sprawl
_SPRAWL_COUNT = 8
_SPRAWL_EACH_USD = 25.0
# input-side model-token spend at/above this monthly $ is worth a prompt-caching / context look
_CONTEXT_MIN_USD = 100.0
# cached-input under this % of input-side spend suggests the stable prompt prefix isn't reused
_CACHE_LOW_PCT = 10.0
# input-side (input + cached + cache write) at/above this % of a model's token $ means context dominates
_INPUT_HEAVY_PCT = 70.0
# model-token spend at/above this monthly $ with no Batch/Flex usage is worth an async-tier look
_BATCH_MIN_USD = 500.0
# Foundry automatically tags project usage with this tag key (preview)
_PROJECT_TAG = "project"
_UNTAGGED = "(untagged)"


def _norm(text) -> str:
    return str(text or "").strip().lower()


def _key(resource_id) -> str:
    """Case-insensitive resource-id key (ARM ids are case-insensitive)."""
    return _norm(resource_id)


def _resource_name(resource_id) -> str:
    """Last ARM path segment (the account/workspace name), else the raw id."""
    rid = str(resource_id or "").strip()
    return rid.rsplit("/", 1)[-1] if "/" in rid else rid


def _service_family(consumed_service) -> str:
    """Report family for a consumedService; None if it isn't an AI service we track."""
    return _SERVICE_FAMILY.get(_norm(consumed_service))


def _meter_type(family, item) -> str:
    """Classify a line as model-token, compute, or other-cognitive spend."""
    name = _norm(item.get("meterName"))
    sub = _norm(item.get("meterSubCategory"))
    cat = _norm(item.get("meterCategory"))
    haystack = " ".join((name, sub, cat))

    if any(h in haystack for h in _TOKEN_HINTS):
        return _MODEL_TOKEN
    if any(h in haystack for h in _COMPUTE_HINTS):
        return _COMPUTE
    # Machine Learning with no token signal is compute (managed endpoints / training).
    if family == "Machine Learning":
        return _COMPUTE
    return _OTHER_COGNITIVE


def _meter_words(item) -> set:
    text = " ".join((_norm(item.get("meterName")), _norm(item.get("meterSubCategory"))))
    return {w for w in re.split(r"[^a-z0-9]+", text) if w}


def _token_direction(words) -> str:
    """Best-effort input / cached-input / cache-write / output split from meter words."""
    if words & _CACHE_WORDS:
        return _CACHE_WRITE if words & _WRITE_WORDS else _CACHED_INPUT
    if words & _INPUT_WORDS:
        return _INPUT
    if words & _OUTPUT_WORDS:
        return _OUTPUT
    return _UNSPECIFIED


def _deployment_type(item, words) -> str:
    """Best-effort deployment type from meter words; "unspecified" when not encoded."""
    if "data zone" in _norm(item.get("meterName")):
        words = words | {"dz"}
    for label, markers in _DEPLOYMENT_WORDS:
        if words & markers:
            return label
    return _UNSPECIFIED


def _is_fine_tune_hosting(item, words) -> bool:
    if "hosting" in _norm(item.get("meterName")):
        return True
    return bool(words & {"ft", "finetuned", "finetune"}) and bool(words & {"hour", "hours", "hr"})


def _project_tag(item) -> str:
    """Value of the Foundry `project` tag (case-insensitive key), or "" when absent."""
    tags = item.get("tags")
    if not isinstance(tags, dict):
        return ""
    for key, value in tags.items():
        if _norm(key) == _PROJECT_TAG and str(value or "").strip():
            return str(value).strip()
    return ""


def _parse_model(item) -> str:
    """Best-effort model name for a model-token line.

    Prefer meterSubCategory when it looks like a model; else strip the trailing
    token-type words off meterName. Returns "" when nothing usable is present.
    """
    sub = str(item.get("meterSubCategory") or "").strip()
    if sub and _norm(sub) not in ("", "tokens", "token"):
        return sub

    name = str(item.get("meterName") or "").strip()
    if not name:
        return ""
    low = name.lower()
    cut = len(name)
    for marker in _MODEL_SUFFIX_MARKERS:
        idx = low.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    model = name[:cut].strip()
    return model or name


def _pct(part, whole) -> float:
    if not whole:
        return 0.0
    return round(100.0 * part / whole, 1)


def _ranked(cost_by_key, *, label, extra=None, limit=None):
    """Turn a {key: usd} map into a sorted list of {label, monthly_usd, pct[, extra...]}."""
    total = sum(cost_by_key.values())
    rows = []
    for key, usd in cost_by_key.items():
        row = {label: key, "monthly_usd": round(usd, 2), "pct": _pct(usd, total)}
        if extra:
            row.update(extra(key))
        rows.append(row)
    rows.sort(key=lambda r: r["monthly_usd"], reverse=True)
    return rows[:limit] if limit else rows


def attribute_ai_costs(line_items=None, *, resource_kinds=None):
    """Attribute AI spend across resources, models, service families, and meter types.

    line_items      list of UsageDetails rows (already filtered to AI service families).
                    Rows whose consumedService isn't an AI family are ignored defensively.
    resource_kinds  optional {resourceId: kind} (from Resource Graph) to label a resource
                    OpenAI / AIServices / the ML kind; omit to leave the label off.

    Returns a dict:
      {
        "as_of",                       # max line-item date, or None
        "total_ai_usd",
        "resource_count", "model_count",
        "by_service_family": [{"service_family","monthly_usd","pct"}...],
        "by_meter_type":     [{"meter_type","monthly_usd","pct"}...],
        "by_resource":       [{"resourceId","resourceName","kind","service_family","monthly_usd","pct","top_model"}...],
        "by_model":          [{"model","monthly_usd","pct","resource_count"}...],   # model_token only
        "top_drivers":       [{"resourceName","service_family","model","meter_type","monthly_usd"}...],
        "by_token_direction":  [{"token_direction","monthly_usd","pct"}...],  # model_token only
        "by_deployment_type":  [{"deployment_type","monthly_usd","pct"}...],  # model_token only
        "by_project":          [{"project","monthly_usd","pct"}...],  # [] unless a `project` tag is seen
        "hints":             [{"type","target","detail","monthly_usd"}...],
      }
    """
    line_items = line_items or []
    kinds = {_key(rid): k for rid, k in (resource_kinds or {}).items()}

    total = 0.0
    as_of = None
    fam_cost = defaultdict(float)
    type_cost = defaultdict(float)
    model_cost = defaultdict(float)                 # model -> usd (model_token lines only)
    model_resources = defaultdict(set)              # model -> {resourceId}
    res_cost = defaultdict(float)                   # resourceId -> usd
    res_meta = {}                                   # resourceId -> {name, family}
    res_model_cost = defaultdict(lambda: defaultdict(float))  # resourceId -> {model: usd}
    driver_cost = defaultdict(float)                # (resourceId, model, meter_type) -> usd
    direction_cost = defaultdict(float)             # token direction -> usd (model_token only)
    model_direction_cost = defaultdict(lambda: defaultdict(float))  # model -> {direction: usd}
    deployment_cost = defaultdict(float)            # deployment type -> usd (model_token only)
    project_cost = defaultdict(float)               # project tag -> usd
    tagged_project_seen = False
    ft_hosting_cost = defaultdict(float)            # resourceId -> fine-tuned hosting usd

    for item in line_items:
        cost = item.get("cost")
        if not isinstance(cost, (int, float)):
            continue
        family = _service_family(item.get("consumedService"))
        if family is None:
            continue  # not an AI service family — skip defensively

        total += cost
        date = item.get("date")
        if date and (as_of is None or str(date) > as_of):
            as_of = str(date)

        mtype = _meter_type(family, item)
        words = _meter_words(item)
        rid = item.get("resourceId") or ""
        fam_cost[family] += cost
        type_cost[mtype] += cost
        res_cost[rid] += cost
        res_meta.setdefault(rid, {"name": _resource_name(rid), "family": family})

        project = _project_tag(item)
        tagged_project_seen = tagged_project_seen or bool(project)
        project_cost[project or _UNTAGGED] += cost

        if mtype == _COMPUTE and _is_fine_tune_hosting(item, words):
            ft_hosting_cost[rid] += cost

        model = ""
        if mtype == _MODEL_TOKEN:
            direction = _token_direction(words)
            direction_cost[direction] += cost
            deployment_cost[_deployment_type(item, words)] += cost
            model = _parse_model(item)
            if model:
                model_cost[model] += cost
                model_resources[model].add(rid)
                res_model_cost[rid][model] += cost
                model_direction_cost[model][direction] += cost
        driver_cost[(rid, model, mtype)] += cost

    by_service_family = _ranked(fam_cost, label="service_family")
    by_meter_type = _ranked(type_cost, label="meter_type")

    by_model = _ranked(
        model_cost, label="model",
        extra=lambda m: {"resource_count": len(model_resources[m])},
        limit=_TOP_MODELS,
    )

    def _res_extra(rid):
        meta = res_meta.get(rid, {"name": _resource_name(rid), "family": ""})
        top_model = ""
        if res_model_cost.get(rid):
            top_model = max(res_model_cost[rid].items(), key=lambda kv: kv[1])[0]
        return {
            "resourceName": meta["name"],
            "kind": kinds.get(_key(rid), ""),
            "service_family": meta["family"],
            "top_model": top_model,
        }

    by_resource = _ranked(res_cost, label="resourceId", extra=_res_extra, limit=_TOP_RESOURCES)

    top_drivers = []
    for (rid, model, mtype), usd in sorted(driver_cost.items(), key=lambda kv: kv[1], reverse=True)[:_TOP_DRIVERS]:
        meta = res_meta.get(rid, {"name": _resource_name(rid), "family": ""})
        top_drivers.append({
            "resourceName": meta["name"],
            "service_family": meta["family"],
            "model": model,
            "meter_type": mtype,
            "monthly_usd": round(usd, 2),
        })

    hints = _build_hints(
        model_cost=model_cost,
        res_cost=res_cost,
        res_meta=res_meta,
        res_model_cost=res_model_cost,
        direction_cost=direction_cost,
        model_direction_cost=model_direction_cost,
        deployment_cost=deployment_cost,
        ft_hosting_cost=ft_hosting_cost,
    )

    return {
        "as_of": as_of,
        "total_ai_usd": round(total, 2),
        "resource_count": len(res_cost),
        "model_count": len(model_cost),
        "by_service_family": by_service_family,
        "by_meter_type": by_meter_type,
        "by_resource": by_resource,
        "by_model": by_model,
        "top_drivers": top_drivers,
        "by_token_direction": _ranked(direction_cost, label="token_direction"),
        "by_deployment_type": _ranked(deployment_cost, label="deployment_type"),
        "by_project": _ranked(project_cost, label="project") if tagged_project_seen else [],
        "hints": hints,
    }


def _build_hints(*, model_cost, res_cost, res_meta, res_model_cost, direction_cost=None,
                 model_direction_cost=None, deployment_cost=None, ft_hosting_cost=None):
    """Light, read-only, COST-ONLY advisory hints. Never claims idle without utilization —
    true idle/under-use detection needs Azure Monitor token metrics (deferred to v2)."""
    hints = []
    direction_cost = direction_cost or {}
    model_direction_cost = model_direction_cost or {}
    deployment_cost = deployment_cost or {}
    ft_hosting_cost = ft_hosting_cost or {}

    # 1. Model concentration — one model dominates model-token spend.
    model_total = sum(model_cost.values())
    if model_total > 0:
        top_model, top_usd = max(model_cost.items(), key=lambda kv: kv[1])
        share = _pct(top_usd, model_total)
        if share >= _CONCENTRATION_PCT:
            hints.append({
                "type": "model_concentration",
                "target": top_model,
                "detail": (f"{top_model} is {share}% of model-token spend — most workloads mix simple and "
                           f"complex requests; evaluate model router (cost or balanced mode) or a smaller/"
                           f"fine-tuned model for routine requests, gated on your evaluation set."),
                "monthly_usd": round(top_usd, 2),
            })

    # 2. Commitment / PTU look — a resource with high steady model-token spend.
    for rid, models in res_model_cost.items():
        usd = sum(models.values())
        if usd >= _COMMITMENT_USD:
            name = res_meta.get(rid, {}).get("name", _resource_name(rid))
            hints.append({
                "type": "commitment_opportunity",
                "target": name,
                "detail": (f"{name} has ~${round(usd, 2)}/mo of model-token spend — if traffic is steady and "
                           f"predictable, evaluate Provisioned Throughput (PTU) with spillover to pay-as-you-go "
                           f"vs Standard (estimate; confirm with token-usage metrics)."),
                "monthly_usd": round(usd, 2),
            })

    # 3. Compute with no model tokens — a Cognitive/ML resource billing compute but no token
    #    usage may be an unused deployment/endpoint. Flag to VERIFY (needs metrics to confirm).
    #    Fine-tuned hosting gets its own, more specific hint (5) instead.
    for rid, usd in res_cost.items():
        if usd <= 0:
            continue
        if not res_model_cost.get(rid) and not ft_hosting_cost.get(rid):
            meta = res_meta.get(rid, {})
            name = meta.get("name", _resource_name(rid))
            hints.append({
                "type": "compute_no_tokens_verify",
                "target": name,
                "detail": (f"{name} bills ~${round(usd, 2)}/mo with no model-token usage — verify it "
                           f"isn't an idle endpoint/compute; confirm with Azure Monitor metrics before acting."),
                "monthly_usd": round(usd, 2),
            })

    # 4. Sprawl — many small AI resources.
    small = [rid for rid, usd in res_cost.items() if 0 < usd < _SPRAWL_EACH_USD]
    if len(small) >= _SPRAWL_COUNT:
        hints.append({
            "type": "resource_sprawl",
            "target": f"{len(small)} resources",
            "detail": (f"{len(small)} AI resources each under ${_SPRAWL_EACH_USD}/mo — consider "
                       f"consolidating into fewer accounts to simplify governance and quota."),
            "monthly_usd": round(sum(res_cost[rid] for rid in small), 2),
        })

    # 5. Fine-tuned model hosting bills hourly whether or not the deployment serves traffic.
    for rid, usd in ft_hosting_cost.items():
        if usd <= 0:
            continue
        name = res_meta.get(rid, {}).get("name", _resource_name(rid))
        hints.append({
            "type": "fine_tune_hosting_verify",
            "target": name,
            "detail": (f"{name} bills ~${round(usd, 2)}/mo of fine-tuned model hosting, charged hourly "
                       f"regardless of traffic — verify each deployment is used; an undeployed fine-tuned "
                       f"model stays stored and can be redeployed later."),
            "monthly_usd": round(usd, 2),
        })

    # 6. Prompt caching — input-side spend with little or no cached-input billing.
    input_side = sum(direction_cost.get(d, 0.0) for d in (_INPUT, _CACHED_INPUT, _CACHE_WRITE))
    cached = direction_cost.get(_CACHED_INPUT, 0.0)
    if input_side >= _CONTEXT_MIN_USD and _pct(cached, input_side) < _CACHE_LOW_PCT:
        hints.append({
            "type": "prompt_caching_verify",
            "target": "model-token input",
            "detail": (f"Cached input is {_pct(cached, input_side)}% of ~${round(input_side, 2)}/mo input-side "
                       f"spend — verify cache hits (cached_tokens); put stable instructions, tool schemas, and "
                       f"examples first and per-request content (timestamps, user data, history) last. "
                       f"Meters may not split cached tokens."),
            "monthly_usd": round(direction_cost.get(_INPUT, 0.0), 2),
        })

    # 7. Context-heavy models — input-side dollars dominate (context window paid every turn).
    for model, dirs in model_direction_cost.items():
        model_usd = sum(dirs.values())
        side = sum(dirs.get(d, 0.0) for d in (_INPUT, _CACHED_INPUT, _CACHE_WRITE))
        if side < _CONTEXT_MIN_USD or not dirs.get(_OUTPUT):
            continue
        share = _pct(side, model_usd)
        if share >= _INPUT_HEAVY_PCT:
            hints.append({
                "type": "context_heavy_input",
                "target": model,
                "detail": (f"{model}: {share}% of token spend is input — the context window is re-sent every "
                           f"turn; review retrieved content, tool definitions (tool search/toolboxes), and "
                           f"replayed history (summaries/memory) before changing models."),
                "monthly_usd": round(side, 2),
            })

    # 8. No Batch/Flex usage on material spend — async work may be on interactive pricing.
    token_total = sum(deployment_cost.values())
    classified = token_total - deployment_cost.get(_UNSPECIFIED, 0.0)
    async_usd = sum(deployment_cost.get(d, 0.0) for d in _ASYNC_DEPLOYMENTS)
    if token_total >= _BATCH_MIN_USD and classified > 0 and async_usd == 0:
        hints.append({
            "type": "batch_candidate",
            "target": "model-token spend",
            "detail": (f"~${round(token_total, 2)}/mo of model-token spend with no Batch/Flex meters — move "
                       f"delay-tolerant work (classification, extraction, evaluation runs) to Batch (50% less "
                       f"than Global Standard, 24-hour target) or Flex if eligible (estimate)."),
            "monthly_usd": round(token_total, 2),
        })

    hints.sort(key=lambda h: h["monthly_usd"], reverse=True)
    return hints
