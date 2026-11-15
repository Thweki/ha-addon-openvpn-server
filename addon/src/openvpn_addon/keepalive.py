"""Read-only inventory of global keepalive directives, never live client state."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any


TIMERS = {"keepalive": 2, "ping": 1, "ping-restart": 1, "ping-exit": 1}


def _tokens(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#;"
    return list(lexer)


def collect_keepalive(path: Path) -> dict[str, Any]:
    """Read only server.conf; emit allowlisted names and validated integers.

    Expansions describe each keepalive directive in server mode, not resolved
    effective values. Includes, CCD files, scripts and profiles are not read.
    """
    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    report = {
        "source": "server.conf",
        "scope": "On-disk global configuration; not confirmed live client settings. "
        "Per-client overrides, scripts, includes and client pull filters are not evaluated.",
        "available": False,
        "directives": entries,
        "warnings": warnings,
    }
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        warnings.append("server.conf is missing; start the add-on to render it.")
        return report
    except (OSError, UnicodeError):
        # Exception messages and input lines can contain sensitive values.
        warnings.append("server.conf could not be read as UTF-8 text.")
        return report
    report["available"] = True
    inline_tag = None
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if inline_tag is not None:
            if stripped == f"</{inline_tag}>":
                inline_tag = None
            continue
        if stripped.startswith("<"):
            match = re.match(r"<([\w-]+)>", stripped)
            if match and f"</{match[1]}>" not in stripped:
                inline_tag = match[1]
            continue
        if not stripped or stripped.startswith(("#", ";")):
            continue
        head = stripped.split(maxsplit=1)[0].removeprefix("--")
        if head in {"config", "push-reset", "push-remove"}:
            warnings.append(f"Line {number}: {head} is not evaluated; this inventory may be incomplete.")
            continue
        if head not in {*TIMERS, "push"}:
            continue
        try:
            tokens = _tokens(stripped)
            directive = tokens[0].removeprefix("--")
            scope = "server"
            if directive == "push":
                if len(tokens) != 2:
                    raise ValueError
                tokens = _tokens(tokens[1])
                if not tokens or tokens[0] not in TIMERS:
                    continue
                directive = tokens[0]
                scope = "client_push"
            values = tokens[1:]
            if len(values) != TIMERS[directive] or any(
                re.fullmatch(r"[0-9]{1,10}", value) is None for value in values
            ):
                raise ValueError
            seconds = [int(value) for value in values]
            if any(value > 2_147_483_647 for value in seconds):
                raise ValueError
        except ValueError:
            warnings.append(f"Line {number}: malformed timer or push directive omitted.")
            continue
        entry = {"line": number, "scope": scope, "directive": directive, "values_seconds": seconds}
        entries.append(entry)
        if directive == "keepalive":
            interval, timeout = seconds
            if scope == "client_push":
                warnings.append(f"Line {number}: keepalive is not a supported client push; push ping timers instead.")
            elif interval == timeout == 0:
                warnings.append(f"Line {number}: keepalive helper is disabled.")
            elif interval == 0 or timeout < 2 * interval or timeout > 1_073_741_823:
                warnings.append(f"Line {number}: keepalive cannot safely expand; use positive timers with timeout at least twice interval.")
            else:
                entry["server_mode_expansion"] = {
                    "server": {"ping": interval, "ping-restart": timeout * 2},
                    "client_push": {"ping": interval, "ping-restart": timeout},
                }
    if inline_tag is not None:
        warnings.append("Unterminated inline block; remaining content was ignored.")
    if not entries:
        warnings.append("No supported timer directives found; no defaults are inferred.")
    seen = set()
    for entry in entries:
        key = (entry["scope"], entry["directive"])
        if key in seen:
            warnings.append(f"Line {entry['line']}: repeated {key[1]} in {key[0]}; no effective value is inferred.")
        seen.add(key)
    if ("server", "keepalive") in seen and any(name != "keepalive" for _, name in seen):
        warnings.append("Keepalive and explicit ping timers coexist; inspect for server conflicts or duplicate pushes. Expansions are illustrative only.")
    return report
