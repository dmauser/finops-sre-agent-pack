"""Deterministic adapters from FinOps analysis outputs to report models."""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


_CENT = Decimal("0.01")
_STATUS_ORDER = {
    "over_budget": 0,
    "forecast_over": 1,
    "at_risk": 2,
    "on_track": 3,
}
_VERIFY_HINTS = {
    "commitment_opportunity",
    "compute_no_tokens_verify",
    "fine_tune_hosting_verify",
    "prompt_caching_verify",
    "batch_candidate",
    "always_on_waived_verify",
    "deleted_agent_billed",
    "model_choice_verify",
}


def _field(obj, *names, default=None):
    for name in names:
        if name in obj:
            return obj[name]
    return default


def _object(value, field):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _objects(value, field):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    rows = []
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            raise ValueError(f"{field}[{index}] must be an object")
        rows.append(row)
    return rows


def _strings(value, field):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"{field}[{index}] must be text")
        result.append(item)
    return result


def _text(value, field, *, default=""):
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    return value


def _boolean(value, field, *, default=False):
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be boolean")
    return value


def _decimal(value, field, *, optional=False):
    if value is None:
        if optional:
            return None
        return Decimal(0)
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{field} must be a finite number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite number")
    return result


def _integer(value, field, *, default=0):
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _money(value, currency="USD"):
    if value is None:
        return "—"
    amount = value.quantize(_CENT, rounding=ROUND_HALF_UP)
    if currency == "USD":
        sign = "-" if amount < 0 else ""
        return f"{sign}${abs(amount):,.2f}"
    return f"{amount:,.2f} {currency}"


def _percent(value):
    return "—" if value is None else f"{value.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP):,.1f}%"


def _number(value):
    return "—" if value is None else f"{value.normalize():f}"


def _chart_number(value):
    return float(value)


def _decimal_key(value):
    return "" if value is None else format(value, "f")


def _stable_strings(values):
    return sorted(set(values), key=lambda item: (item.casefold(), item))


def _short_resource(resource_id):
    return resource_id.rstrip("/").rsplit("/", 1)[-1] if resource_id else "(unknown)"


def _resource_group_of(resource_id):
    parts = (resource_id or "").split("/")
    for index, part in enumerate(parts[:-1]):
        if part.casefold() == "resourcegroups":
            return parts[index + 1]
    return "—"


def _case_canonicalizer(values):
    """Map case-insensitive Azure names (resource groups, providers) to one display spelling.

    The most frequent spelling wins; ties prefer mixed case over all-lower/all-upper.
    """

    counts = {}
    for value in values:
        if value:
            counts.setdefault(value.casefold(), {}).setdefault(value, 0)
            counts[value.casefold()][value] += 1
    preferred = {
        key: max(spellings, key=lambda v: (spellings[v], v not in (v.lower(), v.upper()), v))
        for key, spellings in counts.items()
    }
    return lambda value: preferred.get(value.casefold(), value) if value else value


def _display_resource(value):
    if value.casefold().startswith("/subscriptions/") and "/providers/" in value.casefold():
        group = _resource_group_of(value)
        name = _short_resource(value)
        return name if group == "—" else f"{name} ({group})"
    return value


def _validated(value, field):
    if value is None:
        return "verify first"
    valid = _boolean(value, field)
    return "validated" if valid else "contradicted — verify first"


def _coverage_count(row, prefix, name, *keys):
    value = _field(row, *keys)
    if value is None:
        return "—"
    if isinstance(value, list):
        return str(len(value))
    return str(_integer(value, f"{prefix}.{name}"))


def _scope_coverage_section(rows):
    coverage = []
    any_partial = False
    any_pct = False
    for index, row in enumerate(_objects(rows, "scope_coverage")):
        prefix = f"scope_coverage[{index}]"
        scope = _text(_field(row, "scope", "scopeId"), f"{prefix}.scope")
        status = _text(row.get("status"), f"{prefix}.status", default="complete")
        cost = _field(row, "cost_usd", "costUsd", "included_cost_usd", "included_cost")
        pct = _decimal(
            _field(row, "attribution_coverage_pct", "coverage_pct"),
            f"{prefix}.attribution_coverage_pct",
            optional=True,
        )
        any_pct = any_pct or pct is not None
        detail = _text(
            _field(row, "detail", "warning"), f"{prefix}.detail", default=""
        )
        row_partial = _boolean(row.get("partial"), f"{prefix}.partial")
        any_partial = any_partial or row_partial
        coverage.append(
            (
                scope,
                status,
                _coverage_count(row, prefix, "included", "included_count", "includedCount", "included"),
                _coverage_count(row, prefix, "excluded", "excluded_count", "excludedCount", "excluded"),
                _coverage_count(
                    row, prefix, "unattributed", "unattributed_count", "unattributedCount", "unattributed"
                ),
                _money(_decimal(cost, f"{prefix}.cost_usd", optional=True)),
                detail,
                _percent(pct),
            )
        )
    coverage.sort(
        key=lambda row: (
            row[0].casefold(),
            row[0],
            row[1].casefold(),
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6].casefold(),
            row[6],
        )
    )
    if not coverage:
        return None, any_partial
    columns = ["Scope", "Status", "Included", "Excluded", "Unattributed", "Cost", "Detail"]
    if any_pct:
        columns.insert(6, "Attribution")
        table = [list(row[:6]) + [row[7], row[6]] for row in coverage]
    else:
        table = [list(row[:7]) for row in coverage]
    return {
        "title": "Scope coverage",
        "description": "Included, excluded, and unattributed results by managed scope.",
        "columns": columns,
        "rows": table,
        "emptyMessage": "No scope coverage rows were supplied.",
    }, any_partial


def _context(
    source,
    *,
    refreshed_at,
    scope_summary,
    partial,
    warnings,
    scope_coverage,
    extra_partial=False,
    extra_warnings=None,
):
    source = source if isinstance(source, dict) else {}
    refreshed = _text(refreshed_at, "refreshed_at").strip()
    if not refreshed:
        raise ValueError("refreshed_at is required")

    source_scopes = _field(source, "scope_summary", "scopeSummary", default=[])
    scopes = _strings(
        source_scopes if scope_summary is None else scope_summary, "scope_summary"
    )
    source_warnings = _strings(source.get("warnings"), "warnings")
    supplied_warnings = _strings(warnings, "warnings")
    generated_warnings = _strings(extra_warnings, "extra_warnings")
    source_partial = _boolean(source.get("partial"), "partial")
    supplied_partial = _boolean(partial, "partial")

    coverage_rows = scope_coverage
    if coverage_rows is None:
        coverage_rows = _field(source, "scope_coverage", "scopeCoverage", default=[])
    coverage_section, coverage_partial = _scope_coverage_section(coverage_rows)
    return {
        "refreshedAt": refreshed,
        "scopeSummary": _stable_strings(scopes),
        "partial": source_partial or supplied_partial or extra_partial or coverage_partial,
        "warnings": _stable_strings(
            source_warnings + supplied_warnings + generated_warnings
        ),
        "coverageSection": coverage_section,
    }


def _model(title, description, context, metrics, chart, sections):
    coverage = context["coverageSection"]
    if coverage:
        sections = list(sections) + [coverage]
    return {
        "title": title,
        "description": description,
        "refreshedAt": context["refreshedAt"],
        "scopeSummary": context["scopeSummary"],
        "partial": context["partial"],
        "warnings": context["warnings"],
        "metrics": metrics,
        "chart": chart,
        "sections": sections,
    }


def build_cost_overview_model(
    overview,
    *,
    refreshed_at,
    scope_summary=None,
    partial=False,
    warnings=None,
    scope_coverage=None,
):
    """Adapt a deterministic 30-day cost aggregation to the generic report model."""

    overview = _object(overview, "overview")
    metric = _text(
        _field(overview, "cost_metric", "costMetric"), "overview.cost_metric",
        default="AmortizedCost",
    ) or "AmortizedCost"
    currency = _text(
        _field(overview, "currency"), "overview.currency", default="USD"
    ) or "USD"
    total = _decimal(
        _field(overview, "total_usd", "totalUsd", "total_cost", "total"), "overview.total_usd"
    )
    daily = []
    for index, row in enumerate(
        _objects(_field(overview, "daily", "daily_totals", default=[]), "overview.daily")
    ):
        date = _text(_field(row, "date", "label"), f"overview.daily[{index}].date")
        cost = _decimal(
            _field(row, "cost_usd", "costUsd", "cost"),
            f"overview.daily[{index}].cost_usd",
        )
        daily.append((date, cost))
    daily.sort(key=lambda row: (row[0], row[1]))

    services = []
    for index, row in enumerate(
        _objects(
            _field(overview, "by_service", "top_services", default=[]),
            "overview.by_service",
        )
    ):
        name = _text(
            _field(row, "service", "name"), f"overview.by_service[{index}].service"
        )
        cost = _decimal(
            _field(row, "cost_usd", "costUsd", "cost"),
            f"overview.by_service[{index}].cost_usd",
        )
        services.append((name, cost))
    services.sort(key=lambda row: (-row[1], row[0].casefold(), row[0]))

    groups = []
    for index, row in enumerate(
        _objects(
            _field(overview, "by_resource_group", "top_resource_groups", default=[]),
            "overview.by_resource_group",
        )
    ):
        name = _text(
            _field(row, "resource_group", "resourceGroup", "name"),
            f"overview.by_resource_group[{index}].resource_group",
        )
        cost = _decimal(
            _field(row, "cost_usd", "costUsd", "cost"),
            f"overview.by_resource_group[{index}].cost_usd",
        )
        groups.append((name or "(unattributed)", cost))
    # Azure resource-group names are case-insensitive; UsageDetails can emit
    # several casings for one group, so merge them under the costliest casing.
    merged = {}
    for name, cost in groups:
        entry = merged.setdefault(name.casefold(), {"total": Decimal(0), "variants": {}})
        entry["total"] += cost
        entry["variants"][name] = entry["variants"].get(name, Decimal(0)) + cost
    groups = [
        (
            sorted(entry["variants"].items(), key=lambda item: (-item[1], item[0]))[0][0],
            entry["total"],
        )
        for entry in merged.values()
    ]
    groups.sort(key=lambda row: (-row[1], row[0].casefold(), row[0]))
    dates = sorted({date for date, _ in daily})
    if dates and len(dates) < 28:
        spend_label = "Period spend"
        spend_detail = f"{metric}, {dates[0]} to {dates[-1]} ({len(dates)} days)"
    else:
        spend_label = "30-day spend"
        spend_detail = metric

    coverage = scope_coverage
    if coverage is None:
        coverage = _field(overview, "scope_coverage", "scopeCoverage", "coverage", default=[])
    context = _context(
        overview,
        refreshed_at=refreshed_at,
        scope_summary=scope_summary,
        partial=partial,
        warnings=warnings,
        scope_coverage=coverage,
    )
    chart = {
        "type": "line",
        "label": f"Daily {metric} ({currency})",
        "labels": [date for date, _ in daily],
        "values": [_chart_number(cost) for _, cost in daily],
    }
    return _model(
        "FinOps: Cost Overview",
        f"Daily-refreshed Azure {metric} snapshot of up to 30 days; recent UsageDetails can still be settling.",
        context,
        [
            {"label": spend_label, "value": _money(total, currency), "detail": spend_detail},
            {
                "label": "Days represented",
                "value": str(len(daily)),
                "detail": "Daily cost points",
            },
            {
                "label": "Services represented",
                "value": str(len(services)),
                "detail": "Ranked service totals",
            },
        ],
        chart,
        [
            {
                "title": "Top services",
                "description": "Service totals ranked by cost.",
                "columns": ["Service", "Cost"],
                "rows": [[name, _money(cost, currency)] for name, cost in services],
                "emptyMessage": "No service cost rows were produced.",
            },
            {
                "title": "Top resource groups",
                "description": "Resource-group totals ranked by cost.",
                "columns": ["Resource group", "Cost"],
                "rows": [[name, _money(cost, currency)] for name, cost in groups],
                "emptyMessage": "No resource-group cost rows were produced.",
            },
        ],
    )


def build_rightsizing_savings_model(
    result,
    *,
    refreshed_at,
    scope_summary=None,
    partial=False,
    warnings=None,
    scope_coverage=None,
):
    """Adapt recommend_rightsizing output to the generic report model."""

    source = result if isinstance(result, dict) else {}
    raw_findings = _field(source, "findings", "recommendations", default=[]) if source else result
    findings = []
    for index, row in enumerate(_objects(raw_findings, "findings")):
        prefix = f"findings[{index}]"
        resource_id = _text(row.get("resourceId"), f"{prefix}.resourceId")
        resource_type = _text(row.get("resourceType"), f"{prefix}.resourceType")
        kind = _text(row.get("kind"), f"{prefix}.kind")
        sku = _text(row.get("currentSku"), f"{prefix}.currentSku")
        action = _text(row.get("recommendedAction"), f"{prefix}.recommendedAction")
        current = _decimal(
            row.get("currentMonthlyUsd"), f"{prefix}.currentMonthlyUsd", optional=True
        )
        savings = _decimal(
            row.get("estMonthlySavingsUsd"),
            f"{prefix}.estMonthlySavingsUsd",
            optional=True,
        )
        validated_raw = row.get("validated")
        validated = _validated(validated_raw, f"{prefix}.validated")
        evidence = _strings(row.get("evidence"), f"{prefix}.evidence")
        findings.append(
            {
                "resource_id": resource_id,
                "resource_type": resource_type,
                "kind": kind,
                "sku": sku,
                "action": action,
                "current": current,
                "savings": savings,
                "validated": validated,
                "validated_raw": validated_raw,
                "evidence": "; ".join(evidence),
            }
        )
    findings.sort(
        key=lambda row: (
            row["savings"] is None,
            -(row["savings"] or Decimal(0)),
            row["resource_id"].casefold(),
            row["resource_id"],
            row["action"].casefold(),
            row["action"],
            row["resource_type"].casefold(),
            row["resource_type"],
            row["kind"].casefold(),
            row["kind"],
            row["sku"].casefold(),
            row["sku"],
            _decimal_key(row["current"]),
            row["validated"],
            row["evidence"].casefold(),
            row["evidence"],
        )
    )
    quantified = [row["savings"] for row in findings if row["savings"] is not None]
    total_savings = sum(quantified, Decimal(0))
    actionable = [row for row in findings if row["kind"].casefold() != "review"]
    review_count = len(findings) - len(actionable)
    verified_count = sum(row["validated_raw"] is True for row in actionable)
    verify_count = len(actionable) - verified_count
    chart_rows = [row for row in findings if row["savings"] is not None][:10]

    context = _context(
        source,
        refreshed_at=refreshed_at,
        scope_summary=scope_summary,
        partial=partial,
        warnings=warnings,
        scope_coverage=scope_coverage,
    )
    return _model(
        "FinOps: Rightsizing Savings",
        "Weekly read-only rightsizing and idle-resource savings snapshot.",
        context,
        [
            {
                "label": "Estimated monthly savings",
                "value": _money(total_savings),
                "detail": f"{len(quantified)} quantified recommendations",
                "tone": "good",
            },
            {
                "label": "Recommendations",
                "value": str(len(actionable)),
                "detail": f"Ranked opportunities; {review_count} high-spend for review",
            },
            {
                "label": "Validated",
                "value": str(verified_count),
                "detail": "Supported by available evidence",
                "tone": "good",
            },
            {
                "label": "Verify first",
                "value": str(verify_count),
                "detail": "Unvalidated or contradicted",
                "tone": "warning" if verify_count else "neutral",
            },
        ],
        {
            "type": "bar",
            "label": "Estimated monthly savings (USD)",
            "labels": [_short_resource(row["resource_id"]) for row in chart_rows],
            "values": [_chart_number(row["savings"]) for row in chart_rows],
        },
        [
            {
                "title": "Ranked savings opportunities",
                "description": "Known savings first; ties are ordered by resource ID.",
                "columns": [
                    "Resource",
                    "Type",
                    "Kind",
                    "Current SKU",
                    "Recommended action",
                    "Current monthly cost",
                    "Estimated monthly savings",
                    "Validation",
                    "Evidence",
                ],
                "rows": [
                    [
                        _display_resource(row["resource_id"]),
                        row["resource_type"] or "—",
                        row["kind"],
                        row["sku"] or "—",
                        row["action"],
                        _money(row["current"]),
                        _money(row["savings"]),
                        row["validated"],
                        row["evidence"],
                    ]
                    for row in findings
                    if row["kind"].casefold() != "review"
                ],
                "emptyMessage": "No rightsizing opportunities were produced.",
            },
            {
                "title": "High spend, no idle rule yet — review",
                "description": "Costly resources no automated rule or Advisor evaluated. No savings are claimed.",
                "columns": ["Resource", "Type", "Current monthly cost", "Evidence"],
                "rows": [
                    [
                        _display_resource(row["resource_id"]),
                        row["resource_type"] or "—",
                        _money(row["current"]),
                        row["evidence"],
                    ]
                    for row in sorted(
                        (row for row in findings if row["kind"].casefold() == "review"),
                        key=lambda row: (
                            -(row["current"] or Decimal(0)),
                            row["resource_id"].casefold(),
                            row["resource_id"],
                        ),
                    )
                ],
                "emptyMessage": "Every costly resource was evaluated by a rule or Advisor.",
            },
        ],
    )


