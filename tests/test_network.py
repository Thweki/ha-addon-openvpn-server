import io
import os
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openvpn_addon import health, main, network


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
        self._write_script("iptables", FAKE_IPTABLES)
        self._write_script("ip", FAKE_IP)
        self._write_script("sysctl", FAKE_SYSCTL)

        self.ip_forward = root / "ip_forward"
        self.ip_forward.write_text("1\n", encoding="utf-8")
        original_path = network.IP_FORWARD_PATH
        network.IP_FORWARD_PATH = self.ip_forward
        self.addCleanup(setattr, network, "IP_FORWARD_PATH", original_path)

        # Use only fake commands, and restore every environment variable we change.
        self.enterContext(patch.dict(os.environ, {
            "PATH": str(self.bin),
            "IPTABLES_LOG": str(self.log),
            "TEST_IP_FORWARD": str(self.ip_forward),
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

    def forwarding_health(self):
        pki = Mock()
        pki.crl_info.return_value = SimpleNamespace(next_update="2030", days_remaining=365)
        return {check.name: check for check in health.collect_health(self.config, pki)}

    @contextmanager
    def mock_commands(self, fail=lambda command: False, missing=None):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if command[0] == missing:
                raise FileNotFoundError(2, "mock executable missing", missing)
            code = 1 if fail(command) else 0
            output = ""
            if command[0] == "ip":
                output = "2: enp1s0 inet 192.168.1.6/24\n" if "addr" in command else "default dev enp1s0\n"
            return subprocess.CompletedProcess(command, code, output, "mock command failed" if code else "")

        with patch.object(network.subprocess, "run", side_effect=run):
            yield calls

    def test_missing_return_rule_is_not_healthy(self):
        with self.mock_commands(lambda command: "-o" in command and "ACCEPT" in command) as calls:
            check = self.forwarding_health()["lan_forwarding"]
            self.assertEqual(health.ERROR, check.status)
            self.assertIn("return ACCEPT", check.detail)
            result = network.verify_network(self.config)
            self.assertFalse(result.ok)
            self.assertFalse(result.applied)
            self.assertIn("tun0 return ACCEPT rule", result.missing_rules)
        self.assertTrue(any("conntrack" in command for command in calls))
        self.assertTrue(any("state" in command for command in calls))
        self.assertTrue(all("-C" in command for command in calls if command[0] == "iptables"))

    def test_verifies_conntrack_or_legacy_state_return_rule(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), self.mock_commands(lambda command: legacy and "conntrack" in command) as calls:
                self.assertTrue(network.verify_network(self.config).ok)
                self.assertEqual(health.OK, self.forwarding_health()["lan_forwarding"].status)
                self.assertTrue(any("conntrack" in command for command in calls))
                self.assertEqual(legacy, any("state" in command for command in calls))

    def test_missing_outbound_rules_are_not_healthy(self):
        for failed in ("POSTROUTING", "MASQUERADE", "FORWARD", "-i"):
            with self.subTest(rule=failed), self.mock_commands(lambda command: command[0] == "iptables" and failed in command):
                self.assertFalse(network.verify_network(self.config).ok)
                self.assertEqual(health.ERROR, self.forwarding_health()["lan_forwarding"].status)

    def test_apply_uses_legacy_state_when_conntrack_fails(self):
        with self.mock_commands(lambda command: "conntrack" in command) as calls:
            self.assertTrue(network.apply_network(self.config).ok)
        self.assertTrue(any("state" in command for command in calls))

    def test_rule_setup_nonzero_exits_are_reported(self):
        for failed in ("-N", "-F", "MASQUERADE", "ACCEPT", "POSTROUTING", "FORWARD"):
            with self.subTest(rule=failed), self.mock_commands(
                lambda command: command[0] == "iptables" and
                (failed in command or "-L" in command or "-C" in command)
            ):
                result = network.apply_network(self.config)
                self.assertFalse(result.ok)
                self.assertTrue(any("mock command failed" in error for error in result.errors))

    def test_missing_executables_during_launch_are_reported(self):
        for executable in ("iptables", "ip", "sysctl"):
            with self.subTest(executable=executable):
                self.ip_forward.write_text("0\n" if executable == "sysctl" else "1\n", encoding="utf-8")
                with self.mock_commands(missing=executable):
                    result = network.apply_network(self.config)
                    self.assertFalse(result.ok)
                    self.assertTrue(any(executable in error for error in result.errors))
                    if executable != "sysctl":
                        result = network.verify_network(self.config)
                        self.assertFalse(result.ok)
                        self.assertTrue(any(executable in error for error in result.errors))
                        check = self.forwarding_health()["lan_forwarding"]
                        self.assertEqual(health.ERROR, check.status)
                        self.assertIn(executable, check.detail)

    def test_missing_iptables_is_reported_by_verification_and_health(self):
        (self.bin / "iptables").unlink()
        self.assertFalse(network.verify_network(self.config).ok)
        check = self.forwarding_health()["lan_forwarding"]
        self.assertEqual(health.ERROR, check.status)
        self.assertIn("iptables", check.detail)

    def test_successful_forwarding_enablement(self):
        self.ip_forward.write_text("0\n", encoding="utf-8")
        self._write_script("sysctl", '#!/bin/sh\nprintf "1\\n" > "$TEST_IP_FORWARD"\n')
        self.assertTrue(network.apply_network(self.config).ok)
        self.assertEqual("1\n", self.ip_forward.read_text(encoding="utf-8"))

    def test_sysctl_success_without_enabled_forwarding_is_an_error(self):
        self.ip_forward.write_text("0\n", encoding="utf-8")
        self._write_script("sysctl", "#!/bin/sh\nexit 0\n")
        result = network.apply_network(self.config)
        self.assertFalse(result.ok)
        self.assertTrue(any("ip_forward" in error for error in result.errors))
        self.assertEqual(health.ERROR, self.forwarding_health()["ip_forward"].status)

    def test_network_launch_failure_does_not_prevent_panel_startup(self):
        self.enterContext(patch("openvpn_addon.config.DATA_ROOT", self.ip_forward.parent))
        config = self.config
        config.runtime_dir.mkdir()
        supervisor = Mock()
        panel = Mock()
        output = io.StringIO()
        with patch.multiple(main, load_and_validate=Mock(return_value=config), ensure_dirs=Mock(),
                            PkiManager=Mock(), sync_client_ips=Mock(), render_server_config=Mock(return_value=""),
                            log_routing_intent=Mock(), export_default_client=Mock(), process_pending_requests=Mock(),
                            OpenVpnSupervisor=Mock(return_value=supervisor), RequestWatcher=Mock(), remove_network=Mock()), \
                patch.object(main, "start_panel", return_value=[panel]) as start_panel, \
                patch.object(main.signal, "signal"), \
                self.mock_commands(missing="iptables"), \
                redirect_stdout(output):
            self.assertEqual(0, main.cmd_run(None))
        start_panel.assert_called_once()
        supervisor.supervise_forever.assert_called_once()
        self.assertIn("ERROR", output.getvalue())
        self.assertIn("iptables", output.getvalue())


if __name__ == "__main__":
    unittest.main()
