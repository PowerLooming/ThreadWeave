# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""The release pipeline must not publish an empty release section.

2026-10-11, v0.4.26: the branch behind PR #22 was cut before the 0.4.24 and
0.4.25 bumps, so its author wrote the changelog entry under [Unreleased]. By the
time it merged, master had consumed [Unreleased] into a dated section and added
two more, and the textual three-way merge filed the branch's entry under
[0.4.24] instead: with both sides inserting lines, git places the branch's lines
next to the nearest matching context, which was a *released* section. Nothing
failed. No conflict, no failing test, CI green, and the release step took the
now-empty [Unreleased] as v0.4.26's notes. Releases publish zero-touch, so the
empty release was public before anyone read it (PR #23 re-filed the entry).

A repository cannot make git choose a different merge position, so the guard
lives where the consequence is: `scripts/changelog_release.py check` refuses to
let the pipeline tag a release whose section would be empty while code changed
since the last tag, and names the placement as the likely cause when the
changelog diff shows it. The first tests here reproduce the merge with real git
in a real repository and pin the misplacement, so a change in git's behaviour
(or a missing guard) is visible rather than silent.

Real repositories in tmp_path, real commits, real merges, real CLI runs: the
thing under test is what a clean merge does to a file, so a mocked git would
test nothing.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
GUARD = REPO_ROOT / "scripts" / "changelog_release.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "auto-tag-release.yml"

_spec = importlib.util.spec_from_file_location("changelog_release", GUARD)
assert _spec and _spec.loader
changelog_release = importlib.util.module_from_spec(_spec)
sys.modules["changelog_release"] = changelog_release
_spec.loader.exec_module(changelog_release)

# The branch point: [Unreleased] holds the entry of a release that has not
# happened yet, and 0.1.0 is the last released section.
BASE_CHANGELOG = """\
# Changelog

## [Unreleased]

### Fixed

- **Something already in flight.**
  Prose about it.

## [0.1.0] — 2026-01-01

### Fixed

- **An old fix.**
"""

# The same file, in the hand of a branch cut before the 0.1.1 and 0.1.2 bumps:
# the entry is correctly under [Unreleased].
BRANCH_CHANGELOG = """\
# Changelog

## [Unreleased]

### Fixed

- **The gate stops failing open.**
  A second line of prose.

- **Something already in flight.**
  Prose about it.

## [0.1.0] — 2026-01-01

### Fixed

- **An old fix.**
"""

# Master's two releases: the first consumes [Unreleased], the second ships empty
# (which is what the guard refuses), and [Unreleased] is empty either way.
MASTER_AFTER_0_1_1 = """\
# Changelog

## [Unreleased]

## [0.1.1] — 2026-01-02

### Fixed

- **Something already in flight.**
  Prose about it.

## [0.1.0] — 2026-01-01

### Fixed

- **An old fix.**
"""

MASTER_AFTER_0_1_2 = """\
# Changelog

## [Unreleased]

## [0.1.2] — 2026-01-03



## [0.1.1] — 2026-01-02

### Fixed

- **Something already in flight.**
  Prose about it.

## [0.1.0] — 2026-01-01

### Fixed

- **An old fix.**
"""

# PR #23's repair, as the guard's refusal describes it: the entry moves up under
# [Unreleased] and the released sections are back to what v0.1.2 published.
FIXED_CHANGELOG = """\
# Changelog

## [Unreleased]

### Fixed

- **The gate stops failing open.**
  A second line of prose.

## [0.1.2] — 2026-01-03



## [0.1.1] — 2026-01-02

### Fixed

- **Something already in flight.**
  Prose about it.

## [0.1.0] — 2026-01-01

### Fixed

- **An old fix.**
"""


def _git(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _write(repo: pathlib.Path, name: str, content: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _pre_fix_release_body(changelog: str) -> str:
    """The release step's own extraction before the guard existed.

    Copied out of `auto-tag-release.yml` as it stood at v0.4.26, so this test
    states what actually shipped an empty section rather than an approximation
    of it.
    """
    marker = "## [Unreleased]"
    assert marker in changelog
    _head, rest = changelog.split(marker, 1)
    match = re.search(r"\n## \[", rest)
    if match:
        return rest[: match.start()].rstrip("\n").strip()
    return rest.rstrip("\n").strip()


def _released_text(text: str) -> str:
    """Everything from the first dated heading on, the released sections."""
    match = re.search(r"^## \[\d", text, re.M)
    return text[match.start():] if match else ""


def run_script(
    cwd: pathlib.Path, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Run the guard CLI the way the release step does: as a script, no install."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    # Deterministic decoding of the em dash in a section heading on any host.
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(GUARD), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


@dataclass(frozen=True)
class Incident:
    """The v0.4.26 shape: a cut-before-the-bump branch, merged after two bumps."""

    path: pathlib.Path
    tag: str
    changelog_at_tag: str
    merged_changelog: str


@pytest.fixture()
def incident(tmp_path: pathlib.Path) -> Incident:
    repo = tmp_path / "threadweave"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for key, value in (
        ("user.email", "dev@example.com"),
        ("user.name", "Dev"),
        ("commit.gpgsign", "false"),
        ("core.autocrlf", "false"),
    ):
        _git(repo, "config", key, value)
    # The real repository pins LF for markdown (.gitattributes); without it a
    # Windows checkout would hand the guard CRLF and hide an empty section.
    _write(repo, ".gitattributes", "*.md text eol=lf\n")
    _write(repo, "CHANGELOG.md", BASE_CHANGELOG)
    _write(repo, "src/thing.py", "def thing():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base: before the 0.1.1 and 0.1.2 bumps")

    # Cut here. The entry is written where it belongs.
    _git(repo, "checkout", "-q", "-b", "fix/gate")
    _write(repo, "CHANGELOG.md", BRANCH_CHANGELOG)
    _write(repo, "src/thing.py", "def thing():\n    return 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fix: the gate stops failing open")

    # Master releases twice while the branch sits open.
    _git(repo, "checkout", "-q", "main")
    _write(repo, "CHANGELOG.md", MASTER_AFTER_0_1_1)
    _git(repo, "commit", "-q", "-am", "chore: bump version to v0.1.1 [skip ci]")
    _write(repo, "CHANGELOG.md", MASTER_AFTER_0_1_2)
    _git(repo, "commit", "-q", "-am", "chore: bump version to v0.1.2 [skip ci]")
    _git(repo, "tag", "-a", "v0.1.2", "-m", "Release v0.1.2")
    changelog_at_tag = (repo / "CHANGELOG.md").read_text(encoding="utf-8")

    merged = subprocess.run(
        ["git", "merge", "--no-edit", "fix/gate"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert "CONFLICT" not in merged.stdout

    return Incident(
        path=repo,
        tag="v0.1.2",
        changelog_at_tag=changelog_at_tag,
        merged_changelog=(repo / "CHANGELOG.md").read_text(encoding="utf-8"),
    )


# ── the merge that caused it ─────────────────────────────────


def test_the_merge_is_clean_and_the_entry_never_reaches_unreleased(incident):
    """Nothing is lost and nothing complains: the entry is filed wrong."""
    assert "The gate stops failing open" in incident.merged_changelog
    assert changelog_release.unreleased_body(incident.merged_changelog) == ""


def test_the_entry_lands_under_a_released_section(incident):
    """Pins git's merge behaviour.

    The branch and master both insert lines, so the three-way merge has no
    conflict to report and git places the branch's lines beside the nearest
    matching context, which is a section that already shipped. If a future git
    changes that, this test fails and the guard can be reconsidered instead of
    being trusted blindly.
    """
    misplaced = changelog_release.added_lines_under_dated_sections(
        incident.changelog_at_tag, incident.merged_changelog
    )
    assert {heading for heading, _ in misplaced} == {"## [0.1.1] — 2026-01-02"}
    texts = [line.strip() for _, line in misplaced if line.strip()]
    assert any("The gate stops failing open" in text for text in texts)
    assert any("A second line of prose." in text for text in texts)
    # The lines that were already in that section are not blamed for moving.
    assert not any("Something already in flight" in text for text in texts)


def test_the_pre_fix_extraction_would_have_published_an_empty_section(incident):
    """The bug, stated in the release step's own terms."""
    assert _pre_fix_release_body(incident.merged_changelog) == ""


def test_the_guard_refuses_the_merged_changelog_and_names_the_placement(incident):
    refusals, advisories = changelog_release.check(
        incident.merged_changelog,
        base_text=incident.changelog_at_tag,
        base_label=incident.tag,
    )
    assert len(refusals) == 1
    message = refusals[0]
    assert "the [Unreleased] section is empty" in message
    assert f"while code changed since {incident.tag}" in message
    assert "Most likely cause" in message
    assert "## [0.1.1] — 2026-01-02" in message
    assert "move those lines under [Unreleased]" in message
    # The same lines are what a maintainer sees first, as notes.
    assert advisories
    assert "## [0.1.1] — 2026-01-02" in advisories[0]


def test_the_cli_refuses_the_merge_with_exit_1_and_a_named_cause(incident):
    proc = run_script(
        incident.path, "check", "--changelog", "CHANGELOG.md",
        "--base-rev", incident.tag,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "refusing to publish this release" in proc.stderr
    assert "Most likely cause" in proc.stderr
    assert "## [0.1.1] — 2026-01-02" in proc.stderr
    assert "v0.1.2" in proc.stderr
    assert proc.stdout == ""  # a refusal is not reported as a pass


def test_the_documented_fix_makes_the_release_publishable(incident):
    """Moving the lines is the whole repair; nothing else has to change."""
    assert changelog_release.added_lines_under_dated_sections(
        incident.changelog_at_tag, FIXED_CHANGELOG
    ) == [], "the fixture must leave the released sections as v0.1.2 published them"
    _write(incident.path, "CHANGELOG.md", FIXED_CHANGELOG)
    _git(incident.path, "add", "-A")
    _git(incident.path, "commit", "-q", "-m",
         "docs(changelog): file the entry under the section it belongs to")

    proc = run_script(
        incident.path, "check", "--changelog", "CHANGELOG.md",
        "--base-rev", incident.tag,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "is ready to become a release" in proc.stdout


def test_a_misplaced_line_is_a_note_when_the_release_itself_is_fine(incident):
    """A publishable release can still carry lines a human must check.

    The v0.4.26 repair looks like this from the guard's side: lines added under
    a dated section. It must not block a release, and it must not be silent
    either, because that is the same signature as the fault.
    """
    text = FIXED_CHANGELOG.replace(
        "- **An old fix.**", "- **An old fix.**\n- **A late line under 0.1.0.**"
    )
    refusals, advisories = changelog_release.check(
        text, base_text=incident.changelog_at_tag, base_label=incident.tag
    )
    assert refusals == [], "an entry under [Unreleased] is enough to publish"
    assert len(advisories) == 1
    assert "## [0.1.0] — 2026-01-01" in advisories[0]
    assert "v0.1.2" in advisories[0]


# ── the CLI contract ─────────────────────────────────────────


def test_the_cli_runs_from_a_bare_checkout_without_the_package(tmp_path):
    """The release step has no `uv sync`: the guard must be stdlib only."""
    (tmp_path / "CHANGELOG.md").write_text(FIXED_CHANGELOG, encoding="utf-8")
    proc = run_script(
        tmp_path,
        "check",
        "--changelog",
        "CHANGELOG.md",
        extra_env={"PYTHONPATH": ""},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_cli_reports_a_missing_changelog_as_could_not_run(tmp_path):
    proc = run_script(tmp_path, "check", "--changelog", "CHANGELOG.md")
    assert proc.returncode == 2
    assert "does not exist" in proc.stderr


def test_the_cli_reports_an_unknown_base_revision_as_could_not_run(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(FIXED_CHANGELOG, encoding="utf-8")
    _git(tmp_path, "init", "-q", "-b", "main")
    proc = run_script(
        tmp_path, "check", "--changelog", "CHANGELOG.md", "--base-rev", "v9.9.9"
    )
    assert proc.returncode == 2
    assert "could not read CHANGELOG.md at v9.9.9" in proc.stderr


def test_the_cli_reports_a_changelog_without_the_section_as_could_not_run(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\nNothing here.\n",
                                           encoding="utf-8")
    proc = run_script(tmp_path, "check", "--changelog", "CHANGELOG.md")
    assert proc.returncode == 2
    assert "no `## [Unreleased]` section" in proc.stderr


def test_quiet_hides_the_notes_but_not_the_refusal(incident, tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(
        FIXED_CHANGELOG.replace(
            "- **An old fix.**", "- **An old fix.**\n- **A late line under 0.1.0.**"
        ),
        encoding="utf-8",
    )
    loud = run_script(
        tmp_path, "check", "--changelog", "CHANGELOG.md",
        "--repo", str(incident.path), "--base-rev", incident.tag,
    )
    quiet = run_script(
        tmp_path, "check", "--changelog", "CHANGELOG.md",
        "--repo", str(incident.path), "--base-rev", incident.tag, "--quiet",
    )
    assert loud.returncode == quiet.returncode == 0, loud.stderr + quiet.stderr
    assert "note:" in loud.stderr
    assert "note:" not in quiet.stderr

    refusing = run_script(
        incident.path, "check", "--changelog", "CHANGELOG.md",
        "--base-rev", incident.tag, "--quiet",
    )
    assert refusing.returncode == 1
    assert "refusing to publish this release" in refusing.stderr


# ── the manual-bump path ─────────────────────────────────────


def test_a_hand_written_release_section_must_have_content():
    empty = "# Changelog\n\n## [Unreleased]\n\n## [0.2.0] — 2026-01-04\n\n\n\n## [0.1.0] — 2026-01-01\n"
    refusals, _ = changelog_release.check(empty, version="0.2.0")
    assert len(refusals) == 1
    assert "the [0.2.0] section is empty" in refusals[0]
    assert "`## [0.2.0]`" in refusals[0]


def test_a_hand_written_release_section_with_content_passes():
    written = "# Changelog\n\n## [Unreleased]\n\n## [0.2.0] — 2026-01-04\n\n### Added\n\n- **A feature.**\n\n## [0.1.0] — 2026-01-01\n"
    refusals, advisories = changelog_release.check(written, version="0.2.0")
    assert (refusals, advisories) == ([], [])


def test_a_missing_hand_written_section_is_refused(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(FIXED_CHANGELOG, encoding="utf-8")
    proc = run_script(
        tmp_path, "check", "--changelog", "CHANGELOG.md", "--version", "0.2.0"
    )
    assert proc.returncode == 1
    assert "no `## [0.2.0]` section" in proc.stderr


# ── what "the release section" means ─────────────────────────


def test_unreleased_body_is_the_slice_the_release_notes_come_from():
    text = "# Changelog\n\n## [Unreleased]\n\n- one\n\n\n## [1.0.0] — 2026-01-01\n\n- old\n"
    assert changelog_release.unreleased_body(text) == "- one"
    # The extraction was replaced, not redefined.
    assert changelog_release.unreleased_body(text) == _pre_fix_release_body(text)


def test_a_whitespace_only_section_is_empty():
    text = "## [Unreleased]\n\n   \n\t\n\n## [1.0.0] — 2026-01-01\n\n- old\n"
    assert changelog_release.unreleased_body(text) == ""
    assert changelog_release.check(text)[0] != []


def test_a_crlf_checkout_is_judged_the_same():
    """A Windows checkout must not hide an empty section behind carriage returns."""
    text = "# Changelog\r\n\r\n## [Unreleased]\r\n\r\n## [1.0.0] — 2026-01-01\r\n\r\n- old\r\n"
    assert changelog_release.unreleased_body(text) == ""
    assert changelog_release.check(text)[0] != []
    assert changelog_release.dated_section_body(text, "1.0.0") == "- old"


def test_migrate_files_the_section_exactly_as_the_release_step_did():
    after = changelog_release.migrate(BASE_CHANGELOG, "0.1.1", "2026-01-02")
    assert after == (
        "# Changelog\n\n## [Unreleased]\n\n## [0.1.1] — 2026-01-02\n\n"
        "### Fixed\n\n- **Something already in flight.**\n  Prose about it.\n\n"
        "## [0.1.0] — 2026-01-01\n\n### Fixed\n\n- **An old fix.**\n"
    )


def test_migrate_leaves_every_released_section_untouched():
    after = changelog_release.migrate(BASE_CHANGELOG, "0.1.1", "2026-01-02")
    released = _released_text(BASE_CHANGELOG)
    # The published sections are still there, byte for byte, and still last.
    assert after.endswith(released)
    assert after.count(released) == 1
    assert changelog_release.unreleased_body(after) == ""


def test_migrate_normalizes_a_crlf_changelog_to_lf():
    after = changelog_release.migrate(
        BASE_CHANGELOG.replace("\n", "\r\n"), "0.1.1", "2026-01-02"
    )
    assert "\r" not in after
    assert after == changelog_release.migrate(BASE_CHANGELOG, "0.1.1", "2026-01-02")


def test_apply_cli_writes_lf_and_files_the_section(tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(BASE_CHANGELOG, encoding="utf-8")
    proc = run_script(
        tmp_path, "apply", "--changelog", "CHANGELOG.md",
        "--version", "0.1.1", "--date", "2026-01-02",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    raw = (tmp_path / "CHANGELOG.md").read_bytes()
    assert b"\r" not in raw
    text = raw.decode("utf-8")
    assert changelog_release.unreleased_body(text) == ""
    assert changelog_release.dated_section_body(text, "0.1.1") == (
        "### Fixed\n\n- **Something already in flight.**\n  Prose about it."
    )


@pytest.mark.parametrize(
    "args",
    [
        ("--version", "0.5", "--date", "2026-01-02"),
        ("--version", "0.1.1", "--date", "yesterday"),
    ],
)
def test_apply_rejects_a_malformed_version_or_date(tmp_path, args):
    (tmp_path / "CHANGELOG.md").write_text(BASE_CHANGELOG, encoding="utf-8")
    proc = run_script(tmp_path, "apply", "--changelog", "CHANGELOG.md", *args)
    assert proc.returncode == 2
    assert "error:" in proc.stderr


# ── the release workflow's wiring ────────────────────────────


def test_the_workflow_runs_the_guard_before_it_commits_the_bump():
    text = WORKFLOW.read_text(encoding="utf-8")
    guard = text.index("scripts/changelog_release.py check")
    bump = text.index('git commit -m "chore: bump version to v$VERSION [skip ci]"')
    # Before the bump commit, so a refusal leaves master and the tags untouched.
    assert guard < bump
    step = text[
        text.index("- name: Refuse to publish an empty release"):
        text.index("- name: Auto-increment patch version")
    ]
    assert "if: steps.code.outputs.has_code == 'true'" in step
    assert 'BASE="--base-rev ${{ steps.code.outputs.latest }}"' in step
    assert 'SECTION="--version ${{ steps.version.outputs.version }}"' in step
    assert (
        "python3 scripts/changelog_release.py check --changelog CHANGELOG.md "
        "$BASE $SECTION" in step
    )


def test_the_workflow_files_the_section_through_the_guarded_module():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "changelog_release.migrate(" in text
    # The old inline extraction is gone: the section the guard approved is the
    # section that gets filed, because there is one implementation of it.
    assert 'marker = "## [Unreleased]"' not in text
    assert 'newline="\\n"' in text


def test_the_workflow_is_valid_yaml_with_the_guard_in_place():
    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    names = [step.get("name") for step in document["jobs"]["auto-release"]["steps"]]
    assert "Refuse to publish an empty release" in names
    assert names.index("Refuse to publish an empty release") < names.index(
        "Auto-increment patch version"
    )
    assert "Tag the release" in names


def test_the_inline_release_python_compiles():
    """The release step is python inside YAML, published zero-touch: a syntax
    error there would only surface at release time. Compile it here instead."""
    text = WORKFLOW.read_text(encoding="utf-8")
    body = text.split("python - <<'EOF'", 1)[1].split("\n          EOF\n", 1)[0]
    compile(textwrap.dedent(body), "auto-tag-release.yml", "exec")
