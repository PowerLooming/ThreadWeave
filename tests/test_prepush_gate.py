# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for the pre-push hygiene gate (scripts/prepush_gate.py).

Real repositories in tmp_path, real commits, real CLI invocation: the gate's
whole value is that it sees what a push would make public, so a mocked git would
test nothing. The rules file under test is the shipped one, and every site
specific term is injected through a temporary private overlay.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

# prepush-allow-file: *
# This file is the scanner's own fixture set, so it deliberately contains fake
# credentials and identifiers. Every fixture term is fictional (acme, nordvik);
# terms from the private overlay are never exempt, and real ones must never
# appear here.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "scripts" / "prepush_gate.py"
RULES = REPO_ROOT / "scripts" / "prepush_rules.toml"

_spec = importlib.util.spec_from_file_location("prepush_gate", GATE)
assert _spec and _spec.loader
prepush = importlib.util.module_from_spec(_spec)
# dataclass processing resolves annotations through sys.modules, so the module
# has to be registered before it is executed.
sys.modules["prepush_gate"] = prepush
_spec.loader.exec_module(prepush)


def _git(repo: pathlib.Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _write(repo: pathlib.Path, name: str, content: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture()
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A git repository with one clean commit and no remote."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "dev@example.com")
    _git(tmp_path, "config", "user.name", "Dev")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    _write(tmp_path, "README.md", "# clean\n\nNothing to see here.\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "Initial commit")
    return tmp_path


@pytest.fixture()
def private_file(tmp_path_factory):
    """A private terms file OUTSIDE the repository under test.

    Inside it, ``git add -A`` would commit the terms file itself and the gate
    would (correctly) report the terms as leaked content.
    """

    def _make(body: str) -> pathlib.Path:
        base = tmp_path_factory.mktemp("private")
        path = base / "prepush-private.txt"
        path.write_text(body, encoding="utf-8")
        return path

    return _make


def run_gate(
    repo: pathlib.Path,
    *extra: str,
    private: pathlib.Path | None = None,
    json_out: bool = True,
):
    env = dict(os.environ)
    env["THREADWEAVE_PREPUSH_PRIVATE"] = str(private) if private else str(repo / "absent.txt")
    env.pop("THREADWEAVE_PREPUSH_ADVISORY", None)
    cmd = [
        sys.executable,
        str(GATE),
        "--repo",
        str(repo),
        "--rules",
        str(RULES),
        *extra,
    ]
    if json_out:
        cmd.append("--json")
    return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(repo))


def findings_of(proc: subprocess.CompletedProcess, severity: str | None = None) -> list[dict]:
    payload = json.loads(proc.stdout)
    items = payload["findings"]
    if severity:
        items = [f for f in items if f["severity"] == severity]
    return items


def commit(repo: pathlib.Path, name: str, content: str, message: str = "Add file") -> None:
    _write(repo, name, content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


# ── clean input ──────────────────────────────────────────────


def test_clean_repository_passes(repo):
    proc = run_gate(repo, "--all")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert findings_of(proc) == []


def test_clean_addition_passes(repo):
    commit(repo, "src/thing.py", "def add(a, b):\n    return a + b\n")
    proc = run_gate(repo, "--all")
    assert proc.returncode == 0
    assert findings_of(proc) == []


# ── deterministic content rules ──────────────────────────────


def test_tenant_domain_blocks(repo):
    commit(repo, "docs/notes.md", "Tenant: https://acme-tenant.onmicrosoft.com\n")
    proc = run_gate(repo, "--all")
    assert proc.returncode == 1
    ids = [f["rule_id"] for f in findings_of(proc, "block")]
    assert "tenant-domain" in ids


def test_api_key_shape_blocks(repo):
    commit(repo, "tests/fixture.py", 'KEY = "sk-proj-A1b2C3d4E5f6G7h8I9j0K1l2"\n')
    proc = run_gate(repo, "--all")
    assert proc.returncode == 1
    blocks = findings_of(proc, "block")
    assert "openai-key" in [f["rule_id"] for f in blocks]
    # A long credential is never echoed unless the caller insists.
    excerpt = next(f["excerpt"] for f in blocks if f["rule_id"] == "openai-key")
    assert excerpt.startswith("sk-pro")
    assert "A1b2C3d4" not in excerpt
    shown = run_gate(repo, "--all", "--show-matches", json_out=False)
    assert "A1b2C3d4E5f6G7h8I9j0K1l2" in shown.stdout


def test_windows_user_path_blocks(repo):
    commit(repo, "docs/setup.md", "Config lives in C:\\Users\\Harald\\.threadweave\n")
    proc = run_gate(repo, "--all")
    assert proc.returncode == 1
    assert "windows-user-path" in [f["rule_id"] for f in findings_of(proc, "block")]


def test_placeholder_keys_are_allowed(repo):
    commit(
        repo,
        ".env.example",
        "OPENAI_API_KEY=sk-test-0000000000000000000000\nHOME=C:\\Users\\<user>\\app\n",
    )
    proc = run_gate(repo, "--all")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert findings_of(proc, "block") == []


def test_dsn_with_password_blocks(repo):
    commit(repo, "docker-compose.yml", "PG=postgres://weave:hunter2sekret@db:5432/weave\n")
    proc = run_gate(repo, "--all")
    assert proc.returncode == 1
    assert "dsn-with-password" in [f["rule_id"] for f in findings_of(proc, "block")]


# ── path rules ───────────────────────────────────────────────


def test_env_file_blocks(repo):
    commit(repo, ".env", "THREADWEAVE_API_KEY=realvalue\n")
    proc = run_gate(repo, "--all")
    assert proc.returncode == 1
    assert "env-file" in [f["rule_id"] for f in findings_of(proc, "block")]


def test_credentials_json_blocks(repo):
    commit(repo, "config/keys.json", '{"keys": [{"key": "real"}]}\n')
    proc = run_gate(repo, "--all")
    assert proc.returncode == 1
    assert "local-key-store" in [f["rule_id"] for f in findings_of(proc, "block")]


def test_sqlite_store_warns_and_blocks(repo):
    commit(repo, "data/entries.sqlite3", "not really a database\n")
    proc = run_gate(repo, "--all")
    assert proc.returncode == 1
    assert "local-database" in [f["rule_id"] for f in findings_of(proc, "block")]


# ── private overlay ──────────────────────────────────────────


def test_private_terms_block_and_are_readable(repo, private_file):
    private = private_file("[block]\nnordvik\n\n[warn]\ninternal-notes\n")
    commit(repo, "docs/pilot.md", "Pilot tenant is nordvik, notes in internal-notes.\n")
    proc = run_gate(repo, "--all", private=private)
    assert proc.returncode == 1
    blocks = findings_of(proc, "block")
    assert [f["rule_id"] for f in blocks] == ["private-block-1"]
    # A short term is echoed, because otherwise the report cannot be acted on.
    assert blocks[0]["excerpt"] == "nordvik"
    assert blocks[0]["target"] == "docs/pilot.md"


def test_private_term_shown_with_show_matches(repo, private_file):
    private = private_file("[block]\nnordvik\n")
    commit(repo, "docs/pilot.md", "Pilot tenant is nordvik.\n")
    proc = run_gate(repo, "--all", "--show-matches", private=private, json_out=False)
    assert proc.returncode == 1
    assert "nordvik" in proc.stdout


def test_private_warn_does_not_block(repo, private_file):
    private = private_file("[warn]\ninternal-notes\n")
    commit(repo, "docs/pilot.md", "Notes live in internal-notes.\n")
    proc = run_gate(repo, "--all", private=private)
    assert proc.returncode == 0
    assert findings_of(proc, "block") == []


def test_private_regex_term(repo, private_file):
    private = private_file("[block]\nre:tenant[0-9]{6}\n")
    commit(repo, "docs/notes.md", "See tenant998877 for the pilot.\n")
    proc = run_gate(repo, "--all", private=private)
    assert proc.returncode == 1
    assert "private-block-1" in [f["rule_id"] for f in findings_of(proc, "block")]


def test_missing_private_file_is_fine(repo):
    commit(repo, "docs/notes.md", "Nothing site specific here.\n")
    proc = run_gate(repo, "--all")  # points at a path that does not exist
    assert proc.returncode == 0


# ── commit messages ──────────────────────────────────────────


def test_commit_message_is_scanned(repo):
    commit(repo, "docs/a.md", "text\n", message="Deploy for acme-tenant.onmicrosoft.com pilot")
    proc = run_gate(repo, "--all")
    assert proc.returncode == 1
    blocks = findings_of(proc, "block")
    assert any(f["scope"] == "commit" for f in blocks)


def test_inline_suppression_skips_a_line(repo, private_file):
    private = private_file("[block]\nnordvik\n")
    commit(
        repo,
        "docs/notes.md",
        "The pilot tenant nordvik is redacted here.  # prepush-allow\n",
    )
    proc = run_gate(repo, "--all", private=private)
    assert proc.returncode == 0
    assert findings_of(proc, "block") == []


# ── file level exemption ─────────────────────────────────────


def test_file_marker_exempts_a_fixture_file(repo):
    commit(
        repo,
        "tests/fixtures.py",
        "# prepush-allow-file: *\nTENANT = 'acme-tenant.onmicrosoft.com'\n",
    )
    proc = run_gate(repo, "--all")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert findings_of(proc, "block") == []


def test_file_marker_can_list_specific_rules(repo):
    commit(
        repo,
        "scripts/data.toml",
        "# prepush-allow-file: tenant-domain\n"
        "a = 'acme-tenant.onmicrosoft.com'\n"
        "b = 'sk-proj-A1b2C3d4E5f6G7h8I9j0K1l2'\n",
    )
    proc = run_gate(repo, "--all")
    ids = [f["rule_id"] for f in findings_of(proc, "block")]
    assert "tenant-domain" not in ids
    assert "openai-key" in ids


def test_file_marker_never_exempts_the_private_overlay(repo, private_file):
    private = private_file("[block]\nnordvik\n")
    commit(repo, "tests/fixtures.py", "# prepush-allow-file: *\nNAME = 'nordvik'\n")
    proc = run_gate(repo, "--all", private=private)
    assert proc.returncode == 1
    assert "private-block-1" in [f["rule_id"] for f in findings_of(proc, "block")]


def test_gate_does_not_exempt_itself():
    """Prose about the marker must not disarm the files that document it."""
    gate_text = (REPO_ROOT / "scripts" / "prepush_gate.py").read_text(encoding="utf-8")
    assert prepush.file_marker(gate_text) is None
    docs_text = (REPO_ROOT / "docs" / "pre-push-gate.md").read_text(encoding="utf-8")
    assert prepush.file_marker(docs_text) is None
    rules_text = (REPO_ROOT / "scripts" / "prepush_rules.toml").read_text(encoding="utf-8")
    assert prepush.file_marker(rules_text) == {"connection-string-secret"}


# ── ref handling and modes ───────────────────────────────────


def test_stdin_refs_diff_mode_ignores_untouched_lines(repo, private_file):
    private = private_file("[block]\nnordvik\n")
    # The term lands in history, then a normal push adds an unrelated line.
    commit(repo, "docs/legacy.md", "tenant nordvik appears here\n", message="Legacy note")
    first = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
    ).stdout.strip()
    commit(repo, "src/ok.py", "x = 1\n")

    refs = tmp_refs(repo, "refs/heads/main", "refs/heads/main", first)
    proc = run_gate(repo, "--refs-file", str(refs), private=private)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert findings_of(proc, "block") == []


def test_stdin_refs_new_branch_scans_whole_tree(repo, private_file):
    private = private_file("[block]\nnordvik\n")
    commit(repo, "docs/legacy.md", "tenant nordvik appears here\n", message="Legacy note")
    refs = tmp_refs(repo, "refs/heads/main", "refs/heads/main", "0" * 40)
    proc = run_gate(repo, "--refs-file", str(refs), private=private)
    assert proc.returncode == 1
    assert "private-block-1" in [f["rule_id"] for f in findings_of(proc, "block")]


def test_push_already_on_the_remote_scans_nothing(repo, private_file):
    """A branch level with, or behind, its remote publishes nothing new.

    Live case: pushing master was rejected as non-fast-forward because CI had
    added a version bump commit on the remote. That commit was not in the local
    repository, so there was no sha to diff against, the pushed set came back
    empty, and the gate fell through to a whole-tree scan of 197 commits and
    16 blocking findings over content that was already public.
    """
    private = private_file("[block]\nnordvik\n")
    commit(repo, "docs/legacy.md", "tenant nordvik appears here\n", message="Legacy note")
    # This branch is already published: a remote-tracking ref holds it.
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    # The remote's newer tip is unknown locally, like CI's bump commit.
    refs = tmp_refs(repo, "refs/heads/main", "refs/heads/main", "f" * 40)

    proc = run_gate(repo, "--refs-file", str(refs), private=private)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert findings_of(proc) == []
    assert json.loads(proc.stdout)["stats"][0]["mode"] == "none"


def test_nothing_new_is_reported_in_words(repo):
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    refs = tmp_refs(repo, "refs/heads/main", "refs/heads/main", "f" * 40)

    proc = run_gate(repo, "--refs-file", str(refs), json_out=False)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "nothing new to publish" in proc.stdout


def test_resolve_base_separates_nothing_new_from_an_unknown_base(repo):
    """None means "scan the tree"; NOTHING_NEW means "there is nothing to scan"."""
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
    ).stdout.strip()

    # Case 1: the remote has this exact commit, so diff against it.
    assert prepush.resolve_base(str(repo), sha, sha, "origin") == sha
    # Case 4: the remote has moved on to a commit we never fetched.
    assert prepush.resolve_base(str(repo), sha, "f" * 40, "origin") is prepush.NOTHING_NEW


def test_branch_deletion_is_skipped(repo):
    refs = tmp_refs(repo, "(delete)", "refs/heads/gone", "0" * 40, local_sha="0" * 40)
    proc = run_gate(repo, "--refs-file", str(refs))
    assert proc.returncode == 0


def test_head_local_ref_is_parsed_and_scanned(repo):
    """``git push origin HEAD:<branch>`` sends a local ref of literally ``HEAD``.

    The commit being pushed is the second field, so the local ref is a label, not
    a decision. Rejecting the literal emptied the ref list, which is the exit 2
    "could not run" path, and the hook allows the push unscanned on exit 2.
    """
    commit(repo, "src/one.py", "x = 1\n")
    base = head_sha(repo)
    commit(repo, "src/two.py", "y = 2\n")

    refs = tmp_refs(repo, "HEAD", "refs/heads/topic", base)
    proc = run_gate(repo, "--refs-file", str(refs))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    stats = json.loads(proc.stdout)["stats"][0]
    assert stats["mode"] == "diff"
    assert stats["files"] == 1  # src/two.py, the only file this push adds


def test_head_local_ref_still_blocks_a_private_term(repo, private_file):
    """The HEAD push has to be scanned, not merely parsed."""
    private = private_file("[block]\nnordvik\n")
    commit(repo, "docs/legacy.md", "clean\n")
    base = head_sha(repo)
    commit(repo, "docs/pilot.md", "Pilot tenant is nordvik.\n")

    refs = tmp_refs(repo, "HEAD", "refs/heads/topic", base)
    proc = run_gate(repo, "--refs-file", str(refs), private=private)

    assert proc.returncode == 1
    assert "private-block-1" in [f["rule_id"] for f in findings_of(proc, "block")]


def test_the_other_local_ref_shapes_git_prints_are_parsed(repo, private_file):
    """A ``<sha>:<branch>`` push sends the object name, and a ``<rev>:<branch>``
    push sends the revision as typed. Both emptied the ref list like HEAD did."""
    private = private_file("[block]\nnordvik\n")
    commit(repo, "docs/legacy.md", "clean\n")
    base = head_sha(repo)
    commit(repo, "docs/pilot.md", "Pilot tenant is nordvik.\n")
    sha = head_sha(repo)

    for local in (sha, sha[:9], "HEAD~1"):
        refs = tmp_refs(repo, local, "refs/heads/topic", base)
        proc = run_gate(repo, "--refs-file", str(refs), private=private)
        assert proc.returncode == 1, f"{local}: {proc.stdout}{proc.stderr}"


def head_sha(repo: pathlib.Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
    ).stdout.strip()


def tmp_refs(repo: pathlib.Path, local: str, remote: str, remote_sha: str, local_sha: str | None = None):
    sha = local_sha or head_sha(repo)
    path = repo / ".." / "refs.txt"
    path = path.resolve()
    path.write_text(f"{local} {sha} {remote} {remote_sha}\n", encoding="utf-8")
    return path


# ── reporting ────────────────────────────────────────────────


def test_json_output_shape(repo):
    commit(repo, "docs/notes.md", "Tenant: acme-tenant.onmicrosoft.com\n")
    proc = run_gate(repo, "--all")
    payload = json.loads(proc.stdout)
    assert set(payload) >= {"findings", "stats", "blocking", "warnings", "note"}
    assert payload["blocking"] >= 1
    assert payload["stats"][0]["mode"] in {"diff", "tree"}


def test_quiet_hides_warnings(repo):
    commit(repo, "docs/notes.md", "Mail me at dev.lead@realcompany.no please.\n")
    noisy = run_gate(repo, "--all", json_out=False)
    assert noisy.returncode == 0
    assert "email-address" in noisy.stdout
    quiet = run_gate(repo, "--all", "--quiet", json_out=False)
    assert "email-address" not in quiet.stdout


def test_missing_rules_file_exits_two(repo):
    proc = subprocess.run(
        [
            sys.executable,
            str(GATE),
            "--repo",
            str(repo),
            "--rules",
            str(repo / "nope.toml"),
            "--all",
        ],
        capture_output=True,
        text=True,
        cwd=str(repo),
    )
    assert proc.returncode == 2


def test_empty_stdin_refs_is_not_reported_as_a_clean_scan(repo):
    """An unreadable ref list must not look like a passing scan.

    The hook reads the ref list from stdin, so an empty or unparsable list means
    the gate never saw what the push would publish. Exit 2 is the loud "could not
    run" path, which the hook prints before allowing the push.
    """
    env = dict(os.environ)
    env["THREADWEAVE_PREPUSH_PRIVATE"] = str(repo / "absent.txt")
    proc = subprocess.run(
        [
            sys.executable,
            str(GATE),
            "--repo",
            str(repo),
            "--rules",
            str(RULES),
            "--stdin-refs",
        ],
        input="",
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo),
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "could not read the push refs" in proc.stderr


def test_unparsable_refs_file_is_not_reported_as_a_clean_scan(repo):
    refs = (repo / ".." / "junk-refs.txt").resolve()
    refs.write_text("this is not a ref line\n", encoding="utf-8")
    proc = run_gate(repo, "--refs-file", str(refs))
    assert proc.returncode == 2
    assert "could not read the push refs" in proc.stderr


def test_unparsable_stdin_refs_stay_exit_two(repo):
    """Prose on stdin is still not a push, now that every shape git prints is.

    Accepting the HEAD literal must not turn "anything with four words" into a
    target: a line still needs a refs/ remote ref and two object names.
    """
    env = dict(os.environ)
    env["THREADWEAVE_PREPUSH_PRIVATE"] = str(repo / "absent.txt")
    proc = subprocess.run(
        [
            sys.executable,
            str(GATE),
            "--repo",
            str(repo),
            "--rules",
            str(RULES),
            "--stdin-refs",
        ],
        input="this is not a ref line\n",
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo),
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "could not read the push refs" in proc.stderr


def test_hook_script_is_present_and_executable():
    hook = REPO_ROOT / ".githooks" / "pre-push"
    assert hook.is_file()
    text = hook.read_text(encoding="utf-8")
    assert "prepush_gate.py" in text
    assert "exit 0" in text  # fail-open path exists


# ── unit level helpers ───────────────────────────────────────


def test_redact_policy():
    # Short identifiers stay readable, long credentials are cut down.
    assert prepush.redact("nordvik") == "nordvik"
    assert prepush.redact("someone@realcompany.no") == "someone@realcompany.no"
    assert prepush.redact("sk-proj-abcdefghijklmnopqrstuvwxyz") == "sk-pro…(34 chars)"
    assert prepush.redact("x" * 40, keep_full=True) == "x" * 40


def test_is_push_source_names_every_shape_git_prints(repo):
    """The local ref field is a label for the report, not a scan decision.

    Each shape below was captured from a real pre-push hook; a shape missing from
    this list empties the ref list and the hook then allows the push unscanned.
    """
    commit(repo, "src/extra.py", "z = 3\n")  # so HEAD~1 resolves
    sha = head_sha(repo)
    assert prepush.is_push_source("refs/heads/main", str(repo))
    assert prepush.is_push_source("refs/tags/v1", str(repo))
    assert prepush.is_push_source("refs/remotes/origin/main", str(repo))
    assert prepush.is_push_source("(delete)", str(repo))  # branch or tag deletion
    assert prepush.is_push_source("HEAD", str(repo))  # git push origin HEAD:branch
    assert prepush.is_push_source(sha, str(repo))  # git push origin <sha>:branch
    assert prepush.is_push_source(sha[:9], str(repo))  # ... written abbreviated
    assert prepush.is_push_source("HEAD~1", str(repo))  # ... written as a rev
    # Not a push source: prose, a rev that resolves to nothing, or an option.
    assert not prepush.is_push_source("this", str(repo))
    assert not prepush.is_push_source("HEAD~9", str(repo))
    assert not prepush.is_push_source("--all", str(repo))


def test_parse_added_lines_tracks_numbers():
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,1 +1,2 @@\n"
        " keep\n"
        "+added\n"
    )
    added, paths = prepush.parse_added_lines(diff)
    assert paths == ["x.py"]
    assert added == [("x.py", 2, "added")]


