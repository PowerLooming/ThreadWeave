# Pilot Test Plan — Teams passive capture (Gate 6)

Covers the three built pieces on branch `teams-watch-daemon`:

1. `teams-watch` daemon: passive Graph delta polling of channel messages
2. Camera sign delivery chain: DM → activity feed → email → skip
3. RSC consent probe: loud warning instead of silent @mention-only mode

Success = Gate 6 acceptance: a channel message in a team where the bot
was never installed is captured within one poll interval, and its
author receives a capture notification.

All commands run on the ThreadWeave host (git-bash, `uv run`).

## 0. Preconditions

### 0.1 Portal: app registration permissions (you do this yourself)

Grant these in batches, in this order, so each delivery path gets tested
with real portal state:

| Batch | Permission | Purpose | When |
|---|---|---|---|
| 1 | `ChannelMessage.Read.All` | teams-watch channel reads | Start of Phase 2 |
| 1 | `Team.ReadBasic.All` | teams-watch team/channel enumeration | Start of Phase 2 |
| 1 | `User.Read.All` | email → AAD id and AAD id → email resolution | Start of Phase 2 |
| 1 | `Mail.Send` | email fallback leg | Phase 3, test 3.3 |
| 1 | `TeamsAppInstallation.ReadForTeam.All` | RSC probe verification call | Phase 1, test 1.3 |
| 2 | `TeamsActivity.Send` | activity-feed leg | Phase 3, test 3.4 (grant LAST) |

Admin consent after each batch. All on the app registration already used
by the SharePoint watcher (`AZURE_CLIENT_ID`).

### 0.2 Host: services and configuration

Start the API server (port 8000) and keep it running for every phase:

```bash
uv run python -m threadweave.cli serve
```

Configure the daemons (secrets land in env files, not shell history):

```bash
# teams-watch: fast interval for testing (30s), no team filter first
uv run python -m threadweave.cli daemon config teams-watch \
  --set AZURE_TENANT_ID=<tenant> AZURE_CLIENT_ID=<app> AZURE_CLIENT_SECRET=<secret>
uv run python -m threadweave.cli daemon config teams-watch \
  --set THREADWEAVE_DAEMON_INTERVAL=30

# teams-bot: bot identity + Graph creds + email sender
uv run python -m threadweave.cli daemon config teams-bot \
  --set MICROSOFT_APP_ID=<bot app id> MICROSOFT_APP_PASSWORD=<bot secret>
uv run python -m threadweave.cli daemon config teams-bot \
  --set AZURE_TENANT_ID=<tenant> AZURE_CLIENT_ID=<app> AZURE_CLIENT_SECRET=<secret>
uv run python -m threadweave.cli daemon config teams-bot \
  --set THREADWEAVE_NOTIFY_SENDER=<service mailbox UPN>
```

Install both as services (Startup folder launchers):

```bash
uv run python -m threadweave.cli daemon install teams-watch
uv run python -m threadweave.cli daemon install teams-bot
```

Verify they run (`daemon status` shows launcher presence; actual
output goes to logs):

```bash
uv run python -m threadweave.cli daemon status all
tail -f ~/.threadweave/logs/teams-watch.log
tail -f ~/.threadweave/logs/teams-bot.log
```

The bot messaging endpoint must stay reachable from Microsoft as in the
existing pilot (tunnel or published endpoint), or camera-sign delivery
tests cannot run.

### 0.3 Teams topology for the tests

- **Team A, Channel Capture**: where passive capture is tested. The
  bot app is NEVER installed here during Phase 2 and Phase 4.
- **Team B, Channel BotHome**: the bot is installed here for Phase 1
  (consent probe) and Phase 3 (DM tests).

## Phase 1: RSC consent probe

**1.1 Missing consent is loud.** Bot installed in Team B, RSC consent
NOT granted yet. Restart the teams-bot process (or reboot). Expected:
log line `RSC consent MISSING for team <Team B id>` with the TAC click
path, and `rsc_status` on `GET http://localhost:3978/health` shows
`{"status": "missing"}` for Team B.

