from __future__ import annotations

import ipaddress
import json
import os
import ssl
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import AddonConfig
from .validator import validate_client_name


EASYRSA = Path("/usr/share/easy-rsa/easyrsa")

STATUS_VALID = "valid"
STATUS_REVOKED = "revoked"
STATUS_MISSING = "missing"


@dataclass(slots=True)
class ClientInfo:
    name: str
    status: str
    expires: str | None = None


@dataclass(slots=True)
class IssueResult:
    client_name: str
    action: str
    detail: str


@dataclass(slots=True)
class CrlInfo:
    last_update: str | None = None
    next_update: str | None = None
    days_remaining: int | None = None


class PkiManager:
    def __init__(self, config: AddonConfig) -> None:
        self.config = config

    def ensure_base(self) -> None:
        if not self.config.pki_dir.exists():
            self._run("init-pki", cwd=self.config.pki_dir.parent)

        if not (self.config.pki_dir / "ca.crt").exists():
            self._run("build-ca", "nopass", cwd=self.config.pki_dir.parent, cn="Home Assistant OpenVPN CA")

        if not (self.config.pki_dir / "issued" / "server.crt").exists():
            self._run("build-server-full", "server", "nopass", cwd=self.config.pki_dir.parent)

        if not self.config.tls_crypt_path.exists():
            subprocess.run(
                ["openvpn", "--genkey", "secret", str(self.config.tls_crypt_path)],
                check=True,
            )

        self.refresh_crl()

    def refresh_crl(self) -> None:
        self._run("gen-crl", cwd=self.config.pki_dir.parent)

    def ensure_client(self, client_name: str) -> IssueResult:
        validate_client_name(client_name)
        status = self.client_status(client_name)
        if status == STATUS_VALID:
            return IssueResult(client_name, "exists", "client certificate already exists and is valid")
        if status == STATUS_REVOKED:
            # EasyRSA keeps revoked names in index.txt, so the certificate is re-enabled rather
            # than silently re-exported in a revoked state.
            self._run("unrevoke", client_name, cwd=self.config.pki_dir.parent)
            self.refresh_crl()
            return IssueResult(
                client_name,
                "unrevoked",
                "client certificate was revoked and has been unrevoked; issue a client with a new "
                "name if the previous key may have leaked",
            )

        self._run(
            "build-client-full",
            client_name,
            "nopass",
            cwd=self.config.pki_dir.parent,
            days=str(self.config.client_expire_days),
        )
        return IssueResult(client_name, "created", "client certificate created")

    def revoke_client(self, client_name: str) -> None:
        validate_client_name(client_name)
        status = self.client_status(client_name)
        if status == STATUS_MISSING:
            raise FileNotFoundError(f"Client certificate not found: {client_name}")
        if status == STATUS_REVOKED:
            raise ValueError(f"Client certificate is already revoked: {client_name}")
        self._run("revoke", client_name, cwd=self.config.pki_dir.parent)
        self.refresh_crl()

    def client_status(self, client_name: str) -> str:
        entries = self.index_entries()
        status = entries.get(client_name)
        if status is None:
            if (self.config.pki_dir / "issued" / f"{client_name}.crt").exists():
                return STATUS_VALID
            return STATUS_MISSING
        return STATUS_VALID if status == "V" else STATUS_REVOKED

    def index_entries(self) -> dict[str, str]:
        index = self.config.pki_dir / "index.txt"
        if not index.exists():
            return {}
        entries: dict[str, str] = {}
        for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
            fields = line.split("\t")
            if len(fields) < 6:
                continue
            name = fields[5].strip()
            if name.startswith("/CN="):
                entries[name[len("/CN=") :]] = fields[0].strip()
        return entries

    def list_clients(self) -> list[ClientInfo]:
        names = set(self.index_entries())
        issued_dir = self.config.pki_dir / "issued"
        if issued_dir.exists():
            names.update(item.stem for item in issued_dir.glob("*.crt") if item.name != "server.crt")
        names.discard("server")

        result: list[ClientInfo] = []
        for name in sorted(names):
            status = self.client_status(name)
            if status == STATUS_MISSING:
                continue
            expires = self._cert_expiry(name)
            result.append(ClientInfo(name=name, status=status, expires=expires))
        return result

    def crl_info(self) -> CrlInfo:
        if not self.config.crl_path.exists():
            return CrlInfo()
        completed = subprocess.run(
            ["openssl", "crl", "-in", str(self.config.crl_path), "-noout", "-lastupdate", "-nextupdate"],
            capture_output=True,
            text=True,
        )
        info = CrlInfo()
        if completed.returncode != 0:
            return info
        for line in completed.stdout.splitlines():
            key, _, value = line.partition("=")
            value = value.strip()
            key = key.strip().lower()
            if key == "lastupdate":
                info.last_update = value
            elif key == "nextupdate":
                info.next_update = value
                try:
                    seconds = ssl.cert_time_to_seconds(value)
                except ValueError:
                    continue
                remaining = datetime.fromtimestamp(seconds, tz=timezone.utc) - datetime.now(timezone.utc)
                info.days_remaining = remaining.days
        return info

    def ensure_client_ips(self, names: list[str]) -> dict[str, str]:
        """Assign a stable VPN address to every client and write its ccd entry."""
        if not self.config.uses_client_config_dir or not names:
            self._clear_ccd()
            return {}

        network = ipaddress.IPv4Network(self.config.vpn_network, strict=False)
        assignments = self._load_client_ips()
        assignments = {name: ip for name, ip in assignments.items() if name in names}

        taken = set(assignments.values())
        free = (
            str(address)
            for address in network.hosts()
            if address != ipaddress.IPv4Address(int(network.network_address) + 1) and str(address) not in taken
        )
        for name in names:
            if name in assignments:
                continue
            address = next(free, None)
            if address is None:
                raise RuntimeError(f"VPN network {network} has no free address for client {name}")
            assignments[name] = address

        self._save_client_ips(assignments)

        self.config.ccd_dir.mkdir(parents=True, exist_ok=True)
        for stale in self.config.ccd_dir.iterdir():
            if stale.name not in assignments:
                stale.unlink()
        for name, address in assignments.items():
            (self.config.ccd_dir / name).write_text(
                f"ifconfig-push {address} {self.config.vpn_netmask}\n",
                encoding="utf-8",
            )
        return assignments

    def _clear_ccd(self) -> None:
        if not self.config.ccd_dir.exists():
            return
        for stale in self.config.ccd_dir.iterdir():
            stale.unlink()

    def _load_client_ips(self) -> dict[str, str]:
        path = self.config.client_ips_path
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(key): str(value) for key, value in payload.items()}

    def _save_client_ips(self, assignments: dict[str, str]) -> None:
        path = self.config.client_ips_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(assignments, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _cert_expiry(self, client_name: str) -> str | None:
        cert = self.config.pki_dir / "issued" / f"{client_name}.crt"
        if not cert.exists():
            return None
        completed = subprocess.run(
            ["openssl", "x509", "-in", str(cert), "-noout", "-enddate"],
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            return None
        return completed.stdout.strip().partition("=")[2].strip() or None

    def _run(self, *args: str, cwd: Path, cn: str | None = None, days: str | None = None) -> None:
        env = os.environ.copy()
        env["EASYRSA_BATCH"] = "1"
        env["EASYRSA_PKI"] = str(self.config.pki_dir)
        env["EASYRSA_CRL_DAYS"] = str(self.config.crl_valid_days)
        if cn:
            env["EASYRSA_REQ_CN"] = cn
        if days:
            env["EASYRSA_CERT_EXPIRE"] = days
        subprocess.run([str(EASYRSA), *args], cwd=cwd, env=env, check=True)
