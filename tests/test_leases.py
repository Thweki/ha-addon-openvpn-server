import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openvpn_addon.config import AddonConfig
from openvpn_addon.main import build_parser
from openvpn_addon.leases import collect_leases


CLIENT_HEADER = "Common Name,Real Address,Bytes Received,Bytes Sent,Connected Since\n"
ROUTE_HEADER = "Virtual Address,Common Name,Real Address,Last Ref\n"


class LeaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.root_patch = patch("openvpn_addon.config.DATA_ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.config = AddonConfig()
        self.config.runtime_dir.mkdir()

    def status(self, clients="", routes="", complete=True):
        self.config.status_path.write_text(
            "OpenVPN CLIENT LIST\nUpdated,Wed Oct 14 08:00:00 2026\n"
            + CLIENT_HEADER + clients + "ROUTING TABLE\n" + ROUTE_HEADER + routes
            + ("GLOBAL STATS\nMax bcast/mcast queue length,0\nEND\n" if complete else ""),
            encoding="utf-8",
        )
        os.utime(self.config.status_path, (1000, 1000))

    def fixed(self, assignments):
        self.config.client_ips_path.write_text(json.dumps(assignments), encoding="utf-8")

    def collect(self, now=1001):
        return collect_leases(self.config, now=now)

    def test_joins_routing_table_without_treating_byte_counts_as_addresses(self):
        self.status(
            "alice,203.0.113.1:1234,100,200,Wed Oct 14 07:00:00 2026\n",
            "10.8.0.2,alice,203.0.113.1:1234,Wed Oct 14 08:00:00 2026\n",
        )
        row = self.collect()["leases"][0]
        self.assertEqual({
            "name": "alice", "vpn_address": "10.8.0.2", "source": "session",
            "real_address": "203.0.113.1:1234", "connected_since": "Wed Oct 14 07:00:00 2026",
        }, row)

    def test_shared_certificate_sessions_are_not_collapsed(self):
        self.config.duplicate_cn = True
        self.fixed({"alice": "10.8.0.99"})
        self.status(
            "alice,203.0.113.1:1234,100,200,today\nalice,203.0.113.1:5678,300,400,today\n",
            "10.8.0.2,alice,203.0.113.1:1234,today\n10.8.0.3,alice,203.0.113.1:5678,today\n",
        )
        result = self.collect()
        self.assertEqual(["10.8.0.2", "10.8.0.3"], [row["vpn_address"] for row in result["leases"]])
        self.assertEqual("disabled", result["fixed_assignments"])

    def test_saved_assignments_are_distinct_from_observed_addresses(self):
        self.fixed({"bob": "10.8.0.3", "alice": "10.8.0.2", "carol": "10.8.0.4"})
        self.status(
            "alice,203.0.113.1:1234,100,200,today\ncarol,203.0.113.2:1234,100,200,today\n",
            "10.8.0.2,alice,203.0.113.1:1234,today\n10.8.0.5,carol,203.0.113.2:1234,today\n",
        )
        rows = self.collect()["leases"]
        self.assertEqual([
            ("alice", "10.8.0.2", "session"), ("bob", "10.8.0.3", "fixed"),
            ("carol", "10.8.0.4", "fixed"), ("carol", "10.8.0.5", "session"),
        ], [(row["name"], row["vpn_address"], row["source"]) for row in rows])
        self.assertIsNone(rows[1]["real_address"])

    def test_missing_sources_are_explicit_and_create_nothing(self):
        result = self.collect()
        self.assertEqual("missing", result["status"]["state"])
        self.assertEqual("missing", result["fixed_assignments"])
        self.assertEqual([], result["leases"])
        self.assertEqual([], list(self.config.runtime_dir.iterdir()))

    def test_empty_complete_snapshot_is_fresh(self):
        self.status()
        self.assertEqual({"state": "fresh", "age_seconds": 1}, self.collect()["status"])
        self.assertEqual([], self.collect()["leases"])

    def test_stale_snapshot_is_labelled_and_retained(self):
        self.status("alice,203.0.113.1:1234,100,200,today\n", "10.8.0.2,alice,203.0.113.1:1234,today\n")
        result = self.collect(now=1061)
        self.assertEqual({"state": "stale", "age_seconds": 61}, result["status"])
        self.assertEqual("session", result["leases"][0]["source"])

    def test_incomplete_snapshot_does_not_claim_sessions(self):
        self.fixed({"alice": "10.8.0.2"})
        self.status("alice,203.0.113.1:1234,100,200,today\n", complete=False)
        result = self.collect()
        self.assertEqual("invalid", result["status"]["state"])
        self.assertEqual(["fixed"], [row["source"] for row in result["leases"]])

    def test_session_without_route_has_unknown_address(self):
        self.status("alice,203.0.113.1:1234,100,200,today\n")
        self.assertIsNone(self.collect()["leases"][0]["vpn_address"])

    def test_routes_must_match_session_and_be_individual_ip_addresses(self):
        self.status(
            "alice,203.0.113.1:1234,100,200,today\n",
            "10.8.0.8,mallory,203.0.113.1:1234,today\n"
            "10.8.0.9,alice,203.0.113.1:9999,today\n"
            "192.168.1.0/24,alice,203.0.113.1:1234,today\n"
            "not-an-address,alice,203.0.113.1:1234,today\n"
            "10.8.0.2,alice,203.0.113.1:1234,today\n",
        )
        self.assertEqual(["10.8.0.2"], [row["vpn_address"] for row in self.collect()["leases"]])

    def test_malformed_client_rows_invalidate_snapshot(self):
        self.status("alice,203.0.113.1:1234\n")
        self.assertEqual("invalid", self.collect()["status"]["state"])

    def test_invalid_assignment_entries_are_omitted_with_warning(self):
        self.fixed({"alice": "10.8.0.2", "bob": 123, "carol": "garbage"})
        result = self.collect()
        self.assertEqual("partial", result["fixed_assignments"])
        self.assertEqual(["alice"], [row["name"] for row in result["leases"]])

    def test_invalid_assignment_files_are_reported_without_echoing_contents(self):
        for contents in ("{broken", "[]", "\ufffd"):
            with self.subTest(contents=contents):
                self.config.client_ips_path.write_text(contents, encoding="utf-8")
                result = self.collect()
                self.assertEqual("invalid", result["fixed_assignments"])
                self.assertEqual([], result["leases"])

    def test_disabled_fixed_mode_ignores_retained_map(self):
        self.config.fixed_client_ips = False
        self.fixed({"alice": "10.8.0.2"})
        self.assertEqual([], self.collect()["leases"])
        self.assertEqual("disabled", self.collect()["fixed_assignments"])

    def test_unreadable_sources_are_reported(self):
        with patch.object(Path, "open", side_effect=PermissionError):
            result = self.collect()
        self.assertEqual("unreadable", result["status"]["state"])
        self.assertEqual("unreadable", result["fixed_assignments"])

    def test_ipv6_routes_and_duplicate_rows(self):
        self.status(
            "alice,[2001:db8::1]:1234,100,200,today\n",
            "fd00::2,alice,[2001:db8::1]:1234,today\n"
            "fd00::2,alice,[2001:db8::1]:1234,today\n"
            "10.8.0.2,alice,[2001:db8::1]:1234,today\n",
        )
        self.assertEqual(["10.8.0.2", "fd00::2"], [row["vpn_address"] for row in self.collect()["leases"]])

    def test_versions_two_and_three_use_named_columns_and_preserve_sessions(self):
        for delimiter in (",", "\t"):
            with self.subTest(delimiter=delimiter):
                lines = [
                    ["TITLE", "OpenVPN 2.6"],
                    ["TIME", "today", "1000"],
                    ["HEADER", "CLIENT_LIST", "Real Address", "Bytes Sent", "Virtual IPv6 Address",
                     "Common Name", "Connected Since", "Virtual Address", "Future Column"],
                    ["CLIENT_LIST", "203.0.113.1:1234", "200", "fd00::2", "alice", "today", "10.8.0.2", "extra"],
                    ["CLIENT_LIST", "203.0.113.1:5678", "400", "", "alice", "today", "10.8.0.3", "extra"],
                    ["END"],
                ]
                self.config.status_path.write_text("\n".join(delimiter.join(row) for row in lines) + "\n")
                result = self.collect()
                self.assertEqual("fresh", result["status"]["state"])
                self.assertEqual(["10.8.0.2", "10.8.0.3", "fd00::2"],
                                 [row["vpn_address"] for row in result["leases"]])
                self.assertEqual("203.0.113.1:5678", result["leases"][1]["real_address"])

    def test_version_two_missing_header_or_short_rows_are_invalid(self):
        for contents in (
            "CLIENT_LIST,alice,203.0.113.1:1234,10.8.0.2,today\nEND\n",
            "HEADER,CLIENT_LIST,Common Name,Real Address,Connected Since\nEND\n",
            "HEADER,CLIENT_LIST,Common Name,Real Address,Virtual Address,Connected Since\nCLIENT_LIST,alice\nEND\n",
        ):
            with self.subTest(contents=contents):
                self.config.status_path.write_text(contents)
                self.assertEqual("invalid", self.collect()["status"]["state"])

    def test_unsupported_or_invalid_encoding_status_is_not_empty_success(self):
        for contents in (b"TITLE,OpenVPN 2.6\nEND\n", b"\xff"):
            with self.subTest(contents=contents):
                self.config.status_path.write_bytes(contents)
                result = self.collect()
                self.assertEqual("invalid", result["status"]["state"])
                self.assertEqual([], result["leases"])

    def test_each_collection_reads_a_new_snapshot(self):
        self.status()
        self.assertEqual([], self.collect()["leases"])
        self.status("alice,203.0.113.1:1234,100,200,today\n", "10.8.0.2,alice,203.0.113.1:1234,today\n")
        self.assertEqual(1, len(self.collect()["leases"]))

    def test_change_during_read_is_reported_as_invalid(self):
        self.status()
        before = self.config.status_path.stat()
        self.status("alice,203.0.113.1:1234,100,200,today\n")
        after = self.config.status_path.stat()
        with patch("openvpn_addon.leases.os.fstat", side_effect=[before, after]):
            self.assertEqual("invalid", self.collect()["status"]["state"])

    def test_cli_reads_only_status_and_address_map_without_mutation(self):
        self.status()
        self.fixed({"alice": "10.8.0.2"})
        allowed = {self.config.status_path, self.config.client_ips_path}
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in allowed}
        original_open = Path.open

        def read_only(path, mode="r", *args, **kwargs):
            self.assertIn(path, allowed)
            self.assertEqual("r", mode)
            return original_open(path, mode, *args, **kwargs)

        output = io.StringIO()
        with patch("openvpn_addon.main.load_and_validate", return_value=self.config), \
             patch("openvpn_addon.main.PkiManager", side_effect=AssertionError("PKI accessed")), \
             patch("subprocess.run", side_effect=AssertionError("subprocess called")), \
             patch.object(Path, "open", read_only), contextlib.redirect_stdout(output):
            args = build_parser().parse_args(["client", "leases"])
            self.assertEqual(0, args.func(args))
        self.assertEqual("alice", json.loads(output.getvalue())["leases"][0]["name"])
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in allowed})


if __name__ == "__main__":
    unittest.main()
