"""Inspect explicit DNS pushes on disk without loading options, PKI or profiles."""
from __future__ import annotations

import ipaddress
import os
import re
import shlex
import stat
from typing import Any

from .config import AddonConfig


DOMAIN_OPTIONS = {"DOMAIN", "DOMAIN-SEARCH", "ADAPTER_DOMAIN_SUFFIX"}
DNS_FLAGS = {"block-outside-dns", "register-dns"}
UNEVALUATED = {"config", "client-config-dir", "client-connect", "plugin", "push-remove", "push-reset", "dns"}
DOMAIN_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def _tokens(line: str, *, comments: bool = True) -> list[str]:
    lexer = shlex.shlex(line, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#;" if comments else ""
    return list(lexer)


def _dns_option(tokens: list[str]) -> dict[str, str] | None:
    """Only return known DNS fields, never raw lines or arbitrary push values."""
    directive, *args = tokens
    if directive in DNS_FLAGS:
        if args:
            raise ValueError
        return {"option": directive}
    if directive != "dhcp-option":
        return None
    if not args:
        raise ValueError
    option = args[0]
    if option not in {"DNS", "DNS6", *DOMAIN_OPTIONS}:
        # Unknown DNS extensions must not silently look like an empty result.
        if option.startswith(("DNS", "DOMAIN")):
            raise ValueError
        return None
    if len(args) != 2:
        raise ValueError
    value = args[1]
    if option in {"DNS", "DNS6"}:
        # Scoped IPv6 addresses carry an arbitrary interface name; omit them.
        if "%" in value:
            raise ValueError
        address = ipaddress.ip_address(value)
        if option == "DNS6" and address.version != 6:
            raise ValueError
        value = str(address)
    elif len(value.rstrip(".")) > 253 or not all(
        DOMAIN_LABEL.fullmatch(label) for label in value.removesuffix(".").split(".")
    ):
        raise ValueError
    return {"option": f"dhcp-option {option}", "value": value}


def collect_dns(config: AddonConfig) -> dict[str, Any]:
    report: dict[str, Any] = {
        "source": "server.conf",
        "scope": "Explicit global DNS pushes on disk; not live or client-applied DNS. Client overrides are not evaluated.",
        "entries": [],
        "warnings": [],
    }

    def warn(message: str, line: int | None = None) -> None:
        warning: dict[str, Any] = {"message": message}
        if line is not None:
            warning["line"] = line
        report["warnings"].append(warning)

    try:
        if config.runtime_dir.is_symlink():
            raise OSError
        # Refuse symlinks and special files; never follow key/include/CCD paths.
        fd = os.open(config.server_conf_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError
            block = None
            for number, line in enumerate(stream, 1):
                stripped = line.strip()
                if block:
                    if stripped == f"</{block}>":
                        block = None
                    continue
                if stripped.startswith("<") and stripped.endswith(">"):
                    block = stripped[1:-1]
                    continue
                try:
                    tokens = _tokens(line)
                    if not tokens:
                        continue
                    directive = tokens[0].removeprefix("--")
                    if directive in UNEVALUATED:
                        warn("Included, dynamic, per-client or overriding options are not evaluated.", number)
                        continue
                    if directive != "push":
                        continue
                    if len(tokens) != 2:
                        raise ValueError
                    pushed = _tokens(tokens[1], comments=False)
                    if not pushed:
                        raise ValueError
                    if pushed[0] == "dns":
                        warn("The newer dns syntax is not inspected; only legacy DNS pushes are listed.", number)
                        continue
                    fields = _dns_option(pushed)
                    if fields is not None:
                        report["entries"].append({"line": number, **fields})
                except ValueError:
                    warn("Malformed or unsupported option omitted.", number)
            if block:
                warn("Unterminated inline block.")
    except (OSError, UnicodeError):
        warn("Server configuration missing, unreadable, symlinked or not a regular UTF-8 file.")
    report["complete"] = not report["warnings"]
    return report
