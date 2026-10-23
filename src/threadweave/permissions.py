# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Permission story: what each connector asks for, and what Graph granted.

Two things live here, and they are deliberately in one place so they cannot
drift apart:

1. ``CONNECTORS`` — the machine-readable half of ``docs/permissions.md``:
   which registration backs which daemon, which application roles and
   delegated scopes it needs, and what least-privilege path exists.
2. The audit itself — obtain a token with the registration's own
   credentials, read the ``roles`` claim out of it, and report what is
   actually granted in the tenant.

Why the token claim and not the Graph admin endpoints: the roles claim is
the identity provider's own statement of what was consented for this app.
Reading it needs *no* extra permission (the app's own credentials are the
credential), while ``GET /servicePrincipals/{id}/appRoleAssignments`` would
require ``Directory.Read.All`` or ``Application.ReadWrite.All``, which is
exactly the kind of grant this audit exists to argue against. The claim is
not verified as a signature here, and it does not need to be: it is read
from a token we just received over TLS from the tenant's own authority, and
it is used for reporting, not for authorisation.

The audit never prints a secret. A test asserts that a client secret present
in the environment cannot appear in the report.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

GRAPH_SCOPE = "https://graph.microsoft.com/.default"

# ---- Registrations -------------------------------------------------------

@dataclass(frozen=True)
class Registration:
    key: str
    label: str
    id_env: str
    secret_env: str
    tenant_env: str = "AZURE_TENANT_ID"


REGISTRATIONS: dict[str, Registration] = {
    "reader": Registration(
        key="reader",
        label="ThreadWeave-GraphReader (reader app)",
        id_env="AZURE_CLIENT_ID",
        secret_env="AZURE_CLIENT_SECRET",
    ),
    "bot": Registration(
        key="bot",
        label="ThreadWeave Teams bot app",
        id_env="MICROSOFT_APP_ID",
        secret_env="MICROSOFT_APP_PASSWORD",
    ),
}

# ---- Connectors ----------------------------------------------------------
#
# One entry per connector, not per daemon: two daemons (email and sharepoint)
# share the reader registration and must be listed separately, because their
# blast radius differs even when the credential is the same one.


@dataclass(frozen=True)
class Connector:
    key: str
    label: str
    registration: str                     # "reader" | "bot" | "" for delegated-only
    daemons: tuple[str, ...]              # daemons that use it ("" entry = CLI only)
    app_roles: tuple[str, ...] = ()       # application permissions, required for the daemon to run
    optional_app_roles: tuple[str, ...] = ()
    delegated_scopes: tuple[str, ...] = ()
    touches: str = ""                     # the Graph endpoints it actually calls
    least_privilege: str = ""
    delegated_only: bool = False
    rsc: tuple[str, ...] = ()             # Teams RSC grants (not Graph app roles)