def test_advisory_texts_collects_messages_and_comments(repo):
    _git(repo, "checkout", "-q", "-b", "feature")
    commit(
        repo,
        "src/mod.py",
        "# This comment explains the internal pilot decision\nx = 1\n",
        message="Add module for the internal pilot discussion",
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
    ).stdout.strip()
    base = subprocess.run(
        ["git", "rev-parse", "HEAD~1"], cwd=str(repo), capture_output=True, text=True
    ).stdout.strip()
    rules = prepush.load_rules(RULES, None)
    texts = prepush.advisory_texts(str(repo), base, head, rules)
    locations = [loc for loc, _ in texts]
    assert any(loc.startswith("commit ") for loc in locations)
    assert "src/mod.py" in locations
    joined = " ".join(text for _, text in texts)
    assert "internal pilot decision" in joined


# ── the hook's interpreter lookup ────────────────────────────
#
# The hook runs before the gate does, and finding an interpreter is its whole
# job. Answering on PATH is not the same as being able to run anything: on
# Windows `python3` is an App Execution Alias that prints a Store advert and
# exits 49, and a fresh `git worktree` has no .venv, so the alias used to be the
# only interpreter the hook found. Reading its 49 as "the gate found nothing"
# still allowed the push. These tests build real repositories and put real
# stubs on PATH, because the failure being guarded against is exactly that a
# candidate which never ran the gate looks like a gate that passed.

