#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Changelog handling for the release pipeline: check a release is publishable,
and migrate the [Unreleased] section into the release it becomes.

Background (2026-10-11, v0.4.26): a branch cut before a version bump carries an
[Unreleased] entry. By the time it merges, master has consumed [Unreleased] into
a dated section and added new ones. The textual three-way merge has nothing to
conflict about: the branch's added lines are placed next to the nearest matching
context, which is a *released* section. The merge is clean, CI is green, and the
release step, which takes everything between `## [Unreleased]` and the next
`## [` heading as the release notes, publishes an empty section. v0.4.26 shipped
empty that way (PR #22; the entry was re-filed by PR #23).

`check` refuses that release before anything is tagged: an empty [Unreleased]
section on a push that changed code means the next release publishes nothing.
When the changelog diff against the last tag shows lines added under a dated
section, the refusal names that placement as the likely cause and says where the
entry belongs. `apply` is the same migration the workflow used to run inline, so
the check and the migration cannot disagree about what "the release section"
means.

Stdlib only, no imports from the package: this runs before anything is installed
and from a plain checkout.
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import pathlib
import re
import subprocess
import sys

UNRELEASED = "## [Unreleased]"
_HEADING = re.compile(r"^## \[", re.M)
_DATED_HEADING = re.compile(r"^## \[\d+\.\d+\.\d+\]")

READY = 0
REFUSED = 1
COULD_NOT_RUN = 2


class ChangelogError(Exception):
    """The changelog cannot be read as a changelog (exit 2, not a refusal)."""


def _lf(text: str) -> str:
    """Line endings normalized to LF.

    The changelog is read from whatever the release workflow checked out. On a
    Windows checkout with core.autocrlf that is CRLF, and a section of nothing
    but line endings would then report content (`"\\r\\r"`), which is exactly the
    "is this release empty" question this module exists to answer.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def unreleased_body(text: str) -> str:
    """The [Unreleased] section's body: its text up to the next `## [` heading.

    This is exactly the slice the release notes are built from, so an empty
    result is an empty release.
    """
    text = _lf(text)
    if UNRELEASED not in text:
        raise ChangelogError("the changelog has no `## [Unreleased]` section")
    rest = text.split(UNRELEASED, 1)[1]
    match = _HEADING.search(rest)
    body = rest if match is None else rest[: match.start()]
    return body.strip()


def migrate(text: str, version: str, date: str) -> str:
    """Move the [Unreleased] body into a new dated section, leaving it empty.

    Byte for byte what the release workflow did inline before this module
    existed: `[Unreleased]`, blank line, `## [version] — date`, blank line, the
    section body, blank line, then the released sections untouched. The result is
    LF, whatever the input used.
    """
    text = _lf(text)
    body = unreleased_body(text)
    head, rest = text.split(UNRELEASED, 1)
    match = _HEADING.search(rest)
    tail = "" if match is None else rest[match.start():]
    release_heading = (
        f"{UNRELEASED}\n\n"
        f"## [{version}] \u2014 {date}\n\n"
        f"{body}\n\n"
    )
    return head + release_heading + tail.lstrip("\n")


def _dated_section_per_line(lines: list[str]) -> list[str | None]:
    """For each line, the dated heading it sits under, or None.

    Lines above the first dated heading (the title and [Unreleased] itself) map
    to None, which is what makes "added under a released section" decidable
    without parsing the changelog's structure.
    """
    current: str | None = None
    sections: list[str | None] = []
    for line in lines:
        if line.startswith("## ["):
            current = line if _DATED_HEADING.match(line) else None
        sections.append(current)
    return sections


def added_lines_under_dated_sections(
    base_text: str, head_text: str
) -> list[tuple[str, str]]:
    """(dated heading, added line) for every line added inside a released section.

    Between two tags the only changelog section that may legitimately gain lines
    is [Unreleased]. Lines that appear under a dated heading are the misplaced
    entry signature from the v0.4.26 incident, and they are invisible otherwise:
    the file still parses, the section still has content, the merge is clean.
    """
    base_lines = _lf(base_text).splitlines()
    head_lines = _lf(head_text).splitlines()
    sections = _dated_section_per_line(head_lines)
    added: list[tuple[str, str]] = []
    matcher = difflib.SequenceMatcher(a=base_lines, b=head_lines, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag not in ("insert", "replace"):
            continue
        for j in range(j1, j2):
            if sections[j] is not None:
                added.append((sections[j], head_lines[j]))
    return added


def dated_section_body(text: str, version: str) -> str | None:
    """The body of `## [version]`, or None when there is no such section."""
    text = _lf(text)
    match = re.search(rf"^## \[{re.escape(version)}\][^\n]*$", text, re.M)
    if match is None:
        return None
    rest = text[match.end():]
    following = _HEADING.search(rest)
    body = rest if following is None else rest[: following.start()]
    return body.strip()


def check(
    changelog_text: str,
    *,
    base_text: str | None = None,
    base_label: str = "the last release tag",
    version: str | None = None,
) -> tuple[list[str], list[str]]:
    """Judge whether this changelog is ready to become a release.

    The section this push publishes is [Unreleased]'s body, because the release
    step files that body under a new dated heading. With `version` the published
    section is `## [version]` itself: that is the manual-bump path, where the
    maintainer wrote the heading by hand, and an empty one publishes an empty
    release just the same.

    Returns (refusals, advisories). Refusals are fatal. Advisories are things a
    maintainer should see, but that repairing an already-published section may
    legitimately look like.
    """
    refusals: list[str] = []
    advisories: list[str] = []

    if version:
        body = dated_section_body(changelog_text, version)
        subject = f"the [{version}] section"
        destination = f"`## [{version}]`"
        missing = body is None
    else:
        body = unreleased_body(changelog_text)
        subject = "the [Unreleased] section"
        destination = "[Unreleased]"
        missing = False

    misplaced = (
        added_lines_under_dated_sections(base_text, changelog_text)
        if base_text is not None
        else []
    )

    def diagnosis() -> str:
        """The placed-in-the-wrong-section explanation, when the diff shows one."""
        if not misplaced:
            return ""
        headings = "/".join(sorted({heading for heading, _ in misplaced}))
        lines = sum(1 for _, line in misplaced if line.strip())
        return (
            f"\n  Most likely cause: {lines} changelog line(s) added since "
            f"{base_label} landed under {headings} instead of {destination}. That "
            f"is what a merge of a branch cut before a version bump does: master's "
            f"new dated sections give the branch's added entry a released section "
            f"to sit in, the merge reports no conflict, and the entry is silently "
            f"attributed to a release that already shipped (v0.4.26, PR #22)."
            f"\n  Fix: move those lines under {destination} in CHANGELOG.md and push "
            f"again; the release step reads that section verbatim."
        )

    if missing:
        refusals.append(
            f"the changelog has no `## [{version}]` section, so this push would "
            f"tag v{version} with no release notes at all."
            f"{diagnosis()}"
            f"\n  Fix: add `## [{version}] — <date>` to CHANGELOG.md with this "
            f"release's entries, and push again."
        )
    elif not body:
        generic_fix = (
            f"\n  Fix: write the user-visible change under {destination} in "
            f"CHANGELOG.md, because that section becomes the release "
            f"announcement verbatim, and push again. A release that ships code "
            f"and says nothing is what this guard exists to prevent; if the "
            f"change genuinely has nothing to say, say that."
        )
        refusals.append(
            f"{subject} is empty, so the release this push publishes would have "
            f"an empty section while code changed since {base_label}."
            f"{diagnosis() or generic_fix}"
        )

    by_heading: dict[str, list[str]] = {}
    for heading, line in misplaced:
        if line.strip():
            by_heading.setdefault(heading, []).append(line.strip())
    for heading, lines in sorted(by_heading.items()):
        advisories.append(
            f"{len(lines)} changelog line(s) sit under the released section "
            f"{heading} and were not there at {base_label}: {lines[0][:80]}"
        )

    return refusals, advisories


def read_changelog_base(repo: str, rev: str, path: str) -> str:
    """The changelog as of `rev`, via `git show`, or a ChangelogError."""
    result = subprocess.run(
        ["git", "-C", repo, "show", f"{rev}:{path}"],
        capture_output=True,
        text=True,
        # The changelog is UTF-8 and git returns it as stored: decoding with the
        # host's locale would turn every em dash in a section heading into
        # mojibake, and the diagnostic's whole job is to name that heading.
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "no such revision"
        raise ChangelogError(
            f"could not read {path} at {rev} ({detail}): the placement check "
            f"needs the changelog as it was at the last release"
        )
    return result.stdout


def _cmd_check(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.changelog)
    if not path.is_file():
        print(f"error: {path} does not exist", file=sys.stderr)
        return COULD_NOT_RUN
    text = path.read_text(encoding="utf-8")
    base_text = None
    base_label = "the last release tag"
    if args.base_rev:
        try:
            base_text = read_changelog_base(
                args.repo, args.base_rev, args.changelog
            )
        except ChangelogError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return COULD_NOT_RUN
        base_label = args.base_rev

    try:
        refusals, advisories = check(
            text,
            base_text=base_text,
            base_label=base_label,
            version=args.version or None,
        )
    except ChangelogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return COULD_NOT_RUN

    if not args.quiet:
        for advisory in advisories:
            print(f"note: {advisory}", file=sys.stderr)
    if refusals:
        print("refusing to publish this release:", file=sys.stderr)
        for refusal in refusals:
            print(f"  {refusal}", file=sys.stderr)
        return REFUSED
    print(f"{args.changelog} is ready to become a release")
    return READY


def _cmd_apply(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.changelog)
    if not path.is_file():
        print(f"error: {path} does not exist", file=sys.stderr)
        return COULD_NOT_RUN
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version or ""):
        print(f"error: --version {args.version!r} is not X.Y.Z", file=sys.stderr)
        return COULD_NOT_RUN
    date = args.date or datetime.datetime.now(
        datetime.timezone.utc
    ).date().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        print(f"error: --date {date!r} is not YYYY-MM-DD", file=sys.stderr)
        return COULD_NOT_RUN

    text = path.read_text(encoding="utf-8")
    try:
        migrated = migrate(text, args.version, date)
    except ChangelogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return COULD_NOT_RUN
    # newline="\n" keeps the changelog LF on a Windows checkout too; .gitattributes
    # normalizes on commit, but the file the release notes are read from should
    # not depend on which host ran the migration.
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(migrated)
    print(f"filed the [Unreleased] section as [{args.version}] — {date}")
    return READY


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    check_parser = sub.add_parser(
        "check",
        help="refuse a release whose section would be empty while code changed",
    )
    check_parser.add_argument("--changelog", default="CHANGELOG.md")
    check_parser.add_argument(
        "--base-rev",
        default="",
        help="the last release tag: enables the placement diagnostic",
    )
    check_parser.add_argument("--repo", default=".")
    check_parser.add_argument(
        "--version",
        default="",
        help=(
            "the version this push tags when the maintainer bumped it by hand: "
            "the published section is `## [version]` instead of [Unreleased]"
        ),
    )
    check_parser.add_argument("--quiet", action="store_true")
    check_parser.set_defaults(func=_cmd_check)

    apply_parser = sub.add_parser(
        "apply", help="file the [Unreleased] section as the new release"
    )
    apply_parser.add_argument("--changelog", default="CHANGELOG.md")
    apply_parser.add_argument("--version", required=True)
    apply_parser.add_argument("--date", default="")
    apply_parser.set_defaults(func=_cmd_apply)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
