"""Generate the sample Live Reports shown in the README gallery.

Every number here is fictional ("Contoso" estate, all-zero subscription id). The data is passed
through the same deterministic skill helpers and renderer that the scheduled tasks use, so the
samples look exactly like real reports without exposing any customer data.

    python docs/samples/generate_samples.py          # writes docs/samples/*.html
"""

import importlib.util
import math
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "plugins" / "finops" / "skills"
OUT = Path(__file__).resolve().parent


def _load(name, folder, filename):
    spec = importlib.util.spec_from_file_location(name, SKILLS / folder / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


renderer = _load("sample_render", "finops-report-renderer", "render.py")
models = _load("sample_models", "finops-report-renderer", "models.py")
allocate = _load("sample_allocate", "finops-cost-allocation", "allocate.py")
waste = _load("sample_waste", "finops-waste-finder", "waste.py")
commit = _load("sample_commit", "finops-commitment-advisor", "commit.py")
detect = _load("sample_detect", "finops-cost-anomaly-detection", "detect.py")
summarize = _load("sample_summarize", "finops-cost-optimization-report", "summarize.py")

SUB = "/subscriptions/00000000-0000-0000-0000-000000000000"
AS_OF = date(2026, 10, 23)
START = date(2026, 10, 1)
REFRESHED = "2026-10-23T15:00:00Z"
DAYS = [(START + timedelta(days=i)).isoformat() for i in range((AS_OF - START).days)]


def rid(rg, provider, name):
    return f"{SUB}/resourceGroups/{rg}/providers/{provider}/{name}"


def context(cost_usd, included=1840, excluded=0, unattributed=12):
    return {
        "refreshed_at": REFRESHED,
        "scope_summary": [SUB],
        "partial": False,
        "warnings": [],
        "scope_coverage": [{
            "scope": SUB, "status": "complete", "included": included, "excluded": excluded,
            "unattributed": unattributed, "cost_usd": cost_usd, "partial": False,
            "detail": "Contoso Production (sample data)",
        }],
    }


def daily_curve(base, amplitude):
    rows = []
    for i, day in enumerate(DAYS):
        weekday = (START + timedelta(days=i)).weekday()
        value = base + amplitude * math.sin(i / 3.0) - (base * 0.12 if weekday >= 5 else 0)
        rows.append({"date": day, "cost_usd": round(value, 2)})
    return rows


# --- Shared fictional estate -----------------------------------------------------------------
VM_WEB1 = rid("rg-web-prod", "Microsoft.Compute/virtualMachines", "vm-web-01")
VM_WEB2 = rid("rg-web-prod", "Microsoft.Compute/virtualMachines", "vm-web-02")
VM_BATCH = rid("rg-data-prod", "Microsoft.Compute/virtualMachines", "vm-batch-01")
VM_JUMP = rid("rg-shared-network", "Microsoft.Compute/virtualMachines", "vm-jumpbox-win")
VM_REPORT = rid("rg-finance-prod", "Microsoft.Compute/virtualMachines", "vm-reporting-win")
SQL = rid("rg-data-prod", "Microsoft.Sql/servers", "sql-orders/databases/orders")
AKS = rid("rg-web-prod", "Microsoft.ContainerService/managedClusters", "aks-storefront")
COSMOS = rid("rg-data-prod", "Microsoft.DocumentDB/databaseAccounts", "cosmos-catalog")
AOAI = rid("rg-ai-prod", "Microsoft.CognitiveServices/accounts", "aoai-contoso-eus")
FOUNDRY = rid("rg-ai-prod", "Microsoft.CognitiveServices/accounts", "foundry-assistants")
AGW = rid("rg-web-prod", "Microsoft.Network/applicationGateways", "agw-storefront")
LAW = rid("rg-shared-network", "Microsoft.OperationalInsights/workspaces", "law-contoso-prod")
STORAGE = rid("rg-data-prod", "Microsoft.Storage/storageAccounts", "stcontosolake")
DEV_VM = rid("rg-dev", "Microsoft.Compute/virtualMachines", "vm-dev-sandbox")

COSTS = {
    AKS: 9120.40, SQL: 7480.15, COSMOS: 5210.80, VM_BATCH: 4380.00, AOAI: 3925.60,
    VM_WEB1: 2190.00, VM_WEB2: 2190.00, AGW: 1640.25, LAW: 1515.90, STORAGE: 1288.70,
    FOUNDRY: 1104.35, VM_REPORT: 980.00, VM_JUMP: 412.30, DEV_VM: 655.20,
}
TOTAL = round(sum(COSTS.values()), 2)

TAGS = {
    AKS: {"team": "storefront", "env": "prod", "costCenter": "CC-1001"},
    VM_WEB1: {"team": "storefront", "env": "prod"},
    VM_WEB2: {"Team": "Storefront", "env": "prod"},
    AGW: {"team": "storefront", "env": "prod"},
    SQL: {"team": "data-platform", "env": "prod", "costCenter": "CC-2002"},
    COSMOS: {"team": "data-platform", "env": "prod"},
    VM_BATCH: {"team": "data-platform", "env": "prod"},
    STORAGE: {"team": "data-platform"},
    AOAI: {"team": "ai-assistants", "env": "prod"},
    FOUNDRY: {"team": "ai-assistants", "env": "prod"},
    VM_REPORT: {"team": "finance-apps", "env": "prod"},
}


def overview():
    by_service = [
        ("Virtual Machines", 10807.50), ("Azure Kubernetes Service", 9120.40),
        ("SQL Database", 7480.15), ("Azure Cosmos DB", 5210.80),
        ("Azure OpenAI", 3925.60), ("Application Gateway", 1640.25),
        ("Log Analytics", 1515.90), ("Storage", 1288.70), ("Azure AI Foundry", 1104.35),
    ]
    by_rg = {}
    for key, cost in COSTS.items():
        rg = key.split("/resourceGroups/")[1].split("/")[0]
        by_rg[rg] = by_rg.get(rg, 0) + cost
    daily = daily_curve(TOTAL / len(DAYS), 140)
    scale = TOTAL / sum(r["cost_usd"] for r in daily)
    for row in daily:
        row["cost_usd"] = round(row["cost_usd"] * scale, 2)
    data = {
        "total_usd": TOTAL,
        "daily": daily,
        "by_service": [{"service": s, "cost_usd": c} for s, c in by_service],
        "by_resource_group": [{"resource_group": g, "cost_usd": round(c, 2)}
                              for g, c in by_rg.items()],
    }
    return models.build_cost_overview_model(data, **context(TOTAL))


def rightsizing_findings():
    return [
        {"resourceId": VM_BATCH, "resourceType": "microsoft.compute/virtualmachines",
         "kind": "oversized", "currentSku": "Standard_E16s_v5",
         "recommendedAction": "Resize to Standard_E8s_v5", "currentMonthlyUsd": 4380.00,
         "estMonthlySavingsUsd": 2190.00, "validated": True,
         "evidence": ["p95 CPU 11% over 7 days", "max CPU 23%"],
         "sources": ["advisor", "azure-monitor"]},
        {"resourceId": VM_WEB2, "resourceType": "microsoft.compute/virtualmachines",
         "kind": "oversized", "currentSku": "Standard_D8s_v5",
         "recommendedAction": "Resize to Standard_D4s_v5", "currentMonthlyUsd": 2190.00,
         "estMonthlySavingsUsd": 1095.00, "validated": True,
         "evidence": ["p95 CPU 14% over 7 days"], "sources": ["advisor", "azure-monitor"]},
        {"resourceId": DEV_VM, "resourceType": "microsoft.compute/virtualmachines",
         "kind": "idle", "currentSku": "Standard_D4s_v5",
         "recommendedAction": "Shut down or add an auto-shutdown schedule",
         "currentMonthlyUsd": 655.20, "estMonthlySavingsUsd": 655.20, "validated": True,
         "evidence": ["p95 CPU 1.2% over 7 days", "no network egress"],
         "sources": ["azure-monitor"]},
        {"resourceId": rid("rg-data-prod", "Microsoft.Compute/disks", "disk-batch-scratch-02"),
         "resourceType": "microsoft.compute/disks", "kind": "idle",
         "currentSku": "Premium_LRS", "recommendedAction": "Delete the unattached managed disk.",
         "currentMonthlyUsd": 135.17, "estMonthlySavingsUsd": 135.17, "validated": True,
         "evidence": ["Managed disk is Unattached."], "sources": ["resource-graph"]},
        {"resourceId": VM_JUMP, "resourceType": "microsoft.compute/virtualmachines",
         "kind": "advisor", "currentSku": "Standard_D4s_v5",
         "recommendedAction": "Verify and resize to Standard_B2ms",
         "currentMonthlyUsd": 412.30, "estMonthlySavingsUsd": 286.10, "validated": None,
         "evidence": ["Advisor only; metrics cover 2 days"], "sources": ["advisor"]},
        {"resourceId": COSMOS, "resourceType": "microsoft.documentdb/databaseaccounts",
         "kind": "review", "currentSku": "", "recommendedAction": "Review provisioned RU/s",
         "currentMonthlyUsd": 5210.80, "estMonthlySavingsUsd": None, "validated": None,
         "evidence": [], "sources": ["cost"]},
    ]


def rightsizing():
    return models.build_rightsizing_savings_model(rightsizing_findings(), **context(TOTAL))


def budgets_result():
    budgets = [
        ("contoso-prod-monthly", SUB, 60000, 43094.22, 58780.00, "azure", "at_risk", [
            {"name": "actual_75", "threshold": 75, "type": "actual"}]),
        ("rg-ai-prod-monthly", f"{SUB}/resourceGroups/rg-ai-prod", 6000, 5029.95, 6860.00,
         "azure", "forecast_over", [{"name": "actual_80", "threshold": 80, "type": "actual"}]),
        ("rg-web-prod-monthly", f"{SUB}/resourceGroups/rg-web-prod", 20000, 15140.65,
         20640.00, "run-rate", "at_risk", []),
        ("rg-dev-monthly", f"{SUB}/resourceGroups/rg-dev", 1500, 655.20, 893.00, "run-rate",
         "on_track", []),
    ]
    rows = []
    for name, scope, amount, cur, fc, src, status, breached in budgets:
        rows.append({
            "name": name, "scope": scope, "amount": amount, "currency": "USD",
            "current_spend": cur, "pct_used": round(100 * cur / amount, 1),
            "forecast_spend": fc, "forecast_source": src,
            "pct_forecast": round(100 * fc / amount, 1), "status": status,
            "breached_notifications": breached, "mtd_crosscheck": None,
        })
    return {
        "as_of": AS_OF.isoformat(), "budget_count": len(rows), "budgets": rows,
        "summary": {
            "total_amount": None, "total_current": None, "total_forecast": None,
            "aggregation_reason": "Subscription and resource-group budgets overlap; totals are not additive.",
            "over_budget": 0, "forecast_over": 1, "at_risk": 2, "on_track": 1,
        },
        "gates": [{"name": "rg-ai-prod-monthly", "status": "forecast_over",
                   "reason": "forecast is 114% of the budget amount"}],
        "no_budgets": False,
    }


def budgets():
    return models.build_budget_status_model(budgets_result(), **context(TOTAL))


def allocation_result():
    inherited = {f"{SUB}/resourceGroups/rg-shared-network": {"team": "platform-shared"}}
    result = allocate.allocate_costs(COSTS, TAGS, dimension="team", inherited_tags=inherited, currency="USD")
    previous = {k: v * (0.82 if k in (AOAI, FOUNDRY) else 0.97) for k, v in COSTS.items()}
    result["month_over_month"] = allocate.month_over_month(
        COSTS, previous, dimension_tags=TAGS, dimension="team")
    return result


def allocation():
    return models.build_cost_allocation_model(allocation_result(), **context(TOTAL))


def waste_result():
    rows = [
        {"id": rid("rg-web-prod", "Microsoft.Network/publicIPAddresses", "pip-legacy-lb"),
         "type": "microsoft.network/publicipaddresses", "sku": {"name": "Standard"},
         "properties": {}},
        {"id": rid("rg-dev", "Microsoft.Network/publicIPAddresses", "pip-old-vpn"),
         "type": "microsoft.network/publicipaddresses", "sku": {"name": "Standard"},
         "properties": {}},
        {"id": rid("rg-data-prod", "Microsoft.Compute/snapshots", "snap-orders-2025-12"),
         "type": "microsoft.compute/snapshots", "sku": {"name": "Standard_LRS"},
         "properties": {"timeCreated": "2025-12-02T03:00:00Z", "diskSizeGB": 512}},
        {"id": rid("rg-web-prod", "Microsoft.Network/loadBalancers", "lb-storefront-v1"),
         "type": "microsoft.network/loadbalancers", "sku": {"name": "Standard"},
         "properties": {"backendAddressPools": [], "loadBalancingRules": []}},
        {"id": rid("rg-shared-network", "Microsoft.Network/natGateways", "nat-unused"),
         "type": "microsoft.network/natgateways", "sku": {"name": "Standard"},
         "properties": {}},
    ]
    costs = {
        rows[0]["id"]: 3.65, rows[1]["id"]: 3.65, rows[2]["id"]: 25.60, rows[3]["id"]: 18.25,
        rows[4]["id"]: 32.85,
    }
    return waste.find_waste(rows, costs=costs, managed_scopes=[SUB], currency="USD",
                            as_of=AS_OF)


def waste_report():
    return models.build_waste_model(waste_result(), **context(TOTAL))


def commitments_result():
    reservations = [{
        "id": "/providers/Microsoft.Capacity/reservationOrders/ro-1/reservations/r-1",
        "sku": {"name": "Standard_D4s_v5"},
        "properties": {
            "displayName": "ri-web-d4sv5-1y", "reservedResourceType": "VirtualMachines",
            "term": "P1Y", "quantity": 2, "userFriendlyAppliedScopeType": "Single",
            "appliedScopes": [SUB], "displayProvisioningState": "Succeeded", "renew": False,
            "expiryDateTime": "2026-12-01T00:00:00Z",
            "utilization": {"aggregates": [{"grain": 7, "value": 64.0},
                                           {"grain": 30, "value": 61.5}]},
        },
    }]
    reservation_recs = [{
        "location": "eastus",
        "properties": {
            "subscriptionId": "00000000-0000-0000-0000-000000000000",
            "resourceType": "virtualmachines", "skuName": "Standard_E8s_v5",
            "scope": "Single", "lookBackPeriod": "Last30Days", "term": "P1Y",
            "recommendedQuantity": 2, "netSavings": 9840.0,
            "costWithNoReservedInstances": 25640.0, "totalCostWithReservedInstances": 15800.0,
        },
    }]
    benefit_recs = [{
        "properties": {
            "armSkuName": "Compute_Savings_Plan", "scope": "Single",
            "subscriptionId": "00000000-0000-0000-0000-000000000000", "term": "P3Y",
            "lookBackPeriod": "Last30Days", "totalHours": 720, "currencyCode": "USD",
            "costWithoutBenefit": 14200.0,
            "recommendationDetails": {"savingsAmount": 4610.0, "totalCost": 9590.0,
                                      "commitmentAmount": 13.32, "coveragePercentage": 88.0},
        },
    }]
    usage_rows = []
    for key, cost in COSTS.items():
        model = "Reservation" if key == VM_WEB1 else "OnDemand"
        category = "Virtual Machines" if "virtualMachines" in key else "Other"
        usage_rows.append({"resourceId": key, "pricingModel": model, "meterCategory": category,
                           "chargeType": "Usage", "cost": cost})
    usage_rows.append({"resourceId": "", "pricingModel": "Reservation",
                       "meterCategory": "Virtual Machines", "chargeType": "UnusedReservation",
                       "cost": 842.0})
    vms = [
        {"id": VM_JUMP, "properties": {"storageProfile": {"osDisk": {"osType": "Windows"}}}},
        {"id": VM_REPORT, "properties": {"storageProfile": {"osDisk": {"osType": "Windows"}}}},
    ]
    return commit.analyze_commitments(
        reservations=reservations, reservation_recommendations=reservation_recs,
        benefit_recommendations=benefit_recs, usage_rows=usage_rows, vms=vms, costs=COSTS,
        managed_scopes=[SUB], currency="USD", as_of=AS_OF)


def commitments():
    return models.build_commitment_model(commitments_result(), **context(TOTAL))


def anomaly_items():
    items = []
    start = AS_OF - timedelta(days=31)
    for offset in range(32):
        day = (start + timedelta(days=offset)).isoformat()
        wobble = 1 + 0.03 * math.sin(offset)
        spike = 1460.0 if offset == 30 else 0.0
        items.append({"date": day, "cost": round(312.0 * wobble, 2),
                      "meterCategory": "Azure Kubernetes Service",
                      "resourceGroup": "rg-web-prod", "resourceId": AKS})
        items.append({"date": day, "cost": round(178.0 * wobble + spike, 2),
                      "meterCategory": "Azure OpenAI", "resourceGroup": "rg-ai-prod",
                      "resourceId": AOAI})
        items.append({"date": day, "cost": round(240.0 * wobble, 2),
                      "meterCategory": "SQL Database", "resourceGroup": "rg-data-prod",
                      "resourceId": SQL})
    return items


def anomalies_result():
    items = anomaly_items()
    daily = {}
    for item in items:
        daily[item["date"]] = daily.get(item["date"], 0.0) + item["cost"]
    return {
        "date": (AS_OF - timedelta(days=1)).isoformat(),
        "anomalies": detect.detect_anomalies(items),
        "credits": detect.summarize_credits(items),
        "daily": [{"date": d, "cost_usd": round(v, 2)} for d, v in sorted(daily.items())],
    }


def anomalies():
    return models.build_cost_anomaly_model(anomalies_result(), **context(TOTAL))


def sre_agent_result():
    daily = [{"date": day, "usd": round(38.4 + 9 * abs(math.sin(i / 2.5)), 2)}
             for i, day in enumerate(DAYS)]
    total = round(sum(r["usd"] for r in daily), 2)
    always_on = round(19.2 * len(DAYS), 2)
    active = round(total - always_on, 2)

    def agent(name, share, mtd, limit, projected):
        cost = round(total * share, 2)
        return {"agentName": name, "resourceId": rid("rg-sre", "Microsoft.App/agents", name),
                "status": "active", "split": "measured", "monthly_usd": cost,
                "always_on_usd": round(always_on / 2, 2),
                "active_usd": round(cost - always_on / 2, 2), "mtd_active_aau": mtd,
                "limit_aau": limit, "projected_limit_pct": projected, "model": "Automatic",
                "always_on_waived": False}

    return {
        "currency": "USD", "total_usd": total, "always_on_usd": always_on,
        "active_usd": active, "always_on_pct": round(100 * always_on / total, 1),
        "agent_count": 2, "inventory_count": 2, "always_on_monthly_usd_per_agent": 292.0,
        "window_start": DAYS[0], "window_end": DAYS[-1],
        "by_agent": [agent("sre-contoso-prod", 0.62, 3120.0, 6000.0, 74.0),
                     agent("sre-contoso-dev", 0.38, 410.0, 2000.0, 29.0)],
        "by_source": [
            {"label": "Incidents", "aau": 1520.0, "usd": round(active * 0.43, 2), "pct": 43.0,
             "thread_count": 38},
            {"label": "Scheduled tasks", "aau": 1180.0, "usd": round(active * 0.33, 2),
             "pct": 33.0, "thread_count": 66},
            {"label": "Chat", "aau": 830.0, "usd": round(active * 0.24, 2), "pct": 24.0,
             "thread_count": 41},
        ],
        "top_threads": [
            {"agentName": "sre-contoso-prod", "threadId": "incident-4821",
             "source": "Incidents", "aau": 214.0, "usd": 21.40, "days": 2,
             "last_active": "2026-10-21"},
            {"agentName": "sre-contoso-prod", "threadId": "task-finops-weekly",
             "source": "Scheduled tasks", "aau": 168.0, "usd": 16.80, "days": 4,
             "last_active": "2026-10-20"},
            {"agentName": "sre-contoso-dev", "threadId": "chat-capacity-review",
             "source": "Chat", "aau": 92.0, "usd": 9.20, "days": 1, "last_active": "2026-10-18"},
        ],
        "daily": daily,
        "hints": [
            {"type": "idle_agent_candidate", "target": "sre-contoso-dev",
             "detail": "Low active usage; always-on flow is most of its cost. Consider deleting "
                       "it when it is not needed.", "monthly_usd": 292.0},
        ],
    }


def sre_agent():
    return models.build_sre_agent_spend_model(sre_agent_result(), **context(TOTAL))


def ai_spend():
    data = {
        "as_of": AS_OF.isoformat(), "total_ai_usd": 5029.95, "resource_count": 2,
        "model_count": 4,
        "by_service_family": [
            {"service_family": "Cognitive Services / OpenAI", "monthly_usd": 3925.60,
             "pct": 78.0},
            {"service_family": "AI Foundry", "monthly_usd": 1104.35, "pct": 22.0},
        ],
        "by_meter_type": [
            {"meter_type": "model_token", "monthly_usd": 4210.45, "pct": 83.7},
            {"meter_type": "compute", "monthly_usd": 819.50, "pct": 16.3},
        ],
        "by_resource": [
            {"resourceId": AOAI, "resourceName": "aoai-contoso-eus", "kind": "OpenAI",
             "service_family": "Cognitive Services / OpenAI", "monthly_usd": 3925.60,
             "pct": 78.0, "top_model": "gpt-4o"},
            {"resourceId": FOUNDRY, "resourceName": "foundry-assistants", "kind": "AIServices",
             "service_family": "AI Foundry", "monthly_usd": 1104.35, "pct": 22.0,
             "top_model": "gpt-4o-mini"},
        ],
        "by_model": [
            {"model": "gpt-4o", "monthly_usd": 2840.10, "pct": 67.5, "resource_count": 1},
            {"model": "gpt-4o-mini", "monthly_usd": 712.40, "pct": 16.9, "resource_count": 2},
            {"model": "text-embedding-3-large", "monthly_usd": 418.55, "pct": 9.9,
             "resource_count": 1},
            {"model": "o4-mini", "monthly_usd": 239.40, "pct": 5.7, "resource_count": 1},
        ],
        "top_drivers": [
            {"resourceName": "aoai-contoso-eus", "service_family": "Cognitive Services / OpenAI",
             "model": "gpt-4o", "meter_type": "model_token", "monthly_usd": 2840.10},
            {"resourceName": "foundry-assistants", "service_family": "AI Foundry",
             "model": "(compute)", "meter_type": "compute", "monthly_usd": 819.50},
        ],
        "hints": [
            {"type": "commitment_opportunity", "target": "aoai-contoso-eus",
             "detail": "Steady gpt-4o token usage; evaluate provisioned throughput (PTU).",
             "monthly_usd": 2840.10},
            {"type": "model_downshift", "target": "foundry-assistants",
             "detail": "Test gpt-4o-mini for the classification route.", "monthly_usd": 712.40},
        ],
    }
    return models.build_ai_spend_model(data, **context(TOTAL))


def reliability():
    def res(key, service, cost, alerts, sev, health, advisor, score, band, signal):
        counts = {"Sev0": 0, "Sev1": 0, "Sev2": 0, "Sev3": 0, "Sev4": 0}
        counts.update(sev)
        return {
            "resourceId": key, "resourceName": key.rsplit("/", 1)[1],
            "resourceGroup": key.split("/resourceGroups/")[1].split("/")[0], "service": service,
            "monthly_usd": cost, "pct": round(100 * cost / TOTAL, 1), "alert_count": alerts,
            "sev_counts": counts, "sev0": counts["Sev0"], "sev1": counts["Sev1"],
            "sev2": counts["Sev2"], "sev3": counts["Sev3"], "sev4": counts["Sev4"],
            "health_event_count": health, "advisor_reliability_count": advisor,
            "reliability_score": score,
            "pain_per_1000_usd": round(1000 * score / cost, 2) if score else None,
            "risk_band": band, "primary_signal": signal,
        }

    rows = [
        res(AKS, "Azure Kubernetes Service", 9120.40, 14, {"Sev1": 3, "Sev2": 11}, 2, 1, 41,
            "high", "alert"),
        res(SQL, "SQL Database", 7480.15, 6, {"Sev2": 6}, 1, 1, 17, "medium", "alert"),
        res(AGW, "Application Gateway", 1640.25, 9, {"Sev1": 2, "Sev2": 7}, 0, 1, 24, "high",
            "alert"),
        res(COSMOS, "Azure Cosmos DB", 5210.80, 0, {}, 0, 0, 0, "low", "none"),
        res(VM_BATCH, "Virtual Machines", 4380.00, 1, {"Sev3": 1}, 0, 0, 1, "low", "alert"),
    ]
    services = {}
    for r in rows:
        s = services.setdefault(r["service"], {
            "service": r["service"], "monthly_usd": 0, "resource_count": 0, "alert_count": 0,
            "health_event_count": 0, "advisor_reliability_count": 0, "reliability_score": 0})
        s["monthly_usd"] += r["monthly_usd"]
        s["resource_count"] += 1
        for k in ("alert_count", "health_event_count", "advisor_reliability_count",
                  "reliability_score"):
            s[k] += r[k]
    for s in services.values():
        s["pct"] = round(100 * s["monthly_usd"] / TOTAL, 1)
        s["risk_band"] = "high" if s["reliability_score"] >= 20 else (
            "medium" if s["reliability_score"] >= 10 else "low")
    data = {
        "as_of": AS_OF.isoformat(), "total_usd": TOTAL, "resource_count": len(COSTS),
        "reliability_signal_count": 41,
        "coverage": {"cost_resource_count": len(COSTS), "reliability_resource_count": 6,
                     "joined_resource_count": 5, "unmatched_reliability_count": 1,
                     "subscription_level_event_count": 0},
        "by_resource": rows, "by_service": list(services.values()), "top_drivers": [],
        "hints": [
            {"type": "high_incident_low_spend", "target": "agw-storefront",
             "detail": "Frequent Sev1 backend-health alerts on low spend; add zone redundancy "
                       "and autoscale headroom.", "monthly_usd": 1640.25,
             "reliability_score": 24},
            {"type": "high_spend_no_pain", "target": "cosmos-catalog",
             "detail": "High spend with no reliability signals; verify RU/s headroom before "
                       "reducing.", "monthly_usd": 5210.80, "reliability_score": 0},
        ],
        "unmatched_reliability": [
            {"resourceId": rid("rg-web-prod", "Microsoft.Web/sites", "func-webhooks"),
             "resourceName": "func-webhooks", "signal_type": "alert", "severity": "Sev2",
             "score": 3, "date": "2026-10-19", "detail": "No cost rows this period"},
        ],
        "data_quality": {
            "cost_partial": False,
            "sources_used": {"usage_details": True, "alerts": True, "resource_health": True,
                             "advisor_highavailability": True},
            "limitations": ["Alert counts are severity-weighted, not incident durations."],
        },
    }
    return models.build_cost_vs_reliability_model(data, **context(TOTAL))


def optimization():
    data = summarize.summarize_optimization(
        anomalies=anomalies_result()["anomalies"], rightsizing=rightsizing_findings(),
        allocation=allocation_result(), budgets=budgets_result(), waste=waste_result(),
        commitments=commitments_result(), as_of=AS_OF,
        spend_period={"from": DAYS[0], "to": DAYS[-1]})
    return models.build_cost_optimization_model(data, **context(TOTAL))


def monthly():
    kpis = [
        ("allocated_pct", "Spend allocated", "Allocation", 87.6, "%", 90, True, "warn",
         "Tag law-contoso-prod and vm-jumpbox-win with an owning team."),
        ("untagged_pct", "Untagged spend", "Allocation (tag governance)", 4.1, "%", 5, False,
         "pass", None),
        ("budget_coverage_pct", "Managed scopes with a budget", "Budgeting (governance)", 80.0,
         "%", 100, True, "warn", "Add a budget for rg-data-prod."),
        ("forecast_vs_budget_pct", "Forecast vs budget", "Forecasting", 98.0, "%", 100, False,
         "pass", None),
        ("commitment_coverage_pct", "Commitment coverage", "Rate optimization", 9.1, "%", 60,
         True, "fail", "Review the Compute savings plan recommendation."),
        ("commitment_utilization_pct", "Commitment utilization", "Rate optimization", 61.5,
         "%", 95, True, "fail", "Exchange or re-scope ri-web-d4sv5-1y."),
        ("waste_pct", "Waste as share of spend", "Workload optimization", 0.6, "%", 2, False,
         "pass", None),
        ("anomaly_impact_pct", "Anomaly impact", "Anomaly management", 3.4, "%", 2, False,
         "warn", "Confirm the Azure OpenAI spike on 2026-10-22 was planned."),
        ("unit_cost_change_pct", "Unit cost change", "Unit economics", -4.2, "%", 0, False,
         "pass", None),
    ]
    rows = [{"id": k, "name": n, "capability": c, "value": v, "unit": u, "target": t,
             "higher_is_better": h, "status": s, "action": a, "detail": None}
            for k, n, c, v, u, t, h, s, a in kpis]
    disk = rid("rg-data-prod", "Microsoft.Compute/disks", "disk-batch-scratch-02")
    lb = rid("rg-web-prod", "Microsoft.Network/loadBalancers", "lb-storefront-v1")
    review = {
        "month": "2026-10",
        "scorecard": {
            "score_pct": round(100 * sum(r["status"] == "pass" for r in rows) / len(rows), 1),
            "kpi_health_pct": round(100 * sum(r["status"] == "pass" for r in rows) / len(rows), 1),
            "evidence_coverage_pct": 100.0,
            "organizational_maturity": "not_assessed",
            "maturity": "walk", "kpis": rows,
            "actions": [{"kpi": r["id"], "status": r["status"], "action": r["action"]}
                        for r in rows if r["action"]],
        },
        "forecast": {
            "available": True, "month": "2026-10", "next_month": "2026-11",
            "month_to_date": TOTAL, "days_remaining": 9, "month_end_forecast": 58780.0,
            "month_end_low": 57200.0, "month_end_high": 60100.0,
            "next_month_forecast": 59900.0, "next_month_low": 56800.0,
            "next_month_high": 63000.0, "run_rate_30d": 58400.0, "method": "linear_trend",
            "confidence": "high", "budget_amount": 60000.0, "forecast_vs_budget_pct": 98.0,
            "budget_status": "at_risk", "warnings": [],
        },
        "month_over_month": {
            "current_usd": 58780.0, "previous_usd": 55310.0, "delta_usd": 3470.0,
            "delta_pct": 6.3,
            "by_owner": [
                {"owner": "ai-assistants", "previous_usd": 5420, "current_usd": 6860,
                 "delta_usd": 1440, "delta_pct": 26.6},
                {"owner": "storefront", "previous_usd": 19650, "current_usd": 20640,
                 "delta_usd": 990, "delta_pct": 5.0},
                {"owner": "data-platform", "previous_usd": 24100, "current_usd": 24880,
                 "delta_usd": 780, "delta_pct": 3.2},
                {"owner": "finance-apps", "previous_usd": 1390, "current_usd": 1340,
                 "delta_usd": -50, "delta_pct": -3.6},
            ],
            "movers": {
                "new": [{"resourceId": FOUNDRY, "owner": "ai-assistants", "previous_usd": 0,
                         "current_usd": 1104.35, "delta_usd": 1104.35, "delta_pct": None}],
                "increased": [{"resourceId": AOAI, "owner": "ai-assistants",
                               "previous_usd": 3220, "current_usd": 3925.60,
                               "delta_usd": 705.60, "delta_pct": 21.9}],
                "decreased": [{"resourceId": VM_REPORT, "owner": "finance-apps",
                               "previous_usd": 1030, "current_usd": 980, "delta_usd": -50,
                               "delta_pct": -4.9}],
                "removed": [],
            },
        },
        "routing": {
            "owners": [
                {"owner": "data-platform", "total_usd": 2325.17, "items": [
                    {"resourceId": VM_BATCH, "source": "rightsizing", "amount_usd": 2190.0,
                     "action": "Resize to Standard_E8s_v5"},
                    {"resourceId": disk, "source": "rightsizing", "amount_usd": 135.17,
                     "action": "Snapshot, then delete the unattached disk"},
                ]},
                {"owner": "storefront", "total_usd": 1113.25, "items": [
                    {"resourceId": VM_WEB2, "source": "rightsizing", "amount_usd": 1095.0,
                     "action": "Resize to Standard_D4s_v5"},
                    {"resourceId": lb, "source": "waste", "amount_usd": 18.25,
                     "action": "Delete the empty load balancer"},
                ]},
            ],
            "unrouted": [{"resourceId": DEV_VM, "source": "rightsizing", "amount_usd": 655.20,
                          "action": "Add an owner tag, then shut down"}],
            "unrouted_usd": 655.20, "routed_pct": 84.0,
        },
        "unit_economics": {"unit": "order", "per": 1000, "cost_per_unit": 41.20,
                           "previous_cost_per_unit": 43.00, "change_pct": -4.2,
                           "trend": "improving", "interpretation": None, "warnings": []},
    }
    return models.build_monthly_review_model(review, **context(TOTAL))


REPORTS = {
    "cost-overview": overview,
    "cost-anomalies": anomalies,
    "budget-status": budgets,
    "rightsizing-savings": rightsizing,
    "cost-allocation": allocation,
    "waste-cleanup": waste_report,
    "cost-optimization": optimization,
    "commitments": commitments,
    "ai-spend": ai_spend,
    "sre-agent-spend": sre_agent,
    "cost-vs-reliability": reliability,
    "monthly-review": monthly,
}


def main():
    for name, build in REPORTS.items():
        model = build()
        model["description"] = f"{model.get('description') or ''} Sample data (fictional).".strip()
        path = renderer.write_report(model, OUT / f"{name}.html")
        # The Live Report host injects a per-request nonce; use a fixed one so the sample
        # files (and their charts) open directly in a browser.
        text = Path(path).read_text(encoding="utf-8").replace("{REPORT_NONCE}", "sample")
        Path(path).write_text(text, encoding="utf-8")
        print(path)


if __name__ == "__main__":
    main()
