import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openvpn_addon import network


FAKE_IPTABLES = """#!/bin/sh
echo "$@" >> "$IPTABLES_LOG"
case "$*" in
  *" -C "*|*"-C "*) exit 1 ;;
  *"-L OPENVPN_ADDON"*) exit 1 ;;
esac
exit 0
"""

FAKE_IP = """#!/bin/sh
case "$*" in
  "route show default") echo "default via 192.168.1.1 dev enp1s0 proto dhcp"; exit 0 ;;
esac
case "$*" in
  *"-o addr show"*)
    echo "2: enp1s0    inet 192.168.1.6/24 brd 192.168.1.255 scope global enp1s0"
    exit 0 ;;
esac
exit 1
"""

FAKE_SYSCTL = """#!/bin/sh
echo "$@" >> "$SYSCTL_LOG"
exit 1
"""


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)

        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "iptables.log"
        self.sysctl_log = root / "sysctl.log"
        self._write_script("iptables", FAKE_IPTABLES)
        self._write_script("ip", FAKE_IP)
        self._write_script("sysctl", FAKE_SYSCTL)

        self.ip_forward = root / "ip_forward"
        self.ip_forward.write_text("1\n", encoding="utf-8")
        original_path = network.IP_FORWARD_PATH
        network.IP_FORWARD_PATH = self.ip_forward
        self.addCleanup(setattr, network, "IP_FORWARD_PATH", original_path)

        # Only test executables are reachable; never run host networking tools.
        self.enterContext(patch.dict(os.environ, {
            "PATH": str(self.bin),
            "IPTABLES_LOG": str(self.log),
            "SYSCTL_LOG": str(self.sysctl_log),
            "TEST_IP_FORWARD_PATH": str(self.ip_forward),
        }))

        from openvpn_addon.config import AddonConfig

        self.config = AddonConfig(server_host="192.168.1.6", local_networks=["192.168.1.0/24"])

    def _write_script(self, name, body):
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def calls(self):
        if not self.log.exists():
            return []
        return [line.strip() for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_applies_rules_through_the_dedicated_chain(self):
        result = network.apply_network(self.config)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual("enp1s0", result.out_interface)
        calls = self.calls()
        self.assertIn("-t nat -N OPENVPN_ADDON", calls)
        self.assertIn("-t nat -F OPENVPN_ADDON", calls)
        self.assertIn("-t nat -A OPENVPN_ADDON -s 10.8.0.0/24 -o enp1s0 -j MASQUERADE", calls)
        self.assertIn("-A OPENVPN_ADDON -i tun0 -s 10.8.0.0/24 -j ACCEPT", calls)
        self.assertIn("-t nat -A POSTROUTING -j OPENVPN_ADDON", calls)
        self.assertIn("-t filter -A FORWARD -j OPENVPN_ADDON", calls)
        self.assertFalse(self.sysctl_log.exists())

    def test_disabled_nat_reports_a_warning_and_touches_nothing(self):
        self.config.nat_enabled = False
        result = network.apply_network(self.config)
        self.assertFalse(result.applied)
        self.assertEqual(1, len(result.warnings))
        self.assertIn("nat_enabled", result.warnings[0])
        self.assertEqual([], self.calls())

    def test_missing_iptables_is_reported_not_raised(self):
        for entry in self.bin.iterdir():
            entry.unlink()
        os.environ["PATH"] = str(self.bin)
        result = network.apply_network(self.config)
        self.assertFalse(result.applied)
        self.assertTrue(any("iptables" in error for error in result.errors))

    def test_ip_forward_off_is_an_error_not_a_crash(self):
        self.ip_forward.write_text("0\n", encoding="utf-8")
        result = network.apply_network(self.config)
        self.assertFalse(result.ip_forward)
        self.assertTrue(any("ip_forward" in error for error in result.errors))
        self.assertEqual("-w net.ipv4.ip_forward=1\n", self.sysctl_log.read_text(encoding="utf-8"))
        self.assertEqual("0\n", self.ip_forward.read_text(encoding="utf-8"))

    def test_ip_forward_can_be_enabled_without_touching_the_host(self):
        self.ip_forward.write_text("0\n", encoding="utf-8")
        self._write_script("sysctl", '''#!/bin/sh
echo "1" > "$TEST_IP_FORWARD_PATH"
''')
        result = network.apply_network(self.config)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual("1\n", self.ip_forward.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
