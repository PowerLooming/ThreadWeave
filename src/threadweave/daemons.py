# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
Daemon packaging — turn the connector daemons into managed services.

The capture story only works if the daemons keep running. This module
registers them as OS-managed services so they start at login/boot and
restart on crash:

- Windows: scheduled task (schtasks, onlogon, restart-on-failure)
- Linux:   systemd unit (EnvironmentFile, Restart=always)

Each daemon has an env file at ~/.threadweave/daemons/<name>.env that
holds its secrets and options — one place, not shell history. The
`threadweave daemon run <name>` command loads it and dispatches.

Daemons:
- email-watch     threadweave email watch
- sharepoint-watch threadweave sharepoint watch
- graph-daemon    threadweave graph daemon
- teams-bot       python -m threadweave.connectors.teams.adapter
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

DAEMON_DIR = "~/.threadweave/daemons"
STATE_DIR = "~/.threadweave"

# name -> (description, argv template). {env} is not used here; argv
# builders read the env file themselves via `daemon run`.
DAEMONS: dict[str, dict] = {
    "email-watch": {
        "description": "Continuous one-way email harvesting (M365 -> on-prem)",
        "argv": [sys.executable, "-m", "threadweave.cli", "email", "watch"],
        "env_defaults": {
            "THREADWEAVE_EMAIL_MAILBOX": "",
            "THREADWEAVE_DAEMON_INTERVAL": "300",
            "THREADWEAVE_DAEMON_MAX_RESULTS": "20",
        },
    },
    "sharepoint-watch": {
        "description": "Continuous SharePoint + OneNote delta polling",
        "argv": [sys.executable, "-m", "threadweave.cli", "sharepoint", "watch"],
        "env_defaults": {
            "THREADWEAVE_SP_SITE": "",
            "THREADWEAVE_DAEMON_INTERVAL": "300",
            "THREADWEAVE_SP_ONENOTE": "0",
        },
    },
    "graph-daemon": {
        "description": "Continuous Graph external-connection sync (Copilot)",
        "argv": [sys.executable, "-m", "threadweave.cli", "graph", "daemon"],
        "env_defaults": {
            "THREADWEAVE_GRAPH_INTERVAL": "300",
        },
    },
    "teams-bot": {
        "description": "Teams bot (capture, privacy commands, notifications)",
        "argv": [sys.executable, "-m", "threadweave.connectors.teams.adapter"],
        "env_defaults": {
            "PORT": "3978",
            "THREADWEAVE_BOT_MODE": "both",
            "THREADWEAVE_NOTIFY_ENABLED": "1",
            "THREADWEAVE_NOTIFY_INTERVAL": "60",
        },
    },
}

WINDOWS_TASK_PREFIX = "ThreadWeave-"


def daemons_dir() -> Path:
    return Path(os.path.expanduser(DAEMON_DIR))


def env_file(name: str) -> Path:
    return daemons_dir() / f"{name}.env"


