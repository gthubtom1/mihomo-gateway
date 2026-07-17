#!/usr/bin/env python3
"""Local management API for SOCKS listeners and subscription providers."""
import json
import os
import re
import secrets
import subprocess
import time
import urllib.request
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

HOST_PUBLIC = os.environ.get("PUBLIC_IP", "127.0.0.1")
CONFIG = Path(os.environ.get("MIHOMO_CONFIG", "/etc/mihomo/config.yaml"))
PROVIDERS_DIR = Path(os.environ.get("MIHOMO_PROVIDERS_DIR", str(CONFIG.parent / "providers")))
BACKUP_DIR = Path(os.environ.get("MIHOMO_BACKUP_DIR", "/root/mihomo-backups"))
MIHOMO_SECRET = os.environ.get("MIHOMO_SECRET", "")
LISTEN_HOST = os.environ.get("PANEL_API_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("PANEL_API_PORT", "9092"))
PROTECTED_PROVIDERS = set(
    x.strip() for x in os.environ.get("PROTECTED_PROVIDERS", "").split(",") if x.strip()
)


def load_cfg():
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}


def save_cfg(cfg):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG.exists():
        bak = BACKUP_DIR / f"config.yaml.{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}"
        bak.write_bytes(CONFIG.read_bytes())
    raw = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False).encode("utf-8")
    _atomic_write(CONFIG, raw)


def _atomic_write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(raw)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _restore_config(raw):
    _atomic_write(CONFIG, raw)


def _safe_provider_path(name):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name or ""):
        raise RuntimeError("name invalid: use A-Za-z0-9_- (1-32)")
    return PROVIDERS_DIR / f"{name}.yaml"


def _find_orphan_path(name):
    name = (name or "").strip()
    if not name or len(name) > 128 or name in {".", ".."}:
        return None
    if "/" in name or "\\" in name:
        return None
    if not PROVIDERS_DIR.exists():
        return None
    for path in PROVIDERS_DIR.glob("*.y*ml"):
        if path.stem == name and path.resolve().parent == PROVIDERS_DIR.resolve():
            return path
    return None


def _provider_path(name, provider=None):
    raw = (provider or {}).get("path") or f"./providers/{name}.yaml"
    path = Path(raw)
    if not path.is_absolute():
        path = CONFIG.parent / path
    path = path.resolve()
    root = PROVIDERS_DIR.resolve()
    if path.parent != root:
        raise RuntimeError(f"unsafe provider path: {raw}")
    return path


def _provider_node_count(path):
    if not path.exists():
        return 0
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
        proxies = data.get("proxies") if isinstance(data, dict) else None
        return len(proxies) if isinstance(proxies, list) else 0
    except Exception:
        return 0


def _backup_provider_file(path):
    if not path.exists():
        return None
    target_dir = BACKUP_DIR / "providers"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}"
    target.write_bytes(path.read_bytes())
    os.chmod(target, 0o600)
    return target


def group_names(cfg):
    return [g.get("name") for g in (cfg.get("proxy-groups") or []) if g.get("name")]


def list_socks(cfg):
    rows = []
    for lis in cfg.get("listeners") or []:
        if lis.get("type") != "socks":
            continue
        users = lis.get("users") or []
        user = (users[0] or {}).get("username", "") if users else ""
        pw = (users[0] or {}).get("password", "") if users else ""
        port = lis.get("port")
        rows.append({
            "name": lis.get("name"),
            "port": port,
            "group": lis.get("proxy"),
            "user": user,
            "password": pw,
            "url": f"socks5://{user}:{pw}@{HOST_PUBLIC}:{port}" if user and pw and port else "",
        })
    rows.sort(key=lambda x: int(x.get("port") or 0))
    return rows


