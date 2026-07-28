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

Used by the Email Watcher and SharePoint Watcher.

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

## Step 4: Test Each Connector

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

## Production Checklist

- Client secrets expire — set a calendar reminder for 24 months from creation
- Store secrets in a vault (Azure Key Vault, HashiCorp Vault), not in shell history
- Use separate app registrations per environment (dev/staging/prod)
- Monitor the audit log at `/api/v1/audit/recent` for connector activity
- Test with `--dry-run` and small `--max` values before full ingestion
