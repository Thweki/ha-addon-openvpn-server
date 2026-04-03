from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .config import AddonConfig
from .validator import validate_client_name


EASYRSA = Path("/usr/share/easy-rsa/easyrsa")


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

        self._run("gen-crl", cwd=self.config.pki_dir.parent)

    def ensure_client(self, client_name: str) -> None:
        validate_client_name(client_name)
        cert = self.config.pki_dir / "issued" / f"{client_name}.crt"
        if cert.exists():
            return
        self._run(
            "build-client-full",
            client_name,
            "nopass",
            cwd=self.config.pki_dir.parent,
            days=str(self.config.client_expire_days),
        )

    def revoke_client(self, client_name: str) -> None:
        validate_client_name(client_name)
        cert = self.config.pki_dir / "issued" / f"{client_name}.crt"
        if not cert.exists():
            raise FileNotFoundError(f"Client certificate not found: {client_name}")
        self._run("revoke", client_name, cwd=self.config.pki_dir.parent)
        self._run("gen-crl", cwd=self.config.pki_dir.parent)

    def list_clients(self) -> list[str]:
        issued_dir = self.config.pki_dir / "issued"
        if not issued_dir.exists():
            return []
        result = []
        for item in issued_dir.glob("*.crt"):
            if item.name == "server.crt":
                continue
            result.append(item.stem)
        return sorted(result)

    def _run(self, *args: str, cwd: Path, cn: str | None = None, days: str | None = None) -> None:
        env = os.environ.copy()
        env["EASYRSA_BATCH"] = "1"
        env["EASYRSA_PKI"] = str(self.config.pki_dir)
        if cn:
            env["EASYRSA_REQ_CN"] = cn
        if days:
            env["EASYRSA_CERT_EXPIRE"] = days
        subprocess.run([str(EASYRSA), *args], cwd=cwd, env=env, check=True)