CONNECTORS: tuple[Connector, ...] = (
    Connector(
        key="email",
        label="Email watcher",
        registration="reader",
        daemons=("email-watch",),
        app_roles=("Mail.Read",),
        optional_app_roles=("User.Read.All", "Mail.ReadWrite"),
        delegated_scopes=("Mail.Read",),
        touches=(
            "/users/{mailbox}/mailFolders/{folder}/messages, "
            "/users/{mailbox}/messages, /users/{address} (department lookup); "
            "in delegated mode the same calls go to /me"
        ),
        least_privilege=(
            "Run email-watch delegated: `threadweave email login` (device code) "
            "then THREADWEAVE_MAIL_AUTH=delegated. The token then reaches only "
            "the signed-in mailbox, and no registration needs tenant-wide "
            "Mail.Read. App-only installations can narrow the same grant with "
            "an Exchange Application Access Policy to one mailbox or group."
        ),
    ),
    Connector(
        key="sharepoint",
        label="SharePoint watcher",
        registration="reader",
        daemons=("sharepoint-watch",),
        app_roles=("Sites.Read.All",),
        optional_app_roles=("User.Read.All",),
        touches="/sites, /sites/{id}/drives, /sites/{id}/drives/{id}/root/delta",
        least_privilege=(
            "Sites.Selected plus a per-site grant instead of Sites.Read.All. "
            "The daemon then reads only the sites listed for it. `--site` "
            "narrows what it polls but not the grant."
        ),
    ),
    Connector(
        key="onenote",
        label="OneNote reader (sharepoint-watch --onenote)",
        registration="",
        daemons=("sharepoint-watch",),
        delegated_scopes=("Notes.Read.All", "User.Read"),
        touches="/me/onenote/notebooks, /me/onenote/sections/.../pages",
        least_privilege=(
            "Delegated only, by Microsoft's deprecation of app-only OneNote "
            "tokens (2025-03-31). Leave --onenote off and no Notes grant is "
            "needed at all."
        ),
        delegated_only=True,
    ),
    Connector(
        key="teams-watch",
        label="Teams watcher",
        registration="reader",
        daemons=("teams-watch",),
        app_roles=(
            "ChannelMessage.Read.All",
            "Channel.ReadBasic.All",
            "Team.ReadBasic.All",
        ),
        optional_app_roles=("ChannelMember.Read.All",),
        touches=(
            "/teams, /teams/{id}/channels, "
            "/teams/{id}/channels/{id}/messages/delta, "
            "/teams/{id}/channels/{id}/members"
        ),
        least_privilege=(
            "No narrower app-only Graph role exists for channel messages. The "
            "tighter options are RSC (ChannelMessage.Read.Group, only where "
            "the app is installed) or `--team <name>`, which limits what the "
            "daemon polls. Capture notices for these authors are sent by the "
            "bot registration, not this one."
        ),
    ),
    Connector(
        key="org-sync",
        label="Org tracker (org-sync)",
        registration="reader",
        daemons=("org-sync",),
        app_roles=("Team.ReadBasic.All", "TeamMember.Read.All"),
        touches="/teams, /teams/{id}/members",
        least_privilege=(
            "Optional daemon. Leave it uninstalled and neither role is "
            "needed; there is no narrower app-only role for team membership."
        ),
    ),
    Connector(
        key="teams-bot",
        label="Teams bot (capture notices, RSC probe)",
        registration="bot",
        daemons=("teams-bot",),
        app_roles=("TeamsActivity.Send", "Mail.Send", "User.Read.All"),
        optional_app_roles=("TeamsAppInstallation.ReadForTeam.All",),
        touches=(
            "/users/{id}/teamwork/sendActivityNotification, "
            "/users/{sender}/sendMail, /users/{email}, "
            "/teams/{id}/permissionGrants"
        ),
        least_privilege=(
            "All three are notice legs, not capture: the bot stores and "
            "serves knowledge without them. Drop Mail.Send to remove the "
            "email fallback (THREADWEAVE_NOTIFY_EMAIL=0), and skip the RSC "
            "probe to avoid TeamsAppInstallation.ReadForTeam.All."
        ),
        rsc=("ChannelMessage.Read.Group", "ChatMessage.Read.Chat"),
    ),
    Connector(
        key="teams-publish",
        label="Teams app publishing (CLI, not a daemon)",
        registration="",
        daemons=(),
        delegated_scopes=("AppCatalog.Submit", "User.Read"),
        touches="/appCatalogs/teamsApps",
        least_privilege=(
            "Used only by `threadweave teams publish`, as the signed-in "
            "admin. Never install it as a daemon."
        ),
        delegated_only=True,
    ),
    Connector(
        key="graph-mail-harvester",
        label="Standalone mail harvester (ingest_graph_mail.py)",
        registration="",
        daemons=(),
        delegated_scopes=("Mail.Read",),
        touches="/me/mailFolders/{folder}/messages",
        least_privilege=(
            "Already the delegated path: the operator signs in, only their "
            "own mailbox is read, and no app registration is required."
        ),
        delegated_only=True,
    ),
)

DAEMON_KEYS: tuple[str, ...] = tuple(
    sorted({d for c in CONNECTORS for d in c.daemons if d})
)

