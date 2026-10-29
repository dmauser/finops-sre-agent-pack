# Production operations

This pack is an analysis system, not an autonomous cost-remediation service. An installation is
**not production-validated merely because deployment succeeded or the tests passed**. Gate each
customer environment on source coverage, permissions, transport, reconciliation and canary reports.
The optional native-export path remains disabled until its actual SRE Agent transport is proven.

## Financial contract

Every execution creates a unique `run_id` and writes only under
`/mnt/data/finops-runs/<run_id>/`. Retain the thread ID and run-start filters for tool-output discovery.
Never delete shared scratch directories or assemble a ledger by scanning another execution's files.

Use `make_cost_snapshot` followed by `save_cost_snapshot` from the managed-scope helper. The latter
checks the content hash and refuses to overwrite a snapshot in the same run. Persist rows, not a
plain JSON serialization of `CostMap` (which would discard currency, nonresource charges and tags).
Rebuild resource aggregates from the snapshot rows. Pass `cost_context=snapshot` to every renderer.
Record the **actual installed source SHA** alongside the plugin version; never invent a version.

The ledger distinguishes requested dates, observed usage dates, and a verified `coverage_period`.
Coverage must come from the completed source request/run, not the earliest/latest charge alone.
Absent coverage remains partial. A complete transport does not establish invoice settlement:
late charges, corrections and credits can revise history. Reconcile exact-period totals, currency
and ActualCost/AmortizedCost basis with Cost Management independently; account explicitly for
nonresource charges and unresolved scope attribution.

ActualCost supports invoice/budget comparisons; AmortizedCost supports usage and commitment
allocation. Do not mix bases, currencies or scopes in one total. Do not substitute month-to-date
data when a closed month is missing. Confirm an empty period from its source before treating it as
zero. Forecast/budget comparison also requires equal filter, scope, currency and period.

## Optional native exports

`deploy/exports.bicep` is additive and works independently of agent creation. It provisions one Hot
Standard_ZRS account, daily and closed-month containers, four native CSV/gzip jobs (ActualCost and
AmortizedCost for each cadence), retention, and container-scoped read access for the agent.
The export service owns its destination-write identity; the agent does not receive blob writes.

Prerequisites: a supported native dataset version for the billing agreement, destination provider
`Microsoft.CostManagementExports` registration, export-create and role-assignment permissions for
the deploying operator, and a security-approved network path. The operator's permissions are not
granted to the agent.

```bash
EXPORT_DATA_VERSION=2021-01-01 \
EXPORT_SCHEDULE_START=<future-UTC-date> \
./plugins/finops/deploy/deploy.sh --exports-only \
  -s <subscription> -g <existing-agent-rg> -n <existing-agent> -y
```

The default is `EXPORT_STATUS=Inactive`. The one-year recurrence end must be renewed before expiry.
Daily generations expire after 45 days, closed history after 400 days; seven-day soft deletion can
extend billed storage. Review retention against accounting/audit requirements.

The account disables anonymous blobs, shared keys and insecure transport. Public network access
defaults to **Disabled**. The optional `publicNetworkAccess=Enabled` Bicep parameter enables an
authenticated public endpoint, **not private network isolation**; use only with explicit security
approval. No policy exemption or `SecurityControl:Ignore` tag is created. If your policy disables
public access, preserve that control and design/price approved connectivity separately.

### Transport gate

1. Complete one on-demand export. Inspect status and its real manifest; a queued run is not proof.
2. Use `deploy/Test-ExportTransport.ps1` with the real manifest blob. It requests one bounded,
   read-only agent download using Entra authentication and verifies the **same file** is readable
   by the Python sandbox. Confirm actual tool/file evidence, not just task success.
3. If the read-only CLI tool rejects download, network policy blocks it, or the sandbox cannot see
   the downloaded file, stop. Do not transcribe large ledgers, enable write tools, use account keys
   or silently add a connector/compute service.
4. Download every manifest partition, check generation identity, ETag, dataset version, basis,
   byte counts and row counts with `read_export_snapshot`. Configure `expected_export_id` and pass
   the completed run's verified `source_period`. Preserve manifest and partition hashes.
