#!/usr/bin/env python3
"""Ingest emails via IMAP into ThreadWeave.

Works with any email provider: Outlook, Gmail, custom domains.
No Azure app, no COM, no export — just your email credentials.

Usage:
    python ingest_imap.py --host outlook.office365.com --user you@company.com
    python ingest_imap.py --host imap.gmail.com --user you@gmail.com
    python ingest_imap.py --folder "Sent Items" --max 100
    python ingest_imap.py --dry-run

Gmail: you need an App Password (not your regular password).
    https://myaccount.google.com/apppasswords
Outlook/M365: use your regular password or an app password if MFA is on.
"""
import argparse
import email
import email.policy
import getpass
import imaplib
import ssl
import sys

import httpx

API = "http://localhost:8000/api/v1/ingest"

# Common IMAP servers
SERVERS = {
    "outlook": "outlook.office365.com",
    "gmail": "imap.gmail.com",
    "yahoo": "imap.mail.yahoo.com",
}


def strip_html(text: str) -> str:
    import re
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_message(raw_bytes: bytes) -> dict | None:
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)

    subject = msg.get("Subject", "").strip()
    sender = msg.get("From", "").strip()
    date = msg.get("Date", "").strip()

    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(errors="replace")
                    break
            elif ctype == "text/html" and not body:
                payload = part.get_payload(decode=True)
                if payload:
                    body = strip_html(payload.decode(errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            ctype = msg.get_content_type()
            if ctype == "text/plain":
                body = payload.decode(errors="replace")
            else:
                body = strip_html(payload.decode(errors="replace"))

    if not body or len(body) < 50:
        return None

    return {"content": body, "subject": subject, "sender": sender, "date": date}


def main():
    parser = argparse.ArgumentParser(description="Ingest emails via IMAP into ThreadWeave")
    parser.add_argument("--host", help="IMAP server (or use --provider: outlook, gmail, yahoo)")
    parser.add_argument("--provider", choices=["outlook", "gmail", "yahoo"],
                        help="Shorthand for common providers")
    parser.add_argument("--user", required=True, help="Email address")
    parser.add_argument("--password", help="Password or app password (prompts if omitted)")
    parser.add_argument("--folder", default="INBOX", help="IMAP folder (default: INBOX)")
    parser.add_argument("--max", dest="max_results", type=int, default=50)
    parser.add_argument("--wing", default="email", help="Team/department (default: email)")
    parser.add_argument("--room", default="inbox", help="Topic (default: inbox)")
    parser.add_argument("--port", type=int, default=8000, help="ThreadWeave API port")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    args = parser.parse_args()

    # Resolve server
    host = args.host or SERVERS.get(args.provider or "")
    if not host:
        print("Specify --host or --provider (outlook, gmail, yahoo)")
        sys.exit(1)

    password = args.password or getpass.getpass(f"Password for {args.user}: ")

    api_url = f"http://localhost:{args.port}/api/v1/ingest"

    print(f"Connecting to {host} as {args.user}...")
    print(f"Folder: {args.folder}  |  Max: {args.max_results}")
    print(f"Wing: {args.wing}  |  Room: {args.room}")
    if args.dry_run:
        print("DRY RUN — nothing will be saved")
    print()

    # Connect via IMAP
    try:
        ctx = ssl.create_default_context()
        mail = imaplib.IMAP4_SSL(host, 993, ssl_context=ctx)
        mail.login(args.user, password)
    except imaplib.IMAP4.error as e:
        print(f"Login failed: {e}")
        print()
        if "gmail" in host:
            print("Gmail requires an App Password, not your regular password.")
            print("Create one: https://myaccount.google.com/apppasswords")
        elif "office365" in host or "outlook" in host:
            print("If MFA is enabled, you may need an app password.")
        sys.exit(1)

    try:
        mail.select(f'"{args.folder}"' if " " in args.folder else args.folder)
    except imaplib.IMAP4.error:
        # List available folders
        status, folders = mail.list()
        print(f"Folder '{args.folder}' not found. Available:")
        for f in folders:
            name = f.decode().split('"/" ')[-1].strip('"')
            print(f"  {name}")
        mail.logout()
        sys.exit(1)

    # Search for all messages
    status, ids = mail.search(None, "ALL")
    if status != "OK" or not ids[0]:
        print("No messages found.")
        mail.logout()
        sys.exit(0)

    all_ids = ids[0].split()
    # Take newest N
    target_ids = all_ids[-args.max_results:] if len(all_ids) > args.max_results else all_ids

    print(f"Found {len(all_ids)} messages, processing newest {len(target_ids)}...\n")

    saved = 0
    skipped = 0
    errors = 0

    for i, msg_id in enumerate(reversed(target_ids), 1):  # Newest first
        try:
            status, data = mail.fetch(msg_id, "(RFC822)")
            if status != "OK" or not data or not data[0]:
                skipped += 1
                continue

            raw = data[0][1] if isinstance(data[0], tuple) else data[0]
            email_data = parse_message(raw)
            if email_data is None:
                skipped += 1
                continue

            if args.dry_run:
                print(f"[{i}/{len(target_ids)}] {email_data['subject'][:80]}")
                print(f"    From: {email_data['sender']}")
                print(f"    Body: {len(email_data['content'])} chars")
                print()
                continue

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
            }, timeout=30)
            resp.raise_for_status()
            result = resp.json()

            status = "SAVED" if result["should_save"] else "SKIP"
            if result["should_save"]:
                saved += 1
            else:
                skipped += 1

            reason = ""
            if result.get("deduplicated"):
                reason = "dup"
            elif result.get("has_pii"):
                reason = "PII"
            elif not result["should_save"]:
                reason = f"{result['content_type']} ({result['confidence']:.2f})"
            else:
                reason = f"{result['content_type']} ({result['confidence']:.2f})"

            print(f"[{i}/{len(target_ids)}] {status:5} {reason:20} | {email_data['subject'][:60]}")

        except Exception as e:
            errors += 1
            print(f"[{i}/{len(target_ids)}] ERROR: {e}")

    mail.logout()

    print()
    print(f"Done: {len(target_ids)} emails → {saved} saved, {skipped} skipped, {errors} errors")

    if not args.dry_run and saved > 0:
        print(f"\nSearch: threadweave search '<query>'")


if __name__ == "__main__":
    main()
