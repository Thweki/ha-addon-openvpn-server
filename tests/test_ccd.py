import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import openvpn_addon.config as config_module
from openvpn_addon import renderer
from openvpn_addon.config import AddonConfig, load_config
from openvpn_addon.main import sync_client_ips
from openvpn_addon.pki import ClientInfo, PkiManager
from openvpn_addon.validator import validate_config


def override(name="cabin", routes="192.168.50.0/24"):
    return {"client_name": name, "iroutes": routes}


class ClientOverrideTests(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)
        root_patch = patch.object(config_module, "DATA_ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        templates_patch = patch.object(
            renderer, "TEMPLATES_DIR", Path(__file__).resolve().parents[1] / "addon/templates"
        )
        templates_patch.start()
        self.addCleanup(templates_patch.stop)
        self.config = AddonConfig(
            experimental_client_overrides=True,
            client_overrides=[override()],
            local_networks=["192.168.1.0/24"],
        )
        self.pki = PkiManager(self.config)

    def test_loads_options_and_defaults_off(self):
        path = self.root / "options.json"
        path.write_text("{}")
        config = load_config(path)
        self.assertFalse(config.experimental_client_overrides)
        self.assertEqual([], config.client_overrides)
        path.write_text(json.dumps({
            "experimental_client_overrides": True,
            "client_overrides": [override()],
        }))
        config = load_config(path)
        validate_config(config)
        self.assertEqual([override()], config.client_overrides)
        self.assertEqual([], config.unknown_options)
        self.assertTrue(config.experimental_client_overrides)

    def test_disabled_overrides_have_no_effect(self):
        self.config.experimental_client_overrides = False
        self.config.client_overrides = ["invalid dormant configuration"]
        validate_config(self.config)
        self.pki.ensure_client_ips(["cabin"])
        self.assertEqual(
            "ifconfig-push 10.8.0.2 255.255.255.0\n",
            (self.config.ccd_dir / "cabin").read_text(),
        )
        self.assertFalse(any(line.startswith("route ") for line in
                             renderer.render_server_config(self.config).splitlines()))

    def test_renders_owned_routes_and_preserves_fixed_addresses(self):
        self.config.client_overrides = [
            override(routes="192.168.50.0/24, 172.20.0.7/32"),
            override("office", "192.168.60.0/24"),
        ]
        validate_config(self.config)
        assignments = self.pki.ensure_client_ips(["cabin", "office", "phone"])
        self.assertEqual(
            "ifconfig-push 10.8.0.2 255.255.255.0\n"
            "iroute 192.168.50.0 255.255.255.0\n"
            "iroute 172.20.0.7 255.255.255.255\n",
            (self.config.ccd_dir / "cabin").read_text(),
        )
        self.assertEqual(
            "ifconfig-push 10.8.0.3 255.255.255.0\niroute 192.168.60.0 255.255.255.0\n",
            (self.config.ccd_dir / "office").read_text(),
        )
        self.assertNotIn("iroute", (self.config.ccd_dir / "phone").read_text())
        self.assertEqual(assignments, self.pki.ensure_client_ips(["phone", "office", "cabin"]))
        output = renderer.render_server_config(self.config)
        self.assertIn(f"client-config-dir {self.config.ccd_dir}\n", output)
        for route in ["192.168.50.0 255.255.255.0", "172.20.0.7 255.255.255.255",
                      "192.168.60.0 255.255.255.0"]:
            self.assertIn(f"\nroute {route}\n", output)
            self.assertNotIn(f'push "route {route}"', output)
        self.assertIn('push "route 192.168.1.0 255.255.255.0"', output)

    def test_existing_extra_routes_can_advertise_an_owned_network(self):
        self.config.push_routes = ["192.168.50.0/24"]
        validate_config(self.config)
        output = renderer.render_server_config(self.config)
        self.assertIn('push "route 192.168.50.0 255.255.255.0"', output)
        self.assertIn("\nroute 192.168.50.0 255.255.255.0\n", output)

    def test_removing_override_or_disabling_flag_cleans_generated_routes(self):
        for disable in (False, True):
            with self.subTest(disable=disable):
                self.config.experimental_client_overrides = True
                self.config.client_overrides = [override()]
                original = self.pki.ensure_client_ips(["cabin"])
                if disable:
                    self.config.experimental_client_overrides = False
                else:
                    self.config.client_overrides = []
                self.assertEqual(original, self.pki.ensure_client_ips(["cabin"]))
                self.assertNotIn("iroute", (self.config.ccd_dir / "cabin").read_text())
                self.assertNotIn("\nroute 192.168.50.0", renderer.render_server_config(self.config))

    def test_missing_or_revoked_clients_do_not_get_ccd_files(self):
        self.config.client_overrides.append(override("future", "192.168.60.0/24"))
        with patch.object(self.pki, "list_clients", return_value=[ClientInfo("cabin", "valid")]):
            sync_client_ips(self.config, self.pki)
        self.assertTrue((self.config.ccd_dir / "cabin").exists())
        self.assertFalse((self.config.ccd_dir / "future").exists())
        with patch.object(self.pki, "list_clients", return_value=[ClientInfo("cabin", "revoked")]):
            sync_client_ips(self.config, self.pki)
        self.assertEqual([], list(self.config.ccd_dir.iterdir()))
        self.assertFalse(self.config.pki_dir.exists())

    def test_incompatible_modes_are_rejected(self):
        for field, value in [("duplicate_cn", True), ("fixed_client_ips", False), ("device", "tap")]:
            with self.subTest(field=field):
                config = AddonConfig(experimental_client_overrides=True, client_overrides=[override()])
                setattr(config, field, value)
                with self.assertRaisesRegex(ValueError, "requires device=tun"):
                    validate_config(config)

    def test_invalid_shapes_and_names_are_rejected(self):
        entries = [None, {}, "raw directive", [None], [{}], [override(None)],
                   [override("../cabin")], [override("cabin\n")], [override("DEFAULT")],
                   [override("server")], [override(routes=None)], [override(routes=[])],
                   [override(routes="")], [dict(override(), extra="push-reset")],
                   [override(), override()]]
        for entries_value in entries:
            with self.subTest(entries=entries_value):
                self.config.client_overrides = entries_value
                with self.assertRaises(ValueError):
                    validate_config(self.config)

    def test_invalid_or_conflicting_networks_are_rejected(self):
        for routes in ["::/64", "0.0.0.0/0", "10.8.0.0/25", "192.168.1.0/24",
                       "192.168.50.1/24", "192.168.50.0", "192.168.50.0/33",
                       "192.168.50.0/24,", "192.168.50.0/24\npush-reset",
                       "192.168.50.0/24,192.168.50.0/25"]:
            with self.subTest(routes=routes):
                self.config.client_overrides = [override(routes=routes)]
                with self.assertRaises(ValueError):
                    validate_config(self.config)
        self.config.client_overrides = [override(), override("office", "192.168.50.128/25")]
        with self.assertRaisesRegex(ValueError, "overlaps another client route"):
            validate_config(self.config)

    def test_invalid_override_leaves_existing_files_untouched(self):
        self.pki.ensure_client_ips(["cabin"])
        before = (self.config.ccd_dir / "cabin").read_bytes()
        addresses = self.config.client_ips_path.read_bytes()
        self.config.client_overrides = [override("../escape")]
        with self.assertRaises(ValueError):
            self.pki.ensure_client_ips(["cabin"])
        with self.assertRaises(ValueError):
            renderer.render_server_config(self.config)
        self.assertEqual(before, (self.config.ccd_dir / "cabin").read_bytes())
        self.assertEqual(addresses, self.config.client_ips_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
