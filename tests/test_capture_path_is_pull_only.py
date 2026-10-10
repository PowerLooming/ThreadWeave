# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""The capture path is pull only: nothing registers a push route.

`docs/ai-publication-boundary.md` closes every route that would let tenant
content reach a cloud service on a schedule of its own, and the SharePoint
WebhookManager and the email mail-subscription helper were removed because
they read as the receiving half of exactly such a route. Capture is the
daemon polling Graph. These tests keep a webhook subscription or a
notification receiver from creeping back in, and keep the word "webhook"
from appearing anywhere outside a sentence that denies one.
"""

from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "threadweave"

# Tokens that exist only to register or receive a pushed Graph notification.
PUSH_PATH_TOKENS = (
    "WebhookManager",
    "create_subscription",
    "create_mail_subscription",
    "parse_notifications",
    "/subscriptions",
    "notificationUrl",
    "notification_url",
)


def _python_sources() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_no_source_registers_or_receives_a_graph_notification():
    offenders = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8")
        for token in PUSH_PATH_TOKENS:
            if token in text:
                offenders.append(f"{path.relative_to(SRC)}: {token}")
    assert offenders == [], (
        "the capture path is pull only; remove the push plumbing:\n"
        + "\n".join(offenders)
    )


def test_webhook_is_mentioned_only_to_deny_it():
    offenders = []
    for path in _python_sources():
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, 1):
            if "webhook" in line.lower() and "no webhook" not in line.lower():
                offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")
    assert offenders == [], (
        'a webhook is not part of the capture path; a mention has to say '
        '"no webhook":\n' + "\n".join(offenders)
    )


def test_the_sharepoint_package_no_longer_exports_a_webhook_manager():
    from threadweave.connectors import sharepoint

    assert "WebhookManager" not in sharepoint.__all__
    assert not hasattr(sharepoint, "WebhookManager")