**1.2 New team triggers the probe without a restart.** With the bot
running, install it into a third team (Team C). Expected: within
seconds the log shows `New team observed: <Team C id>` and the MISSING
warning for Team C. This proves the fire-and-forget path.

**1.3 Consent flips the probe to verified.** Teams admin center →
Teams apps → Manage apps → ThreadWeave → Permissions → Review
permissions and consent → grant `ChannelMessage.Read.Group` and
`ChatMessage.Read.Chat`. Restart the bot. Expected: `RSC consent
verified for team <Team B id>: ChannelMessage.Read.Group,
ChatMessage.Read.Chat` and `rsc_status` shows `"granted"`.

**1.4 Negative check (optional).** Empty the `AZURE_CLIENT_ID` in the
teams-bot env, restart, and confirm the log says the check was skipped
instead of erroring. Restore the value afterwards.

Pass: 1.1, 1.2, 1.3 behave as described.

## Phase 2: teams-watch daemon (permission batch 1 granted)

**2.1 Prime mode.** Stop teams-watch. Post message M1 in Team
A/Capture. Start teams-watch. Post message M2. Expected: the first
poll primes the channel's delta token without backfilling, so M1 is
never captured (it predates the prime) and M2 is captured within one
30s interval. Verify the entry:

```bash
uv run python -m threadweave.cli search "text from M2"
```

Check the palace placement: wing = team A display name lowercased with
underscores, room = channel display name.

**2.2 Skip rules.** Post in Team A/Capture: a message from a Teams
workflow/bot app (if you have one) and a reply that is just `+1`.
Expected: bot/app posts and sub-50-character texts are counted as
skipped in the watcher summary line, never ingested.

**2.3 Restart resilience (delta tokens).** Stop teams-watch. Post M3,
M4. Start teams-watch. Expected: only M3 and M4 are processed, M1 and
M2 are not re-read (the persisted delta token resumed the poll).

**2.4 Team filter.** Stop teams-watch. Add a second team with channel
traffic. Configure `THREADWEAVE_TEAMS_FILTER=<Team A name>` and start.
Expected: only Team A messages captured; the summary shows the other
team filtered out.

**2.5 Backfill.** Stop teams-watch. Rename the state file:

```bash
mv ~/.threadweave/teams_delta.json ~/.threadweave/teams_delta.json.bak
```

Set `THREADWEAVE_TEAMS_BACKFILL=1` (leave
`THREADWEAVE_TEAMS_MAX_MESSAGES=100`) and start. Expected: channel
history is captured from newest backwards, capped at 100 per channel.
Restore the env to 0 and stop the daemon afterwards.

**2.6 Opt-out blocks the author.** A user (not you) sends `opt out` in
a DM to the bot. That user then posts M5 in Team A/Capture. Expected:
M5 is skipped (watcher skipped counter increments). Verify with:

```bash
curl http://localhost:8000/api/v1/optout
```

M5's author id appears in the list. The user sends `opt in` and M6 is
captured again.

**2.7 API-down containment.** Stop the API server, post M7, wait one
interval. Expected: the watcher logs ingest errors and keeps polling
(it does not crash). Start the API server again; next poll picks up
the delta (the failed message is retried by the next poll only if
still in the delta page, so do not assert M7 specifically; assert the
watcher recovered and new messages flow).

**2.8 Edit chaining (optional).** Edit a captured message's text.
Expected: a new entry appears chained to the original via
`GET /api/v1/entries/<id>/versions`.

Pass: 2.1, 2.2, 2.3, 2.4, 2.6, 2.7 behave as described.

## Phase 3: camera sign delivery chain

Poll interval note: the bot checks the notification queue every 60s
(`THREADWEAVE_NOTIFY_INTERVAL`); delivery tests need up to a minute
plus one poll interval before asserting.