def build_budget_status_model(
    result,
    *,
    refreshed_at,
    scope_summary=None,
    partial=False,
    warnings=None,
    scope_coverage=None,
):
    """Adapt evaluate_budgets output to the generic report model."""

    result = _object(result, "result")
    summary = _object(result.get("summary") or {}, "result.summary")
    budgets = []
    has_run_rate = False
    has_zero_spend = False
    for index, row in enumerate(_objects(result.get("budgets"), "result.budgets")):
        prefix = f"result.budgets[{index}]"
        name = _text(row.get("name"), f"{prefix}.name")
        scope = _text(row.get("scope"), f"{prefix}.scope")
        currency = _text(row.get("currency"), f"{prefix}.currency", default="USD") or "USD"
        amount = _decimal(row.get("amount"), f"{prefix}.amount", optional=True)
        current = _decimal(row.get("current_spend"), f"{prefix}.current_spend")
        used = _decimal(row.get("pct_used"), f"{prefix}.pct_used", optional=True)
        forecast = _decimal(
            row.get("forecast_spend"), f"{prefix}.forecast_spend", optional=True
        )
        forecast_pct = _decimal(
            row.get("pct_forecast"), f"{prefix}.pct_forecast", optional=True
        )
        forecast_source = _text(
            row.get("forecast_source"), f"{prefix}.forecast_source"
        )
        status = _text(row.get("status"), f"{prefix}.status")
        breached = []
        for notice_index, notice in enumerate(
            _objects(row.get("breached_notifications"), f"{prefix}.breached_notifications")
        ):
            notice_name = _text(
                notice.get("name"),
                f"{prefix}.breached_notifications[{notice_index}].name",
            )
            threshold = _decimal(
                notice.get("threshold"),
                f"{prefix}.breached_notifications[{notice_index}].threshold",
            )
            notice_type = _text(
                notice.get("type"),
                f"{prefix}.breached_notifications[{notice_index}].type",
            )
            breached.append(f"{notice_name}: {_percent(threshold)} {notice_type}")
        breached.sort(key=lambda item: (item.casefold(), item))
        has_run_rate = has_run_rate or forecast_source == "run-rate"
        has_zero_spend = has_zero_spend or current == 0
        budgets.append(
            {
                "name": name,
                "scope": scope,
                "currency": currency,
                "amount": amount,
                "current": current,
                "used": used,
                "forecast": forecast,
                "forecast_pct": forecast_pct,
                "forecast_source": forecast_source,
                "status": status,
                "breached": "; ".join(breached),
            }
        )
    budgets.sort(
        key=lambda row: (
            _STATUS_ORDER.get(row["status"], 99),
            -(row["used"] or Decimal(-1)),
            row["name"].casefold(),
            row["name"],
            row["scope"].casefold(),
            row["scope"],
            _decimal_key(row["amount"]),
            _decimal_key(row["current"]),
            _decimal_key(row["forecast"]),
            _decimal_key(row["forecast_pct"]),
            row["forecast_source"],
            row["breached"].casefold(),
            row["breached"],
        )
    )

    gates = []
    for index, row in enumerate(_objects(result.get("gates"), "result.gates")):
        name = _text(row.get("name"), f"result.gates[{index}].name")
        reason = _text(row.get("reason"), f"result.gates[{index}].reason")
        status = _text(row.get("status"), f"result.gates[{index}].status")
        gates.append((status, name, reason))
    gates.sort(
        key=lambda row: (
            _STATUS_ORDER.get(row[0], 99),
            row[1].casefold(),
            row[1],
            row[2].casefold(),
            row[2],
        )
    )

    generated = []
    if len({row["currency"] for row in budgets}) > 1:
        generated.append(
            "Budgets use more than one currency; portfolio totals are not summed across currencies."
        )
    if has_run_rate:
        generated.append("Run-rate forecasts are estimates, not Azure forecastSpend.")
    if has_zero_spend:
        generated.append(
            "Azure updates currentSpend asynchronously; zero spend on a new budget may not be synchronized yet."
        )
    context = _context(
        result,
        refreshed_at=refreshed_at,
        scope_summary=scope_summary,
        partial=partial,
        warnings=warnings,
        scope_coverage=scope_coverage,
        extra_warnings=generated,
    )

    chart_labels = []
    chart_values = []
    for row in budgets[:12]:
        if row["used"] is not None:
            chart_labels.append(f"{row['name']} — used")
            chart_values.append(_chart_number(row["used"]))
        if row["forecast_pct"] is not None:
            chart_labels.append(f"{row['name']} — forecast")
            chart_values.append(_chart_number(row["forecast_pct"]))

    summary_currency = _text(
        summary.get("currency"), "result.summary.currency", default=""
    )
    if not summary_currency:
        currencies = {row["currency"] for row in budgets}
        summary_currency = currencies.pop() if len(currencies) == 1 else ""
    mixed_currency = not summary_currency and bool(budgets)
    total_amount = _decimal(
        summary.get("total_amount"), "result.summary.total_amount", optional=mixed_currency
    )
    total_current = _decimal(
        summary.get("total_current"), "result.summary.total_current", optional=mixed_currency
    )
    total_forecast = _decimal(
        summary.get("total_forecast"), "result.summary.total_forecast", optional=mixed_currency
    )
    if mixed_currency:
        total_amount = total_current = total_forecast = None
    summary_currency = summary_currency or "USD"
    over = _integer(summary.get("over_budget"), "result.summary.over_budget")
    forecast_over = _integer(
        summary.get("forecast_over"), "result.summary.forecast_over"
    )
    at_risk = _integer(summary.get("at_risk"), "result.summary.at_risk")
    on_track = _integer(summary.get("on_track"), "result.summary.on_track")

    return _model(
        "FinOps: Budget Status",
        "Daily Azure budget-governance snapshot; currentSpend is updated asynchronously.",
        context,
        [
            {"label": "Portfolio budget", "value": _money(total_amount, summary_currency), "detail": "Configured amount" if not mixed_currency else "Mixed currencies"},
            {"label": "Current spend", "value": _money(total_current, summary_currency), "detail": "Azure currentSpend"},
            {
                "label": "Forecast spend",
                "value": _money(total_forecast, summary_currency),
                "detail": "Azure or labeled run-rate forecast",
            },
            {
                "label": "Gated budgets",
                "value": str(len(gates)),
                "detail": f"{over} over, {forecast_over} forecast over",
                "tone": "critical" if gates else "good",
            },
            {
                "label": "Portfolio states",
                "value": str(len(budgets)),
                "detail": f"{at_risk} at risk, {on_track} on track",
            },
        ],
        {
            "type": "bar",
            "label": "Percent of budget (100% = budget limit)",
            "labels": chart_labels,
            "values": chart_values,
        },
        [
            {
                "title": "Gated budgets",
                "description": "Budgets requiring a human review before additional spend.",
                "columns": ["Status", "Budget", "Reason"],
                "rows": [list(row) for row in gates],
                "emptyMessage": "No budgets currently require a governance gate.",
            },
            {
                "title": "Budget status",
                "description": "Gated states first, followed by at-risk and on-track budgets.",
                "columns": [
                    "Budget",
                    "Scope",
                    "Amount",
                    "Spent",
                    "Used",
                    "Forecast",
                    "Forecast source",
                    "Forecast %",
                    "Status",
                    "Breached thresholds",
                ],
                "rows": [
                    [
                        row["name"],
                        row["scope"],
                        _money(row["amount"], row["currency"]),
                        _money(row["current"], row["currency"]),
                        _percent(row["used"]),
                        _money(row["forecast"], row["currency"]),
                        row["forecast_source"],
                        _percent(row["forecast_pct"]),
                        row["status"],
                        row["breached"],
                    ]
                    for row in budgets
                ],
                "emptyMessage": (
                    "No budgets are defined. Create an Azure budget to establish spend guardrails."
                    if _boolean(result.get("no_budgets"), "result.no_budgets")
                    else "No evaluated budget rows were produced."
                ),
            },
        ],
    )


def _optimization_rightsizing(rows):
    result = []
    for index, row in enumerate(_objects(rows, "result.rightsizing.top")):
        prefix = f"result.rightsizing.top[{index}]"
        resource_id = _text(row.get("resourceId"), f"{prefix}.resourceId")
        savings = _decimal(
            row.get("estMonthlySavingsUsd"),
            f"{prefix}.estMonthlySavingsUsd",
            optional=True,
        )
        result.append(
            {
                "resource_id": resource_id,
                "kind": _text(row.get("kind"), f"{prefix}.kind"),
                "action": _text(
                    row.get("recommendedAction"), f"{prefix}.recommendedAction"
                ),
                "savings": savings,
                "validation": _validated(row.get("validated"), f"{prefix}.validated"),
            }
        )
    result.sort(
        key=lambda row: (
            row["savings"] is None,
            -(row["savings"] or Decimal(0)),
            row["resource_id"].casefold(),
            row["resource_id"],
            row["kind"].casefold(),
            row["kind"],
            row["action"].casefold(),
            row["action"],
            row["validation"],
        )
    )
    return result


