from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .validator import validate_client_name


@dataclass(slots=True)
class ClientRequest:
    request_path: Path
    action: str
    client_name: str


@dataclass(slots=True)
class RejectedRequest:
    request_path: Path
    reason: str


@dataclass(slots=True)
class RequestScan:
    accepted: list[ClientRequest] = field(default_factory=list)
    rejected: list[RejectedRequest] = field(default_factory=list)


SUPPORTED_REQUEST_ACTIONS = {"issue", "revoke"}


def scan_client_requests(requests_dir: Path) -> RequestScan:
    """Parse every request file; malformed files are reported instead of aborting startup."""
    scan = RequestScan()
    if not requests_dir.exists():
        return scan

    for path in sorted(requests_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
            scan.rejected.append(RejectedRequest(path, f"unreadable JSON: {error}"))
            continue

        if not isinstance(payload, dict):
            scan.rejected.append(RejectedRequest(path, "request body must be a JSON object"))
            continue

        action = str(payload.get("action", "issue")).strip()
        if action not in SUPPORTED_REQUEST_ACTIONS:
            allowed = ", ".join(sorted(SUPPORTED_REQUEST_ACTIONS))
            scan.rejected.append(RejectedRequest(path, f"unsupported action '{action}' (allowed: {allowed})"))
            continue

        client_name = str(payload.get("client_name", "")).strip()
        try:
            validate_client_name(client_name)
        except ValueError as error:
            scan.rejected.append(RejectedRequest(path, str(error)))
            continue

        scan.accepted.append(ClientRequest(request_path=path, action=action, client_name=client_name))

    return scan


def write_result(results_dir: Path, request_name: str, payload: dict[str, object]) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    result_path = results_dir / request_name.replace(".json", ".result.json")
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result_path


def archive_request(archive_dir: Path, request_path: Path) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    target_path = archive_dir / request_path.name
    _move(request_path, target_path)
    return target_path


def reject_request(rejected_dir: Path, request_path: Path) -> Path:
    rejected_dir.mkdir(parents=True, exist_ok=True)
    target_path = rejected_dir / request_path.name
    _move(request_path, target_path)
    return target_path


def _move(source: Path, target: Path) -> None:
    if target.exists():
        target.unlink()
    shutil.move(str(source), str(target))
