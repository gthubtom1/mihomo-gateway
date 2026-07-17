import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("gateway_app", ROOT / "panel" / "app.py")
gateway = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gateway)


class ProviderManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        gateway.CONFIG = root / "config.yaml"
        gateway.PROVIDERS_DIR = root / "providers"
        gateway.BACKUP_DIR = root / "backups"
        gateway.PROVIDERS_DIR.mkdir()
        gateway.PROTECTED_PROVIDERS = set()
        gateway.CONFIG.write_text(
            yaml.safe_dump(
                {
                    "proxy-providers": {},
                    "proxy-groups": [
                        {"name": "AUTO", "type": "url-test", "use": []},
                        {"name": "自定义", "type": "select", "use": []},
                    ],
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_provider_persists_valid_clash_yaml_and_client_header(self):
        body = yaml.safe_dump(
            {"proxies": [{"name": "node-a", "type": "ss", "server": "example.com", "port": 443}]},
            sort_keys=False,
        ).encode()

        with mock.patch.object(
            gateway,
            "fetch_subscription",
            return_value=(body, "https://example.com/sub", 200, "ClashMeta/1.19.0"),
        ), mock.patch.object(gateway, "validate_and_restart"):
            result = gateway.add_provider("airport", "https://example.com/sub", 3600)

        cache = yaml.safe_load((gateway.PROVIDERS_DIR / "airport.yaml").read_text(encoding="utf-8"))
        cfg = gateway.load_cfg()
        self.assertEqual(1, result["nodes"])
        self.assertEqual("node-a", cache["proxies"][0]["name"])
        self.assertEqual(
            ["ClashMeta/1.19.0"],
            cfg["proxy-providers"]["airport"]["header"]["User-Agent"],
        )
        self.assertIn("airport", cfg["proxy-groups"][0]["use"])

    def test_add_provider_rejects_403_without_writing_config_or_cache(self):
        before = gateway.CONFIG.read_bytes()
        with mock.patch.object(
            gateway,
            "fetch_subscription",
            side_effect=RuntimeError("HTTP Error 403: Forbidden"),
        ):
            with self.assertRaisesRegex(RuntimeError, "403"):
                gateway.add_provider("blocked", "https://example.com/sub", 3600)

        self.assertEqual(before, gateway.CONFIG.read_bytes())
        self.assertFalse((gateway.PROVIDERS_DIR / "blocked.yaml").exists())

    def test_orphan_yaml_is_listed_and_can_be_deleted_with_backup(self):
        orphan = gateway.PROVIDERS_DIR / "旧机场.yaml"
        orphan.write_text("proxies:\n  - name: old-node\n    type: direct\n", encoding="utf-8")

        rows = gateway.list_providers(gateway.load_cfg())
        row = next(item for item in rows if item["name"] == "旧机场")
        self.assertEqual("orphan", row["status"])
        self.assertEqual(1, row["nodes"])

        result = gateway.del_provider("旧机场")
        self.assertTrue(result["deleted"])
        self.assertTrue(result["orphan"])
        self.assertFalse(orphan.exists())
        self.assertTrue(any(gateway.BACKUP_DIR.rglob("旧机场.yaml*")))

    def test_delete_provider_rolls_back_when_validation_fails(self):
        cfg = gateway.load_cfg()
        cfg["proxy-providers"]["airport"] = {
            "type": "http",
            "url": "https://example.com/sub",
            "path": "./providers/airport.yaml",
        }
        cfg["proxy-groups"][0]["use"] = ["airport"]
        gateway.save_cfg(cfg)
        cache = gateway.PROVIDERS_DIR / "airport.yaml"
        cache.write_text("proxies:\n  - name: node-a\n    type: direct\n", encoding="utf-8")
        before = gateway.CONFIG.read_bytes()

        with mock.patch.object(
            gateway,
            "validate_and_restart",
            side_effect=RuntimeError("invalid config"),
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid config"):
                gateway.del_provider("airport")

        self.assertEqual(before, gateway.CONFIG.read_bytes())
        self.assertTrue(cache.exists())

    def test_orphan_delete_rejects_path_traversal(self):
        outside = gateway.PROVIDERS_DIR.parent / "outside.yaml"
        outside.write_text("proxies: []\n", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "not found"):
            gateway.del_provider("../outside")

        self.assertTrue(outside.exists())


if __name__ == "__main__":
    unittest.main()