def build_cost_optimization_model(
    result,
    *,
    refreshed_at,
    scope_summary=None,
    partial=False,
    warnings=None,
    scope_coverage=None,
):
    """Adapt summarize_optimization output to the generic report model."""

    result = _object(result, "result")
    headline = _object(result.get("headline") or {}, "result.headline")
    rightsizing_block = _object(result.get("rightsizing") or {}, "result.rightsizing")
    anomaly_block = _object(result.get("anomalies") or {}, "result.anomalies")
    budget_block = _object(result.get("budgets") or {}, "result.budgets")
    governance = _object(result.get("governance") or {}, "result.governance")
    rightsizing = _optimization_rightsizing(rightsizing_block.get("top"))

    priorities = []
    for index, row in enumerate(_objects(result.get("priorities"), "result.priorities")):
        prefix = f"result.priorities[{index}]"
        rank = _integer(row.get("rank"), f"{prefix}.rank", default=index + 1)
        impact = _decimal(row.get("impact_usd"), f"{prefix}.impact_usd", optional=True)
        priorities.append(
            {
                "rank": rank,
                "category": _text(row.get("category"), f"{prefix}.category"),
                "impact_type": _text(
                    row.get("impact_type"), f"{prefix}.impact_type"
                ),
                "impact": impact,
                "title": _text(row.get("title"), f"{prefix}.title"),
                "detail": _text(row.get("detail"), f"{prefix}.detail"),
                "action": _text(row.get("action"), f"{prefix}.action"),
                "validation": _validated(
                    row.get("validated"), f"{prefix}.validated"
                )
                if row.get("category") == "rightsizing"
                else "",
            }
        )
    priorities.sort(
        key=lambda row: (
            row["rank"],
            row["category"].casefold(),
            row["category"],
            row["title"].casefold(),
            row["title"],
            row["impact_type"].casefold(),
            row["impact_type"],
            _decimal_key(row["impact"]),
            row["detail"].casefold(),
            row["detail"],
            row["action"].casefold(),
            row["action"],
            row["validation"],
        )
    )

    anomalies = []
    for index, row in enumerate(
        _objects(anomaly_block.get("top"), "result.anomalies.top")
    ):
        prefix = f"result.anomalies.top[{index}]"
        impact = _decimal(row.get("impact_usd"), f"{prefix}.impact_usd", optional=True)
        anomalies.append(
            {
                "kind": _text(row.get("kind"), f"{prefix}.kind"),
                "dimension": _text(row.get("dimension"), f"{prefix}.dimension"),
                "value": _text(row.get("value"), f"{prefix}.value"),
                "current": _decimal(
                    row.get("current_usd"), f"{prefix}.current_usd", optional=True
                ),
                "baseline": _decimal(
                    row.get("baseline_mean_usd"),
                    f"{prefix}.baseline_mean_usd",
                    optional=True,
                ),
                "impact": impact,
            }
        )
    anomalies.sort(
        key=lambda row: (
            row["impact"] is None,
            -(row["impact"] or Decimal(0)),
            row["dimension"].casefold(),
            row["dimension"],
            row["value"].casefold(),
            row["value"],
            row["kind"].casefold(),
            row["kind"],
            _decimal_key(row["current"]),
            _decimal_key(row["baseline"]),
        )
    )

    budget_rows = []
    has_run_rate = False
    for bucket in ("over", "forecast_over", "at_risk"):
        for index, row in enumerate(
            _objects(budget_block.get(bucket), f"result.budgets.{bucket}")
        ):
            prefix = f"result.budgets.{bucket}[{index}]"
            source = _text(row.get("forecast_source"), f"{prefix}.forecast_source")
            has_run_rate = has_run_rate or source == "run-rate"
            budget_rows.append(
                {
                    "name": _text(row.get("name"), f"{prefix}.name"),
                    "status": _text(row.get("status"), f"{prefix}.status"),
                    "currency": _text(
                        row.get("currency"), f"{prefix}.currency", default="USD"
                    )
                    or "USD",
                    "current": _decimal(
                        row.get("current_spend"), f"{prefix}.current_spend", optional=True
                    ),
                    "amount": _decimal(
                        row.get("amount"), f"{prefix}.amount", optional=True
                    ),
                    "forecast": _decimal(
                        row.get("forecast_spend"),
                        f"{prefix}.forecast_spend",
                        optional=True,
                    ),
                    "forecast_source": source,
                }
            )
    budget_rows.sort(
        key=lambda row: (
            _STATUS_ORDER.get(row["status"], 99),
            row["name"].casefold(),
            row["name"],
            row["currency"],
            _decimal_key(row["current"]),
            _decimal_key(row["amount"]),
            _decimal_key(row["forecast"]),
            row["forecast_source"],
        )
    )

    governance_rows = []
    for index, row in enumerate(
        _objects(governance.get("untagged_top"), "result.governance.untagged_top")
    ):
        rid = _text(row.get("resourceId"), f"result.governance.untagged_top[{index}].resourceId")
        cost = _decimal(
            row.get("monthly_usd"),
            f"result.governance.untagged_top[{index}].monthly_usd",
            optional=True,
        )
        governance_rows.append(("untagged resource", rid, _money(cost), "", "Add ownership tags."))
    for index, row in enumerate(
        _objects(governance.get("tag_hygiene"), "result.governance.tag_hygiene")
    ):
        canonical = _text(
            row.get("canonical"), f"result.governance.tag_hygiene[{index}].canonical"
        )
        variants = _object(
            row.get("variants") or {},
            f"result.governance.tag_hygiene[{index}].variants",
        )
        variant_names = []
        for name in variants:
            variant_names.append(
                _text(name, f"result.governance.tag_hygiene[{index}].variants key")
            )
        affected = _decimal(
            row.get("cost_affected"),
            f"result.governance.tag_hygiene[{index}].cost_affected",
            optional=True,
        )
        governance_rows.append(
            (
                "tag hygiene",
                canonical,
                _money(affected),
                ", ".join(_stable_strings(variant_names)),
                "Consolidate tag values.",
            )
        )
    for index, row in enumerate(
        _objects(governance.get("budget_gates"), "result.governance.budget_gates")
    ):
        name = _text(row.get("name"), f"result.governance.budget_gates[{index}].name")
        status = _text(
            row.get("status"), f"result.governance.budget_gates[{index}].status"
        )
        overrun = _decimal(
            row.get("overrun_usd"),
            f"result.governance.budget_gates[{index}].overrun_usd",
            optional=True,
        )
        governance_rows.append(("budget gate", name, _money(overrun), status, "Review gated spend."))
    governance_rows.sort(
        key=lambda row: (
            row[0].casefold(),
            row[0],
            row[1].casefold(),
            row[1],
            row[2],
        )
    )

    context = _context(
        result,
        refreshed_at=refreshed_at,
        scope_summary=scope_summary,
        partial=partial,
        warnings=warnings,
        scope_coverage=scope_coverage,
        extra_warnings=(
            ["Run-rate budget forecasts are estimates."] if has_run_rate else []
        ),
    )
    chart_rows = [row for row in rightsizing if row["savings"] is not None][:10]
    monthly_spend = _decimal(
        headline.get("total_monthly_spend"),
        "result.headline.total_monthly_spend",
        optional=True,
    )
    potential_savings = _decimal(
        headline.get("potential_monthly_savings"),
        "result.headline.potential_monthly_savings",
    )
    anomaly_count = _integer(
        headline.get("anomaly_count"), "result.headline.anomaly_count"
    )
    budgets_over = _integer(
        headline.get("budgets_over"), "result.headline.budgets_over"
    )
    budgets_forecast = _integer(
        headline.get("budgets_forecast_over"),
        "result.headline.budgets_forecast_over",
    )
    untagged = _decimal(
        headline.get("untagged_usd"), "result.headline.untagged_usd"
    )
    spend_period = headline.get("spend_period") if isinstance(headline.get("spend_period"), dict) else {}
    spend_detail = (
        f"{_text(spend_period.get('from'), 'result.headline.spend_period.from')} to "
        f"{_text(spend_period.get('to'), 'result.headline.spend_period.to')}"
        if spend_period.get("from") and spend_period.get("to")
        else "Usage window analyzed"
    )
    extra_metrics = []
    extra_sections = []
    if "waste" in result:
        metric, section = _optimization_waste(result)
        extra_metrics.append(metric)
        extra_sections.append(section)
    if "commitments" in result:
        metrics, section = _optimization_commitments(result)
        extra_metrics.extend(metrics)
        extra_sections.append(section)
    if "sre_agents" in result:
        metric, sections = _sre_agent_rollup_sections(result["sre_agents"], "result.sre_agents")
        extra_metrics.append(metric)
        extra_sections.extend(sections)

    return _model(
        "FinOps: Cost Optimization",
        "Weekly executive rollup; cost data can lag by roughly one day.",
        context,
        [
            {"label": "Spend analyzed", "value": _money(monthly_spend), "detail": spend_detail},
            {
                "label": "Potential monthly savings",
                "value": _money(potential_savings),
                "detail": "Rightsizing only",
                "tone": "good",
            },
            {"label": "Anomalies", "value": str(anomaly_count), "detail": "Spikes, drops, and new spend"},
            {
                "label": "Budget gates",
                "value": str(budgets_over + budgets_forecast),
                "detail": f"{budgets_over} over, {budgets_forecast} forecast over",
                "tone": "critical" if budgets_over + budgets_forecast else "good",
            },
            {"label": "Untagged spend", "value": _money(untagged), "detail": "Governance exposure"},
        ]
        + extra_metrics,
        {
            "type": "bar",
            "label": "Estimated monthly savings (USD)",
            "labels": [_short_resource(row["resource_id"]) for row in chart_rows],
            "values": [_chart_number(row["savings"]) for row in chart_rows],
        },
        [
            {
                "title": "Top priorities",
                "description": "Impact types remain separate; values are not added together.",
                "columns": [
                    "Rank",
                    "Category",
                    "Impact type",
                    "Impact",
                    "Title",
                    "Detail",
                    "Action",
                    "Validation",
                ],
                "rows": [
                    [
                        str(row["rank"]),
                        row["category"],
                        row["impact_type"],
                        _money(row["impact"]),
                        row["title"],
                        row["detail"],
                        row["action"],
                        row["validation"],
                    ]
                    for row in priorities
                ],
                "emptyMessage": "No blended priorities were produced.",
            },
            {
                "title": "Rightsizing",
                "description": "Savings opportunities from the rightsizing analysis.",
                "columns": ["Resource", "Kind", "Action", "Savings", "Validation"],
                "rows": [
                    [
                        _display_resource(row["resource_id"]),
                        row["kind"],
                        row["action"],
                        _money(row["savings"]),
                        row["validation"],
                    ]
                    for row in rightsizing
                ],
                "emptyMessage": "No rightsizing detail was produced.",
            },
            {
                "title": "Cost anomalies",
                "description": "Anomalies ranked by dollar impact.",
                "columns": ["Kind", "Dimension", "Value", "Current", "Baseline", "Impact"],
                "rows": [
                    [
                        row["kind"],
                        row["dimension"],
                        _display_resource(row["value"]),
                        _money(row["current"]),
                        _money(row["baseline"]),
                        _money(row["impact"]),
                    ]
                    for row in anomalies
                ],
                "emptyMessage": "No cost anomalies were produced.",
            },
            {
                "title": "Budget status",
                "description": "Over-budget, forecast-over, and at-risk budgets.",
                "columns": ["Budget", "Status", "Current", "Amount", "Forecast", "Forecast source"],
                "rows": [
                    [
                        row["name"],
                        row["status"],
                        _money(row["current"], row["currency"]),
                        _money(row["amount"], row["currency"]),
                        _money(row["forecast"], row["currency"]),
                        row["forecast_source"],
                    ]
                    for row in budget_rows
                ],
                "emptyMessage": "No budget exceptions were produced.",
            },
            {
                "title": "Governance and policy",
                "description": "Tagging and budget-gate findings; exposure is not claimed as savings.",
                "columns": ["Type", "Target", "Impact", "Detail", "Action"],
                "rows": [list(row) for row in governance_rows],
                "emptyMessage": "No governance findings were produced.",
            },
        ]
        + extra_sections,
    )


def _sre_agent_rollup_sections(block, path):
    """SRE Agent (AAU) metric + sections shared by the Cost Optimization and Monthly Review models.

    `block` is analyze_sre_agent_costs() output or the summarize_optimization `sre_agents` rollup.
    """

    block = _object(block or {}, path)
    currency = _text(block.get("currency"), f"{path}.currency", default="USD") or "USD"
    total = _decimal(block.get("total_usd"), f"{path}.total_usd", optional=True)
    always_on_pct = _decimal(block.get("always_on_pct"), f"{path}.always_on_pct", optional=True)
    savings = _decimal(
        block.get("potential_monthly_savings"), f"{path}.potential_monthly_savings", optional=True
    )

    agents = []
    for index, row in enumerate(_objects(block.get("by_agent"), f"{path}.by_agent")):
        prefix = f"{path}.by_agent[{index}]"
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd")
        projected = _decimal(
            row.get("projected_limit_pct"), f"{prefix}.projected_limit_pct", optional=True
        )
        agents.append(
            (
                _text(row.get("agentName"), f"{prefix}.agentName"),
                _text(row.get("status"), f"{prefix}.status"),
                cost,
                _decimal(row.get("always_on_usd"), f"{prefix}.always_on_usd", optional=True),
                _decimal(row.get("active_usd"), f"{prefix}.active_usd", optional=True),
                projected,
                _text(row.get("resourceId"), f"{prefix}.resourceId"),
            )
        )
    agents.sort(key=lambda r: (-r[2], r[0].casefold(), r[0], r[6].casefold(), r[6]))

    hints = []
    for index, row in enumerate(_objects(block.get("hints"), f"{path}.hints")):
        prefix = f"{path}.hints[{index}]"
        hint_type = _text(row.get("type"), f"{prefix}.type")
        hints.append(
            (
                hint_type,
                _text(row.get("target"), f"{prefix}.target"),
                _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd", optional=True),
                _text(row.get("detail"), f"{prefix}.detail"),
                "verify first" if hint_type in _VERIFY_HINTS else "review",
            )
        )
    hints.sort(
        key=lambda r: (
            r[2] is None, -(r[2] or Decimal(0)), r[0].casefold(), r[0], r[1].casefold(), r[1], r[3]
        )
    )

    detail = f"{_percent(always_on_pct)} always-on flow"
    if savings is not None:
        detail += f"; {_money(savings, currency)}/month removable"
    metric = {"label": "SRE Agent spend", "value": _money(total, currency), "detail": detail}
    sections = [
        {
            "title": "SRE Agent spend",
            "description": (
                "Azure Agent Unit (AAU) cost per SRE Agent. Always-on flow is billed until the "
                "agent is deleted; stopping does not end it. See the FinOps: SRE Agent Spend report."
            ),
            "columns": ["Agent", "Status", "Spend", "Always-on", "Active", "Projected limit use"],
            "rows": [
                [
                    r[0],
                    r[1],
                    _money(r[2], currency),
                    _money(r[3], currency),
                    _money(r[4], currency),
                    _percent(r[5]),
                ]
                for r in agents
            ],
            "emptyMessage": "No SRE Agent spend was found in the managed scopes.",
        },
        {
            "title": "SRE Agent findings",
            "description": "Read-only hints; delete or consolidate agents only after owner review.",
            "columns": ["Type", "Target", "Monthly impact", "Detail", "Action"],
            "rows": [[r[0], r[1], _money(r[2], currency), r[3], r[4]] for r in hints],
            "emptyMessage": "No SRE Agent findings were produced.",
        },
    ]
    return metric, sections


def _optimization_waste(result):
    block = _object(result["waste"] or {}, "result.waste")
    currency = _text(block.get("currency"), "result.waste.currency", default="USD") or "USD"
    savings = _decimal(
        block.get("potential_monthly_savings"),
        "result.waste.potential_monthly_savings",
        optional=True,
    )
    rows = []
    for index, row in enumerate(_objects(block.get("top"), "result.waste.top")):
        prefix = f"result.waste.top[{index}]"
        rows.append(
            [
                _text(row.get("resourceId"), f"{prefix}.resourceId"),
                _text(row.get("kind"), f"{prefix}.kind"),
                _text(row.get("reason"), f"{prefix}.reason"),
                _money(
                    _decimal(row.get("monthlyCost"), f"{prefix}.monthlyCost", optional=True),
                    currency,
                ),
                _money(
                    _decimal(
                        row.get("estMonthlySavings"),
                        f"{prefix}.estMonthlySavings",
                        optional=True,
                    ),
                    currency,
                ),
                _text(row.get("recommendedAction"), f"{prefix}.recommendedAction"),
            ]
        )
    metric = {
        "label": "Waste cleanup savings",
        "value": _money(savings, currency),
        "detail": "Orphaned, idle, and stale resources",
        "tone": "good",
    }
    section = {
        "title": "Waste cleanup",
        "description": (
            "Orphaned, idle, and stale resources not already listed under rightsizing; "
            "confirm with the owner before deleting."
        ),
        "columns": ["Resource", "Kind", "Reason", "Monthly cost", "Savings", "Action"],
        "rows": rows,
        "emptyMessage": "No waste findings were produced.",
    }
    return metric, section


def _optimization_commitments(result):
    block = _object(result["commitments"] or {}, "result.commitments")
    currency = (
        _text(block.get("currency"), "result.commitments.currency", default="USD") or "USD"
    )
    savings = _decimal(
        block.get("potential_monthly_savings"),
        "result.commitments.potential_monthly_savings",
        optional=True,
    )
    coverage = _decimal(
        block.get("coverage_pct"), "result.commitments.coverage_pct", optional=True
    )
    unused = _decimal(
        block.get("unused_commitment_cost"),
        "result.commitments.unused_commitment_cost",
        optional=True,
    )
    best = _text(block.get("best_family"), "result.commitments.best_family")
    rows = []
    for index, row in enumerate(_objects(block.get("purchases"), "result.commitments.purchases")):
        prefix = f"result.commitments.purchases[{index}]"
        family = _text(row.get("family"), f"{prefix}.family")
        rows.append(
            [
                "buy" + (" (headline)" if family == best else " (alternative)"),
                f"{family.replace('_', ' ')} {_text(row.get('term'), f'{prefix}.term')}",
                _text(row.get("sku"), f"{prefix}.sku"),
                _money(
                    _decimal(
                        row.get("est_monthly_savings"),
                        f"{prefix}.est_monthly_savings",
                        optional=True,
                    ),
                    _text(row.get("currency"), f"{prefix}.currency", default=currency)
                    or currency,
                ),
                "Purchase after usage optimization",
            ]
        )
    for index, row in enumerate(
        _objects(block.get("underutilized"), "result.commitments.underutilized")
    ):
        prefix = f"result.commitments.underutilized[{index}]"
        util = _decimal(row.get("utilization"), f"{prefix}.utilization", optional=True)
        rows.append(
            [
                "underutilized",
                _text(row.get("kind"), f"{prefix}.kind").replace("_", " "),
                _text(row.get("name"), f"{prefix}.name"),
                _percent(util),
                _text(row.get("action"), f"{prefix}.action"),
            ]
        )
    for index, row in enumerate(_objects(block.get("expiring"), "result.commitments.expiring")):
        prefix = f"result.commitments.expiring[{index}]"
        days = _integer(row.get("days_to_expiry"), f"{prefix}.days_to_expiry")
        rows.append(
            [
                "expiring",
                _text(row.get("kind"), f"{prefix}.kind").replace("_", " "),
                _text(row.get("name"), f"{prefix}.name"),
                f"{days} days",
                "Decide renewal (auto-renew is off)",
            ]
        )
    for index, row in enumerate(
        _objects(block.get("hybrid_benefit"), "result.commitments.hybrid_benefit")
    ):
        prefix = f"result.commitments.hybrid_benefit[{index}]"
        rows.append(
            [
                "hybrid benefit",
                _text(row.get("license"), f"{prefix}.license"),
                _text(row.get("resourceId"), f"{prefix}.resourceId"),
                _money(
                    _decimal(row.get("monthly_cost"), f"{prefix}.monthly_cost", optional=True),
                    currency,
                ),
                "Verify license entitlement, then enable",
            ]
        )
    metrics = [
        {
            "label": "Rate optimization savings",
            "value": _money(savings, currency),
            "detail": (
                f"Best family: {best.replace('_', ' ')}; alternatives not summed"
                if best
                else "Commitment purchases; alternatives not summed"
            ),
            "tone": "good",
        },
        {
            "label": "Commitment coverage",
            "value": _percent(coverage),
            "detail": f"Unused commitment {_money(unused, currency)}",
        },
    ]
    section = {
        "title": "Rate optimization (commitments)",
        "description": (
            "Reservation and savings plan options cover the same usage and are alternatives; "
            "only the headline family is counted. Optimize usage before committing."
        ),
        "columns": ["Type", "Commitment", "Target", "Value", "Action"],
        "rows": rows,
        "emptyMessage": "No commitment findings were produced.",
    }
    return metrics, section


