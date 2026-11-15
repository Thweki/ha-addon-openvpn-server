"""Experimental aggregate download shaping, invoked by OpenVPN's up hook.

Run as a standalone file: OpenVPN script environments need not have PYTHONPATH.
The qdisc belongs to the TUN device and disappears when OpenVPN closes it. With
persist-tun, both device and qdisc survive soft restarts (no up-restart hook).
"""
from __future__ import annotations

import os
import subprocess
import sys


def validate_download_rate(rate: int) -> None:
    if type(rate) is not int or not 64 <= rate <= 1000000:
        raise ValueError("download_limit_kbps must be an integer from 64 to 1000000")


def main() -> int:
    try:
        rate = int(sys.argv[1])
        validate_download_rate(rate)
        # Never apply a qdisc to a host uplink, even with a malformed hook call.
        if sys.argv[2] != "tun0" or os.environ.get("dev") != "tun0" or os.environ.get("script_type") != "up":
            raise ValueError("Expected the tun0 up hook")
        # Allow one full packet or 10 ms of tokens (also accommodates HZ=100).
        burst = max(1600, rate * 1000 // 8 // 100)
        subprocess.run(
            ["/sbin/tc", "qdisc", "add", "dev", "tun0", "root", "tbf",
             "rate", f"{rate}kbit", "burst", str(burst), "latency", "100ms"],
            check=True, capture_output=True, timeout=10,
        )
    except (IndexError, ValueError, OSError, subprocess.SubprocessError):
        # Do not replace another owner's qdisc or silently continue uncapped.
        print(
            "Bandwidth limit setup failed: expected a tun0 up hook, a valid rate, "
            "and tc/TBF support with NET_ADMIN and no existing root qdisc. "
            "Disable experimental_bandwidth_limit and restart to recover.",
            file=sys.stderr,
        )
        return 1
    print(f"Experimental bandwidth limit installed: aggregate downloads {rate} kbit/s on tun0.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
