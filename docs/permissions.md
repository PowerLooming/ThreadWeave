# Permissions and least privilege

What ThreadWeave asks for, what each daemon actually touches, and the tighter
grant that exists where one does. This page is written for the security
reviewer who has to say yes or no, not for the operator installing it (that
walkthrough is in [m365-connectors.md](m365-connectors.md)).

Everything here is checkable against a live tenant:

```bash
threadweave daemon permissions --audit
```

That command obtains a token with the registration's own credentials, reads
the `roles` claim the tenant put in it, and prints what is granted, the blast
radius of what is granted, and which daemons are therefore complete. It needs
no directory permission of its own. Exit codes: `0` every declared
requirement is granted, `1` an audit ran and something required is missing,
`2` nothing is configured to audit, `3` the tenant refused a token or nobody
is signed in.

## The registrations

| Registration | Env vars | Used by | Lifetime of the credential |
|---|---|---|---|
| ThreadWeave-GraphReader | `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | email watcher, SharePoint watcher, Teams watcher, org tracker, activity-feed and email notices | Client secret, so it outlives any one person |
| ThreadWeave Teams bot app | `MICROSOFT_APP_ID`, `MICROSOFT_APP_PASSWORD` | Teams bot daemon (capture, privacy commands, capture notices), RSC consent probe | App password, rotatable; also the AAD app behind the Teams app manifest |
| Delegated sign-in, own app or Microsoft's public client | `THREADWEAVE_MAIL_CLIENT_ID`, `THREADWEAVE_MAIL_TENANT_ID`, MSAL cache at `~/.threadweave/msal_cache.json` | email watcher in delegated mode, `ingest_graph_mail.py`, OneNote extraction, `threadweave teams publish` | A refresh token for one person, revocable by them |

One registration can back several daemons, which is exactly why the blast
radius of a leak is a property of the grants, not of the daemon count. The
reader app below is the one that matters: it is the credential that can see
other people's content.

## The matrix

Application permissions (app-only, tenant admin consent, they work with
nobody signed in):

| Permission | On | Used by | Graph calls it enables | Least-privilege alternative |
|---|---|---|---|---|
| Mail.Read | reader | email-watch | `/users/{mailbox}/mailFolders/{folder}/messages`, `/users/{mailbox}/messages` | Delegated Mail.Read for one mailbox (`threadweave email login`); or an Exchange Application Access Policy scoping the app to one mailbox or group |
| Sites.Read.All | reader | sharepoint-watch | `/sites`, `/sites/{id}/drives`, `/sites/{id}/drives/{id}/root/delta` | Sites.Selected plus a per-site grant; or leave the daemon uninstalled |
| ChannelMessage.Read.All | reader | teams-watch | `/teams/{id}/channels/{id}/messages/delta` | RSC `ChannelMessage.Read.Group` where the app is installed, or `--team` scoping. No narrower app-only role exists |
| Channel.ReadBasic.All | reader | teams-watch | `/teams/{id}/channels` | None narrower for app-only channel enumeration |
| Team.ReadBasic.All | reader | teams-watch, org-sync | `/teams` | None narrower; daemons are opt-in per installation |
| ChannelMember.Read.All | reader, optional | teams-watch | `/teams/{id}/channels/{id}/members` | Optional: without it capture still works, participant lists are empty |
| TeamMember.Read.All | reader | org-sync | `/teams/{id}/members` | Optional daemon; do not install org-sync and the grant is not needed |
| User.Read.All | reader, optional | email-watch wing mapping, notice resolution | `/users/{address}`, `/users/{email}` | Optional: without it every capture lands in the fallback wing, and delegated mode needs it not at all |
| Mail.ReadWrite | reader, optional | email-watch with `--mark-read` | `PATCH /users/{mailbox}/messages/{id}` | Leave mark-read off (the default) and the grant is never needed |
| TeamsActivity.Send | bot | teams-bot capture notices | `/users/{id}/teamwork/sendActivityNotification` | Must be on the bot app (Graph only allows custom text from the app the recipient installed). Without it notices fall back to email, or are skipped |
| Mail.Send | bot | teams-bot email fallback | `/users/{sender}/sendMail` | Turn the fallback off (`THREADWEAVE_NOTIFY_EMAIL=0`) and drop the grant |
| TeamsAppInstallation.ReadForTeam.All | bot, optional | RSC consent probe | `/teams/{id}/permissionGrants` | Optional: skip the probe and verify RSC consent by hand in the Teams admin center |

Delegated permissions (a person signs in, the token reaches only what that
person can already reach):

| Scope | Registration | Used by | Least-privilege note |
|---|---|---|---|
| Mail.Read | delegated app | email-watch in delegated mode, `ingest_graph_mail.py` | The tightest mail option shipped: one mailbox, no tenant-wide grant on any registration |
| Mail.ReadWrite | delegated app | email-watch with `--mark-read` | Only requested when mark-read is on, because it is a write |
| Notes.Read.All | reader (delegated) | sharepoint-watch `--onenote` | Delegated only, by Microsoft's deprecation of app-only OneNote tokens. Leave `--onenote` off and no Notes grant is needed |
| User.Read | delegated app | OneNote extraction, `teams publish` | Basic profile of the signed-in user |
| AppCatalog.Submit | delegated app | `threadweave teams publish` | Used once, as an admin, to publish the Teams app. Never installed as a daemon |

Teams RSC grants on the bot app (declared in the manifest, consented per
tenant by an admin, not Graph app roles):

| Grant | What it enables | Note |
|---|---|---|
| ChannelMessage.Read.Group | bot receives all messages in channels of teams it is installed in | Only where installed. The daemon does not depend on it |
| ChatMessage.Read.Chat | bot receives all messages in group chats it was added to | 1:1 chats are never covered by any bot mechanism |

## Which daemon uses which credential

| Daemon | Credential | Required | Optional | What it touches |
|---|---|---|---|---|
| email-watch | reader, or delegated | Mail.Read | User.Read.All, Mail.ReadWrite | one mailbox's inbox messages and threads |
| sharepoint-watch | reader (+ delegated for OneNote) | Sites.Read.All | User.Read.All; Notes.Read.All and User.Read delegated | every site's drives, changed files |
| teams-watch | reader | ChannelMessage.Read.All, Channel.ReadBasic.All, Team.ReadBasic.All | ChannelMember.Read.All | every team's channels, channel message deltas |
| teams-bot | bot app | TeamsActivity.Send, Mail.Send, User.Read.All | TeamsAppInstallation.ReadForTeam.All | activity notifications, sendMail, RSC probes |
| org-sync | reader | Team.ReadBasic.All, TeamMember.Read.All | none | team membership snapshots |

Two facts fall out of that table and they are the argument of this page:

1. The only daemon that needs to read other people's written content with an
   app-only credential is the one you point at a mailbox. That one has a
   delegated mode, and the delegated mode is the default under the personal
   profile.
2. Every role marked optional is genuinely optional: the daemon runs without
   it, with a stated degradation. An installation that wants the smallest
   footprint installs email-watch only, in delegated mode, and holds no
   application permissions at all.

## Running email-watch with no tenant-wide Mail.Read

The whole point of the delegated path: the person whose mailbox is read signs
in once, and the token is scoped to their own mailbox. No registration needs
`Mail.Read` (application), no admin grant is involved where the tenant allows
user consent, and the daemon refreshes the token silently.

```bash
# 1. One-time sign-in as the mailbox owner (device code, MFA supported).
threadweave email login