def build_ai_spend_model(
    result,
    *,
    refreshed_at,
    scope_summary=None,
    partial=False,
    warnings=None,
    scope_coverage=None,
):
    """Adapt attribute_ai_costs output to the generic report model."""

    result = _object(result, "result")
    total = _decimal(result.get("total_ai_usd"), "result.total_ai_usd")
    resource_count = _integer(result.get("resource_count"), "result.resource_count")
    model_count = _integer(result.get("model_count"), "result.model_count")

    meter_rows = []
    token_spend = Decimal(0)
    compute_spend = Decimal(0)
    for index, row in enumerate(
        _objects(result.get("by_meter_type"), "result.by_meter_type")
    ):
        prefix = f"result.by_meter_type[{index}]"
        meter_type = _text(row.get("meter_type"), f"{prefix}.meter_type")
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd")
        pct = _decimal(row.get("pct"), f"{prefix}.pct", optional=True)
        if meter_type == "model_token":
            token_spend += cost
        elif meter_type == "compute":
            compute_spend += cost
        meter_rows.append((meter_type, cost, pct))
    meter_rows.sort(
        key=lambda row: (-row[1], row[0].casefold(), row[0], _decimal_key(row[2]))
    )

    family_rows = []
    for index, row in enumerate(
        _objects(result.get("by_service_family"), "result.by_service_family")
    ):
        prefix = f"result.by_service_family[{index}]"
        name = _text(row.get("service_family"), f"{prefix}.service_family")
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd")
        pct = _decimal(row.get("pct"), f"{prefix}.pct", optional=True)
        family_rows.append((name, cost, pct))
    family_rows.sort(
        key=lambda row: (-row[1], row[0].casefold(), row[0], _decimal_key(row[2]))
    )

    split_rows = []
    for field, key, label in (
        ("by_token_direction", "token_direction", "token direction"),
        ("by_deployment_type", "deployment_type", "deployment type"),
        ("by_project", "project", "Foundry project"),
    ):
        rows = []
        for index, row in enumerate(_objects(result.get(field), f"result.{field}")):
            prefix = f"result.{field}[{index}]"
            name = _text(row.get(key), f"{prefix}.{key}")
            cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd")
            pct = _decimal(row.get("pct"), f"{prefix}.pct", optional=True)
            rows.append((name, cost, pct))
        rows.sort(
            key=lambda row: (-row[1], row[0].casefold(), row[0], _decimal_key(row[2]))
        )
        split_rows.extend(
            [label, name, _money(cost), _percent(pct)] for name, cost, pct in rows
        )

    models = []
    for index, row in enumerate(_objects(result.get("by_model"), "result.by_model")):
        prefix = f"result.by_model[{index}]"
        name = _text(row.get("model"), f"{prefix}.model")
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd")
        pct = _decimal(row.get("pct"), f"{prefix}.pct", optional=True)
        count = _integer(row.get("resource_count"), f"{prefix}.resource_count")
        models.append((name, cost, pct, count))
    models.sort(
        key=lambda row: (
            -row[1],
            row[0].casefold(),
            row[0],
            _decimal_key(row[2]),
            row[3],
        )
    )

    resources = []
    for index, row in enumerate(
        _objects(result.get("by_resource"), "result.by_resource")
    ):
        prefix = f"result.by_resource[{index}]"
        rid = _text(row.get("resourceId"), f"{prefix}.resourceId")
        name = _text(row.get("resourceName"), f"{prefix}.resourceName")
        kind = _text(row.get("kind"), f"{prefix}.kind")
        family = _text(row.get("service_family"), f"{prefix}.service_family")
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd")
        pct = _decimal(row.get("pct"), f"{prefix}.pct", optional=True)
        top_model = _text(row.get("top_model"), f"{prefix}.top_model")
        resources.append((name, rid, kind, family, cost, pct, top_model))
    resources.sort(
        key=lambda row: (
            -row[4],
            row[0].casefold(),
            row[0],
            row[1].casefold(),
            row[1],
            row[2].casefold(),
            row[2],
            row[3].casefold(),
            row[3],
            _decimal_key(row[5]),
            row[6].casefold(),
            row[6],
        )
    )

    drivers = []
    for index, row in enumerate(
        _objects(result.get("top_drivers"), "result.top_drivers")
    ):
        prefix = f"result.top_drivers[{index}]"
        name = _text(row.get("resourceName"), f"{prefix}.resourceName")
        family = _text(row.get("service_family"), f"{prefix}.service_family")
        model = _text(row.get("model"), f"{prefix}.model")
        meter_type = _text(row.get("meter_type"), f"{prefix}.meter_type")
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd")
        drivers.append((name, family, model, meter_type, cost))
    drivers.sort(
        key=lambda row: (
            -row[4],
            row[0].casefold(),
            row[0],
            row[2].casefold(),
            row[2],
            row[3],
            row[1].casefold(),
            row[1],
        )
    )

    hints = []
    for index, row in enumerate(_objects(result.get("hints"), "result.hints")):
        prefix = f"result.hints[{index}]"
        hint_type = _text(row.get("type"), f"{prefix}.type")
        target = _text(row.get("target"), f"{prefix}.target")
        detail = _text(row.get("detail"), f"{prefix}.detail")
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd", optional=True)
        action = "verify first" if hint_type in _VERIFY_HINTS else "review"
        hints.append((hint_type, target, detail, cost, action))
    hints.sort(
        key=lambda row: (
            row[3] is None,
            -(row[3] or Decimal(0)),
            row[0].casefold(),
            row[0],
            row[1].casefold(),
            row[1],
            row[2].casefold(),
            row[2],
            row[4],
        )
    )

    context = _context(
        result,
        refreshed_at=refreshed_at,
        scope_summary=scope_summary,
        partial=partial,
        warnings=warnings,
        scope_coverage=scope_coverage,
    )
    return _model(
        "FinOps: AI Spend",
        "Weekly Azure OpenAI, Foundry, Cognitive Services, and Machine Learning cost snapshot.",
        context,
        [
            {"label": "Total AI spend", "value": _money(total), "detail": "Monthly UsageDetails cost"},
            {"label": "AI resources", "value": str(resource_count), "detail": "Attributed resources"},
            {"label": "Models", "value": str(model_count), "detail": "Token-meter models"},
            {"label": "Model-token spend", "value": _money(token_spend), "detail": "Kept separate from compute"},
            {"label": "Compute spend", "value": _money(compute_spend), "detail": "Endpoints, training, and compute"},
        ],
        {
            "type": "bar",
            "label": "Model-token spend (USD)",
            "labels": [row[0] for row in models[:10]],
            "values": [_chart_number(row[1]) for row in models[:10]],
        },
        [
            {
                "title": "Service and meter split",
                "description": (
                    "AI service families, meter types, and (when billed meters encode them) token "
                    "direction, deployment type, and Foundry project remain separate."
                    if split_rows
                    else "AI service families and meter types remain separate."
                ),
                "columns": ["Breakdown", "Type", "Cost", "Share"],
                "rows": (
                    [["service family", name, _money(cost), _percent(pct)] for name, cost, pct in family_rows]
                    + [["meter type", name, _money(cost), _percent(pct)] for name, cost, pct in meter_rows]
                    + split_rows
                ),
                "emptyMessage": "No AI service or meter breakdown was produced.",
            },
            {
                "title": "By model",
                "description": "Model-token spend ranked by cost.",
                "columns": ["Model", "Cost", "Share", "Resources"],
                "rows": [
                    [name, _money(cost), _percent(pct), str(count)]
                    for name, cost, pct, count in models
                ],
                "emptyMessage": "No model-token spend was found.",
            },
            {
                "title": "By resource",
                "description": "AI resources ranked by attributed cost.",
                "columns": ["Resource", "Resource ID", "Kind", "Service family", "Cost", "Share", "Top model"],
                "rows": [
                    [name, rid, kind, family, _money(cost), _percent(pct), top_model]
                    for name, rid, kind, family, cost, pct, top_model in resources
                ],
                "emptyMessage": "No AI resource spend was found.",
            },
            {
                "title": "Top cost drivers",
                "description": "Largest resource/model/meter combinations.",
                "columns": ["Resource", "Service family", "Model", "Meter type", "Cost"],
                "rows": [
                    [name, family, model, meter_type, _money(cost)]
                    for name, family, model, meter_type, cost in drivers
                ],
                "emptyMessage": "No AI cost drivers were produced.",
            },
            {
                "title": "Where to look first",
                "description": "Read-only optimization hints; marked items require verification.",
                "columns": ["Type", "Target", "Detail", "Cost", "Action"],
                "rows": [
                    [hint_type, target, detail, _money(cost), action]
                    for hint_type, target, detail, cost, action in hints
                ],
                "emptyMessage": (
                    "No AI spend was found in the supplied managed scopes."
                    if total == 0
                    else "No AI optimization hints were produced."
                ),
            },
        ],
    )


def _optional_text(value, field):
    return "—" if value is None else _text(value, field)


def build_sre_agent_spend_model(
    result,
    *,
    refreshed_at,
    scope_summary=None,
    partial=False,
    warnings=None,
    scope_coverage=None,
):
    """Adapt analyze_sre_agent_costs output to the generic report model."""

    result = _object(result, "result")
    currency = _text(result.get("currency"), "result.currency", default="USD") or "USD"
    total = _decimal(result.get("total_usd"), "result.total_usd")
    always_on = _decimal(result.get("always_on_usd"), "result.always_on_usd")
    active = _decimal(result.get("active_usd"), "result.active_usd")
    always_on_pct = _decimal(result.get("always_on_pct"), "result.always_on_pct", optional=True)
    agent_count = _integer(result.get("agent_count"), "result.agent_count")
    inventory_count = _integer(result.get("inventory_count"), "result.inventory_count")
    per_agent = _decimal(
        result.get("always_on_monthly_usd_per_agent"),
        "result.always_on_monthly_usd_per_agent",
        optional=True,
    )
    window_start = _text(result.get("window_start"), "result.window_start")
    window_end = _text(result.get("window_end"), "result.window_end")
    window = f"{window_start} to {window_end}" if window_start and window_end else "No billed days"

    agents = []
    for index, row in enumerate(_objects(result.get("by_agent"), "result.by_agent")):
        prefix = f"result.by_agent[{index}]"
        name = _text(row.get("agentName"), f"{prefix}.agentName")
        rid = _text(row.get("resourceId"), f"{prefix}.resourceId")
        status = _text(row.get("status"), f"{prefix}.status")
        split = _text(row.get("split"), f"{prefix}.split")
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd")
        on = _decimal(row.get("always_on_usd"), f"{prefix}.always_on_usd", optional=True)
        act = _decimal(row.get("active_usd"), f"{prefix}.active_usd", optional=True)
        mtd = _decimal(row.get("mtd_active_aau"), f"{prefix}.mtd_active_aau", optional=True)
        limit = _decimal(row.get("limit_aau"), f"{prefix}.limit_aau", optional=True)
        projected = _decimal(
            row.get("projected_limit_pct"), f"{prefix}.projected_limit_pct", optional=True
        )
        model = _text(row.get("model"), f"{prefix}.model")
        waived = _boolean(row.get("always_on_waived"), f"{prefix}.always_on_waived")
        agents.append(
            (name, rid, status, model, cost, on, act, split, mtd, limit, projected, waived)
        )
    agents.sort(key=lambda row: (-row[4], row[0].casefold(), row[0], row[1].casefold(), row[1]))

    sources = []
    for index, row in enumerate(_objects(result.get("by_source"), "result.by_source")):
        prefix = f"result.by_source[{index}]"
        label = _text(row.get("label"), f"{prefix}.label")
        aau = _decimal(row.get("aau"), f"{prefix}.aau")
        cost = _decimal(row.get("usd"), f"{prefix}.usd")
        pct = _decimal(row.get("pct"), f"{prefix}.pct", optional=True)
        count = _integer(row.get("thread_count"), f"{prefix}.thread_count")
        sources.append((label, aau, cost, pct, count))
    sources.sort(key=lambda row: (-row[1], row[0].casefold(), row[0]))

    threads = []
    for index, row in enumerate(_objects(result.get("top_threads"), "result.top_threads")):
        prefix = f"result.top_threads[{index}]"
        agent = _text(row.get("agentName"), f"{prefix}.agentName")
        thread = _text(row.get("threadId"), f"{prefix}.threadId")
        source = _text(row.get("source"), f"{prefix}.source")
        aau = _decimal(row.get("aau"), f"{prefix}.aau")
        cost = _decimal(row.get("usd"), f"{prefix}.usd", optional=True)
        days = _integer(row.get("days"), f"{prefix}.days")
        last = _optional_text(row.get("last_active"), f"{prefix}.last_active")
        threads.append((agent, thread, source, aau, cost, days, last))
    threads.sort(key=lambda row: (-row[3], row[0].casefold(), row[1]))

    daily = []
    for index, row in enumerate(_objects(result.get("daily"), "result.daily")):
        prefix = f"result.daily[{index}]"
        day = _text(row.get("date"), f"{prefix}.date")
        cost = _decimal(row.get("usd"), f"{prefix}.usd")
        daily.append((day, cost))
    daily.sort()

    hints = []
    for index, row in enumerate(_objects(result.get("hints"), "result.hints")):
        prefix = f"result.hints[{index}]"
        hint_type = _text(row.get("type"), f"{prefix}.type")
        target = _text(row.get("target"), f"{prefix}.target")
        detail = _text(row.get("detail"), f"{prefix}.detail")
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd", optional=True)
        action = "verify first" if hint_type in _VERIFY_HINTS else "review"
        hints.append((hint_type, target, detail, cost, action))
    hints.sort(
        key=lambda row: (
            row[3] is None,
            -(row[3] or Decimal(0)),
            row[0].casefold(),
            row[0],
            row[1].casefold(),
            row[1],
            row[2].casefold(),
            row[2],
        )
    )

    context = _context(
        result,
        refreshed_at=refreshed_at,
        scope_summary=scope_summary,
        partial=partial,
        warnings=warnings,
        scope_coverage=scope_coverage,
    )

    def money(value):
        return _money(value, currency)

    def limit_text(mtd, limit, projected):
        if mtd is None and limit is None:
            return "—"
        used = "—" if mtd is None else f"{mtd.quantize(Decimal('1'), rounding=ROUND_HALF_UP):,}"
        cap = "—" if limit is None else f"{limit.quantize(Decimal('1'), rounding=ROUND_HALF_UP):,}"
        return f"{used} / {cap} AAU (projected {_percent(projected)})"

    return _model(
        "FinOps: SRE Agent Spend",
        "Weekly Azure SRE Agent (Azure Agent Unit) cost snapshot: always-on vs active flow, "
        "active flow by source, and monthly-limit headroom.",
        context,
        [
            {"label": "SRE Agent spend", "value": money(total), "detail": window},
            {
                "label": "Always-on flow",
                "value": money(always_on),
                "detail": f"{_percent(always_on_pct)} of spend; ends only when an agent is deleted",
            },
            {"label": "Active flow", "value": money(active), "detail": "Chats, incidents, tasks, triggers"},
            {
                "label": "Agents billed",
                "value": str(agent_count),
                "detail": f"{inventory_count} in inventory",
            },
            {
                "label": "Always-on per agent",
                "value": money(per_agent),
                "detail": "Monthly fixed cost (4 AAU/hour)",
            },
        ],
        {
            "type": "line",
            "label": f"Daily SRE Agent spend ({currency})",
            "labels": [row[0] for row in daily],
            "values": [_chart_number(row[1]) for row in daily],
        },
        [
            {
                "title": "By agent",
                "description": (
                    "Billed cost split into always-on and active flow. 'estimated' splits assume "
                    "96 AAU/day always-on because no per-thread usage was available."
                ),
                "columns": [
                    "Agent", "Status", "Model", "Cost", "Always-on", "Active", "Split",
                    "MTD active vs limit", "Resource ID",
                ],
                "rows": [
                    [
                        name,
                        status,
                        model or "—",
                        money(cost),
                        money(on) + (" (waived)" if waived else ""),
                        money(act),
                        split,
                        limit_text(mtd, limit, projected),
                        rid,
                    ]
                    for name, rid, status, model, cost, on, act, split, mtd, limit, projected, waived
                    in agents
                ],
                "emptyMessage": "No SRE Agent spend was found in the supplied managed scopes.",
            },
            {
                "title": "Active flow by source",
                "description": "Measured active AAU from each agent's per-thread usage.",
                "columns": ["Source", "AAU", "Cost", "Share", "Threads"],
                "rows": [
                    [label, _number(aau), money(cost), _percent(pct), str(count)]
                    for label, aau, cost, pct, count in sources
                ],
                "emptyMessage": "No per-thread active usage was available.",
            },
            {
                "title": "Top threads",
                "description": "Costliest threads; match thread ids in Settings > Agent consumption.",
                "columns": ["Agent", "Thread", "Source", "AAU", "Cost", "Days", "Last active"],
                "rows": [
                    [agent, thread, source, _number(aau), money(cost), str(days), last]
                    for agent, thread, source, aau, cost, days, last in threads
                ],
                "emptyMessage": "No thread-level usage was available.",
            },
            {
                "title": "Where to look first",
                "description": "Read-only hints; marked items require verification.",
                "columns": ["Type", "Target", "Detail", "Monthly cost", "Action"],
                "rows": [
                    [hint_type, target, detail, money(cost), action]
                    for hint_type, target, detail, cost, action in hints
                ],
                "emptyMessage": (
                    "No SRE Agent spend was found in the supplied managed scopes."
                    if total == 0
                    else "No SRE Agent optimization hints were produced."
                ),
            },
        ],
    )