SH = shutil.which("sh")
STORE_ADVERT = (
    "Python was not found; run without arguments to install from the Microsoft Store"
)

needs_shell = pytest.mark.skipif(
    SH is None, reason="the pre-push hook is a POSIX shell script"
)


def _hook_repo(tmp_path: pathlib.Path, name: str = "hooked", *, venv: bool = False):
    """A repository carrying the shipped hook and gate, with no remote."""
    repo = tmp_path / name
    repo.mkdir()
    for rel in (
        ".githooks/pre-push",
        "scripts/prepush_gate.py",
        "scripts/prepush_rules.toml",
    ):
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / rel, dest)
    _write(repo, "README.md", "# clean\n\nNothing to see here.\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Dev")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Initial commit")
    if venv:
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(repo / ".venv")],
            check=True,
            capture_output=True,
        )
    return repo


def _store_stub(directory: pathlib.Path, name: str) -> pathlib.Path:
    """A candidate that answers on PATH and runs nothing, like the Store alias."""
    path = directory / name
    path.write_text(
        f"#!/bin/sh\necho '{STORE_ADVERT}' >&2\nexit 49\n", encoding="utf-8"
    )
    path.chmod(0o755)
    return path


def _candidate_wrapper(directory: pathlib.Path, name: str, python: str) -> pathlib.Path:
    """A candidate that can really run the gate."""
    path = directory / name
    path.write_text(f'#!/bin/sh\nexec "{python}" "$@"\n', encoding="utf-8")
    path.chmod(0o755)
    return path