# What a granted role reaches if the credential is stolen. Printed only for
# roles that are actually granted, so the report states the real blast radius
# rather than a generic caution.
BLAST_RADIUS: dict[str, str] = {
    "Mail.Read": "read every mailbox in the tenant",
    "Mail.ReadWrite": "read and modify every mailbox in the tenant",
    "Mail.Send": "send mail as any mailbox in the tenant",
    "Sites.Read.All": "read every SharePoint site and file in the tenant",
    "User.Read.All": "read every user's directory profile",
    "ChannelMessage.Read.All": "read every channel message in every team",
    "Channel.ReadBasic.All": "enumerate every team's channels",
    "ChannelMember.Read.All": "read every channel's member list",
    "Team.ReadBasic.All": "enumerate every team",
    "TeamMember.Read.All": "read every team's member list",
    "TeamsActivity.Send": "send Teams activity notifications to any user",
    "TeamsAppInstallation.ReadForTeam.All": "read every team's app installations",
}


# ---- Auditing ------------------------------------------------------------


@dataclass
class RegistrationAudit:
    """The result of auditing one app registration."""

    registration: str = ""
    label: str = ""
    app_id: str = ""
    tenant_id: str = ""
    granted: list[str] = field(default_factory=list)
    # ok | no-roles | unconfigured | refused — the difference matters: an
    # audit that ran and found nothing is evidence, a token the tenant
    # refused is not.
    status: str = "ok"
    error: str = ""


@dataclass
class DelegatedAudit:
    """The result of auditing the signed-in owner's delegated token."""

    account: str = ""
    granted: list[str] = field(default_factory=list)
    status: str = "ok"          # ok | no-scopes | refused
    error: str = ""


@dataclass
class Coverage:
    """Whether one daemon's requirements are met by the granted set."""

    daemon: str
    label: str
    mode: str                      # "app" | "delegated"
    required: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)
    optional_missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing


def effective_env(daemon: Optional[str] = None) -> dict[str, str]:
    """The environment a daemon would run with, then the process env.

    ``threadweave daemon run`` merges the daemon's env file *over* the
    process environment, so the audit has to do the same or it describes a
    different registration than the one the daemon really uses.
    """
    env: dict[str, str] = {k: v for k, v in os.environ.items()}
    if not daemon:
        return env
    from threadweave.daemons import DAEMONS, load_daemon_env

    if daemon in DAEMONS:
        env.update(load_daemon_env(daemon))
    return env


def decode_jwt_claims(token: str) -> dict:
    """Decode a JWT payload without verifying the signature (reporting only)."""
    if not token or token.count(".") < 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def acquire_app_token(tenant_id: str, client_id: str, client_secret: str) -> dict:
    """Client-credentials token for the registration's own app roles."""
    import msal

    app = msal.ConfidentialClientApplication(
        client_id=client_id,
        client_credential=client_secret,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
    )
    return app.acquire_token_for_client(scopes=[GRAPH_SCOPE])


def audit_registration(
    registration: Registration,
    env: Mapping[str, str],
    token_getter=None,
) -> RegistrationAudit:
    """List the application roles the tenant has actually granted this app."""
    tenant_id = (env.get(registration.tenant_env) or "").strip()
    client_id = (env.get(registration.id_env) or "").strip()
    secret = (env.get(registration.secret_env) or "").strip()

    result = RegistrationAudit(
        registration=registration.key,
        label=registration.label,
        app_id=client_id,
        tenant_id=tenant_id,
    )
    missing = [
        name
        for name, value in (
            (registration.tenant_env, tenant_id),
            (registration.id_env, client_id),
            (registration.secret_env, secret),
        )
        if not value
    ]
    if missing:
        result.status = "unconfigured"
        result.error = (
            "not configured: set " + ", ".join(missing) + " "
            "(values from the app registration's Overview and "
            "Certificates & secrets pages)"
        )
        return result

    getter = token_getter or acquire_app_token
    try:
        token_result = getter(tenant_id, client_id, secret)
    except Exception as exc:  # network, bad authority, msal missing
        result.status = "refused"
        result.error = f"token request failed: {type(exc).__name__}: {exc}"
        return result

    if not isinstance(token_result, dict) or "access_token" not in token_result:
        detail = ""
        if isinstance(token_result, dict):
            detail = str(
                token_result.get("error_description")
                or token_result.get("error")
                or token_result
            )
        result.status = "refused"
        result.error = (
            "the tenant refused a token for this registration "
            f"({detail or 'no access_token in the response'})"
        )
        return result

    claims = decode_jwt_claims(token_result["access_token"])
    result.granted = sorted(str(r) for r in (claims.get("roles") or []))
    result.tenant_id = str(claims.get("tid") or tenant_id)
    result.app_id = str(claims.get("appid") or client_id)
    if not result.granted:
        result.status = "no-roles"
        result.error = (
            "the token carries no roles claim: nothing is granted for this "
            "app yet, or admin consent was never granted after the "
            "permissions were added"
        )
    return result


