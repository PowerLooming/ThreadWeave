# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Runtime profile — single-user ("personal") vs managed ("org") mode.

The personal tier is NOT a fork: it is the same codebase, store, search,
LLM pipeline and privacy contract as the managed tier. The only difference
is who consents and how many users, which is a config value, not a codebase.

Profile is selected by two env vars:

    THREADWEAVE_PROFILE=org|personal   # default "org"
    THREADWEAVE_OWNER_ID=<user>        # single identity, default "owner"

In personal mode every read is scoped to the owner: the API always builds
the requester context as the owner with admin clearance, and the daemon
registry only exposes the owner-scoped capture connectors.
"""

from __future__ import annotations

import os

PROFILES = ("org", "personal")
DEFAULT_PROFILE = "org"
DEFAULT_OWNER_ID = "owner"


def get_profile() -> str:
    """The active profile, one of ``PROFILES``.

    Reads ``THREADWEAVE_PROFILE``; unknown/empty values fall back to the
    managed ``org`` default rather than failing, so a bad value degrades to
    the safe corporate mode instead of a surprising single-user one.
    """
    value = os.environ.get("THREADWEAVE_PROFILE", DEFAULT_PROFILE)
    profile = value.strip().lower()
    return profile if profile in PROFILES else DEFAULT_PROFILE


def is_personal() -> bool:
    """True when running in single-user mode."""
    return get_profile() == "personal"


def get_owner_id() -> str:
    """The single identity in personal mode (``THREADWEAVE_OWNER_ID``)."""
    value = os.environ.get("THREADWEAVE_OWNER_ID", DEFAULT_OWNER_ID)
    return value.strip() or DEFAULT_OWNER_ID
