#!/usr/bin/env python3
"""Harvest emails from new Outlook / M365 via Microsoft Graph API.

No Azure app registration needed. No COM. No export. No IMAP.
Just sign in with your browser (MFA fully supported).

Usage:
    python ingest_graph_mail.py                    # Inbox, last 50
    python ingest_graph_mail.py --folder "Sent Items" --max 100
    python ingest_graph_mail.py --dry-run          # Preview only
    python ingest_graph_mail.py --wing engineering --room architecture

First run opens a browser for you to sign in.
Token is cached locally — subsequent runs are instant.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

API = "http://localhost:8000/api/v1/ingest"
GRAPH_API = "https://graph.microsoft.com/v1.0"

# Microsoft's well-known "Microsoft Authentication Library" client ID
# for public client apps — works with device code flow without any
# Azure registration. Used by Microsoft's own tools and SDKs.
CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"  # Azure CLI client
SCOPES = ["Mail.Read"]
TOKEN_FILE=Path.home() / ".threadweave" / "graph_token.json"

# Cache folder listing so we only fetch it once
_FOLDER_CACHE: dict[str, str] | None = None


# ── Auth ─────────────────────────────────────────────────────────


def _get_msal():
    """Lazy import — MSAL is optional."""
    try:
        from msal import PublicClientApplication
        return PublicClientApplication
    except ImportError:
        print("This script needs msal. Install it:")
        print("  pip install msal")
        sys.exit(1)


def _acquire_token() -> str:
    """Get a Graph API token via device code flow. Caches to disk."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Try cached token
    if TOKEN_FILE.exists():
        try:
            cached = json.loads(TOKEN_FILE.read_text())
            if cached.get("expires_at", 0) > time.time() + 60:
                return cached["access_token"]
        except (json.JSONDecodeError, KeyError):
            pass

    PublicClientApplication = _get_msal()
    app = PublicClientApplication(CLIENT_ID, authority="https://login.microsoftonline.com/common")

    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        print("Failed to start device auth. Your org may block this method.")
        sys.exit(1)

    print(f"\nSign in: {flow['verification_uri']}")
    print(f"Code:    {flow['user_code']}")
    print("\nWaiting for you to sign in...")

    result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        error = result.get("error_description", result.get("error", "unknown"))
        print(f"\nSign-in failed: {error}")
        sys.exit(1)

    # Cache it
    token_data = {
        "access_token": result["access_token"],
        "expires_at": time.time() + result.get("expires_in", 3600),
    }
    TOKEN_FILE.write_text(json.dumps(token_data))

    return result["access_token"]


