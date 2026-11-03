# Contributing

Contributions are welcome: reproducible customer scenarios, bug fixes, improvements to existing
skills and reports, synthetic test cases, and clearer documentation. Start with a small change
that helps an operator answer a specific question or take a well-supported next step.

## Before you start

Read the [repository overview](README.md), [plugin guide](plugins/finops/README.md), and
[report interpretation guide](docs/REPORTS.md). Search
[existing issues](https://github.com/dmauser/finops-sre-agent-pack/issues) and
[pull requests](https://github.com/dmauser/finops-sre-agent-pack/pulls) before opening a new one.
For a new skill, a breaking change, or a change to permissions or deployment behavior, open an
issue first to agree on the scope. A small documentation fix can go straight to a PR.

For suspected vulnerabilities, follow [SECURITY.md](SECURITY.md), not a public issue or PR.
Do not include credentials or customer data in a security report's public discussion.

## Development setup

Use **Python 3.12**, matching [CI](.github/workflows/ci.yml), and Git. Use Linux, macOS, or WSL
with Bash for installer and deployment-script tests. Native Windows can run the Python helpers,
but Bash-dependent tests need a compatible shell; do not treat skipped shell tests as coverage.

Fork the repository on GitHub, clone your fork, and create a topic branch from current `main`.
From the repository root, the following commands use a Bash shell:

```bash
git clone https://github.com/<your-github-user>/finops-sre-agent-pack.git
cd finops-sre-agent-pack
git remote add upstream https://github.com/dmauser/finops-sre-agent-pack.git
git fetch upstream
git switch -c docs/contribution-example upstream/main
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

The test fixtures are synthetic and the tests run offline: no Azure sign-in, subscription, or
SRE Agent deployment is needed. Do not run deployment or installer scripts against a live agent
just to validate a contribution. If a change needs live validation, agree on an explicitly
authorized test environment and cost budget first; document cleanup and rollback steps.

## Reporting bugs and proposing improvements

Open an [issue](https://github.com/dmauser/finops-sre-agent-pack/issues/new) with a descriptive title
and enough information for someone else to reproduce the problem:

- The customer question or operational decision, and the affected skill, report, or task.
- The plugin version or commit, operating system, Python version, and relevant command.
- Expected versus actual behavior, with a minimal synthetic input and the resulting output or error.
- Relevant assumptions: requested and returned dates, currency, cost basis, managed-scope shape,
  permissions, and missing or partial sources. Use placeholders instead of real identifiers.
- For an enhancement, the proposed result and a measurable acceptance example, not just a feature name.

Useful scenarios include a report counting overlapping budgets twice, a missing historical period
being presented as complete, or unavailable commitment inventory being confused with zero usage.
Explain what the operator needs to distinguish and how a test would prove that distinction.
These are contribution ideas, not a claim that the current implementation handles every case.

## Customer scenarios and safe examples

**Use synthetic fixtures by default.** Do not commit raw billing exports, customer logs, live report
HTML, unredacted screenshots, access tokens, connection strings, or private operational backups.
Issues and PR attachments are public too.

If a customer case motivates a test, recreate the smallest relevant shape using invented values.
Replace subscription and tenant IDs, resource names and IDs, tags, owner emails, endpoints, and
commercial pricing details. Check nested JSON, URLs, screenshot text, and HTML metadata as well as
visible labels. Use consistent placeholders so scope relationships, pagination, duplicate rows,
and joins still exercise the bug. Use invented amounts and dates while preserving the relationships
the calculation needs, such as a refund's sign or a missing day.

State the expected result, units, and why the fixture represents the scenario. Keep helper-specific
cases beside the corresponding test in `tests/`; shared evaluation fixtures belong in
`evals/finops/fixtures/`. Public sample reports are generated from
[`docs/samples/generate_samples.py`](docs/samples/generate_samples.py), not copied from a live agent.
Sanitization alone is not permission to publish customer material.

## Improving skills, reports, and scheduled tasks

Keep changes focused, preferably on one report or customer scenario per PR. Follow existing
interfaces and reuse shared helpers rather than adding a second implementation of a calculation.

| Change | Related surfaces to update |
|---|---|
| Skill behavior or calculation | Its `SKILL.md`, deterministic Python helper, examples, and focused tests |
| Report model or presentation | `finops-report-renderer/models.py` and `render.py`, affected callers, report documentation, and synthetic samples |
| Scheduled behavior | Source prompts and `task-manifest.json`, then generated YAML; installer behavior if affected |
| Deployment or permissions | Deployment scripts/IaC, matching tests, onboarding instructions, and upgrade/rollback notes |

Preserve the read-only, advisory model and fail-closed managed-scope checks. Do not widen a scope,
grant write permissions, or turn a recommendation into an autonomous resource change. Keep proposals
for human execution separate from agent actions. Any approved live adjustment must also be represented
in the deployment scripts/IaC, not left as a portal-only fix.

Put calculations in deterministic helpers, not solely in prompts. Distinguish billed cost,
estimates, potential savings, and realized savings. Make date windows, cost basis, currency, missing
data, and partial coverage explicit. Preserve existing skill names, report titles, and task IDs unless
the change is deliberately breaking and includes migration guidance.

Report changes should use the existing structured models and `write_report`, preserve HTML escaping
and the CSP contract, and keep the pack's static reports free of view-time tool calls. Do not introduce
an alternate rendering stack for one report. Update [docs/REPORTS.md](docs/REPORTS.md) when thresholds,
interpretation, or warnings change, and refresh affected sample HTML and gallery images when needed.

Scheduled-task YAML is generated. Edit the sources under `plugins/finops/scheduled-tasks/`, then run:

```bash
python plugins/finops/scheduled-tasks/render_tasks.py write
python plugins/finops/scheduled-tasks/render_tasks.py check
```

Review the generated diff; do not hand-edit YAML that the renderer will overwrite.

## Testing expectations

Run focused tests while developing, then the offline suite before submitting behavioral changes.
For example:

```bash
python -m pytest tests/test_budget.py tests/test_report_renderer.py -q
python -m pytest tests/ -q
```

CI currently runs `pytest tests/ -v` on Python 3.12. For changes affecting shared data processing,
scope, renderer behavior, or scheduled prompts, also run the
[evaluation gates](evals/finops/README.md):

```bash
python evals/finops/run.py --with-tests
```

That command runs synthetic reconciliation, scope, HTML, performance, and prompt-size gates,
plus the test suite. A direct pytest run alone does not run every evaluation gate.

Add a regression test for each bug fix: first demonstrate that it fails with the original behavior,
then passes with the fix. For a new guard, temporarily remove or break the guarded behavior and
confirm the test fails; restore it before committing. Assert meaningful outputs such as totals,
scope boundaries, coverage status, and saved model shape, not just that a function returns.

Include applicable edge cases: empty input, missing permissions or sources, partial pages,
duplicate records, refunds, and incompatible dates, currencies, or cost bases. Never make a test
pass by replacing missing data with an unexplained zero or by weakening a safety check.

Documentation-only changes need only relevant documentation checks; for example, changelog edits
use `python -m pytest tests/test_changelog.py -q`. Check links and commands as well. If a check cannot
run, report the exact limitation in the PR rather than claiming validation.

## Submitting a pull request

Push your topic branch to your fork and open a PR against this repository's `main`. Link the issue,
describe the scenario and approach, and explain any intentional behavior change or compatibility
impact. Include sanitized before/after output or screenshots for report changes.

Before requesting review:

- [ ] Keep the diff focused; exclude credentials, customer data, local environments, and private artifacts.
- [ ] Add or update tests, and record the commands and results, including any skipped checks.
- [ ] Update affected skill instructions, generated task YAML, samples, and documentation.
- [ ] Add a user-facing changelog entry under `Unreleased` in the same commit.
- [ ] Describe scope, permissions, deployment, cost, and rollback implications when applicable.
- [ ] Confirm examples are synthetic and preserve attribution and license notices.

Maintainers review and merge changes. Contributors should not publish a release or change version
numbers unless the PR is specifically a coordinated release. Agents with auto-update enabled can
receive changes from `main`, so even prompt and installer changes deserve a clear impact statement.

## Changelog and releases

This repo follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). `tests/test_changelog.py` enforces the format.

**Every change** that affects users (skills, reports, installer, docs) adds a bullet under
`## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md), in the same commit, under one of these sections:
`Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`, or `Breaking`. Lead with a bold
summary, then say what changed and why it matters to someone reading a report.

**For maintainers cutting a release after reviewed changes reach `main`:**

1. Pick the version: PATCH for fixes only, MINOR for new skills, reports or behavior, MAJOR for breaking
   changes (for example a renamed skill, task id or report title).
2. In `CHANGELOG.md`, rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, add a new empty
   `## [Unreleased]` above it, and update the compare links at the bottom.
3. Set `"version"` in `plugins/finops/plugin.json` and `metadata.version` in
   `.github/plugin/marketplace.json` to `X.Y.Z`.
4. Run `pytest tests/`, commit, push to `main`, then tag and publish the release with that version's
   changelog section as the notes:

   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z
   gh release create vX.Y.Z --title "vX.Y.Z" --notes-file <section.md>
   ```

Agents installed with `autoUpdate: true` pick up `main` automatically; the tag and release record what
shipped and when.
