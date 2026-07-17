import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
RENDER_CONFIG = ROOT / "scripts" / "render-config.py"
COMMON_SH = ROOT / "scripts" / "common.sh"
INSTALL_SH = ROOT / "install.sh"
BOOTSTRAP_SH = ROOT / "bootstrap.sh"
README = ROOT / "README.md"
PANEL_API_SERVICE = ROOT / "panel" / "mihomo-gateway-api.service"
GATEWAY_CLI = ROOT / "scripts" / "mihomo-gateway"

SPEC = importlib.util.spec_from_file_location("render_config", RENDER_CONFIG)
render_config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render_config)


def find_bash():
    found = shutil.which("bash")
    if found:
        return found
    if os.name == "nt":
        for candidate in (
            Path("D:/Program/Git/bin/bash.exe"),
            Path("C:/Program Files/Git/bin/bash.exe"),
        ):
            if candidate.exists():
                return str(candidate)
    return None


BASH = find_bash()


class InstallConfigTests(unittest.TestCase):
    def render_config(self, output, sub_urls):
        return subprocess.run(
            [
                sys.executable,
                str(RENDER_CONFIG),
                "--template",
                str(ROOT / "config" / "config.template.yaml"),
                "--output",
                str(output),
                "--public-ip",
                "192.0.2.10",
                "--socks-port",
                "1080",
                "--socks-user",
                "test-user",
                "--socks-pass",
                "test-pass",
                "--secret",
                "test-secret",
                "--sub-urls",
                sub_urls,
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_fresh_config_has_no_subscription_providers_even_with_sub_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "config.yaml"

            self.render_config(
                output,
                "airport|https://example.com/one.yaml,https://example.com/two.yaml",
            )

            cfg = yaml.safe_load(output.read_text(encoding="utf-8"))
            self.assertEqual({}, cfg["proxy-providers"])
            for group in cfg["proxy-groups"]:
                if "use" in group:
                    self.assertEqual([], group["use"], group["name"])
            self.assertFalse((root / "custom.yaml").exists())

    def test_sub_url_parser_preserves_named_and_generated_names(self):
        self.assertEqual(
            [
                ("airport", "https://example.com/one.yaml"),
                ("sub2", "https://example.com/two.yaml"),
                ("badname", "http://example.com/three.yaml"),
            ],
            render_config.parse_sub_urls(
                "airport|https://example.com/one.yaml,"
                "https://example.com/two.yaml,"
                "bad name|http://example.com/three.yaml"
            ),
        )

    def test_installer_imports_subscriptions_only_after_services_are_active(self):
        source = INSTALL_SH.read_text(encoding="utf-8")
        self.assertLess(source.index("start_services"), source.index("import_initial_subscriptions"))
        self.assertLess(source.index("import_initial_subscriptions"), source.index("print_summary"))

    def test_installer_does_not_stop_unrelated_port_owners_or_nginx_sites(self):
        source = COMMON_SH.read_text(encoding="utf-8")
        self.assertNotIn('fuser -k "${PANEL_PORT}/tcp"', source)
        self.assertNotIn("rm -f /etc/nginx/sites-enabled/default", source)

    def test_nginx_websocket_map_uses_project_specific_variable(self):
        common = COMMON_SH.read_text(encoding="utf-8")
        nginx = (ROOT / "panel" / "nginx.conf.template").read_text(encoding="utf-8")
        self.assertIn("$mihomo_connection_upgrade", common)
        self.assertIn("$mihomo_connection_upgrade", nginx)
        self.assertNotIn("$connection_upgrade", nginx)

    @unittest.skipUnless(BASH, "bash is required to verify environment escaping")
    def test_generated_environment_file_is_safe_to_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_file = root / "env"
            marker = root / "executed"
            malicious = f"literal $(touch '{marker.as_posix()}')"
            script = f"""
source '{COMMON_SH.as_posix()}'
ENV_FILE='{env_file.as_posix()}'
PUBLIC_IP='192.0.2.10'
PANEL_PORT=9090
PANEL_API_PORT=9092
SOCKS_PORT=1080
SOCKS_USER='user with spaces'
SOCKS_PASS={shlex.quote(malicious)}
MIHOMO_SECRET='secret with spaces and $HOME'
generate_secrets
unset PUBLIC_IP PANEL_PORT PANEL_API_PORT SOCKS_PORT SOCKS_USER SOCKS_PASS MIHOMO_SECRET
source "$ENV_FILE"
[[ "$SOCKS_PASS" == {shlex.quote(malicious)} ]]
[[ "$MIHOMO_SECRET" == 'secret with spaces and $HOME' ]]
[[ ! -e '{marker.as_posix()}' ]]
"""
            result = subprocess.run([BASH, "-c", script], capture_output=True, text=True)

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_systemd_unit_does_not_contain_management_secret(self):
        service = PANEL_API_SERVICE.read_text(encoding="utf-8")
        common = COMMON_SH.read_text(encoding="utf-8")
        self.assertNotIn("__MIHOMO_SECRET__", service)
        self.assertNotIn("Environment=MIHOMO_SECRET", service)
        self.assertIn("source /root/mihomo-gateway/env", service)
        self.assertNotIn("__MIHOMO_SECRET__", common)

    @unittest.skipUnless(BASH, "bash is required to verify installer output")
    def test_install_summary_does_not_print_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            credentials = Path(tmp) / "credentials.txt"
            script = f"""
source '{COMMON_SH.as_posix()}'
CRED_FILE='{credentials.as_posix()}'
PUBLIC_IP='192.0.2.10'
PANEL_PORT=9090
SOCKS_PORT=1080
SOCKS_USER='summary-user'
SOCKS_PASS='summary-password-secret'
MIHOMO_SECRET='summary-management-secret'
RUNTIME_ROOT='/etc/mihomo'
ENV_FILE='/root/mihomo-gateway/env'
print_summary
"""
            result = subprocess.run([BASH, "-c", script], capture_output=True, text=True)

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertNotIn("summary-password-secret", result.stdout)
            self.assertNotIn("summary-management-secret", result.stdout)
            self.assertIn(str(credentials).replace("\\", "/"), result.stdout)

    def test_readme_one_click_command_uses_repository_bootstrap(self):
        source = README.read_text(encoding="utf-8")
        self.assertIn(
            "https://raw.githubusercontent.com/gthubtom1/mihomo-gateway/main/bootstrap.sh",
            source,
        )
        self.assertNotIn("mihomo-gateway/main/install.sh | bash", source)

    @unittest.skipUnless(BASH, "bash is required to exercise the bootstrap script")
    def test_bootstrap_downloads_archive_and_launches_extracted_installer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = root / "mihomo-gateway-test"
            payload.mkdir()
            installer = payload / "install.sh"
            installer.write_text(
                "#!/usr/bin/env bash\nprintf 'bootstrap reached installer: %s\\n' \"${BOOTSTRAP_TEST_VALUE:-missing}\"\n",
                encoding="utf-8",
            )
            archive = root / "source.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                bundle.add(payload, arcname=payload.name)

            env = os.environ.copy()
            env["MIHOMO_GATEWAY_ARCHIVE_URL"] = archive.as_uri()
            env["BOOTSTRAP_TEST_VALUE"] = "preserved"
            result = subprocess.run(
                [BASH, "-c", f"bash '{BOOTSTRAP_SH.as_posix()}'"],
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("bootstrap reached installer: preserved", result.stdout)

    @unittest.skipUnless(BASH, "bash is required to exercise installer failure handling")
    def test_initial_subscription_api_failure_fails_install_clearly(self):
        script = f"""
source '{COMMON_SH.as_posix()}'
SUB_URLS='first|https://example.com/one.yaml,second|https://example.com/two.yaml'

python3() {{
  printf '%s\n' \
    '{{"name":"first","url":"https://example.com/one.yaml"}}' \
    '{{"name":"second","url":"https://example.com/two.yaml"}}'
}}

jq() {{
  local selector input
  selector="${{@: -1}}"
  input="$(cat)"
  if [[ "$selector" == ".name" ]]; then
    printf '%s\n' "$input" | sed -E 's/.*"name":"([^"]+)".*/\\1/'
  else
    printf '%s\n' "$input" | sed -E 's/.*"url":"([^"]+)".*/\\1/'
  fi
}}

mihomo-gateway() {{
  [[ "$3" != "second" ]]
}}

import_initial_subscriptions
printf 'install incorrectly continued\n'
"""
        result = subprocess.run(
            [BASH, "-c", script],
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(0, result.returncode)
        combined = result.stdout + result.stderr
        self.assertIn("failed to import initial subscription: second", combined)
        self.assertNotIn("install incorrectly continued", combined)

    def test_subscription_parser_can_emit_records_for_the_installer(self):
        result = subprocess.run(
            [
                sys.executable,
                str(RENDER_CONFIG),
                "--emit-sub-urls",
                "named|https://example.com/one.yaml,https://example.com/two.yaml",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(
            [
                {"name": "named", "url": "https://example.com/one.yaml"},
                {"name": "sub2", "url": "https://example.com/two.yaml"},
            ],
            [json.loads(line) for line in result.stdout.splitlines()],
        )

    @unittest.skipUnless(BASH, "bash is required to exercise the gateway CLI")
    def test_provider_cli_json_encodes_subscription_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = root / "curl-args.txt"
            url = 'https://example.com/sub?label="quoted"&path=\\value'
            env = os.environ.copy()
            env["CAPTURE_FILE"] = str(capture)
            env["MIHOMO_SECRET"] = "test-secret"
            env["TEST_PYTHON"] = Path(sys.executable).as_posix()
            command = (
                "python3() { \"$TEST_PYTHON\" \"$@\"; }; "
                "curl() { printf '%s\\n' \"$@\" > \"$CAPTURE_FILE\"; printf '{\"ok\":true}\\n'; }; "
                "systemctl() { return 0; }; "
                f"set -- provider add airport {shlex.quote(url)} 3600; "
                f"source {shlex.quote(GATEWAY_CLI.as_posix())}"
            )
            result = subprocess.run(
                [BASH, "-c", command],
                capture_output=True,
                text=True,
                env=env,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            args = capture.read_text(encoding="utf-8").splitlines()
            payload = args[args.index("-d") + 1]
            self.assertEqual(url, json.loads(payload)["url"])

    @unittest.skipUnless(BASH, "bash is required to exercise provider storage cleanup")
    def test_reinstall_backs_up_and_removes_stale_provider_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "runtime"
            backups = root / "backups"
            providers = runtime / "providers"
            providers.mkdir(parents=True)
            config = runtime / "config.yaml"
            config_bytes = b"proxy-providers:\n  legacy:\n    url: https://example.com/sub\n"
            config.write_bytes(config_bytes)
            stale = providers / "旧机场.yaml"
            stale_bytes = "proxies:\n  - name: 旧节点\n    type: direct\n".encode("utf-8")
            stale.write_bytes(stale_bytes)
            keep = providers / "notes.txt"
            keep.write_text("keep\n", encoding="utf-8")

            script = f"""
source '{COMMON_SH.as_posix()}'
RUNTIME_ROOT='{runtime.as_posix()}'
BACKUP_ROOT='{backups.as_posix()}'
prepare_provider_storage
[[ ! -e '{stale.as_posix()}' ]]
[[ -e '{keep.as_posix()}' ]]
find '{backups.as_posix()}' -type f -name '旧机场.yaml' | grep -q .
find '{backups.as_posix()}' -type f -name 'config.yaml' | grep -q .
"""
            result = subprocess.run([BASH, "-c", script], capture_output=True, text=True)

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            backed_config = next(backups.rglob("config.yaml"))
            backed_provider = next(backups.rglob("旧机场.yaml"))
            self.assertEqual(config_bytes, backed_config.read_bytes())
            self.assertEqual(stale_bytes, backed_provider.read_bytes())


if __name__ == "__main__":
    unittest.main()