# 2. Point the daemon at delegated mode in its own env file.
threadweave daemon config email-watch --set THREADWEAVE_MAIL_AUTH=delegated

# 3. Check what the token actually carries, then start it.
threadweave daemon permissions --audit --delegated
threadweave daemon install email-watch
```

`THREADWEAVE_MAIL_AUTH` accepts `app` or `delegated`; the personal profile
(`THREADWEAVE_PROFILE=personal`) already defaults to `delegated`. The
`--mailbox` flag becomes optional, because there is only one mailbox: the
signed-in owner's.

The design consequences, stated plainly, because they are the trade:

- Delegated mode reads exactly one mailbox, the signer's. A shared mailbox
  that users CC (`knowledge@example.com`) is not reachable through the
  signer's own token unless the signer has been granted access to it, and
  `/me` endpoints never reach it at all.
- The token is a person's, so the capture stops when that person leaves and
  their sign-in is revoked. That is the correct behaviour for a personal
  deployment and the wrong one for an org-wide one.
- Mark-as-read needs `Mail.ReadWrite`, requested only when `--mark-read` is
  given. Without it the daemon never writes to the mailbox.
- Department to wing mapping needs `User.Read.All`, which is app-only. In
  delegated mode the daemon passes no Graph client to the processor, so every
  capture lands in the `email` wing instead of a department wing. That is a
  documented degradation, not a bug.

For an app-only installation that must keep working with nobody signed in,
narrow the grant instead of the flow. An Exchange Application Access Policy
limits `Mail.Read` to a mailbox or a mail-enabled security group, which
removes the tenant-wide read while keeping the daemon unmanned:

```powershell
New-ApplicationAccessPolicy -AppId <AZURE_CLIENT_ID> `
  -PolicyScopeGroupId knowledge@example.com `
  -AccessRight RestrictAccess `
  -Description "ThreadWeave reads one mailbox"
```

Test it with `Test-ApplicationAccessPolicy`, and remember that the policy is
per-tenant and outside this repository: the audit command reports the grant,
not the policy.

## Verifying the report is real

`daemon permissions --audit` reads the token the registration obtains, so it
answers "what did this tenant consent to", which is the question a reviewer
asks. Three states worth knowing:

| You see | It means | What to do |
|---|---|---|
| `granted application roles (N)` and `Result: every declared requirement is granted.` | The roles claim carries what the daemons below need | Nothing |
| A role under `MISSING` | The token is valid but the grant is absent, usually consent was never clicked after adding the permission | Grant admin consent for the tenant, re-run |
| `the token carries no roles claim` | The app exists but holds nothing, or delegated permissions were added instead of application permissions | Add the application permission and grant consent |
| `the tenant refused a token` | Wrong secret, expired secret, or wrong tenant | Fix the credential; do not read it as a permission gap |

`--json` prints the same result for an evidence pack, and
`--registration reader|bot` limits the run to one registration when the two
have different owners. `--daemon <name>` limits the coverage table, which is
how you check one daemon without reading a wall of text.

The command never prints a secret. A test asserts that a client secret
present in the environment cannot reach the report.

## What no grant here can do

The audit lists permissions; the privacy contract is a separate claim and it
holds regardless of grants. Nothing in the matrix grants write access to
captured content in M365, nothing publishes entries back into the tenant, and
there is no route from a cloud assistant into this server. See
[ai-publication-boundary.md](ai-publication-boundary.md) and
[privacy.md](privacy.md).

## Revoking access

- Remove an application permission in Entra, then re-run the audit: the role
  disappears from the report as soon as the token no longer carries it.
- Remove a delegated sign-in by deleting `~/.threadweave/msal_cache.json` and
  revoking the app's consent for that user, or the user revoking it in
  [myapps.microsoft.com](https://myapps.microsoft.com).
- Uninstall a daemon (`threadweave daemon uninstall <name>`) so nothing keeps
  polling, and delete its env file at `~/.threadweave/daemons/<name>.env`,
  which is where its secret lives.
