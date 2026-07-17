import importlib.util
import http.client
import io
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
            return_value=(body, 1, "https://cdn.example.com/signed", 200, "ClashMeta/1.19.0"),
        ), mock.patch.object(gateway, "validate_and_restart"):
            result = gateway.add_provider("airport", "https://example.com/sub?token=secret", 3600)

        cache = yaml.safe_load((gateway.PROVIDERS_DIR / "airport.yaml").read_text(encoding="utf-8"))
        cfg = gateway.load_cfg()
        self.assertEqual(1, result["nodes"])
        self.assertEqual("node-a", cache["proxies"][0]["name"])
        self.assertEqual(
            ["ClashMeta/1.19.0"],
            cfg["proxy-providers"]["airport"]["header"]["User-Agent"],
        )
        self.assertEqual(
            "https://example.com/sub?token=secret",
            cfg["proxy-providers"]["airport"]["url"],
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

    def test_delete_legacy_last_provider_adds_direct_fallbacks(self):
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

        self.assertNotIn("custom", gateway.load_cfg()["proxy-providers"])

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

    def test_fetch_retries_when_first_user_agent_returns_non_yaml(self):
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
        self.assertEqual("ClashMeta/1.19.0", ua)

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

    def test_management_request_body_has_application_size_limit(self):
        handler = object.__new__(gateway.H)
        handler.headers = {"Content-Length": str(64 * 1024 + 1)}
        handler.rfile = io.BytesIO(b"{}")

        with self.assertRaisesRegex(RuntimeError, "too large"):
            handler._body()


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

    def read(self, _limit):
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


if __name__ == "__main__":
    unittest.main()
