"""Read-only inventory of packet-size directives, not an OpenVPN evaluator."""
from __future__ import annotations

import os
import re
import shlex
import stat
from pathlib import Path
from typing import Any


DIRECTIVES = ("tun-mtu", "tun-mtu-extra", "link-mtu", "mssfix", "fragment", "max-packet-size")
UNEVALUATED = {"config", "push-reset", "push-remove", "compat-mode"}


def _tokens(line: str, *, comments: bool = True) -> list[str]:
    lexer = shlex.shlex(line, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#;" if comments else ""
    return list(lexer)


def _arguments(directive: str, args: list[str]) -> list[int | str]:
    # Only numeric arguments and documented mode keywords may reach the output.
    # This recognizes syntax, not the installed OpenVPN version's value limits.
    if directive == "mssfix" and not args:
        return []
    modes = {"mtu", "fixed"} if directive == "mssfix" else {"mtu"} if directive == "fragment" else set()
    if not args or len(args) > 2 or (len(args) == 2 and args[1] not in modes):
        raise ValueError
    if not re.fullmatch(r"[0-9]{1,10}", args[0]):
        raise ValueError
    return [int(args[0]), *args[1:]]


def collect_mtu(path: Path) -> dict[str, Any]:
    settings: dict[str, dict[str, list[dict[str, Any]]]] = {
        scope: {name: [] for name in DIRECTIVES} for scope in ("server", "push")
    }
    warnings: list[str] = []
    report: dict[str, Any] = {
        "source": "server.conf",
        "scope": "Explicit global settings on disk; not live, negotiated or measured path MTU.",
        "available": False,
        "complete": False,
        "settings": settings,
        "warnings": warnings,
        "notes": [
            "Empty lists mean no explicit occurrence; defaults are not inferred.",
            "Occurrences are in file order; repeated values and push acceptance are not resolved.",
            "Includes, per-client overrides, scripts and client profiles are not read or evaluated.",
            "mssfix with no arguments inherits fragment parameters or OpenVPN defaults; 0 disables it.",
            "mtu includes outer IP/UDP headers; fixed limits inner IP packets without calculating encapsulation.",
            "max-packet-size also affects mssfix; this report does not expand implicit settings.",
            "Syntax is inventoried, not validated against the installed OpenVPN version or transport.",
        ],
    }
    block: str | None = None
    try:
        # The runtime file must not redirect this reader to PKI or block on a FIFO.
        if path.parent.is_symlink():
            raise OSError
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise OSError
            for number, raw in enumerate(source, 1):
                line = raw.strip()
                if block is not None:
                    if line.split("#", 1)[0].split(";", 1)[0].strip() == f"</{block}>":
                        block = None
                    continue
                opening = re.match(r"<([A-Za-z0-9_-]+)>", line)
                if opening:
                    block = opening[1]
                    if block == "connection":
                        warnings.append(f"Line {number}: connection block not evaluated.")
                    if f"</{block}>" in line[opening.end():]:
                        block = None
                    continue
                if not line or line.startswith(("#", ";")):
                    continue
                scope = "server"
                try:
                    tokens = _tokens(line)
                    if not tokens:
                        continue
                    directive = tokens[0].removeprefix("--")
                    if directive in UNEVALUATED:
                        warnings.append(f"Line {number}: {directive} not evaluated.")
                        continue
                    if directive == "push":
                        if len(tokens) != 2:
                            raise ValueError
                        tokens = _tokens(tokens[1], comments=False)
                        if not tokens:
                            raise ValueError
                        directive = tokens[0].removeprefix("--")
                        scope = "push"
                    if directive not in DIRECTIVES:
                        continue
                    args = _arguments(directive, tokens[1:])
                except ValueError:
                    # Never echo malformed lines, tokens, paths or exception text.
                    warnings.append(f"Line {number}: malformed or unsupported syntax; omitted.")
                    continue
                entries = settings[scope][directive]
                if entries:
                    warnings.append(f"Line {number}: repeated {scope} {directive}; review all occurrences.")
                entries.append({"line": number, "arguments": args})
    except (OSError, UnicodeError):
        warnings[:] = ["Server configuration missing, unreadable, symlinked or not a regular UTF-8 file."]
        for directives in settings.values():
            for entries in directives.values():
                entries.clear()
        return report
    if block is not None:
        warnings.append("Unterminated inline block; remaining content omitted.")
    report["available"] = True
    report["complete"] = not warnings
    return report
