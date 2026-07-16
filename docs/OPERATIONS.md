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
```

Restore example:

```bash
cp /root/mihomo-backups/config.yaml.YYYYMMDD-HHMMSS /etc/mihomo/config.yaml
mihomo -t -d /etc/mihomo
systemctl restart mihomo
```

## Subscriptions

CLI:

```bash
mihomo-gateway provider add airport3 'https://example.com/clash.yaml' 3600
mihomo-gateway provider list
mihomo-gateway provider del airport3
```

Panel:

Left sidebar -> SOCKS5 -> Subscription URL

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
