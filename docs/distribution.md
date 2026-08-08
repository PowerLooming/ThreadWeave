# ThreadWeave distribution — how customers get the app

How an organization gets ThreadWeave's Teams app into their tenant, and what
it costs. Three tiers, each building on the previous.

## Tier 1: Manual upload (pilot stage)

**What:** the customer admin downloads the app package and uploads it to
their own org app catalog in the Teams admin center.

**How:**

```bash
uv run python -m threadweave.cli teams package --version 1.0.1
# → dist/threadweave-teams-app-1.0.1.zip (deterministic, validated)
```

1. Send the zip to the customer (download page, email, repo release)
2. Customer admin: https://admin.teams.microsoft.com → Teams apps →
   Manage apps → Upload new app → Publish
3. Users install from Apps → Built for your org

**Cost:** $0. Manual portal work per customer.

## Tier 2: Scripted publish (customer onboarding)

**What:** one command uploads the package to the customer's org app catalog
via the Graph API. The admin signs in once with a device code and consents
`AppCatalog.Submit` (must be a Teams admin or app catalog submitter).

**How (run by the customer admin on their machine):**

```bash
uv run python -m threadweave.cli teams publish --package dist/threadweave-teams-app-1.0.1.zip
#  1. Open  https://microsoft.com/devicelogin
#  2. Enter code  XXXX
# → Uploaded: id=...  Catalog status: ...
# → Finish in admin center: Manage apps → Publish
```

The upload goes to `POST /appCatalogs/teamsApps?requiresReview=true` (Graph
v1.0) — the `requiresReview` path is required: `AppCatalog.Submit` submits
for review only (a direct POST 403s even for global admins; verified live).
The app lands in the catalog as pending review, and the admin approves it in
the Teams admin center. A cached MSAL token makes repeat publishes silent;
re-uploads of the same version print "Already published" and exit cleanly.
Environment: `THREADWEAVE_PUBLISH_CLIENT_ID` (app registration with
`AppCatalog.Submit` delegated + public client flows enabled) and
`AZURE_TENANT_ID` — the authority must be tenant-scoped (AADSTS50059 with
the `common` authority).

**Cost:** $0. One sign-in + one click per customer.

## Tier 3: Microsoft Teams Store (product stage)

**What:** the app is listed publicly in the Teams Store; any org installs it
without any upload.

**Requirements (verified from Microsoft's Marketplace Publisher Guide,
2026-08-08):**

- Partner Center account (accept Microsoft Partner Agreement + Publisher
  agreement). Enrollment is free.
- Activate the "Microsoft 365 and Copilot program" within Partner Center
  (required for M365/Teams apps).
- App passes Microsoft validation: working app, manifest compliance (same
  validation service we run at build time), accessibility, privacy policy
  URL, company name consistency. First review takes up to 4 weeks.
- Developer/company name must match the Partner Center publisher name.

**Cost (verified from the FAQ):**

| Item | Cost |
|---|---|
| Listing fee | $0 — "no cost to publish offers" |
| Store service fee | 3% only on transact offers (paid purchases via Microsoft billing) |
| Free app (current model) | $0 forever |
| Certification | Included, no fee |
| Payouts (if transacting) | $50 minimum, 30-day cycles, agency model (Microsoft bills, you set price) |

The Microsoft AI Cloud Partner Program basic membership is free to enroll;
paid Solution Partner designations are optional and not required to publish.

## Recommendation

Ship Tier 1 now (package built, deterministic), use Tier 2 for customer
onboarding (built, live-test with your own tenant: consent AppCatalog.Submit
once, publish your dev tenant's copy), and enter Tier 3 when there is
customer demand — the 4-week review is the only real cost, so time it
against an actual go-to-market date.