def list_providers(cfg):
    rows = []
    configured_paths = set()
    for name, p in (cfg.get("proxy-providers") or {}).items():
        try:
            path = _provider_path(name, p)
            configured_paths.add(path)
            exists = path.exists()
            nodes = _provider_node_count(path)
        except RuntimeError:
            exists = False
            nodes = 0
        rows.append({
            "name": name,
            "type": p.get("type"),
            "url": p.get("url") or "",
            "path": p.get("path") or "",
            "interval": p.get("interval"),
            "protected": name in PROTECTED_PROVIDERS,
            "status": "active" if exists else "missing",
            "nodes": nodes,
        })
    if PROVIDERS_DIR.exists():
        for path in sorted(PROVIDERS_DIR.glob("*.y*ml")):
            resolved = path.resolve()
            if resolved in configured_paths:
                continue
            rows.append({
                "name": path.stem,
                "type": "orphan",
                "url": "",
                "path": str(path),
                "interval": None,
                "protected": False,
                "status": "orphan",
                "nodes": _provider_node_count(path),
            })
    rows.sort(key=lambda x: x["name"])
    return rows


def validate_and_restart():
    p = subprocess.run(["mihomo", "-t", "-d", str(CONFIG.parent)], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stdout or "") + (p.stderr or ""))
    subprocess.check_call(["systemctl", "restart", "mihomo"])
    last_error = None
    for _ in range(50):
        try:
            urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:9091/version",
                headers={"Authorization": f"Bearer {MIHOMO_SECRET}"},
            ), timeout=2)
            return
        except Exception as e:
            last_error = e
            time.sleep(0.25)
    raise RuntimeError(f"mihomo did not become ready after restart: {last_error}")


def ufw_allow(port):
    try:
        st = subprocess.check_output(["ufw", "status"], text=True)
        if "Status: active" in st:
            subprocess.call(["ufw", "allow", f"{port}/tcp"])
    except Exception:
        pass


def ufw_delete(port):
    try:
        st = subprocess.check_output(["ufw", "status"], text=True)
        if "Status: active" in st:
            subprocess.call(["ufw", "--force", "delete", "allow", f"{port}/tcp"])
    except Exception:
        pass


def default_auth(listeners):
    for lis in listeners:
        users = lis.get("users") or []
        if users and users[0].get("username"):
            return users[0]["username"], users[0]["password"]
    return f"socks_{secrets.token_hex(3)}", secrets.token_urlsafe(18)


def add_socks(port, group, name=None, user=None, password=None):
    cfg = load_cfg()
    port = int(port)
    if port < 1024 or port > 65535:
        raise RuntimeError("port must be 1024-65535")
    if group not in group_names(cfg):
        raise RuntimeError(f"group not found: {group}")
    listeners = cfg.setdefault("listeners", [])
    name = name or f"socks-{port}"
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name):
        raise RuntimeError("invalid name")
    for lis in listeners:
        if lis.get("type") == "socks" and int(lis.get("port") or 0) == port:
            raise RuntimeError(f"port exists: {port}")
        if lis.get("name") == name:
            raise RuntimeError(f"name exists: {name}")
    duser, dpw = default_auth(listeners)
    user = user or duser
    password = password or dpw
    listeners.append({
        "name": name,
        "type": "socks",
        "port": port,
        "listen": "0.0.0.0",
        "users": [{"username": user, "password": password}],
        "proxy": group,
    })
    save_cfg(cfg)
    validate_and_restart()
    ufw_allow(port)
    return {
        "name": name,
        "port": port,
        "group": group,
        "user": user,
        "password": password,
        "url": f"socks5://{user}:{password}@{HOST_PUBLIC}:{port}",
    }


def del_socks(port=None, name=None):
    cfg = load_cfg()
    listeners = cfg.get("listeners") or []
    kept, removed = [], None
    for lis in listeners:
        hit = False
        if lis.get("type") == "socks":
            if port is not None and int(lis.get("port") or 0) == int(port):
                hit = True
            if name and lis.get("name") == name:
                hit = True
        if hit:
            removed = lis
        else:
            kept.append(lis)
    if not removed:
        raise RuntimeError("not found")
    cfg["listeners"] = kept
    save_cfg(cfg)
    validate_and_restart()
    if removed.get("port") is not None:
        ufw_delete(int(removed["port"]))
    return removed


