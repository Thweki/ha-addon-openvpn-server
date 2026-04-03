import json
import tempfile
import unittest
from pathlib import Path

from openvpn_addon.requests import archive_request, load_client_requests, write_result


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_load_client_issue_request(self):
        requests_dir = self.root / "requests"
        requests_dir.mkdir()
        (requests_dir / "iphone.json").write_text(
            json.dumps({"action": "issue", "client_name": "iphone"}),
            encoding="utf-8",
        )
        items = load_client_requests(requests_dir)
        self.assertEqual(1, len(items))
        self.assertEqual("iphone", items[0].client_name)
        self.assertEqual("issue", items[0].action)

    def test_load_client_revoke_request(self):
        requests_dir = self.root / "requests"
        requests_dir.mkdir()
        (requests_dir / "iphone.json").write_text(
            json.dumps({"action": "revoke", "client_name": "iphone"}),
            encoding="utf-8",
        )
        items = load_client_requests(requests_dir)
        self.assertEqual(1, len(items))
        self.assertEqual("revoke", items[0].action)

    def test_write_result_and_archive(self):
        results_dir = self.root / "results"
        archive_dir = self.root / "archive"
        request_path = self.root / "iphone.json"
        request_path.write_text("{}", encoding="utf-8")
        result_path = write_result(results_dir, "iphone.json", {"status": "ok"})
        archived_path = archive_request(archive_dir, request_path)
        self.assertTrue(result_path.exists())
        self.assertTrue(archived_path.exists())


if __name__ == "__main__":
    unittest.main()
