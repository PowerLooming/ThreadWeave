# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""P2: the private link. Mutual TLS material, and a real handshake.

The handshake tests open a real socket and run a real TLS server with the exact
verification the served instance applies, because "the server requires a client
certificate" is a claim about a socket, not about a config dict.
"""
from __future__ import annotations

import http.client
import os
import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from threadweave import auth as auth_mod
from threadweave import peer_tls

PEER_KEY = "sk-peer-nordvik-tls"
PEER_ID = "nordvik"


# ── Material ─────────────────────────────────────────────────

@pytest.fixture
def material(tmp_path):
    """A CA, a server certificate for 127.0.0.1, and one partner certificate."""
    directory = tmp_path / "peer-tls"
    written = peer_tls.init_authority(["127.0.0.1", "peer.example.invalid"], [PEER_ID], str(directory))
    return written


def test_material_is_written_and_private(material):
    for name in ["ca.pem", "ca.key", "server.pem", "server.key",
                 f"client-{PEER_ID}.pem", f"client-{PEER_ID}.key"]:
        path = Path(material["dir"]) / name
        assert path.is_file(), name
        assert path.stat().st_size > 0
    # A world-readable CA key can mint a certificate for any partner. Windows
    # has no POSIX mode bits, so this claim is only checkable on POSIX; the
    # write path still calls chmod(0o600) so a Linux or macOS host is covered.
    if os.name != "nt":
        for secret in ["ca.key", "server.key", f"client-{PEER_ID}.key"]:
            mode = (Path(material["dir"]) / secret).stat().st_mode & 0o777
            assert mode & 0o077 == 0, f"{secret} is {oct(mode)}"


def test_second_init_refuses_to_invalidate_issued_certificates(material):
    with pytest.raises(FileExistsError):
        peer_tls.init_authority(["127.0.0.1"], [PEER_ID], material["dir"])
    again = peer_tls.init_authority(["127.0.0.1"], [PEER_ID], material["dir"], force=True)
    assert again["ca_fingerprint"] != material["ca_fingerprint"]


def test_status_reports_what_exists_and_when_it_expires(material):
    report = peer_tls.status(material["dir"])
    assert report["ca"]["fingerprint"] == material["ca_fingerprint"]
    assert "127.0.0.1" in report["server"]["hosts"]
    assert "peer.example.invalid" in report["server"]["hosts"]
    assert len(report["clients"]) == 1
    client = report["clients"][0]
    assert f"peer:{PEER_ID}" in client["subject"]
    assert client["has_key"] is True


def test_status_on_an_empty_directory_says_so(tmp_path):
    report = peer_tls.status(str(tmp_path / "nothing"))
    assert report["ca"] is None
    assert report["clients"] == []


def test_server_kwargs_refuse_to_start_without_a_client_ca(material):
    directory = Path(material["dir"])
    (directory / "ca.pem").rename(directory / "ca.pem.moved")
    with pytest.raises(FileNotFoundError) as excinfo:
        peer_tls.server_ssl_kwargs(str(directory))
    assert "client CA" in str(excinfo.value)
    # Without the requirement it still starts: that is the honest non-peer mode.
    kwargs = peer_tls.server_ssl_kwargs(str(directory), require_client_cert=False)
    assert kwargs["ssl_cert_reqs"] == ssl.CERT_NONE


def test_server_kwargs_require_a_certificate_by_default(material):
    kwargs = peer_tls.server_ssl_kwargs(material["dir"])
    assert kwargs["ssl_cert_reqs"] == ssl.CERT_REQUIRED
    assert kwargs["ssl_ca_certs"].endswith("ca.pem")


# ── A real handshake ─────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib naming
        payload = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):  # keep the test output quiet
        return


@pytest.fixture
def tls_server(material):
    """A real TLS server using the peer-link material."""
    context = peer_tls.server_ssl_context(material["dir"])
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"https://127.0.0.1:{server.server_address[1]}", material
    server.shutdown()
    server.server_close()


def _client(material, *, cert: bool = True, ca: str = ""):
    context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH,
        cafile=ca or str(Path(material["dir"]) / "ca.pem"),
    )
    if cert:
        context.load_cert_chain(
            certfile=str(Path(material["dir"]) / f"client-{PEER_ID}.pem"),
            keyfile=str(Path(material["dir"]) / f"client-{PEER_ID}.key"),
        )
    return context


def _get(url: str, context) -> int:
    parsed = http.client.urlsplit(url)
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port, context=context, timeout=10)
    try:
        connection.request("GET", "/")
        return connection.getresponse().status
    finally:
        connection.close()


def test_the_partner_certificate_is_accepted(tls_server):
    url, material = tls_server
    assert _get(url, _client(material)) == 200


def test_a_client_without_a_certificate_is_refused(tls_server):
    url, material = tls_server
    with pytest.raises((ssl.SSLError, ConnectionResetError, socket.timeout, OSError)):
        _get(url, _client(material, cert=False))


def test_a_certificate_from_another_authority_is_refused(tls_server, tmp_path):
    url, material = tls_server
    foreign = peer_tls.init_authority(["127.0.0.1"], [PEER_ID], str(tmp_path / "foreign"))
    foreign_material = dict(material)
    foreign_material["dir"] = foreign["dir"]
    context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH,
        cafile=str(Path(material["dir"]) / "ca.pem"),
    )
    context.load_cert_chain(
        certfile=foreign["clients"][PEER_ID]["cert"],
        keyfile=foreign["clients"][PEER_ID]["key"],
    )
    with pytest.raises((ssl.SSLError, ConnectionResetError, socket.timeout, OSError)):
        _get(url, context)


def test_a_client_that_trusts_another_ca_refuses_this_server(tls_server, tmp_path):
    url, material = tls_server
    foreign = peer_tls.init_authority(["127.0.0.1"], [PEER_ID], str(tmp_path / "foreign2"))
    context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH,
        cafile=str(Path(foreign["dir"]) / "ca.pem"),
    )
    context.load_cert_chain(
        certfile=str(Path(material["dir"]) / f"client-{PEER_ID}.pem"),
        keyfile=str(Path(material["dir"]) / f"client-{PEER_ID}.key"),
    )
    with pytest.raises((ssl.SSLError, OSError)):
        _get(url, context)


# ── The rate limit ───────────────────────────────────────────

def _world(monkeypatch, tmp_path, *, max_results=25, rate_limit="1"):
    home = tmp_path / "home"
    (home / ".threadweave").mkdir(parents=True)
    (home / ".threadweave" / "keys.json").write_text(json.dumps({"keys": [
        {"key": "sk-tenant-tls", "tenant_id": "default", "role": "readwrite",
         "wing": "engineering", "person_id": "alice"},
        {"key": PEER_KEY, "tenant_id": "default", "role": "readonly",
         "wing": "engineering", "label": f"peer:{PEER_ID}", "peer_id": PEER_ID,
         "issued_by": "harald", "issued_at": "2026-10-10T00:00:00+00:00",
         "expires_at": "2099-01-01T00:00:00+00:00", "max_results": max_results},
    ]}))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("THREADWEAVE_REQUIRE_AUTH", "1")
    if rate_limit is not None:
        monkeypatch.setenv(auth_mod.PEER_RATE_LIMIT_ENV, rate_limit)
    auth_mod.reset()
    auth_mod.reset_peer_rate_limits()
    return home


def test_the_rate_limit_is_per_peer_and_refills(monkeypatch):
    monkeypatch.setenv(auth_mod.PEER_RATE_LIMIT_ENV, "60")
    auth_mod.reset_peer_rate_limits()
    assert auth_mod.peer_rate_limit() == 60
    allowed = [auth_mod.peer_rate_allow("peer:nordvik", 60, now=1000.0) for _ in range(60)]
    assert all(allowed)
    assert auth_mod.peer_rate_allow("peer:nordvik", 60, now=1000.0) is False
    # One second later a single token has come back, not a full bucket.
    assert auth_mod.peer_rate_allow("peer:nordvik", 60, now=1001.0) is True
    assert auth_mod.peer_rate_allow("peer:nordvik", 60, now=1001.0) is False
    # Another peer has its own bucket.
    assert auth_mod.peer_rate_allow("peer:other", 60, now=1001.0) is True


def test_zero_disables_the_limit(monkeypatch):
    assert auth_mod.peer_rate_allow("peer:nordvik", 0) is True
    monkeypatch.setenv(auth_mod.PEER_RATE_LIMIT_ENV, "0")
    assert auth_mod.peer_rate_limit() == 0


def _api_client():
    from threadweave.api import app

    return TestClient(app)


def test_a_peer_over_its_limit_gets_429(monkeypatch, tmp_path):
    _world(monkeypatch, tmp_path, rate_limit="2")
    client = _api_client()
    headers = {"X-API-Key": PEER_KEY}
    body = {"query": "postgres", "limit": 5}
    codes = [client.post("/api/v1/search", json=body, headers=headers).status_code for _ in range(4)]
    assert codes[:2] == [200, 200]
    assert codes[2:] == [429, 429]
    resp = client.post("/api/v1/search", json=body, headers=headers)
    assert resp.headers.get("Retry-After") == "60"
    assert "rate limit" in resp.json()["error"].lower()
    auth_mod.configure(enabled=False)


def test_an_ordinary_key_ignores_the_peer_rate_limit(monkeypatch, tmp_path):
    _world(monkeypatch, tmp_path, rate_limit="1")
    client = _api_client()
    headers = {"X-API-Key": "sk-tenant-tls"}
    for _ in range(3):
        resp = client.post("/api/v1/search", json={"query": "postgres", "limit": 5}, headers=headers)
        assert resp.status_code == 200
    auth_mod.configure(enabled=False)


def test_every_certificate_carries_key_identifiers(material):
    """A leaf without an authority key identifier fails verification outright.

    Found by running the acceptance harness with a newer OpenSSL: the chain was
    refused with "Missing Authority Key Identifier" while an older client
    accepted it, which is the worst shape of bug for a link another
    organisation has to trust.
    """
    from cryptography import x509

    for name in ["ca.pem", "server.pem", f"client-{PEER_ID}.pem"]:
        cert = x509.load_pem_x509_certificate((Path(material["dir"]) / name).read_bytes())
        cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
        cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
