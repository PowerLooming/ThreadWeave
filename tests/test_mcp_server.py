# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""MCP server surface (Streamable HTTP) — the agent tool contract.

The tests drive the real wire protocol an MCP client uses: initialize,
notifications/initialized, tools/list, tools/call, with the JSON-RPC
envelope and the SSE-framed response body. The tools then hit the real
REST API in-process, so a call exercises the actual store, detector,
gossip gate, confidentiality ACL and audit trail rather than a mock of
them. Only the transport-facing tests stub the API, and they say so.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import threadweave.auth as auth
from threadweave import api as tw_api
from threadweave import mcp_server, store
from threadweave.api import app as api_app

SSE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

# Loopback host: the SDK's DNS-rebinding guard admits loopback by default,
# which is exactly the deployment question the transport-security tests probe.
FIXTURE_BASE_URL = "http://127.0.0.1:8100"

DECISION_TEXT = (
    "We decided to standardise on Postgres 16 for the knowledge service "
    "because JSONB and full-text search cover our query needs."
)
CHAT_TEXT = "ok thanks, sounds good, see you tomorrow about the Postgres thing maybe"
GOSSIP_TEXT = (
    "Apparently Adele only got the promotion because she gossips with the "
    "boss, everyone says she is useless at her job."
)


def _asgi_factory():
    """Tool calls reach the REST API in-process (no live server needed)."""

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app),
            base_url="http://threadweave.test",
            timeout=30,
        )

    return factory


