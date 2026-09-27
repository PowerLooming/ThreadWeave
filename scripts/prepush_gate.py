#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Pre-push hygiene gate: nothing leaves for a public remote by accident.

Two layers, separated by what each one is actually good at:

1. Deterministic rules. A leaked tenant id, API key or absolute home path is an
   exact string, and a regex cannot miss it once it is written. A classifier can
   only ever answer "0.83 probably", and a secret scan that misses one real
   identifier is worse than no scan at all. So the blocking layer is regexes,
   filename rules and an allowlist, in ``scripts/prepush_rules.toml`` (shapes
   only, safe to publish) plus a private overlay of site-specific terms that is
   never committed (default ``~/.threadweave/prepush-private.txt``).
2. Optional advisory layer (``--advisory``). The genuinely fuzzy rules, where a
   regex is the wrong tool: does this commit message or comment reference an
   internal document, name an organisation, or read as internal commentary. It
   asks the local NLI encoder from the typed decision layer, in one pass per
   state, and never blocks anything.

Scope: only the commits actually being pushed. ``git diff <base> <head>`` added
lines for an existing branch, the whole tree of the pushed commit when the
branch is new to that remote (because then the whole content is about to become
public), plus every commit message in the range.

Usage::

    python scripts/prepush_gate.py --stdin-refs          # from .githooks/pre-push
    python scripts/prepush_gate.py --all                 # everything not on origin
    python scripts/prepush_gate.py --all --advisory      # + encoder hygiene pass
    python scripts/prepush_gate.py --all --json          # machine readable

Exit codes: 0 clean (warnings allowed), 1 blocking findings, 2 the gate could not
run. The hook fails OPEN on 2: a broken seatbelt must not stop work, but it says
so loudly.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ZERO_SHA = "0" * 40
DEFAULT_RULES_PATH = "scripts/prepush_rules.toml"
PRIVATE_ENV = "THREADWEAVE_PREPUSH_PRIVATE"