def audit_delegated(env: Mapping[str, str], token_getter=None) -> DelegatedAudit:
    """List the delegated scopes the signed-in owner's token carries."""
    from threadweave.connectors.email.delegated import DelegatedMailAuth

    result = DelegatedAudit()
    auth = DelegatedMailAuth()
    result.account = auth.account()
    getter = token_getter or (lambda: auth.get_token())
    try:
        token = getter()
    except Exception as exc:
        result.status = "refused"
        result.error = f"{exc}"
        return result
    claims = decode_jwt_claims(token)
    scopes = str(claims.get("scp") or "")
    result.granted = sorted(s for s in scopes.split() if s)
    if not result.granted:
        result.status = "no-scopes"
        result.error = (
            "the delegated token carries no scp claim; re-run "
            "`threadweave email login`"
        )
    return result


def coverage_for(granted: Iterable[str], mode: str,
                 daemons: Optional[Iterable[str]] = None,
                 registration: str = "") -> list[Coverage]:
    """Map the granted set onto each connector's declared requirements.

    ``registration`` filters app-mode coverage to the registration that was
    audited: a bot-app grant says nothing about a reader daemon, and mixing
    the two would report a false miss on every run.
    """
    have = set(granted)
    wanted = set(daemons) if daemons else None
    rows: list[Coverage] = []

    def add(connector: Connector, required: Iterable[str],
            optional: Iterable[str]) -> None:
        for daemon in connector.daemons:
            if wanted is not None and daemon not in wanted:
                continue
            required = list(required)
            optional = list(optional)
            rows.append(
                Coverage(
                    daemon=daemon,
                    label=connector.label,
                    mode=mode,
                    required=required,
                    missing=[r for r in required if r not in have],
                    optional=optional,
                    optional_missing=[r for r in optional if r not in have],
                )
            )

    for connector in CONNECTORS:
        if mode == "app" and connector.app_roles:
            if registration and connector.registration != registration:
                continue
            add(connector, connector.app_roles, connector.optional_app_roles)
        elif mode == "delegated" and connector.delegated_scopes:
            add(connector, connector.delegated_scopes, ())

    if wanted is not None:
        # A daemon asked for explicitly but covered by no connector is a
        # matrix gap, not a pass.
        covered = {row.daemon for row in rows}
        for daemon in sorted(wanted - covered):
            rows.append(
                Coverage(daemon=daemon, label="(no connector declares it)",
                         mode=mode, missing=["not described in the matrix"])
            )
    return rows


def unused_grants(granted: Iterable[str]) -> list[str]:
    """Roles granted in the tenant that no ThreadWeave connector asks for."""
    known = {r for c in CONNECTORS
             for r in (c.app_roles + c.optional_app_roles)}
    return sorted(set(granted) - known)


# ---- Rendering -----------------------------------------------------------


