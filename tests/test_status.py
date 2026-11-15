import json
import tempfile
import unittest
from pathlib import Path

import openvpn_addon.config as config_module
from openvpn_addon.config import AddonConfig
from openvpn_addon.status import _parse_status, collect_status, status_json


STATUS_V2 = (
    "TITLE,OpenVPN 2.6\n"
    "TIME,2026-10-14 08:00:00,1791964800\n"
    "HEADER,CLIENT_LIST,Common Name,Real Address,Virtual Address,Virtual IPv6 Address,"
    "Bytes Received,Bytes Sent,Connected Since,Connected Since (time_t),Username,Client ID,Peer ID,Data Channel Cipher\n"
    "CLIENT_LIST,alice,203.0.113.5:51234,10.8.0.2,,123456,789012,2026-10-14 07:00:00,1791961200,UNDEF,0,1,AES-256-GCM\n"
    "HEADER,ROUTING_TABLE,Virtual Address,Common Name,Real Address,Last Ref,Last Ref (time_t)\n"
    "ROUTING_TABLE,10.8.0.2,alice,203.0.113.5:51234,2026-10-14 08:00:00,1791964800\n"
    "GLOBAL_STATS,Max bcast/mcast queue length,0\n"
    "END\n"
)


class StatusParserTests(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.path = Path(tempdir.name) / "status.log"

    def test_v2_byte_counts_and_vpn_address_do_not_shift(self):
        for ipv6 in ("", "2001:db8::2"):
            with self.subTest(ipv6=ipv6):
                self.path.write_text(STATUS_V2.replace("10.8.0.2,,", f"10.8.0.2,{ipv6},"), encoding="utf-8")
                self.assertEqual(
                    {"alice": {
                        "common_name": "alice",
                        "real_address": "203.0.113.5:51234",
                        "virtual_address": "10.8.0.2",
                        "bytes_received": "123456",
                        "bytes_sent": "789012",
                        "connected_since": "2026-10-14 07:00:00",
                    }},
                    _parse_status(self.path),
                )

    def test_v2_fields_are_read_by_header_name(self):
        self.path.write_text(
            "HEADER,CLIENT_LIST,Common Name,Real Address,Virtual Address,Bytes Sent,Bytes Received,Connected Since\n"
            "CLIENT_LIST,bob,198.51.100.4:1234,10.8.0.3,200,100,2026-10-14 07:30:00\n",
            encoding="utf-8",
        )
        session = _parse_status(self.path)["bob"]
        self.assertEqual("100", session["bytes_received"])
        self.assertEqual("200", session["bytes_sent"])
        self.assertEqual("10.8.0.3", session["virtual_address"])
        self.assertEqual("2026-10-14 07:30:00", session["connected_since"])

    def test_missing_or_incomplete_status_has_no_sessions(self):
        self.assertEqual({}, _parse_status(self.path))
        for content in (
            "",
            "CLIENT_LIST,alice,203.0.113.5:51234,10.8.0.2,,100,200\n",
            "HEADER,CLIENT_LIST,Common Name,Real Address\nCLIENT_LIST,alice,203.0.113.5:51234\n",
            STATUS_V2.split("CLIENT_LIST,alice")[0] + "CLIENT_LIST,alice,203.0.113.5:51234\n",
        ):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                self.assertEqual({}, _parse_status(self.path))


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.original_root = config_module.DATA_ROOT
        self.original_share = config_module.SHARE_ROOT
        config_module.DATA_ROOT = root
        config_module.SHARE_ROOT = root / "share"
        self.addCleanup(setattr, config_module, "DATA_ROOT", self.original_root)
        self.addCleanup(setattr, config_module, "SHARE_ROOT", self.original_share)

        self.config = AddonConfig(
            server_host="0.0.0.0",
            public_endpoint="vpn.example.com",
            listen_port=1194,
            public_port=443,
            protocol="udp",
            local_networks=["192.168.1.0/24"],
        )
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.config.share_exports_dir.mkdir(parents=True, exist_ok=True)
        self.config.share_requests_dir.mkdir(parents=True, exist_ok=True)
        self.config.share_results_dir.mkdir(parents=True, exist_ok=True)

    def test_collect_status_includes_summary_routes_and_warnings(self):
        data = collect_status(self.config)
        self.assertEqual("0.0.0.0:1194", data["summary"]["listen_address"])
        self.assertEqual("vpn.example.com:443", data["summary"]["public_address"])
        self.assertEqual(["192.168.1.0/24"], data["routes"])
        self.assertEqual(
            ["public_port differs from listen_port; ensure your router or firewall forwards the external port to the local listening port."],
            data["warnings"],
        )

    def test_collect_status_includes_health_and_networking(self):
        data = collect_status(self.config)
        self.assertIn(data["health"]["status"], {"ok", "warn", "error"})
        names = [item["name"] for item in data["health"]["checks"]]
        self.assertIn("lan_forwarding", names)
        self.assertIn("ip_forward", names)
        self.assertIn("crl", names)
        self.assertEqual("10.8.0.0/24", data["networking"]["vpn_network"])
        self.assertIn("next_update", data["crl"])

    def test_clients_merge_certificates_with_live_sessions(self):
        self.config.pki_dir.mkdir(parents=True)
        (self.config.pki_dir / "index.txt").write_text(
            "V\t271014070000Z\t\t01\tunknown\t/CN=alice\n"
            "V\t271014070000Z\t\t02\tunknown\t/CN=bob\n",
            encoding="utf-8",
        )
        self.config.status_path.write_text(STATUS_V2, encoding="utf-8")
        for fixed_ip in (None, "10.8.0.20"):
            with self.subTest(fixed_ip=fixed_ip):
                if fixed_ip:
                    self.config.client_ips_path.write_text(json.dumps({"alice": fixed_ip}), encoding="utf-8")
                data = collect_status(self.config)
                self.assertEqual(["alice"], data["clients_online"])
                self.assertEqual(
                    [
                        {
                            "name": "alice", "status": "valid", "expires": None,
                            "vpn_address": fixed_ip or "10.8.0.2", "online": True,
                            "real_address": "203.0.113.5:51234",
                            "bytes_received": "123456", "bytes_sent": "789012",
                            "connected_since": "2026-10-14 07:00:00",
                        },
                        {
                            "name": "bob", "status": "valid", "expires": None,
                            "vpn_address": None, "online": False, "real_address": None,
                            "bytes_received": None, "bytes_sent": None, "connected_since": None,
                        },
                    ],
                    data["clients"],
                )

    def test_status_json_is_valid_json(self):
        payload = json.loads(status_json(self.config))
        self.assertEqual("udp", payload["summary"]["protocol"])


if __name__ == "__main__":
    unittest.main()
