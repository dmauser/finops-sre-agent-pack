"""Keep CHANGELOG.md in Keep a Changelog + SemVer form and in sync with the manifests."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
REPO = "https://github.com/dmauser/finops-sre-agent-pack"
RELEASE = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})$", re.M)
SECTIONS = {"Added", "Changed", "Deprecated", "Removed", "Fixed", "Security", "Breaking"}


def _releases():
    return RELEASE.findall(CHANGELOG)


def _semver(version):
    return tuple(int(part) for part in version.split("."))


def test_header_declares_keep_a_changelog_and_semver():
    assert CHANGELOG.startswith("# Changelog\n")
    assert "https://keepachangelog.com/en/1.1.0/" in CHANGELOG
    assert "https://semver.org/spec/v2.0.0.html" in CHANGELOG


def test_unreleased_comes_before_every_release():
    headings = re.findall(r"^## (.+)$", CHANGELOG, re.M)
    assert headings[0] == "[Unreleased]"
    assert all(RELEASE.match(f"## {heading}") for heading in headings[1:])


def test_releases_are_newest_first_with_non_decreasing_dates():
    releases = _releases()
    assert releases, "at least one released version is required"
    versions = [_semver(version) for version, _ in releases]
    dates = [date for _, date in releases]
    assert versions == sorted(versions, reverse=True)
    assert len(set(versions)) == len(versions)
    assert dates == sorted(dates, reverse=True)


def test_only_keep_a_changelog_section_names_are_used():
    assert set(re.findall(r"^### (.+)$", CHANGELOG, re.M)) <= SECTIONS


def test_every_version_has_a_compare_link():
    releases = [version for version, _ in _releases()]
    latest = releases[0]
    assert f"[Unreleased]: {REPO}/compare/v{latest}...HEAD" in CHANGELOG
    for newer, older in zip(releases, releases[1:]):
        assert f"[{newer}]: {REPO}/compare/v{older}...v{newer}" in CHANGELOG
    assert f"[{releases[-1]}]: {REPO}/releases/tag/v{releases[-1]}" in CHANGELOG


def test_manifest_versions_match_latest_release():
    latest = _releases()[0][0]
    plugin = json.loads((ROOT / "plugins" / "finops" / "plugin.json").read_text())
    marketplace = json.loads(
        (ROOT / ".github" / "plugin" / "marketplace.json").read_text()
    )
    assert plugin["version"] == latest
    assert marketplace["metadata"]["version"] == latest
