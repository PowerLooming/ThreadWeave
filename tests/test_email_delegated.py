# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for delegated (owner-consent) mail access in the personal profile.

The managed tier reads a mailbox with app-only client credentials. The
personal tier must read the signed-in owner's own mailbox instead, or its
"no org-wide grants" claim is empty. These tests cover the token path and the
/me endpoints the watcher uses in that mode.
"""

import sys

import pytest

sys.path.insert(0, "src")
from threadweave.connectors.email import delegated as delegated_mod
from threadweave.connectors.email.delegated import DelegatedMailAuth
from threadweave.connectors.email.watcher import MailWatcher
from threadweave.cli import _resolve_mail_auth

WEB_LINK = "https://outlook.office365.com/owa/?ItemID=AAA&viewmodel=ReadMessageItem"


class FakeMsalApp:
    """Stands in for msal.PublicClientApplication."""

    def __init__(self, accounts=(), silent=None, device=None, **kwargs):
        self._accounts = list(accounts)
        self._silent = silent
        self._device = device or {}
        self.kwargs = kwargs

    def get_accounts(self):
        return self._accounts

    def acquire_token_silent(self, scopes, account=None):
        return self._silent

    def initiate_device_flow(self, scopes):
        return self._device

    def acquire_token_by_device_flow(self, flow):
        return self._device.get("result", {})


def make_auth(monkeypatch, tmp_path, app):
    monkeypatch.setattr(
        delegated_mod.msal, "PublicClientApplication", lambda **kw: app
    )
    return DelegatedMailAuth(cache_file=str(tmp_path / "msal_cache.json"))


# ---- token acquisition ----


def test_silent_token_from_the_cache(monkeypatch, tmp_path):
    app = FakeMsalApp(
        accounts=[{"username": "owner@example.com"}],
        silent={"access_token": "tok-cached"},
    )
    auth = make_auth(monkeypatch, tmp_path, app)
    assert auth.get_token() == "tok-cached"
    assert auth.account() == "owner@example.com"


def test_no_account_tells_the_operator_to_sign_in(monkeypatch, tmp_path):
    auth = make_auth(monkeypatch, tmp_path, FakeMsalApp())
    with pytest.raises(RuntimeError) as exc:
        auth.get_token()
    assert "threadweave email login" in str(exc.value)
    assert auth.account() == ""


def test_device_flow_signs_in_when_interactive(monkeypatch, tmp_path):
    app = FakeMsalApp(
        accounts=[],
        device={
            "user_code": "ABCD-1234",
            "verification_uri": "https://microsoft.com/devicelogin",
            "result": {"access_token": "tok-device"},
        },
    )
    auth = make_auth(monkeypatch, tmp_path, app)
    assert auth.get_token(interactive=True) == "tok-device"


def test_device_flow_failure_is_loud(monkeypatch, tmp_path):
    app = FakeMsalApp(
        accounts=[],
        device={"user_code": "X", "verification_uri": "https://x",
                "result": {"error_description": "consent required"}},
    )
    auth = make_auth(monkeypatch, tmp_path, app)
    with pytest.raises(RuntimeError) as exc:
        auth.get_token(interactive=True)
    assert "consent required" in str(exc.value)


# ---- watcher transport ----


class RecordingWatcher(MailWatcher):
    """Delegated watcher whose transport records instead of calling Graph."""

    def __init__(self):
        self.delegated = True
        self._delegated_auth = None
        self.graph = None
        self.calls = []

    async def _delegated_request(self, method, path, params=None, json_body=None):
        self.calls.append((method, path, params, json_body))
        if path.endswith("/messages") and method == "GET":
            return {"value": [{"id": "m1", "conversationId": "c1", "subject": "s",
                               "webLink": WEB_LINK,
                               "body": {"contentType": "text", "content": "body"},
                               "receivedDateTime": "2026-09-27T13:00:00Z"}]}
        return {}


@pytest.mark.asyncio
async def test_delegated_fetch_unread_uses_me_endpoints():
    w = RecordingWatcher()
    messages = await w.fetch_unread("ignored@x.com")
    method, path, params, _ = w.calls[0]
    assert (method, path) == ("GET", "/me/mailFolders/inbox/messages")
    assert "webLink" in params["$select"]
    assert messages[0].web_link == WEB_LINK


@pytest.mark.asyncio
async def test_delegated_fetch_thread_uses_me_endpoints():
    w = RecordingWatcher()
    await w.fetch_thread("ignored@x.com", "c1")
    method, path, params, _ = w.calls[0]
    assert (method, path) == ("GET", "/me/messages")
    assert "conversationId eq 'c1'" in params["$filter"]


@pytest.mark.asyncio
async def test_delegated_mark_read_patches_me():
    w = RecordingWatcher()
    await w.mark_as_read("ignored@x.com", ["m1"])
    method, path, _params, body = w.calls[0]
    assert (method, path, body) == ("PATCH", "/me/messages/m1", {"isRead": True})


def test_delegated_watcher_needs_no_client_credentials(monkeypatch):
    """Delegated mode must not require AZURE_CLIENT_SECRET (app-only creds)."""
    for var in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        delegated_mod.msal, "PublicClientApplication", lambda **kw: FakeMsalApp()
    )
    w = MailWatcher(delegated=True)
    assert w.graph is None
    assert w.delegated_account() == ""


# ---- mode resolution ----


def test_personal_profile_defaults_to_delegated(monkeypatch):
    monkeypatch.setenv("THREADWEAVE_PROFILE", "personal")
    monkeypatch.delenv("THREADWEAVE_MAIL_AUTH", raising=False)
    assert _resolve_mail_auth(None) == "delegated"
    assert _resolve_mail_auth("app") == "app"           # flag wins
    monkeypatch.setenv("THREADWEAVE_MAIL_AUTH", "app")
    assert _resolve_mail_auth(None) == "app"            # env beats profile
    assert _resolve_mail_auth("delegated") == "delegated"


def test_org_profile_defaults_to_app(monkeypatch):
    monkeypatch.setenv("THREADWEAVE_PROFILE", "org")
    monkeypatch.delenv("THREADWEAVE_MAIL_AUTH", raising=False)
    assert _resolve_mail_auth(None) == "app"
    assert _resolve_mail_auth("delegated") == "delegated"


def test_personal_watch_fails_fast_without_a_token(monkeypatch, capsys):
    """A missing delegated token must stop the start, not fail once per poll."""
    from types import SimpleNamespace

    import threadweave.cli as cli_mod
    from threadweave.connectors.email import delegated as d

    class NoToken(d.DelegatedMailAuth):
        def __init__(self, *a, **k):
            pass

        def account(self):
            return ""

        def get_token(self, interactive=False):
            raise RuntimeError(
                "No delegated mail token. Run 'threadweave email login' once."
            )

    monkeypatch.setattr(d, "DelegatedMailAuth", NoToken)
    monkeypatch.setenv("THREADWEAVE_PROFILE", "personal")
    monkeypatch.delenv("THREADWEAVE_MAIL_AUTH", raising=False)
    args = SimpleNamespace(mailbox="", interval=300, max_results=20,
                           mark_read=False, no_threads=False, mail_auth=None)

    with pytest.raises(SystemExit) as exc:
        cli_mod.cmd_email_watch(args)
    assert exc.value.code == 1
    assert "threadweave email login" in capsys.readouterr().err
