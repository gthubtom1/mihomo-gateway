#!/usr/bin/env python3
"""Local management API for SOCKS listeners and subscription providers."""
import json
import os
import re
import secrets
import subprocess
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

HOST_PUBLIC = os.environ.get("PUBLIC_IP", "127.0.0.1")
CONFIG = Path(os.environ.get("MIHOMO_CONFIG", "/etc/mihomo/config.yaml"))
BACKUP_DIR = Path(os.environ.get("MIHOMO_BACKUP_DIR", "/root/mihomo-backups"))
MIHOMO_SECRET = os.environ.get("MIHOMO_SECRET", "")
LISTEN_HOST = os.environ.get("PANEL_API_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("PANEL_API_PORT", "9092"))
PROTECTED_PROVIDERS = set(
    x.strip() for x in os.environ.get("PROTECTED_PROVIDERS", "custom").split(",") if x.strip()
)


def load_cfg():
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}


def save_cfg(cfg):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG.exists():
        bak = BACKUP_DIR / f"config.yaml.{time.strftime('%Y%m%d-%H%M%S')}"
        bak.write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    CONFIG.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")


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
    for name, p in (cfg.get("proxy-providers") or {}).items():
        rows.append({
            "name": name,
            "type": p.get("type"),
            "url": p.get("url") or "",
            "path": p.get("path") or "",
            "interval": p.get("interval"),
            "protected": name in PROTECTED_PROVIDERS,
        })
    rows.sort(key=lambda x: x["name"])
    return rows


def validate_and_restart():
    p = subprocess.run(["mihomo", "-t", "-d", str(CONFIG.parent)], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stdout or "") + (p.stderr or ""))
    subprocess.check_call(["systemctl", "restart", "mihomo"])
    for _ in range(50):
        try:
            urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:9091/version",
                headers={"Authorization": f"Bearer {MIHOMO_SECRET}"},
            ), timeout=2)
            break
        except Exception:
            time.sleep(0.25)


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
        if g.get("name") == "自定义" and use == ["custom"]:
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
                    return body, final, status
                last_err = RuntimeError("empty subscription body")
        except Exception as e:
            last_err = e
            msg = str(e)
            # retry only on 403/401/429/5xx-ish; other errors still try next UA once
            continue
    raise RuntimeError(last_err)

def add_provider(name, url, interval=3600):
    cfg = load_cfg()
    name = (name or "").strip()
    url = (url or "").strip()
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
    warning = ""
    try:
        body, final_url, status = fetch_subscription(url)
        if not body:
            raise RuntimeError("empty subscription body")
        if final_url and final_url != url:
            url = final_url
        head = body[:400].lstrip().lower()
        if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
            raise RuntimeError(
                f"subscription returned HTML (HTTP {status}); URL/token may be invalid"
            )
    except Exception as e:
        msg = str(e)
        # Airports often block VPS/datacenter IPs or non-client UAs with 403.
        # Still write the provider and let mihomo pull it with its own client.
        if any(x in msg for x in ("403", "401", "429", "Forbidden", "Unauthorized")):
            warning = (
                f"precheck failed ({msg}); provider saved, mihomo will retry pull. "
                "If nodes stay empty, the airport may block this VPS IP."
            )
        else:
            raise RuntimeError(f"fetch subscription failed: {e}")
    providers[name] = {
        "type": "http",
        "url": url,
        "interval": interval,
        "path": f"./providers/{name}.yaml",
        "health-check": {
            "enable": True,
            "interval": 600,
            "url": "https://www.gstatic.com/generate_204",
        },
    }
    attach_provider_to_groups(cfg, name)
    save_cfg(cfg)
    validate_and_restart()
    out = {
        "name": name,
        "type": "http",
        "url": url,
        "interval": interval,
        "path": f"./providers/{name}.yaml",
    }
    if warning:
        out["warning"] = warning
    return out


def del_provider(name):
    cfg = load_cfg()
    name = (name or "").strip()
    if name in PROTECTED_PROVIDERS:
        raise RuntimeError(f"protected provider cannot delete: {name}")
    providers = cfg.get("proxy-providers") or {}
    if name not in providers:
        raise RuntimeError("provider not found")
    del providers[name]
    cfg["proxy-providers"] = providers
    detach_provider_from_groups(cfg, name)
    p = Path(f"/etc/mihomo/providers/{name}.yaml")
    if p.exists():
        try:
            p.unlink()
        except Exception:
            pass
    save_cfg(cfg)
    validate_and_restart()
    return {"name": name, "deleted": True}


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