class _NothingNew:
    """``resolve_base`` sentinel: this push publishes nothing new.

    Distinct from ``None``, which means the base is unknown and the whole tree
    is about to become public. A branch that is level with, or behind, its
    remote falls here: every commit it carries is already published, so there
    is no content to review.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "NOTHING_NEW"


NOTHING_NEW = _NothingNew()
ADVISORY_THRESHOLD_ENV = "THREADWEAVE_PREPUSH_ADVISORY_THRESHOLD"
LANGUAGE_MIN_CONFIDENCE_ENV = "THREADWEAVE_PREPUSH_LANGUAGE_MIN_CONFIDENCE"
ADVISORY_DEFAULT_THRESHOLD = 0.60
LANGUAGE_DEFAULT_MIN_CONFIDENCE = 0.25

COMMENT_PREFIXES = ("#", "//", "/*", "*", "<!--", "--", ">", ";", "%")
PROSE_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
# Matches at or below this length are printed as they are; see redact().
SHORT_MATCH_CHARS = 24
# A file may declare itself exempt: ``prepush-allow-file: *`` or a list of rule
# ids, as a comment line of its own. Terms from the private overlay always
# apply, so a real identifier in a fixture file is still caught.
FILE_MARKER = "prepush-allow-file"
MARKER_RE = re.compile(
    rf"^\s*(?:#|//|/\*|\*|<!--|--|;|%)?\s*{re.escape(FILE_MARKER)}\s*[:=]\s*"
    r"([A-Za-z0-9_*,\- ]*?)\s*(?:\*/|-->)?\s*$"
)


# ── data model ───────────────────────────────────────────────


@dataclass(frozen=True)
class Rule:
    """One deterministic check. ``scope`` is content, commit, path or any."""

    id: str
    pattern: str
    scope: str = "content"
    severity: str = "block"
    why: str = ""
    keep_full: bool = False
    source: str = "rules"

    def regex(self) -> re.Pattern[str]:
        return re.compile(self.pattern, re.IGNORECASE)


@dataclass
class Finding:
    rule_id: str
    severity: str
    scope: str
    target: str
    line: Optional[int]
    why: str
    excerpt: str
    source: str = "rules"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "scope": self.scope,
            "target": self.target,
            "line": self.line,
            "why": self.why,
            "excerpt": self.excerpt,
            "source": self.source,
        }


@dataclass
class Rules:
    rules: List[Rule] = field(default_factory=list)
    allow: List[re.Pattern[str]] = field(default_factory=list)
    skip_content: List[str] = field(default_factory=list)
    max_line_length: int = 1000
    show_matches: bool = False

    def for_scope(self, scope: str) -> List[Rule]:
        return [r for r in self.rules if r.scope in (scope, "any")]

    def suppressed(self, excerpt: str) -> bool:
        return any(a.search(excerpt) for a in self.allow)

    def skip_path(self, path: str) -> bool:
        base = path.rsplit("/", 1)[-1]
        return any(
            fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(base, pat)
            for pat in self.skip_content
        )


@dataclass
class Target:
    """One ref pair from the pre-push hook (or HEAD for a manual run)."""

    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str
    remote: str = "origin"


# ── git plumbing ─────────────────────────────────────────────


class GitError(RuntimeError):
    pass


def git(
    args: Sequence[str],
    *,
    repo: Optional[str] = None,
    binary: bool = False,
    check: bool = False,
) -> Any:
    cmd = ["git", "-c", "core.quotepath=false", *args]
    proc = subprocess.run(cmd, cwd=repo, capture_output=True)
    if check and proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {detail}")
    if binary:
        return proc.stdout
    return proc.stdout.decode("utf-8", "replace")


def commits_in_range(repo: Optional[str], base: Optional[str], head: str) -> List[str]:
    if base:
        out = git(["rev-list", f"{base}..{head}"], repo=repo)
    else:
        out = git(["rev-list", head], repo=repo)
    return [line.strip() for line in out.splitlines() if line.strip()]


def has_tracking_refs(repo: Optional[str], remote: str) -> bool:
    """Whether this remote has been fetched, so its refs can be excluded."""
    out = git(
        ["for-each-ref", "--count=1", "--format=%(refname)", f"refs/remotes/{remote}/"],
        repo=repo,
    )
    return bool(out.strip())


def resolve_base(
    repo: Optional[str], local_sha: str, remote_sha: str, remote: str
) -> "str | _NothingNew | None":
    """The commit the pushed content is compared against, or None for a full tree.

    Four cases, in order:

    1. The remote already has this branch: compare against that sha, which is
       what a normal push of new commits means.
    2. The branch is new there. If that remote has been fetched, exclude its
       refs and the base is the parent of the oldest commit it does not have,
       so pushing a fresh branch reviews the branch and not the repository.
       If the remote has never been fetched, fall back to every remote, because
       content already published elsewhere is not what is being published here.
    3. Nothing is published anywhere, so the whole tree is about to become
       public. That is the one case that warrants scanning all of it.
    4. Nothing this branch carries is new to that remote: a push of a branch
       that is level with or behind its remote, which is what a rejected
       non-fast-forward looks like from the hook. Returns ``NOTHING_NEW``
       rather than the ``None`` of case 3: scanning the tree here reported
       already-published history as if it were about to become public.
       Case 1 cannot catch it, because the remote's newer tip (a CI version
       bump, in the case that surfaced this) is not in the local repository
       yet, so there is no sha to diff against.
    """
    if remote_sha and remote_sha != ZERO_SHA:
        exists = subprocess.run(
            ["git", "cat-file", "-e", f"{remote_sha}^{{commit}}"],
            cwd=repo,
            capture_output=True,
        ).returncode == 0
        if exists:
            return remote_sha
    if has_tracking_refs(repo, remote):
        pushed = git(
            ["rev-list", local_sha, "--not", f"--remotes={remote}"], repo=repo
        ).split()
    else:
        pushed = git(["rev-list", local_sha, "--not", "--remotes"], repo=repo).split()
    if not pushed:
        return NOTHING_NEW
    oldest = pushed[-1]
    parents = git(["rev-list", "--parents", "-n", "1", oldest], repo=repo).split()
    return parents[1] if len(parents) > 1 else None


def read_tree(repo: Optional[str], sha: str) -> List[Tuple[str, str]]:
    """(path, text) for every text file in a tree, read in one cat-file batch."""
    names = [
        n for n in git(["ls-tree", "-r", "-z", "--name-only", sha], repo=repo).split("\0") if n
    ]
    if not names:
        return []
    proc = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=repo,
        input="\n".join(f"{sha}:{n}" for n in names).encode("utf-8"),
        capture_output=True,
    )
    out: List[Tuple[str, str]] = []
    stream = proc.stdout
    pos = 0
    for name in names:
        end = stream.find(b"\n", pos)
        if end < 0:
            break
        header = stream[pos:end].decode("utf-8", "replace")
        pos = end + 1
        if header.endswith("missing") or " missing" in header:
            continue
        parts = header.rsplit(" ", 2)
        try:
            size = int(parts[-1])
        except (ValueError, IndexError):
            continue
        blob = stream[pos : pos + size]
        pos += size + 1
        if b"\0" in blob:
            continue
        out.append((name, blob.decode("utf-8", "replace")))
    return out


def parse_added_lines(diff_text: str) -> Tuple[List[Tuple[str, int, str]], List[str]]:
    """(added lines as path/lineno/text, every changed path) out of a unified diff."""
    added: List[Tuple[str, int, str]] = []
    paths: List[str] = []
    path: Optional[str] = None
    lineno = 0
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            path = None
            continue
        if raw.startswith("+++ "):
            candidate = raw[4:].strip()
            if candidate.startswith('"') and candidate.endswith('"'):
                candidate = candidate[1:-1]
            if candidate == "/dev/null":
                path = None
            else:
                path = candidate[2:] if candidate.startswith("b/") else candidate
                if path not in paths:
                    paths.append(path)
            continue
        if raw.startswith("@@"):
            match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            lineno = int(match.group(1)) if match else 0
            continue
        if path is None:
            continue
        if raw.startswith("+"):
            added.append((path, lineno, raw[1:]))
            lineno += 1
        elif raw.startswith(" "):
            lineno += 1
    return added, paths


def commit_records(repo: Optional[str], base: Optional[str], head: str) -> List[Dict[str, str]]:
    args = ["log", "--format=%H%x1f%an%x1f%ae%x1f%B%x1e", head]
    if base:
        args = ["log", "--format=%H%x1f%an%x1f%ae%x1f%B%x1e", f"{base}..{head}"]
    raw = git(args, repo=repo)
    out: List[Dict[str, str]] = []
    for chunk in raw.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        parts = chunk.split("\x1f")
        if len(parts) < 4:
            continue
        out.append(
            {
                "sha": parts[0].strip(),
                "author": parts[1].strip(),
                "email": parts[2].strip(),
                "message": parts[3].strip(),
            }
        )
    return out


# ── rules loading ────────────────────────────────────────────


def load_rules(rules_path: Path, private_path: Optional[Path]) -> Rules:
    data = tomllib.loads(rules_path.read_text(encoding="utf-8"))
    settings = data.get("settings", {})
    rules = Rules(
        skip_content=list(settings.get("skip_content", [])),
        max_line_length=int(settings.get("max_line_length", 1000)),
    )
    for pattern in data.get("allow", {}).get("patterns", []):
        rules.allow.append(re.compile(pattern, re.IGNORECASE))
    for entry in data.get("rules", []):
        rules.rules.append(
            Rule(
                id=str(entry["id"]),
                pattern=str(entry["pattern"]),
                scope=str(entry.get("scope", "content")),
                severity=str(entry.get("severity", "block")),
                why=str(entry.get("why", "")),
                keep_full=bool(entry.get("keep_full", False)),
                source="rules",
            )
        )
    rules.rules.extend(load_private_rules(private_path))
    return rules


def load_private_rules(path: Optional[Path]) -> List[Rule]:
    """Site-specific terms from a file that is never committed.

    Line format: ``[block]`` / ``[warn]`` section headers, ``#`` comments, plain
    terms (matched literally, case-insensitively) and ``re:<regex>`` lines.
    """
    if path is None or not path.is_file():
        return []
    out: List[Rule] = []
    severity = "block"
    counters = {"block": 0, "warn": 0}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lowered = line.lower()
        if lowered in ("[block]", "[warn]"):
            severity = lowered.strip("[]")
            continue
        body = line
        if body.lower().startswith("re:"):
            pattern = body[3:].strip()
        elif body.lower().startswith("warn:"):
            severity = "warn"
            pattern = re.escape(body[5:].strip())
        elif body.lower().startswith("block:"):
            severity = "block"
            pattern = re.escape(body[6:].strip())
        else:
            pattern = re.escape(body)
        counters[severity] += 1
        out.append(
            Rule(
                id=f"private-{severity}-{counters[severity]}",
                pattern=pattern,
                scope="any",
                severity=severity,
                why="site-specific term that must not be published",
                keep_full=False,
                source="private",
            )
        )
    return out


def default_private_path() -> Optional[Path]:
    env = os.environ.get(PRIVATE_ENV)
    if env:
        return Path(env).expanduser()
    candidates = [
        Path.home() / ".threadweave" / "prepush-private.txt",
        Path.cwd() / ".prepush-private.txt",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


# ── scanning ─────────────────────────────────────────────────


def redact(text: str, keep_full: bool = False) -> str:
    """Shorten a match before printing it.

    Short matches are printed as they are, on purpose: a tenant alias or a
    mail address is only actionable if you can see which one it is, and this
    output goes to the machine of the person who wrote the line. Long matches
    are the ones that must never be echoed (keys, tokens, PEM blocks, DSNs),
    because they end up in screenshots and pasted logs, so those are cut to a
    prefix plus their length. ``--show-matches`` disables this.
    """
    clean = text.strip()
    if keep_full or len(clean) <= SHORT_MATCH_CHARS:
        return clean
    return f"{clean[:6]}…({len(clean)} chars)"


def file_marker(text: str) -> Optional[set[str]]:
    """Rule ids a file exempts itself from, or None.

    The directive is a whole comment line, ``prepush-allow-file: *`` for every
    rule in the published rules file or a comma separated list of rule ids.
    Overlay terms are never exempt, because that overlay is the protection that
    must hold everywhere.

    Matching is anchored to the whole line on purpose: prose *about* the marker
    (including this module's own comments and the FILE_MARKER constant) must not
    silently exempt the file that documents it.
    """
    for line in text.splitlines():
        match = MARKER_RE.match(line)
        if not match:
            continue
        ids = {part for part in re.split(r"[,\s]+", match.group(1)) if part}
        return ids or {"*"}
    return None


def read_file_at(repo: Optional[str], sha: str, path: str) -> str:
    return git(["show", f"{sha}:{path}"], repo=repo)


def scan_text(
    rules: Rules,
    text: str,
    *,
    scope: str,
    target: str,
    line: Optional[int] = None,
    inline_allow: bool = False,
) -> List[Finding]:
    findings: List[Finding] = []
    if "prepush-allow" in text and inline_allow:
        return findings
    if len(text) > rules.max_line_length:
        return findings
    for rule in rules.for_scope(scope):
        match = rule.regex().search(text)
        if not match:
            continue
        excerpt = match.group(0)
        if rules.suppressed(excerpt):
            continue
        findings.append(
            Finding(
                rule_id=rule.id,
                severity=rule.severity,
                scope=scope,
                target=target,
                line=line,
                why=rule.why,
                excerpt=excerpt if rules.show_matches else redact(excerpt, rule.keep_full),
                source=rule.source,
            )
        )
    return findings


def scan_target(
    rules: Rules,
    repo: Optional[str],
    target: Target,
) -> Tuple[List[Finding], Dict[str, Any]]:
    base = resolve_base(repo, target.local_sha, target.remote_sha, target.remote)
    if base is NOTHING_NEW:
        # Nothing this branch carries is new to that remote: no diff to read and
        # no tree to scan. Reporting findings here would flag already-published
        # history as if the push were about to publish it.
        return [], {
            "base": None,
            "head": target.local_sha,
            "mode": "none",
            # The ref line carries the remote *ref*; the pre-push hook does not
            # forward the remote name, so this is not necessarily a remote name.
            "remote_ref": target.remote_ref,
            "added_lines": 0,
            "files": 0,
            "commits": 0,
        }
    findings: List[Finding] = []
    stats: Dict[str, Any] = {
        "base": base,
        "head": target.local_sha,
        "mode": "diff" if base else "tree",
        "added_lines": 0,
        "files": 0,
        "commits": 0,
    }

    if base:
        diff = git(["diff", "--unified=0", "--no-color", "--no-ext-diff", base, target.local_sha], repo=repo)
        added, paths = parse_added_lines(diff)
        tree_text: Dict[str, str] = {}
    else:
        files = read_tree(repo, target.local_sha)
        tree_text = {path: text for path, text in files}
        added = []
        paths = []
        for path, text in files:
            paths.append(path)
            for lineno, line in enumerate(text.splitlines(), start=1):
                added.append((path, lineno, line))

    stats["added_lines"] = len(added)
    stats["files"] = len(paths)

    for path in paths:
        for finding in scan_text(rules, path, scope="path", target=path):
            findings.append(finding)

    marker_cache: Dict[str, Optional[set[str]]] = {}

    def exempt_for(path: str) -> Optional[set[str]]:
        if path not in marker_cache:
            text = tree_text.get(path)
            if text is None:
                text = read_file_at(repo, target.local_sha, path)
            marker_cache[path] = file_marker(text)
        return marker_cache[path]

    for path, lineno, text in added:
        if rules.skip_path(path):
            continue
        for finding in scan_text(
            rules, text, scope="content", target=path, line=lineno, inline_allow=True
        ):
            # A fixture file may exempt itself from the published rules, but
            # never from the private overlay.
            if finding.source == "rules":
                exempt = exempt_for(path)
                if exempt and ("*" in exempt or finding.rule_id in exempt):
                    continue
            findings.append(finding)

    records = commit_records(repo, base, target.local_sha)
    stats["commits"] = len(records)
    for record in records:
        for finding in scan_text(
            rules,
            record["message"],
            scope="commit",
            target=f"commit {record['sha'][:8]}",
            inline_allow=True,
        ):
            findings.append(finding)
        for finding in scan_text(
            rules,
            f"{record['author']} <{record['email']}>",
            scope="commit",
            target=f"author {record['sha'][:8]}",
        ):
            findings.append(finding)
    return findings, stats


# ── advisory layer (encode, never generate) ──────────────────


def advisory_texts(
    repo: Optional[str], base: Optional[str], head: str, rules: Rules
) -> List[Tuple[str, str]]:
    """(location, text) pairs worth a fuzzy judgment: messages, comments, prose."""
    out: List[Tuple[str, str]] = []
    for record in commit_records(repo, base, head):
        message = record["message"].strip()
        if len(message) >= 20:
            out.append((f"commit {record['sha'][:8]}", message))

    if base:
        diff = git(["diff", "--unified=0", "--no-color", "--no-ext-diff", base, head], repo=repo)
        added, _ = parse_added_lines(diff)
    else:
        added = []
        for path, text in read_tree(repo, head):
            for lineno, line in enumerate(text.splitlines(), start=1):
                added.append((path, lineno, line))

    buffer: Dict[str, List[str]] = {}
    for path, lineno, text in added:
        if rules.skip_path(path):
            continue
        stripped = text.strip()
        if not stripped:
            continue
        is_comment = stripped.startswith(COMMENT_PREFIXES)
        is_prose = path.endswith(PROSE_SUFFIXES)
        if not (is_comment or is_prose):
            continue
        if len(stripped) < 12:
            continue
        buffer.setdefault(path, []).append(stripped)
    for path, lines in buffer.items():
        joined = " ".join(lines)[:1500]
        if len(joined) >= 20:
            out.append((path, joined))
    return out


def run_advisory(
    repo: Optional[str],
    targets: List[Target],
    rules: Rules,
    threshold: float,
    language_min: float,
) -> Tuple[List[Finding], str]:
    """Encoder hygiene pass. Returns (advisory findings, note)."""
    os.environ.setdefault("THREADWEAVE_DECISION_PROVIDER", "encoder")
    repo_root = repo or os.getcwd()
    src = str(Path(repo_root) / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        from threadweave.decision_providers import get_decision_provider  # noqa: PLC0415
        from threadweave.decisions import DecisionAudit, DecisionEngine, Noul  # noqa: PLC0415
        from threadweave.language_id import identify  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - depends on the environment
        return [], f"advisory layer unavailable ({exc.__class__.__name__}: {exc})"

    provider = get_decision_provider()
    if provider is None or not provider.is_available():
        return [], "advisory layer unavailable (no encoder provider; install the decisions extra)"

    questions = {
        "names_organisation": Noul(
            instructions="Does this text name a specific customer, employer, or organisation?",
            statement="This text names a specific customer, employer, or organisation.",
        ),
        "internal_reference": Noul(
            instructions="Does this text refer to an internal document, plan, pilot, budget or meeting?",
            statement="This text refers to an internal document, plan, pilot, budget or meeting.",
        ),
        "internal_commentary": Noul(
            instructions=(
                "Is this text internal commentary about specific people, office politics or "
                "competitors rather than public documentation?"
            ),
            statement=(
                "This text is internal commentary about specific people, office politics or "
                "competitors rather than public documentation."
            ),
        ),
    }
    # The audit log is for ingest decisions; repo content does not belong in it.
    engine = DecisionEngine(provider, audit=DecisionAudit(enabled=False))

    findings: List[Finding] = []
    seen: set[str] = set()
    for target in targets:
        base = resolve_base(repo, target.local_sha, target.remote_sha, target.remote)
        if base is NOTHING_NEW:
            continue
        for location, text in advisory_texts(repo, base, target.local_sha, rules):
            key = f"{location}\x00{text[:80]}"
            if key in seen:
                continue
            seen.add(key)
            response = engine.ask(text, questions, purpose="prepush-hygiene")
            for qid in questions:
                answer = response.answers.get(qid)
                probability = getattr(answer, "noul", None)
                if probability is None or probability < threshold:
                    continue
                findings.append(
                    Finding(
                        rule_id=qid,
                        severity="warn",
                        scope="advisory",
                        target=location,
                        line=None,
                        why=f"encoder says {probability:.2f} (bar {threshold:.2f})",
                        excerpt=text[:70] + ("…" if len(text) > 70 else ""),
                        source="advisory",
                    )
                )
            guess = identify(text, min_confidence=language_min)
            language = getattr(guess, "language", None)
            confidence = float(getattr(guess, "confidence", 0.0) or 0.0)
            if language and not str(language).startswith("en") and confidence >= language_min:
                findings.append(
                    Finding(
                        rule_id="language",
                        severity="warn",
                        scope="advisory",
                        target=location,
                        line=None,
                        why=f"added text looks like {language} (conf {confidence:.2f})",
                        excerpt=text[:70] + ("…" if len(text) > 70 else ""),
                        source="advisory",
                    )
                )
    note = f"advisory: {len(seen)} states, provider {provider.describe()}"
    return findings, note


# ── reporting ────────────────────────────────────────────────


def render(findings: List[Finding], stats: List[Dict[str, Any]], width: int = 26) -> str:
    lines: List[str] = []
    for stat in stats:
        if stat.get("mode") == "none":
            lines.append(
                "prepush-gate: nothing new to publish ({head} is already on "
                "the remote)".format(head=str(stat.get("head"))[:8])
            )
            continue
        lines.append(
            "prepush-gate: {commits} commit(s), {files} file(s), {added_lines} added line(s) "
            "({mode} scan {base}..{head})".format(
                commits=stat.get("commits", 0),
                files=stat.get("files", 0),
                added_lines=stat.get("added_lines", 0),
                mode=stat.get("mode", "diff"),
                base=(stat.get("base") or "none")[:8],
                head=str(stat.get("head"))[:8],
            )
        )
    for finding in findings:
        location = finding.target
        if finding.line is not None:
            location = f"{location}:{finding.line}"
        label = f"{finding.severity.upper()} [{finding.scope}]"
        lines.append(
            f"{label:<16} {finding.rule_id:<{width}} {location}  {finding.why}  -> {finding.excerpt}"
        )
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prepush_gate",
        description="Deterministic pre-push hygiene gate (plus an optional encoder pass).",
    )
    parser.add_argument("--repo", default=None, help="repository path (default: cwd)")
    parser.add_argument(
        "--rules", default=None, help=f"rules file (default: {DEFAULT_RULES_PATH})"
    )
    parser.add_argument(
        "--private",
        default=None,
        help=f"private terms file (default: ${PRIVATE_ENV}, ~/.threadweave/prepush-private.txt, ./.prepush-private.txt)",
    )
    parser.add_argument(
        "--stdin-refs",
        action="store_true",
        help="read the pre-push ref lines from stdin (this is what the hook does)",
    )
    parser.add_argument(
        "--refs-file", default=None, help="read the pre-push ref lines from a file instead"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="scan everything on HEAD that is not on the remote (manual run)",
    )
    parser.add_argument("--advisory", action="store_true", help="run the encoder hygiene pass too")
    parser.add_argument(
        "--advisory-threshold",
        type=float,
        default=float(os.environ.get(ADVISORY_THRESHOLD_ENV, ADVISORY_DEFAULT_THRESHOLD)),
        help="probability above which an advisory question is reported",
    )
    parser.add_argument("--show-matches", action="store_true", help="print full matches (unsafe)")
    parser.add_argument("--json", action="store_true", help="emit findings as JSON")
    parser.add_argument("--list-rules", action="store_true", help="print the loaded rules and exit")
    parser.add_argument("--quiet", action="store_true", help="only report blocking findings")
    parser.add_argument("--verbose", action="store_true", help="extra detail on stderr")
    args = parser.parse_args(argv)

    repo_root = args.repo or os.getcwd()
    rules_file = Path(args.rules) if args.rules else Path(repo_root) / DEFAULT_RULES_PATH
    if not rules_file.is_file():
        print(f"prepush-gate: rules file not found: {rules_file}", file=sys.stderr)
        return 2
    private = Path(args.private).expanduser() if args.private else default_private_path()
    rules = load_rules(rules_file, private)
    rules.show_matches = bool(args.show_matches)

    if args.list_rules:
        print(f"rules file:  {rules_file}")
        print(f"private:     {private if private else '(none)'}")
        print(f"allow:       {len(rules.allow)} pattern(s)")
        print(f"skip:        {len(rules.skip_content)} path pattern(s)")
        for rule in rules.rules:
            display = rule.pattern
            if rule.source == "private":
                # The overlay holds real identifiers; do not spray them across
                # a terminal that people paste from.
                display = redact(re.sub(r"\\(.)", r"\1", rule.pattern), False)
            print(f"  {rule.severity:<5} {rule.scope:<7} {rule.id:<24} {display}")
        return 0

    targets: List[Target] = []
    if args.stdin_refs or args.refs_file:
        raw = (
            Path(args.refs_file).read_text(encoding="utf-8")
            if args.refs_file
            else sys.stdin.read()
        )
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            local_ref, local_sha, remote_ref, remote_sha = parts[:4]
            remote = remote_ref.split("/", 1)[0] if "/" in remote_ref else "origin"
            if local_sha == ZERO_SHA:
                continue  # branch deletion: nothing becomes public
            targets.append(Target(local_ref, local_sha, remote_ref, remote_sha, remote))
    elif args.all:
        head = git(["rev-parse", "HEAD"], repo=repo_root).strip()
        branch = git(["rev-parse", "--abbrev-ref", "HEAD"], repo=repo_root).strip()
        upstream = git(
            ["rev-parse", "--verify", "--quiet", f"origin/{branch}"], repo=repo_root
        ).strip()
        targets.append(
            Target(f"refs/heads/{branch}", head, f"refs/heads/{branch}", upstream or ZERO_SHA, "origin")
        )
    else:
        parser.error("one of --stdin-refs, --refs-file or --all is required")

    if not targets:
        if not args.quiet:
            print("prepush-gate: nothing to scan")
        return 0

    findings: List[Finding] = []
    stats: List[Dict[str, Any]] = []
    for target in targets:
        target_findings, target_stats = scan_target(rules, repo_root, target)
        findings.extend(target_findings)
        stats.append(target_stats)

    note = ""
    if args.advisory:
        advisory, note = run_advisory(
            repo_root,
            targets,
            rules,
            args.advisory_threshold,
            float(os.environ.get(LANGUAGE_MIN_CONFIDENCE_ENV, LANGUAGE_DEFAULT_MIN_CONFIDENCE)),
        )
        findings.extend(advisory)

    blocking = [f for f in findings if f.severity == "block"]
    warnings = [f for f in findings if f.severity != "block"]
    if args.quiet:
        findings = blocking

    if args.json:
        print(
            json.dumps(
                {
                    "findings": [f.as_dict() for f in findings],
                    "stats": stats,
                    "blocking": len(blocking),
                    "warnings": len(warnings),
                    "note": note,
                },
                indent=2,
            )
        )
    else:
        body = render(findings, stats)
        if body:
            print(body)
        if args.verbose and note:
            print(f"prepush-gate: {note}", file=sys.stderr)

    if blocking:
        print(
            f"\nprepush-gate: {len(blocking)} blocking finding(s), {len(warnings)} warning(s). "
            "Fix them, or bypass once with: git push --no-verify",
            file=sys.stderr,
        )
        return 1
    if warnings and not args.quiet:
        print(
            f"\nprepush-gate: {len(warnings)} warning(s), nothing blocking. Push allowed.",
            file=sys.stderr,
        )
    return 0


def cli() -> int:
    """Fail-open wrapper: a crash in the gate must not stop a push.

    Exit 2 means "could not run", which the hook turns into a loud allow.
    """
    try:
        return main()
    except GitError as exc:
        print(f"prepush-gate: could not run ({exc})", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive
        print(
            f"prepush-gate: internal error, allowing push ({exc.__class__.__name__}: {exc})",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(cli())
