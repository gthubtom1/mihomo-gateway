#!/usr/bin/env python3
"""Local management API for SOCKS listeners and subscription providers."""
import base64
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.request
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlparse, urlsplit

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
MUTATION_LOCK = threading.RLock()
MAX_API_BODY = 64 * 1024


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
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _restore_config(raw):
    _atomic_write(CONFIG, raw)


def _safe_provider_path(name):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name or ""):
        raise RuntimeError("name invalid: use A-Za-z0-9_- (1-32)")
    return PROVIDERS_DIR / f"{name}.yaml"


def _find_orphan_path(name):
    name = "" if name is None else str(name)
    if not name or len(name) > 128 or name in {".", ".."}:
        return None
    if "/" in name or "\\" in name:
        return None
    if not PROVIDERS_DIR.exists():
        return None
    for path in _provider_files():
        if path.stem == name and path.resolve().parent == PROVIDERS_DIR.resolve():
            return path
    return None


def _find_orphan_file(filename):
    filename = "" if filename is None else str(filename)
    if not filename or len(filename) > 255 or filename in {".", ".."}:
        return None
    if "/" in filename or "\\" in filename:
        return None
    if Path(filename).suffix.lower() not in {".yaml", ".yml"}:
        return None
    for path in _provider_files():
        if path.name == filename and path.resolve().parent == PROVIDERS_DIR.resolve():
            return path
    return None


