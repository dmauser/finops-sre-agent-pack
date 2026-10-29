# Contributing

## Changelog and releases

This repo follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). `tests/test_changelog.py` enforces the format.

**Every change** that affects users (skills, reports, installer, docs) adds a bullet under
`## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md), in the same commit, under one of these sections:
`Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`, or `Breaking`. Lead with a bold
summary, then say what changed and why it matters to someone reading a report.

**To cut a release:**

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
