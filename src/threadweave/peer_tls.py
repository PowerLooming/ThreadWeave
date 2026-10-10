# SPDX-License-Identifier: MIT
# Copyright (C) 2026 ThreadWeave contributors
"""Mutual-TLS material for a peer link.

P2 of the peer-sharing design: the instance may be reachable from the partner's
network and from nowhere else, so the transport authenticates both ends before
the peer key is even read. This module provisions a private certificate
authority, one server certificate for this instance, and one client certificate
per partner, then hands uvicorn the four settings that make the server require
a client certificate.

Nothing here talks to a certificate authority on the internet: the CA is a file
in a directory the operator owns, because the link is private by construction
and a public CA would make the instance resolvable from anywhere.

`cryptography` is imported lazily: it arrives with the M365/Teams extras (msal
depends on it) and the module must stay importable, and the rest of the product
usable, when it is absent.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import os
import ssl
from pathlib import Path
from typing import Optional

TLS_DIR_ENV = "THREADWEAVE_PEER_TLS_DIR"
CA_DAYS = 3650
CERT_DAYS = 730


def tls_dir(directory: Optional[str] = None) -> Path:
    """Where the peer-link material lives."""
    chosen = directory or os.environ.get(TLS_DIR_ENV) or (Path.home() / ".threadweave" / "peer-tls")
    return Path(chosen)


def _crypto():
    """The cryptography pieces this module needs, or a clear error."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise RuntimeError(
            "peer TLS material needs the 'cryptography' package: it ships with the "
            "M365/Teams extras, or install it with 'uv pip install cryptography'"
        ) from exc
    return x509, hashes, serialization, rsa, NameOID, ExtendedKeyUsageOID


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path


def _fingerprint(der: bytes) -> str:
    digest = hashlib.sha256(der).hexdigest()
    return ":".join(digest[i:i + 2] for i in range(0, 16, 2))


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def init_authority(
    hosts: list[str],
    peers: list[str],
    directory: Optional[str] = None,
    *,
    force: bool = False,
    ca_days: int = CA_DAYS,
    cert_days: int = CERT_DAYS,
) -> dict:
    """Create the CA, the server certificate and one client certificate per peer.

    ``hosts`` are the names and addresses the instance answers to (every one
    goes into the server certificate's subjectAltName, because a certificate
    without the name the partner dials is refused by the partner's client), and
    ``peers`` are the partner ids that get a client certificate.
    """
    x509, hashes, serialization, rsa, NameOID, ExtendedKeyUsageOID = _crypto()
    out = tls_dir(directory)
    out.mkdir(parents=True, exist_ok=True)
    wanted = ["ca.pem", "ca.key", "server.pem", "server.key"] + [
        f"client-{_safe(peer)}.pem" for peer in peers
    ] + [f"client-{_safe(peer)}.key" for peer in peers]
    existing = [name for name in wanted if (out / name).exists()]
    if existing and not force:
        raise FileExistsError(
            "peer TLS material already exists ("
            + ", ".join(sorted(existing))
            + "); pass force=True to replace it, which invalidates every issued client certificate"
        )

    def _key(bits: int):
        return rsa.generate_private_key(public_exponent=65537, key_size=bits)

    ca_key = _key(4096)
    ca_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"ThreadWeave peer CA on {os.uname().nodename}"
                           if hasattr(os, "uname") else "ThreadWeave peer CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ThreadWeave peer link"),
    ])
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - _dt.timedelta(minutes=5))
        .not_valid_after(_now() + _dt.timedelta(days=ca_days))
        .add_extension(ca_ski, critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    san: list = []
    for host in hosts:
        san.append(x509.IPAddress(__import__("ipaddress").ip_address(host))
                   if _is_ip(host) else x509.DNSName(host))
    if not san:
        raise ValueError("the server certificate needs at least one host name or address")
    server_key = _key(2048)
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - _dt.timedelta(minutes=5))
        .not_valid_after(_now() + _dt.timedelta(days=cert_days))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )

    written = {
        "dir": str(out),
        "ca": str(_write(out / "ca.pem", ca_cert.public_bytes(serialization.Encoding.PEM))),
        "ca_key": str(_write(out / "ca.key", ca_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))),
        "server": str(_write(out / "server.pem", server_cert.public_bytes(serialization.Encoding.PEM))),
        "server_key": str(_write(out / "server.key", server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))),
        "ca_fingerprint": _fingerprint(ca_cert.fingerprint(hashes.SHA256())),
        "server_fingerprint": _fingerprint(server_cert.fingerprint(hashes.SHA256())),
        "hosts": list(hosts),
        "clients": {},
    }

    for peer in peers:
        name = _safe(peer)
        client_key = _key(2048)
        client_cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"peer:{peer}")]))
            .issuer_name(ca_name)
            .public_key(client_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - _dt.timedelta(minutes=5))
            .not_valid_after(_now() + _dt.timedelta(days=cert_days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(client_key.public_key()), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
            )
            .sign(ca_key, hashes.SHA256())
        )
        written["clients"][peer] = {
            "cert": str(_write(out / f"client-{name}.pem",
                               client_cert.public_bytes(serialization.Encoding.PEM))),
            "key": str(_write(out / f"client-{name}.key", client_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))),
            "fingerprint": _fingerprint(client_cert.fingerprint(hashes.SHA256())),
            "expires": client_cert.not_valid_after_utc.date().isoformat(),
        }
    return written