def _resource_id(kind, value):
    token = base64.urlsafe_b64encode(str(value).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{kind}:{token}"


def _decode_resource_id(value):
    if not value or ":" not in value:
        raise RuntimeError("invalid provider id")
    kind, token = value.split(":", 1)
    if kind not in {"provider", "orphan"} or not token:
        raise RuntimeError("invalid provider id")
    try:
        padding = "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(token + padding).decode("utf-8")
    except Exception as e:
        raise RuntimeError("invalid provider id") from e
    return kind, decoded


def _display_url(raw):
    try:
        parsed = urlsplit(raw or "")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return f"{parsed.scheme}://{host}/..."
    except Exception:
        return ""


def _resolve_safe_subscription_url(raw):
    if not isinstance(raw, str) or not raw or len(raw) > 8192:
        raise RuntimeError("invalid subscription URL")
    if any(char in raw for char in "\r\n\t\\"):
        raise RuntimeError("invalid subscription URL")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as e:
        raise RuntimeError(f"invalid subscription URL: {e}") from e
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("subscription URL must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError("subscription URL must not contain user information")

    service_port = port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = socket.getaddrinfo(
            parsed.hostname,
            service_port,
            type=socket.SOCK_STREAM,
        )
    except OSError as e:
        raise RuntimeError(f"cannot resolve subscription host: {e}") from e
    if not addresses:
        raise RuntimeError("cannot resolve subscription host")

    public_ips = []
    for address in addresses:
        raw_ip = address[4][0].split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError as e:
            raise RuntimeError(f"invalid subscription host address: {raw_ip}") from e
        if not ip.is_global:
            raise RuntimeError("subscription URL must resolve only to a public address")
        if raw_ip not in public_ips:
            public_ips.append(raw_ip)
    return parsed, public_ips


def _assert_safe_subscription_url(raw):
    _resolve_safe_subscription_url(raw)
    return raw


class SubscriptionHTTPError(RuntimeError):
    def __init__(self, status, retry_after=None):
        super().__init__(f"HTTP Error {status}")
        self.status = status
        self.retry_after = retry_after


class SubscriptionAttemptError(RuntimeError):
    def __init__(self, last_error, statuses):
        super().__init__(str(last_error))
        self.last_error = last_error
        self.statuses = statuses
        self.status = getattr(last_error, "status", None)
        self.retry_after = getattr(last_error, "retry_after", None)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, address, port, server_hostname, timeout, connector=None):
        super().__init__(
            address,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._tls_server_hostname = server_hostname
        self._connector = connector

    def connect(self):
        if self._connector:
            self.sock = self._connector(self.host, self.port, self.timeout)
        else:
            self.sock = self._create_connection(
                (self.host, self.port),
                self.timeout,
                self.source_address,
            )
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(
            self.sock,
            server_hostname=self._tls_server_hostname,
        )


class _SubscriptionResponse:
    def __init__(self, response, connection, final_url):
        self._response = response
        self._connection = connection
        self._final_url = final_url
        self.status = response.status

    def read(self, limit=-1):
        return self._response.read(limit)

    def getheader(self, name, default=None):
        return self._response.getheader(name, default)

    def geturl(self):
        return self._final_url

    def close(self):
        try:
            self._response.close()
        finally:
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
        return False


class _ConnectorHTTPConnection(http.client.HTTPConnection):
    def __init__(self, address, port, timeout, connector):
        super().__init__(address, port, timeout=timeout)
        self._connector = connector

    def connect(self):
        self.sock = self._connector(self.host, self.port, self.timeout)


def _recv_exact(sock, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise RuntimeError("SOCKS5 proxy closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _socks5_connect(proxy_host, proxy_port, target_ip, target_port, user, password, timeout):
    sock = socket.create_connection((proxy_host, int(proxy_port)), timeout=timeout)
    try:
        use_auth = bool(user and password)
        methods = b"\x02" if use_auth else b"\x00"
        sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
        version, method = _recv_exact(sock, 2)
        if version != 5 or method == 0xFF:
            raise RuntimeError("SOCKS5 proxy rejected authentication methods")
        if use_auth and method != 2:
            raise RuntimeError("SOCKS5 proxy rejected username/password authentication")
        if method == 2:
            username = str(user or "").encode("utf-8")
            secret = str(password or "").encode("utf-8")
            if len(username) > 255 or len(secret) > 255:
                raise RuntimeError("SOCKS5 credentials are too long")
            sock.sendall(
                b"\x01" + bytes([len(username)]) + username
                + bytes([len(secret)]) + secret
            )
            auth_version, auth_status = _recv_exact(sock, 2)
            if auth_version != 1 or auth_status != 0:
                raise RuntimeError("SOCKS5 proxy authentication failed")
        elif method != 0:
            raise RuntimeError("SOCKS5 proxy selected an unsupported method")

        address = ipaddress.ip_address(target_ip)
        atyp = 1 if address.version == 4 else 4
        sock.sendall(
            b"\x05\x01\x00" + bytes([atyp]) + address.packed
            + int(target_port).to_bytes(2, "big")
        )
        reply_version, reply_status, _reserved, reply_atyp = _recv_exact(sock, 4)
        if reply_version != 5 or reply_status != 0:
            raise RuntimeError(f"SOCKS5 proxy connection failed: {reply_status}")
        if reply_atyp == 1:
            _recv_exact(sock, 4)
        elif reply_atyp == 4:
            _recv_exact(sock, 16)
        elif reply_atyp == 3:
            _recv_exact(sock, _recv_exact(sock, 1)[0])
        else:
            raise RuntimeError("SOCKS5 proxy returned an invalid address type")
        _recv_exact(sock, 2)
        return sock
    except Exception:
        sock.close()
        raise


def _provider_proxy_names(path):
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
        proxies = data.get("proxies") if isinstance(data, dict) else None
        return {
            proxy.get("name") for proxy in (proxies or [])
            if isinstance(proxy, dict) and proxy.get("name")
        }
    except Exception:
        return set()


def _runtime_route_uses_provider(group_name, cfg, provider_names):
    if not group_name or not MIHOMO_SECRET or not provider_names:
        return False
    groups = {
        group.get("name") for group in (cfg.get("proxy-groups") or [])
        if group.get("name")
    }
    current = group_name
    for _ in range(12):
        if current == "DIRECT":
            return False
        if current not in groups:
            return current in provider_names
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:9091/proxies/{quote(current, safe='')}",
                headers={"Authorization": f"Bearer {MIHOMO_SECRET}"},
            )
            with urllib.request.urlopen(req, timeout=2) as response:
                selected = (json.loads(response.read().decode("utf-8")) or {}).get("now")
        except Exception:
            return False
        if not selected or selected == current:
            return False
        current = selected
    return False


def _subscription_proxy_connector():
    try:
        cfg = load_cfg()
    except Exception:
        return None
    providers = cfg.get("proxy-providers") or {}
    provider_names = set()
    for name, provider in providers.items():
        try:
            provider_names.update(_provider_proxy_names(_provider_path(name, provider)))
        except Exception:
            continue
    if not provider_names:
        return None

    for listener in cfg.get("listeners") or []:
        users = listener.get("users") or []
        if listener.get("type") != "socks" or not listener.get("port") or not users:
            continue
        user = users[0].get("username") or ""
        password = users[0].get("password") or ""
        if not user or not password:
            continue
        if not _runtime_route_uses_provider(listener.get("proxy"), cfg, provider_names):
            continue
        port = int(listener["port"])
        return lambda address, target_port, timeout: _socks5_connect(
            "127.0.0.1",
            port,
            address,
            target_port,
            user,
            password,
            timeout,
        )
    return None


def _open_pinned_once(url, headers, timeout, connector=None):
    parsed, addresses = _resolve_safe_subscription_url(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    target = parsed.path or "/"
    if parsed.query:
        target += f"?{parsed.query}"

    host = parsed.hostname
    host_header = f"[{host}]" if ":" in host else host
    default_port = 443 if parsed.scheme == "https" else 80
    if parsed.port and parsed.port != default_port:
        host_header = f"{host_header}:{parsed.port}"

    last_error = None
    for address in addresses:
        connection = None
        try:
            if parsed.scheme == "https":
                kwargs = {
                    "server_hostname": host,
                    "timeout": timeout,
                }
                if connector:
                    kwargs["connector"] = connector
                connection = _PinnedHTTPSConnection(address, port, **kwargs)
            elif connector:
                connection = _ConnectorHTTPConnection(
                    address,
                    port,
                    timeout=timeout,
                    connector=connector,
                )
            else:
                connection = http.client.HTTPConnection(address, port, timeout=timeout)
            connection.putrequest(
                "GET",
                target,
                skip_host=True,
                skip_accept_encoding=True,
            )
            connection.putheader("Host", host_header)
            for key, value in headers.items():
                if key.lower() != "host":
                    connection.putheader(key, value)
            connection.endheaders()
            response = connection.getresponse()
            return _SubscriptionResponse(response, connection, url)
        except Exception as e:
            last_error = e
            if connection is not None:
                connection.close()
    raise RuntimeError(f"subscription connection failed: {last_error}")


def _open_subscription(req, timeout, connector=None):
    url = req.full_url
    if connector and urlsplit(url).scheme != "https":
        raise RuntimeError("subscription proxy fallback requires HTTPS")
    headers = dict(req.header_items())
    redirect_statuses = {301, 302, 303, 307, 308}
    for redirect_count in range(6):
        response = _open_pinned_once(url, headers, timeout, connector=connector)
        if response.status in redirect_statuses:
            location = response.getheader("Location")
            if not location:
                response.close()
                raise RuntimeError("subscription redirect has no Location header")
            if redirect_count >= 5:
                response.close()
                raise RuntimeError("subscription has too many redirects")
            next_url = urljoin(url, location)
            response.close()
            if connector and urlsplit(next_url).scheme != "https":
                raise RuntimeError("subscription proxy fallback requires HTTPS redirects")
            _assert_safe_subscription_url(next_url)
            url = next_url
            continue
        if response.status >= 400:
            status = response.status
            retry_after = response.getheader("Retry-After")
            response.close()
            raise SubscriptionHTTPError(status, retry_after=retry_after)
        response._final_url = url
        return response
    raise RuntimeError("subscription has too many redirects")


def _provider_files():
    if not PROVIDERS_DIR.exists():
        return []
    return sorted(
        path for path in PROVIDERS_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in {".yaml", ".yml"}
    )


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
            "id": _resource_id("provider", name),
            "name": name,
            "type": p.get("type"),
            "display_url": _display_url(p.get("url")),
            "path": p.get("path") or "",
            "interval": p.get("interval"),
            "protected": name in PROTECTED_PROVIDERS,
            "status": "active" if exists else "missing",
            "nodes": nodes,
        })
    if PROVIDERS_DIR.exists():
        for path in _provider_files():
            resolved = path.resolve()
            if resolved in configured_paths:
                continue
            rows.append({
                "id": _resource_id("orphan", path.name),
                "name": path.stem,
                "type": "orphan",
                "display_url": "",
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


def _ufw_status():
    try:
        return subprocess.check_output(["ufw", "status"], text=True)
    except FileNotFoundError:
        return ""


def _ufw_has_rule(status, port):
    prefix = re.compile(rf"^\s*{re.escape(str(port))}/tcp(?:\s|\()")
    return any(prefix.search(line) and re.search(r"\bALLOW\b", line) for line in status.splitlines())


def ufw_allow(port):
    status = _ufw_status()
    if "Status: active" not in status or _ufw_has_rule(status, port):
        return False
    subprocess.check_call(
        ["ufw", "allow", f"{port}/tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True


def ufw_delete(port):
    status = _ufw_status()
    if "Status: active" not in status or not _ufw_has_rule(status, port):
        return False
    subprocess.check_call(
        ["ufw", "--force", "delete", "allow", f"{port}/tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True


def default_auth(listeners):
    for lis in listeners:
        users = lis.get("users") or []
        if users and users[0].get("username"):
            return users[0]["username"], users[0]["password"]
    return f"socks_{secrets.token_hex(3)}", secrets.token_urlsafe(18)


def add_socks(port, group, name=None, user=None, password=None):
    with MUTATION_LOCK:
        cfg = load_cfg()
        original_config = CONFIG.read_bytes()
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
        firewall_changed = False
        try:
            save_cfg(cfg)
            validate_and_restart()
            firewall_changed = ufw_allow(port)
        except Exception:
            _restore_config(original_config)
            try:
                validate_and_restart()
                if firewall_changed:
                    ufw_delete(port)
            except Exception:
                pass
            raise
        return {
            "name": name,
            "port": port,
            "group": group,
            "user": user,
            "password": password,
            "url": f"socks5://{user}:{password}@{HOST_PUBLIC}:{port}",
        }


def del_socks(port=None, name=None):
    with MUTATION_LOCK:
        cfg = load_cfg()
        original_config = CONFIG.read_bytes()
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
        removed_port = int(removed["port"]) if removed.get("port") is not None else None
        firewall_changed = False
        try:
            save_cfg(cfg)
            validate_and_restart()
            if removed_port is not None:
                firewall_changed = ufw_delete(removed_port)
        except Exception:
            _restore_config(original_config)
            try:
                validate_and_restart()
                if removed_port is not None and firewall_changed:
                    ufw_allow(removed_port)
            except Exception:
                pass
            raise
        return removed


def attach_provider_to_groups(cfg, provider_name):
    for g in cfg.get("proxy-groups") or []:
        use = g.get("use")
        if not isinstance(use, list):
            continue
        if provider_name not in use:
            use.append(provider_name)
            g["use"] = use
        if g.get("type") in {"url-test", "fallback"} and g.get("proxies") == ["DIRECT"]:
            g["proxies"] = []


def detach_provider_from_groups(cfg, provider_name):
    for g in cfg.get("proxy-groups") or []:
        use = g.get("use")
        if isinstance(use, list) and provider_name in use:
            g["use"] = [x for x in use if x != provider_name]
            if not g["use"] and not g.get("proxies"):
                g["proxies"] = ["DIRECT"]


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
    _assert_safe_subscription_url(url)

    def attempt(connector=None):
        last_error = None
        statuses = []
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
                with _open_subscription(req, timeout=timeout, connector=connector) as r:
                    final = getattr(r, "geturl", lambda: url)()
                    body = r.read(16 * 1024 * 1024 + 1)
                    if len(body) > 16 * 1024 * 1024:
                        raise RuntimeError("subscription exceeds 16 MiB limit")
                    status = getattr(r, "status", 200)
                    if not body:
                        raise RuntimeError("empty subscription body")
                    normalized, node_count = normalize_subscription(body)
                    return normalized, node_count, final, status, ua
            except Exception as e:
                last_error = e
                statuses.append(getattr(e, "status", None))
                if getattr(e, "status", None) == 429:
                    break
        last_error = last_error or RuntimeError("subscription fetch failed")
        raise SubscriptionAttemptError(last_error, statuses)

    try:
        return attempt()
    except Exception as direct_error:
        final_error = direct_error
        statuses = getattr(direct_error, "statuses", [])
        rate_limited = getattr(direct_error, "status", None) == 429
        all_forbidden = bool(statuses) and all(status == 403 for status in statuses)
        fallback_allowed = urlsplit(url).scheme == "https" and (rate_limited or all_forbidden)
        if fallback_allowed:
            connector = _subscription_proxy_connector()
            if connector:
                try:
                    return attempt(connector=connector)
                except Exception as proxy_error:
                    final_error = direct_error if rate_limited else proxy_error

        source_error = getattr(final_error, "last_error", final_error)
        if getattr(final_error, "status", None) == 429:
            retry_after = getattr(final_error, "retry_after", None)
            if retry_after and str(retry_after).isdigit():
                message = f"HTTP Error 429: rate limited; retry after {retry_after} seconds"
            elif retry_after:
                message = f"HTTP Error 429: rate limited; retry after {retry_after}"
            else:
                message = "HTTP Error 429: rate limited; wait before retrying"
            raise RuntimeError(message) from source_error
        raise RuntimeError(source_error) from source_error


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
    name = (name or "").strip()
    url = unescape((url or "").strip())
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
        raise RuntimeError("name invalid: use A-Za-z0-9_- (1-32)")
    if name in PROTECTED_PROVIDERS:
        raise RuntimeError(f"protected provider: {name}")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise RuntimeError("url must start with http:// or https://")
    interval = max(300, int(interval or 3600))
    try:
        normalized, node_count, _final_url, _status, user_agent = fetch_subscription(url)
    except Exception as e:
        raise RuntimeError(f"fetch subscription failed: {e}")
    with MUTATION_LOCK:
        cfg = load_cfg()
        providers = cfg.setdefault("proxy-providers", {})
        if name in providers:
            raise RuntimeError(f"provider exists: {name}")
        cache_path = _safe_provider_path(name)
        if cache_path.exists():
            raise RuntimeError(
                f"orphan provider file exists: {cache_path.name}; delete it first"
            )
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
            "display_url": _display_url(url),
            "interval": interval,
            "path": f"./providers/{name}.yaml",
            "nodes": node_count,
        }


def del_provider(name=None, provider_id=None):
    with MUTATION_LOCK:
        cfg = load_cfg()
        providers = cfg.get("proxy-providers") or {}
        kind = None
        target = None
        if provider_id:
            kind, target = _decode_resource_id(provider_id)
        else:
            raw_name = "" if name is None else str(name)
            target = raw_name if raw_name in providers else raw_name.strip()
            kind = "provider" if target in providers else "orphan"

        if kind == "orphan":
            orphan = _find_orphan_file(target) if provider_id else _find_orphan_path(target)
            if orphan is None:
                raise RuntimeError("provider not found")
            backup = _backup_provider_file(orphan)
            orphan.unlink()
            return {
                "id": _resource_id("orphan", orphan.name),
                "name": orphan.stem,
                "deleted": True,
                "orphan": True,
                "backup": str(backup) if backup else "",
            }

        name = target
        if name in PROTECTED_PROVIDERS:
            raise RuntimeError(f"protected provider cannot delete: {name}")
        if name not in providers:
            raise RuntimeError("provider not found")
        original_config = CONFIG.read_bytes()
        provider = providers[name]
        path = _provider_path(name, provider)
        backup = _backup_provider_file(path)
        del providers[name]
        cfg["proxy-providers"] = providers
        detach_provider_from_groups(cfg, name)
        try:
            save_cfg(cfg)
            validate_and_restart()
            path.unlink(missing_ok=True)
        except Exception:
            _restore_config(original_config)
            try:
                validate_and_restart()
            except Exception:
                pass
            raise
        return {
            "id": _resource_id("provider", name),
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
        if n < 0 or n > MAX_API_BODY:
            raise RuntimeError("request body too large")
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def _auth_ok(self):
        if not MIHOMO_SECRET:
            return False
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            return secrets.compare_digest(auth[7:].strip(), MIHOMO_SECRET)
        return secrets.compare_digest(self.headers.get("X-Secret") or "", MIHOMO_SECRET)

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
                return self._json(200, del_provider(
                    name=qs.get("name", [None])[0],
                    provider_id=qs.get("id", [None])[0],
                ))
            return self._json(404, {"error": "not found"})
        except Exception as e:
            return self._json(400, {"error": str(e)})

    def log_message(self, *args):
        return


if __name__ == "__main__":
    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), H)
    print(f"gateway api on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    httpd.serve_forever()
