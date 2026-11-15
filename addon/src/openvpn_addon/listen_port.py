from __future__ import annotations

import errno
import random
import socket

from .config import AddonConfig


MIN_RANDOM_PORT = 49152
MAX_RANDOM_PORT = 65535
PORT_ATTEMPTS = 64


def select_listen_port(config: AddonConfig) -> int:
    """Probe random high ports once per add-on run, never per child restart.

    OpenVPN opens its own socket, so this is an availability check, not a
    reservation. A competing process can still claim the port after we close it.
    """
    if not config.experimental_random_port:
        return config.listen_port

    kind = socket.SOCK_DGRAM if config.protocol == "udp" else socket.SOCK_STREAM
    candidates = random.SystemRandom().sample(
        range(MIN_RANDOM_PORT, MAX_RANDOM_PORT + 1), PORT_ATTEMPTS
    )
    for port in candidates:
        with socket.socket(socket.AF_INET, kind) as probe:
            try:
                # The shipped server template listens on all IPv4 interfaces.
                probe.bind(("0.0.0.0", port))
            except OSError as error:
                if error.errno == errno.EADDRINUSE:
                    continue
                raise
        return port
    raise RuntimeError("No available experimental listen port found after 64 probes")


def recorded_listen_port(config: AddonConfig) -> int:
    """Read the last rendered listener for CLI status without choosing a port."""
    if not config.experimental_random_port or not config.server_conf_path.exists():
        return config.listen_port
    with config.server_conf_path.open(encoding="utf-8") as source:
        for line in source:
            parts = line.split()
            if len(parts) == 2 and parts[0] == "port":
                port = int(parts[1])
                if 1 <= port <= 65535:
                    return port
                break
    raise ValueError("Rendered server configuration has no valid listen port")
