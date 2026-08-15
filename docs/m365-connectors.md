# Setting Up M365 Connectors

Connect ThreadWeave to Exchange Online, SharePoint, and Microsoft Copilot via Microsoft Graph.

## What You'll Set Up

| Connector | What It Does | Permission Needed |
|---|---|---|
| Email Watcher | Monitors Exchange Online inboxes for unread emails, reconstructs threads, ingests into ThreadWeave | Mail.Read |
| SharePoint Watcher | Discovers SharePoint sites and monitors document libraries for changes | Sites.Read.All |
| Graph External Connector | Pushes ThreadWeave entries to Microsoft Graph so they appear in Copilot and Microsoft Search results | ExternalConnection.ReadWrite.OwnedBy |

> **Already ingesting email?** If you just want to read your own inbox, use `ingest_graph_mail.py` instead — it uses device-code OAuth (browser sign-in, MFA supported) and needs no Azure app registration. The connectors below are for organization-wide, automated ingestion.

## Prerequisites

- An **Azure subscription** with an Entra ID (Azure AD) tenant
- **Global Administrator** or **Application Administrator** role in that tenant (to grant admin consent)
- For Email Watcher & SharePoint Watcher: **Exchange Online** and **SharePoint Online** licenses in the tenant
- For Graph External Connector: a tenant with **Microsoft Graph connectors** support (M365 E5, or the Graph connectors add-on)
- ThreadWeave installed and running (`threadweave serve`)

> **Don't have a tenant?** The free M365 E5 developer sandbox is no longer open to everyone as of 2026. You now need a Visual Studio Professional/Enterprise subscription or membership in the Microsoft AI Cloud Partner Program. Check with your IT department for a dev/test tenant.

## Step 1: Create the Azure App Registrations

We recommend two separate app registrations — different permissions, different security boundaries.

### App 1: ThreadWeave-GraphReader

Used by the Email, SharePoint, and OneNote connectors (app-only + delegated).

**Application permissions (admin consent):**
- `Mail.Read` — read mailboxes (email watcher)
- `Sites.Read.All` — read SharePoint sites and documents
- `User.Read.All` — resolve sender departments for palace wing mapping

**Delegated permission (for OneNote — see note below):**
- `Notes.Read.All` — read notebook pages via the Graph OneNote API

**Authentication settings:**
- Enable **"Allow public client flows"** = Yes (Authentication → Advanced settings).
  Required for the OneNote device-code sign-in. The delegated OneNote consent
  happens during the one-time sign-in, not via admin consent.

> **Why OneNote needs delegated auth:** Microsoft deprecated app-only tokens
> for the OneNote API on 2025-03-31. The `.one` binary is proprietary and
> unparseable, so notebooks are read through the Graph OneNote API with a
> user-context token. One-time setup: `threadweave sharepoint onenote-login`
> (prints a device code; sign in once; the token cache is persisted at
> `~/.threadweave/msal_cache.json` and refreshed silently thereafter).

1. Go to **Azure Portal** → **Microsoft Entra ID** → **App registrations** → **New registration**
2. Name: `ThreadWeave-GraphReader`
3. Supported account types: **"Accounts in this organizational directory only"**
4. Redirect URI: leave blank (app-only flow)
5. Click **Register**

After creation, note these values:

```
Application (client) ID:  _______________
Directory (tenant) ID:    _______________
```

Now create a client secret and set permissions:

