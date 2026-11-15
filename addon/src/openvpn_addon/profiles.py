from __future__ import annotations

import os
import zipfile
from pathlib import Path

from .config import AddonConfig
from .pki import PkiManager, STATUS_VALID
from .renderer import render_client_config
from .validator import validate_client_name


def bundle_profiles(config: AddonConfig, names: list[str], output: Path) -> None:
    """Re-render selected clients into a new private ZIP without changing PKI or exports."""
    if not names or len(set(names)) != len(names):
        raise ValueError("Select one or more distinct client names")
    if config.public_address == "0.0.0.0":
        raise ValueError("Set public_endpoint or a concrete server_host before bundling profiles")

    pki = PkiManager(config)
    for name in names:
        validate_client_name(name)
        if name == "server" or name != name.strip():
            raise ValueError("Bundle entries must be client identities with no surrounding whitespace")
        if pki.client_status(name) != STATUS_VALID:
            raise ValueError(f"Client is not eligible for export: {name}")

    # Finish rendering before creating the archive: missing material must not leave a partial bundle.
    profiles = [(name, render_client_config(config, name)) for name in names]
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, profile in profiles:
                    entry = zipfile.ZipInfo(f"{name}.ovpn")
                    entry.create_system = 3
                    entry.external_attr = (0o100600 << 16)
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(entry, profile.encode("utf-8"))
    except BaseException:
        output.unlink()
        raise
