"""Offline preflight for this add-on's inline profiles. Never execute profile directives."""
from __future__ import annotations

import hmac
import os
import re
import stat
import subprocess
from pathlib import Path

from .config import AddonConfig
from .renderer import TLS_VERSION_MIN, _data_ciphers, _resolve_remote


MAX_PROFILE_BYTES = 128 * 1024
OPENSSL_TIMEOUT = 5
DIRECTIVE_ARITY = {
    "client": 0, "dev": 1, "proto": 1, "remote": 2, "resolv-retry": 1,
    "nobind": 0, "persist-key": 0, "persist-tun": 0, "remote-cert-tls": 1,
    "data-ciphers": 1, "tls-version-min": 1, "auth": 1, "verb": 1,
}
BLOCKS = {"ca", "cert", "key", "tls-crypt"}


def error_report(check: str, message: str) -> dict:
    return {"schema_version": 1, "status": "error", "checks": [
        {"check": check, "status": "error", "message": message},
    ]}


def _read_bounded(path: Path) -> bytes:
    # Reject FIFOs/devices without blocking; keep file contents and paths out of errors.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Expected a regular file")
        data = stream.read(MAX_PROFILE_BYTES + 1)
    if len(data) > MAX_PROFILE_BYTES:
        raise ValueError("Input exceeds size limit")
    return data


def _parse_profile(data: bytes) -> tuple[dict[str, list[str]], dict[str, bytes]]:
    directives: dict[str, list[str]] = {}
    blocks: dict[str, bytes] = {}
    active: str | None = None
    body: list[str] = []
    for raw in data.decode("utf-8").splitlines():
        line = raw.strip()
        if active is not None:
            if line == f"</{active}>":
                blocks[active] = ("\n".join(body) + "\n").encode("ascii")
                active = None
                body = []
            elif "<" in line or ">" in line:
                raise ValueError("Nested or mismatched block")
            else:
                body.append(line)
            continue
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("<"):
            name = line[1:-1]
            if line != f"<{name}>" or name not in BLOCKS or name in blocks:
                raise ValueError("Unsupported or duplicate block")
            active = name
            continue
        parts = line.split()
        name = parts[0]
        if name not in DIRECTIVE_ARITY or name in directives or len(parts) != DIRECTIVE_ARITY[name] + 1:
            raise ValueError("Unsupported or duplicate directive")
        directives[name] = parts[1:]
    if active or set(directives) != set(DIRECTIVE_ARITY) or not {"ca", "cert", "key"} <= blocks.keys():
        raise ValueError("Incomplete profile")
    # OpenSSL can ignore trailing objects: insist on one PEM object per block.
    for name, labels in (("ca", "CERTIFICATE"), ("cert", "CERTIFICATE"),
                         ("key", "(?:RSA |EC )?PRIVATE KEY")):
        pattern = rb"-----BEGIN (" + labels.encode() + rb")-----\s+[A-Za-z0-9+/=\s]+-----END \1-----\s*"
        if not re.fullmatch(pattern, blocks[name]):
            raise ValueError("Expected a single unencrypted PEM object")
    return directives, blocks


def _openssl(data: bytes, *arguments: str) -> subprocess.CompletedProcess:
    # All arguments are controlled here. Secrets go through stdin, never argv or files.
    # Capture even failure output: OpenSSL errors can contain certificate identities.
    return subprocess.run(
        ["openssl", *arguments], input=data, capture_output=True, timeout=OPENSSL_TIMEOUT,
    )


def _tls_key(data: bytes) -> bytes:
    lines = [line.strip() for line in data.splitlines() if line.strip() and not line.startswith(b"#")]
    if (len(lines) < 3 or lines[0] != b"-----BEGIN OpenVPN Static key V1-----"
            or lines[-1] != b"-----END OpenVPN Static key V1-----"):
        raise ValueError("Invalid TLS key")
    body = b"".join(lines[1:-1])
    if not re.fullmatch(rb"[a-fA-F0-9]{512}", body):
        raise ValueError("Invalid TLS key")
    return bytes.fromhex(body.decode("ascii"))


def check_profile(config: AddonConfig, path: Path) -> tuple[dict, int]:
    """Return a redacted JSON-compatible report and exit code; write no files."""
    try:
        directives, blocks = _parse_profile(_read_bounded(path))
    except (OSError, ValueError, UnicodeError):
        return error_report("profile_format", "Unreadable or unsupported profile; use an inline add-on export (at most 128 KiB)."), 2

    checks: list[dict] = []

    def record(name: str, passed: bool, failure: str) -> None:
        checks.append({"check": name, "status": "pass" if passed else "fail",
                       "message": "OK" if passed else failure})

    try:
        host, port = _resolve_remote(config)
        expected = {
            "client": [], "dev": ["tun"], "proto": [config.protocol],
            "remote": [host, str(port)], "resolv-retry": ["infinite"], "nobind": [],
            "persist-key": [], "persist-tun": [], "remote-cert-tls": ["server"],
            "data-ciphers": [_data_ciphers(config)], "tls-version-min": [TLS_VERSION_MIN],
            "auth": [config.auth], "verb": ["3"],
        }
        record("connection_settings", bool(config.public_endpoint.strip()) and directives == expected,
               "Profile settings differ from the current export settings, or public_endpoint is unset; re-export the profile.")

        ca = _openssl(blocks["ca"], "x509", "-outform", "DER")
        current_ca = _openssl(_read_bounded(config.pki_dir / "ca.crt"), "x509", "-outform", "DER")
        record("ca_match", ca.returncode == current_ca.returncode == 0 and ca.stdout == current_ca.stdout,
               "Embedded CA is invalid or differs from the current CA; re-export the profile.")

        key_valid = _openssl(blocks["key"], "pkey", "-check", "-noout", "-passin", "pass:")
        key = _openssl(blocks["key"], "pkey", "-pubout", "-passin", "pass:")
        cert_key = _openssl(blocks["cert"], "x509", "-pubkey", "-noout")
        record("key_match", key_valid.returncode == key.returncode == cert_key.returncode == 0
               and key.stdout == cert_key.stdout,
               "Private key is invalid or does not match the client certificate.")

        verify_args = ("verify", "-CAfile", str(config.pki_dir / "ca.crt"),
                       "-no-CApath", "-no-CAstore", "-purpose", "sslclient")
        chain = _openssl(blocks["cert"], *verify_args)
        record("certificate", chain.returncode == 0,
               "Client certificate fails current CA, validity, or TLS client purpose verification.")
        crl = _openssl(blocks["cert"], *verify_args, "-CRLfile", str(config.crl_path), "-crl_check")
        record("revocation", crl.returncode == 0,
               "Certificate verification with the current CRL failed; check revocation and CRL validity.")

        tls_ok = "tls-crypt" not in blocks
        if config.tls_crypt:
            tls_ok = False
            if "tls-crypt" in blocks:
                try:
                    tls_ok = hmac.compare_digest(_tls_key(blocks["tls-crypt"]),
                                                 _tls_key(_read_bounded(config.tls_crypt_path)))
                except ValueError:
                    pass
        record("tls_crypt", tls_ok,
               "TLS-crypt material is missing, invalid, or differs from the current server settings.")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return error_report("environment", "Unable to complete checks; ensure current PKI files and OpenSSL are available."), 2

    passed = all(item["status"] == "pass" for item in checks)
    return {"schema_version": 1, "status": "pass" if passed else "fail", "checks": checks}, 0 if passed else 1
