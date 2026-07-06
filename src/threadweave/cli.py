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
import sys
from datetime import datetime, timezone

from threadweave.detector import detect, is_worth_saving


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

    args = parser.parse_args()

    if args.command == "detect":
        cmd_detect(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "save":
        cmd_save(args)
    elif args.command == "serve":
        cmd_serve(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