def build_cost_vs_reliability_model(
    result,
    *,
    refreshed_at,
    scope_summary=None,
    partial=False,
    warnings=None,
    scope_coverage=None,
):
    """Adapt analyze_cost_vs_reliability output to the generic report model."""

    result = _object(result, "result")
    coverage = _object(result.get("coverage") or {}, "result.coverage")
    quality = _object(result.get("data_quality") or {}, "result.data_quality")
    total = _decimal(result.get("total_usd"), "result.total_usd")
    resource_count = _integer(result.get("resource_count"), "result.resource_count")
    signal_count = _integer(
        result.get("reliability_signal_count"), "result.reliability_signal_count"
    )
    cost_count = _integer(
        coverage.get("cost_resource_count"), "result.coverage.cost_resource_count"
    )
    joined_count = _integer(
        coverage.get("joined_resource_count"), "result.coverage.joined_resource_count"
    )
    join_pct = (
        Decimal(joined_count) * Decimal(100) / Decimal(cost_count)
        if cost_count
        else None
    )

    resources = []
    for index, row in enumerate(
        _objects(result.get("by_resource"), "result.by_resource")
    ):
        prefix = f"result.by_resource[{index}]"
        name = _text(row.get("resourceName"), f"{prefix}.resourceName")
        rid = _text(row.get("resourceId"), f"{prefix}.resourceId")
        group = _text(row.get("resourceGroup"), f"{prefix}.resourceGroup")
        service = _text(row.get("service"), f"{prefix}.service")
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd")
        alerts = _integer(row.get("alert_count"), f"{prefix}.alert_count")
        severities = [
            _integer(row.get(f"sev{level}"), f"{prefix}.sev{level}")
            for level in range(5)
        ]
        health = _integer(
            row.get("health_event_count"), f"{prefix}.health_event_count"
        )
        advisor = _integer(
            row.get("advisor_reliability_count"),
            f"{prefix}.advisor_reliability_count",
        )
        score = _decimal(row.get("reliability_score"), f"{prefix}.reliability_score")
        pain = _decimal(
            row.get("pain_per_1000_usd"),
            f"{prefix}.pain_per_1000_usd",
            optional=True,
        )
        risk = _text(row.get("risk_band"), f"{prefix}.risk_band")
        primary = _text(row.get("primary_signal"), f"{prefix}.primary_signal")
        resources.append(
            (
                name,
                rid,
                group,
                service,
                cost,
                alerts,
                severities,
                health,
                advisor,
                score,
                pain,
                risk,
                primary,
            )
        )
    resources.sort(
        key=lambda row: (
            -row[9],
            -row[4],
            row[0].casefold(),
            row[0],
            row[1].casefold(),
            row[1],
            row[2].casefold(),
            row[2],
            row[3].casefold(),
            row[3],
            row[5],
            row[6],
            row[7],
            row[8],
            _decimal_key(row[10]),
            row[11],
            row[12],
        )
    )

    services = []
    for index, row in enumerate(
        _objects(result.get("by_service"), "result.by_service")
    ):
        prefix = f"result.by_service[{index}]"
        service = _text(row.get("service"), f"{prefix}.service")
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd")
        count = _integer(row.get("resource_count"), f"{prefix}.resource_count")
        alerts = _integer(row.get("alert_count"), f"{prefix}.alert_count")
        health = _integer(
            row.get("health_event_count"), f"{prefix}.health_event_count"
        )
        advisor = _integer(
            row.get("advisor_reliability_count"),
            f"{prefix}.advisor_reliability_count",
        )
        score = _decimal(row.get("reliability_score"), f"{prefix}.reliability_score")
        risk = _text(row.get("risk_band"), f"{prefix}.risk_band")
        services.append((service, cost, count, alerts, health, advisor, score, risk))
    services.sort(
        key=lambda row: (
            -row[6],
            -row[1],
            row[0].casefold(),
            row[0],
            row[2],
            row[3],
            row[4],
            row[5],
            row[7],
        )
    )

    hints = []
    for index, row in enumerate(_objects(result.get("hints"), "result.hints")):
        prefix = f"result.hints[{index}]"
        hint_type = _text(row.get("type"), f"{prefix}.type")
        target = _text(row.get("target"), f"{prefix}.target")
        detail = _text(row.get("detail"), f"{prefix}.detail")
        cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd", optional=True)
        score = _decimal(
            row.get("reliability_score"),
            f"{prefix}.reliability_score",
            optional=True,
        )
        hints.append((hint_type, target, detail, cost, score))
    hints.sort(
        key=lambda row: (
            row[4] is None,
            -(row[4] or Decimal(0)),
            row[3] is None,
            -(row[3] or Decimal(0)),
            row[0].casefold(),
            row[0],
            row[1].casefold(),
            row[1],
            row[2].casefold(),
            row[2],
        )
    )
    investment = [row for row in hints if row[0] == "high_incident_low_spend"]
    verify = [row for row in hints if row[0] == "high_spend_no_pain"]

    unmatched = []
    for index, row in enumerate(
        _objects(result.get("unmatched_reliability"), "result.unmatched_reliability")
    ):
        prefix = f"result.unmatched_reliability[{index}]"
        unmatched.append(
            (
                _text(row.get("resourceName"), f"{prefix}.resourceName"),
                _text(row.get("resourceId"), f"{prefix}.resourceId"),
                _text(row.get("signal_type"), f"{prefix}.signal_type"),
                _text(row.get("severity"), f"{prefix}.severity"),
                _decimal(row.get("score"), f"{prefix}.score"),
                _text(row.get("date"), f"{prefix}.date"),
                _text(row.get("detail"), f"{prefix}.detail"),
            )
        )
    unmatched.sort(
        key=lambda row: (
            -row[4],
            row[0].casefold(),
            row[0],
            row[2].casefold(),
            row[2],
            row[5],
            row[1].casefold(),
            row[1],
            row[3].casefold(),
            row[3],
            row[6].casefold(),
            row[6],
        )
    )

    sources = _object(quality.get("sources_used") or {}, "result.data_quality.sources_used")
    source_rows = []
    for name in sorted(sources, key=lambda item: (item.casefold(), item)):
        source_name = _text(name, "result.data_quality.sources_used key")
        used = _boolean(sources[name], f"result.data_quality.sources_used.{name}")
        source_rows.append(["source", source_name, "used" if used else "not available"])
    coverage_rows = [
        ["coverage", "Cost resources", str(cost_count)],
        [
            "coverage",
            "Reliability resources",
            str(
                _integer(
                    coverage.get("reliability_resource_count"),
                    "result.coverage.reliability_resource_count",
                )
            ),
        ],
        ["coverage", "Joined resources", str(joined_count)],
        [
            "coverage",
            "Unmatched reliability signals",
            str(
                _integer(
                    coverage.get("unmatched_reliability_count"),
                    "result.coverage.unmatched_reliability_count",
                )
            ),
        ],
        [
            "coverage",
            "Subscription-level events",
            str(
                _integer(
                    coverage.get("subscription_level_event_count"),
                    "result.coverage.subscription_level_event_count",
                )
            ),
        ],
    ]
    limitations = _stable_strings(
        _strings(quality.get("limitations"), "result.data_quality.limitations")
    )
    quality_rows = coverage_rows + source_rows + [
        ["limitation", str(index), limitation]
        for index, limitation in enumerate(limitations, start=1)
    ]

    generated = [
        "Cost data can lag by roughly one day.",
        "Weighted alert counts are not a complete incident system or duration measure.",
    ]
    cost_partial = _boolean(
        quality.get("cost_partial"), "result.data_quality.cost_partial"
    )
    context = _context(
        result,
        refreshed_at=refreshed_at,
        scope_summary=scope_summary,
        partial=partial,
        warnings=warnings,
        scope_coverage=scope_coverage,
        extra_partial=cost_partial,
        extra_warnings=generated,
    )
    chart_rows = resources[:10]
    rg_name = _case_canonicalizer(row[2] for row in resources)
    service_name = _case_canonicalizer(row[3] for row in resources)
    return _model(
        "FinOps: Cost vs Reliability",
        "Weekly view of Azure spend against transparent weighted reliability signals.",
        context,
        [
            {"label": "Monthly spend", "value": _money(total), "detail": "UsageDetails cost"},
            {"label": "Costed resources", "value": str(resource_count), "detail": "UsageDetails resources"},
            {"label": "Reliability signals", "value": str(signal_count), "detail": "Alerts, health, and Advisor HA"},
            {
                "label": "Join coverage",
                "value": _percent(join_pct),
                "detail": f"{joined_count} of {cost_count} costed resources",
                "tone": "warning" if join_pct is not None and join_pct < 100 else "good",
            },
        ],
        {
            "type": "bar",
            "label": "Reliability score",
            "labels": [f"{row[0]} ({_money(row[4])})" for row in chart_rows],
            "values": [_chart_number(row[9]) for row in chart_rows],
        },
        [
            {
                "title": "Spend and pain",
                "description": "Resources ranked by reliability score, then monthly cost.",
                "columns": [
                    "Resource",
                    "Resource group",
                    "Service",
                    "Monthly cost",
                    "Alerts",
                    "Sev0/1/2/3/4",
                    "Health events",
                    "Advisor HA",
                    "Reliability score",
                    "Pain per $1K",
                    "Risk",
                    "Primary signal",
                ],
                "rows": [
                    [
                        row[0],
                        rg_name(row[2]),
                        service_name(row[3]),
                        _money(row[4]),
                        str(row[5]),
                        "/".join(str(value) for value in row[6]),
                        str(row[7]),
                        str(row[8]),
                        _number(row[9]),
                        _number(row[10]),
                        row[11],
                        row[12],
                    ]
                    for row in resources
                ],
                "emptyMessage": "No costed resource rows were produced.",
            },
            {
                "title": "Service rollup",
                "description": "Cost and reliability signals aggregated by service.",
                "columns": ["Service", "Cost", "Resources", "Alerts", "Health events", "Advisor HA", "Score", "Risk"],
                "rows": [
                    [
                        row[0],
                        _money(row[1]),
                        str(row[2]),
                        str(row[3]),
                        str(row[4]),
                        str(row[5]),
                        _number(row[6]),
                        row[7],
                    ]
                    for row in services
                ],
                "emptyMessage": "No service reliability rollup was produced.",
            },
            {
                "title": "Reliability investment candidates",
                "description": "High-pain, low-spend resources where resilience may matter more than cost cutting.",
                "columns": ["Type", "Target", "Detail", "Monthly cost", "Score"],
                "rows": [
                    [row[0], row[1], row[2], _money(row[3]), _number(row[4])]
                    for row in investment
                ],
                "emptyMessage": "No high-pain, low-spend investment candidates were produced.",
            },
            {
                "title": "Verify before cutting",
                "description": "High-spend resources with no pain in the available signals.",
                "columns": ["Type", "Target", "Detail", "Monthly cost", "Score"],
                "rows": [
                    [row[0], row[1], row[2], _money(row[3]), _number(row[4])]
                    for row in verify
                ],
                "emptyMessage": "No high-spend/no-pain verification candidates were produced.",
            },
            {
                "title": "Unmatched reliability signals",
                "description": "Signals that did not join to a UsageDetails resource ID.",
                "columns": ["Resource", "Resource ID", "Signal", "Severity", "Score", "Date", "Detail"],
                "rows": [
                    [row[0], row[1], row[2], row[3], _number(row[4]), row[5], row[6]]
                    for row in unmatched
                ],
                "emptyMessage": "All resource-level reliability signals joined to cost.",
            },
            {
                "title": "Data quality",
                "description": "Coverage, source availability, and known limitations.",
                "columns": ["Type", "Item", "Value"],
                "rows": quality_rows,
                "emptyMessage": "No data-quality details were supplied.",
            },
        ],
    )


_KPI_STATUS_ORDER = {"fail": 0, "warn": 1, "unknown": 2, "pass": 3}
_MATURITY_TONE = {"run": "good", "walk": "warning", "crawl": "critical"}
_FORECAST_TONE = {"forecast_over": "critical", "at_risk": "warning", "on_track": "good"}
# Must match finops-kpi-scorecard scorecard.FORECAST_RECORD_PREFIX (parse_forecast_record).
FORECAST_RECORD_PREFIX = "finops-forecast-record:v1"
_MOVER_BUCKETS = ("new", "increased", "decreased", "removed")


def _kpi_value(value, unit, field):
    number = _decimal(value, field, optional=True)
    if number is None:
        return "—"
    if unit == "%":
        return _percent(number)
    if unit.casefold() in ("usd", "$"):
        return _money(number)
    return _number(number)


def _detail_text(detail, field):
    if detail is None:
        return ""
    if isinstance(detail, str):
        return detail
    if isinstance(detail, bool):
        return "yes" if detail else "no"
    if isinstance(detail, list):
        return ", ".join(_stable_strings(_strings(detail, field)))
    if isinstance(detail, dict):
        keys = sorted(detail, key=lambda key: (str(key).casefold(), str(key)))
        return "; ".join(
            f"{key}: {_detail_text(detail[key], f'{field}.{key}')}" for key in keys
        )
    return _number(_decimal(detail, field))


def _optional_object(value, field):
    return None if value is None else _object(value, field)


