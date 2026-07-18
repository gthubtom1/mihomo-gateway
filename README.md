# Mihomo Gateway

One-click Mihomo (Clash.Meta) public SOCKS5 gateway for VPS, with MetaCubeXD panel and subscription/port management.

## Features

- Convert airport subscriptions into public authenticated SOCKS5 endpoints
- Accept Clash/Mihomo YAML plus common Base64, URI, Surge, Quantumult, and similar subscription formats through a pinned Sub-Store parser
- Multi-group routing: AUTO / GPT / region groups
- MetaCubeXD dashboard on port 9090
- Sidebar tools: create/delete SOCKS5 ports, add/delete subscription URLs, and import local YAML files
- systemd services + Nginx static UI with WebSocket overview
- Secrets generated at install time (never committed)

## Requirements

- Ubuntu 20.04 / 22.04 / 24.04
- root
- Open ports: 22, 9090, and SOCKS ports you use (default 1080+)

## One-click install

```bash
curl -fsSL https://raw.githubusercontent.com/gthubtom1/mihomo-gateway/main/bootstrap.sh | bash
```

Or:

```bash
git clone https://github.com/gthubtom1/mihomo-gateway.git
cd mihomo-gateway
bash install.sh
```

## Custom install

```bash
export PANEL_PORT=9090
export SOCKS_PORT=1080
export SOCKS_USER=myuser
export SOCKS_PASS='strong-password'
export MIHOMO_SECRET='optional-custom-secret'
export SUB_URLS='airport1|https://example.com/clash1.yaml,airport2|https://example.com/clash2.yaml'
bash install.sh
```

## After install

Panel:

```text
http://YOUR_VPS_IP:9090/
```

If asked for backend:

```text
Backend: http://YOUR_VPS_IP:9090
Secret:  (stored in the root-only credentials file)
```

Credentials:

```bash
cat /root/mihomo-gateway/credentials.txt
```

The installer does not print the Secret or SOCKS password to stdout. Run `mihomo-gateway credentials` as root when you need them.

Left sidebar **SOCKS5**:

- create/delete public SOCKS5 ports
- add/delete airport subscription URLs (writes providers and reloads)
- import/delete local Clash or Mihomo YAML files as static providers

Managed URL providers refresh through the local API, which safely fetches the original URL and serves converted Mihomo YAML back to the core. Common `ss`, `ssr`, `vmess`, `vless`, `trojan`, Hysteria, TUIC, and AnyTLS-style inputs are supported by the bundled parser version. Provider-specific encrypted formats, browser login/CAPTCHA flows, and expired or IP-blocked tokens still require a valid compatible URL from the airport.

If only the download URL expires while its node credentials remain valid, import the downloaded YAML as a static snapshot. If the returned node ports or credentials also expire, a static snapshot will stop working; automatic refresh requires a stable extraction endpoint and an interval shorter than the credential lifetime. A URL returning `403/429` or an expired token must be replaced by the airport.

## Commands

```bash
systemctl status mihomo mihomo-gateway-api nginx
journalctl -u mihomo -f
mihomo-gateway status
mihomo-gateway socks list
mihomo-gateway provider list
```

## Uninstall

```bash
bash uninstall.sh
```

## Security

- No real IPs/passwords/subscription URLs in the repo
- API listens on 127.0.0.1 only
- Frontend does not embed secrets in source for public clone
- Enable HTTPS and firewall restrictions before heavy public use

See [docs/SECURITY.md](docs/SECURITY.md).

Third-party runtime components and pinned versions are listed in [THIRD_PARTY.md](THIRD_PARTY.md).

## License

MIT