def attach_provider_to_groups(cfg, provider_name):
    for g in cfg.get("proxy-groups") or []:
        use = g.get("use")
        if not isinstance(use, list):
            continue
        if provider_name not in use:
            use.append(provider_name)
            g["use"] = use


def detach_provider_from_groups(cfg, provider_name):
    for g in cfg.get("proxy-groups") or []:
        use = g.get("use")
        if isinstance(use, list) and provider_name in use:
            g["use"] = [x for x in use if x != provider_name]


def fetch_subscription(url, timeout=25):
    """Fetch subscription with client-like headers and UA fallbacks.

    Many airports return 403 to bare Python UA / short UA.
    """
    uas = [
        # common clash clients
        "clash.meta",
        "ClashMeta/1.19.0",
        "clash-verge/v2.0.0",
        "ClashForWindows/0.20.39",
        "mihomo/1.19.0",
        # browser fallback for strict CDNs
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ]
    last_err = None
    for ua in uas:
        headers = {
            "User-Agent": ua,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read(2 * 1024 * 1024)  # 2MB probe max for precheck
                final = getattr(r, "geturl", lambda: url)()
                status = getattr(r, "status", 200)
                if body:
                    return body, final, status, ua
                last_err = RuntimeError("empty subscription body")
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(last_err)


def normalize_subscription(body):
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise RuntimeError(f"subscription is not UTF-8 Clash YAML: {e}")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise RuntimeError(f"subscription is not valid Clash YAML: {e}")
    if not isinstance(data, dict):
        raise RuntimeError(
            "subscription is not Clash/Mihomo YAML; select the Clash subscription URL"
        )
    proxies = data.get("proxies")
    if not isinstance(proxies, list) or not proxies:
        raise RuntimeError(
            "subscription contains no proxies; select the Clash/Mihomo subscription URL"
        )
    for index, proxy in enumerate(proxies, 1):
        if not isinstance(proxy, dict) or not proxy.get("name") or not proxy.get("type"):
            raise RuntimeError(f"invalid proxy at item {index}: name/type required")
    normalized = yaml.safe_dump(
        {"proxies": proxies}, allow_unicode=True, sort_keys=False
    ).encode("utf-8")
    return normalized, len(proxies)


def add_provider(name, url, interval=3600):
    cfg = load_cfg()
    name = (name or "").strip()
    url = unescape((url or "").strip())
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
        raise RuntimeError("name invalid: use A-Za-z0-9_- (1-32)")
    if name in PROTECTED_PROVIDERS:
        raise RuntimeError(f"protected provider: {name}")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise RuntimeError("url must start with http:// or https://")
    interval = max(300, int(interval or 3600))
    providers = cfg.setdefault("proxy-providers", {})
    if name in providers:
        raise RuntimeError(f"provider exists: {name}")
    cache_path = _safe_provider_path(name)
    if cache_path.exists():
        raise RuntimeError(
            f"orphan provider file exists: {cache_path.name}; delete it first"
        )
    try:
        body, final_url, _status, user_agent = fetch_subscription(url)
        if final_url and final_url != url:
            url = final_url
        normalized, node_count = normalize_subscription(body)
    except Exception as e:
        raise RuntimeError(f"fetch subscription failed: {e}")
    original_config = CONFIG.read_bytes()
    providers[name] = {
        "type": "http",
        "url": url,
        "interval": interval,
        "path": f"./providers/{name}.yaml",
        "header": {"User-Agent": [user_agent]},
        "health-check": {
            "enable": True,
            "interval": 600,
            "url": "https://www.gstatic.com/generate_204",
        },
    }
    attach_provider_to_groups(cfg, name)
    try:
        _atomic_write(cache_path, normalized)
        save_cfg(cfg)
        validate_and_restart()
    except Exception:
        _restore_config(original_config)
        cache_path.unlink(missing_ok=True)
        try:
            validate_and_restart()
        except Exception:
            pass
        raise
    return {
        "name": name,
        "type": "http",
        "url": url,
        "interval": interval,
        "path": f"./providers/{name}.yaml",
        "nodes": node_count,
    }


def del_provider(name):
    cfg = load_cfg()
    name = (name or "").strip()
    if name in PROTECTED_PROVIDERS:
        raise RuntimeError(f"protected provider cannot delete: {name}")
    providers = cfg.get("proxy-providers") or {}
    if name not in providers:
        orphan = _find_orphan_path(name)
        if orphan is None:
            raise RuntimeError("provider not found")
        backup = _backup_provider_file(orphan)
        orphan.unlink()
        return {
            "name": name,
            "deleted": True,
            "orphan": True,
            "backup": str(backup) if backup else "",
        }
    original_config = CONFIG.read_bytes()
    provider = providers[name]
    path = _provider_path(name, provider)
    del providers[name]
    cfg["proxy-providers"] = providers
    detach_provider_from_groups(cfg, name)
    try:
        save_cfg(cfg)
        validate_and_restart()
    except Exception:
        _restore_config(original_config)
        try:
            validate_and_restart()
        except Exception:
            pass
        raise
    backup = _backup_provider_file(path)
    path.unlink(missing_ok=True)
    return {
        "name": name,
        "deleted": True,
        "orphan": False,
        "backup": str(backup) if backup else "",
    }


class H(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def _auth_ok(self):
        if not MIHOMO_SECRET:
            return False
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            return auth[7:].strip() == MIHOMO_SECRET
        return (self.headers.get("X-Secret") or "") == MIHOMO_SECRET

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/healthz":
            return self._json(200, {"ok": True})
        if not u.path.startswith("/panel-api/"):
            return self._json(404, {"error": "not found"})
        if not self._auth_ok():
            return self._json(401, {"error": "unauthorized"})
        cfg = load_cfg()
        if u.path == "/panel-api/socks":
            return self._json(200, {"socks": list_socks(cfg), "groups": group_names(cfg)})
        if u.path == "/panel-api/providers":
            return self._json(200, {"providers": list_providers(cfg)})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if not self._auth_ok():
            return self._json(401, {"error": "unauthorized"})
        try:
            body = self._body()
            if u.path == "/panel-api/socks":
                return self._json(200, add_socks(
                    body.get("port"), body.get("group"),
                    name=body.get("name"), user=body.get("user"), password=body.get("password"),
                ))
            if u.path == "/panel-api/providers":
                return self._json(200, add_provider(
                    body.get("name"), body.get("url"), interval=body.get("interval", 3600),
                ))
            return self._json(404, {"error": "not found"})
        except Exception as e:
            return self._json(400, {"error": str(e)})

    def do_DELETE(self):
        u = urlparse(self.path)
        if not self._auth_ok():
            return self._json(401, {"error": "unauthorized"})
        qs = parse_qs(u.query)
        try:
            if u.path == "/panel-api/socks":
                removed = del_socks(port=qs.get("port", [None])[0], name=qs.get("name", [None])[0])
                return self._json(200, {"ok": True, "removed": removed.get("name"), "port": removed.get("port")})
            if u.path == "/panel-api/providers":
                return self._json(200, del_provider(qs.get("name", [None])[0]))
            return self._json(404, {"error": "not found"})
        except Exception as e:
            return self._json(400, {"error": str(e)})

    def log_message(self, *args):
        return


if __name__ == "__main__":
    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), H)
    print(f"gateway api on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    httpd.serve_forever()
