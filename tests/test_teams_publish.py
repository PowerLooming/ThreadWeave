# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Tests for the Teams app publisher (org catalog upload)."""

import json
from pathlib import Path

import pytest

from threadweave.connectors.teams.publish import TeamsAppPublisher


class _FakeApp:
    """Minimal MSAL-like fake that returns canned tokens."""

    class _Cache:
        has_state_changed = False

        def serialize(self):
            return "{}"

    def __init__(self, token="fake-token"):
        self._token = token
        self.token_cache = self._Cache()

    def get_accounts(self):
        return [{"home_account_id": "acc"}]

    def acquire_token_silent(self, scopes, account=None):
        return {"access_token": self._token}

    def initiate_device_flow(self, scopes=None):
        return {"user_code": "ABC123", "verification_uri": "https://microsoft.com/devicelogin"}

    def acquire_token_by_device_flow(self, flow):
        return {"access_token": self._token}


@pytest.fixture
def publisher(monkeypatch, tmp_path):
    cache = tmp_path / "cache.json"
    p = TeamsAppPublisher(cache_file=str(cache))
    monkeypatch.setattr(p, "_app", _FakeApp())
    return p


def test_upload_sends_zip_and_parses_response(publisher, monkeypatch):
    calls = {}

    def fake_post(url, headers=None, data=None, timeout=None):
        calls["url"] = url
        calls["content_type"] = headers["Content-Type"]
        calls["auth"] = headers["Authorization"]
        calls["data"] = data
        return _Resp(201, {"id": "app-1", "externalId": "ext-1",
                           "displayName": "ThreadWeave"})

    monkeypatch.setattr("threadweave.connectors.teams.publish.requests.post",
                        fake_post)
    pkg = Path("tests/assets/teams-app.zip")
    pkg.parent.mkdir(parents=True, exist_ok=True)
    pkg.write_bytes(b"PK\x03\x04fakezip")

    result = publisher.upload(pkg)

    assert calls["url"] == ("https://graph.microsoft.com/v1.0/"
                            "appCatalogs/teamsApps?requiresReview=true")
    assert calls["content_type"] == "application/zip"
    assert calls["auth"] == "Bearer fake-token"
    assert calls["data"] == b"PK\x03\x04fakezip"
    assert result["id"] == "app-1"


def test_upload_failure_raises(publisher, monkeypatch, tmp_path):
    def fake_post(url, headers=None, data=None, timeout=None):
        return _Resp(403, {"error": {"code": "Forbidden"}})

    monkeypatch.setattr("threadweave.connectors.teams.publish.requests.post",
                        fake_post)
    pkg = tmp_path / "x.zip"
    pkg.write_bytes(b"zip")
    with pytest.raises(RuntimeError, match="Upload failed"):
        publisher.upload(pkg)


def test_upload_conflict_raises_already_in_catalog(publisher, monkeypatch,
                                                   tmp_path):
    def fake_post(url, headers=None, data=None, timeout=None):
        return _Resp(409, {"error": {"code": "AppDefinitionAlreadyExists"}})

    monkeypatch.setattr("threadweave.connectors.teams.publish.requests.post",
                        fake_post)
    pkg = tmp_path / "x.zip"
    pkg.write_bytes(b"zip")
    from threadweave.connectors.teams.publish import AlreadyInCatalog
    with pytest.raises(AlreadyInCatalog, match="already in the org catalog"):
        publisher.upload(pkg)


def test_wait_ready_polls_until_200(publisher, monkeypatch):
    results = iter([_Resp(404, {}), _Resp(200, {"id": "app-1"})])

    def fake_get(url, headers=None, timeout=None):
        return next(results)

    monkeypatch.setattr("threadweave.connectors.teams.publish.requests.get",
                        fake_get)
    ready = publisher.wait_ready("app-1", timeout=30)
    assert ready["id"] == "app-1"


def test_wait_ready_timeout(publisher, monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        return _Resp(404, {})

    monkeypatch.setattr("threadweave.connectors.teams.publish.requests.get",
                        fake_get)
    with pytest.raises(TimeoutError):
        publisher.wait_ready("app-1", timeout=1)


def test_list_catalog_apps(publisher, monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        return _Resp(200, {"value": [{"id": "a"}, {"id": "b"}]})

    monkeypatch.setattr("threadweave.connectors.teams.publish.requests.get",
                        fake_get)
    assert [a["id"] for a in publisher.list_catalog_apps()] == ["a", "b"]


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload
