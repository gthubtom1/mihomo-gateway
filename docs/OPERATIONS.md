# Operations

## Service map

| Component | Role | Listen |
|-----------|------|--------|
| mihomo | proxy core | SOCKS public ports + `127.0.0.1:9091` |
| mihomo-gateway-api | SOCKS/provider management API | `127.0.0.1:9092` |
| nginx | static MetaCubeXD + reverse proxy | public panel port (default 9090) |

## Logs

```bash
journalctl -u mihomo -f
journalctl -u mihomo-gateway-api -f
journalctl -u nginx -f
```

## Backup / restore

Config backups:

```bash
ls -lt /root/mihomo-backups
find /root/mihomo-backups -maxdepth 3 -type f -print
```

Provider deletions are copied under `/root/mihomo-backups/providers/`. Reinstall cleanup stores the previous `config.yaml` and stale YAML together under a timestamped `/root/mihomo-backups/reinstall-*/` directory.

Restore example:

```bash
cp /root/mihomo-backups/config.yaml.YYYYMMDD-HHMMSS /etc/mihomo/config.yaml
cp /root/mihomo-backups/providers/airport.yaml.YYYYMMDD-HHMMSS /etc/mihomo/providers/airport.yaml
mihomo -t -d /etc/mihomo
systemctl restart mihomo
```

For a reinstall snapshot, restore `reinstall-*/config.yaml` and the matching `reinstall-*/providers/` files as one set.

## Subscriptions

CLI:

```bash
mihomo-gateway provider add airport3 'https://example.com/clash.yaml' 3600
mihomo-gateway provider list
mihomo-gateway provider del airport3
```

Panel:

Left sidebar -> SOCKS5 -> Subscription URL

If an HTTPS subscription host returns 403/429, the gateway first stops rate-limit amplification. When an existing cached provider is available and the listener's current runtime route resolves to one of its nodes, it retries through the local authenticated SOCKS route. With no eligible provider route, wait for the reported retry interval or seed the gateway with a local YAML before retrying the URL.

## SOCKS endpoints

```bash
mihomo-gateway socks add 1100 GPT
mihomo-gateway socks list
mihomo-gateway socks del 1100
```

## Upgrade

```bash
cd /path/to/mihomo-gateway
git pull
bash install.sh
```

Set `FORCE_MIHOMO_REINSTALL=1` to force binary reinstall.
