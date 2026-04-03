from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .validator import validate_client_name


@dataclass(slots=True)
class ClientRequest:
    request_path: Path
    action: str
    client_name: str


SUPPORTED_REQUEST_ACTIONS = {"issue", "revoke"}


def load_client_requests(requests_dir: Path) -> list[ClientRequest]:
    items: list[ClientRequest] = []
    if not requests_dir.exists():
        return items

    for path in sorted(requests_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        action = str(payload.get("action", "issue")).strip()
        if action not in SUPPORTED_REQUEST_ACTIONS:
            raise ValueError(f"Unsupported request action in {path.name}: {action}")

        client_name = str(payload.get("client_name", "")).strip()
        validate_client_name(client_name)
        items.append(ClientRequest(request_path=path, action=action, client_name=client_name))

    return items


def write_result(results_dir: Path, request_name: str, payload: dict[str, object]) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    result_path = results_dir / request_name.replace(".json", ".result.json")
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result_path


def archive_request(archive_dir: Path, request_path: Path) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    target_path = archive_dir / request_path.name
    shutil.move(str(request_path), str(target_path))
    return target_path