def render_report(
    app_audits: list[RegistrationAudit],
    delegated: Optional[DelegatedAudit] = None,
    daemons: Optional[Iterable[str]] = None,
) -> tuple[str, list[Coverage]]:
    """Human-readable audit. Returns the text and every coverage row."""
    lines: list[str] = ["ThreadWeave permission audit", ""]
    all_rows: list[Coverage] = []

    for audit in app_audits:
        lines.append(f"{audit.label}")
        lines.append(f"  app id:  {audit.app_id or '(unset)'}")
        lines.append(f"  tenant:  {audit.tenant_id or '(unset)'}")
        if audit.status in ("unconfigured", "refused"):
            lines.append(f"  ERROR: {audit.error}")
            lines.append("")
            continue
        lines.append(f"  granted application roles ({len(audit.granted)}):")
        if audit.granted:
            for role in audit.granted:
                lines.append(f"    {role}")
        else:
            lines.append("    (none)")
        if audit.error:
            lines.append(f"  NOTE: {audit.error}")

        extra = unused_grants(audit.granted)
        if extra:
            lines.append("  granted but not used by any ThreadWeave connector:")
            for role in extra:
                lines.append(f"    {role}")

        radii = [f"    {role}: {BLAST_RADIUS[role]}"
                 for role in audit.granted if role in BLAST_RADIUS]
        if radii:
            lines.append("  blast radius of what is granted:")
            lines.extend(radii)

        rows = coverage_for(audit.granted, "app", daemons, audit.registration)
        all_rows.extend(rows)
        if rows:
            lines.append("  daemons on this registration:")
            for row in rows:
                verdict = "OK" if row.ok else "MISSING " + ", ".join(row.missing)
                needed = ", ".join(row.required) or "(none)"
                lines.append(f"    {row.daemon:<17} {needed:<58} {verdict}")
                if row.optional_missing:
                    lines.append(
                        f"    {'':<17} optional, not granted: "
                        + ", ".join(row.optional_missing)
                    )
        lines.append("")

    if delegated is not None:
        lines.append("Delegated access (the signed-in owner, no app roles)")
        lines.append(f"  account: {delegated.account or '(not signed in)'}")
        if delegated.status == "refused":
            lines.append(f"  ERROR: {delegated.error}")
        else:
            lines.append(f"  granted delegated scopes ({len(delegated.granted)}):")
            for scope in delegated.granted or ["(none)"]:
                lines.append(f"    {scope}")
            if delegated.error:
                lines.append(f"  NOTE: {delegated.error}")
            rows = coverage_for(delegated.granted, "delegated", daemons)
            all_rows.extend(rows)
            if rows:
                lines.append("  daemons on delegated credentials:")
                for row in rows:
                    verdict = "OK" if row.ok else "MISSING " + ", ".join(row.missing)
                    lines.append(
                        f"    {row.daemon:<17} {', '.join(row.required):<58} "
                        f"{verdict}"
                    )
        lines.append("")

    if all_rows:
        failed = [row for row in all_rows if not row.ok]
        if failed:
            lines.append(
                "Result: INCOMPLETE. Missing for "
                + ", ".join(sorted({row.daemon for row in failed}))
            )
        else:
            lines.append("Result: every declared requirement is granted.")
    else:
        lines.append("Result: nothing to compare (no daemons selected).")

    return "\n".join(lines), all_rows


def audit_to_json(
    app_audits: list[RegistrationAudit],
    rows: list[Coverage],
    delegated: Optional[DelegatedAudit] = None,
) -> str:
    """Machine-readable form for a security reviewer's evidence pack."""
    payload = {
        "registrations": [
            {
                "registration": a.registration,
                "label": a.label,
                "app_id": a.app_id,
                "tenant_id": a.tenant_id,
                "granted": a.granted,
                "status": a.status,
                "error": a.error,
            }
            for a in app_audits
        ],
        "delegated": (
            {"account": delegated.account, "granted": delegated.granted,
             "status": delegated.status, "error": delegated.error}
            if delegated is not None else None
        ),
        "daemons": [
            {
                "daemon": row.daemon,
                "connector": row.label,
                "mode": row.mode,
                "required": row.required,
                "missing": row.missing,
                "optional_missing": row.optional_missing,
                "ok": row.ok,
            }
            for row in rows
        ],
        "ok": all(row.ok for row in rows) if rows else True,
    }
    return json.dumps(payload, indent=2)
