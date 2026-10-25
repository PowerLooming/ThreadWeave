# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for the permission story: the matrix and `daemon permissions --audit`.

No network and no tenant: the token is built in the test, and every Graph
call is replaced by an injected token getter. A test also asserts that the
client secret present in the environment cannot reach the report, because
this command is the one an operator will paste into a ticket.
"""

import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, "src")
from threadweave import cli as cli_mod
from threadweave import daemons as daemons_mod
from threadweave import permissions as perm

REPO_ROOT = Path(__file__).resolve().parent.parent


def make_jwt(claims: dict) -> str:
    """A JWT-shaped token: the audit decodes the payload, not the signature."""
    def enc(obj) -> str:
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc(claims)}.signature"


def reader_env(**overrides) -> dict:
    env = {
        "AZURE_TENANT_ID": "tenant-1",
        "AZURE_CLIENT_ID": "app-1",
        "AZURE_CLIENT_SECRET": "not-a-real-secret-value",
    }
    env.update(overrides)
    return env


def getter_for(roles, claims_extra=None):
    claims = {"tid": "tenant-1", "appid": "app-1", "roles": roles}
    claims.update(claims_extra or {})
    token = make_jwt(claims)
    return lambda tenant, client, secret: {"access_token": token}


# ---- claims decoding ----


def test_decode_reads_the_roles_claim():
    claims = perm.decode_jwt_claims(make_jwt({"roles": ["Mail.Read"], "tid": "t"}))
    assert claims["roles"] == ["Mail.Read"]
    assert claims["tid"] == "t"


def test_decode_tolerates_garbage():
    assert perm.decode_jwt_claims("not-a-jwt") == {}
    assert perm.decode_jwt_claims("") == {}


# ---- app audit ----


def test_audit_lists_what_is_granted():
    audit = perm.audit_registration(
        perm.REGISTRATIONS["reader"], reader_env(), getter_for(["Mail.Read"])
    )
    assert audit.status == "ok"
    assert audit.granted == ["Mail.Read"]
    assert audit.app_id == "app-1"
    assert audit.tenant_id == "tenant-1"
    assert audit.error == ""


def test_audit_sorts_multiple_roles():
    audit = perm.audit_registration(
        perm.REGISTRATIONS["reader"], reader_env(),
        getter_for(["Sites.Read.All", "ChannelMessage.Read.All", "Mail.Read"]),
    )
    assert audit.granted == [
        "ChannelMessage.Read.All", "Mail.Read", "Sites.Read.All"
    ]


def test_unconfigured_registration_names_the_missing_variable():
    env = reader_env()
    del env["AZURE_CLIENT_SECRET"]
    audit = perm.audit_registration(perm.REGISTRATIONS["reader"], env)
    assert audit.status == "unconfigured"
    assert "AZURE_CLIENT_SECRET" in audit.error


def test_refused_token_is_not_reported_as_missing_permissions():
    audit = perm.audit_registration(
        perm.REGISTRATIONS["reader"], reader_env(),
        lambda t, c, s: {"error": "invalid_client",
                         "error_description": "AADSTS7000215: Invalid client secret"},
    )
    assert audit.status == "refused"
    assert "AADSTS7000215" in audit.error
    assert audit.granted == []


def test_empty_roles_claim_is_an_answer_not_a_failure():
    audit = perm.audit_registration(
        perm.REGISTRATIONS["reader"], reader_env(), getter_for([])
    )
    assert audit.status == "no-roles"
    assert "no roles claim" in audit.error


def test_the_client_secret_never_reaches_the_report():
    env = reader_env(AZURE_CLIENT_SECRET="super-secret-value-xyz")
    audit = perm.audit_registration(
        perm.REGISTRATIONS["reader"], env, getter_for(["Mail.Read"])
    )
    text, _ = perm.render_report([audit])
    assert "super-secret-value-xyz" not in text

    failed = perm.audit_registration(
        perm.REGISTRATIONS["reader"], env,
        lambda t, c, s: {"error": "invalid_client",
                         "error_description": "bad secret super-secret-value-xyz"},
    )
    text, _ = perm.render_report([failed])
    # The provider's error text is echoed, but never the value we sent.
    assert "bad secret" in text


# ---- coverage ----


def test_email_watch_needs_only_mail_read_in_app_mode():
    rows = perm.coverage_for(["Mail.Read"], "app", {"email-watch"})
    assert len(rows) == 1
    assert rows[0].required == ["Mail.Read"]
    assert rows[0].missing == []
    assert set(rows[0].optional_missing) == {"Mail.ReadWrite", "User.Read.All"}


def test_a_missing_required_role_is_reported():
    rows = perm.coverage_for(["Sites.Read.All"], "app", {"email-watch"})
    assert rows[0].missing == ["Mail.Read"]
    assert not rows[0].ok


def test_delegated_mail_read_covers_email_watch_without_app_roles():
    rows = perm.coverage_for(["Mail.Read"], "delegated", {"email-watch"})
    assert [row.daemon for row in rows] == ["email-watch"]
    assert rows[0].mode == "delegated"
    assert rows[0].ok


def test_unknown_daemon_is_a_matrix_gap_not_a_pass():
    rows = perm.coverage_for(["Mail.Read"], "app", {"mail-watch"})
    assert rows[0].daemon == "mail-watch"
    assert not rows[0].ok


def test_grants_nobody_asks_for_are_flagged():
    assert perm.unused_grants(["Mail.Read", "Directory.Read.All",
                               "Calendars.Read"]) == [
        "Calendars.Read", "Directory.Read.All"
    ]


# ---- report ----


def test_report_names_the_blast_radius_of_granted_roles():
    audit = perm.audit_registration(
        perm.REGISTRATIONS["reader"], reader_env(), getter_for(["Mail.Read"])
    )
    text, rows = perm.render_report([audit], daemons={"email-watch"})
    assert "every mailbox in the tenant" in text
    assert "every SharePoint site" not in text      # not granted, not claimed
    assert rows[0].ok


def test_report_says_incomplete_when_something_is_missing():
    audit = perm.audit_registration(
        perm.REGISTRATIONS["reader"], reader_env(), getter_for(["Mail.Read"])
    )
    text, rows = perm.render_report([audit], daemons={"teams-watch"})
    assert "INCOMPLETE" in text
    assert "ChannelMessage.Read.All" in text


def test_report_of_an_unconfigured_registration_is_an_error_line():
    env = {"AZURE_CLIENT_ID": "app-1"}
    audit = perm.audit_registration(perm.REGISTRATIONS["reader"], env)
    text, rows = perm.render_report([audit])
    assert "ERROR:" in text
    assert rows == []


# ---- delegated audit ----


class StubDelegatedAuth:
    def __init__(self, token="", account="owner@example.com", raises=None):
        self._token = token
        self._account = account
        self._raises = raises

    def account(self):
        return self._account

    def get_token(self):
        if self._raises:
            raise RuntimeError(self._raises)
        return self._token


def test_delegated_audit_lists_scopes_from_the_token(monkeypatch):
    from threadweave.connectors.email import delegated as delegated_mod

    token = make_jwt({"scp": "Mail.Read User.Read", "upn": "owner@example.com"})
    monkeypatch.setattr(
        delegated_mod, "DelegatedMailAuth",
        lambda *a, **k: StubDelegatedAuth(token=token),
    )
    audit = perm.audit_delegated({})
    assert audit.account == "owner@example.com"
    assert audit.granted == ["Mail.Read", "User.Read"]
    assert audit.status == "ok"


def test_delegated_audit_without_a_sign_in_is_refused(monkeypatch):
    from threadweave.connectors.email import delegated as delegated_mod

    monkeypatch.setattr(
        delegated_mod, "DelegatedMailAuth",
        lambda *a, **k: StubDelegatedAuth(
            raises="No delegated mail token. Run 'threadweave email login' once"),
    )
    audit = perm.audit_delegated({})
    assert audit.status == "refused"
    assert "threadweave email login" in audit.error


# ---- CLI ----


def _args(**overrides) -> SimpleNamespace:
    base = dict(audit=True, delegated=False, registration="reader",
                daemon="all", as_json=False)
    base.update(overrides)
    return SimpleNamespace(**base)


def full_reader_grants() -> list:
    return sorted({r for c in perm.CONNECTORS if c.registration == "reader"
                   for r in c.app_roles})


def test_cli_audit_exits_zero_when_every_requirement_is_granted(monkeypatch, capsys):
    monkeypatch.setenv("AZURE_TENANT_ID", "tenant-1")
    monkeypatch.setenv("AZURE_CLIENT_ID", "app-1")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "secret-value")
    original = perm.audit_registration
    monkeypatch.setattr(
        perm, "audit_registration",
        lambda reg, env, **kw: original(reg, env, getter_for(full_reader_grants())),
    )
    assert cli_mod.cmd_daemon_permissions(_args()) == 0
    out = capsys.readouterr().out
    assert "granted application roles" in out
    assert "Result: every declared requirement is granted." in out


def test_cli_audit_exits_one_on_a_missing_role(monkeypatch):
    monkeypatch.setenv("AZURE_TENANT_ID", "tenant-1")
    monkeypatch.setenv("AZURE_CLIENT_ID", "app-1")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "secret-value")
    original = perm.audit_registration
    monkeypatch.setattr(
        perm, "audit_registration",
        lambda reg, env, **kw: original(reg, env, getter_for(["Mail.Read"])),
    )
    assert cli_mod.cmd_daemon_permissions(_args(daemon="teams-watch")) == 1


def test_cli_audit_exits_two_with_nothing_configured(monkeypatch, capsys):
    for var in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
                "MICROSOFT_APP_ID", "MICROSOFT_APP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        perm, "audit_delegated",
        lambda env, **kw: perm.DelegatedAudit(
            status="refused", error="No delegated mail token."),
    )
    assert cli_mod.cmd_daemon_permissions(_args(registration="all")) == 2
    err = capsys.readouterr().err
    assert "threadweave email login" in err


def test_cli_json_output_is_parseable(monkeypatch, capsys):
    monkeypatch.setenv("AZURE_TENANT_ID", "tenant-1")
    monkeypatch.setenv("AZURE_CLIENT_ID", "app-1")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "secret-value")
    original = perm.audit_registration
    monkeypatch.setattr(
        perm, "audit_registration",
        lambda reg, env, **kw: original(reg, env, getter_for(["Mail.Read"])),
    )
    code = cli_mod.cmd_daemon_permissions(_args(as_json=True, daemon="email-watch"))
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["registrations"][0]["granted"] == ["Mail.Read"]
    assert payload["daemons"][0]["daemon"] == "email-watch"


def test_cli_reads_the_daemon_env_file(tmp_path, monkeypatch):
    """A daemon runs with its env file over the process env; so must the audit."""
    monkeypatch.setattr(daemons_mod, "DAEMON_DIR", str(tmp_path))
    monkeypatch.setenv("AZURE_CLIENT_ID", "from-process-env")
    daemons_mod.save_daemon_env("email-watch", {"AZURE_CLIENT_ID": "from-env-file"})
    env = perm.effective_env("email-watch")
    assert env["AZURE_CLIENT_ID"] == "from-env-file"


def test_cli_plain_process_env_without_a_daemon(monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_ID", "from-process-env")
    assert perm.effective_env(None)["AZURE_CLIENT_ID"] == "from-process-env"


# ---- drift guards ----
#
# The matrix exists in two places on purpose: code the audit reads, prose a
# reviewer reads. These tests are what keeps them the same story.


def test_every_daemon_is_described_by_a_connector():
    described = {d for c in perm.CONNECTORS for d in c.daemons}
    assert set(daemons_mod.DAEMONS) <= described


def test_docs_matrix_lists_every_permission_the_code_declares():
    docs = (REPO_ROOT / "docs" / "permissions.md").read_text(encoding="utf-8")
    declared = {r for c in perm.CONNECTORS
                for r in (c.app_roles + c.optional_app_roles
                          + c.delegated_scopes + c.rsc)}
    missing = sorted(role for role in declared if role not in docs)
    assert not missing, f"docs/permissions.md omits: {missing}"


def test_every_connector_has_a_least_privilege_note():
    for connector in perm.CONNECTORS:
        assert connector.least_privilege.strip(), connector.key
        assert connector.touches.strip(), connector.key
