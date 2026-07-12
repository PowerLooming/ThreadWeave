#!/usr/bin/env python3
"""Ingest emails directly from Outlook into ThreadWeave — no export needed.

Usage:
    python ingest_outlook.py                          # Inbox, last 50 emails
    python ingest_outlook.py --folder "Sent Items"    # Specific folder
    python ingest_outlook.py --max 200 --wing legal   # More emails, specific wing
    python ingest_outlook.py --dry-run                # Preview only, don't submit
"""
import argparse
import sys

import httpx

API = "http://localhost:8000/api/v1/ingest"


def strip_html(text: str) -> str:
    import re
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def get_outlook_mapi():
    """Late import — only needed when using this feature."""
    try:
        import win32com.client  # pip install pywin32
    except ImportError:
        print("This script requires pywin32. Install it:")
        print("  pip install pywin32")
        sys.exit(1)

    outlook = win32com.client.Dispatch("Outlook.Application")
    return outlook.GetNamespace("MAPI")


def iter_emails(folder_name="Inbox", max_results=50, unread_only=False):
    """Yield (subject, sender, body, date) from Outlook folder."""
    namespace = get_outlook_mapi()

    # Find the folder
    folder = None
    for store in namespace.Folders:
        try:
            f = store.Folders[folder_name]
        except Exception:
            # Try nested: Inbox, Sent Items are often under the default store
            pass
        else:
            folder = f
            break

    # Fallback: search recursively
    if folder is None:
        def find_folder(root, name):
            for f in root.Folders:
                if f.Name.lower() == name.lower():
                    return f
                try:
                    result = find_folder(f, name)
                    if result:
                        return result
                except Exception:
                    pass
            return None
        for store in namespace.Folders:
            folder = find_folder(store, folder_name)
            if folder:
                break

    if folder is None:
        print(f"Folder '{folder_name}' not found. Available top-level folders:")
        for store in namespace.Folders:
            print(f"  Store: {store.Name}")
            for f in store.Folders:
                print(f"    - {f.Name}")
        sys.exit(1)

    items = folder.Items
    items.Sort("[ReceivedTime]", True)  # Newest first
    count = 0

    for i, item in enumerate(items):
        if count >= max_results:
            break
        try:
            subject = item.Subject or ""
            sender = ""
            try:
                sender = item.SenderName or ""
            except Exception:
                try:
                    sender = item.SenderEmailAddress or ""
                except Exception:
                    pass

            body = item.Body or ""
            if not body:
                continue

            if unread_only and not getattr(item, "UnRead", False):
                continue

            date = str(item.ReceivedTime) if hasattr(item, "ReceivedTime") else ""

            count += 1
            yield {
                "subject": subject.strip(),
                "sender": sender.strip(),
                "body": body.strip(),
                "date": date,
            }
        except Exception:
            continue


def main():
    parser = argparse.ArgumentParser(description="Ingest Outlook emails into ThreadWeave")
    parser.add_argument("--folder", default="Inbox", help="Outlook folder name (default: Inbox)")
    parser.add_argument("--max", dest="max_results", type=int, default=50, help="Max emails (default: 50)")
    parser.add_argument("--unread", action="store_true", help="Only unread emails")
    parser.add_argument("--wing", default="email", help="Team/department (default: email)")
    parser.add_argument("--room", default="inbox", help="Topic (default: inbox)")
    parser.add_argument("--port", type=int, default=8000, help="ThreadWeave API port")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, don't submit")
    args = parser.parse_args()

    api_url = f"http://localhost:{args.port}/api/v1/ingest"

    print(f"Reading '{args.folder}' from Outlook (max {args.max_results} emails)...")
    print(f"Wing: {args.wing}  |  Room: {args.room}")
    if args.dry_run:
        print("DRY RUN — nothing will be saved")
    print()

    saved = 0
    skipped = 0
    errors = 0
    total = 0

    for email_data in iter_emails(args.folder, args.max_results, args.unread):
        total += 1

        if args.dry_run:
            print(f"[{total}] {email_data['subject'][:80]}")
            print(f"    From: {email_data['sender']}")
            print(f"    Body: {len(email_data['body'])} chars")
            print()
            continue

        try:
            resp = httpx.post(api_url, json={
                "content": email_data["body"],
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

            detail = ""
            if result.get("deduplicated"):
                detail = " (duplicate)"
            elif result.get("has_pii"):
                detail = " (PII)"
            else:
                detail = f" ({result['content_type']}, {result['confidence']:.2f})"

            print(f"[{total}] {status}{detail} | {email_data['subject'][:70]}")

        except Exception as e:
            errors += 1
            print(f"[{total}] ERROR | {email_data['subject'][:70]}")
            print(f"    {e}")

    print()
    print(f"Done: {total} emails → {saved} saved, {skipped} skipped, {errors} errors")

    if not args.dry_run and saved > 0:
        print(f"\nSearch your knowledge:")
        print(f"  threadweave search '<your query>'")


if __name__ == "__main__":
    main()
