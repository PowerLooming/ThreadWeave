# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for daemon packaging (env files, argv, systemd units)."""

import os
import sys

import pytest

from threadweave.daemons import (
    DAEMONS, build_argv, load_daemon_env, save_daemon_env, systemd_unit,
)


@pytest.fixture(autouse=True)
def isolated_daemon_dir(tmp_path, monkeypatch):
    """Point daemon env storage at a temp dir for every test."""
    monkeypatch.setattr(
        "threadweave.daemons.daemons_dir",
        lambda: tmp_path / "daemons",
    )


def test_env_file_roundtrip():
    save_daemon_env("email-watch", {
        "THREADWEAVE_EMAIL_MAILBOX": "admin@corp.com",
        "THREADWEAVE_DAEMON_INTERVAL": "120",
    })
    env = load_daemon_env("email-watch")
    assert env["THREADWEAVE_EMAIL_MAILBOX"] == "admin@corp.com"
    assert env["THREADWEAVE_DAEMON_INTERVAL"] == "120"


def test_env_merge_keeps_existing():
    save_daemon_env("email-watch", {"THREADWEAVE_EMAIL_MAILBOX": "a@x.com"})
    save_daemon_env("email-watch", {"THREADWEAVE_DAEMON_INTERVAL": "60"})
    env = load_daemon_env("email-watch")
    assert env["THREADWEAVE_EMAIL_MAILBOX"] == "a@x.com"  # preserved
    assert env["THREADWEAVE_DAEMON_INTERVAL"] == "60"


def test_build_argv_email_watch():
    save_daemon_env("email-watch", {
        "THREADWEAVE_EMAIL_MAILBOX": "admin@corp.com",
        "THREADWEAVE_DAEMON_INTERVAL": "300",
    })
    argv = build_argv("email-watch")
    joined = " ".join(argv)
    assert "email watch" in joined
    assert "--mailbox admin@corp.com" in joined
    assert "--interval 300" in joined


def test_build_argv_sharepoint_onenote_flag():
    save_daemon_env("sharepoint-watch", {
        "THREADWEAVE_SP_SITE": "Mark 8",
        "THREADWEAVE_SP_ONENOTE": "1",
    })
    joined = " ".join(build_argv("sharepoint-watch"))
    assert "sharepoint watch" in joined
    assert "--site Mark 8" in joined
    assert "--onenote" in joined


def test_build_argv_sharepoint_without_onenote():
    save_daemon_env("sharepoint-watch", {"THREADWEAVE_SP_ONENOTE": "0"})
    joined = " ".join(build_argv("sharepoint-watch"))
    assert "--onenote" not in joined


def test_build_argv_graph_daemon_interval():
    save_daemon_env("graph-daemon", {"THREADWEAVE_GRAPH_INTERVAL": "600"})
    joined = " ".join(build_argv("graph-daemon"))
    assert "graph daemon" in joined
    assert "--interval 600" in joined


def test_build_argv_teams_bot():
    joined = " ".join(build_argv("teams-bot"))
    assert "connectors.teams.adapter" in joined


def test_systemd_unit_content():
    save_daemon_env("email-watch", {"THREADWEAVE_EMAIL_MAILBOX": "a@x.com"})
    unit = systemd_unit("email-watch")
    assert "[Unit]" in unit
    assert "[Service]" in unit
    assert "Restart=always" in unit
    assert "EnvironmentFile=" in unit
    assert "daemon run email-watch" in unit
    assert "[Install]" in unit
    assert "WantedBy=multi-user.target" in unit


def test_all_daemons_have_argv_and_description():
    for name, spec in DAEMONS.items():
        assert spec["description"]
        assert spec["argv"], name
        assert len(build_argv(name)) >= 3, name


def test_run_daemon_unknown(capsys):
    from threadweave.daemons import run_daemon
    assert run_daemon("nope") == 2


def test_windows_launcher_content_uses_cmd_wrapper_and_clean_crlf():
    from threadweave.daemons import windows_launcher_content
    content = windows_launcher_content("teams-bot")
    # WshShell.Run does not route through cmd.exe, so cmd built-ins (cd, &&, >>)
    # only work if the whole command is wrapped in `cmd /c`.
    assert content.startswith("Set WshShell")
    assert "cmd /c " in content
    assert "daemon run teams-bot" in content
    assert content.endswith("0, False\r\n")
    assert "\r\r" not in content


def test_install_windows_writes_launcher_without_double_cr(tmp_path, monkeypatch):
    from threadweave.daemons import install_windows
    monkeypatch.setattr("threadweave.daemons.windows_startup_dir", lambda: tmp_path)
    assert install_windows("teams-bot") is True
    raw = (tmp_path / "ThreadWeave-teams-bot.vbs").read_bytes()
    # Text-mode writes on Windows turn every \n into \r\n, which would turn the
    # explicit \r\n line endings into \r\r\n.
    assert b"\r\r" not in raw
    assert raw.count(b"\r\n") == 2
    assert b"cmd /c " in raw
    assert raw.decode("utf-8").endswith("0, False\r\n")