def load_daemon_env(name: str) -> dict[str, str]:
    """Load a daemon's env file (missing file -> empty dict)."""
    path = env_file(name)
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def save_daemon_env(name: str, values: dict[str, str]) -> None:
    """Write a daemon's env file (merging over existing values)."""
    current = load_daemon_env(name)
    current.update({k: v for k, v in values.items() if v is not None})
    daemons_dir().mkdir(parents=True, exist_ok=True)
    lines = [f"# ThreadWeave daemon: {name}", f"# {DAEMONS[name]['description']}"]
    for key in sorted(current):
        lines.append(f"{key}={current[key]}")
    env_file(name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_argv(name: str) -> list[str]:
    """Build the argv for a daemon, applying env-file options."""
    env = load_daemon_env(name)
    argv = list(DAEMONS[name]["argv"])
    if name == "email-watch":
        mailbox = env.get("THREADWEAVE_EMAIL_MAILBOX", "")
        if mailbox:
            argv += ["--mailbox", mailbox]
        argv += ["--interval", env.get("THREADWEAVE_DAEMON_INTERVAL", "300")]
        argv += ["--max-results", env.get("THREADWEAVE_DAEMON_MAX_RESULTS", "20")]
    elif name == "sharepoint-watch":
        site = env.get("THREADWEAVE_SP_SITE", "")
        if site:
            argv += ["--site", site]
        argv += ["--interval", env.get("THREADWEAVE_DAEMON_INTERVAL", "300")]
        if env.get("THREADWEAVE_SP_ONENOTE", "0") == "1":
            argv += ["--onenote"]
    elif name == "graph-daemon":
        argv += ["--interval", env.get("THREADWEAVE_GRAPH_INTERVAL", "300")]
    return argv


# ---- run (shared entry: `threadweave daemon run <name>`) ----

def run_daemon(name: str) -> int:
    """Run a daemon with its env file loaded (used by OS services).

    Dispatches in-process (no os.execvpe — that segfaults on MSYS/
    git-bash Windows). The env file is merged over the environment
    before the handler runs.
    """
    if name not in DAEMONS:
        print(f"Unknown daemon: {name}. Known: {', '.join(DAEMONS)}")
        return 2
    env = load_daemon_env(name)
    os.environ.update(env)
    argv = build_argv(name)
    logger.info("Starting daemon %s: %s", name, shlex.join(argv))

    from types import SimpleNamespace

    if name == "email-watch":
        from threadweave.cli import cmd_email_watch
        args = SimpleNamespace(
            mailbox=env.get("THREADWEAVE_EMAIL_MAILBOX", ""),
            interval=int(env.get("THREADWEAVE_DAEMON_INTERVAL", "300")),
            max_results=int(env.get("THREADWEAVE_DAEMON_MAX_RESULTS", "20")),
            mark_read=env.get("THREADWEAVE_EMAIL_MARK_READ", "0") == "1",
            no_threads=env.get("THREADWEAVE_EMAIL_NO_THREADS", "0") == "1",
        )
        cmd_email_watch(args)
    elif name == "sharepoint-watch":
        from threadweave.cli import cmd_sharepoint_watch
        args = SimpleNamespace(
            interval=int(env.get("THREADWEAVE_DAEMON_INTERVAL", "300")),
            site=env.get("THREADWEAVE_SP_SITE", ""),
            state_file=os.path.expanduser(
                env.get("THREADWEAVE_SP_STATE_FILE",
                        "~/.threadweave/sharepoint_delta.json")),
            onenote=env.get("THREADWEAVE_SP_ONENOTE", "0") == "1",
        )
        cmd_sharepoint_watch(args)
    elif name == "graph-daemon":
        from threadweave.cli import cmd_graph_daemon
        args = SimpleNamespace(
            interval=int(env.get("THREADWEAVE_GRAPH_INTERVAL", "300")),
        )
        cmd_graph_daemon(args)
    elif name == "teams-bot":
        from threadweave.connectors.teams.adapter import main as bot_main
        bot_main()
    return 0


# ---- Windows (scheduled task) ----

def windows_task_name(name: str) -> str:
    return f"{WINDOWS_TASK_PREFIX}{name}"


def windows_startup_dir() -> Path:
    """The per-user Startup folder (no admin needed for autostart)."""
    import ctypes
    from ctypes import wintypes

    CSIDL_STARTUP = 7
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(
        None, CSIDL_STARTUP, None, 0, buf
    )
    return Path(buf.value)


def install_windows(name: str) -> bool:
    """Register a daemon to start at login via the Startup folder.

    A .cmd launcher is dropped into shell:startup that runs
    `threadweave daemon run <name>`. Requires no admin rights (unlike
    schtasks /create) and survives reboots. Restart-on-failure is
    handled by the daemon loops themselves (resilient polling loops).
    """
    if name not in DAEMONS:
        return False
    startup = windows_startup_dir()
    startup.mkdir(parents=True, exist_ok=True)
    logs_dir = Path(os.path.expanduser("~/.threadweave/logs"))
    logs_dir.mkdir(parents=True, exist_ok=True)
    launcher = startup / f"ThreadWeave-{name}.cmd"
    log_path = logs_dir / f"{name}.log"
    # Project root = two dirs above this module (src/threadweave/daemons.py)
    project_root = Path(__file__).resolve().parent.parent.parent
    cmd = (
        f'@echo off\r\n'
        f'cd /d "{project_root}"\r\n'
        f'"{sys.executable}" -m threadweave.cli daemon run {name} '
        f'>> "{log_path}" 2>&1\r\n'
    )
    launcher.write_text(cmd, encoding="utf-8")
    logger.info("Installed startup launcher: %s", launcher)
    return True


def uninstall_windows(name: str) -> bool:
    launcher = windows_startup_dir() / f"ThreadWeave-{name}.cmd"
    try:
        launcher.unlink(missing_ok=True)
    except Exception:
        pass
    return True


def windows_status(name: str) -> dict:
    launcher = windows_startup_dir() / f"ThreadWeave-{name}.cmd"
    return {"installed": launcher.exists()}


# ---- Linux (systemd) ----

def systemd_unit(name: str) -> str:
    env = load_daemon_env(name)
    env_path = env_file(name)
    exec_path = f"{sys.executable} -m threadweave.cli daemon run {name}"
    unit = (
        "[Unit]\n"
        f"Description=ThreadWeave {name} — {DAEMONS[name]['description']}\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n[Service]\n"
        "Type=simple\n"
        f"EnvironmentFile={env_path}\n"
        f"ExecStart={exec_path}\n"
        "Restart=always\n"
        "RestartSec=10\n"
        "User=" + env.get("THREADWEAVE_SYSTEMD_USER", os.environ.get("USER", "")) + "\n"
        "\n[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    return unit


def install_systemd(name: str, system: bool = False) -> bool:
    """Install a systemd unit. Requires root unless --user."""
    if name not in DAEMONS:
        return False
    unit = systemd_unit(name)
    if system:
        path = Path(f"/etc/systemd/system/threadweave-{name}.service")
        path.write_text(unit, encoding="utf-8")
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(
            ["systemctl", "enable", "--now", f"threadweave-{name}.service"],
            check=False,
        )
    else:
        path = Path(
            os.path.expanduser("~/.config/systemd/user")
        ) / f"threadweave-{name}.service"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(unit, encoding="utf-8")
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"], check=False
        )
        subprocess.run(
            ["systemctl", "--user", "enable", "--now",
             f"threadweave-{name}.service"],
            check=False,
        )
    return True


def uninstall_systemd(name: str, system: bool = False) -> bool:
    if system:
        subprocess.run(
            ["systemctl", "disable", "--now", f"threadweave-{name}.service"],
            check=False,
        )
        Path(f"/etc/systemd/system/threadweave-{name}.service").unlink(
            missing_ok=True
        )
    else:
        subprocess.run(
            ["systemctl", "--user", "disable", "--now",
             f"threadweave-{name}.service"],
            check=False,
        )
        Path(
            os.path.expanduser("~/.config/systemd/user")
        ) / f"threadweave-{name}.service" .unlink(missing_ok=True)
    return True


def is_windows() -> bool:
    return os.name == "nt"


def install(name: str) -> bool:
    if name not in DAEMONS:
        print(f"Unknown daemon: {name}. Known: {', '.join(DAEMONS)}")
        return False
    if is_windows():
        ok = install_windows(name)
        print(f"Windows scheduled task installed: {windows_task_name(name)}")
        return ok
    ok = install_systemd(name)
    print(f"systemd user unit installed: threadweave-{name}.service")
    return ok


def uninstall(name: str) -> bool:
    if name not in DAEMONS:
        return False
    if is_windows():
        return uninstall_windows(name)
    return uninstall_systemd(name)


def status(name: str) -> dict:
    if name not in DAEMONS:
        return {"installed": False, "reason": "unknown daemon"}
    if is_windows():
        return windows_status(name)
    result = subprocess.run(
        ["systemctl", "--user", "is-active", f"threadweave-{name}.service"],
        capture_output=True, text=True, check=False,
    )
    active = result.stdout.strip() == "active"
    return {"installed": True, "status": result.stdout.strip() or "unknown",
            "active": active}
