---
name: finops-carbon-optimization
description: Cloud sustainability for FinOps, read-only. Reports Azure carbon emissions (kgCO2e) for the managed scope from Azure Carbon Optimization — 12-month trend, month-over-month change, top-emitting resources, resource types and regions, and the largest increases — and links them to cost-optimization findings. Because the Carbon Optimization report API is a query POST that the agent's read-only tools block, the bundled carbon.py builds validated requests and a human-run read-only script, then analyzes the saved responses in the sandbox. Use for "what are our cloud emissions", carbon trend, sustainability reporting, and green FinOps.
---

## When to use this skill

Use it when the user asks about **cloud carbon emissions or sustainability**: "what are our
emissions", "which resources emit the most", "did emissions go up", or "add sustainability to the
FinOps review". It is read-only and recommends actions only.

Microsoft's FinOps Framework treats **cloud sustainability** as part of optimizing usage and cost:
the same rightsizing, cleanup and scheduling that cut cost also cut emissions. Pair this skill's
top emitters with `finops-rightsizing-advisor` and `finops-waste-finder` findings on the same
resources.

## What Azure Carbon Optimization provides

- Free. Emissions are reported in **kgCO2e** for Scope 1, 2 and 3 (GHG Protocol), at resource,
  resource group, resource type, region and subscription level.
- **Monthly** data for the last **12 months**. The previous month is normally available by
  day 19 of the current month.
- Access: **Carbon Optimization Reader** or **Reader** on the subscription. Carbon Optimization
  Reader at billing-account scope applies to MCA only.
- The **Emissions Impact Dashboard** for Power BI retires on **31 March 2027**. Recommend Azure
  Carbon Optimization (portal, export, or this API) instead, even where older FinOps articles
  still mention the dashboard.

Sources: https://learn.microsoft.com/en-us/azure/carbon-optimization/overview ·
https://learn.microsoft.com/en-us/azure/carbon-optimization/permissions ·
https://learn.microsoft.com/en-us/cloud-computing/finops/framework/optimize/sustainability
Last reviewed: 2026-10-23

## Required access

- **Carbon Optimization Reader** or **Reader** on each subscription, for the human who runs the
  query script.
- Read-only `az` (`RunAzCliReadCommands`) for the managed-scope GET, and sandbox Python: use
  `ExecutePythonCode` when available, otherwise `RunInTerminal` with `python3`.
- **The agent issues no POST.** The Carbon Optimization report API
  (`POST /providers/Microsoft.Carbon/carbonEmissionReports?api-version=2025-04-01`) only reads data,
  but `RunAzCliReadCommands` blocks every POST as a write. A human runs the generated script, or
  exports the data from the portal, and gives the agent the saved JSON.

## Procedure

### Step 0 — Resolve the managed boundary

First load `finops-managed-scope` and follow its `scope.py` procedure to dynamically GET and validate
the current agent `managedResources`; never use cached scope. Managed scopes and their expanded
descendants are the default boundary. Scheduled runs are strict/fail-closed with no override. An
interactive named resource outside the boundary requires disclosure and explicit confirmation in a
subsequent turn before any broader query. Broad RBAC never expands scope implicitly.

Build requests for each effective scope from the validated boundary only. Report the included
scopes, excluded items, unattributed items, and any scope this skill cannot express (for example
a management group that was not expanded to subscriptions).

### Step 1 — Build the requests and the human-run script

```
read_skill_file(skill_name="finops-carbon-optimization", file_path="carbon.py")
```

```python
from carbon import standard_requests, human_run_script
requests = standard_requests(effective_scopes, today_utc)   # overall, monthly, top-* (latest month)
script = human_run_script(requests, out_dir="carbon-data")
```

`standard_requests` targets the newest published month (`latest_complete_month`: the previous
month from day 19, otherwise the month before). It returns an `OverallSummaryReport`, a 12-month
`MonthlySummaryReport`, and `TopItemsSummaryReport`s for resources, resource types and locations.
Subscription and resource-group filters come from the managed scope. Mixing whole-subscription and
resource-group scopes raises an error: build one request set per kind. Use
`build_report_request(...)` directly for other cuts, for example an `ItemDetailsReport` by resource
group; the script follows its `skipToken` pages.

Give the user the script and ask them to run it with their own Azure CLI sign-in. It only runs
Carbon Optimization queries and writes JSON files. Then ask them to paste or upload each saved
file. The agent never runs the script.

### Step 2 — Analyze (bundled carbon.py, in-sandbox)

Save each response the user provides with `finops-managed-scope`'s `save_finops_input` (for
example `carbon-overall`, `carbon-monthly`, `carbon-top-resources`), then load them for analysis:

```python
from usage import load_finops_input        # finops-managed-scope/usage.py
from carbon import analyze_carbon
result = analyze_carbon(
    {name: load_finops_input(name, run_id=run_id) for name in saved_names},
    managed_scopes=effective_scopes,
)
```

`analyze_carbon` returns, in **kgCO2e**:

- **overall** — the latest month, the previous month, and the change in kg and %.
- **monthly** — the trend by month, with carbon intensity when the API returns it.
- **top_items** — the top emitters per category (Resource, ResourceType, Location, ...).
- **increases** — items whose emissions rose by at least `increase_threshold_pct` (10%).
- **access** — subscriptions allowed and denied, with the denial reason.
- **excluded_count / unattributed_count** — items outside the managed scope, and items with no
  ARM id that rely on the request filters.
- **hints** — `access_denied`, `emissions_increase`, and `concentration` (share of the top
  resources).

### Step 3 — Report

Lead with the latest month's total and its month-over-month change, then the 12-month trend. Then
show the top resources, resource types and regions, and the largest increases. List denied
subscriptions and how to grant access. State that emissions are monthly, lag by up to about three
weeks, and are estimates from Microsoft's methodology.

Close with **reduction levers**, each tied to an existing pack finding where one exists:

- delete idle and orphaned resources (`finops-waste-finder`);
- rightsize or shut down underused VMs and services (`finops-rightsizing-advisor`);
- schedule non-production off-hours and use autoscale;
- consider regions with lower carbon intensity when moving or building workloads;
- move cold data to cooler storage tiers.

Recommend only — this skill changes nothing.

Source: https://learn.microsoft.com/en-us/azure/carbon-optimization/reduce-emissions.
Last reviewed: 2026-10-23.

## Notes

- Request rules (enforced by `build_report_request`): subscription ids in lowercase, at most 100
  per request; months as `YYYY-MM-01`; `ItemDetailsReport` and `TopItemsSummaryReport` cover a
  single month; `topItems` is 1–10; `pageSize` is at most 5000.
- `queryCarbonEmissionDataAvailableDateRange` (also a POST) returns the published `startDate` and
  `endDate`; the script saves it as `available-range.json`.
- There is no Live Report for this skill yet, because a scheduled run cannot fetch the data
  without a human. The data can also be exported from the portal to CSV.
- API reference: https://learn.microsoft.com/en-us/rest/api/carbon/carbon-service/query-carbon-emission-reports.
  Last reviewed: 2026-10-23.