5. Reconcile a real source fixture with Cost Management before enabling export-backed reports.
   Never concatenate yesterday's cumulative MonthToDate export with today's generation.

No automatic downloader or connector is bundled. The offline reader is not a claim that every
SRE Agent runtime supports shared-file downloads. If the gate fails, leave exports inactive and
explicitly use the Consumption UsageDetails fallback. It must retrieve one terminal continuation
chain per scope/basis, disclose limited history, and refuse unavailable closed-month analyses.

**Reference validation, 2026-10-29:** an on-demand native export completed, but the tested SRE Agent
runtime rejected `az storage blob download` before execution: its read-only gate accepts only
`get`, `list`, `show`, and `query`. Sandbox Python confirmed the manifest file was absent.
The execution itself reported success, illustrating why tool/file evidence is required.
This export-ingestion path is therefore **blocked in that runtime**. Removing a storage network
restriction would not resolve the separate tool restriction. A separately approved read-only
connector/transport is needed; no such connector is included or claimed validated here.

After transport and reconciliation pass, backfill the two required closed months and verify
coverage before activating schedules. Historical revisions replace a selected generation; they
are not incremental usage to append. Keep the prior ledger for audit.

## Controlled upgrades

Capture the installed plugin/source SHA, imported skills, investigator definition, every FinOps
task's prompt/cron/status/model/start/end/max-executions/delivery, reports and role assignments in
private operational storage. Never commit customer exports or operational snapshots.

The installer defaults to Cost Optimization only and `PLUGIN_AUTO_UPDATE=false`. Use an explicit
task list on upgrades; existing tasks keep scheduling/model/status fields unless overridden.
`PRESERVE_TASK_SETTINGS=false` is an explicit reset to manifest settings. An existing email task
requires its approved `ALERT_EMAIL`, or explicit `DISABLE_NOTIFICATIONS=true`; no inferred recipients.

`EXPECTED_SOURCE_COMMIT=<full-sha>` verifies the repository default-branch HEAD **before** direct
installation and the installed `sourceCommit` **afterward**. It detects races; it is not a server-side
immutable-ref installation facility. The current direct API clones the repository URL and rejects
GitHub `/tree/<sha>` URLs. A candidate branch therefore needs an explicitly approved default-branch
promotion before this installation path. Do not infer merge approval from a request to commit/push.

Pause only FinOps schedules and drain their active executions. Install the approved SHA, confirm
auto-update remains off, skill count/content, managed scope and read-only tool inventory. Run Cost
Overview and Budget Status canaries, inspect rendered provenance/amounts and reconcile independent
totals. Then refresh each remaining selected report once and inspect uncertainty/savings semantics.
Restore the captured schedules and delivery only after mandatory checks pass. Record AAUs and
limitations rather than promising savings from fewer API calls.

Rollback requires a known-good source promotion through the same verified installation path (or
a separately proven platform rollback mechanism), plus the saved investigator/task settings.
Simply editing installation metadata does not prove that skill content rolled back. Do not enable
auto-update as a substitute for controlled recovery.

## Permissions

FinOps analysis needs Reader, Cost Management Reader, Log Analytics Reader and Monitoring Reader
at the required scopes. Export reads use Storage Blob Data Reader on the two containers only.
Keep platform-owned agent-resource-group roles separate from subscription analysis permissions.

For legacy deployments, `monitoring-reader.bicep` adds the replacement reader first and outputs
the exact deterministic pack-owned subscription Monitoring Contributor assignment. Use
`Migrate-MonitoringRole.ps1` to validate principal/scope/role, capture rollback data and remove only
that assignment. Never delete all roles with the same display name, unrelated assignments, or the
platform's resource-group monitoring role.

## Acceptance and release evidence

Keep the release commit, tests/evaluations, deployment result, post-policy network posture,
transport result, manifest/reconciliation evidence, canary report IDs, measured AAUs and restored
schedule comparison together. Missing evidence is a blocked gate, not a successful deployment.

See [report interpretation](REPORTS.md), [incremental cost and validation costs](COST.md), and
[onboarding](ONBOARDING.md). The bundled examples are fictional, not evidence for a customer's bill.