class MCPClient:
    """Minimal Streamable HTTP MCP client over the ASGI app."""

    def __init__(self, client: TestClient, api_key: str = ""):
        self.client = client
        self.headers = dict(SSE_HEADERS)
        if api_key:
            self.headers["X-API-Key"] = api_key
        self.session_id: str | None = None
        self.init_result: dict = {}

    def _post(self, payload: dict, with_session: bool = True) -> httpx.Response:
        headers = dict(self.headers)
        if with_session and self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return self.client.post("/mcp", json=payload, headers=headers)

    @staticmethod
    def _result(resp: httpx.Response) -> dict:
        """The JSON-RPC message from an SSE-framed response body."""
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return json.loads(resp.text)

    def initialize(self) -> httpx.Response:
        resp = self._post(
            {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            },
            with_session=False,
        )
        self.session_id = resp.headers.get("mcp-session-id")
        if resp.status_code < 400:
            self.init_result = self._result(resp)["result"]
            self._post(
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            )
        return resp

    def tools(self) -> list[dict]:
        resp = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert resp.status_code == 200, resp.text
        return self._result(resp)["result"]["tools"]

    def call_raw(self, name: str, arguments: dict) -> dict:
        resp = self._post({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        assert resp.status_code == 200, resp.text
        return self._result(resp)["result"]

    def call(self, name: str, arguments: dict | None = None) -> dict:
        """Call a tool and return its payload (fails the test on isError)."""
        result = self.call_raw(name, arguments or {})
        assert result.get("isError") is not True, result
        return json.loads(result["content"][0]["text"])


@pytest.fixture(autouse=True)
def empty_palace(tmp_path, monkeypatch):
    """Every test starts on an empty palace.

    The API holds a process-wide memory store plus a SQLite entry store, so
    without this the entries one test captures are visible to the next and
    the counts stop meaning anything.
    """
    monkeypatch.setenv("THREADWEAVE_ENTRY_DB", str(tmp_path / "entries.sqlite3"))
    store._store = None
    tw_api._memory_store.clear()
    tw_api._tenant_stores.clear()
    tw_api._dedup_hashes.clear()
    yield
    store._store = None
    tw_api._memory_store.clear()
    tw_api._tenant_stores.clear()
    tw_api._dedup_hashes.clear()


@pytest.fixture()
def mcp_client():
    mcp_server.set_client_factory(_asgi_factory())
    with TestClient(mcp_server.streamable_http_app(), base_url=FIXTURE_BASE_URL) as client:
        mcp = MCPClient(client)
        resp = mcp.initialize()
        assert resp.status_code == 200, resp.text
        yield mcp
    mcp_server.reset()
    auth.reset()


class TestToolSurface:
    def test_all_tools_are_advertised_with_schemas(self, mcp_client):
        tools = {t["name"]: t for t in mcp_client.tools()}
        assert set(tools) == {
            "search_memory", "get_decisions", "get_entry", "entry_provenance",
            "who_knows", "list_open_commitments", "classify_text",
            "capture_knowledge", "browse_topics",
        }
        for tool in tools.values():
            assert tool.get("description"), f"{tool['name']} has no description"

        search = tools["search_memory"]["inputSchema"]
        assert search["required"] == ["query"]
        # The injected request context must never leak into the schema.
        assert "ctx" not in search["properties"]
        assert tools["capture_knowledge"]["inputSchema"]["required"] == ["content", "wing"]
        # Read-only hints steer the agent's confirmation behaviour.
        assert tools["search_memory"].get("annotations", {}).get("readOnlyHint") is True
        assert tools["capture_knowledge"].get("annotations", {}).get("readOnlyHint") is False

    def test_server_identifies_itself_with_the_api_version(self, mcp_client):
        info = mcp_client.init_result["serverInfo"]
        assert info["name"] == "threadweave"
        assert info["version"] == api_app.version
        assert mcp_client.init_result.get("instructions")


class TestCaptureAndSearch:
    def test_capture_then_search_round_trip(self, mcp_client):
        saved = mcp_client.call("capture_knowledge", {
            "content": DECISION_TEXT, "wing": "engineering",
            "room": "database", "title": "Postgres 16 standard",
        })
        assert saved["saved"] is True
        assert saved["id"]

        found = mcp_client.call("search_memory", {"query": "Postgres"})
        assert found["total"] == 1
        hit = found["results"][0]
        assert hit["id"] == saved["id"]
        assert hit["wing"] == "engineering"
        assert hit["content_type"] == "decision"
        assert "source_url" in hit and "sensitivity" in hit

    def test_get_entry_returns_full_content(self, mcp_client):
        saved = mcp_client.call("capture_knowledge", {
            "content": DECISION_TEXT, "wing": "engineering",
        })
        entry = mcp_client.call("get_entry", {"entry_id": saved["id"]})
        assert entry["content"] == DECISION_TEXT
        assert entry["source_type"] == "agent"
        assert entry["scope"] == "team"

    def test_get_decisions_excludes_other_content_types(self, mcp_client):
        mcp_client.call("capture_knowledge", {
            "content": DECISION_TEXT, "wing": "engineering", "title": "Postgres standard",
        })
        mcp_client.call("capture_knowledge", {
            "content": CHAT_TEXT, "wing": "engineering",
        })
        unfiltered = mcp_client.call("search_memory", {"query": "Postgres"})
        assert unfiltered["total"] == 2

        decisions = mcp_client.call("get_decisions", {"query": "Postgres"})
        assert decisions["total"] == 1
        assert decisions["results"][0]["content_type"] == "decision"

    def test_capture_refuses_gossip(self, mcp_client):
        out = mcp_client.call("capture_knowledge", {
            "content": GOSSIP_TEXT, "wing": "engineering",
        })
        assert out["saved"] is False
        assert out["reason"] == "rejected_gossip"
        # Nothing was stored.
        assert mcp_client.call("search_memory", {"query": "promotion"})["total"] == 0

    def test_classify_text_reports_type_and_gossip(self, mcp_client):
        decision = mcp_client.call("classify_text", {"text": DECISION_TEXT})
        assert decision["content_type"] == "decision"
        assert decision["should_save"] is True
        assert decision["has_gossip"] is False
        assert "sensitivity" in decision

        gossip = mcp_client.call("classify_text", {"text": GOSSIP_TEXT})
        assert gossip["has_gossip"] is True

    def test_deleted_entry_reports_the_api_error(self, mcp_client):
        out = mcp_client.call("get_entry", {"entry_id": "does-not-exist"})
        assert out["status"] == 404
        assert "error" in out


class TestDiscoveryTools:
    def test_who_knows_groups_by_author_and_team(self, mcp_client):
        mcp_client.call("capture_knowledge", {
            "content": DECISION_TEXT, "wing": "engineering",
        })
        out = mcp_client.call("who_knows", {"topic": "Postgres"})
        assert out["people"][0]["author_id"] == "agent:mcp"
        assert out["people"][0]["teams"] == ["engineering"]
        assert out["teams"] == [{"wing": "engineering", "entries": 1}]

    def test_provenance_reports_the_version_chain(self, mcp_client):
        saved = mcp_client.call("capture_knowledge", {
            "content": DECISION_TEXT, "wing": "engineering",
        })
        out = mcp_client.call("entry_provenance", {"entry_id": saved["id"]})
        assert out["entry_id"] == saved["id"]
        assert out["is_latest"] is True
        assert [v["id"] for v in out["version_chain"]] == [saved["id"]]

    def test_browse_topics_uses_the_topics_endpoint(self, mcp_client):
        mcp_client.call("capture_knowledge", {
            "content": DECISION_TEXT, "wing": "engineering", "title": "Postgres standard",
        })
        out = mcp_client.call("browse_topics", {})
        assert out["total_entries"] == 1


class TestCommitmentsForwarding:
    @pytest.mark.parametrize(
        "arguments,expected",
        [
            ({}, "status=open"),
            ({"owner": "Adele", "status": "all"}, "owner=Adele&status=all"),
        ],
    )
    def test_owner_and_status_reach_the_api(self, arguments, expected):
        """Transport-facing: the tool must not drop its filters."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"tasks": [
                {"id": "t1", "action": "chase the vendor", "owner": "adele",
                 "status": "open"},
            ]})

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url="http://threadweave.test",
                timeout=10,
            )

        mcp_server.set_client_factory(factory)
        try:
            with TestClient(mcp_server.streamable_http_app(), base_url=FIXTURE_BASE_URL) as client:
                mcp = MCPClient(client)
                mcp.initialize()
                out = mcp.call("list_open_commitments", arguments)
        finally:
            mcp_server.reset()

        assert seen == [f"http://threadweave.test/api/v1/tasks?{expected}"]
        assert out["count"] == 1
        assert out["tasks"][0]["action"] == "chase the vendor"


class TestTransportSecurity:
    """The published-endpoint trap: Host allowlisting.

    Copilot Studio and Agent 365 call the MCP endpoint under its public
    hostname. The SDK's rebinding guard admits loopback only, so an
    unconfigured endpoint answers 421 and the agent reports a broken tool.
    """

    def test_foreign_host_is_refused_by_default(self, mcp_client):
        with TestClient(mcp_server.streamable_http_app(), base_url="http://mcp.corp.example") as client:
            mcp = MCPClient(client)
            assert mcp.initialize().status_code == 421

    def test_configured_host_is_admitted(self, mcp_client):
        app = mcp_server.streamable_http_app(
            allowed_hosts=["mcp.corp.example", "mcp.corp.example:*"],
        )
        with TestClient(app, base_url="http://mcp.corp.example") as client:
            mcp = MCPClient(client)
            assert mcp.initialize().status_code == 200
            assert mcp.call("search_memory", {"query": "anything"})["results"] == []

    def test_env_var_configures_the_allowlist(self, monkeypatch):
        monkeypatch.setenv("THREADWEAVE_MCP_ALLOWED_HOSTS", "mcp.corp.example")
        monkeypatch.setenv("THREADWEAVE_MCP_ALLOWED_ORIGINS", "https://mcp.corp.example")
        settings = mcp_server.transport_security_settings()
        assert settings is not None
        assert settings.allowed_hosts == ["mcp.corp.example"]
        assert settings.allowed_origins == ["https://mcp.corp.example"]

    def test_unset_env_keeps_the_sdk_default(self, monkeypatch):
        monkeypatch.delenv("THREADWEAVE_MCP_ALLOWED_HOSTS", raising=False)
        monkeypatch.delenv("THREADWEAVE_MCP_ALLOWED_ORIGINS", raising=False)
        assert mcp_server.transport_security_settings() is None


class TestAuth:
    def test_key_is_required_when_auth_is_on(self, mcp_client):
        auth.configure(True, "acme:sk-acme,other:sk-other")

        mcp_server.set_client_factory(_asgi_factory())
        with TestClient(mcp_server.streamable_http_app(), base_url=FIXTURE_BASE_URL) as client:
            anonymous = MCPClient(client)
            assert anonymous.initialize().status_code == 401

            wrong = MCPClient(client, api_key="sk-nope")
            assert wrong.initialize().status_code == 403

            valid = MCPClient(client, api_key="sk-acme")
            assert valid.initialize().status_code == 200

    def test_tenant_key_only_sees_its_own_tenant(self, mcp_client):
        auth.configure(True, "acme:sk-acme,other:sk-other")

        mcp_server.set_client_factory(_asgi_factory())
        with TestClient(mcp_server.streamable_http_app(), base_url=FIXTURE_BASE_URL) as client:
            acme = MCPClient(client, api_key="sk-acme")
            acme.initialize()
            saved = acme.call("capture_knowledge", {
                "content": DECISION_TEXT, "wing": "engineering",
            })
            assert saved["saved"] is True

            assert acme.call("search_memory", {"query": "Postgres"})["total"] == 1

            other = MCPClient(client, api_key="sk-other")
            other.initialize()
            assert other.call("search_memory", {"query": "Postgres"})["total"] == 0


class TestOptionalExtra:
    """The MCP SDK is an extra: say which one instead of a bare ImportError."""

    def test_missing_extra_prints_the_install_hint(self, monkeypatch, capsys):
        import argparse
        import builtins
        import sys

        from threadweave import cli

        real_import = builtins.__import__

        def guarded(name, *args, **kwargs):
            if name == "mcp" or name.startswith("mcp."):
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)
            return real_import(name, *args, **kwargs)

        # The module is already imported in this process; drop it so the tool
        # import runs again, this time against a machine without the extra.
        monkeypatch.delitem(sys.modules, "threadweave.mcp_server", raising=False)
        monkeypatch.setattr(builtins, "__import__", guarded)

        with pytest.raises(SystemExit) as exc:
            cli.cmd_mcp(argparse.Namespace(host="127.0.0.1", port=8100, api_url=""))

        assert exc.value.code == 1
        stderr = capsys.readouterr().err
        assert "optional extra" in stderr
        assert 'threadweave-memory[mcp]' in stderr


class TestTransportFailures:
    """An agent must not read 'no knowledge found' when the API is down."""

    @staticmethod
    def _unreachable_factory():
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url="http://127.0.0.1:8000",
                timeout=5,
            )

        return factory

    def test_unreachable_api_surfaces_as_a_tool_error(self, mcp_client):
        mcp_server.set_client_factory(self._unreachable_factory())
        result = mcp_client.call_raw("search_memory", {"query": "Postgres"})
        assert result.get("isError") is True
        assert "search_memory" in result["content"][0]["text"]

    async def test_the_message_tells_the_operator_what_to_start(self):
        """The SDK replaces the message, so pin the raised one directly."""
        mcp_server.set_client_factory(self._unreachable_factory())
        try:
            with pytest.raises(RuntimeError) as err:
                await mcp_server._api(
                    SimpleNamespace(headers={}), "GET", "/api/v1/health",
                )
        finally:
            mcp_server.reset()
        assert "unreachable" in str(err.value)
        assert mcp_server.DEFAULT_API_BASE_URL in str(err.value)
