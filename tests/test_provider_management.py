import base64
import importlib.util
import http.client
import io
import json
import socket
import subprocess
import tempfile
import threading
import time
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
        gateway.MIHOMO_SECRET = "test-secret"
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

    def _seed_provider(self, names, provider_name="airport"):
        cfg = gateway.load_cfg()
        cfg["proxy-providers"][provider_name] = {
            "type": "file",
            "path": f"./providers/{provider_name}.yaml",
        }
        for group in cfg["proxy-groups"]:
            group["use"] = [provider_name]
        gateway.save_cfg(cfg)
        (gateway.PROVIDERS_DIR / f"{provider_name}.yaml").write_text(
            yaml.safe_dump(
                {"proxies": [{"name": name, "type": "direct"} for name in names]},
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def test_add_provider_persists_valid_clash_yaml_and_client_header(self):
        body = yaml.safe_dump(
            {"proxies": [{"name": "node-a", "type": "ss", "server": "example.com", "port": 443}]},
            sort_keys=False,
        ).encode()

        with mock.patch.object(
            gateway,
            "fetch_subscription",
            return_value=(body, 1, "https://cdn.example.com/signed", 200, "ClashMeta/1.19.0"),
        ), mock.patch.object(gateway, "validate_and_restart"):
            result = gateway.add_provider("airport", "https://example.com/sub?token=secret", 3600)

        cache = yaml.safe_load((gateway.PROVIDERS_DIR / "airport.yaml").read_text(encoding="utf-8"))
        cfg = gateway.load_cfg()
        self.assertEqual(1, result["nodes"])
        self.assertEqual("node-a", cache["proxies"][0]["name"])
        self.assertEqual(
            "http://127.0.0.1:9092/internal/providers/airport",
            cfg["proxy-providers"]["airport"]["url"],
        )
        self.assertEqual(
            "https://example.com/sub?token=secret",
            cfg["proxy-providers"]["airport"]["x-source-url"],
        )
        self.assertEqual("DIRECT", cfg["proxy-providers"]["airport"]["proxy"])
        self.assertEqual(
            ["test-secret"],
            cfg["proxy-providers"]["airport"]["header"]["X-Secret"],
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

    def test_add_static_provider_imports_yaml_as_file_provider(self):
        body = yaml.safe_dump(
            {
                "proxies": [{
                    "name": "local-node",
                    "type": "ss",
                    "server": "example.com",
                    "port": 443,
                    "cipher": "aes-128-gcm",
                    "password": "test-password",
                }],
                "rules": ["MATCH,DIRECT"],
            },
            sort_keys=False,
        ).encode()

        with mock.patch.object(gateway, "validate_and_restart"):
            result = gateway.add_static_provider("local", body)

        cfg = gateway.load_cfg()
        provider = cfg["proxy-providers"]["local"]
        cache = yaml.safe_load(
            (gateway.PROVIDERS_DIR / "local.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual("file", provider["type"])
        self.assertEqual("./providers/local.yaml", provider["path"])
        self.assertNotIn("url", provider)
        self.assertNotIn("rules", cache)
        self.assertEqual(1, result["nodes"])
        self.assertIn("local", cfg["proxy-groups"][0]["use"])

    def test_add_static_provider_rolls_back_when_restart_fails(self):
        body = yaml.safe_dump(
            {"proxies": [{"name": "local-node", "type": "direct"}]},
            sort_keys=False,
        ).encode()
        before = gateway.CONFIG.read_bytes()

        with mock.patch.object(
            gateway,
            "validate_and_restart",
            side_effect=[RuntimeError("restart failed"), None],
        ):
            with self.assertRaisesRegex(RuntimeError, "restart failed"):
                gateway.add_static_provider("local", body)

        self.assertEqual(before, gateway.CONFIG.read_bytes())
        self.assertFalse((gateway.PROVIDERS_DIR / "local.yaml").exists())

    def test_orphan_yaml_is_listed_and_can_be_deleted_with_backup(self):
        orphan = gateway.PROVIDERS_DIR / " 旧机场 .yaml"
        orphan.write_text("proxies:\n  - name: old-node\n    type: direct\n", encoding="utf-8")

        rows = gateway.list_providers(gateway.load_cfg())
        row = next(item for item in rows if item["name"] == " 旧机场 ")
        self.assertEqual("orphan", row["status"])
        self.assertEqual(1, row["nodes"])
        self.assertTrue(row["id"].startswith("orphan:"))

        result = gateway.del_provider(provider_id=row["id"])
        self.assertTrue(result["deleted"])
        self.assertTrue(result["orphan"])
        self.assertFalse(orphan.exists())
        self.assertTrue(any(gateway.BACKUP_DIR.rglob(" 旧机场 .yaml*")))

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

    def test_delete_legacy_last_provider_adds_reject_fallbacks(self):
        cfg = gateway.load_cfg()
        cfg["proxy-providers"]["custom"] = {
            "type": "file",
            "path": "./providers/custom.yaml",
        }
        cfg["proxy-groups"] = [
            {"name": "AUTO", "type": "url-test", "use": ["custom"]},
            {"name": "故障转移", "type": "fallback", "use": ["custom"]},
            {"name": "自定义", "type": "select", "use": ["custom"], "proxies": ["DIRECT"]},
        ]
        gateway.save_cfg(cfg)
        (gateway.PROVIDERS_DIR / "custom.yaml").write_text("proxies: []\n", encoding="utf-8")

        def assert_no_empty_groups():
            current = gateway.load_cfg()
            empty = [
                g["name"] for g in current["proxy-groups"]
                if not (g.get("use") or g.get("proxies"))
            ]
            if empty:
                raise RuntimeError(f"empty groups: {empty}")

        with mock.patch.object(gateway, "validate_and_restart", side_effect=assert_no_empty_groups):
            gateway.del_provider("custom")

        current = gateway.load_cfg()
        self.assertNotIn("custom", current["proxy-providers"])
        for group in current["proxy-groups"]:
            self.assertEqual(["REJECT"], group["proxies"])

    def test_first_provider_removes_direct_from_automatic_groups(self):
        cfg = gateway.load_cfg()
        cfg["proxy-groups"] = [
            {"name": "AUTO", "type": "url-test", "use": [], "proxies": ["DIRECT"]},
            {"name": "GPT", "type": "url-test", "use": [], "proxies": ["DIRECT"]},
            {"name": "故障转移", "type": "fallback", "use": [], "proxies": ["DIRECT"]},
            {"name": "自定义", "type": "select", "use": [], "proxies": ["DIRECT"]},
        ]
        gateway.attach_provider_to_groups(cfg, "airport")

        groups = {group["name"]: group for group in cfg["proxy-groups"]}
        self.assertEqual([], groups["AUTO"]["proxies"])
        self.assertEqual([], groups["GPT"]["proxies"])
        self.assertEqual([], groups["故障转移"]["proxies"])
        self.assertEqual([], groups["自定义"]["proxies"])

    def test_new_provider_does_not_expand_existing_managed_route_scope(self):
        cfg = gateway.load_cfg()
        cfg["proxy-groups"] = [
            {"name": "AUTO", "type": "url-test", "use": ["old"]},
            {"name": "MGW-1100-P-source", "type": "url-test", "use": ["old"]},
            {"name": "MGW-1100-B-source", "type": "fallback", "use": ["old"]},
        ]

        gateway.attach_provider_to_groups(cfg, "new")

        groups = {group["name"]: group for group in cfg["proxy-groups"]}
        self.assertEqual(["old", "new"], groups["AUTO"]["use"])
        self.assertEqual(["old"], groups["MGW-1100-P-source"]["use"])
        self.assertEqual(["old"], groups["MGW-1100-B-source"]["use"])

    def test_orphan_id_cannot_delete_same_stem_configured_provider(self):
        cfg = gateway.load_cfg()
        cfg["proxy-providers"]["foo"] = {
            "type": "file",
            "path": "./providers/foo.yaml",
        }
        gateway.save_cfg(cfg)
        (gateway.PROVIDERS_DIR / "foo.yaml").write_text("proxies: []\n", encoding="utf-8")
        orphan = gateway.PROVIDERS_DIR / "foo.yml"
        orphan.write_text("proxies: []\n", encoding="utf-8")
        orphan_row = next(
            row for row in gateway.list_providers(gateway.load_cfg())
            if row["status"] == "orphan"
        )

        gateway.del_provider(provider_id=orphan_row["id"])

        self.assertIn("foo", gateway.load_cfg()["proxy-providers"])
        self.assertTrue((gateway.PROVIDERS_DIR / "foo.yaml").exists())
        self.assertFalse(orphan.exists())

    def test_delete_backs_up_before_committing_config(self):
        cfg = gateway.load_cfg()
        cfg["proxy-providers"]["airport"] = {
            "type": "file",
            "path": "./providers/airport.yaml",
        }
        gateway.save_cfg(cfg)
        cache = gateway.PROVIDERS_DIR / "airport.yaml"
        cache.write_text("proxies: []\n", encoding="utf-8")
        before = gateway.CONFIG.read_bytes()

        with mock.patch.object(
            gateway,
            "_backup_provider_file",
            side_effect=OSError("disk full"),
        ), mock.patch.object(gateway, "validate_and_restart") as restart:
            with self.assertRaisesRegex(OSError, "disk full"):
                gateway.del_provider("airport")

        self.assertEqual(before, gateway.CONFIG.read_bytes())
        self.assertTrue(cache.exists())
        restart.assert_not_called()

    def test_add_socks_rolls_back_when_restart_fails(self):
        self._seed_provider(["node-a", "node-b"])
        before = gateway.CONFIG.read_bytes()
        with mock.patch.object(
            gateway,
            "validate_and_restart",
            side_effect=[RuntimeError("not ready"), None],
        ), mock.patch.object(gateway, "ufw_allow") as allow, \
             mock.patch.object(gateway, "ufw_delete") as delete:
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                gateway.add_socks(1100, "AUTO")

        self.assertEqual(before, gateway.CONFIG.read_bytes())
        allow.assert_not_called()
        delete.assert_not_called()

    def test_add_socks_assigns_distinct_primary_nodes_and_fallback_groups(self):
        self._seed_provider(["node-a", "node-b", "node-c"])

        with mock.patch.object(gateway, "validate_and_restart"), \
             mock.patch.object(gateway, "ufw_allow", return_value=False):
            first = gateway.add_socks(1100, "AUTO")
            second = gateway.add_socks(1101, "AUTO")

        self.assertEqual("independent", first["mode"])
        self.assertNotEqual(first["primary"], second["primary"])
        cfg = gateway.load_cfg()
        listeners = {row["port"]: row for row in cfg["listeners"]}
        groups = {row["name"]: row for row in cfg["proxy-groups"]}
        for port, result in ((1100, first), (1101, second)):
            route = listeners[port]["proxy"]
            self.assertEqual(f"MGW-{port}", route)
            self.assertEqual("fallback", groups[route]["type"])
            self.assertEqual(60, groups[route]["interval"])
            self.assertTrue(groups[route]["lazy"])
            self.assertEqual(401, groups[route]["expected-status"])
            primary_name, backup_name = groups[route]["proxies"]
            self.assertEqual("url-test", groups[primary_name]["type"])
            self.assertEqual("fallback", groups[backup_name]["type"])
            self.assertEqual("REJECT", groups[primary_name]["empty-fallback"])
            self.assertEqual("REJECT", groups[backup_name]["empty-fallback"])
            self.assertEqual(["airport"], groups[primary_name]["use"])
            self.assertRegex(result["primary"], groups[primary_name]["filter"])
            self.assertRegex(result["primary"], groups[backup_name]["exclude-filter"])
            self.assertNotIn("(?:", groups[primary_name]["filter"])
            self.assertNotIn("(?:", groups[backup_name]["exclude-filter"])

    def test_add_socks_rejects_when_distinct_primary_candidates_are_exhausted(self):
        self._seed_provider(["node-a", "node-b"])

        with mock.patch.object(gateway, "validate_and_restart"), \
             mock.patch.object(gateway, "ufw_allow", return_value=False):
            first = gateway.add_socks(1100, "AUTO")
            second = gateway.add_socks(1101, "AUTO")
            with self.assertRaisesRegex(RuntimeError, "distinct healthy primary"):
                gateway.add_socks(1102, "AUTO")

        self.assertFalse(first["reused"])
        self.assertFalse(second["reused"])
        self.assertNotEqual(first["primary"], second["primary"])
        self.assertEqual(2, len(gateway.list_socks(gateway.load_cfg())))

    def test_add_socks_prefers_runtime_healthy_provider_nodes(self):
        self._seed_provider(["dead-node", "healthy-one", "healthy-two"])

        with mock.patch.object(gateway, "validate_and_restart"), \
             mock.patch.object(gateway, "ufw_allow", return_value=False):
            created = gateway.add_socks(
                1100,
                "AUTO",
                healthy_names={"healthy-one", "healthy-two"},
            )

        self.assertEqual("healthy-one", created["primary"])

    def test_provider_scoped_health_rejects_same_name_from_other_provider(self):
        self._seed_provider(["shared", "backup"], "first")
        cfg = gateway.load_cfg()
        cfg["proxy-providers"]["second"] = {
            "type": "file",
            "path": "./providers/second.yaml",
        }
        gateway.save_cfg(cfg)
        (gateway.PROVIDERS_DIR / "second.yaml").write_text(
            yaml.safe_dump(
                {"proxies": [{"name": "shared", "type": "direct"}]},
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with mock.patch.object(gateway, "validate_and_restart") as restart:
            with self.assertRaisesRegex(RuntimeError, "at least two"):
                gateway.add_socks(
                    1100,
                    "AUTO",
                    healthy_names={("second", "shared"), ("first", "backup")},
                )

        restart.assert_not_called()

    def test_duplicate_node_names_keep_provider_scoped_primary_identity(self):
        self._seed_provider(["shared"], "first")
        cfg = gateway.load_cfg()
        cfg["proxy-providers"]["second"] = {
            "type": "file",
            "path": "./providers/second.yaml",
        }
        for group in cfg["proxy-groups"]:
            group["use"] = ["first", "second"]
        gateway.save_cfg(cfg)
        (gateway.PROVIDERS_DIR / "second.yaml").write_text(
            yaml.safe_dump(
                {"proxies": [
                    {"name": "shared", "type": "direct"},
                    {"name": "backup", "type": "direct"},
                ]},
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        healthy = {
            ("first", "shared"),
            ("second", "shared"),
            ("second", "backup"),
        }

        with mock.patch.object(gateway, "validate_and_restart"), \
             mock.patch.object(gateway, "ufw_allow", return_value=False):
            first = gateway.add_socks(1100, "AUTO", healthy_names=healthy)
            second = gateway.add_socks(1101, "AUTO", healthy_names=healthy)

        self.assertEqual("shared", first["primary"])
        self.assertEqual("shared", second["primary"])
        self.assertNotEqual(first["primary_provider"], second["primary_provider"])
        groups = {group["name"]: group for group in gateway.load_cfg()["proxy-groups"]}
        for port, result in ((1100, first), (1101, second)):
            primary_group = groups[groups[f"MGW-{port}"]["proxies"][0]]
            self.assertEqual([result["primary_provider"]], primary_group["use"])

    def test_add_socks_requires_a_distinct_backup_candidate(self):
        self._seed_provider(["only-node"])

        with mock.patch.object(gateway, "validate_and_restart") as restart, \
             mock.patch.object(gateway, "ufw_allow") as allow:
            with self.assertRaisesRegex(RuntimeError, "at least two"):
                gateway.add_socks(1100, "AUTO", healthy_names={"only-node"})

        restart.assert_not_called()
        allow.assert_not_called()

    def test_add_socks_rejects_independent_route_without_provider_nodes(self):
        before = gateway.CONFIG.read_bytes()

        with mock.patch.object(gateway, "validate_and_restart") as restart, \
             mock.patch.object(gateway, "ufw_allow") as allow:
            with self.assertRaisesRegex(RuntimeError, "eligible provider node"):
                gateway.add_socks(1100, "AUTO")

        self.assertEqual(before, gateway.CONFIG.read_bytes())
        restart.assert_not_called()
        allow.assert_not_called()

    def test_migrate_socks_preserves_source_filter_and_credentials(self):
        self._seed_provider(["US-one", "JP-one", "US-two"])
        cfg = gateway.load_cfg()
        cfg["proxy-groups"].append({
            "name": "GPT",
            "type": "url-test",
            "use": ["airport"],
            "filter": "US",
            "exclude-filter": "(?i)(blocked)",
        })
        cfg["listeners"] = [{
            "name": "existing",
            "type": "socks",
            "port": 1100,
            "listen": "0.0.0.0",
            "users": [{"username": "keep-user", "password": "keep-pass"}],
            "proxy": "GPT",
        }]
        gateway.save_cfg(cfg)

        with mock.patch.object(gateway, "validate_and_restart") as restart, \
             mock.patch.object(gateway, "ufw_allow") as allow, \
             mock.patch.object(gateway, "ufw_delete") as delete:
            result = gateway.migrate_socks()

        self.assertEqual(1, result["migrated"])
        self.assertEqual("US-one", result["socks"][0]["primary"])
        current = gateway.load_cfg()
        listener = current["listeners"][0]
        self.assertEqual([{"username": "keep-user", "password": "keep-pass"}], listener["users"])
        groups = {row["name"]: row for row in current["proxy-groups"]}
        primary_name, backup_name = groups[listener["proxy"]]["proxies"]
        self.assertEqual(["airport"], groups[primary_name]["use"])
        self.assertEqual("US", groups[backup_name]["filter"])
        self.assertIn("blocked", groups[backup_name]["exclude-filter"])
        self.assertTrue(groups[backup_name]["exclude-filter"].startswith("(?i)"))
        self.assertNotIn("(?:", groups[backup_name]["exclude-filter"])
        restart.assert_called_once_with()
        allow.assert_not_called()
        delete.assert_not_called()

    def test_migrate_socks_uses_runtime_selection_for_top_level_select_group(self):
        self._seed_provider(["US-one", "JP-one", "US-two"])
        cfg = gateway.load_cfg()
        cfg["proxy-groups"] = [
            {"name": "PROXY", "type": "select", "proxies": ["AUTO", "GPT"]},
            {"name": "AUTO", "type": "url-test", "use": ["airport"]},
            {"name": "GPT", "type": "url-test", "use": ["airport"], "filter": "US"},
        ]
        cfg["listeners"] = [{
            "name": "existing",
            "type": "socks",
            "port": 1100,
            "users": [{"username": "user", "password": "pass"}],
            "proxy": "PROXY",
        }]
        gateway.save_cfg(cfg)

        with mock.patch.object(gateway, "validate_and_restart"):
            result = gateway.migrate_socks(
                healthy_names={"US-one", "US-two", "JP-one"},
                selected_groups={"PROXY": "GPT"},
            )

        self.assertEqual("US-one", result["socks"][0]["primary"])
        groups = {row["name"]: row for row in gateway.load_cfg()["proxy-groups"]}
        route = groups["MGW-1100"]
        backup = groups[route["proxies"][1]]
        self.assertEqual("US", backup["filter"])

    def test_select_group_with_provider_use_prefers_runtime_selected_node(self):
        self._seed_provider(["node-a", "node-b", "node-c"])

        with mock.patch.object(gateway, "validate_and_restart"), \
             mock.patch.object(gateway, "ufw_allow", return_value=False):
            result = gateway.add_socks(
                1100,
                "自定义",
                healthy_names={
                    ("airport", "node-a"),
                    ("airport", "node-b"),
                    ("airport", "node-c"),
                },
                selected_groups={"自定义": "node-b"},
            )

        self.assertEqual("node-b", result["primary"])

    def test_migrate_socks_is_idempotent(self):
        self._seed_provider(["node-a", "node-b"])
        cfg = gateway.load_cfg()
        cfg["listeners"] = [
            {"name": "one", "type": "socks", "port": 1100, "users": [], "proxy": "AUTO"},
            {"name": "two", "type": "socks", "port": 1101, "users": [], "proxy": "AUTO"},
        ]
        gateway.save_cfg(cfg)

        with mock.patch.object(gateway, "validate_and_restart") as restart:
            first = gateway.migrate_socks()
            after_first = gateway.CONFIG.read_bytes()
            second = gateway.migrate_socks()

        self.assertEqual(2, first["migrated"])
        self.assertEqual(0, second["migrated"])
        self.assertEqual(2, second["skipped"])
        self.assertEqual(after_first, gateway.CONFIG.read_bytes())
        restart.assert_called_once_with()
        self.assertEqual(
            2,
            len({row["primary"] for row in gateway.list_socks(gateway.load_cfg())}),
        )

    def test_migrate_socks_keeps_fixed_primary_when_temporarily_unhealthy(self):
        self._seed_provider(["node-a", "node-b", "node-c"])
        cfg = gateway.load_cfg()
        cfg["listeners"] = [
            {"name": "one", "type": "socks", "port": 1100, "users": [], "proxy": "AUTO"},
            {"name": "two", "type": "socks", "port": 1101, "users": [], "proxy": "AUTO"},
        ]
        gateway.save_cfg(cfg)

        with mock.patch.object(gateway, "validate_and_restart"):
            gateway.migrate_socks()
            before = gateway.CONFIG.read_bytes()
            result = gateway.migrate_socks(healthy_names={"node-b", "node-c"})

        rows = {row["port"]: row for row in gateway.list_socks(gateway.load_cfg())}
        self.assertEqual(0, result["migrated"])
        self.assertEqual(2, result["skipped"])
        self.assertEqual(before, gateway.CONFIG.read_bytes())
        self.assertEqual("node-a", rows[1100]["primary"])
        self.assertEqual("node-b", rows[1101]["primary"])

    def test_migrate_socks_rolls_back_when_restart_fails(self):
        self._seed_provider(["node-a", "node-b"])
        cfg = gateway.load_cfg()
        cfg["listeners"] = [
            {"name": "one", "type": "socks", "port": 1100, "users": [], "proxy": "AUTO"},
        ]
        gateway.save_cfg(cfg)
        before = gateway.CONFIG.read_bytes()

        with mock.patch.object(
            gateway,
            "validate_and_restart",
            side_effect=[RuntimeError("not ready"), None],
        ):
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                gateway.migrate_socks()

        self.assertEqual(before, gateway.CONFIG.read_bytes())

    def test_migrate_socks_reports_rollback_restart_failure(self):
        self._seed_provider(["node-a", "node-b"])
        cfg = gateway.load_cfg()
        cfg["listeners"] = [
            {"name": "one", "type": "socks", "port": 1100, "users": [], "proxy": "AUTO"},
        ]
        gateway.save_cfg(cfg)

        with mock.patch.object(
            gateway,
            "validate_and_restart",
            side_effect=[RuntimeError("migration failed"), RuntimeError("rollback failed")],
        ):
            with self.assertRaisesRegex(RuntimeError, "rollback restart failed"):
                gateway.migrate_socks()

    def test_delete_socks_removes_its_managed_groups(self):
        self._seed_provider(["node-a", "node-b"])
        with mock.patch.object(gateway, "validate_and_restart"), \
             mock.patch.object(gateway, "ufw_allow", return_value=False):
            created = gateway.add_socks(1100, "AUTO")

        with mock.patch.object(gateway, "validate_and_restart"), \
             mock.patch.object(gateway, "ufw_delete", return_value=False):
            gateway.del_socks(port=1100)

        group_names = {row["name"] for row in gateway.load_cfg()["proxy-groups"]}
        self.assertNotIn(created["route"], group_names)
        self.assertFalse(any(name.startswith("MGW-1100-") for name in group_names))

    def test_delete_last_provider_rejects_managed_routes_instead_of_using_direct(self):
        self._seed_provider(["node-a", "node-b"])
        with mock.patch.object(gateway, "validate_and_restart"), \
             mock.patch.object(gateway, "ufw_allow", return_value=False):
            created = gateway.add_socks(1100, "AUTO")

        with mock.patch.object(gateway, "validate_and_restart"):
            gateway.del_provider("airport")

        groups = {row["name"]: row for row in gateway.load_cfg()["proxy-groups"]}
        primary_name, backup_name = groups[created["route"]]["proxies"]
        self.assertEqual(["REJECT"], groups[primary_name]["proxies"])
        self.assertEqual(["REJECT"], groups[backup_name]["proxies"])
        self.assertEqual(["REJECT"], groups["AUTO"]["proxies"])

    def test_provider_health_checks_run_every_sixty_seconds(self):
        body = yaml.safe_dump(
            {"proxies": [{"name": "node-a", "type": "direct"}]},
            sort_keys=False,
        ).encode()

        with mock.patch.object(gateway, "validate_and_restart"):
            gateway.add_static_provider("local", body)

        provider = gateway.load_cfg()["proxy-providers"]["local"]
        self.assertEqual(60, provider["health-check"]["interval"])
        self.assertFalse(provider["health-check"].get("lazy", False))
        self.assertEqual("https://api.openai.com/v1/models", provider["health-check"]["url"])
        self.assertEqual(401, provider["health-check"]["expected-status"])
        for group in gateway.load_cfg()["proxy-groups"]:
            if group.get("use"):
                self.assertEqual("REJECT", group["empty-fallback"])
                if group.get("type") in {"url-test", "fallback"}:
                    self.assertEqual("https://api.openai.com/v1/models", group["url"])
                    self.assertEqual(401, group["expected-status"])

    def test_runtime_proxy_state_is_required_for_api_mutations(self):
        with mock.patch.object(gateway, "runtime_proxy_state", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "health status unavailable"):
                gateway.require_runtime_proxy_state()

    def test_runtime_proxy_state_combines_provider_health_and_group_selection(self):
        provider_payload = json.dumps({
            "providers": {
                "airport": {
                    "proxies": [
                        {"name": "node-a", "alive": True},
                        {"name": "node-b", "alive": False},
                    ],
                },
            },
        }).encode()
        group_payload = json.dumps({
            "proxies": {
                "PROXY": {"type": "Selector", "now": "GPT"},
                "GPT": {"type": "URLTest", "now": "node-a"},
            },
        }).encode()

        def fake_open(request, timeout):
            if request.full_url.endswith("/providers/proxies"):
                return _Response(provider_payload)
            return _Response(group_payload)

        with mock.patch.object(gateway.urllib.request, "urlopen", side_effect=fake_open):
            state = gateway.runtime_proxy_state()

        self.assertEqual({("airport", "node-a")}, state["healthy"])
        self.assertEqual("GPT", state["selected"]["PROXY"])

    def test_fresh_proxy_state_transaction_updates_health_before_action(self):
        self._seed_provider(["node-a", "node-b"])

        def fresh_state(refresh=False):
            self.assertTrue(refresh)
            health = gateway.load_cfg()["proxy-providers"]["airport"]["health-check"]
            self.assertEqual("https://api.openai.com/v1/models", health["url"])
            self.assertEqual(401, health["expected-status"])
            return {"healthy": {"node-a", "node-b"}, "selected": {}}

        with mock.patch.object(gateway, "validate_and_restart") as restart, \
             mock.patch.object(gateway, "require_runtime_proxy_state", side_effect=fresh_state):
            result = gateway.with_fresh_proxy_state(lambda state: len(state["healthy"]))

        self.assertEqual(2, result)
        restart.assert_called_once_with()

    def test_fresh_proxy_state_transaction_restores_config_when_refresh_fails(self):
        self._seed_provider(["node-a", "node-b"])
        before = gateway.CONFIG.read_bytes()

        with mock.patch.object(gateway, "validate_and_restart", side_effect=[None, None]) as restart, \
             mock.patch.object(
                 gateway,
                 "require_runtime_proxy_state",
                 side_effect=RuntimeError("health refresh failed"),
             ):
            with self.assertRaisesRegex(RuntimeError, "health refresh failed"):
                gateway.with_fresh_proxy_state(lambda state: state)

        self.assertEqual(before, gateway.CONFIG.read_bytes())
        self.assertEqual(2, restart.call_count)

    def test_delete_socks_rolls_back_when_restart_fails(self):
        cfg = gateway.load_cfg()
        cfg["listeners"] = [{
            "name": "socks-1100",
            "type": "socks",
            "port": 1100,
            "listen": "0.0.0.0",
            "users": [{"username": "user", "password": "pass"}],
            "proxy": "AUTO",
        }]
        gateway.save_cfg(cfg)
        before = gateway.CONFIG.read_bytes()

        with mock.patch.object(
            gateway,
            "validate_and_restart",
            side_effect=[RuntimeError("not ready"), None],
        ), mock.patch.object(gateway, "ufw_allow") as allow, \
             mock.patch.object(gateway, "ufw_delete") as delete:
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                gateway.del_socks(port=1100)

        self.assertEqual(before, gateway.CONFIG.read_bytes())
        allow.assert_not_called()
        delete.assert_not_called()

    def test_active_ufw_command_failure_is_not_reported_as_success(self):
        status = "Status: active\n\nTo                         Action      From\n"
        with mock.patch.object(gateway.subprocess, "check_output", return_value=status), \
             mock.patch.object(
                 gateway.subprocess,
                 "check_call",
                 side_effect=subprocess.CalledProcessError(1, ["ufw"]),
             ):
            with self.assertRaises(subprocess.CalledProcessError):
                gateway.ufw_allow(1100)

    def test_delete_provider_rolls_back_when_cache_unlink_fails(self):
        cfg = gateway.load_cfg()
        cfg["proxy-providers"]["airport"] = {
            "type": "file",
            "path": "./providers/airport.yaml",
        }
        gateway.save_cfg(cfg)
        cache = gateway.PROVIDERS_DIR / "airport.yaml"
        cache.write_text("proxies: []\n", encoding="utf-8")
        before = gateway.CONFIG.read_bytes()
        original_unlink = Path.unlink

        def fail_cache_unlink(path, *args, **kwargs):
            if path == cache:
                raise OSError("unlink failed")
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", new=fail_cache_unlink), \
             mock.patch.object(gateway, "validate_and_restart", side_effect=[None, None]):
            with self.assertRaisesRegex(OSError, "unlink failed"):
                gateway.del_provider("airport")

        self.assertEqual(before, gateway.CONFIG.read_bytes())
        self.assertTrue(cache.exists())

    def test_mutations_are_serialized_without_lost_updates(self):
        active = 0
        max_active = 0
        state_lock = threading.Lock()

        def fake_fetch(url):
            name = url.rsplit("/", 1)[-1]
            body = yaml.safe_dump(
                {"proxies": [{"name": name, "type": "direct"}]},
                sort_keys=False,
            ).encode()
            return body, 1, url, 200, "ClashMeta/1.19.0"

        def slow_restart():
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.03)
            with state_lock:
                active -= 1

        errors = []
        with mock.patch.object(gateway, "fetch_subscription", side_effect=fake_fetch), \
             mock.patch.object(gateway, "validate_and_restart", side_effect=slow_restart):
            threads = [
                threading.Thread(
                    target=lambda n=name: _capture_error(
                        errors, lambda: gateway.add_provider(n, f"https://example.com/{n}")
                    )
                )
                for name in ("one", "two")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual([], errors)
        self.assertEqual(1, max_active)
        self.assertEqual({"one", "two"}, set(gateway.load_cfg()["proxy-providers"]))

    def test_fetch_starts_with_browser_user_agent(self):
        good = yaml.safe_dump(
            {"proxies": [{"name": "node-browser", "type": "direct"}]},
            sort_keys=False,
        ).encode()
        with mock.patch.object(gateway, "_assert_safe_subscription_url"), \
             mock.patch.object(gateway, "_open_subscription", return_value=_Response(good)) as opened:
            normalized, count, _final, _status, ua = gateway.fetch_subscription(
                "https://example.com/sub"
            )

        self.assertEqual(1, count)
        self.assertIn(b"node-browser", normalized)
        self.assertTrue(ua.startswith("Mozilla/5.0"))
        request = opened.call_args.args[0]
        self.assertTrue(request.get_header("User-agent").startswith("Mozilla/5.0"))

    def test_fetch_retries_when_browser_user_agent_returns_non_subscription(self):
        good = yaml.safe_dump(
            {"proxies": [{"name": "node-a", "type": "direct"}]},
            sort_keys=False,
        ).encode()
        responses = [_Response(b"<html>blocked</html>"), _Response(good)]
        with mock.patch.object(gateway, "_assert_safe_subscription_url"), \
             mock.patch.object(gateway, "_open_subscription", side_effect=responses):
            normalized, count, _final, _status, ua = gateway.fetch_subscription(
                "https://example.com/sub"
            )

        self.assertEqual(1, count)
        self.assertIn(b"node-a", normalized)
        self.assertEqual("clash.meta", ua)

    def test_normalize_uses_substore_for_base64_uri_subscription(self):
        raw = base64.b64encode(
            b"ss://YWVzLTEyOC1nY206cGFzc0BleGFtcGxlLmNvbTo0NDM=#node-a\n"
        )
        converted = yaml.safe_dump(
            {
                "proxies": [{
                    "name": "node-a",
                    "type": "ss",
                    "server": "example.com",
                    "port": 443,
                    "cipher": "aes-128-gcm",
                    "password": "pass",
                }]
            },
            sort_keys=False,
        ).encode()

        with mock.patch.object(
            gateway,
            "_convert_with_substore",
            return_value=converted,
            create=True,
        ) as convert:
            normalized, count = gateway.normalize_subscription(raw)

        convert.assert_called_once_with(raw, timeout=30)
        self.assertEqual(1, count)
        self.assertEqual("ss", yaml.safe_load(normalized)["proxies"][0]["type"])

    def test_normalize_rejects_invalid_converter_output(self):
        with mock.patch.object(
            gateway,
            "_convert_with_substore",
            return_value=b"not: a-provider\n",
            create=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "converter output"):
                gateway.normalize_subscription(b"dmxlc3M6Ly9leGFtcGxl")

    def test_converter_uses_private_ephemeral_work_directory(self):
        converted = yaml.safe_dump(
            {"proxies": [{"name": "node-a", "type": "direct"}]},
            sort_keys=False,
        ).encode()
        def fake_run(*_args, **kwargs):
            kwargs["stdout"].write(converted)
            return mock.Mock(returncode=0)
        temporary = mock.MagicMock()
        temporary.__enter__.return_value = "/tmp/converter-private"
        temporary.__exit__.return_value = False

        with mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch.object(gateway.tempfile, "TemporaryDirectory", return_value=temporary), \
             mock.patch.object(gateway.os, "chown", create=True) as chown, \
             mock.patch.object(gateway.os, "chmod") as chmod, \
             mock.patch.object(gateway.subprocess, "run", side_effect=fake_run) as run:
            output = gateway._convert_with_substore(b"raw subscription")

        self.assertEqual(converted, output)
        chown.assert_not_called()
        chmod.assert_called_once_with("/tmp/converter-private", 0o777)
        self.assertEqual("/", run.call_args.kwargs["cwd"])
        self.assertIsNot(run.call_args.kwargs["stdout"], subprocess.PIPE)
        command = run.call_args.args[0]
        self.assertIn("/usr/bin/bwrap", command)
        self.assertIn("--unshare-all", command)
        self.assertIn("--cap-drop", command)
        self.assertIn("/usr/bin/prlimit", command)
        self.assertIn("--fsize=33554432", command)
        bind_index = command.index("--bind")
        self.assertEqual(
            ["/tmp/converter-private", "/work"],
            command[bind_index + 1:bind_index + 3],
        )

    def test_fetch_stops_after_first_rate_limit_and_reports_retry_after(self):
        error = _RateLimitError("HTTP Error 429", retry_after="120")
        with mock.patch.object(gateway, "_assert_safe_subscription_url"), \
             mock.patch.object(gateway, "_open_subscription", side_effect=error) as opened:
            with self.assertRaisesRegex(RuntimeError, "retry after 120 seconds"):
                gateway.fetch_subscription("https://example.com/sub")

        self.assertEqual(1, opened.call_count)

    def test_fetch_timeout_is_a_total_budget_across_user_agents(self):
        def slow_failure(*_args, **_kwargs):
            raise RuntimeError("network failed")

        with mock.patch.object(gateway, "_assert_safe_subscription_url"), \
             mock.patch.object(
                 gateway.time,
                 "monotonic",
                 side_effect=[0.0, 0.01, 0.03, 0.06],
             ), \
             mock.patch.object(
                 gateway,
                 "_open_subscription",
                 side_effect=slow_failure,
             ) as opened:
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                gateway.fetch_subscription("https://example.com/sub", timeout=0.05)

        self.assertEqual(2, opened.call_count)

    def test_http_429_preserves_retry_after_header(self):
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.20", 443)),
        ]
        response = _RawHTTPResponse(
            b"",
            status=429,
            headers={"Retry-After": "90"},
        )
        connection = mock.Mock()
        connection.getresponse.return_value = response
        request = gateway.urllib.request.Request("https://limited.example/sub")

        with mock.patch.object(gateway.socket, "getaddrinfo", return_value=addresses), \
             mock.patch.object(
                 gateway.ipaddress,
                 "ip_address",
                 return_value=mock.Mock(is_global=True),
             ), \
             mock.patch.object(gateway, "_PinnedHTTPSConnection", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "HTTP Error 429") as raised:
                gateway._open_subscription(request, timeout=3)

        self.assertEqual("90", getattr(raised.exception, "retry_after", None))

    def test_fetch_uses_existing_socks_after_all_direct_uas_are_forbidden(self):
        good = yaml.safe_dump(
            {"proxies": [{"name": "node-via-proxy", "type": "direct"}]},
            sort_keys=False,
        ).encode()
        forbidden = _HTTPStatusError("HTTP Error 403", status=403)
        connector = mock.Mock(name="socks_connector")
        responses = [forbidden] * 6 + [_Response(good)]

        with mock.patch.object(gateway, "_assert_safe_subscription_url"), \
             mock.patch.object(
                 gateway,
                 "_subscription_proxy_connector",
                 return_value=connector,
                 create=True,
             ) as connector_factory, \
             mock.patch.object(gateway, "_open_subscription", side_effect=responses) as opened:
            try:
                normalized, count, _final, _status, _ua = gateway.fetch_subscription(
                    "https://example.com/sub"
                )
            except Exception as exc:
                self.fail(f"proxy fallback was not used: {exc}")

        self.assertEqual(1, count)
        self.assertIn(b"node-via-proxy", normalized)
        connector_factory.assert_called_once_with()
        self.assertIs(connector, opened.call_args_list[-1].kwargs.get("connector"))

    def test_socks_connector_sends_validated_ip_with_authentication(self):
        connector = getattr(gateway, "_socks5_connect", None)
        self.assertIsNotNone(connector, "SOCKS5 connector is required")
        fake_socket = _FakeSocket(
            b"\x05\x02"
            b"\x01\x00"
            b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        )
        with mock.patch.object(gateway.socket, "create_connection", return_value=fake_socket):
            result = connector(
                "127.0.0.1",
                1080,
                "192.0.2.20",
                443,
                "test-user",
                "test-password",
                3,
            )

        self.assertIs(fake_socket, result)
        connect_request = fake_socket.sent[-1]
        self.assertEqual(b"\x05\x01\x00\x01", connect_request[:4])
        self.assertEqual(socket.inet_pton(socket.AF_INET, "192.0.2.20"), connect_request[4:8])

    def test_socks_connector_rejects_no_auth_downgrade_when_credentials_exist(self):
        fake_socket = _FakeSocket(
            b"\x05\x00"
            b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        )
        with mock.patch.object(gateway.socket, "create_connection", return_value=fake_socket):
            with self.assertRaisesRegex(RuntimeError, "authentication"):
                gateway._socks5_connect(
                    "127.0.0.1",
                    1080,
                    "192.0.2.20",
                    443,
                    "test-user",
                    "test-password",
                    3,
                )

    def test_http_subscription_never_uses_socks_fallback(self):
        forbidden = _HTTPStatusError("HTTP Error 403", status=403)
        connector = mock.Mock(name="socks_connector")
        with mock.patch.object(gateway, "_assert_safe_subscription_url"), \
             mock.patch.object(
                 gateway,
                 "_subscription_proxy_connector",
                 return_value=connector,
             ) as connector_factory, \
             mock.patch.object(gateway, "_open_subscription", side_effect=forbidden):
            with self.assertRaisesRegex(RuntimeError, "HTTP Error 403"):
                gateway.fetch_subscription("http://example.com/sub?token=test")

        connector_factory.assert_not_called()

    def test_socks_fallback_rejects_https_to_http_redirect(self):
        redirect = _RawHTTPResponse(
            b"",
            status=302,
            headers={"Location": "http://example.com/sub?token=test"},
        )
        wrapped = gateway._SubscriptionResponse(redirect, mock.Mock(), "https://example.com/sub")
        request = gateway.urllib.request.Request("https://example.com/sub")
        with mock.patch.object(gateway, "_open_pinned_once", return_value=wrapped), \
             mock.patch.object(gateway, "_assert_safe_subscription_url"):
            with self.assertRaisesRegex(RuntimeError, "HTTPS"):
                gateway._open_subscription(request, timeout=3, connector=mock.Mock())

    def test_direct_fetch_rejects_https_to_http_redirect(self):
        redirect = _RawHTTPResponse(
            b"",
            status=302,
            headers={"Location": "http://example.com/sub?token=test"},
        )
        wrapped = gateway._SubscriptionResponse(redirect, mock.Mock(), "https://example.com/sub")
        request = gateway.urllib.request.Request("https://example.com/sub")
        with mock.patch.object(gateway, "_open_pinned_once", return_value=wrapped), \
             mock.patch.object(gateway, "_assert_safe_subscription_url"):
            with self.assertRaisesRegex(RuntimeError, "downgrade"):
                gateway._open_subscription(request, timeout=3)

    def test_direct_retry_after_survives_failed_proxy_fallback(self):
        direct_limit = _RateLimitError("HTTP Error 429", retry_after="120")
        proxy_forbidden = _HTTPStatusError("HTTP Error 403", status=403)
        connector = mock.Mock(name="socks_connector")
        responses = [direct_limit] + [proxy_forbidden] * 6
        with mock.patch.object(gateway, "_assert_safe_subscription_url"), \
             mock.patch.object(
                 gateway,
                 "_subscription_proxy_connector",
                 return_value=connector,
             ), \
             mock.patch.object(gateway, "_open_subscription", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "retry after 120 seconds"):
                gateway.fetch_subscription("https://example.com/sub")

    def test_mixed_direct_errors_do_not_trigger_proxy_fallback(self):
        mixed_errors = [RuntimeError("network failed")] + [
            _HTTPStatusError("HTTP Error 403", status=403)
        ] * 5
        connector = mock.Mock(name="socks_connector")
        with mock.patch.object(gateway, "_assert_safe_subscription_url"), \
             mock.patch.object(
                 gateway,
                 "_subscription_proxy_connector",
                 return_value=connector,
             ) as connector_factory, \
             mock.patch.object(gateway, "_open_subscription", side_effect=mixed_errors):
            with self.assertRaisesRegex(RuntimeError, "HTTP Error 403"):
                gateway.fetch_subscription("https://example.com/sub")

        connector_factory.assert_not_called()

    def test_subscription_proxy_requires_runtime_route_to_cached_provider(self):
        cfg = gateway.load_cfg()
        cfg["proxy-providers"]["static"] = {
            "type": "file",
            "path": "./providers/static.yaml",
        }
        cfg["listeners"] = [{
            "name": "socks-main",
            "type": "socks",
            "port": 1080,
            "users": [{"username": "user", "password": "password"}],
            "proxy": "PROXY",
        }]
        gateway.save_cfg(cfg)
        (gateway.PROVIDERS_DIR / "static.yaml").write_text(
            "proxies:\n  - name: node-a\n    type: direct\n",
            encoding="utf-8",
        )

        with mock.patch.object(
            gateway,
            "_runtime_route_uses_provider",
            return_value=False,
            create=True,
        ) as route_check:
            self.assertIsNone(gateway._subscription_proxy_connector())

        route_check.assert_called_once()

    def test_subscription_url_rejects_loopback_target(self):
        body = yaml.safe_dump(
            {"proxies": [{"name": "internal", "type": "direct"}]},
            sort_keys=False,
        ).encode()
        with mock.patch.object(gateway.urllib.request, "urlopen", return_value=_Response(body)):
            with self.assertRaisesRegex(RuntimeError, "public address"):
                gateway.fetch_subscription("http://127.0.0.1/admin")

    def test_subscription_redirect_rejects_private_target(self):
        public_addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.20", 80)),
        ]
        private_addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80)),
        ]
        redirect = _RawHTTPResponse(
            b"",
            status=302,
            headers={"Location": "http://169.254.169.254/latest/meta-data/"},
        )
        legacy = mock.Mock()
        legacy.open.return_value = redirect
        connection = mock.Mock()
        connection.getresponse.return_value = redirect
        request = gateway.urllib.request.Request("http://public.example/sub")

        with mock.patch.object(
            gateway.socket,
            "getaddrinfo",
            side_effect=[public_addresses, private_addresses],
        ), mock.patch.object(
            gateway.ipaddress,
            "ip_address",
            side_effect=lambda raw: mock.Mock(is_global=raw == "192.0.2.20"),
        ), mock.patch.object(
            http.client,
            "HTTPConnection",
            return_value=connection,
        ), mock.patch.object(
            gateway.urllib.request,
            "build_opener",
            return_value=legacy,
        ):
            with self.assertRaisesRegex(RuntimeError, "public address"):
                gateway._open_subscription(request, timeout=3)

    def test_subscription_connection_uses_the_validated_ip(self):
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.20", 80)),
        ]
        response = _RawHTTPResponse(b"proxies: []\n")
        connection = mock.Mock()
        connection.getresponse.return_value = response
        request = gateway.urllib.request.Request("http://rebind.example/sub")

        with mock.patch.object(gateway.socket, "getaddrinfo", return_value=addresses), \
             mock.patch.object(
                 gateway.ipaddress,
                 "ip_address",
                 return_value=mock.Mock(is_global=True),
             ), \
             mock.patch.object(http.client, "HTTPConnection", return_value=connection) as http_conn, \
             mock.patch.object(gateway.urllib.request, "build_opener") as legacy_opener:
            opened = gateway._open_subscription(request, timeout=3)

        legacy_opener.assert_not_called()
        http_conn.assert_called_once_with("192.0.2.20", 80, timeout=3)
        opened.close()

    def test_provider_list_redacts_subscription_url(self):
        cfg = gateway.load_cfg()
        cfg["proxy-providers"]["airport"] = {
            "type": "http",
            "url": "https://example.com/sub/path?token=secret&user=alice",
            "path": "./providers/airport.yaml",
        }
        row = gateway.list_providers(cfg)[0]

        self.assertNotIn("url", row)
        self.assertNotIn("secret", row["display_url"])
        self.assertNotIn("alice", row["display_url"])

    def test_provider_list_uses_redacted_source_url_for_converted_provider(self):
        cfg = gateway.load_cfg()
        cfg["proxy-providers"]["airport"] = {
            "type": "http",
            "url": "http://127.0.0.1:9092/internal/providers/airport",
            "x-source-url": "https://example.com/sub/path?token=secret&user=alice",
            "path": "./providers/airport.yaml",
        }
        row = gateway.list_providers(cfg)[0]

        self.assertIn("example.com", row["display_url"])
        self.assertNotIn("127.0.0.1", row["display_url"])
        self.assertNotIn("secret", row["display_url"])
        self.assertNotIn("alice", row["display_url"])

    def test_refresh_converted_provider_uses_original_source_url(self):
        cfg = gateway.load_cfg()
        cfg["proxy-providers"]["airport"] = {
            "type": "http",
            "url": "http://127.0.0.1:9092/internal/providers/airport",
            "x-source-url": "https://example.com/sub?token=secret",
            "path": "./providers/airport.yaml",
        }
        normalized = yaml.safe_dump(
            {"proxies": [{"name": "node-a", "type": "direct"}]},
            sort_keys=False,
        ).encode()
        with mock.patch.object(
            gateway,
            "fetch_subscription",
            return_value=(normalized, 1, "https://example.com/final", 200, "browser"),
        ) as fetch:
            body, count = gateway.refresh_converted_provider("airport", cfg)

        fetch.assert_called_once_with("https://example.com/sub?token=secret", timeout=15)
        self.assertEqual(normalized, body)
        self.assertEqual(1, count)

    def test_concurrent_managed_refresh_serves_existing_cache_immediately(self):
        cfg = gateway.load_cfg()
        cfg["proxy-providers"]["airport"] = {
            "type": "http",
            "url": "http://127.0.0.1:9092/internal/providers/airport",
            "x-source-url": "https://example.com/sub",
            "path": "./providers/airport.yaml",
        }
        cache = yaml.safe_dump(
            {"proxies": [{"name": "cached", "type": "direct"}]},
            sort_keys=False,
        ).encode()
        (gateway.PROVIDERS_DIR / "airport.yaml").write_bytes(cache)
        lock = threading.Lock()
        lock.acquire()

        with mock.patch.object(gateway, "_provider_refresh_lock", return_value=lock, create=True), \
             mock.patch.object(gateway, "refresh_converted_provider") as refresh:
            body, count = gateway.managed_provider_content("airport", cfg)

        lock.release()
        refresh.assert_not_called()
        self.assertEqual(1, count)
        self.assertIn(b"cached", body)

    def test_refresh_rejects_provider_without_managed_source(self):
        cfg = gateway.load_cfg()
        cfg["proxy-providers"]["airport"] = {
            "type": "http",
            "url": "https://example.com/sub",
            "path": "./providers/airport.yaml",
        }

        with self.assertRaisesRegex(RuntimeError, "managed provider"):
            gateway.refresh_converted_provider("airport", cfg)

    def test_management_request_body_has_application_size_limit(self):
        handler = object.__new__(gateway.H)
        handler.headers = {"Content-Length": str(64 * 1024 + 1)}
        handler.rfile = io.BytesIO(b"{}")

        with self.assertRaisesRegex(RuntimeError, "too large"):
            handler._body()

    def test_yaml_upload_body_has_subscription_size_limit(self):
        handler = object.__new__(gateway.H)
        handler.headers = {"Content-Length": str(16 * 1024 * 1024 + 1)}
        handler.rfile = io.BytesIO(b"")

        with self.assertRaisesRegex(RuntimeError, "too large"):
            handler._raw_body(gateway.MAX_SUBSCRIPTION_BODY)


def _capture_error(errors, action):
    try:
        action()
    except Exception as exc:
        errors.append(exc)


class _Response:
    def __init__(self, body):
        self.body = body
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=None):
        return self.body

    def geturl(self):
        return "https://cdn.example.com/signed"


class _RawHTTPResponse:
    def __init__(self, body, status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {}

    def read(self, limit=-1):
        return self.body if limit < 0 else self.body[:limit]

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def close(self):
        return None


class _RateLimitError(RuntimeError):
    status = 429

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class _HTTPStatusError(RuntimeError):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


class _FakeSocket:
    def __init__(self, received):
        self.received = bytearray(received)
        self.sent = []

    def recv(self, size):
        chunk = bytes(self.received[:size])
        del self.received[:size]
        return chunk

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        return None


if __name__ == "__main__":
    unittest.main()