def build_monthly_review_model(
    review,
    *,
    refreshed_at,
    scope_summary=None,
    partial=False,
    warnings=None,
    scope_coverage=None,
):
    """Adapt the monthly FinOps review to the generic report model.

    `review` bundles the deterministic finops-kpi-scorecard and finops-cost-allocation outputs:
    {"month": "YYYY-MM", "scorecard": kpi_scorecard(...), "forecast": forecast_spend(...),
     "month_over_month": month_over_month(...), "routing": route_to_owners(...),
     "unit_economics": unit_economics(...) or a list of them,
     "sre_agents": analyze_sre_agent_costs(...) or None}. Only `scorecard` is required.
    """

    review = _object(review, "review")
    month = _text(review.get("month"), "review.month")
    scorecard = _object(review.get("scorecard") or {}, "review.scorecard")
    forecast = _optional_object(review.get("forecast"), "review.forecast")
    mom = _optional_object(review.get("month_over_month"), "review.month_over_month")
    routing = _optional_object(review.get("routing"), "review.routing")
    unit_input = review.get("unit_economics")
    if isinstance(unit_input, dict):
        unit_input = [unit_input]
    generated = []

    kpis = []
    for index, row in enumerate(_objects(scorecard.get("kpis"), "review.scorecard.kpis")):
        prefix = f"review.scorecard.kpis[{index}]"
        unit = _text(row.get("unit"), f"{prefix}.unit")
        status = _text(row.get("status"), f"{prefix}.status", default="unknown") or "unknown"
        higher = _boolean(row.get("higher_is_better"), f"{prefix}.higher_is_better", default=True)
        target = _kpi_value(row.get("target"), unit, f"{prefix}.target")
        if target != "—":
            target = ("≥ " if higher else "≤ ") + target
        kpis.append(
            (
                _KPI_STATUS_ORDER.get(status, 99),
                _text(row.get("capability"), f"{prefix}.capability"),
                _text(row.get("name"), f"{prefix}.name"),
                _kpi_value(row.get("value"), unit, f"{prefix}.value"),
                target,
                status,
                _text(row.get("action"), f"{prefix}.action"),
                _detail_text(row.get("detail"), f"{prefix}.detail"),
                _text(row.get("id"), f"{prefix}.id"),
            )
        )
    kpis.sort(
        key=lambda r: (
            r[0], r[1].casefold(), r[1], r[2].casefold(), r[2], r[8], r[3], r[4], r[6], r[7]
        )
    )
    counts = {status: sum(1 for row in kpis if row[5] == status) for status in _KPI_STATUS_ORDER}
    known = len(kpis) - counts["unknown"]
    score = _decimal(scorecard.get("score_pct"), "review.scorecard.score_pct", optional=True)
    maturity = _text(scorecard.get("maturity"), "review.scorecard.maturity", default="unknown")

    actions = []
    for index, row in enumerate(_objects(scorecard.get("actions"), "review.scorecard.actions")):
        prefix = f"review.scorecard.actions[{index}]"
        status = _text(row.get("status"), f"{prefix}.status")
        actions.append(
            (
                _KPI_STATUS_ORDER.get(status, 99),
                status,
                _text(row.get("kpi"), f"{prefix}.kpi"),
                _text(row.get("action"), f"{prefix}.action"),
            )
        )
    actions.sort(key=lambda r: (r[0], r[2].casefold(), r[2], r[3].casefold(), r[3], r[1]))

    forecast_rows = []
    forecast_record = None
    forecast_metric = {"label": "Month-end forecast", "value": "—", "detail": "Not supplied"}
    if forecast is None:
        generated.append("Forecast was not supplied.")
    elif not _boolean(forecast.get("available"), "review.forecast.available"):
        reason = _text(forecast.get("reason"), "review.forecast.reason", default="unknown reason")
        generated.append(f"Forecast unavailable: {reason}.")
        forecast_metric["detail"] = "Unavailable"
    else:
        def money(name):
            return _money(_decimal(forecast.get(name), f"review.forecast.{name}", optional=True))

        method = _text(forecast.get("method"), "review.forecast.method")
        confidence = _text(forecast.get("confidence"), "review.forecast.confidence")
        days = _integer(forecast.get("days_remaining"), "review.forecast.days_remaining")
        forecast_rows = [
            [
                _text(forecast.get("month"), "review.forecast.month"),
                money("month_end_forecast"),
                money("month_end_low"),
                money("month_end_high"),
                f"{money('month_to_date')} actual to date; {days} days remaining",
            ],
            [
                _text(forecast.get("next_month"), "review.forecast.next_month"),
                money("next_month_forecast"),
                money("next_month_low"),
                money("next_month_high"),
                f"30-day run rate {money('run_rate_30d')}; {method}, {confidence} confidence",
            ],
        ]
        generated.extend(_strings(forecast.get("warnings"), "review.forecast.warnings"))
        record_month = _text(forecast.get("next_month"), "review.forecast.next_month")
        record_value = _decimal(
            forecast.get("next_month_forecast"), "review.forecast.next_month_forecast", optional=True
        )
        record_as_of = _text(forecast.get("as_of"), "review.forecast.as_of")
        if (
            record_value is not None
            and re.fullmatch(r"\d{4}-\d{2}", record_month or "")
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", record_as_of or "")
        ):
            forecast_record = (
                f"{FORECAST_RECORD_PREFIX} month={record_month} "
                f"forecast={float(record_value):.2f} as_of={record_as_of}"
            )
        budget_status = _text(forecast.get("budget_status"), "review.forecast.budget_status")
        budget_pct = _decimal(
            forecast.get("forecast_vs_budget_pct"),
            "review.forecast.forecast_vs_budget_pct",
            optional=True,
        )
        forecast_metric = {
            "label": "Month-end forecast",
            "value": money("month_end_forecast"),
            "detail": (
                f"{_percent(budget_pct)} of {money('budget_amount')} budget ({budget_status})"
                if budget_status
                else "No budget supplied"
            ),
            "tone": _FORECAST_TONE.get(budget_status, "neutral"),
        }

    owner_rows = []
    mover_rows = []
    mom_metric = {"label": "Month-over-month change", "value": "—", "detail": "Not supplied"}
    if mom is None:
        generated.append("Month-over-month variance was not supplied.")
    else:
        delta = _decimal(mom.get("delta_usd"), "review.month_over_month.delta_usd", optional=True)
        delta_pct = _decimal(
            mom.get("delta_pct"), "review.month_over_month.delta_pct", optional=True
        )
        previous = _decimal(
            mom.get("previous_usd"), "review.month_over_month.previous_usd", optional=True
        )
        mom_metric = {
            "label": "Month-over-month change",
            "value": _money(delta),
            "detail": f"{_percent(delta_pct)} vs {_money(previous)} previous month",
        }
        for index, row in enumerate(
            _objects(mom.get("by_owner"), "review.month_over_month.by_owner")
        ):
            prefix = f"review.month_over_month.by_owner[{index}]"
            owner_rows.append(
                (
                    _text(row.get("owner"), f"{prefix}.owner"),
                    _decimal(row.get("previous_usd"), f"{prefix}.previous_usd"),
                    _decimal(row.get("current_usd"), f"{prefix}.current_usd"),
                    _decimal(row.get("delta_usd"), f"{prefix}.delta_usd"),
                    _decimal(row.get("delta_pct"), f"{prefix}.delta_pct", optional=True),
                )
            )
        owner_rows.sort(
            key=lambda r: (-abs(r[3]), r[0].casefold(), r[0], r[1], r[2], _decimal_key(r[4]))
        )
        movers = _object(mom.get("movers") or {}, "review.month_over_month.movers")
        for bucket in _MOVER_BUCKETS:
            for index, row in enumerate(
                _objects(movers.get(bucket), f"review.month_over_month.movers.{bucket}")
            ):
                prefix = f"review.month_over_month.movers.{bucket}[{index}]"
                mover_rows.append(
                    (
                        bucket,
                        _text(row.get("resourceId"), f"{prefix}.resourceId"),
                        _text(row.get("owner"), f"{prefix}.owner"),
                        _decimal(row.get("previous_usd"), f"{prefix}.previous_usd"),
                        _decimal(row.get("current_usd"), f"{prefix}.current_usd"),
                        _decimal(row.get("delta_usd"), f"{prefix}.delta_usd"),
                    )
                )
        mover_rows.sort(
            key=lambda r: (
                -abs(r[5]), r[0], r[1].casefold(), r[1], r[2].casefold(), r[2], r[3], r[4]
            )
        )
        mover_rows = mover_rows[:20]

    unit_rows = []
    for index, row in enumerate(_objects(unit_input, "review.unit_economics")):
        prefix = f"review.unit_economics[{index}]"
        per = _decimal(row.get("per"), f"{prefix}.per", optional=True)
        unit = _text(row.get("unit"), f"{prefix}.unit", default="unit")
        label = f"per {_number(per)} {unit}" if per is not None and per != 1 else f"per {unit}"
        unit_rows.append(
            [
                label,
                _number(_decimal(row.get("cost_per_unit"), f"{prefix}.cost_per_unit", optional=True)),
                _number(
                    _decimal(
                        row.get("previous_cost_per_unit"),
                        f"{prefix}.previous_cost_per_unit",
                        optional=True,
                    )
                ),
                _percent(_decimal(row.get("change_pct"), f"{prefix}.change_pct", optional=True)),
                _text(row.get("trend"), f"{prefix}.trend", default="unknown"),
                _text(row.get("interpretation"), f"{prefix}.interpretation"),
            ]
        )
        generated.extend(_strings(row.get("warnings"), f"{prefix}.warnings"))
    unit_rows.sort(key=lambda r: tuple((cell.casefold(), cell) for cell in r))

    def routed_item(row, prefix):
        amount = _decimal(row.get("amount_usd"), f"{prefix}.amount_usd", optional=True)
        return (
            amount,
            _text(row.get("source"), f"{prefix}.source"),
            _text(row.get("resourceId"), f"{prefix}.resourceId"),
            _text(row.get("action"), f"{prefix}.action"),
        )

    action_rows = []
    routing_metric = {"label": "Owner-routed findings", "value": "—", "detail": "Not supplied"}
    if routing is not None:
        for g_index, group in enumerate(_objects(routing.get("owners"), "review.routing.owners")):
            g_prefix = f"review.routing.owners[{g_index}]"
            owner = _text(group.get("owner"), f"{g_prefix}.owner")
            total = _decimal(group.get("total_usd"), f"{g_prefix}.total_usd")
            for i_index, item in enumerate(_objects(group.get("items"), f"{g_prefix}.items")):
                action_rows.append(
                    (0, -total, owner, *routed_item(item, f"{g_prefix}.items[{i_index}]"))
                )
        for index, item in enumerate(_objects(routing.get("unrouted"), "review.routing.unrouted")):
            action_rows.append(
                (1, Decimal(0), "needs an owner tag", *routed_item(item, f"review.routing.unrouted[{index}]"))
            )
        unrouted_usd = _decimal(routing.get("unrouted_usd"), "review.routing.unrouted_usd")
        routed_pct = _decimal(routing.get("routed_pct"), "review.routing.routed_pct", optional=True)
        routing_metric = {
            "label": "Owner-routed findings",
            "value": _percent(routed_pct),
            "detail": f"{_money(unrouted_usd)} needs an owner tag",
            "tone": "warning" if unrouted_usd > 0 else "good",
        }
    action_rows.sort(
        key=lambda r: (
            r[0], r[1], r[2].casefold(), r[2], r[3] is None, -(r[3] or Decimal(0)),
            r[5].casefold(), r[5], r[4], r[6].casefold(), r[6],
        )
    )

    context = _context(
        review,
        refreshed_at=refreshed_at,
        scope_summary=scope_summary,
        partial=partial,
        warnings=warnings,
        scope_coverage=scope_coverage,
        extra_warnings=generated,
    )
    chart_rows = owner_rows[:12]
    kpi_tone = (
        "critical" if counts["fail"] else "warning" if counts["warn"] else "good" if known else "neutral"
    )
    governance = [row for row in kpis if "governance" in row[1].casefold()]
    practice_sections = []
    if "next_iteration_focus" in scorecard:
        focus = _strings(scorecard.get("next_iteration_focus"), "review.scorecard.next_iteration_focus")
        practice_sections.append(
            {
                "title": "Next FinOps iteration",
                "description": "Capabilities with failing, then warning, KPIs: pick these as the "
                "focus of the next iteration and set 1-3 goals for each.",
                "columns": ["Order", "Capability"],
                "rows": [[str(i + 1), name] for i, name in enumerate(focus)],
                "emptyMessage": "Every scored KPI passes; keep the current focus.",
            }
        )
    if "benchmarks" in scorecard:
        bench_rows = []
        for index, row in enumerate(_objects(scorecard.get("benchmarks"), "review.scorecard.benchmarks")):
            prefix = f"review.scorecard.benchmarks[{index}]"
            unit = _text(row.get("unit"), f"{prefix}.unit")
            shown = _kpi_value(row.get("value"), unit, f"{prefix}.value")
            bench_rows.append([_text(row.get("name"), f"{prefix}.name"), shown])
        practice_sections.append(
            {
                "title": "Benchmarks",
                "description": "Context values without a Microsoft-published target; not scored.",
                "columns": ["Benchmark", "Value"],
                "rows": bench_rows,
                "emptyMessage": "No benchmark inputs were supplied.",
            }
        )
    sre_sections = []
    if review.get("sre_agents") is not None:
        _, sre_sections = _sre_agent_rollup_sections(review["sre_agents"], "review.sre_agents")
    if forecast_record:
        practice_sections.append(
            {
                "title": "Forecast record",
                "description": "Next month's review reads this line back to score forecast "
                "accuracy against the actual cost.",
                "columns": ["Record"],
                "rows": [[forecast_record]],
                "emptyMessage": "No forecast was recorded.",
            }
        )
    return _model(
        "FinOps: Monthly Review",
        f"Monthly FinOps review for {month or 'the closed month'}: KPI scorecard, governance, "
        "forecast, month-over-month variance, and owner actions. Recommend only.",
        context,
        [
            {
                "label": "FinOps score",
                "value": _percent(score),
                "detail": f"Maturity: {maturity}",
                "tone": _MATURITY_TONE.get(maturity, "neutral"),
            },
            {
                "label": "KPIs passing",
                "value": f"{counts['pass']} of {known}",
                "detail": f"{counts['warn']} warn, {counts['fail']} fail, {counts['unknown']} unknown",
                "tone": kpi_tone,
            },
            forecast_metric,
            mom_metric,
            routing_metric,
        ],
        {
            "type": "bar",
            "label": "Month-over-month change by owner (USD)",
            "labels": [row[0] for row in chart_rows],
            "values": [_chart_number(row[3]) for row in chart_rows],
        },
        [
            {
                "title": "KPI scorecard",
                "description": "Failing KPIs first; unknown means the source was not supplied.",
                "columns": ["Capability", "KPI", "Value", "Target", "Status", "Action"],
                "rows": [[r[1], r[2], r[3], r[4], r[5], r[6]] for r in kpis],
                "emptyMessage": "No KPIs were scored.",
            },
            {
                "title": "Governance checks",
                "description": "Tag and budget governance, including uncovered budget scopes.",
                "columns": ["Check", "Status", "Value", "Target", "Detail"],
                "rows": [[r[2], r[5], r[3], r[4], r[7]] for r in governance],
                "emptyMessage": "No governance checks were scored.",
            },
            {
                "title": "Forecast",
                "description": "Linear-trend forecast with an 80% band; AmortizedCost.",
                "columns": ["Month", "Forecast", "Low", "High", "Detail"],
                "rows": forecast_rows,
                "emptyMessage": "No forecast was produced.",
            },
            {
                "title": "Month-over-month by owner",
                "description": "Closed month vs the month before it, by owner tag.",
                "columns": ["Owner", "Previous", "Current", "Change", "Change %"],
                "rows": [
                    [r[0], _money(r[1]), _money(r[2]), _money(r[3]), _percent(r[4])]
                    for r in owner_rows
                ],
                "emptyMessage": "No month-over-month variance was produced.",
            },
            {
                "title": "Top resource movers",
                "description": "Largest absolute resource changes (top 20).",
                "columns": ["Change type", "Resource", "Owner", "Previous", "Current", "Change"],
                "rows": [
                    [r[0], _short_resource(r[1]), r[2], _money(r[3]), _money(r[4]), _money(r[5])]
                    for r in mover_rows
                ],
                "emptyMessage": "No resource moved above the threshold.",
            },
            {
                "title": "Unit economics",
                "description": "Cost per business unit; rising unit cost is the signal to investigate.",
                "columns": ["Unit", "Cost per unit", "Previous", "Change", "Trend", "Interpretation"],
                "rows": unit_rows,
                "emptyMessage": "No unit metric was supplied.",
            },
            {
                "title": "Owner actions",
                "description": "One action list per owner; unrouted items need an owner tag.",
                "columns": ["Owner", "Source", "Resource", "Action", "Est. monthly impact"],
                "rows": [
                    [r[2], r[4], _short_resource(r[5]), r[6], _money(r[3])]
                    for r in action_rows
                ],
                "emptyMessage": "No findings were routed.",
            },
            {
                "title": "Ordered actions",
                "description": "KPI actions, failing first.",
                "columns": ["Status", "KPI", "Action"],
                "rows": [[r[1], r[2], r[3]] for r in actions],
                "emptyMessage": "No KPI actions are open.",
            },
        ]
        + practice_sections
        + sre_sections,
    )


def _yes_no(value, field):
    if value is None:
        return "—"
    return "yes" if _boolean(value, field) else "no"


def _signed_money(value, currency="USD"):
    if value is None:
        return "—"
    text = _money(value, currency)
    return text if value < 0 else "+" + text