def _safe(name: str) -> str:
    keep = [c for c in (name or "") if c.isalnum() or c in "-_"]
    return "".join(keep) or "peer"


def _is_ip(value: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def status(directory: Optional[str] = None) -> dict:
    """What material exists, and when it expires."""
    x509, hashes, serialization, rsa, NameOID, ExtendedKeyUsageOID = _crypto()
    out = tls_dir(directory)
    report: dict = {"dir": str(out), "ca": None, "server": None, "clients": []}
    if not out.is_dir():
        return report
    ca_path = out / "ca.pem"
    if ca_path.is_file():
        ca_cert = x509.load_pem_x509_certificate(ca_path.read_bytes())
        report["ca"] = {
            "path": str(ca_path),
            "fingerprint": _fingerprint(ca_cert.fingerprint(hashes.SHA256())),
            "expires": ca_cert.not_valid_after_utc.date().isoformat(),
        }
    server_path = out / "server.pem"
    if server_path.is_file():
        server_cert = x509.load_pem_x509_certificate(server_path.read_bytes())
        names = []
        try:
            ext = server_cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            names = [str(value) for value in ext.value.get_values_for_type(x509.DNSName)]
            names += [str(value) for value in ext.value.get_values_for_type(x509.IPAddress)]
        except x509.ExtensionNotFound:  # pragma: no cover - only for hand-made certs
            names = []
        report["server"] = {
            "path": str(server_path),
            "fingerprint": _fingerprint(server_cert.fingerprint(hashes.SHA256())),
            "expires": server_cert.not_valid_after_utc.date().isoformat(),
            "hosts": names,
        }
    for cert_path in sorted(out.glob("client-*.pem")):
        client_cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        report["clients"].append({
            "path": str(cert_path),
            "subject": client_cert.subject.rfc4514_string(),
            "fingerprint": _fingerprint(client_cert.fingerprint(hashes.SHA256())),
            "expires": client_cert.not_valid_after_utc.date().isoformat(),
            "has_key": cert_path.with_suffix(".key").is_file(),
        })
    return report


def server_ssl_kwargs(
    directory: Optional[str] = None,
    *,
    require_client_cert: bool = True,
    cert: Optional[str] = None,
    key: Optional[str] = None,
    client_ca: Optional[str] = None,
) -> dict:
    """The four uvicorn settings that turn on mutual TLS.

    Every path is checked here rather than at the first handshake, because a
    server that starts and then refuses every peer is harder to diagnose than a
    server that refuses to start.
    """
    out = tls_dir(directory)
    cert = cert or str(out / "server.pem")
    key = key or str(out / "server.key")
    client_ca = client_ca or str(out / "ca.pem")
    for label, path in (("certificate", cert), ("private key", key)):
        if not Path(path).is_file():
            raise FileNotFoundError(
                f"peer TLS {label} not found at {path}: run 'threadweave peer tls init' first"
            )
    kwargs: dict = {"ssl_certfile": cert, "ssl_keyfile": key}
    if require_client_cert:
        if not Path(client_ca).is_file():
            raise FileNotFoundError(
                f"client CA not found at {client_ca}: run 'threadweave peer tls init' first, "
                "or start without --require-client-cert to accept any client"
            )
        kwargs["ssl_ca_certs"] = client_ca
        kwargs["ssl_cert_reqs"] = ssl.CERT_REQUIRED
    else:
        kwargs["ssl_cert_reqs"] = ssl.CERT_NONE
    return kwargs


def server_ssl_context(directory: Optional[str] = None, **kwargs) -> ssl.SSLContext:
    """The server context for the same material, for anything that is not uvicorn.

    The acceptance run and the tests use this so they exercise exactly the
    verification the served instance applies.
    """
    settings = server_ssl_kwargs(directory, **kwargs)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(settings["ssl_certfile"], settings["ssl_keyfile"])
    if "ssl_ca_certs" in settings:
        context.load_verify_locations(settings["ssl_ca_certs"])
    context.verify_mode = settings["ssl_cert_reqs"]
    return context


def client_ssl_context(
    ca: str,
    cert: Optional[str] = None,
    key: Optional[str] = None,
) -> ssl.SSLContext:
    """A client context that trusts only this CA and presents this certificate."""
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca)
    if cert and key:
        context.load_cert_chain(certfile=cert, keyfile=key)
    return context
