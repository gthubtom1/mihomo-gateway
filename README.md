# Mihomo Gateway

One-click Mihomo (Clash.Meta) public SOCKS5 gateway for VPS, with MetaCubeXD panel and subscription/port management.

## Features

- Convert airport subscriptions into public authenticated SOCKS5 endpoints
- Multi-group routing: AUTO / GPT / region groups
- MetaCubeXD dashboard on port 9090
- Sidebar tools: create/delete SOCKS5 ports, add/delete subscription URLs
- systemd services + Nginx static UI with WebSocket overview
- Secrets generated at install time (never committed)

## Requirements

- Ubuntu 20.04 / 22.04 / 24.04
- root
- Open ports: 22, 9090, and SOCKS ports you use (default 1080+)

## One-click install

```bash
curl -fsSL https://raw.githubusercontent.com/gthubtom1/mihomo-gateway/main/install.sh | bash
```

Or:

```bash
git clone https://github.com/gthubtom1/mihomo-gateway.git
cd mihomo-gateway
bash install.sh
```

Replace `OWNER` with your GitHub username/org.

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
Secret:  (printed by installer / credentials file)
```

Credentials:

```bash
cat /root/mihomo-gateway/credentials.txt
```

Left sidebar **SOCKS5**:

- create/delete public SOCKS5 ports
- add/delete airport subscription URLs (writes providers and reloads)

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

## License

MIT