def build_cost_anomaly_model(
    result,
    *,
    refreshed_at,
    scope_summary=None,
    partial=False,
    warnings=None,
    scope_coverage=None,
):
    """Adapt detect_anomalies (+ optional summarize_credits and daily totals).

    Accepts the anomaly list itself or ``{"date", "anomalies", "credits", "daily"}`` where
    ``credits`` is the ``summarize_credits`` output and ``daily`` is ``[{date, cost_usd}]``.
    """

    source = result if isinstance(result, dict) else {}
    raw = _field(source, "anomalies", default=[]) if source else result
    anomalies = []
    for index, row in enumerate(_objects(raw, "anomalies")):
        prefix = f"anomalies[{index}]"
        causes = _field(row, "candidate_causes", "causes", default=None)
        if isinstance(causes, list):
            cause = "; ".join(_strings(causes, f"{prefix}.candidate_causes"))
        else:
            cause = _text(_field(row, "cause", "candidate_cause"), f"{prefix}.cause")
        anomalies.append(
            {
                "kind": _text(row.get("kind"), f"{prefix}.kind"),
                "direction": _text(row.get("direction"), f"{prefix}.direction"),
                "dimension": _text(row.get("dimension"), f"{prefix}.dimension"),
                "value": _text(row.get("value"), f"{prefix}.value"),
                "date": _text(row.get("date"), f"{prefix}.date"),
                "current": _decimal(row.get("current_usd"), f"{prefix}.current_usd", optional=True),
                "baseline": _decimal(
                    row.get("baseline_mean_usd"), f"{prefix}.baseline_mean_usd", optional=True
                ),
                "dod": _decimal(row.get("dod_delta_usd"), f"{prefix}.dod_delta_usd", optional=True),
                "wow": _decimal(row.get("wow_delta_usd"), f"{prefix}.wow_delta_usd", optional=True),
                "impact": _decimal(row.get("impact_usd"), f"{prefix}.impact_usd", optional=True),
                "sustained": row.get("sustained"),
                "sustained_text": _yes_no(row.get("sustained"), f"{prefix}.sustained"),
                "cause": cause,
            }
        )
    anomalies.sort(
        key=lambda row: (
            row["impact"] is None,
            -(row["impact"] or Decimal(0)),
            row["dimension"].casefold(),
            row["value"].casefold(),
            row["value"],
            row["kind"],
            row["date"],
        )
    )
    increases = [row for row in anomalies if row["direction"] != "down"]
    drops = [row for row in anomalies if row["direction"] == "down"]
    sustained = sum(row["sustained"] is True for row in anomalies)
    largest = max((row["impact"] for row in increases if row["impact"] is not None), default=None)

    daily = []
    for index, row in enumerate(_objects(_field(source, "daily", default=[]), "daily")):
        daily.append(
            (
                _text(_field(row, "date", "label"), f"daily[{index}].date"),
                _decimal(_field(row, "cost_usd", "costUsd", "cost"), f"daily[{index}].cost_usd"),
            )
        )
    daily.sort()

    credits = _object(_field(source, "credits", default={}) or {}, "credits")
    credit_total = _decimal(
        credits.get("total_credit_usd"), "credits.total_credit_usd", optional=True
    )
    credit_rows = []
    for index, row in enumerate(_objects(credits.get("by_charge_type"), "credits.by_charge_type")):
        credit_rows.append(
            [
                _text(row.get("chargeType"), f"credits.by_charge_type[{index}].chargeType"),
                _money(
                    _decimal(
                        row.get("credit_usd"), f"credits.by_charge_type[{index}].credit_usd"
                    )
                ),
                str(_integer(row.get("row_count"), f"credits.by_charge_type[{index}].row_count")),
            ]
        )

    analyzed = _text(_field(source, "date", "today"), "date") or (
        anomalies[0]["date"] if anomalies else ""
    )
    if daily:
        chart = {
            "type": "line",
            "title": "Daily spend",
            "label": "Daily cost (USD)",
            "labels": [date for date, _ in daily],
            "values": [_chart_number(cost) for _, cost in daily],
        }
    else:
        top = [row for row in anomalies if row["impact"] is not None][:10]
        chart = {
            "type": "bar",
            "title": "Largest anomalies",
            "label": "Impact (USD)",
            "labels": [_short_resource(row["value"]) for row in top],
            "values": [_chart_number(row["impact"]) for row in top],
        }
    context = _context(
        source,
        refreshed_at=refreshed_at,
        scope_summary=scope_summary,
        partial=partial,
        warnings=warnings,
        scope_coverage=scope_coverage,
    )
    return _model(
        "FinOps: Cost Anomalies",
        "Daily anomaly scan of the last complete day; the newest day is still settling and is excluded.",
        context,
        [
            {
                "label": "Day analyzed",
                "value": analyzed or "—",
                "detail": "Last complete billing day",
            },
            {
                "label": "Spikes and new spend",
                "value": str(len(increases)),
                "detail": "Per dimension; one change can appear several times",
                "tone": "critical" if increases else "good",
            },
            {
                "label": "Largest increase",
                "value": _money(largest),
                "detail": "Impact vs baseline or last week",
                "tone": "warning" if largest else "neutral",
            },
            {
                "label": "Drops",
                "value": str(len(drops)),
                "detail": "Savings win or outage — verify",
                "tone": "warning" if drops else "neutral",
            },
            {
                "label": "Sustained",
                "value": str(sustained),
                "detail": "Elevated for 3 complete days",
            },
            {
                "label": "Credits and refunds",
                "value": _money(credit_total),
                "detail": "Excluded from detection",
            },
        ],
        chart,
        [
            {
                "title": "Anomalies",
                "description": (
                    "Ranked by dollar impact. Causes are correlated evidence, not proof; "
                    "the same change can surface by meter, resource group, and resource."
                ),
                "columns": [
                    "Kind",
                    "Dimension",
                    "Value",
                    "Day cost",
                    "Baseline",
                    "vs baseline",
                    "vs last week",
                    "Impact",
                    "Sustained",
                    "Candidate cause",
                ],
                "rows": [
                    [
                        row["kind"],
                        row["dimension"],
                        _display_resource(row["value"]),
                        _money(row["current"]),
                        _money(row["baseline"]),
                        _signed_money(row["dod"]),
                        _signed_money(row["wow"]),
                        _money(row["impact"]),
                        row["sustained_text"],
                        row["cause"] or "No correlated change found",
                    ]
                    for row in anomalies
                ],
                "emptyMessage": "No cost anomalies were detected for the last complete day.",
            },
            {
                "title": "Credits and refunds",
                "description": "Negative-cost rows reported separately so they do not look like drops.",
                "columns": ["Charge type", "Credit", "Rows"],
                "rows": sorted(credit_rows, key=lambda r: (r[0].casefold(), r[0])),
                "emptyMessage": "No credits or refunds in the analyzed window.",
            },
        ],
    )


def build_cost_allocation_model(
    result,
    *,
    refreshed_at,
    scope_summary=None,
    partial=False,
    warnings=None,
    scope_coverage=None,
):
    """Adapt allocate_costs output (optionally with a ``month_over_month`` key) to a showback report."""

    result = _object(result, "result")
    dimension = _text(result.get("dimension"), "result.dimension", default="team") or "team"
    total = _decimal(result.get("total_usd"), "result.total_usd")
    allocated = _decimal(result.get("allocated_usd"), "result.allocated_usd")
    unallocated = _decimal(result.get("unallocated_usd"), "result.unallocated_usd")
    unallocated_pct = _decimal(result.get("unallocated_pct"), "result.unallocated_pct", optional=True)
    untagged = _decimal(result.get("untagged_usd"), "result.untagged_usd")
    undistributed = _decimal(
        result.get("undistributed_usd"), "result.undistributed_usd", optional=True
    ) or Decimal(0)
    inheritance = _object(result.get("inheritance") or {}, "result.inheritance")
    inherited = _decimal(
        inheritance.get("allocated_via_inheritance_usd"),
        "result.inheritance.allocated_via_inheritance_usd",
        optional=True,
    )

    groups = []
    for index, row in enumerate(_objects(result.get("groups"), "result.groups")):
        prefix = f"result.groups[{index}]"
        direct = _decimal(_field(row, "direct_usd", "monthly_usd"), f"{prefix}.direct_usd")
        groups.append(
            {
                "owner": _text(row.get("owner"), f"{prefix}.owner"),
                "direct": direct,
                "shared": _decimal(row.get("shared_usd"), f"{prefix}.shared_usd", optional=True)
                or Decimal(0),
                "loaded": _decimal(
                    row.get("fully_loaded_usd"), f"{prefix}.fully_loaded_usd", optional=True
                )
                or direct,
                "pct": _decimal(row.get("pct"), f"{prefix}.pct", optional=True),
                "count": _integer(row.get("resource_count"), f"{prefix}.resource_count"),
                "inherited": _decimal(
                    row.get("inherited_usd"), f"{prefix}.inherited_usd", optional=True
                ),
            }
        )
    groups.sort(key=lambda g: (-g["loaded"], g["owner"].casefold(), g["owner"]))
    unalloc_block = _object(result.get("unallocated") or {}, "result.unallocated")
    showback = [
        [
            g["owner"],
            _money(g["direct"]),
            _money(g["shared"]),
            _money(g["loaded"]),
            _percent(g["pct"]),
            str(g["count"]),
            _money(g["inherited"]),
        ]
        for g in groups
    ]
    showback.append(
        [
            "(unallocated)",
            _money(unallocated),
            "—",
            _money(unallocated),
            _percent(unallocated_pct),
            str(_integer(unalloc_block.get("resource_count"), "result.unallocated.resource_count")),
            "—",
        ]
    )
    if undistributed:
        showback.append(["(shared, undistributed)", "—", _money(undistributed), _money(undistributed), "—", "—", "—"])
    showback.append(["Total", "—", "—", _money(total), "100.0%", "—", "—"])

    def _resource_rows(rows, field, extra=False):
        out = []
        for index, row in enumerate(_objects(rows, field)):
            prefix = f"{field}[{index}]"
            rid = _text(row.get("resourceId"), f"{prefix}.resourceId")
            cost = _decimal(row.get("monthly_usd"), f"{prefix}.monthly_usd", optional=True)
            line = [rid, _money(cost)]
            if extra:
                owner = _text(row.get("inherited_owner"), f"{prefix}.inherited_owner")
                source = _text(row.get("inherited_from"), f"{prefix}.inherited_from")
                line.append(f"{owner} (from {_short_resource(source)})" if owner else "—")
            out.append((-(cost or Decimal(0)), rid.casefold(), rid, line))
        out.sort(key=lambda r: r[:3])
        return [r[3] for r in out]

    inventory = []
    for index, row in enumerate(_objects(result.get("tag_inventory"), "result.tag_inventory")):
        prefix = f"result.tag_inventory[{index}]"
        cost = _decimal(row.get("cost_usd"), f"{prefix}.cost_usd")
        inventory.append(
            (
                -cost,
                _text(row.get("key"), f"{prefix}.key"),
                str(_integer(row.get("resource_count"), f"{prefix}.resource_count")),
                _money(cost),
                _percent(_decimal(row.get("pct"), f"{prefix}.pct", optional=True)),
            )
        )
    inventory.sort(key=lambda r: (r[0], r[1].casefold(), r[1]))

    coverage_rows = []
    for index, row in enumerate(
        _objects(result.get("recommended_coverage"), "result.recommended_coverage")
    ):
        prefix = f"result.recommended_coverage[{index}]"
        present = _boolean(row.get("present"), f"{prefix}.present")
        coverage_rows.append(
            [
                _text(row.get("key"), f"{prefix}.key"),
                "present" if present else "missing — adopt",
                str(_integer(row.get("resource_count"), f"{prefix}.resource_count")),
                _money(_decimal(row.get("cost_usd"), f"{prefix}.cost_usd", optional=True)),
                _percent(_decimal(row.get("pct"), f"{prefix}.pct", optional=True)),
            ]
        )
    coverage_rows.sort(key=lambda r: (r[1] == "present", r[0].casefold(), r[0]))

    hygiene = []
    for index, row in enumerate(_objects(result.get("tag_hygiene"), "result.tag_hygiene")):
        prefix = f"result.tag_hygiene[{index}]"
        variants = _object(row.get("variants") or {}, f"{prefix}.variants")
        affected = _decimal(row.get("cost_affected"), f"{prefix}.cost_affected", optional=True)
        canonical = _text(row.get("canonical"), f"{prefix}.canonical")
        hygiene.append(
            (
                -(affected or Decimal(0)),
                canonical.casefold(),
                [
                    canonical,
                    ", ".join(_stable_strings([_text(k, f"{prefix}.variants key") for k in variants])),
                    _money(affected),
                ],
            )
        )
    hygiene = [r[2] for r in sorted(hygiene, key=lambda r: (r[0], r[1], r[2]))]

    sections = [
        {
            "title": f"Showback by {dimension}",
            "description": (
                "Direct cost per owner, shared cost redistributed, and the fully loaded total. "
                "Unallocated spend stays visible and is never spread onto owners."
            ),
            "columns": ["Owner", "Direct", "Shared", "Fully loaded", "% of total", "Resources", "Via inheritance"],
            "rows": showback,
            "emptyMessage": "No cost rows were allocated.",
        },
        {
            "title": "Recommended ownership tags",
            "description": "Coverage of the recommended allocation keys; add missing keys through Azure Policy.",
            "columns": ["Tag key", "Status", "Resources", "Cost covered", "% of cost"],
            "rows": coverage_rows,
            "emptyMessage": "No recommended keys were evaluated.",
        },
        {
            "title": "Tag inventory",
            "description": "Every tag key in use, ranked by the cost it covers.",
            "columns": ["Tag key", "Resources", "Cost covered", "% of cost"],
            "rows": [list(r[1:]) for r in inventory],
            "emptyMessage": "No tags were found on any costed resource.",
        },
        {
            "title": "Untagged resources",
            "description": "Resources with no tags at all, ranked by cost — the tagging backlog.",
            "columns": ["Resource", "Monthly cost", "Inherited owner"],
            "rows": _resource_rows(result.get("untagged_resources"), "result.untagged_resources", True),
            "emptyMessage": "Every costed resource has at least one tag.",
        },
        {
            "title": f"Resources without the {dimension} tag",
            "description": "Top unallocated resources; tag them to move their cost into showback.",
            "columns": ["Resource", "Monthly cost"],
            "rows": _resource_rows(unalloc_block.get("resources"), "result.unallocated.resources"),
            "emptyMessage": f"Every costed resource has the {dimension} tag.",
        },
        {
            "title": "Tag hygiene",
            "description": "One owner spelled several ways splits their spend; consolidate the values.",
            "columns": ["Canonical value", "Variants", "Cost affected"],
            "rows": hygiene,
            "emptyMessage": "No inconsistent owner spellings were found.",
        },
    ]

    mom = result.get("month_over_month")
    if mom:
        mom = _object(mom, "result.month_over_month")
        rows = []
        for index, row in enumerate(_objects(mom.get("by_owner"), "result.month_over_month.by_owner")):
            prefix = f"result.month_over_month.by_owner[{index}]"
            owner = _text(row.get("owner"), f"{prefix}.owner")
            delta = _decimal(row.get("delta_usd"), f"{prefix}.delta_usd", optional=True)
            rows.append(
                (
                    -abs(delta or Decimal(0)),
                    owner.casefold(),
                    owner,
                    [
                        owner,
                        _money(_decimal(row.get("previous_usd"), f"{prefix}.previous_usd", optional=True)),
                        _money(_decimal(row.get("current_usd"), f"{prefix}.current_usd", optional=True)),
                        _signed_money(delta),
                        _percent(_decimal(row.get("delta_pct"), f"{prefix}.delta_pct", optional=True)),
                    ],
                )
            )
        rows = [r[3] for r in sorted(rows, key=lambda r: r[:3])]
        sections.append(
            {
                "title": "Month-over-month by owner",
                "description": "Variance per owner between the two analyzed windows.",
                "columns": ["Owner", "Previous", "Current", "Change", "Change %"],
                "rows": rows,
                "emptyMessage": "No month-over-month rows were produced.",
            }
        )

    chart_rows = [(g["owner"], g["loaded"]) for g in groups[:10]]
    if unallocated > 0:
        chart_rows.append(("(unallocated)", unallocated))
    context = _context(
        result,
        refreshed_at=refreshed_at,
        scope_summary=scope_summary,
        partial=partial,
        warnings=warnings,
        scope_coverage=scope_coverage,
    )
    period = _text(
        _field(result, "period", "period_label", default=None), "result.period", default=""
    )
    metrics = [
        {
            "label": "Cost analyzed",
            "value": _money(total),
            "detail": period or "Retrieved UsageDetails window",
        },
        {
            "label": "Allocated",
            "value": _money(allocated),
            "detail": f"{len(groups)} distinct {dimension} tag values",
            "tone": "good",
        },
        {
            "label": "Unallocated",
            "value": _percent(unallocated_pct),
            "detail": f"{_money(unallocated)} without the {dimension} tag",
            "tone": "warning" if unallocated > 0 else "good",
        },
        {
            "label": "Untagged spend",
            "value": _money(untagged),
            "detail": "Resources with no tags at all",
            "tone": "warning" if untagged > 0 else "good",
        },
    ]
    if inheritance.get("enabled"):
        metrics.append(
            {"label": "Via tag inheritance", "value": _money(inherited), "detail": "Resource group or subscription tag"}
        )
    return _model(
        "FinOps: Cost Allocation",
        f"Weekly showback of Azure cost by {dimension} tag with tag coverage and hygiene; recommend-only.",
        context,
        metrics,
        {
            "type": "bar",
            "title": f"Cost by {dimension}",
            "label": "Fully loaded cost (USD)",
            "labels": [name for name, _ in chart_rows],
            "values": [_chart_number(cost) for _, cost in chart_rows],
        },
        sections,
    )


