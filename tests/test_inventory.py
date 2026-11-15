import contextlib
import csv
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import Mock, patch

import openvpn_addon.config as config_module
from openvpn_addon.inventory import COLUMNS, collect_inventory, render_inventory
from openvpn_addon.main import build_parser
from openvpn_addon.webui import _build_handler


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


def record(name="alice", state="V", expiry="310101000000Z", serial="01", revocation=""):
    return f"{state}\t{expiry}\t{revocation}\t{serial}\tunknown\t/CN={name}\n"


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.pki = self.root / "pki"
        self.pki.mkdir()
        self.index = self.pki / "index.txt"

    def collect(self, text):
        self.index.write_text(text, encoding="utf-8")
        return collect_inventory(self.pki, now=NOW)

    def test_inventory_keeps_certificate_history_and_excludes_server(self):
        result = self.collect(
            record("server")
            + record("bob", "E", "290101000000Z", "0b")
            + record("alice", "R", "290101000000Z", "01", "280201120000Z,keyCompromise")
            + record("alice", serial="0a")
        )
        self.assertEqual(1, result["schema_version"])
        self.assertEqual("2030-01-01T00:00:00Z", result["generated_at"])
        self.assertEqual([
            {"name": "alice", "serial": "01", "status": "revoked",
             "expires_at": "2029-01-01T00:00:00Z", "revoked_at": "2028-02-01T12:00:00Z"},
            {"name": "alice", "serial": "0A", "status": "valid",
             "expires_at": "2031-01-01T00:00:00Z", "revoked_at": None},
            {"name": "bob", "serial": "0B", "status": "expired",
             "expires_at": "2029-01-01T00:00:00Z", "revoked_at": None},
        ], result["certificates"])

    def test_expiry_is_computed_even_if_ca_has_not_marked_record_expired(self):
        for expiry in ("290101000000Z", "300101000000Z"):
            with self.subTest(expiry=expiry):
                row = self.collect(record(expiry=expiry))["certificates"][0]
                self.assertEqual("expired", row["status"])

    def test_openssl_date_centuries_and_generalized_time(self):
        for expiry, expected in (
            ("490101000000Z", "2049-01-01T00:00:00Z"),
            ("500101000000Z", "1950-01-01T00:00:00Z"),
            ("20500101000000Z", "2050-01-01T00:00:00Z"),
        ):
            with self.subTest(expiry=expiry):
                row = self.collect(record(expiry=expiry))["certificates"][0]
                self.assertEqual(expected, row["expires_at"])

    def test_missing_and_empty_index_produce_empty_inventory_without_creating_files(self):
        missing = self.root / "not-initialized"
        result = collect_inventory(missing, now=NOW)
        self.assertEqual([], result["certificates"])
        self.assertFalse(missing.exists())
        self.assertEqual([], self.collect("\n")["certificates"])
        self.assertEqual(",".join(COLUMNS) + "\n", render_inventory(result, "csv"))

    def test_invalid_records_fail_without_echoing_contents_or_returning_partial_data(self):
        for bad in (
            "sensitive malformed record\n",
            record(name="=formula"),
            record(name="../outside"),
            record(name="alice/OU=unexpected"),
            record(state="?"),
            record(serial="=123"),
            record(expiry="310230000000Z"),
            record(expiry="not-a-date"),
            record(state="R"),
            record(revocation="280101000000Z"),
        ):
            with self.subTest(record=bad):
                with self.assertRaisesRegex(ValueError, "^Invalid CA index record at line 2$"):
                    self.collect(record() + bad)

    def test_json_and_csv_have_only_documented_metadata(self):
        inventory = self.collect(record())
        self.assertEqual(inventory, json.loads(render_inventory(inventory, "json")))
        rows = list(csv.DictReader(io.StringIO(render_inventory(inventory, "csv"))))
        self.assertEqual([dict(inventory["certificates"][0], revoked_at="")], rows)
        self.assertEqual(list(COLUMNS), list(rows[0]))
        with self.assertRaises(ValueError):
            render_inventory(inventory, "zip")

    def test_reads_only_index_and_never_runs_commands_or_changes_files(self):
        self.index.write_text(record(), encoding="utf-8")
        for relative in ("pki/private/alice.key", "pki/private/ca.key", "pki/ta.key", "exports/alice.ovpn"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("PRIVATE TEST SENTINEL", encoding="utf-8")
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        original_open = Path.open
        opened = []

        def only_index(path, *args, **kwargs):
            self.assertEqual(self.index, path)
            opened.append(path)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", only_index), patch("subprocess.run", side_effect=AssertionError("subprocess")):
            output = render_inventory(collect_inventory(self.pki, now=NOW), "json")
        self.assertEqual([self.index], opened)
        self.assertNotIn("PRIVATE TEST SENTINEL", output)
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def invoke(self, *arguments):
        args = build_parser().parse_args(["client", "inventory", *arguments])
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(config_module, "DATA_ROOT", self.root), \
                patch.object(config_module, "OPTIONS_PATH", self.root / "options.json"), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = args.func(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_defaults_to_json_and_supports_csv(self):
        self.index.write_text(record(), encoding="utf-8")
        code, output, error = self.invoke()
        self.assertEqual((0, ""), (code, error))
        self.assertEqual("alice", json.loads(output)["certificates"][0]["name"])
        code, output, error = self.invoke("--format", "csv")
        self.assertEqual((0, ""), (code, error))
        self.assertEqual("alice", next(csv.DictReader(io.StringIO(output)))["name"])

    def test_cli_errors_are_nonzero_with_no_partial_output_or_sensitive_details(self):
        self.index.write_text(record() + "PRIVATE TEST SENTINEL\n", encoding="utf-8")
        code, output, error = self.invoke()
        self.assertEqual((1, ""), (code, output))
        self.assertIn("Unable to read client inventory", error)
        self.assertNotIn("PRIVATE TEST SENTINEL", error)
        with patch("openvpn_addon.main.load_config", side_effect=ValueError("PRIVATE TEST SENTINEL")):
            code, output, error = self.invoke()
        self.assertEqual((1, ""), (code, output))
        self.assertNotIn("PRIVATE TEST SENTINEL", error)

    def request(self, path):
        # Exercise actual HTTP parsing and response generation without binding a
        # socket, so these regression tests also run in restricted sandboxes.
        connection = Mock()
        connection.makefile.return_value = io.BytesIO(f"GET {path} HTTP/1.0\r\n\r\n".encode())
        response = io.BytesIO()
        connection.sendall.side_effect = response.write
        actions = Mock()
        config = config_module.AddonConfig()
        handler = _build_handler(config, actions)
        with patch.object(config_module, "DATA_ROOT", self.root), contextlib.redirect_stdout(io.StringIO()):
            handler(connection, ("127.0.0.1", 12345), Mock())
        self.assertEqual([], actions.mock_calls)
        head, body = response.getvalue().split(b"\r\n\r\n", 1)
        status_line, headers = head.split(b"\r\n", 1)
        return int(status_line.split()[1]), body.decode(), BytesParser().parsebytes(headers)

    def test_panel_inventory_downloads(self):
        self.index.write_text(record(), encoding="utf-8")
        for extension, mime in (("json", "application/json"), ("csv", "text/csv")):
            with self.subTest(extension=extension):
                status, body, headers = self.request(f"/api/clients/inventory.{extension}")
                self.assertEqual(200, status)
                self.assertIn("alice", body)
                self.assertEqual(f"{mime}; charset=utf-8", headers["Content-Type"])
                self.assertEqual("no-store", headers["Cache-Control"])
                self.assertEqual(str(len(body.encode())), headers["Content-Length"])
                self.assertEqual(f'attachment; filename="client-inventory.{extension}"', headers["Content-Disposition"])
                if extension == "json":
                    self.assertEqual("01", json.loads(body)["certificates"][0]["serial"])
                else:
                    self.assertEqual("01", next(csv.DictReader(io.StringIO(body)))["serial"])

    def test_panel_invalid_inventory_returns_sanitized_error(self):
        self.index.write_text("PRIVATE TEST SENTINEL\n", encoding="utf-8")
        for extension in ("json", "csv"):
            status, body, headers = self.request(f"/api/clients/inventory.{extension}")
            self.assertEqual(500, status)
            self.assertNotIn("PRIVATE TEST SENTINEL", body)
            self.assertEqual("error", json.loads(body)["status"])
            self.assertNotIn("Content-Disposition", headers)

    def test_panel_links_and_unsupported_export_format(self):
        status, body, _ = self.request("/")
        self.assertEqual(200, status)
        self.assertIn('data-inventory="json"', body)
        self.assertIn('data-inventory="csv"', body)
        status, _, _ = self.request("/api/clients/inventory.zip")
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()
