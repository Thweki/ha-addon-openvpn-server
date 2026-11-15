import contextlib
import io
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openvpn_addon import bandwidth, renderer
from openvpn_addon.config import load_config
from openvpn_addon.validator import config_warnings, validate_config


ROOT = Path(__file__).resolve().parent.parent


class BandwidthConfigTests(unittest.TestCase):
    def load(self, payload):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "options.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            config = load_config(path)
        validate_config(config)
        return config

    def render(self, config):
        with patch.object(renderer, "TEMPLATES_DIR", ROOT / "addon/templates"):
            return renderer.render_server_config(config)

    def test_default_and_explicitly_disabled_configs_have_no_hook(self):
        for payload in ({}, {"experimental_bandwidth_limit": False},
                        {"experimental_bandwidth_limit": "false", "download_limit_kbps": 2000}):
            with self.subTest(payload=payload):
                config = self.load(payload)
                self.assertFalse(config.experimental_bandwidth_limit)
                output = self.render(config)
                self.assertNotIn("bandwidth.py", output)
                self.assertNotIn("script-security", output)
                self.assertNotIn("disable-dco", output)

    def test_enabled_hook_uses_validated_rate_for_both_transports(self):
        for protocol in ("udp", "tcp"):
            with self.subTest(protocol=protocol):
                config = self.load({"experimental_bandwidth_limit": True,
                                    "download_limit_kbps": "2500", "protocol": protocol})
                self.assertEqual([], config.unknown_options)
                output = self.render(config)
                self.assertIn("disable-dco\n", output)
                self.assertIn("script-security 2\n", output)
                hook = next(line for line in output.splitlines() if line.startswith("up "))
                command = shlex.split(shlex.split(hook)[1])
                self.assertEqual(["/usr/bin/python3",
                                  "/opt/openvpn_server/src/openvpn_addon/bandwidth.py", "2500"], command)
                self.assertNotIn("shaper", output)
                self.assertNotIn("up-restart", output)

    def test_rate_boundaries(self):
        for rate in (64, 1000000):
            self.load({"experimental_bandwidth_limit": True, "download_limit_kbps": rate})
        for rate in (-1, 0, 63, 1000001, "many", "1000\nup bad"):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                self.load({"experimental_bandwidth_limit": True, "download_limit_kbps": rate})

    def test_rejects_modes_that_bypass_tunnel_shaping(self):
        for options in ({"device": "tap"}, {"device": "eth"}, {"client_to_client": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.load({"experimental_bandwidth_limit": True, **options})
        self.load({"experimental_bandwidth_limit": False, "device": "tap", "client_to_client": True})

    def test_addon_options_expose_disabled_flag_and_bounded_rate(self):
        text = (ROOT / "addon/config.yaml").read_text(encoding="utf-8")
        options, schema = text.split("schema:\n")
        self.assertIn("  experimental_bandwidth_limit: false\n", options)
        self.assertIn("  download_limit_kbps: 10000\n", options)
        self.assertIn("  experimental_bandwidth_limit: bool\n", schema)
        self.assertIn("  download_limit_kbps: int(64,1000000)\n", schema)

    def test_enabled_limit_is_visible_in_configuration_warnings(self):
        config = self.load({"experimental_bandwidth_limit": True, "download_limit_kbps": 1234})
        warning = next(item for item in config_warnings(config) if "bandwidth limit" in item)
        self.assertIn("1234 kbit/s", warning)
        self.assertIn("uploads are unlimited", warning)
        self.assertIn("offload is disabled", warning)


class BandwidthHookTests(unittest.TestCase):
    def invoke(self, rate="10000", device="tun0", script_type="up"):
        argv = ["bandwidth.py", rate, device, "1500", "0", "10.8.0.1", "255.255.255.0", "init"]
        with patch.object(bandwidth.sys, "argv", argv), patch.dict(
            os.environ, {"dev": device, "script_type": script_type}, clear=True
        ), contextlib.redirect_stdout(io.StringIO()):
            return bandwidth.main()

    @patch.object(bandwidth.subprocess, "run")
    def test_hook_adds_only_tunnel_egress_shaper(self, run):
        self.assertEqual(0, self.invoke())
        run.assert_called_once_with(
            ["/sbin/tc", "qdisc", "add", "dev", "tun0", "root", "tbf",
             "rate", "10000kbit", "burst", "12500", "latency", "100ms"],
            check=True, capture_output=True, timeout=10,
        )

    @patch.object(bandwidth.subprocess, "run")
    def test_minimum_rate_can_accommodate_full_tunnel_packets(self, run):
        self.assertEqual(0, self.invoke(rate="64"))
        command = run.call_args.args[0]
        self.assertEqual("1600", command[command.index("burst") + 1])

    @patch.object(bandwidth.subprocess, "run")
    def test_bad_rate_or_hook_context_does_not_run_tc(self, run):
        for options in ({"rate": "0"}, {"rate": "1000001"}, {"rate": "1; bad"},
                        {"device": "eth0"}, {"device": "tun1"}, {"script_type": "down"}):
            with self.subTest(options=options), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1, self.invoke(**options))
        run.assert_not_called()

    @patch.object(bandwidth.subprocess, "run")
    def test_hook_requires_matching_device_environment(self, run):
        for env in ({}, {"dev": "eth0", "script_type": "up"}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), patch.object(
                bandwidth.sys, "argv", ["bandwidth.py", "10000", "tun0"]
            ), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1, bandwidth.main())
        run.assert_not_called()

    @patch.object(bandwidth.subprocess, "run")
    def test_setup_failure_is_fatal_and_never_replaces_existing_qdisc(self, run):
        for error in (FileNotFoundError(), PermissionError(),
                      subprocess.CalledProcessError(2, "tc", stderr=b"private diagnostic"),
                      subprocess.TimeoutExpired("tc", 10)):
            with self.subTest(error=type(error).__name__):
                run.reset_mock()
                run.side_effect = error
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(1, self.invoke())
                self.assertIn("Bandwidth limit setup failed", stderr.getvalue())
                self.assertNotIn("private diagnostic", stderr.getvalue())
                self.assertEqual(1, run.call_count)
                self.assertNotIn("replace", run.call_args.args[0])
                self.assertNotIn("del", run.call_args.args[0])

    def test_standalone_hook_needs_no_pythonpath_and_rejects_missing_arguments(self):
        result = subprocess.run(
            ["/usr/bin/python3", str(ROOT / "addon/src/openvpn_addon/bandwidth.py")],
            env={}, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("Bandwidth limit setup failed", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