def build_waste_model(
    result,
    *,
    refreshed_at,
    scope_summary=None,
    partial=False,
    warnings=None,
    scope_coverage=None,
):
    """Adapt find_waste output to the generic report model."""

    result = _object(result, "result")
    currency = _text(result.get("currency"), "result.currency", default="USD") or "USD"
    headline = _object(result.get("headline") or {}, "result.headline")
    findings = []
    for index, row in enumerate(_objects(result.get("findings"), "result.findings")):
        prefix = f"result.findings[{index}]"
        findings.append(
            {
                "resource_id": _text(row.get("resourceId"), f"{prefix}.resourceId"),
                "type": _text(row.get("resourceType"), f"{prefix}.resourceType"),
                "kind": _text(row.get("kind"), f"{prefix}.kind"),
                "reason": _text(row.get("reason"), f"{prefix}.reason"),
                "action": _text(row.get("recommendedAction"), f"{prefix}.recommendedAction"),
                "monthly": _decimal(row.get("monthlyCost"), f"{prefix}.monthlyCost", optional=True),
                "savings": _decimal(
                    row.get("estMonthlySavings"), f"{prefix}.estMonthlySavings", optional=True
                ),
                "evidence": " ".join(_strings(row.get("evidence"), f"{prefix}.evidence")),
            }
        )
    findings.sort(
        key=lambda row: (
            row["savings"] is None,
            -(row["savings"] or Decimal(0)),
            row["kind"],
            row["resource_id"].casefold(),
            row["resource_id"],
        )
    )
    actionable = [row for row in findings if row["kind"] in ("orphan", "idle", "stale")]
    savings = _decimal(
        headline.get("potential_monthly_savings"), "result.headline.potential_monthly_savings",
        optional=True,
    )
    if savings is None:
        savings = sum((row["savings"] or Decimal(0) for row in actionable), Decimal(0))
    unknown = sum(row["savings"] is None for row in actionable)
    by_kind = {}
    for row in findings:
        by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
    chart_rows = [row for row in actionable if row["savings"]][:10]
    context = _context(
        result,
        refreshed_at=refreshed_at,
        scope_summary=scope_summary,
        partial=partial,
        warnings=warnings,
        scope_coverage=scope_coverage,
    )
    return _model(
        "FinOps: Waste Cleanup",
        "Weekly scan for orphaned, idle, and stale resources; confirm with the owner before deleting.",
        context,
        [
            {
                "label": "Potential monthly savings",
                "value": _money(savings, currency),
                "detail": "Orphaned, idle, and stale resources",
                "tone": "good",
            },
            {
                "label": "Actionable findings",
                "value": str(len(actionable)),
                "detail": f"{unknown} without cost data" if unknown else "All have cost data",
            },
            {
                "label": "Review items",
                "value": str(len(findings) - len(actionable)),
                "detail": "Configuration or hygiene, no direct saving",
            },
        ],
        {
            "type": "bar",
            "title": "Largest cleanup savings",
            "label": f"Estimated monthly savings ({currency})",
            "labels": [_short_resource(row["resource_id"]) for row in chart_rows],
            "values": [_chart_number(row["savings"]) for row in chart_rows],
        },
        [
            {
                "title": "Waste findings",
                "description": "Ranked by estimated monthly savings; review items carry no claimed saving.",
                "columns": [
                    "Resource",
                    "Resource group",
                    "Type",
                    "Kind",
                    "Reason",
                    "Monthly cost",
                    "Savings",
                    "Action",
                    "Evidence",
                ],
                "rows": [
                    [
                        _short_resource(row["resource_id"]),
                        _resource_group_of(row["resource_id"]),
                        row["type"],
                        row["kind"],
                        row["reason"],
                        _money(row["monthly"], currency),
                        _money(row["savings"], currency),
                        row["action"],
                        row["evidence"],
                    ]
                    for row in findings
                ],
                "emptyMessage": "No orphaned, idle, or stale resources were found.",
            },
            {
                "title": "Findings by kind",
                "description": "orphan = unattached; idle = attached but unused; stale = old snapshots.",
                "columns": ["Kind", "Findings"],
                "rows": [[kind, str(count)] for kind, count in sorted(by_kind.items())],
                "emptyMessage": "No findings.",
            },
        ],
    )


def build_commitment_model(
    result,
    *,
    refreshed_at,
    scope_summary=None,
    partial=False,
    warnings=None,
    scope_coverage=None,
):
    """Adapt analyze_commitments output to the generic report model."""

    result = _object(result, "result")
    currency = _text(result.get("currency"), "result.currency", default="USD") or "USD"
    headline = _object(result.get("headline") or {}, "result.headline")
    coverage = _object(result.get("coverage") or {}, "result.coverage")
    savings = _decimal(
        headline.get("potential_monthly_savings"), "result.headline.potential_monthly_savings",
        optional=True,
    )
    best = _text(headline.get("best_family"), "result.headline.best_family")
    coverage_pct = _decimal(headline.get("coverage_pct"), "result.headline.coverage_pct", optional=True)
    unused = _decimal(
        headline.get("unused_commitment_cost"), "result.headline.unused_commitment_cost",
        optional=True,
    )
    unavailable = _stable_strings(
        _strings(headline.get("inventory_unavailable"), "result.headline.inventory_unavailable")
    )
    count = _integer(headline.get("commitment_count"), "result.headline.commitment_count")

    inventory = []
    for index, row in enumerate(_objects(result.get("inventory"), "result.inventory")):
        prefix = f"result.inventory[{index}]"
        util = _field(row, "utilization_30d", default=None)
        if util is None:
            util = row.get("utilization_7d")
        days = row.get("days_to_expiry")
        inventory.append(
            [
                _text(row.get("kind"), f"{prefix}.kind").replace("_", " "),
                _text(row.get("name"), f"{prefix}.name"),
                _text(row.get("sku"), f"{prefix}.sku"),
                _text(row.get("term"), f"{prefix}.term"),
                _text(row.get("applied_scope_type"), f"{prefix}.applied_scope_type"),
                _percent(_decimal(util, f"{prefix}.utilization", optional=True)),
                _text(row.get("expiry_date"), f"{prefix}.expiry_date") or "—",
                "—" if days is None else f"{_integer(days, f'{prefix}.days_to_expiry')} days",
                _yes_no(row.get("renew"), f"{prefix}.renew"),
            ]
        )
    inventory.sort(key=lambda r: (r[0], r[1].casefold(), r[1], r[2], r[6]))

    purchases = []
    for index, row in enumerate(_objects(result.get("purchases"), "result.purchases")):
        prefix = f"result.purchases[{index}]"
        family = _text(row.get("family"), f"{prefix}.family")
        row_currency = _text(row.get("currency"), f"{prefix}.currency", default=currency) or currency
        alternatives = []
        for alt_index, alt in enumerate(_objects(row.get("alternatives"), f"{prefix}.alternatives")):
            alternatives.append(
                f"{_text(alt.get('term'), f'{prefix}.alternatives[{alt_index}].term')} "
                + _money(
                    _decimal(
                        alt.get("est_monthly_savings"),
                        f"{prefix}.alternatives[{alt_index}].est_monthly_savings",
                        optional=True,
                    ),
                    row_currency,
                )
            )
        est = _decimal(row.get("est_monthly_savings"), f"{prefix}.est_monthly_savings", optional=True)
        purchases.append(
            {
                "family": family,
                "sku": _text(row.get("sku"), f"{prefix}.sku"),
                "est": est,
                "cells": [
                    family.replace("_", " ") + (" (headline)" if family == best else " (alternative)"),
                    _text(row.get("resource_type"), f"{prefix}.resource_type"),
                    _text(row.get("sku"), f"{prefix}.sku"),
                    _text(row.get("location"), f"{prefix}.location") or "—",
                    _text(row.get("term"), f"{prefix}.term"),
                    _text(row.get("lookback"), f"{prefix}.lookback"),
                    _money(est, row_currency),
                    _percent(_decimal(row.get("savings_pct"), f"{prefix}.savings_pct", optional=True)),
                    ", ".join(sorted(alternatives)) or "—",
                ],
            }
        )
    purchases.sort(
        key=lambda r: (-(r["est"] or Decimal(0)), r["family"], r["sku"].casefold(), r["cells"])
    )

    attention = []
    for index, row in enumerate(_objects(result.get("underutilized"), "result.underutilized")):
        prefix = f"result.underutilized[{index}]"
        attention.append(
            [
                "underutilized",
                _text(row.get("name"), f"{prefix}.name"),
                _percent(_decimal(row.get("utilization"), f"{prefix}.utilization", optional=True)),
                _text(row.get("action"), f"{prefix}.action"),
            ]
        )
    for index, row in enumerate(_objects(result.get("expiring"), "result.expiring")):
        prefix = f"result.expiring[{index}]"
        attention.append(
            [
                "expiring, auto-renew off",
                _text(row.get("name"), f"{prefix}.name"),
                f"{_integer(row.get('days_to_expiry'), f'{prefix}.days_to_expiry')} days",
                _text(row.get("action"), f"{prefix}.action"),
            ]
        )
    attention.sort(key=lambda r: (r[0], r[1].casefold(), r[1], r[2]))

    categories = []
    for index, row in enumerate(_objects(coverage.get("by_meter_category"), "result.coverage.by_meter_category")):
        prefix = f"result.coverage.by_meter_category[{index}]"
        on_demand = _decimal(row.get("on_demand_cost"), f"{prefix}.on_demand_cost")
        categories.append(
            (
                _text(row.get("meter_category"), f"{prefix}.meter_category"),
                _decimal(row.get("covered_cost"), f"{prefix}.covered_cost"),
                on_demand,
                _decimal(row.get("coverage_pct"), f"{prefix}.coverage_pct", optional=True),
            )
        )
    categories.sort(key=lambda r: (-r[2], r[0].casefold(), r[0]))

    hub = []
    for index, row in enumerate(_objects(result.get("hybrid_benefit"), "result.hybrid_benefit")):
        prefix = f"result.hybrid_benefit[{index}]"
        cost = _decimal(row.get("monthly_cost"), f"{prefix}.monthly_cost", optional=True)
        rid = _text(row.get("resourceId"), f"{prefix}.resourceId")
        hub.append(
            (
                cost is None,
                -(cost or Decimal(0)),
                rid.casefold(),
                [
                    _display_resource(rid),
                    _text(row.get("license"), f"{prefix}.license"),
                    _text(row.get("current_license_type"), f"{prefix}.current_license_type"),
                    _money(cost, currency),
                    _text(row.get("action"), f"{prefix}.action"),
                ],
            )
        )
    hub = [r[3] for r in sorted(hub, key=lambda r: (r[0], r[1], r[2], r[3]))]

    if purchases:
        chart_rows = [r for r in purchases if r["est"] is not None][:10]
        chart = {
            "type": "bar",
            "title": "Purchase recommendations",
            "label": f"Estimated monthly savings ({currency})",
            "labels": [f"{r['family'].replace('_', ' ')} {r['sku']}".strip() for r in chart_rows],
            "values": [_chart_number(r["est"]) for r in chart_rows],
        }
    else:
        chart = {
            "type": "bar",
            "title": "On-demand cost by meter category",
            "label": f"On-demand cost ({currency})",
            "labels": [r[0] for r in categories],
            "values": [_chart_number(r[2]) for r in categories],
        }
    context = _context(
        result,
        refreshed_at=refreshed_at,
        scope_summary=scope_summary,
        partial=partial,
        warnings=warnings,
        scope_coverage=scope_coverage,
        extra_partial=bool(unavailable),
    )
    return _model(
        "FinOps: Commitments",
        "Weekly rate-optimization view: reservations and savings plans, coverage, and Hybrid Benefit. "
        "Optimize usage before committing.",
        context,
        [
            {
                "label": "Potential monthly savings",
                "value": _money(savings, currency),
                "detail": (
                    f"Headline family: {best.replace('_', ' ')}; alternatives not summed"
                    if best
                    else "No purchase above threshold"
                ),
                "tone": "good",
            },
            {
                "label": "Commitment coverage",
                "value": _percent(coverage_pct),
                "detail": f"{_money(_decimal(coverage.get('covered_cost'), 'result.coverage.covered_cost', optional=True), currency)} covered of eligible usage",
            },
            {
                "label": "Unused commitment",
                "value": _money(unused, currency),
                "detail": "Amortized unused reservation/plan cost",
                "tone": "warning" if unused else "neutral",
            },
            {
                "label": "Active commitments",
                "value": "unknown" if unavailable else str(count),
                "detail": (
                    "Inventory unavailable: " + ", ".join(k.replace("_", " ") for k in unavailable)
                    if unavailable
                    else f"{len(attention)} need attention"
                ),
                "tone": "warning" if unavailable or attention else "neutral",
            },
            {
                "label": "Hybrid Benefit candidates",
                "value": str(len(hub)),
                "detail": "Verify license entitlement first",
            },
        ],
        chart,
        [
            {
                "title": "Purchase recommendations",
                "description": (
                    "Reservations and savings plans cover the same usage, so they are alternatives; "
                    "only the headline family counts toward savings."
                ),
                "columns": ["Family", "Resource type", "SKU", "Location", "Term", "Lookback", "Monthly savings", "Savings %", "Other terms"],
                "rows": [r["cells"] for r in purchases],
                "emptyMessage": "No purchase recommendation clears the savings threshold.",
            },
            {
                "title": "Coverage by meter category",
                "description": "Covered vs on-demand amortized usage cost; on-demand spend is the commitment opportunity.",
                "columns": ["Meter category", "Covered", "On-demand", "Coverage"],
                "rows": [[r[0], _money(r[1], currency), _money(r[2], currency), _percent(r[3])] for r in categories],
                "emptyMessage": "No eligible usage rows were supplied for coverage.",
            },
            {
                "title": "Existing commitments",
                "description": "Active reservations and savings plans with utilization and expiry.",
                "columns": ["Kind", "Name", "SKU", "Term", "Scope", "Utilization", "Expires", "Days left", "Auto-renew"],
                "rows": inventory,
                "emptyMessage": (
                    "Commitment inventory was not readable; grant Reservations Reader and Savings plan Reader."
                    if unavailable
                    else "No active reservations or savings plans in scope."
                ),
            },
            {
                "title": "Needs attention",
                "description": "Underutilized commitments and ones expiring without auto-renew.",
                "columns": ["Issue", "Commitment", "Value", "Action"],
                "rows": attention,
                "emptyMessage": "No underutilized or expiring commitments.",
            },
            {
                "title": "Azure Hybrid Benefit candidates",
                "description": "Pay-as-you-go Windows/SQL licensing; enable only with eligible licenses.",
                "columns": ["Resource", "License", "Current license type", "Monthly cost", "Action"],
                "rows": hub,
                "emptyMessage": "No Hybrid Benefit candidates were found.",
            },
        ],
    )


__all__ = [
    "build_ai_spend_model",
    "build_budget_status_model",
    "build_commitment_model",
    "build_cost_allocation_model",
    "build_cost_anomaly_model",
    "build_waste_model",
    "build_cost_optimization_model",
    "build_cost_overview_model",
    "build_cost_vs_reliability_model",
    "build_monthly_review_model",
    "build_rightsizing_savings_model",
    "build_sre_agent_spend_model",
]
