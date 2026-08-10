# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Version consistency — the release-drift guard.

The 2026-08-08 incident: code was bumped to 0.4.0 but the tag was
never created, so GitHub Releases showed 0.3.0 while master ran 0.4.0.
These tests fail the suite whenever the version sources disagree, so a
version bump can't silently half-land again.
"""

import re
from pathlib import Path

import pytest

from threadweave.api import app

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"
CHANGELOG = REPO / "CHANGELOG.md"


def _pyproject_version() -> str:
    m = re.search(r'^version = "([^"]+)"', PYPROJECT.read_text(encoding="utf-8"),
                  re.M)
    assert m, "pyproject.toml has no version = line"
    return m.group(1)


def test_pyproject_matches_api_health():
    """The API reports the same version the package declares."""
    expected = _pyproject_version()
    assert app.version == expected, (
        f"api.py reports {app.version}, pyproject.toml says {expected}"
    )


def test_changelog_has_current_version_section():
    """The changelog must have an entry for the current version."""
    version = _pyproject_version()
    assert re.search(
        rf"^## \[{re.escape(version)}\]", CHANGELOG.read_text(encoding="utf-8"),
        re.M
    ), f"CHANGELOG.md has no [{version}] section"


def test_git_tag_exists_for_version():
    """The current version must have a tag (release drift guard).

    With the auto-release workflow the tag is created by CI on push,
    so an in-flight bump (version changed in the last commit) is
    accepted: the next push will tag it.
    """
    import subprocess

    version = _pyproject_version()
    result = subprocess.run(
        ["git", "tag", "-l", f"v{version}"],
        capture_output=True, text=True, check=False,
    )
    if result.stdout.strip() == f"v{version}":
        return  # tagged

    # No tag yet: allow if the version was just bumped (CI will tag
    # on push) — but fail if the version drifted without any change.
    prev = subprocess.run(
        ["git", "show", "HEAD~1:pyproject.toml"],
        capture_output=True, text=True, check=False,
    )
    if prev.returncode != 0:
        pytest.fail(f"no git tag v{version} and no previous version to "
                    "compare — code is at {version} but nothing tags it "
                    "(run: git tag -a v{version} && git push origin "
                    "v{version})")
    m = re.search(r'^version = "([^"]+)"', prev.stdout, re.M)
    prev_version = m.group(1) if m else ""
    if prev_version == version:
        pytest.fail(f"no git tag v{version} and version unchanged since "
                    "HEAD~1 — release drift (push to trigger the "
                    "auto-release workflow, or tag manually)")
    # version changed in the last commit: in-flight bump, CI will tag
