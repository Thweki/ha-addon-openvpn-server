import json
import tempfile
import unittest
from pathlib import Path

from openvpn_addon.requests import archive_request, reject_request, scan_client_requests, write_result


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.requests_dir = self.root / "requests"
        self.requests_dir.mkdir()

    def write_request(self, name, payload):
        (self.requests_dir / name).write_text(
            payload if isinstance(payload, str) else json.dumps(payload),
            encoding="utf-8",
        )

    def test_accepts_issue_and_revoke(self):
        self.write_request("a.json", {"action": "issue", "client_name": "iphone"})
        self.write_request("b.json", {"action": "revoke", "client_name": "laptop"})
        scan = scan_client_requests(self.requests_dir)
        self.assertEqual([], [item.reason for item in scan.rejected])
        self.assertEqual(["iphone", "laptop"], [item.client_name for item in scan.accepted])
        self.assertEqual(["issue", "revoke"], [item.action for item in scan.accepted])

    def test_result_replace_does_not_touch_other_suffixes(self):
        target = write_result(self.root / "results", "iphone.json", {"status": "ok"})
        self.assertEqual("iphone.result.json", target.name)

    def test_broken_json_is_rejected_not_raised(self):
        self.write_request("broken.json", "{not json")
        scan = scan_client_requests(self.requests_dir)
        self.assertEqual([], scan.accepted)
        self.assertEqual(1, len(scan.rejected))
        self.assertIn("unreadable JSON", scan.rejected[0].reason)

    def test_non_object_body_is_rejected(self):
        self.write_request("list.json", "[1, 2, 3]")
        scan = scan_client_requests(self.requests_dir)
        self.assertEqual("request body must be a JSON object", scan.rejected[0].reason)

    def test_unsupported_action_is_rejected_with_the_allowed_actions(self):
        self.write_request("weird.json", {"action": "delete", "client_name": "iphone"})
        scan = scan_client_requests(self.requests_dir)
        self.assertEqual([], scan.accepted)
        self.assertIn("unsupported action 'delete'", scan.rejected[0].reason)
        self.assertIn("issue", scan.rejected[0].reason)

    def test_invalid_client_name_is_rejected(self):
        self.write_request("bad.json", {"action": "issue", "client_name": "bad name"})
        scan = scan_client_requests(self.requests_dir)
        self.assertEqual([], scan.accepted)
        self.assertIn("Client name", scan.rejected[0].reason)

    def test_issued_request_is_archived(self):
        self.write_request("iphone.json", {"action": "issue", "client_name": "iphone"})
        scan = scan_client_requests(self.requests_dir)
        archived = archive_request(self.root / "archive", scan.accepted[0].request_path)
        self.assertTrue(archived.exists())
        self.assertFalse((self.requests_dir / "iphone.json").exists())

    def test_rejected_request_is_quarantined(self):
        self.write_request("broken.json", "{not json")
        scan = scan_client_requests(self.requests_dir)
        rejected = reject_request(self.root / "rejected", scan.rejected[0].request_path)
        self.assertTrue(rejected.exists())
        self.assertFalse((self.requests_dir / "broken.json").exists())

    def test_missing_directory_is_not_an_error(self):
        scan = scan_client_requests(self.root / "nope")
        self.assertEqual([], scan.accepted)
        self.assertEqual([], scan.rejected)


if __name__ == "__main__":
    unittest.main()