1. **Certificates & secrets** → **New client secret** → name it → 24 months → **Add**
2. Copy the secret **Value** immediately (not the Secret ID — that's different)
3. **API permissions** → **Add a permission** → **Microsoft Graph**
4. Select the **Application permissions** tab (not Delegated)
5. Add: `Mail.Read`, `Sites.Read.All`
6. Click **"Grant admin consent for <tenant>"**

> **Critical:** Adding permissions is not enough. You must click "Grant admin consent." Also, make sure you're on the **Application permissions** tab — the dialog defaults to Delegated, but ThreadWeave uses app-only flow which needs Application permissions.

### App 2: ThreadWeave-CopilotConnector

Used by the Graph External Connector to sync ThreadWeave entries to Copilot.

Repeat the same registration steps, with these differences:

- Name: `ThreadWeave-CopilotConnector`
- Permission: `ExternalConnection.ReadWrite.OwnedBy` (Application)
- Grant admin consent

## Step 2: Set Environment Variables

Set these before starting ThreadWeave:

```bash
# For Email Watcher + SharePoint Watcher
export AZURE_TENANT_ID="your-tenant-id"
export AZURE_CLIENT_ID="your-graphreader-client-id"
export AZURE_CLIENT_SECRET="your-secret-value"

# For Graph External Connector (Copilot)
export THREADWEAVE_GRAPH_TENANT_ID="your-tenant-id"
export THREADWEAVE_GRAPH_CLIENT_ID="your-copilotconnector-client-id"
export THREADWEAVE_GRAPH_CLIENT_SECRET="your-secret-value"
```

On Windows PowerShell:

```powershell
$env:AZURE_TENANT_ID = "your-tenant-id"
$env:AZURE_CLIENT_ID = "your-graphreader-client-id"
$env:AZURE_CLIENT_SECRET = "your-secret-value"
```

## Step 3: Verify Authentication

Before running the connectors, verify your app registrations work:

```python
from msal import ConfidentialClientApplication
import json, base64

app = ConfidentialClientApplication(
    client_id="your-client-id",
    client_credential="your-secret",
    authority=f"https://login.microsoftonline.com/your-tenant-id",
)
result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])

if "access_token" not in result:
    print(f"FAILED: {result.get('error_description', result)}")
else:
    token = result["access_token"]
    parts = token.split('.')
    payload = base64.urlsafe_b64decode(parts[1] + '=' * (4 - len(parts[1]) % 4))
    claims = json.loads(payload)
    roles = claims.get('roles', [])
    print(f"OK — roles: {roles}")
```

If `roles` is empty:
- You used **Delegated** permissions instead of Application — switch tabs in API permissions and re-add
- Admin consent wasn't granted — click the button in API permissions
- You copied the **Secret ID** instead of the **Value** — the Value looks like `Xmf8Q~...`

## Step 4: Run the Connectors (daemons)

All connectors run as continuous daemons that pull M365 content one-way into on-prem ThreadWeave. No webhooks, no tunnels, no third-party relays. Content flows outbound from the on-prem host only.

### Email Watch (continuous email harvesting)

```bash
export AZURE_TENANT_ID=... AZURE_CLIENT_ID=... AZURE_CLIENT_SECRET=...
uv run python -m threadweave.cli email watch \
  --mailbox Admin@your-tenant.com --interval 300
```

Polls unread mail, groups conversations into threads, runs detection, and ingests knowledge. Flags: `--interval` (300s), `--max-results` (20), `--mark-read` (default OFF), `--no-threads` (skip thread grouping). Safe to restart anytime: in-memory dedup (1000) prevents re-processing within a run.

### SharePoint Watch (continuous document harvesting)

```bash
uv run python -m threadweave.cli sharepoint watch \
  --interval 300 --site "Mark 8" --onenote
```

Delta-polls all document libraries (or only sites whose name contains `--site`). New and edited files are extracted (txt/md/docx/pdf/xlsx/pptx/csv/json/etc), detected, and ingested. Delta tokens persist to `~/.threadweave/sharepoint_delta.json`, so restarts resume without re-crawling. `--onenote` also polls notebook pages (watermark-based; new pages and edits are caught).

**OneNote one-time sign-in** (delegated auth, required before `--onenote`):

```bash
uv run python -m threadweave.cli sharepoint onenote-login
```

Prints a device code; sign in once as a tenant user with notebook access. The token cache (`~/.threadweave/msal_cache.json`) is refreshed silently thereafter.

### Graph External Connector (Copilot / Microsoft Search)

```bash
export THREADWEAVE_GRAPH_TENANT_ID=... THREADWEAVE_GRAPH_CLIENT_ID=... THREADWEAVE_GRAPH_CLIENT_SECRET=...
uv run python -m threadweave.cli graph setup    # create connection + register schema
uv run python -m threadweave.cli graph sync     # push entries to the search index
uv run python -m threadweave.cli graph daemon   # continuous sync every 5 min
```

## Publishing the Teams app (org app catalog)

The bot starts as a sideloaded zip; making it official = publishing to the
**org app catalog** so users can install it from the Teams app store.

**Build the package** (reproducible, validated):

```bash
uv run python -m threadweave.cli teams package \
  --bot-id cb342c61-8ab1-4c7b-ac0c-0a7f191acf4b --version 1.0.1
# → dist/threadweave-teams-app-1.0.1.zip (byte-identical rebuilds)
```

The builder checks icon sizes (color 192x192, outline 32x32), RSC
permissions, and manifest fields. Version bumps: `--version 1.0.2` etc.

**Upload to the org app catalog** (admin, one time):

1. **https://admin.teams.microsoft.com** → **Teams apps** → **Manage apps**
2. **Upload new app** → choose `dist/threadweave-teams-app-1.0.1.zip`
3. The app appears in **Manage apps** — click it → **Publish**
4. Users find it under **Apps** → **Built for your org** and install it
   themselves (or you push it via an app setup policy)

**For a pilot:** scope the app to one team by installing it only in that
team's channel (works without a policy); the bot only sees conversations it
is in, so nothing from other teams reaches it. The app ID and bot ID stay
`cb342c61-8ab1-4c7b-ac0c-0a7f191acf4b` across versions — existing installs
update in place.

### Teams Watch (passive channel capture, no bot installs)

The Teams bot only sees conversations it was invited into (@mention, DM, or
RSC-consented teams/chats). For true passive capture of channel messages,
the watch daemon polls Graph directly with app-only permissions. No app
installs, no user action, and it also covers teams where the bot was never
added.

Required app permissions on the app registration (tenant admin consent,
once):

- `ChannelMessage.Read.All` — read channel messages in all teams
- `Team.ReadBasic.All` — enumerate teams and channels
- `TeamsActivity.Send` — activity-feed capture notifications to authors
  who never talked to the bot (also needs `User.Read.All` for email to
  AAD id resolution)
- `Mail.Send` — email fallback for capture notifications in tenants
  that refuse `TeamsActivity.Send`; needs `THREADWEAVE_NOTIFY_SENDER`
  set to a mailbox the app may send from. `User.Read.All` also covers
  AAD id to email resolution for this path.

```bash
uv run python -m threadweave.cli teams watch \
  --interval 300 --team "Mark 8"
```

On a channel's first poll the daemon primes its delta token and captures
only messages posted afterwards (nothing retroactive). To also mine
existing history, pass `--backfill`, capped at `--max-messages` per
channel (default 100, 0 = unlimited). Delta tokens persist to
`~/.threadweave/teams_delta.json`, so restarts resume without
re-crawling. `--team` limits polling to teams whose display name matches
a substring (pilot scoping).

Messages are filtered before ingestion: system events, bot/app posts,
unattributable messages, opted-out authors, and texts under 50 characters
are skipped. Everything else goes through the central ingest pipeline
(detection, PII gate, opt-out gate, capture notification queue).

Credentials: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`,
`AZURE_CLIENT_SECRET` (the same app registration the SharePoint watcher
uses, with the two Teams permissions added). Register as a service with
`threadweave daemon install teams-watch`.

### RSC consent (bot passive capture, separate admin step)

The Teams manifest declares two RSC (Resource-Specific Consent)
permissions that let the bot receive ALL messages in a conversation,
not just @mentions:

- `ChannelMessage.Read.Group` — channels of teams where the app is installed
- `ChatMessage.Read.Chat` — group chats the bot was added to

**Declaring RSC in the manifest grants nothing.** A tenant admin must
consent explicitly. Without that step the bot silently degrades to
@mention-only capture with no error anywhere. (The teams-watch daemon
does not depend on RSC at all, it uses Graph app permissions.)

**Grant consent (Teams admin center):**

1. https://admin.teams.microsoft.com → **Teams apps** → **Manage apps**
2. Select **ThreadWeave** → **Permissions** tab
3. **Review permissions and consent** → grant
   `ChannelMessage.Read.Group` and `ChatMessage.Read.Chat`
   (menu labels vary by portal version; an org-wide PowerShell
   preapproval path also exists, see Microsoft's RSC preapproval docs)

**The consent probe.** The bot tracks the teams it observes (install
events, messages) and verifies each team's granted RSC permissions via
Graph `GET /teams/{id}/permissionGrants`, matching the grant's
`clientAppId` against the app id. It runs at startup and whenever a new
team is seen; a missing grant logs a loud warning, and the results are
exposed as `rsc_status` on the bot's `/health` endpoint. For the probe
to read the grant list, the app registration also needs
`TeamsAppInstallation.ReadForTeam.All` (application, admin consent).

## Step 5: Verify Each Connector

### Email Watcher

Fetches unread emails from an Exchange Online inbox and reconstructs conversation threads:

```bash
uv run python -c "
import asyncio, os, sys
sys.path.insert(0, '.')
from threadweave.connectors.email.watcher import MailWatcher

async def test():
    watcher = MailWatcher()
    emails = await watcher.fetch_unread(
        mailbox='user@your-tenant.com',
        max_results=5,
    )
    print(f'Fetched {len(emails)} unread emails')
    for e in emails:
        print(f'  {e.subject[:60]} — {e.sender_name}')

asyncio.run(test())
"
```

Expected: lists unread emails from the specified mailbox.

### SharePoint Watcher

Discovers SharePoint sites in your tenant:

```bash
uv run python -c "
import asyncio, os, sys
sys.path.insert(0, '.')
from threadweave.connectors.sharepoint.watcher import GraphClient

async def test():
    client = GraphClient()
    sites = await client.list_sites()
    print(f'Found {len(sites)} SharePoint sites')
    for s in sites:
        print(f'  {s.display_name} — {s.web_url}')

asyncio.run(test())
"
```

Expected: lists SharePoint sites. If you get `400 Bad Request: Tenant does not have a SPO license`, your tenant lacks SharePoint Online.

### OneNote Watcher

Reads notebook pages via the Graph OneNote API (delegated auth, see the `onenote-login` step above). Verify page listing works before enabling `--onenote`:

```bash
uv run python -c "
import asyncio, os, sys
sys.path.insert(0, '.')
from threadweave.connectors.sharepoint.onenote import OneNoteClient
from threadweave.connectors.sharepoint.watcher import GraphClient

async def test():
    onenote = OneNoteClient()
    gc = GraphClient()
    sites = await gc.list_sites()
    for s in sites:
        if s.display_name == 'Your Site':
            pages = await onenote.list_pages(s.site_id)
            print(f'{len(pages)} pages')
            for p in pages:
                print(f'  {p.title} — {p.last_modified}')

asyncio.run(test())
"
```

Expected: lists notebook pages. Errors to expect if setup is wrong: `40001` (app-only token — must use delegated), `40004` (missing `Notes.Read.All`), `AADSTS65002` (using the Azure CLI client ID — must use your own app with public client flows enabled).

### Graph External Connector

Creates an external connection and registers the ThreadWeave schema:

```bash
uv run python -c "
from threadweave.connectors.graph.connector import ThreadWeaveGraphConnector

connector = ThreadWeaveGraphConnector()
print(f'Configured: {connector.is_configured}')

# Create connection and register schema
result = connector.register_schema()
print(f'Schema registered: {result}')

# Verify connection
info = connector.get_connection()
print(f'Connection state: {info}')
"
```

Expected: connection created (201), schema registered. If schema registration fails with `400`, your tenant may lack Graph Connectors licensing.

## Troubleshooting

| Error | Likely Cause | Fix |
|---|---|---|
| `AADSTS7000215: Invalid client secret` | Used Secret ID instead of Value | Copy the **Value** field from Certificates & secrets, not the Secret ID GUID |
| `AADSTS700016: Application not found` | Wrong tenant directory | Check top-right of Azure Portal for active directory. App registrations are per-tenant |
| `roles` is empty in JWT | Delegated permissions used, or admin consent not granted | Switch to Application permissions tab, re-add, click "Grant admin consent" |
| `401 Unauthorized` on mailbox | Mailbox not in tenant, or no Exchange Online license | Verify user has Exchange Online and is in the same tenant as the app registration |
| `400: Tenant does not have a SPO license` | No SharePoint Online in tenant | Tenant needs SharePoint Online license. Bare Entra ID tenants don't include it |
| `500` on `POST /external/connections` | No Graph connectors license | Requires M365 E5 or Graph connectors add-on. Dev sandbox may not support it |
| `403` on upsert/delete | Schema not registered yet | Run `register_schema()` first — items can't be created without a schema |
| `40001` on `/onenote/...` | App-only token used for OneNote | OneNote requires delegated auth since 2025-03-31 — run `sharepoint onenote-login` and use `--onenote` |
| `40004` on `/onenote/...` | Missing `Notes.Read.All` scope | Add `Notes.Read.All` (Delegated) to GraphReader and re-sign-in |
| `AADSTS65002` on device sign-in | Used the Azure CLI client ID | Use your own app registration with "Allow public client flows" = Yes |
| `403` on a site's drives | App lacks that site collection | Known for root/communication sites with `Sites.Read.All` — the daemon logs and continues |

## Production Checklist

- Client secrets expire — set a calendar reminder for 24 months from creation
- Store secrets in a vault (Azure Key Vault, HashiCorp Vault), not in shell history
- Use separate app registrations per environment (dev/staging/prod)
- Monitor the audit log at `/api/v1/audit/recent` for connector activity
- Test with `--dry-run` and small `--max` values before full ingestion
- Enable the OneNote device-code sign-in with a service account that has
  notebook access, and keep the MSAL cache (`~/.threadweave/msal_cache.json`)
  backed up with the other state files
- Publish the privacy contract: see **`docs/privacy.md`** (transparency,
  opt-out, right to delete, access control) and tell users about the Teams
  commands `opt out` / `opt in` / `delete <topic>` / `status`