def hook_path(tmp_path: pathlib.Path, stubs=("py", "python3", "uv")):
    """PATH entries for the hook: stubs first, then one candidate that works.

    The stub names are the candidates that sit before ``python`` in the hook's
    lookup order, so the interpreter it settles on is reached by rejecting
    stubs rather than by being the first name on PATH.
    """
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    for name in stubs:
        _store_stub(stub_dir, name)
    candidates = tmp_path / "candidates"
    candidates.mkdir()
    _candidate_wrapper(candidates, "python", sys.executable)
    return [stub_dir, candidates]


def run_hook(repo: pathlib.Path, refs: str, *, path_prefix=(), env_extra=None):
    """Run the real hook the way git would, with the ref lines on stdin."""
    env = dict(os.environ)
    # Never let the real overlay into a test: it holds real identifiers.
    env["THREADWEAVE_PREPUSH_PRIVATE"] = str(repo / "absent.txt")
    env.pop("THREADWEAVE_PREPUSH_ADVISORY", None)
    if path_prefix:
        env["PATH"] = os.pathsep.join([str(p) for p in path_prefix] + [env["PATH"]])
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [
            SH,
            str(repo / ".githooks" / "pre-push"),
            "origin",
            "https://example.invalid/threadweave.git",
        ],
        input=refs,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo),
    )


