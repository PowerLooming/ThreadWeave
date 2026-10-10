#!/usr/bin/env python
# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Check a peer link from the partner's side. Standard library only.

Run this on the partner's machine against the instance's peer endpoint. It
answers one question: does the link do what the operator of the instance says it
does, from where I am sitting?

    python peer_acceptance.py --url https://weave.internal:8443 \\
        --key sk-peer-... --ca ca.pem --cert client-nordvik.pem \\
        --cert-key client-nordvik.key --expect-wing engineering

Exit code 0 when every check passes, 1 when any of them does not. Nothing is
written and no content is printed: it reports counts and names, because the
output of a partner-side run is not a safe place for the content itself.
"""
from __future__ import annotations

import argparse
import http.client
import json
import ssl
import sys
import time
import urllib.error
import urllib.request

BREADTH_ROUTES = [
    ("GET", "/api/v1/wings"),
    ("GET", "/api/v1/tenants/default/entries"),
    ("GET", "/api/v1/audit/recent"),
    ("GET", "/api/v1/recap"),
]
WRITE_ROUTE = ("POST", "/api/v1/entries")


class Link:
    def __init__(self, args):
        self.base = args.url.rstrip("/")
        self.key = args.key
        self.context = None
        if args.ca:
            self.context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=args.ca)
            if args.cert and args.cert_key:
                self.context.load_cert_chain(args.cert, args.cert_key)
        self.results: list[tuple[bool, str]] = []

    def call(self, method: str, path: str, body: dict | None = None):
        request = urllib.request.Request(
            self.base + path,
            method=method,
            data=(json.dumps(body).encode() if body is not None else None),
            headers={"Content-Type": "application/json", "X-API-Key": self.key},
        )
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=20) as response:
                return response.status, json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode() or "{}")
            except ValueError:
                return exc.code, {}
        except ssl.SSLError as exc:
            return "tls", {"error": str(exc)[:120]}
        except (http.client.RemoteDisconnected, ConnectionResetError, TimeoutError, OSError) as exc:
            return "no-connection", {"error": type(exc).__name__}

    def check(self, ok: bool, label: str) -> None:
        self.results.append((bool(ok), label))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Peer-link acceptance checks, from the partner's side")
    parser.add_argument("--url", required=True, help="e.g. https://weave.internal:8443")
    parser.add_argument("--key", required=True, help="The peer key the instance issued to you")
    parser.add_argument("--ca", default="", help="The instance's CA certificate")
    parser.add_argument("--cert", default="", help="Your client certificate")
    parser.add_argument("--cert-key", default="", help="Your client key")
    parser.add_argument("--expect-wing", default="", help="The wing the key is scoped to, if known")
    parser.add_argument("--query", default="postgres", help="A word you expect to find something for")
    parser.add_argument("--limit", type=int, default=50, help="A deliberately large limit")
    parser.add_argument("--rate-limit", type=int, default=0, help="Expected requests per minute, to prove the 429")
    args = parser.parse_args(argv)

    print(f"peer link: {args.url}")
    link = Link(args)

    status, health = link.call("GET", "/api/v1/health")
    link.check(status == 200, f"health answers ({status})")

    status, body = link.call("POST", "/api/v1/search", {"query": args.query, "limit": args.limit})
    results = body.get("results", []) if isinstance(body, dict) else []
    link.check(status == 200, f"search answers ({status})")
    wings = sorted({str(r.get("wing", "")) for r in results})
    link.check(len(wings) <= 1, f"one wing only (saw {wings or 'none'})")
    if args.expect_wing:
        link.check(wings in ([args.expect_wing], []), f"the wing is {args.expect_wing} (saw {wings or 'none'})")
    link.check(len(results) <= args.limit, f"the cap holds ({len(results)} of a {args.limit} limit)")

    if results:
        status, _ = link.call("GET", f"/api/v1/entries/{results[0]['id']}")
        link.check(status == 200, f"a single entry reads ({status})")

    for method, path in BREADTH_ROUTES:
        status, _ = link.call(method, path)
        link.check(status == 403, f"{method} {path} is refused ({status})")
    status, _ = link.call(WRITE_ROUTE[0], WRITE_ROUTE[1], {"content": "x", "wing": "engineering"})
    link.check(status == 403, f"{WRITE_ROUTE[0]} {WRITE_ROUTE[1]} is refused ({status})")

    if args.rate_limit:
        codes = []
        for _ in range(args.rate_limit + 3):
            status, _ = link.call("POST", "/api/v1/search", {"query": args.query, "limit": 1})
            codes.append(status)
            if status == 429:
                break
            time.sleep(0.05)
        link.check(429 in codes, f"the rate limit trips within {args.rate_limit + 3} requests ({codes})")

    failed = [label for ok, label in link.results if not ok]
    print()
    print(f"{len(link.results) - len(failed)}/{len(link.results)} checks passed")
    if failed:
        for label in failed:
            print(f"  failed: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
