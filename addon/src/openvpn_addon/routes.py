"""Read explicit route directives from runtime files, never credentials or profiles.

This is a file snapshot, not an evaluation of OpenVPN's per-session push list.
Only numeric route operands and known flags are returned; raw lines and parser
exceptions may contain secrets and must never reach the report.
"""
from __future__ import annotations

import ipaddress
import os
import shlex
import stat
from pathlib import Path
from typing import Any

from .config import AddonConfig
from .validator import CLIENT_NAME_RE


REDIRECT_FLAGS = {"local", "autolocal", "def1", "bypass-dhcp", "bypass-dns", "block-local", "ipv6", "!ipv4"}


def _tokens(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#;"
    return list(lexer)


def _route(tokens: list[str]) -> dict[str, Any]:
    directive, *args = tokens
    ipv6 = directive.endswith("-ipv6")
    internal = directive.startswith("iroute")
    maximum = (1 if ipv6 else 2) if internal else (3 if ipv6 else 4)
    if not 1 <= len(args) <= maximum:
        raise ValueError
    if ipv6:
        network = ipaddress.IPv6Network(args[0], strict=False)
        extra = args[1:]
    else:
        address = ipaddress.IPv4Address(args[0])
        mask = args[1] if len(args) > 1 else "255.255.255.255"
        network = ipaddress.IPv4Network(f"{address}/{mask}", strict=False)
        # IPv4 hostmasks and CIDR operands are outside the generated syntax.
        if str(network.netmask) != mask:
            raise ValueError
        extra = args[2:]
    result: dict[str, Any] = {"network": str(network)}
    if extra:
        gateway = extra[0]
        if gateway not in {"vpn_gateway", "net_gateway", "remote_host", "default"}:
            gateway = str((ipaddress.IPv6Address if ipv6 else ipaddress.IPv4Address)(gateway))
        result["gateway"] = gateway
    if len(extra) > 1:
        if not extra[1].isascii() or not extra[1].isdigit():
            raise ValueError
        result["metric"] = int(extra[1])
    return result


def _scan(path: Path, source: str, report: dict[str, Any]) -> list[str]:
    """Return CCD references internally; emit only allowlisted routing fields."""
    ccd_paths: list[str] = []
    try:
        # Do not follow links into PKI or block on a special file in the CCD directory.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
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
                    args = tokens[1:]
                    if directive == "client-config-dir":
                        if len(args) != 1:
                            raise ValueError
                        ccd_paths.append(args[0])
                        continue
                    if directive in {"config", "client-connect", "plugin", "push-remove"}:
                        report["warnings"].append({"source": source, "line": number,
                                                   "message": "Dynamic, included or filtered directives are not evaluated."})
                        continue
                    pushed = directive == "push"
                    if pushed:
                        if len(args) != 1:
                            raise ValueError
                        tokens = _tokens(args[0])
                        if not tokens:
                            raise ValueError
                        directive, *args = tokens
                    fields: dict[str, Any]
                    if (pushed and directive in {"route", "route-ipv6"}) or (
                        not pushed and directive in {"iroute", "iroute-ipv6"}
                    ):
                        fields = _route([directive, *args])
                    elif pushed and directive == "redirect-gateway":
                        if not set(args) <= REDIRECT_FLAGS:
                            raise ValueError
                        fields = {"flags": args}
                    elif not pushed and directive == "push-reset":
                        if args:
                            raise ValueError
                        fields = {}
                    else:
                        continue
                    report["entries"].append({"source": source, "line": number,
                                              "directive": f"push {directive}" if pushed else directive,
                                              **fields})
                except ValueError:
                    report["warnings"].append({"source": source, "line": number,
                                               "message": "Malformed or unsupported directive omitted."})
            if block:
                report["warnings"].append({"source": source, "message": "Unterminated inline block."})
    except (OSError, UnicodeError):
        report["warnings"].append({"source": source, "message": "File missing, unreadable or not a regular file."})
    return ccd_paths


def collect_routes(config: AddonConfig) -> dict[str, Any]:
    report: dict[str, Any] = {
        "source": "runtime_files",
        "scope": "Explicit directives on disk; not live session or kernel routes. Inheritance is not evaluated.",
        "ccd_enabled": False,
        "entries": [],
        "warnings": [],
    }
    if config.runtime_dir.is_symlink():
        report["warnings"].append({"source": "runtime", "message": "Symlinked runtime directory is not inspected."})
    else:
        ccd_paths = _scan(config.server_conf_path, "server.conf", report)
        if ccd_paths:
            report["ccd_enabled"] = True
            if ccd_paths != [str(config.ccd_dir)] or config.ccd_dir.is_symlink():
                report["warnings"].append({"source": "ccd", "message": "Only the add-on's regular CCD directory is inspected."})
            else:
                try:
                    for path in sorted(config.ccd_dir.iterdir()):
                        if not CLIENT_NAME_RE.fullmatch(path.name):
                            report["warnings"].append({"source": "ccd", "message": "Entry with unsupported name omitted."})
                            continue
                        _scan(path, f"ccd/{path.name}", report)
                except OSError:
                    report["warnings"].append({"source": "ccd", "message": "CCD directory missing or unreadable."})
    report["complete"] = not report["warnings"]
    return report
