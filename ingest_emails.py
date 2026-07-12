#!/usr/bin/env python3
"""Ingest exported .eml files into ThreadWeave.

Usage:
    python ingest_emails.py ~/Desktop/exported-emails/
    python ingest_emails.py ~/Desktop/exported-emails/ --wing legal --room m-and-a

Export from Outlook: select emails → File → Save As → choose a folder.
The script reads every .eml file, extracts subject/body/sender, and submits
to ThreadWeave's ingestion pipeline.
"""
import argparse
import email
import email.policy
import os
import sys
from pathlib import Path

import httpx

API = "http://localhost:8000/api/v1/ingest"


def strip_html(text: str) -> str:
    """Basic HTML to text."""
    import re
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_eml(path: Path) -> dict | None:
    """Extract subject, body, sender from .eml file."""
    try:
        msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    except Exception as e:
        print(f"  SKIP {path.name}: {e}")
        return None

    # Get body
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
            body = payload.decode(errors="replace") if ctype == "text/plain" else strip_html(payload.decode(errors="replace"))

    if not body or len(body) < 50:
        return None

    subject = msg.get("Subject", "").strip()
    sender = msg.get("From", "").strip()
    date = msg.get("Date", "").strip()

    return {
        "content": body,
        "subject": subject,
        "sender": sender,
        "date": date,
    }


def main():
    parser = argparse.ArgumentParser(description="Ingest .eml files into ThreadWeave")
    parser.add_argument("folder", help="Path to folder containing .eml files")
    parser.add_argument("--wing", default="email", help="Team/department (default: email)")
    parser.add_argument("--room", default="inbox", help="Topic (default: inbox)")
    parser.add_argument("--port", type=int, default=8000, help="ThreadWeave API port")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, don't submit")
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"Not a directory: {folder}")
        sys.exit(1)

    api_url = f"http://localhost:{args.port}/api/v1/ingest"
    eml_files = sorted(folder.glob("*.eml"))

    if not eml_files:
        print(f"No .eml files found in {folder}")
        sys.exit(1)

    print(f"Found {len(eml_files)} .eml files in {folder}")
    print(f"Wing: {args.wing}  |  Room: {args.room}")
    print()

    saved = 0
    skipped = 0
    errors = 0

    for i, path in enumerate(eml_files, 1):
        email_data = parse_eml(path)
        if email_data is None:
            skipped += 1
            continue

        if args.dry_run:
            print(f"[{i}/{len(eml_files)}] {email_data['subject'][:80]}")
            print(f"  From: {email_data['sender']}")
            print(f"  Body: {len(email_data['content'])} chars")
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
            }, timeout=30)
            resp.raise_for_status()
            result = resp.json()

            status = "SAVED" if result["should_save"] else "SKIP"
            if result["should_save"]:
                saved += 1
            else:
                skipped += 1

            print(f"[{i}/{len(eml_files)}] {status} | {email_data['subject'][:70]}")
            if result.get("deduplicated"):
                print(f"  ↳ duplicate")
            elif result.get("has_pii"):
                print(f"  ↳ PII rejected")
            else:
                print(f"  ↳ {result['content_type']} ({result['confidence']:.2f})")

        except Exception as e:
            errors += 1
            print(f"[{i}/{len(eml_files)}] ERROR | {email_data['subject'][:70]}")
            print(f"  ↳ {e}")

    print()
    print(f"Done: {saved} saved, {skipped} skipped, {errors} errors")

    if not args.dry_run and saved > 0:
        print(f"Search: threadweave search '<query>'")
        print(f"Or: curl -X POST {api_url.replace('ingest', 'search')} -H 'Content-Type: application/json' -d '{{\"query\":\"...\"}}'")


if __name__ == "__main__":
    main()
