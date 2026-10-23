# Cost allocation — Microsoft guidance

Supporting reference for `finops-cost-allocation`. Everything here is recommend-only: the agent
reads the current state and suggests changes; it never creates rules, tags or policy assignments.

## Allocation strategy

- Define the allocation strategy first: which hierarchy (business unit, cost center, application,
  environment) each cost rolls up to. Then allocate **top-down**: subscriptions and resource groups
  first, then tags.
- Prototype on a small part of the estate before rolling out, and keep it simple. Not every shared
  cost needs to be split.
- Track the percentage of cost that is unallocated, including unallocated shared cost, as a KPI
  (`allocated_pct` and `untagged_pct` in `finops-kpi-scorecard`).
- Common shared services to look for:
  - AKS clusters and App Service plans that host several apps
  - Azure Virtual Desktop
  - Log Analytics workspaces and Microsoft Defender for Cloud
  - Hub networking: ExpressRoute, VPN, Azure Firewall
  - Shared databases: Azure SQL elastic pools, Cosmos DB

  Split them with the `shared=` option of `allocate_costs`.
- Group child resources under their parent application with the `cm-resource-parent` tag. Its
  value is the parent's resource ID. Cost Management uses it to roll up related resources. When
  you see the tag, allocate the child to the parent's owner.

Source: https://learn.microsoft.com/en-us/cloud-computing/finops/framework/understand/allocation
Last reviewed: 2026-10-23

## Native tag inheritance in Cost Management

- `inherited_tags=` emulates Cost Management tag inheritance inside the sandbox. Recommend that
  the customer turn on the native feature, because inherited tags then appear in Cost Management
  views, budgets and exports for everyone.
- Native tag inheritance is available for EA, MCA and MPA billing scopes. A billing account
  administrator turns it on.
- It takes effect within 8–24 hours and applies to the **current month** only. Earlier months are
  not restated.
- A tag on the resource wins over an inherited tag, unless the customer chooses to override. Budgets
  can filter on inherited tags about 24 hours after inheritance is turned on.
- Pay-as-you-go (MOSP) subscriptions cannot use native inheritance. Keep using `inherited_tags=`.

Source: https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/enable-tag-inheritance
Last reviewed: 2026-10-23

## Cost allocation rules

- Cost Management **cost allocation rules** move the cost of shared subscriptions, resource groups
  or tags to other scopes. They are available for EA and MCA. A Billing account owner or an
  Enterprise Administrator creates them.
- Reassigned cost is visible in Cost Analysis, and in Cost Details and Exports through the
  `costAllocationRuleName` column. **The Consumption UsageDetails API that this pack reads does not
  show reassignments.** If the customer uses allocation rules, say that this skill's showback is
  *before* allocation rules, so totals per owner can differ from Cost Analysis.

Source: https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/allocate-costs
Last reviewed: 2026-10-23

## Enforcing tags with Azure Policy

- Recommend built-in tag policies such as "Require a tag on resources" and "Inherit a tag from the
  resource group if missing".
- Roll them out **audit first**: assign them with the `Audit` effect, review compliance, then move to
  `Deny` or `Modify` in stages, by subscription or environment.
- Measure tag compliance as a KPI and review it in the Monthly Review.
- Read-only compliance check. Policy assignments and compliance states are in Azure Resource Graph.
  Assignments are matched by display name, so tag policies that are not named with "tag" will be
  missed:

  ```bash
  az graph query -q "policyresources
  | where type =~ 'microsoft.authorization/policyassignments'
  | where tostring(properties.displayName) contains 'tag'
  | project assignmentId = tolower(id), assignment = tostring(properties.displayName),
            effect = tostring(properties.parameters.effect.value), scope = tostring(properties.scope)
  | join kind=leftouter (policyresources
      | where type =~ 'microsoft.policyinsights/policystates'
      | extend assignmentId = tolower(tostring(properties.policyAssignmentId)),
               state = tostring(properties.complianceState)
      | summarize nonCompliant = countif(state =~ 'NonCompliant'), evaluated = count() by assignmentId)
    on assignmentId
  | project assignment, scope, effect, nonCompliant, evaluated" \
    --subscriptions <EFFECTIVE_SUBSCRIPTIONS> -o json
  ```

  If there are no tag assignments, recommend the built-in policies with the audit-first rollout. If
  assignments exist, report their non-compliant counts next to `untagged_pct`.

Sources: https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/tag-policies ·
https://learn.microsoft.com/en-us/cloud-computing/finops/framework/manage/governance
Last reviewed: 2026-10-23

## Showback before chargeback

Start with showback, then make allocation reliable, then move to chargeback. Use `fully_loaded_usd`
for chargeback only after owners trust the allocation and the unallocated bucket is small.

Source: https://learn.microsoft.com/en-us/cloud-computing/finops/framework/manage/invoicing-chargeback
Last reviewed: 2026-10-23

## FOCUS exports, FinOps hubs and pay-as-you-go

- FinOps hubs from the FinOps toolkit are Microsoft's scale path for cost data. They ingest
  Cost Management exports in FOCUS format into storage and Azure Data Explorer or Microsoft Fabric,
  and include Power BI reports.
- **Caveat for MOSA (pay-as-you-go) subscriptions:** FinOps hubs v0.2 and later need FOCUS exports.
  The hubs documentation states that FOCUS exports are not available for MOSA. Only hubs v0.1.1
  supports MOSA. For pay-as-you-go estates, keep the UsageDetails path in this pack.

Source: https://learn.microsoft.com/en-us/cloud-computing/finops/toolkit/hubs/finops-hubs-overview
Last reviewed: 2026-10-23