**3.1 Personal DM for known authors.** You have DMed the bot before
(in Team B or a 1:1). Post a capture-worthy message from your account
in Team A/Capture. Expected: within ~90s you get a DM from the bot:
"Captured to the palace" with delete/opt-out instructions.

**3.2 Channel refs never receive the DM.** A colleague who has only
ever been active in a channel where the bot lives (never DMed it)
posts a capture-worthy message. Expected: NO message lands in the
channel from the bot. The notification goes out via the next leg
(activity feed or email, depending on which permissions are granted).

**3.3 Email fallback (TeamsActivity.Send still NOT granted).** With
batch 2 not yet granted and `THREADWEAVE_NOTIFY_SENDER` set, have a
passive author (never talked to the bot) post a capture-worthy
message. Expected: email arrives at the author's address from the
sender mailbox, with title, wing, and delete/opt-out instructions.
`/api/v1/notifications/stats` shows `delivered` increased.

**3.4 Activity feed (grant batch 2 now).** Grant `TeamsActivity.Send`
and admin consent. Another passive-author message. Expected: the
author gets a Teams activity-feed notification ("Captured to the
palace"), no email this time (the activity leg succeeds first).

**3.5 Undeliverable → skipped.** Remove `THREADWEAVE_NOTIFY_SENDER`
from the teams-bot env (restart the bot) and stop granting any new
permissions. Create a notification for an author with no DM ref, no
activity permission, and no email sender. Wait ~5-6 minutes (5
attempts x 60s). Expected: the log shows "undeliverable after 5
attempts, marked skipped" and `/api/v1/notifications/stats` shows
`skipped: 1`, `delivered` unchanged. Restore the sender afterwards.

**3.6 Delete from the notice.** In any delivered notice, say
`delete <title>` to the bot. Expected: the bot confirms deletion and
the entry disappears from search.

Pass: 3.1 through 3.6 behave as described.

## Phase 4: Gate acceptance

Bot app is NOT installed in Team A/Capture (it never was). A passive
author posts a fresh knowledge message there. Expected, end to end,
with everything granted and configured:

1. teams-watch captures it within one poll interval (default 300s in
   production config, 30s in the test config)
2. The entry is searchable in the palace, wing/room = Team A/Capture
3. The author receives a camera-sign notice (activity feed for a
   passive author, DM if they know the bot)
4. `/api/v1/notifications/stats` shows one delivered, zero skipped

That closes Gate 6 item 1, 2, and the acceptance criterion.

## Observation points

- Watcher log: `~/.threadweave/logs/teams-watch.log`
- Bot log: `~/.threadweave/logs/teams-bot.log`
- Bot health: `GET http://localhost:3978/health` (stats + rsc_status)
- Notifications: `GET http://localhost:8000/api/v1/notifications/pending`
  and `GET http://localhost:8000/api/v1/notifications/stats`
- Opt-out registry: `GET http://localhost:8000/api/v1/optout`
- Dashboard: `http://localhost:8000` (entries tab)
- Delta state: `~/.threadweave/teams_delta.json`
- Seen teams: `~/.threadweave/teams_seen.json`

## Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| Watcher: 403 on list_teams | Missing consent on batch 1 | App registration permissions |
| Watcher: ingest errors, no entries | API server not running | `http://localhost:8000` |
| Nothing captured in prime mode | Expected: pre-prime history is skipped | Use backfill or post fresh messages |
| Captured but no notification | Queue polling, permissions, sender | stats endpoint, bot log delivery chain |
| Probe says MISSING forever | RSC consent never granted in TAC | Phase 1, test 1.3 path |
| Probe errors with 403 | TeamsAppInstallation.ReadForTeam.All missing | Batch 1 |
| Activity feed fails with 403 | TeamsActivity.Send missing | Batch 2 |
| Email fails with 403 | Mail.Send missing or sender not a mailbox | Batch 1 + env |