def _graph_request(path: str) -> dict:
    """Make a Graph API request."""
    token = _acquire_token()
    headers = {"Authorization": f"Bearer {token}"}
    resp = httpx.get(f"{GRAPH_API}{path}", headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ── Folder discovery ─────────────────────────────────────────────


def _get_folder_id(folder_name: str) -> str:
    """Resolve a folder name (e.g. 'Inbox', 'Sent Items') to Graph ID."""
    global _FOLDER_CACHE
    if _FOLDER_CACHE is None:
        data = _graph_request("/me/mailFolders?$top=50")
        _FOLDER_CACHE = {}
        for f in data.get("value", []):
            _FOLDER_CACHE[f.get("displayName", "").lower()] = f["id"]

    fid = _FOLDER_CACHE.get(folder_name.lower())
    if fid:
        return fid

    # Also check well-known folder names
    well_known = {
        "inbox": "inbox",
        "sent items": "sentitems",
        "drafts": "drafts",
        "deleted items": "deleteditems",
        "archive": "archive",
        "junk email": "junkemail",
    }
    wk = well_known.get(folder_name.lower())
    if wk:
        return wk

    print(f"Folder '{folder_name}' not found. Available:")
    for name in sorted(_FOLDER_CACHE):
        print(f"  {name}")
    sys.exit(1)


# ── Email parsing ────────────────────────────────────────────────


def strip_html(text: str) -> str:
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_message(item: dict) -> dict | None:
    """Extract subject, body, sender from Graph API message."""
    subject = item.get("subject", "").strip()
    sender_info = item.get("from", {}).get("emailAddress", {})
    sender = sender_info.get("name", "") or sender_info.get("address", "")
    sender_email = sender_info.get("address", "")
    date = item.get("receivedDateTime", "")

    # Prefer text body, fall back to HTML
    body_data = item.get("body", {})
    if body_data.get("contentType") == "text":
        body = body_data.get("content", "")
    else:
        body = strip_html(body_data.get("content", ""))

    if not body or len(body) < 50:
        return None

    return {
        "content": body,
        "subject": subject,
        "sender": f"{sender} <{sender_email}>" if sender else sender_email,
        "date": date,
    }


# ── Main ─────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Harvest emails from new Outlook / M365 via Microsoft Graph"
    )
    parser.add_argument("--folder", default="Inbox", help="Mail folder (default: Inbox)")
    parser.add_argument("--max", dest="max_results", type=int, default=50)
    parser.add_argument("--wing", default="email", help="Team/department")
    parser.add_argument("--room", default="inbox", help="Topic")
    parser.add_argument("--port", type=int, default=8000, help="ThreadWeave API port")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    args = parser.parse_args()

    api_url = f"http://localhost:{args.port}/api/v1/ingest"

    print(f"Folder: {args.folder}  |  Max: {args.max_results}")
    print(f"Wing: {args.wing}  |  Room: {args.room}")
    if args.dry_run:
        print("DRY RUN — nothing will be saved")
    print()

    # Get folder ID
    folder_id = _get_folder_id(args.folder)
    token = _acquire_token()
    headers = {"Authorization": f"Bearer {token}", "Prefer": 'outlook.body-content-type="text"'}

    # Fetch messages
    url = (
        f"{GRAPH_API}/me/mailFolders/{folder_id}/messages"
        f"?$top={args.max_results}"
        f"&$orderby=receivedDateTime desc"
        f"&$select=id,subject,from,body,receivedDateTime"
    )
    resp = httpx.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    messages = data.get("value", [])

    if not messages:
        print("No messages found.")
        return

    print(f"Found {len(messages)} messages\n")

    saved = 0
    skipped = 0
    errors = 0

    for i, item in enumerate(messages, 1):
        email_data = parse_message(item)
        if email_data is None:
            skipped += 1
            continue

        if args.dry_run:
            print(f"[{i}/{len(messages)}] {email_data['subject'][:80]}")
            print(f"    From: {email_data['sender']}")
            print(f"    Body: {len(email_data['content'])} chars")
            print()
            continue

        try:
            resp = httpx.post(api_url, json={
                "content": email_data["content"],
                "source": "email",
                "tenant_id": "default",
                "metadata": {
                    "wing": args.wing,
                    "room": args.room,
                    "title": email_data["subject"],
                    "author_id": email_data["sender"],
                    "email_date": email_data["date"],
                },
            }, timeout=300)
            resp.raise_for_status()
            result = resp.json()

            if result["should_save"]:
                saved += 1
                status = "SAVED"
            else:
                skipped += 1
                status = "SKIP"

            detail = ""
            if result.get("deduplicated"):
                detail = " (duplicate)"
            elif result.get("has_pii"):
                detail = " (PII rejected)"
            else:
                detail = f" ({result['content_type']}, {result['confidence']:.2f})"

            print(f"[{i}/{len(messages)}] {status}{detail} | {email_data['subject'][:70]}")

        except Exception as e:
            errors += 1
            print(f"[{i}/{len(messages)}] ERROR | {email_data['subject'][:70]}")
            print(f"    {e}")

    print()
    print(f"Done: {saved} saved, {skipped} skipped, {errors} errors")


if __name__ == "__main__":
    main()
