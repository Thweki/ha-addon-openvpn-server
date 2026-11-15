from __future__ import annotations

import csv
import ipaddress
import json
import os
import time

from .config import AddonConfig
from .validator import validate_client_name


def collect_leases(config: AddonConfig, *, now: float | None = None) -> dict[str, object]:
    """Read address metadata only; never initialize PKI or synchronize assignments."""
    status, sessions = _read_sessions(config, time.time() if now is None else now)
    fixed_state, assignments = _read_assignments(config)
    observed = {(row["name"], row["vpn_address"]) for row in sessions}
    leases = sessions + [
        _lease(name, address, "fixed")
        for name, address in assignments.items()
        if (name, address) not in observed
    ]
    leases.sort(key=lambda row: (row["name"], row["vpn_address"] or "", row["real_address"] or ""))
    return {"status": status, "fixed_assignments": fixed_state, "leases": leases}


def _lease(name: str, address: str | None, source: str,
           real_address: str | None = None, connected_since: str | None = None) -> dict[str, str | None]:
    return {
        "name": name,
        "vpn_address": address,
        "source": source,
        "real_address": real_address,
        "connected_since": connected_since,
    }


def _address(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _valid_name(name: str) -> bool:
    try:
        validate_client_name(name)
    except ValueError:
        return False
    return True


def _read_assignments(config: AddonConfig) -> tuple[str, dict[str, str]]:
    if not config.uses_client_config_dir:
        return "disabled", {}
    try:
        payload = json.loads(config.client_ips_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "missing", {}
    except (ValueError, UnicodeError):
        return "invalid", {}
    except OSError:
        return "unreadable", {}
    if not isinstance(payload, dict):
        return "invalid", {}
    assignments = {}
    for name, value in payload.items():
        address = _address(value)
        if _valid_name(name) and address is not None:
            assignments[name] = address
    return ("ok" if len(assignments) == len(payload) else "partial"), assignments


def _read_sessions(config: AddonConfig, now: float) -> tuple[dict[str, object], list[dict[str, str | None]]]:
    # The add-on currently emits status-version 1. Its client rows do
    # not contain VPN IPs; match routing rows by BOTH name and remote endpoint.
    sessions = []
    direct_rows = []
    columns = []
    routes: dict[tuple[str, str], set[str]] = {}
    section = ""
    client_header = route_header = complete = False
    age = None
    try:
        with config.status_path.open(encoding="utf-8") as stream:
            before = os.fstat(stream.fileno())
            age = max(0, int(now - before.st_mtime))
            for line in stream:
                fields = next(csv.reader([line], delimiter="\t" if "\t" in line else ","))
                if not fields:
                    continue
                if fields[:2] == ["HEADER", "CLIENT_LIST"]:
                    # Versions 2/3 provide assigned addresses directly. Resolve
                    # named columns so new fields cannot shift their meaning.
                    columns = fields[2:]
                    required = {"Common Name", "Real Address", "Virtual Address", "Connected Since"}
                    if not required.issubset(columns) or len(set(columns)) != len(columns):
                        raise ValueError("Invalid client header")
                    client_header = True
                elif fields[0] == "CLIENT_LIST":
                    if not columns or len(fields) - 1 < len(columns):
                        raise ValueError("Invalid client row")
                    client = dict(zip(columns, fields[1:]))
                    if not _valid_name(client["Common Name"]) or not client["Real Address"]:
                        raise ValueError("Invalid client identity")
                    addresses = {
                        address for key in ("Virtual Address", "Virtual IPv6 Address")
                        if (address := _address(client.get(key))) is not None
                    }
                    direct_rows.extend(
                        _lease(client["Common Name"], address, "session",
                               client["Real Address"], client["Connected Since"])
                        for address in sorted(addresses) or [None]
                    )
                elif fields == ["Common Name", "Real Address", "Bytes Received", "Bytes Sent", "Connected Since"]:
                    client_header = True
                    section = "clients"
                elif fields == ["ROUTING TABLE"]:
                    section = ""
                elif fields == ["Virtual Address", "Common Name", "Real Address", "Last Ref"]:
                    route_header = True
                    section = "routes"
                elif fields == ["GLOBAL STATS"]:
                    section = ""
                elif fields == ["END"]:
                    complete = True
                    break
                elif section == "clients":
                    if len(fields) != 5 or not _valid_name(fields[0]) or not fields[1]:
                        raise ValueError("Invalid client row")
                    sessions.append((fields[0], fields[1], fields[4]))
                elif section == "routes":
                    if len(fields) != 4:
                        raise ValueError("Invalid routing row")
                    address = _address(fields[0])
                    if address is not None:
                        routes.setdefault((fields[1], fields[2]), set()).add(address)
            after = os.fstat(stream.fileno())
            if not (client_header and (route_header or columns) and complete) or (
                before.st_mtime_ns, before.st_size
            ) != (after.st_mtime_ns, after.st_size):
                raise ValueError("Incomplete or changing status snapshot")
    except FileNotFoundError:
        return {"state": "missing", "age_seconds": None}, []
    except (ValueError, UnicodeError, csv.Error):
        return {"state": "invalid", "age_seconds": age}, []
    except OSError:
        return {"state": "unreadable", "age_seconds": age}, []

    rows = direct_rows + [
        _lease(name, address, "session", endpoint, since)
        for name, endpoint, since in sessions
        for address in sorted(routes.get((name, endpoint), set())) or [None]
    ]
    state = "stale" if now - before.st_mtime > 2 * config.status_interval else "fresh"
    return {"state": state, "age_seconds": age}, rows