def refs_for(repo: pathlib.Path, remote_sha: str = "0" * 40) -> str:
    """One ref line in git's order: local ref, local sha, remote ref, remote sha."""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
    ).stdout.strip()
    return f"refs/heads/main {head} refs/heads/main {remote_sha}\n"


def interpreter_line(proc: subprocess.CompletedProcess) -> str:
    lines = [
        line
        for line in proc.stderr.splitlines()
        if "prepush_gate.py" in line and "running" in line
    ]
    assert len(lines) == 1, f"expected one interpreter line, got: {proc.stderr}"
    return lines[0]


@needs_shell
def test_hook_skips_a_stub_interpreter_and_runs_the_gate(tmp_path):
    repo = _hook_repo(tmp_path)
    proc = run_hook(repo, refs_for(repo), path_prefix=hook_path(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # The stub's 49 is not a verdict: the gate ran, and its summary says so.
    assert STORE_ADVERT not in proc.stderr + proc.stdout
    assert "commit(s)" in proc.stdout
    # And it named the interpreter that actually ran it, not the stub it passed
    # over. The path is the shell's view, so compare its leaf (MSYS rewrites
    # a Windows tmp path into /tmp/...).
    line = interpreter_line(proc)
    assert "candidates/python" in line
    assert "stubs/" not in line
    assert "could not run" not in proc.stderr
    assert "gate exit 0, push allowed" in proc.stderr


@needs_shell
def test_hook_prefers_the_repository_venv_over_anything_on_path(tmp_path):
    repo = _hook_repo(tmp_path, venv=True)
    all_stubbed = hook_path(tmp_path, stubs=("py", "python3", "python", "uv"))
    proc = run_hook(repo, refs_for(repo), path_prefix=all_stubbed)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert ".venv" in interpreter_line(proc)
    assert "commit(s)" in proc.stdout


@needs_shell
def test_hook_fails_open_loudly_when_no_interpreter_can_run_the_gate(tmp_path):
    repo = _hook_repo(tmp_path)
    all_stubbed = hook_path(tmp_path, stubs=("py", "python3", "python", "uv"))
    proc = run_hook(repo, refs_for(repo), path_prefix=all_stubbed)
    assert proc.returncode == 0, proc.stdout + proc.stderr  # fail open, by design
    assert "no interpreter could run the gate" in proc.stderr
    assert "tried: " in proc.stderr
    # Nothing was scanned, so nothing may read as a passing scan.
    assert "commit(s)" not in proc.stdout
    assert "gate exit" not in proc.stderr


@needs_shell
def test_hook_reports_a_broken_threadweave_prepush_python_and_carries_on(tmp_path):
    repo = _hook_repo(tmp_path)
    broken = tmp_path / "broken"
    broken.mkdir()
    _store_stub(broken, "python")
    proc = run_hook(
        repo,
        refs_for(repo),
        path_prefix=hook_path(tmp_path),
        env_extra={"THREADWEAVE_PREPUSH_PYTHON": str(broken / "python")},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "THREADWEAVE_PREPUSH_PYTHON" in proc.stderr
    assert "cannot run the gate" in proc.stderr
    # The override is not a dead end: the lookup continued and ran the gate.
    assert "commit(s)" in proc.stdout
    assert "candidates/python" in interpreter_line(proc)


@needs_shell
def test_hook_returns_1_when_the_gate_blocks(tmp_path, private_file):
    repo = _hook_repo(tmp_path)
    private = private_file("[block]\nnordvik\n")
    _write(repo, "notes.md", "Signed off by nordvik before the pilot.\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Add notes")
    proc = run_hook(
        repo,
        refs_for(repo),
        path_prefix=hook_path(tmp_path),
        env_extra={"THREADWEAVE_PREPUSH_PRIVATE": str(private)},
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "BLOCK" in proc.stdout
    assert "nordvik" in proc.stdout
    assert "push blocked" in proc.stderr

