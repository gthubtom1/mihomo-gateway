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

Left sidebar -> SOCKS5 -> Subscription URL or YAML file

Local YAML uploads are capped at 16 MiB, normalized to their `proxies` list, validated, and stored as static file providers. A failed validation or Mihomo restart rolls back both config and cache.

If an HTTPS subscription host returns 403/429, the gateway first stops rate-limit amplification. When an existing cached provider is available and the listener's current runtime route resolves to one of its nodes, it retries through the local authenticated SOCKS route. With no eligible provider route, wait for the reported retry interval or seed the gateway with a local YAML before retrying the URL.

New managed providers can accept Clash/Mihomo YAML and common Base64/URI client subscription formats. Their Mihomo URL points to the authenticated loopback management API; `x-source-url` stores the original URL in `/etc/mihomo/config.yaml`. Each scheduled refresh re-fetches, converts when necessary, and validates the result before Mihomo replaces its cache.

If only a generated URL expires while the downloaded node credentials remain valid, the downloaded YAML can be kept as a static provider. If the response contains ports or credentials that expire too, the provider must refresh before that lifetime ends. Set the interval accordingly, but respect upstream `Retry-After`; a token that returns persistent `403/429` needs a fresh URL or YAML from the airport.

Converter diagnostics:

```bash
/opt/mihomo-gateway/node/bin/node --version
sha256sum /opt/mihomo-gateway/vendor/proxy-utils.esm.mjs
journalctl -u mihomo-gateway-api -n 100 --no-pager
```

The expected converter version and checksum are recorded in `scripts/common.sh`. Conversion requires a host where Bubblewrap can create user, mount, PID, IPC, UTS, and network namespaces. The child runs as UID/GID 65534 with no capabilities, a private writable work directory, and `prlimit` bounds for memory, CPU, processes, and output size. Containers that disable unprivileged or nested namespaces are not supported. Do not replace the module without updating the pin, tests, and security review.

## SOCKS endpoints

```bash
mihomo-gateway socks add 1100 GPT
mihomo-gateway socks list
mihomo-gateway socks migrate
mihomo-gateway socks del 1100
```

New listeners use independent egress by default. The migration command keeps every existing port, listener name, username, password, and runtime-selected source group while assigning distinct primary nodes where enough healthy nodes exist. Node identity includes both provider and node name, so equal names from different providers cannot corrupt health or assignment state. At least two OpenAI-capable healthy candidates are required, and primary identities are never reused. Provider nodes are checked against the OpenAI API every 60 seconds, with the expected unauthenticated `401` response treated as healthy. Each listener automatically falls back to another eligible node and returns to its fixed primary after recovery. Running migration again leaves every already-managed primary unchanged, including temporarily unhealthy primaries.

## Upgrade

```bash
cd /path/to/mihomo-gateway
git pull
bash install.sh
```

When an installation already exists, `install.sh` uses a backed-up in-place upgrade path and preserves the current config, provider files, ports, and credentials. A failed upgrade or listener migration restores the previous runtime files.

Set `FORCE_MIHOMO_REINSTALL=1` to force binary reinstall.
