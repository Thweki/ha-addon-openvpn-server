import tempfile
import unittest
from pathlib import Path

import openvpn_addon.config as config_module
from openvpn_addon import renderer
from openvpn_addon.config import AddonConfig


class RendererTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        config_module.DATA_ROOT = root
        templates = root / "templates"
        templates.mkdir()
        (templates / "server.conf.tpl").write_text(
            "proto ${protocol}\n${push_dns}\n${push_routes}\n",
            encoding="utf-8",
        )
        (templates / "client.ovpn.tpl").write_text("remote ${remote_host} ${remote_port}\n<ca>${ca}</ca>\n<cert>${cert}</cert>\n<key>${key}</key>\n${tls_crypt_block}\n", encoding="utf-8")
        renderer.TEMPLATES_DIR = templates

        pki = root / "pki"
        (pki / "issued").mkdir(parents=True)
        (pki / "private").mkdir(parents=True)
        (pki / "ca.crt").write_text("CA", encoding="utf-8")
        (pki / "issued" / "demo.crt").write_text("-----BEGIN CERTIFICATE-----\nCERT\n-----END CERTIFICATE-----\n", encoding="utf-8")
        (pki / "private" / "demo.key").write_text("KEY", encoding="utf-8")
        (pki / "ta.key").write_text("TLS", encoding="utf-8")

        self.config = AddonConfig(server_host="192.168.1.6", dns_servers=["1.1.1.1"])

    def test_render_server_config(self):
        self.config.local_networks = ["192.168.1.0/24"]
        output = renderer.render_server_config(self.config)
        self.assertIn("proto udp", output)
        self.assertIn('push "dhcp-option DNS 1.1.1.1"', output)
        self.assertIn('push "route 192.168.1.0 255.255.255.0"', output)

    def test_render_client_config(self):
        self.config.public_port = 443
        output = renderer.render_client_config(self.config, "demo")
        self.assertIn("remote 192.168.1.6 443", output)
        self.assertIn("<tls-crypt>", output)


if __name__ == "__main__":
    unittest.main()
