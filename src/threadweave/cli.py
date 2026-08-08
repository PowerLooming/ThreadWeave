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
from threadweave.daemons import DAEMONS  # noqa: E402  (registered before parser)


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

    try:
        resp = httpx.post(
            f"http://{args.host}:{args.port}/api/v1/search",
            json={"query": args.query, "limit": args.limit},
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
    import uvicorn
    print(f"ThreadWeave API starting on http://{args.host}:{args.port}")
    print(f"Docs: http://{args.host}:{args.port}/docs")
    uvicorn.run(
        "threadweave.api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


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
        from threadweave.daemons import DAEMONS
        for name in DAEMONS:
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

def cmd_email_watch(args):
    """Run continuous email polling (M365 -> on-prem, one-way)."""
    import asyncio
    from threadweave.connectors.email.watcher import MailWatcher
    from threadweave.connectors.email.processor import EmailProcessor
    from threadweave.connectors.email.daemon import EmailWatchDaemon
    from threadweave.connectors.sharepoint.watcher import GraphClient

    if not args.mailbox:
        print("Email watcher requires --mailbox (or THREADWEAVE_MAILBOX).",
              file=sys.stderr)
        sys.exit(1)

    watcher = MailWatcher()
    # Pass a Graph client so the processor can map sender -> department
    # -> wing (palace model). Same credentials as the watcher.
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


def main():
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

    p_d_run = daemon_sub.add_parser(
        "run", help="Run a daemon with its env file (used by services)")
    p_d_run.add_argument("name", choices=list(DAEMONS))

    p_d_install = daemon_sub.add_parser(
        "install", help="Register a daemon as a scheduled task / systemd unit")
    p_d_install.add_argument("name", choices=list(DAEMONS))

    p_d_uninstall = daemon_sub.add_parser(
        "uninstall", help="Remove a daemon's service registration")
    p_d_uninstall.add_argument("name", choices=list(DAEMONS))

    p_d_status = daemon_sub.add_parser(
        "status", help="Show daemon service status")
    p_d_status.add_argument("name", choices=["all"] + list(DAEMONS))

    p_d_config = daemon_sub.add_parser(
        "config", help="Read/write a daemon's env file")
    p_d_config.add_argument("name", choices=list(DAEMONS))
    p_d_config.add_argument("--set", action="append", nargs="+", default=[],
                            help="KEY=VALUE to set (repeatable, "
                                 "space-separated values)")
    p_d_config.add_argument("--show", action="store_true",
                            help="Show current values (secrets masked)")

    args = parser.parse_args()

    if args.command == "detect":
        cmd_detect(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "save":
        cmd_save(args)
    elif args.command == "serve":
        cmd_serve(args)
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
