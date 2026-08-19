# ThreadWeave Pilot Plan — Kongsberg Maritime

## Overview

ThreadWeave is an on-prem organisational memory system. It watches the
knowledge that already moves through everyday work, email, chat, and
documents, and turns it into a searchable store, so what people know
does not silently leave when they do.

The privacy contract is the product: all data stays on your server,
content flows one way from your Microsoft 365 tenant into the local
store, and nothing is ever sent out to a third party and back again.

This pilot is deliberately small. We start with one mailbox, prove the
capture quality, and only then widen the scope.

## What you need

- A server you control, able to reach Microsoft Graph (outbound HTTPS).
- Microsoft 365 tenant with an account that can register an app and
  grant admin consent.
- One pilot user's mailbox (their email address) and their department
  name.
- For a non-English pilot, a language model. We support a local Ollama
  instance, which keeps everything on your server too.

## How it works

The email connector polls the pilot mailbox on a schedule, reads new
mail, and classifies each message. Mail that looks like durable
knowledge, a decision or an explanation, is stored; everything else,
newsletters, acknowledgments, short replies, is skipped. Nothing is
written back to the mailbox, and messages are never marked as read
unless you explicitly ask for that.

## Setup

### 1. Install on your server

ThreadWeave runs with Python 3.11 and `uv`. From the package directory:

```bash
uv run python -m threadweave.cli serve
```

This starts the API and the local store on port 8000. Keep it running.

### 2. Register the app in Microsoft Entra ID

Create an app registration so ThreadWeave can read mail through
Microsoft Graph with its own identity.

1. Microsoft Entra admin center, Applications, App registrations, New
   registration.
2. Give it a name, leave the account type at single tenant.
3. Note the Application (client) ID and Directory (tenant) ID.
4. Under Certificates & secrets, add a new client secret and note its
   value now, it is shown only once.
5. Under API permissions, Add a permission, Microsoft Graph,
   Application permissions, and add:
   - `Mail.Read` (read the pilot mailbox)
   - `User.Read.All` (map senders to their department)
6. Grant admin consent for both.

ThreadWeave only needs read access. `Mail.ReadWrite` is not required;
we do not mark messages as read by default.

### 3. Configure the email connector

Point the connector at the pilot mailbox and set your app credentials:

```bash
uv run python -m threadweave.cli daemon config email-watch \
  --set AZURE_TENANT_ID=<tenant id> \
  --set AZURE_CLIENT_ID=<client id> \
  --set AZURE_CLIENT_SECRET=<client secret> \
  --set THREADWEAVE_EMAIL_MAILBOX=<pilot user email>
```

### 4. Configure the language model (non-English pilots)

For a non-English pilot, point the classifier at a local Ollama
instance:

```bash
uv run python -m threadweave.cli daemon config email-watch \
  --set THREADWEAVE_LLM_PROVIDER=ollama \
  --set THREADWEAVE_LLM_BASE_URL=http://localhost:11434/v1 \
  --set THREADWEAVE_LLM_MODEL=qwen3.5:9b
```

If you leave this out, the classifier uses a built-in rules engine that
is tuned for English. Privacy-sensitive detection, such as national ID
numbers and salary figures, is multilingual either way.

### 5. Start the harvest

```bash
uv run python -m threadweave.cli daemon run email-watch
```

Each poll prints a summary line:
`fetched=N processed=N submitted=N skipped=N errors=N`

`submitted` is how many emails were stored; `skipped` is how many were
deliberately ignored.

## Email harvest

The harvest is the ongoing loop: poll the pilot mailbox, classify new
mail, and store the messages that look like durable knowledge. It runs
as a background process.

For the pilot you can run it in the foreground and watch the output:

```bash
uv run python -m threadweave.cli daemon run email-watch
```

To keep it running across reboots, install it as a service:

```bash
uv run python -m threadweave.cli daemon install email-watch
```

Every poll prints one line:

```text
fetched=N processed=N submitted=N skipped=N errors=N
```

`fetched` is how many new messages were read. `submitted` is how many
were stored as knowledge. `skipped` is how many were deliberately
ignored, newsletters, acknowledgments, short replies. A healthy harvest
shows a small `submitted` count and a larger `skipped` count, because
most email is not durable knowledge.

To confirm captures landed, search the store:

```bash
uv run python -m threadweave.cli search "a topic you know appeared in email"
```

Or open the dashboard (next section) and look under Recent Entries.

## Dashboard

The dashboard is a web page served by ThreadWeave. With the server
running, open a browser to:

```text
http://localhost:8000
```

It shows:

- Search Memory, a search box over everything captured.
- Recent Entries, the latest stored knowledge.
- All Wings, the teams ThreadWeave has seen, with the number of
  entries in each.

The interactive API reference is at:

```text
http://localhost:8000/docs
```

This is the fastest way to inspect individual entries and the capture
state during calibration.

## Tuning the threshold

Two settings control what gets stored. Both are configuration values,
so tuning is a settings change, not a code change.

- `THREADWEAVE_EMAIL_MIN_CONFIDENCE` (default 0.40). The classifier
  scores each email from 0 to 1. Only emails scored at or above this,
  and classified as a decision or an answer, are stored. Lower it to
  capture more, raise it to capture less.
- `THREADWEAVE_EMAIL_MIN_BODY_LENGTH` (default 100). Emails shorter
  than this many characters are skipped outright.

We calibrate together: run a short list of sample emails through the
classifier, inspect what is stored and what is skipped, and settle the
two values with you.

```bash
uv run python -m threadweave.cli daemon config email-watch \
  --set THREADWEAVE_EMAIL_MIN_CONFIDENCE=0.30
uv run python -m threadweave.cli daemon config email-watch \
  --set THREADWEAVE_EMAIL_MIN_BODY_LENGTH=80
```

Restart the daemon after each change and re-check the submitted and
skipped counts.

## Privacy & consent

ThreadWeave treats capture as something people should be able to see
and control, not something that happens silently.

- Capture notification (the "camera sign"). When a person's email is
  captured, they receive a notice naming what was captured and how to
  delete it or opt out.
- Opt-out registry. Any participant can opt out, and the connector
  checks the registry before ingesting anything. Opt-out is enforced
  per entry and audited.

For the single-user phase this means the pilot participant sees exactly
what is captured and can remove anything, at any time, with one
command.

## Success criteria

- The store holds a representative set of the pilot user's real
  knowledge emails, correctly placed under their team.
- Deliberate noise (newsletters, acknowledgments, short replies) is
  skipped; genuine decisions and explanations are captured.
- The participant confirms the capture notification and can delete or
  opt out without friction.
- The two threshold values are documented and agreed.

## What's next

1. Add the participant's teammates, a handful at a time.
2. Add a second source (Teams channels or SharePoint) if applicable.
3. Move from calibration to a standing rollout with the agreed
   thresholds and the opt-out registry in place.
