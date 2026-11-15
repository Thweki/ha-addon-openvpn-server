import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from openvpn_addon.config import AddonConfig
from openvpn_addon.metrics import collect_metrics, prometheus_metrics


HEADER = "Common Name,Real Address,Bytes Received,Bytes Sent,Connected Since"
ROWS = (
    "private-client,203.0.113.5:51234,100,200,Wed Oct 14 07:00:00 2026\n"
    "private-client,203.0.113.6:51235,300,400,Wed Oct 14 07:01:00 2026\n"
    "other-client,203.0.113.7:51236,500,600,Wed Oct 14 07:02:00 2026\n"
)


def status_v1(rows=ROWS):
    return (
        "OpenVPN CLIENT LIST\nUpdated,Wed Oct 14 08:00:00 2026\n"
        f"{HEADER}\n{rows}ROUTING TABLE\n"
        "Virtual Address,Common Name,Real Address,Last Ref\n"
        "10.8.0.2,private-client,203.0.113.5:51234,Wed Oct 14 08:00:00 2026\n"
        "GLOBAL STATS\nMax bcast/mcast queue length,0\nEND\n"
    )


class MetricsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        patcher = patch("openvpn_addon.config.DATA_ROOT", self.root / "data")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = AddonConfig()
        self.config.runtime_dir.mkdir(parents=True)
        self.now = 1800000000

    def write_status(self, text, age=0):
        self.config.status_path.write_text(text, encoding="utf-8")
        os.utime(self.config.status_path, (self.now - age, self.now - age))

    def collect(self):
        with patch("openvpn_addon.metrics.time.time", return_value=self.now):
            return collect_metrics(self.config)

    def assert_unavailable(self, metrics):
        for key in ("connected_clients", "connected_sessions", "session_received_bytes", "session_sent_bytes"):
            self.assertIsNone(metrics[key], key)
            self.assertNotIn(f"openvpn_addon_{key}", prometheus_metrics(metrics))

    def test_v1_counts_sessions_separately_from_identities_and_sums_bytes(self):
        self.write_status(status_v1())
        self.assertEqual({
            "status_readable": 1, "status_valid": 1, "status_fresh": 1,
            "status_age_seconds": 0, "connected_sessions": 3, "connected_clients": 2,
            "session_received_bytes": 900, "session_sent_bytes": 1200,
        }, self.collect())

    def test_v2_and_v3_use_named_columns_and_keep_large_integers(self):
        for separator in (",", "\t"):
            with self.subTest(separator=separator):
                self.write_status((
                    "TITLE,OpenVPN 2.6\nTIME,Wed Oct 14 08:00:00 2026,1791964800\n"
                    "HEADER,CLIENT_LIST,Bytes Sent,Common Name,Virtual IPv6 Address,Bytes Received,Client ID\n"
                    "CLIENT_LIST,9007199254740993,private-client,,100,1\n"
                    "CLIENT_LIST,7,private-client,,200,2\n"
                    "HEADER,ROUTING_TABLE,Virtual Address,Common Name\n"
                    "ROUTING_TABLE,10.8.0.2,private-client\nEND\n"
                ).replace(",", separator))
                metrics = self.collect()
                self.assertEqual(2, metrics["connected_sessions"])
                self.assertEqual(1, metrics["connected_clients"])
                self.assertEqual(300, metrics["session_received_bytes"])
                self.assertEqual(9007199254741000, metrics["session_sent_bytes"])

    def test_empty_complete_snapshot_is_zero_usage(self):
        for text in (status_v1(""), f"HEADER,CLIENT_LIST,{HEADER}\nEND\n"):
            with self.subTest(text=text):
                self.write_status(text)
                metrics = self.collect()
                self.assertEqual(1, metrics["status_fresh"])
                for key in ("connected_clients", "connected_sessions", "session_received_bytes", "session_sent_bytes"):
                    self.assertEqual(0, metrics[key])

    def test_missing_file_does_not_create_state_or_report_zero_usage(self):
        before = list(self.root.rglob("*"))
        metrics = self.collect()
        self.assertEqual(0, metrics["status_readable"])
        self.assertIsNone(metrics["status_age_seconds"])
        self.assert_unavailable(metrics)
        self.assertEqual(before, list(self.root.rglob("*")))

    def test_unreadable_file_is_unavailable_without_error_details(self):
        with patch.object(Path, "open", side_effect=PermissionError("private-path")):
            metrics = self.collect()
        self.assertEqual(0, metrics["status_readable"])
        self.assert_unavailable(metrics)
        self.assertNotIn("private-path", json.dumps(metrics))

    def test_invalid_encoding_is_unavailable(self):
        self.config.status_path.write_bytes(b"\xff\xfe")
        self.assert_unavailable(self.collect())

    def test_partial_and_malformed_snapshots_are_unavailable(self):
        invalid = [
            "", "unrecognized\nEND\n", status_v1().replace("END\n", ""),
            status_v1().replace("100,200", "-1,200"),
            status_v1().replace("100,200", "nope,200"),
            status_v1().replace("100,200", "1.5,200"),
            status_v1().replace("100,200", "100"),
            status_v1().replace("Bytes Received", "Unknown"),
            status_v1().replace("Bytes Sent", "Bytes Received"),
            "CLIENT_LIST,private-client,100,200\nEND\n",
            f"HEADER,CLIENT_LIST,{HEADER}\nCLIENT_LIST,short-row\nEND\n",
            f"{HEADER}\n{HEADER}\nEND\n",
        ]
        for text in invalid:
            with self.subTest(text=text):
                self.write_status(text)
                metrics = self.collect()
                self.assertEqual(1, metrics["status_readable"])
                self.assertEqual(0, metrics["status_valid"])
                self.assert_unavailable(metrics)

    def test_freshness_boundary_stale_and_future_files(self):
        for interval, age, fresh in ((30, 60, 1), (30, 61, 0), (90, 180, 1), (90, 181, 0), (30, -1, 0)):
            with self.subTest(interval=interval, age=age):
                self.config.status_interval = interval
                self.write_status(status_v1(), age)
                metrics = self.collect()
                self.assertEqual(1, metrics["status_valid"])
                self.assertEqual(fresh, metrics["status_fresh"])
                self.assertEqual(max(0, age), metrics["status_age_seconds"])
                if not fresh:
                    self.assert_unavailable(metrics)

    def test_file_changed_during_read_is_unavailable(self):
        self.write_status(status_v1())
        old_stat = self.config.status_path.stat()
        self.write_status(status_v1(""))
        new_stat = self.config.status_path.stat()
        with patch("openvpn_addon.metrics.os.fstat", side_effect=[old_stat, new_stat]):
            metrics = self.collect()
        self.assertEqual(0, metrics["status_valid"])
        self.assert_unavailable(metrics)

    def test_prometheus_has_only_aggregate_gauges_and_no_labels(self):
        self.write_status(status_v1())
        metrics = self.collect()
        output = prometheus_metrics(metrics)
        for key, value in metrics.items():
            self.assertIn(f"# HELP openvpn_addon_{key} ", output)
            self.assertIn(f"# TYPE openvpn_addon_{key} gauge\n", output)
            self.assertIn(f"openvpn_addon_{key} {value}\n", output)
        for private in ("private-client", "other-client", "203.0.113", "10.8.0.2"):
            self.assertNotIn(private, output + json.dumps(metrics))
        self.assertNotIn("{", output)
        self.assertTrue(output.endswith("\n"))

    def run_cli(self, *args):
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "addon/src"),
                   OPENVPN_ADDON_DATA=str(self.root / "data"),
                   OPENVPN_ADDON_SHARE=str(self.root / "share"),
                   OPENVPN_ADDON_OPTIONS=str(self.root / "options.json"),
                   PYTHONDONTWRITEBYTECODE="1", PATH="")
        return subprocess.run([sys.executable, "-m", "openvpn_addon.main", "metrics", *args],
                              env=env, capture_output=True, text=True, check=False)

    def test_cli_formats_are_read_only_without_external_tools(self):
        self.now = time.time()
        self.write_status(status_v1())
        (self.root / "options.json").write_text('{"public_endpoint":"private-endpoint.example"}')
        self.config.pki_dir.mkdir()
        (self.config.pki_dir / "private.key").write_text("synthetic-private-key-sentinel")

        def snapshot():
            return {str(p): (p.read_bytes() if p.is_file() else None, p.stat().st_mode, p.stat().st_mtime_ns)
                    for p in self.root.rglob("*")}

        before = snapshot()
        for args in ((), ("--format", "json"), ("--format", "prometheus")):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual("", result.stderr)
                if "prometheus" in args:
                    self.assertIn("openvpn_addon_connected_sessions 3\n", result.stdout)
                else:
                    self.assertEqual(3, json.loads(result.stdout)["connected_sessions"])
                for private in ("private-client", "203.0.113", "private-endpoint", "synthetic-private-key"):
                    self.assertNotIn(private, result.stdout)
                self.assertEqual(before, snapshot())

    def test_cli_missing_snapshot_still_emits_machine_readable_result(self):
        result = self.run_cli()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assert_unavailable(json.loads(result.stdout))

    def test_cli_invalid_settings_do_not_echo_values(self):
        (self.root / "options.json").write_text('{"protocol":"synthetic-private-value"}')
        result = self.run_cli()
        self.assertEqual(1, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual("Unable to load valid add-on settings for metrics.\n", result.stderr)

    def test_cli_rejects_unknown_format(self):
        result = self.run_cli("--format", "xml")
        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stdout)


if __name__ == "__main__":
    unittest.main()
