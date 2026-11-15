"""Conservative, read-only inventory of explicit server crypto directives.

This is not an OpenVPN configuration evaluator or a security certification.
See https://build.openvpn.net/man/openvpn-2.6/openvpn.8.html for semantics.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path


DIRECTIVES = (
    "data-ciphers", "data-ciphers-fallback", "cipher", "ncp-ciphers",
    "tls-version-min", "tls-version-max", "tls-cipher", "tls-ciphersuites",
)
DATA_CIPHERS = {"data-ciphers", "data-ciphers-fallback", "cipher", "ncp-ciphers"}
AEAD = {"AES-128-GCM", "AES-192-GCM", "AES-256-GCM", "CHACHA20-POLY1305"}
TLS13 = {"TLS_AES_128_GCM_SHA256", "TLS_AES_256_GCM_SHA384", "TLS_CHACHA20_POLY1305_SHA256"}
TOKEN = re.compile(r"[A-Za-z0-9_?!+@=.,:-]{1,1024}\Z")


def _assess(directive: str, value: str) -> tuple[str, str]:
    name = value.upper()
    if directive.startswith("tls-version-"):
        if name in {"1.0", "1.1"}:
            return "deprecated", "TLS versions below 1.2 are obsolete."
        if name in {"1.2", "1.3"}:
            return "modern", "TLS 1.2 or newer."
    elif directive in DATA_CIPHERS:
        name = name.removeprefix("?")  # OpenVPN's optional-cipher marker.
        if name == "NONE":
            return "deprecated", "Data encryption is disabled."
        if name.startswith(("BF-", "DES-", "DES3-", "RC2-", "CAST5-", "IDEA-")):
            return "deprecated", "Obsolete small-block cipher; migrate to AEAD."
        if name in AEAD:
            return "modern", "AEAD data cipher."
        if re.fullmatch(r"(?:AES|CAMELLIA)-(?:128|192|256)-CBC", name):
            return "legacy", "CBC compatibility cipher; prefer AEAD where clients support it."
    else:
        # Do not interpret OpenSSL selectors, exclusions, or security levels as suites.
        if name.startswith("-") or any(char in name for char in "!+@=") or name in {"DEFAULT", "HIGH", "ALL", "COMPLEMENTOFDEFAULT"}:
            return "unknown", "Library cipher expression; not expanded or assessed."
        if re.search(r"(?:^|[-_])(?:RC4|3DES|DES|NULL|MD5|EXPORT)(?:$|[-_])", name):
            return "deprecated", "Suite names an obsolete or unencrypted algorithm."
        if directive == "tls-ciphersuites" and name in TLS13:
            return "modern", "TLS 1.3 AEAD suite."
        if directive == "tls-cipher" and re.fullmatch(
            r"(?:ECDHE|DHE)-(?:RSA|ECDSA)-(?:AES(?:128|256)-GCM-SHA(?:256|384)|CHACHA20-POLY1305)", name
        ):
            return "modern", "Ephemeral key exchange with an AEAD cipher."
        if "CBC" in name:
            return "legacy", "CBC suite; review compatibility needs."
    return "unknown", "Not in the report's small recognition set; review with the installed crypto library."


def collect_ciphers(path: Path) -> dict[str, object]:
    settings: dict[str, list[dict[str, object]]] = {name: [] for name in DIRECTIVES}
    warnings: list[str] = []
    report: dict[str, object] = {
        "source": str(path),
        "scope": "Explicit directives on disk, not negotiated ciphers or confirmed running configuration.",
        "available": False,
        "complete": False,
        "settings": settings,
        "warnings": warnings,
        "notes": [
            "Empty lists mean not explicitly configured; OpenVPN/library defaults are not inferred.",
            "Occurrences are in file order, not resolved effective settings. Includes are not followed.",
            "Assessments are advisory; unknown does not mean safe, and support is not probed.",
            "tls-cipher applies to TLS 1.2 and below; tls-ciphersuites applies to TLS 1.3.",
        ],
    }
    complete = True
    block: str | None = None
    try:
        with path.open(encoding="utf-8") as source:
            for number, raw in enumerate(source, 1):
                line = raw.strip()
                if block is not None:
                    if line == f"</{block}>":
                        block = None
                    continue
                # Skip every inline block, including key and tls-crypt material.
                opening = re.match(r"<([A-Za-z0-9_-]+)>", line)
                if opening:
                    block = opening[1]
                    if f"</{block}>" in line[opening.end():]:
                        block = None
                    continue
                if not line or line.startswith(("#", ";")):
                    continue
                directive = line.split(maxsplit=1)[0].removeprefix("--")
                if directive in {"config", "compat-mode", "ncp-disable", "secret"}:
                    warnings.append(f"Line {number}: {directive} may affect crypto behavior; not evaluated.")
                    complete = False
                if directive not in settings:
                    continue
                try:
                    lexer = shlex.shlex(line, posix=True)
                    lexer.whitespace_split = True
                    lexer.commenters = "#;"
                    args = list(lexer)[1:]
                except ValueError:
                    args = []
                valid = len(args) == 1 or (
                    directive == "tls-version-min" and len(args) == 2 and args[1] == "or-highest"
                )
                if not valid or not TOKEN.fullmatch(args[0]) or any(not part for part in args[0].split(":")):
                    warnings.append(f"Line {number}: malformed {directive}; value omitted.")
                    complete = False
                    continue
                if settings[directive]:
                    warnings.append(f"Line {number}: repeated {directive}; review all occurrences.")
                if directive in {"cipher", "ncp-ciphers"}:
                    warnings.append(
                        f"Line {number}: legacy {directive} directive; use data-ciphers for negotiation."
                    )
                if directive == "cipher":
                    warnings.append("OpenVPN 2.6 ignores cipher in TLS mode unless compatibility behavior is enabled.")
                values = []
                for value in args[0].split(":"):
                    assessment, reason = _assess(directive, value)
                    values.append({"name": value, "assessment": assessment, "reason": reason})
                settings[directive].append({"line": number, "values": values, "or_highest": len(args) == 2})
    except (OSError, UnicodeError):
        # Do not echo OS errors, file contents, or partially decoded data.
        warnings[:] = ["Rendered server configuration is missing, unreadable, or not UTF-8."]
        for entries in settings.values():
            entries.clear()
        return report
    if block is not None:
        warnings.append("Unterminated inline block; remaining content was omitted.")
        complete = False
    report["available"] = True
    report["complete"] = complete
    return report
