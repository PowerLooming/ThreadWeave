# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""
ThreadWeave CLI — command-line interface for organizational memory.

Usage:
    threadweave detect "some text to analyze"
    threadweave save --wing engineering --room deployment --content "Always check CI..."
    threadweave search "Postgres migration"
    threadweave serve
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

from threadweave.detector import detect, is_worth_saving
from threadweave.daemons import DAEMONS, daemons_for_profile  # noqa: E402


def cmd_detect(args):
    """Analyze text for knowledge potential."""
    should_save, result = is_worth_saving(args.text)
    output = {
        "should_save": should_save,
        "content_type": result.content_type.value,
        "confidence": round(result.confidence, 3),
        "signals": result.signals,
        "entities": result.entities,
        "suggested_scope": result.suggested_scope,
        "suggested_title": result.suggested_title,
        "has_pii": result.has_pii,
    }
    print(json.dumps(output, indent=2))


def cmd_search(args):
    """Search organizational memory."""
    import httpx
    from threadweave.auth import api_headers

    try:
        resp = httpx.post(
            f"http://{args.host}:{args.port}/api/v1/search",
            json={"query": args.query, "limit": args.limit},
            headers=api_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        print(f"\nFound {data['total']} results for: {args.query}\n")
        for i, r in enumerate(data["results"], 1):
            print(f"{i}. [{r['wing']}/{r['room']}] {r['title'] or r['content_preview'][:80]}")
            print(f"   Score: {r['relevance_score']:.2f}  |  {r['created_at'][:10]}")
            print()
    except Exception as e:
        print(f"Error: {e}. Is the server running? (threadweave serve)", file=sys.stderr)
        sys.exit(1)


def cmd_save(args):
    """Save knowledge to organizational memory."""
    import httpx
    from threadweave.auth import api_headers

    payload = {
        "content": args.content,
        "wing": args.wing,
        "room": args.room or "general",
        "scope": args.scope or "team",
        "source_type": args.source or "manual",
        "author_id": args.author or "unknown",
    }

    try:
        resp = httpx.post(
            f"http://{args.host}:{args.port}/api/v1/entries",
            json=payload,
            headers=api_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        print(f"Saved: {data['id']} -> {data['wing']}/{data['room']}")
    except Exception as e:
        print(f"Error: {e}. Is the server running? (threadweave serve)", file=sys.stderr)
        sys.exit(1)


def cmd_serve(args):
    """Start the ThreadWeave API server."""
    import logging

    # --profile overrides the env var for this process so the API module
    # (imported below by uvicorn) sees it. get_profile() is authoritative;
    # an explicit flag just sets the same env it reads.
    if getattr(args, "profile", None):
        os.environ["THREADWEAVE_PROFILE"] = args.profile
    from threadweave.profile import get_profile
    profile = get_profile()
    import uvicorn

    # Startup lines are the only record of how the server came up (which entry
    # store, how many rows were restored from it). Python's logging defaults to
    # WARNING, so those INFO lines never reached the log: the only thing an
    # operator ever saw was a swallowed warning. Give the application logger a
    # handler at INFO unless something already configured one.
    app_logger = logging.getLogger("threadweave")
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(levelname)s %(name)s: %(message)s")
        )
        app_logger.addHandler(handler)
    app_logger.setLevel(logging.INFO)

    print(f"ThreadWeave API starting on http://{args.host}:{args.port}")
    print(f"Profile: {profile}")
    print(f"Docs: http://{args.host}:{args.port}/docs")
    uvicorn.run(
        "threadweave.api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


# ── Action Item (Tasks) Commands ─────────────────────────────────

def _print_task(entry: dict, idx: int) -> None:
    md = entry.get("source_metadata") or {}
    owner = md.get("action_owner_name") or md.get("action_owner") or "(unresolved)"
    status = md.get("action_status", "open")
    deadline = md.get("action_deadline", "")
    deadline_txt = f"  [by {deadline}]" if deadline else ""
    status_txt = f"  ({status})" if status != "open" else ""
    print(f"{idx}. {owner}: {md.get('action', entry.get('content', ''))}"
          f"{deadline_txt}{status_txt}")
    print(f"   {entry['id']}")


def cmd_tasks_list(args):
    """List open action items (optionally by owner)."""
    from threadweave.store import EntryStore
    from threadweave.action_items import list_open_tasks

    store = EntryStore()
    owner = args.owner or ""
    entries = list_open_tasks(store, owner=owner)
    if not entries:
        print("No open action items." + (f" for {owner}" if owner else ""))
        return
    print(f"{len(entries)} open action item(s):\n")
    for i, e in enumerate(entries, 1):
        _print_task(e, i)


def cmd_tasks_team(args):
    """List open action items for a manager's direct reports."""
    from threadweave.store import EntryStore
    from threadweave.org_model import OrgModel
    from threadweave.action_items import list_open_tasks

    store = EntryStore()
    org = OrgModel()
    manager = args.manager or args.owner or ""
    if not manager:
        manager = _current_user()
    reports = org.get_direct_reports(manager)
    if not reports:
        print(f"No direct reports found for '{manager}'.")
        return
    entries = []
    for r in reports:
        entries.extend(list_open_tasks(store, owner=r))
    if not entries:
        print(f"No open action items for {len(reports)} direct report(s) of '{manager}'.")
        return
    print(f"{len(entries)} open action item(s) for {len(reports)} direct report(s) of "
          f"'{manager}':\n")
    for i, e in enumerate(entries, 1):
        _print_task(e, i)


def cmd_tasks_done(args):
    """Mark an action item as done."""
    from threadweave.store import EntryStore
    from threadweave.action_items import set_task_status, ActionStatus

    store = EntryStore()
    ok = set_task_status(store, args.entry_id, ActionStatus.DONE)
    if ok:
        print(f"Marked {args.entry_id} as done.")
    else:
        print(f"No open action item found with id '{args.entry_id}'.", file=sys.stderr)
        sys.exit(1)


def cmd_tasks_undone(args):
    """Reopen a done/suggested-done action item."""
    from threadweave.store import EntryStore
    from threadweave.action_items import set_task_status, ActionStatus

    store = EntryStore()
    ok = set_task_status(store, args.entry_id, ActionStatus.OPEN)
    if ok:
        print(f"Reopened {args.entry_id} (status = open).")
    else:
        print(f"No action item found with id '{args.entry_id}'.", file=sys.stderr)
        sys.exit(1)


def cmd_tasks_reassign(args):
    """Reassign an open action item to a new owner."""
    from threadweave.store import EntryStore
    from threadweave.action_items import reassign_task

    store = EntryStore()
    ok = reassign_task(store, args.entry_id, args.to, args.name or "")
    if ok:
        print(f"Reassigned {args.entry_id} to {args.name or args.to}.")
    else:
        print(f"No open action item found with id '{args.entry_id}'.", file=sys.stderr)
        sys.exit(1)


def cmd_tasks_digest(args):
    """Print a weekly follow-up digest of action items."""
    from threadweave.store import EntryStore
    from threadweave.action_items import build_digest

    store = EntryStore()
    owner = args.owner or ""
    digest = build_digest(store, owner=owner)
    label = f" for {owner}" if owner else ""
    print(f"Action-item digest{label}:")
    print(f"  open:                  {digest['open_count']}")
    print(f"  due soon (<=7d):       {digest['due_soon_count']}")
    print(f"  overdue:               {digest['overdue_count']}")
    print(f"  pending confirmation:  {digest['pending_confirmation_count']}")

    def _fmt(e):
        md = e.get("source_metadata") or {}
        owner_txt = md.get("action_owner_name") or md.get("action_owner") or "(unresolved)"
        deadline = md.get("action_deadline", "")
        dl = f"  [by {deadline}]" if deadline else ""
        return f"    {owner_txt}: {md.get('action', e.get('content',''))}{dl}  ({e['id']})"

    if digest["overdue"]:
        print("\nOverdue:")
        for e in digest["overdue"]:
            print(_fmt(e))
    if digest["due_soon"]:
        print("\nDue soon:")
        for e in digest["due_soon"]:
            print(_fmt(e))
    if digest["pending_confirmation"]:
        print("\nAwaiting confirmation (suggested done):")
        for e in digest["pending_confirmation"]:
            print(_fmt(e))


def _current_user() -> str:
    """Best-effort current OS user as an owner fallback."""
    import getpass
    return getpass.getuser()


# ── Graph Connector Commands ───────────────────────────────────────

def cmd_graph_setup(args):
    """Register the ThreadWeave external connection schema with Microsoft Graph."""
    from threadweave.connectors.graph.sync import SyncEngine
    from threadweave.connectors.graph.connector import ThreadWeaveGraphConnector

    connector = ThreadWeaveGraphConnector(
        threadweave_url=f"http://{args.host}:{args.port}",
    )
    engine = SyncEngine(connector)

    print("Registering ThreadWeave connection schema with Microsoft Graph...")
    success = engine.schema_setup()
    if success:
        print("Schema registered successfully.")
        print(f"Connection ID: threadweave")
        print("Items can now be synced via: threadweave graph sync")
    else:
        print("Schema registration failed. Check credentials and permissions.",
              file=sys.stderr)
        sys.exit(1)


def cmd_graph_sync(args):
    """Sync ThreadWeave entries to Microsoft Graph."""
    from threadweave.connectors.graph.sync import SyncEngine
    from threadweave.connectors.graph.connector import ThreadWeaveGraphConnector

    connector = ThreadWeaveGraphConnector(
        threadweave_url=f"http://{args.host}:{args.port}",
    )
    engine = SyncEngine(connector)

    print(f"Syncing ThreadWeave entries to Microsoft Graph...")
    stats = engine.full_sync()
    print(json.dumps(stats.to_dict(), indent=2))


def cmd_graph_status(args):
    """Show Graph connector status."""
    from threadweave.connectors.graph.sync import SyncEngine
    from threadweave.connectors.graph.connector import ThreadWeaveGraphConnector

    connector = ThreadWeaveGraphConnector(
        threadweave_url=f"http://{args.host}:{args.port}",
    )
    engine = SyncEngine(connector)

    status = engine.status()
    print(json.dumps(status, indent=2))


def cmd_graph_daemon(args):
    """Run continuous sync daemon."""
    from threadweave.connectors.graph.sync import SyncEngine
    from threadweave.connectors.graph.connector import ThreadWeaveGraphConnector

    connector = ThreadWeaveGraphConnector(
        threadweave_url=f"http://{args.host}:{args.port}",
    )
    engine = SyncEngine(connector, sync_interval=args.interval)
    engine.run_daemon()


# ── Teams package builder ─────────────────────────────────────────

def cmd_teams_package(args):
    """Build a store-ready Teams app package (manifest zip)."""
    from threadweave.connectors.teams.package import (
        build_package, _validate_manifest, build_manifest,
    )

    bot_id = args.bot_id or os.environ.get("MICROSOFT_APP_ID", "")
    if not bot_id:
        print("teams package requires --bot-id (or MICROSOFT_APP_ID).",
              file=sys.stderr)
        sys.exit(1)

    manifest = build_manifest(bot_id, args.version)
    problems = _validate_manifest(manifest)
    if problems:
        print("Manifest validation problems:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(1)

    zip_path, icon_problems = build_package(
        bot_id=bot_id,
        version=args.version,
        color_icon=args.color_icon,
        outline_icon=args.outline_icon,
        out_dir=args.out_dir,
    )
    if icon_problems:
        print("Icon problems (package built anyway):", file=sys.stderr)
        for p in icon_problems:
            print(f"  - {p}", file=sys.stderr)

    print(f"Package built: {zip_path}")
    print(f"  version:    {args.version}")
    print(f"  bot id:     {bot_id}")
    print(f"  size:       {zip_path.stat().st_size} bytes")
    print("Upload to: Teams admin center → Teams apps → Manage apps → "
          "Upload new app (org app catalog)")


def cmd_teams_publish(args):
    """Upload a package to the org app catalog via Graph."""
    from threadweave.connectors.teams.publish import (
        AlreadyInCatalog, TeamsAppPublisher,
    )

    zip_path = args.package
    if not os.path.exists(zip_path):
        print(f"Package not found: {zip_path}", file=sys.stderr)
        sys.exit(1)

    publisher = TeamsAppPublisher()
    print(f"Uploading {zip_path} to the org app catalog...")
    try:
        app = publisher.upload(zip_path)
    except AlreadyInCatalog as exc:
        print(f"Already published: {exc}")
        sys.exit(0)
    app_id = app.get("id", "")
    print(f"Uploaded: id={app_id}")

    # Poll for readiness so the admin gets a definitive answer.
    try:
        ready = publisher.wait_ready(app_id, timeout=args.wait)
        status = ready.get("appDefinitions", [{}])[0].get(
            "publishingState", "unknown"
        ) if ready.get("appDefinitions") else "unknown"
        print(f"Catalog status: {status}")
    except TimeoutError as exc:
        print(f"Warning: {exc} — check the admin center.")

    print("\nNext steps in the Teams admin center:")
    print("  1. https://admin.teams.microsoft.com → Teams apps → Manage apps")
    print(f"  2. Find '{app.get('displayName', 'ThreadWeave')}' → Publish")
    print("     (submitted for review — as admin you approve it here)")
    print("  3. Users install from Apps → Built for your org")


def cmd_teams_watch(args):
    """Continuously harvest Teams channel messages via Graph delta polling."""
    import asyncio
    from threadweave.connectors.teams.watcher import (
        TeamsGraphClient, TeamsWatchDaemon,
    )

    graph = TeamsGraphClient()
    daemon = TeamsWatchDaemon(
        graph=graph,
        interval=args.interval,
        api_base_url=args.api_url,
        backfill=args.backfill,
        max_messages=args.max_messages,
        team_filter=args.team,
    )
    asyncio.run(daemon.run())


def cmd_org_sync(args):
    """Continuously sync teams and members into the org model."""
    import asyncio
    from threadweave.orgsync import OrgSyncDaemon
    from threadweave.connectors.sharepoint.watcher import GraphClient

    graph = GraphClient()
    daemon = OrgSyncDaemon(
        graph=graph,
        interval=args.interval,
        api_base_url=args.api_url,
    )
    asyncio.run(daemon.run())


# ── Daemon Management (packaging) ──────────────────────────────────

def cmd_daemon_run(args):
    """Exec a daemon with its env file loaded (used by OS services)."""
    from threadweave.daemons import run_daemon
    sys.exit(run_daemon(args.name))


def cmd_daemon_install(args):
    from threadweave.daemons import install
    install(args.name)


def cmd_daemon_uninstall(args):
    from threadweave.daemons import uninstall
    uninstall(args.name)


def cmd_daemon_status(args):
    from threadweave.daemons import status
    if args.name == "all":
        for name in daemons_for_profile():
            st = status(name)
            print(f"{name}: {'installed' if st.get('installed') else 'not installed'}")
        return
    st = status(args.name)
    print(f"{args.name}: {'installed' if st.get('installed') else 'not installed'}")


def cmd_daemon_config(args):
    from threadweave.daemons import save_daemon_env, load_daemon_env
    if args.show:
        for k, v in sorted(load_daemon_env(args.name).items()):
            if "SECRET" in k or "PASSWORD" in k or "PASS" in k:
                print(f"{k}=***")
            else:
                print(f"{k}={v}")
        return
    values = {}
    for group in args.set or []:
        for kv in group:
            if "=" in kv:
                k, _, v = kv.partition("=")
                values[k.strip()] = v.strip()
    save_daemon_env(args.name, values)
    print(f"{args.name}: {len(values)} values saved to "
          "~/.threadweave/daemons/ config")


# ── SharePoint Commands ────────────────────────────────────────────

def cmd_sharepoint_watch(args):
    """Run continuous SharePoint delta polling (M365 -> on-prem, one-way)."""
    import asyncio
    from threadweave.connectors.sharepoint.watcher import GraphClient
    from threadweave.connectors.sharepoint.processor import DocumentProcessor
    from threadweave.connectors.sharepoint.daemon import SharePointWatchDaemon
    from threadweave.connectors.sharepoint.onenote import OneNoteClient

    graph = GraphClient()
    processor = DocumentProcessor(graph)
    onenote = OneNoteClient() if args.onenote else None
    daemon = SharePointWatchDaemon(
        graph=graph,
        processor=processor,
        interval=args.interval,
        site_filter=args.site,
        state_file=args.state_file,
        onenote_client=onenote,
    )
    asyncio.run(daemon.run())


def cmd_sharepoint_onenote_login(args):
    """Interactive OneNote sign-in (device code, caches token)."""
    import asyncio
    from threadweave.connectors.sharepoint.onenote import OneNoteClient

    client = OneNoteClient(cache_file=args.cache_file)
    token = client.get_token(interactive=True)
    print(f"OneNote sign-in OK (token {len(token)} chars, cached).")


# ── Email Commands ────────────────────────────────────────────────

def _resolve_mail_auth(explicit: str | None = None) -> str:
    """Mail access mode: "app" (client credentials) or "delegated" (as the owner).

    Precedence: explicit flag, then THREADWEAVE_MAIL_AUTH, then the runtime
    profile — the personal profile reads mail as the signed-in owner, which is
    what makes its no-org-wide-grants claim true.
    """
    from threadweave.profile import is_personal

    mode = (explicit or os.environ.get("THREADWEAVE_MAIL_AUTH", "") or "").strip().lower()
    if mode in ("app", "delegated"):
        return mode
    return "delegated" if is_personal() else "app"


def cmd_email_watch(args):
    """Run continuous email polling (M365 -> on-prem, one-way)."""
    import asyncio
    from threadweave.profile import get_owner_id
    from threadweave.connectors.email.watcher import MailWatcher
    from threadweave.connectors.email.processor import EmailProcessor
    from threadweave.connectors.email.daemon import EmailWatchDaemon

    delegated = _resolve_mail_auth(getattr(args, "mail_auth", None)) == "delegated"

    if delegated:
        # The whole point of the mode: no configured mailbox and no client
        # secret, just the owner's own consent.
        args.mailbox = args.mailbox or get_owner_id()
    elif not args.mailbox:
        print("Email watcher requires --mailbox (or THREADWEAVE_MAILBOX).",
              file=sys.stderr)
        sys.exit(1)

    watcher = MailWatcher(delegated=delegated)
    if delegated:
        # Fail fast: a missing or unconsented token must stop the start, not
        # surface once per poll in a daemon that otherwise looks healthy.
        try:
            watcher._delegated_auth.get_token()
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        print(f"Delegated mail access as {watcher.delegated_account() or '(account unresolved)'}")
    # Pass a Graph client so the processor can map sender -> department
    # -> wing (palace model). Same credentials as the watcher. In delegated
    # mode there is none: every mail lands in the fallback wing.
    processor = EmailProcessor(graph_client=watcher.graph)
    daemon = EmailWatchDaemon(
        watcher=watcher,
        processor=processor,
        mailbox=args.mailbox,
        interval=args.interval,
        max_results=args.max_results,
        mark_read=args.mark_read,
        use_threads=not args.no_threads,
    )
    asyncio.run(daemon.run())


def cmd_email_login(args):
    """One-time device-code sign-in so mail can be read as the owner."""
    from threadweave.connectors.email.delegated import DelegatedMailAuth

    auth = DelegatedMailAuth(cache_file=args.cache_file)
    auth.get_token(interactive=True)
    print(f"Signed in as {auth.account() or '(unknown account)'}")


# ── Google Workspace Commands ──────────────────────────────────────

def cmd_gws_check(args):
    """Verify Google Workspace connectivity and list accessible resources."""
    from threadweave.connectors.gws.auth import GWSCredentials, GWSAuth
    from threadweave.connectors.gws.gmail import GmailWatcher
    from threadweave.connectors.gws.chat import ChatListener

    creds = GWSCredentials.from_env()
    if not creds or not creds.is_configured():
        print("GWS credentials not configured.", file=sys.stderr)
        print("Set THREADWEAVE_GWS_CREDENTIALS_PATH and THREADWEAVE_GWS_DELEGATED_ACCOUNT",
              file=sys.stderr)
        sys.exit(1)

    auth = GWSAuth(creds)
    print(f"Authenticated as: {creds.delegated_account}")

    # Test Gmail
    try:
        watcher = GmailWatcher(auth, threadweave_url=f"http://{args.host}:{args.port}")
        msgs = watcher.fetch_recent(max_results=3)
        print(f"Gmail: {len(msgs)} recent messages accessible")
    except Exception as e:
        print(f"Gmail: ERROR — {e}")

    # Test Chat
    try:
        listener = ChatListener(auth, threadweave_url=f"http://{args.host}:{args.port}")
        spaces = listener.list_spaces()
        print(f"Chat: {len(spaces)} spaces accessible")
        for s in spaces[:5]:
            print(f"  - {s.get('displayName', s.get('name', '?'))}")
    except Exception as e:
        print(f"Chat: ERROR — {e}")

    # Test Drive
    try:
        from threadweave.connectors.gws.drive import DriveCrawler
        crawler = DriveCrawler(auth, threadweave_url=f"http://{args.host}:{args.port}")
        docs = crawler.crawl(max_results=3)
        print(f"Drive: {len(docs)} documents accessible")
    except Exception as e:
        print(f"Drive: ERROR — {e}")


def cmd_gws_sync(args):
    """One-shot sync: Gmail + Chat + Drive → ThreadWeave."""
    from threadweave.connectors.gws.auth import GWSCredentials, GWSAuth
    from threadweave.connectors.gws.gmail import GmailWatcher
    from threadweave.connectors.gws.chat import ChatListener
    from threadweave.connectors.gws.drive import DriveCrawler

    creds = GWSCredentials.from_env()
    if not creds or not creds.is_configured():
        print("GWS credentials not configured.", file=sys.stderr)
        sys.exit(1)

    auth = GWSAuth(creds)
    base_url = f"http://{args.host}:{args.port}"
    total = {"submitted": 0, "saved": 0, "skipped": 0, "errors": 0}

    if args.source in ("all", "gmail"):
        print("--- Gmail ---")
        w = GmailWatcher(auth, threadweave_url=base_url)
        s = w.process_inbox(query=args.query or "")
        for k in total:
            total[k] += s.get(k, 0)
        print(json.dumps(s, indent=2))

    if args.source in ("all", "chat"):
        print("--- Chat ---")
        c = ChatListener(auth, threadweave_url=base_url)
        s = c.process_all_spaces()
        for k in total:
            total[k] += s.get(k, 0)
        print(json.dumps(s, indent=2))

    if args.source in ("all", "drive"):
        print("--- Drive ---")
        d = DriveCrawler(auth, threadweave_url=base_url)
        s = d.process_drive(query=args.query or "")
        for k in total:
            total[k] += s.get(k, 0)
        print(json.dumps(s, indent=2))

    print(f"\nTotal: {json.dumps(total, indent=2)}")


def cmd_gws_watch(args):
    """Continuous polling for new GWS content."""
    import time
    from threadweave.connectors.gws.auth import GWSCredentials, GWSAuth
    from threadweave.connectors.gws.gmail import GmailWatcher
    from threadweave.connectors.gws.chat import ChatListener
    from threadweave.connectors.gws.drive import DriveCrawler

    creds = GWSCredentials.from_env()
    if not creds or not creds.is_configured():
        print("GWS credentials not configured.", file=sys.stderr)
        sys.exit(1)

    auth = GWSAuth(creds)
    base_url = f"http://{args.host}:{args.port}"
    interval = args.interval or 300

    print(f"Starting GWS watcher (interval={interval}s, source={args.source})")
    print("Press Ctrl+C to stop.\n")

    gmail = GmailWatcher(auth, threadweave_url=base_url)
    chat = ChatListener(auth, threadweave_url=base_url)
    drive = DriveCrawler(auth, threadweave_url=base_url)

    try:
        while True:
            tick = datetime.now(timezone.utc).isoformat()

            if args.source in ("all", "gmail"):
                try:
                    s = gmail.process_inbox(query="newer_than:1h")
                    if s.get("submitted", 0) > 0:
                        print(f"[{tick[:19]}] Gmail: {json.dumps(s)}")
                except Exception as e:
                    print(f"[{tick[:19]}] Gmail error: {e}")

            if args.source in ("all", "chat"):
                try:
                    s = chat.process_all_spaces()
                    if s.get("submitted", 0) > 0:
                        print(f"[{tick[:19]}] Chat: {json.dumps(s)}")
                except Exception as e:
                    print(f"[{tick[:19]}] Chat error: {e}")

            if args.source in ("all", "drive"):
                try:
                    s = drive.process_drive(query="modifiedTime > '{}'".format(
                        (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
                    ))
                    if s.get("submitted", 0) > 0:
                        print(f"[{tick[:19]}] Drive: {json.dumps(s)}")
                except Exception as e:
                    print(f"[{tick[:19]}] Drive error: {e}")

            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nWatcher stopped.")


def cmd_gws_harvest(args):
    """Harvest knowledge from a departing employee's Google Workspace."""
    from threadweave.connectors.gws.auth import GWSCredentials, GWSAuth
    from threadweave.connectors.gws.harvest import OffboardingHarvester

    if not args.email:
        print("Error: --email is required (the departing employee's email)",
              file=sys.stderr)
        sys.exit(1)

    creds = GWSCredentials.from_env()
    if not creds or not creds.is_configured():
        print("GWS credentials not configured.", file=sys.stderr)
        sys.exit(1)

    auth = GWSAuth(creds)
    harvester = OffboardingHarvester(
        auth,
        threadweave_url=f"http://{args.host}:{args.port}",
    )

    sources = args.source.split(",") if args.source != "all" else None
    stats = harvester.harvest_all(
        user_email=args.email,
        sources=sources,
        max_messages=args.max_messages,
        max_files=args.max_files,
    )

    print(json.dumps(stats.to_dict(), indent=2))


def cmd_gws_harvest_report(args):
    """Show the harvest report for a specific user."""
    from threadweave.connectors.gws.auth import GWSCredentials, GWSAuth
    from threadweave.connectors.gws.harvest import OffboardingHarvester

    creds = GWSCredentials.from_env()
    if not creds or not creds.is_configured():
        print("GWS credentials not configured.", file=sys.stderr)
        sys.exit(1)

    auth = GWSAuth(creds)
    harvester = OffboardingHarvester(
        auth,
        threadweave_url=f"http://{args.host}:{args.port}",
    )

    report = harvester.generate_report(args.email)
    if report:
        print(json.dumps(report, indent=2))
    else:
        print(f"No harvest report found for {args.email}")
        print(f"Run: threadweave gws harvest {args.email}")


def cmd_gws_onboard(args):
    """Generate an onboarding knowledge brief for a new hire."""
    from threadweave.connectors.gws.harvest import generate_onboarding_brief

    if not args.email or not args.predecessor or not args.wing:
        print("Error: --email, --predecessor, and --wing are required",
              file=sys.stderr)
        sys.exit(1)

    brief = generate_onboarding_brief(
        new_hire_email=args.email,
        predecessor_email=args.predecessor,
        team_wing=args.wing,
        threadweave_url=f"http://{args.host}:{args.port}",
    )

    print(f"\n{'=' * 60}")
    print(f"  Onboarding Brief: {args.email}")
    print(f"  Team: {args.wing}")
    print(f"  Predecessor: {args.predecessor}")
    print(f"{'=' * 60}\n")

    if brief["predecessor_knowledge"]:
        print(f"📚 Knowledge from {args.predecessor}:")
        for k in brief["predecessor_knowledge"][:10]:
            print(f"   • {k['preview'][:100]}...")
        print()

    if brief["team_knowledge"]:
        print(f"👥 Team knowledge ({args.wing}):")
        for k in brief["team_knowledge"][:5]:
            print(f"   • {k['preview'][:100]}...")
        print()

    if brief["recent_decisions"]:
        print("📋 Recent decisions:")
        for k in brief["recent_decisions"][:5]:
            print(f"   • {k['preview'][:100]}...")
        print()

    print("✅ Onboarding checklist:")
    for item in brief["onboarding_checklist"]:
        print(f"   ☐ {item}")

    print(f"\nTotal knowledge entries: "
          f"{len(brief['predecessor_knowledge']) + len(brief['team_knowledge'])}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ThreadWeave — Organizational Memory System",
    )
    sub = parser.add_subparsers(dest="command")

    # detect
    p_detect = sub.add_parser("detect", help="Analyze text for knowledge potential")
    p_detect.add_argument("text", help="Text to analyze")

    # search
    p_search = sub.add_parser("search", help="Search organizational memory")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.add_argument("--host", default="localhost")
    p_search.add_argument("--port", type=int, default=8000)

    # save
    p_save = sub.add_parser("save", help="Save knowledge to organizational memory")
    p_save.add_argument("--content", required=True, help="Knowledge content")
    p_save.add_argument("--wing", required=True, help="Team/department")
    p_save.add_argument("--room", default="general", help="Topic")
    p_save.add_argument("--scope", default="team")
    p_save.add_argument("--source", default="cli")
    p_save.add_argument("--author", default="cli-user")
    p_save.add_argument("--host", default="localhost")
    p_save.add_argument("--port", type=int, default=8000)

    # serve
    p_serve = sub.add_parser("serve", help="Start the API server")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.add_argument(
        "--profile", choices=["org", "personal"], default=None,
        help="Runtime profile (overrides THREADWEAVE_PROFILE): "
             "'personal' runs single-user, owner-scoped mode.")

    # graph — M365 Copilot connector
    p_graph = sub.add_parser("graph", help="Microsoft 365 Copilot Graph connector")
    graph_sub = p_graph.add_subparsers(dest="graph_command")

    p_graph_setup = graph_sub.add_parser("setup", help="Register connection schema with Microsoft Graph")
    p_graph_setup.add_argument("--host", default="localhost")
    p_graph_setup.add_argument("--port", type=int, default=8000)

    p_graph_sync = graph_sub.add_parser("sync", help="Full sync to Microsoft Graph")
    p_graph_sync.add_argument("--host", default="localhost")
    p_graph_sync.add_argument("--port", type=int, default=8000)

    p_graph_status = graph_sub.add_parser("status", help="Show connector status")
    p_graph_status.add_argument("--host", default="localhost")
    p_graph_status.add_argument("--port", type=int, default=8000)

    p_graph_daemon = graph_sub.add_parser("daemon", help="Run continuous sync daemon")
    p_graph_daemon.add_argument("--host", default="localhost")
    p_graph_daemon.add_argument("--port", type=int, default=8000)
    p_graph_daemon.add_argument("--interval", type=int, default=300,
                                help="Sync interval in seconds (default: 300)")

    # gws — Google Workspace connector
    p_gws = sub.add_parser("gws", help="Google Workspace connector (Gmail, Chat, Drive)")
    gws_sub = p_gws.add_subparsers(dest="gws_command")

    p_gws_check = gws_sub.add_parser("check", help="Verify GWS connectivity")
    p_gws_check.add_argument("--host", default="localhost")
    p_gws_check.add_argument("--port", type=int, default=8000)

    p_gws_sync = gws_sub.add_parser("sync", help="One-shot sync from GWS to ThreadWeave")
    p_gws_sync.add_argument("--host", default="localhost")
    p_gws_sync.add_argument("--port", type=int, default=8000)
    p_gws_sync.add_argument("--source", default="all",
                            choices=["all", "gmail", "chat", "drive"])
    p_gws_sync.add_argument("--query", default="",
                            help="Gmail/Drive search query filter")

    p_gws_watch = gws_sub.add_parser("watch", help="Continuous GWS polling")
    p_gws_watch.add_argument("--host", default="localhost")
    p_gws_watch.add_argument("--port", type=int, default=8000)
    p_gws_watch.add_argument("--source", default="all",
                             choices=["all", "gmail", "chat", "drive"])
    p_gws_watch.add_argument("--interval", type=int, default=300)

    p_gws_harvest = gws_sub.add_parser("harvest", help="Harvest knowledge from departing employee")
    p_gws_harvest.add_argument("--email", required=True,
                               help="Email of the departing employee")
    p_gws_harvest.add_argument("--host", default="localhost")
    p_gws_harvest.add_argument("--port", type=int, default=8000)
    p_gws_harvest.add_argument("--source", default="all",
                               choices=["all", "gmail", "chat", "drive"])
    p_gws_harvest.add_argument("--max-messages", type=int, default=5000)
    p_gws_harvest.add_argument("--max-files", type=int, default=500)

    p_gws_report = gws_sub.add_parser("harvest-report",
                                      help="Show harvest report for a user")
    p_gws_report.add_argument("--email", required=True)
    p_gws_report.add_argument("--host", default="localhost")
    p_gws_report.add_argument("--port", type=int, default=8000)

    p_gws_onboard = gws_sub.add_parser("onboard",
                                       help="Generate onboarding knowledge brief")
    p_gws_onboard.add_argument("--email", required=True,
                               help="New hire's email")
    p_gws_onboard.add_argument("--predecessor", required=True,
                               help="Predecessor's email")
    p_gws_onboard.add_argument("--wing", required=True,
                               help="Team wing/department")
    p_gws_onboard.add_argument("--host", default="localhost")
    p_gws_onboard.add_argument("--port", type=int, default=8000)

    p_email = sub.add_parser("email", help="Microsoft 365 email connector")
    p_email_watch = email_sub = p_email.add_subparsers(dest="email_command")
    p_email_watch = email_sub.add_parser("watch",
                                         help="Continuous polling of a mailbox "
                                              "(M365 -> on-prem, one-way)")
    p_email_watch.add_argument("--mailbox",
                               default=os.environ.get("THREADWEAVE_MAILBOX", ""),
                               help="Mailbox UPN/email (or THREADWEAVE_MAILBOX)")
    p_email_watch.add_argument("--interval", type=int, default=300,
                               help="Poll interval in seconds (default 300)")
    p_email_watch.add_argument("--max-results", type=int, default=20,
                               help="Unread messages fetched per poll")
    p_email_watch.add_argument("--mark-read", action="store_true",
                               help="Mark processed emails as read")
    p_email_watch.add_argument("--no-threads", action="store_true",
                               help="Process messages individually, skip "
                                    "conversation thread grouping")
    p_email_watch.add_argument(
        "--mail-auth", choices=["app", "delegated"], default=None,
        help="app = client-credentials over a configured mailbox; "
             "delegated = read the signed-in owner's own mailbox "
             "(personal-profile default; overrides THREADWEAVE_MAIL_AUTH)")
    p_email_login = email_sub.add_parser(
        "login", help="One-time device-code sign-in for delegated mail access")
    p_email_login.add_argument(
        "--cache-file",
        default=os.path.expanduser("~/.threadweave/msal_cache.json"),
        help="MSAL token cache path")

    p_sharepoint = sub.add_parser("sharepoint",
                                  help="Microsoft 365 SharePoint connector")
    sp_sub = p_sharepoint.add_subparsers(dest="sharepoint_command")
    p_sp_watch = sp_sub.add_parser("watch",
                                   help="Continuous delta polling of document "
                                        "libraries (M365 -> on-prem, one-way)")
    p_sp_watch.add_argument("--interval", type=int, default=300,
                            help="Poll interval in seconds (default 300)")
    p_sp_watch.add_argument("--site", default="",
                            help="Only watch sites whose name contains this "
                                 "substring (default: all sites)")
    p_sp_watch.add_argument("--state-file",
                            default=os.path.expanduser(
                                "~/.threadweave/sharepoint_delta.json"),
                            help="Path to the delta-token state file")
    p_sp_watch.add_argument("--onenote", action="store_true",
                            help="Also poll OneNote notebooks (requires "
                                 "one-time sign-in: sharepoint onenote-login)")
    p_sp_login = sp_sub.add_parser(
        "onenote-login",
        help="Interactive OneNote sign-in (device code, caches token)")
    p_sp_login.add_argument("--cache-file",
                            default=os.path.expanduser(
                                "~/.threadweave/msal_cache.json"),
                            help="MSAL token cache path")

    p_daemon = sub.add_parser(
        "daemon", help="Manage connector daemons as OS services")
    daemon_sub = p_daemon.add_subparsers(dest="daemon_command")

    # tasks — action items
    p_tasks = sub.add_parser(
        "tasks", help="Action items: list, mark done, manager view")
    tasks_sub = p_tasks.add_subparsers(dest="tasks_command")
    p_tasks_list = tasks_sub.add_parser(
        "list", help="List open action items (optionally by owner)")
    p_tasks_list.add_argument("--owner", default="",
                              help="Only list items assigned to this owner")
    p_tasks_list.add_argument("--host", default="localhost")
    p_tasks_list.add_argument("--port", type=int, default=8000)
    p_tasks_team = tasks_sub.add_parser(
        "team", help="List open action items of a manager's direct reports")
    p_tasks_team.add_argument("--manager", default="",
                              help="Manager id (default: current OS user)")
    p_tasks_team.add_argument("--owner", default="",
                              help="Alias for --manager")
    p_tasks_done = tasks_sub.add_parser(
        "done", help="Mark an action item as done")
    p_tasks_done.add_argument("entry_id")
    p_tasks_undone = tasks_sub.add_parser(
        "undone", help="Reopen a done action item")
    p_tasks_undone.add_argument("entry_id")
    p_tasks_reassign = tasks_sub.add_parser(
        "reassign", help="Reassign an open action item to a new owner")
    p_tasks_reassign.add_argument("entry_id")
    p_tasks_reassign.add_argument("--to", required=True,
                                  help="New owner id")
    p_tasks_reassign.add_argument("--name", default="",
                                  help="New owner display name")
    p_tasks_digest = tasks_sub.add_parser(
        "digest", help="Print a weekly follow-up digest of action items")
    p_tasks_digest.add_argument("--owner", default="",
                                help="Only include this owner's items")

    p_teams = sub.add_parser("teams", help="Teams app tooling")
    teams_sub = p_teams.add_subparsers(dest="teams_command")
    p_teams_pkg = teams_sub.add_parser(
        "package", help="Build a store-ready app package (manifest zip)")
    p_teams_pkg.add_argument("--bot-id", default="",
                             help="Bot application ID (default: "
                                  "MICROSOFT_APP_ID env)")
    p_teams_pkg.add_argument("--version", default="1.0.0",
                             help="Package version (default 1.0.0)")
    p_teams_pkg.add_argument("--color-icon",
                             default=os.path.join(
                                 os.path.dirname(os.path.dirname(
                                     os.path.dirname(os.path.abspath(__file__)))),
                                 "assets/teams/color.png"),
                             help="Color icon path (192x192)")
    p_teams_pkg.add_argument("--outline-icon",
                             default=os.path.join(
                                 os.path.dirname(os.path.dirname(
                                     os.path.dirname(os.path.abspath(__file__)))),
                                 "assets/teams/outline.png"),
                             help="Outline icon path (32x32)")
    p_teams_pkg.add_argument("--out-dir", default="dist",
                             help="Output directory (default: dist)")
    p_teams_pub = teams_sub.add_parser(
        "publish", help="Upload a package to the org app catalog "
                        "(device-code sign-in)")
    p_teams_pub.add_argument("--package", default="dist/threadweave-bot-manifest.zip",
                             help="Package zip path")
    p_teams_pub.add_argument("--wait", type=int, default=120,
                             help="Seconds to poll for catalog readiness")
    p_teams_watch = teams_sub.add_parser(
        "watch", help="Continuously harvest channel messages via "
                      "Graph delta polling (no bot installs needed)")
    p_teams_watch.add_argument("--interval", type=int, default=300,
                               help="Seconds between polls (default 300)")
    p_teams_watch.add_argument("--backfill", action="store_true",
                               help="Process full channel history on the "
                                    "first poll (default: start from now)")
    p_teams_watch.add_argument("--max-messages", type=int, default=100,
                               help="Per-channel history cap in backfill "
                                    "mode, 0 = unlimited (default 100)")
    p_teams_watch.add_argument("--team", default="",
                               help="Only watch teams whose display name "
                                    "contains this substring")
    p_teams_watch.add_argument(
        "--api-url",
        default=os.environ.get("THREADWEAVE_API_URL", "http://localhost:8000"),
        help="ThreadWeave API server URL (default: THREADWEAVE_API_URL "
             "or http://localhost:8000)")

    org_sub = sub.add_parser(
        "org", help="Org model: who is in which team (org tracker)")
    org_cmds = org_sub.add_subparsers(dest="org_command")
    p_org_sync = org_cmds.add_parser(
        "sync", help="Continuously sync teams and members from Graph "
                     "into the org model")
    p_org_sync.add_argument("--interval", type=int, default=3600,
                            help="Seconds between syncs (default 3600)")
    p_org_sync.add_argument(
        "--api-url",
        default=os.environ.get("THREADWEAVE_API_URL", "http://localhost:8000"),
        help="ThreadWeave API server URL (default: THREADWEAVE_API_URL "
             "or http://localhost:8000)")

    p_d_run = daemon_sub.add_parser(
        "run", help="Run a daemon with its env file (used by services)")
    p_d_run.add_argument("name", choices=list(daemons_for_profile()))

    p_d_install = daemon_sub.add_parser(
        "install", help="Register a daemon as a scheduled task / systemd unit")
    p_d_install.add_argument("name", choices=list(daemons_for_profile()))

    p_d_uninstall = daemon_sub.add_parser(
        "uninstall", help="Remove a daemon's service registration")
    p_d_uninstall.add_argument("name", choices=list(daemons_for_profile()))

    p_d_status = daemon_sub.add_parser(
        "status", help="Show daemon service status")
    p_d_status.add_argument("name", choices=["all"] + list(daemons_for_profile()))

    p_d_config = daemon_sub.add_parser(
        "config", help="Read/write a daemon's env file")
    p_d_config.add_argument("name", choices=list(daemons_for_profile()))
    p_d_config.add_argument("--set", action="append", nargs="+", default=[],
                            help="KEY=VALUE to set (repeatable, "
                                 "space-separated values)")
    p_d_config.add_argument("--show", action="store_true",
                            help="Show current values (secrets masked)")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "detect":
        cmd_detect(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "save":
        cmd_save(args)
    elif args.command == "serve":
        cmd_serve(args)
    elif args.command == "teams":
        if args.teams_command == "package":
            cmd_teams_package(args)
        elif args.teams_command == "publish":
            cmd_teams_publish(args)
        elif args.teams_command == "watch":
            cmd_teams_watch(args)
        else:
            p_teams.print_help()
    elif args.command == "daemon":
        if args.daemon_command == "run":
            cmd_daemon_run(args)
        elif args.daemon_command == "install":
            cmd_daemon_install(args)
        elif args.daemon_command == "uninstall":
            cmd_daemon_uninstall(args)
        elif args.daemon_command == "status":
            cmd_daemon_status(args)
        elif args.daemon_command == "config":
            cmd_daemon_config(args)
        else:
            p_daemon.print_help()
    elif args.command == "graph":
        if args.graph_command == "setup":
            cmd_graph_setup(args)
        elif args.graph_command == "sync":
            cmd_graph_sync(args)
        elif args.graph_command == "status":
            cmd_graph_status(args)
        elif args.graph_command == "daemon":
            cmd_graph_daemon(args)
        else:
            p_graph.print_help()
    elif args.command == "tasks":
        if args.tasks_command == "list":
            cmd_tasks_list(args)
        elif args.tasks_command == "team":
            cmd_tasks_team(args)
        elif args.tasks_command == "done":
            cmd_tasks_done(args)
        elif args.tasks_command == "undone":
            cmd_tasks_undone(args)
        elif args.tasks_command == "reassign":
            cmd_tasks_reassign(args)
        elif args.tasks_command == "digest":
            cmd_tasks_digest(args)
        else:
            p_tasks.print_help()
    elif args.command == "org":
        if args.org_command == "sync":
            cmd_org_sync(args)
        else:
            org_sub.print_help()
    elif args.command == "gws":
        if args.gws_command == "check":
            cmd_gws_check(args)
        elif args.gws_command == "sync":
            cmd_gws_sync(args)
        elif args.gws_command == "watch":
            cmd_gws_watch(args)
        elif args.gws_command == "harvest":
            cmd_gws_harvest(args)
        elif args.gws_command == "harvest-report":
            cmd_gws_harvest_report(args)
        elif args.gws_command == "onboard":
            cmd_gws_onboard(args)
        else:
            p_gws.print_help()
    elif args.command == "email":
        if args.email_command == "watch":
            cmd_email_watch(args)
        elif args.email_command == "login":
            cmd_email_login(args)
        else:
            p_email.print_help()
    elif args.command == "sharepoint":
        if args.sharepoint_command == "watch":
            cmd_sharepoint_watch(args)
        elif args.sharepoint_command == "onenote-login":
            cmd_sharepoint_onenote_login(args)
        else:
            p_sharepoint.print_help()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
