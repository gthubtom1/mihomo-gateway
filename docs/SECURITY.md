# Security Notes

## What this project does NOT commit

- Real VPS IPs
- Root passwords
- SOCKS passwords
- Mihomo API secrets
- Airport subscription URLs

Install-time generated secrets live only on the server:

```bash
/root/mihomo-gateway/credentials.txt
/root/mihomo-gateway/env
```

## Hardening checklist after install

1. Change/disable password SSH login; use SSH keys
2. Restrict panel port (`9090`) by firewall / security group when possible
3. Put HTTPS in front of the panel (Caddy/Nginx + certbot or Cloudflare Tunnel)
4. Rotate `MIHOMO_SECRET` and SOCKS passwords if ever leaked
5. Keep UFW/security group minimal: 22 + panel + used SOCKS ports only

## Architecture security choices

- Mihomo external-controller binds `127.0.0.1:9091`
- Management API binds `127.0.0.1:9092`
- Public entry is Nginx on panel port only
- Frontend inject does **not** embed secrets into the published repository assets at build time
- Users authenticate to MetaCubeXD with the install-generated secret
- Subscription imports reject credentials in URLs and any DNS result or redirect target that is not a public IP address; initial HTTP/TLS connections are pinned to the validated IP while preserving Host/SNI
- A 403/429 fallback may use the existing local authenticated SOCKS listener only for HTTPS when its runtime-selected node belongs to a cached provider; the authenticated SOCKS CONNECT request receives the validated IP, not the untrusted hostname, and HTTPS-to-HTTP redirects are rejected
- Subscription responses are capped at 16 MiB and parsed as Clash/Mihomo YAML before being persisted
- The generated environment file is Bash-escaped and mode `0600`; the systemd unit and installer stdout do not contain management or SOCKS credentials

## Known residual risks

- HTTP panel still exposes secret in browser localStorage after login (MetaCubeXD design)
- Public SOCKS ports can be scanned; use strong passwords
- Adding subscription restarts mihomo briefly
- Mihomo performs later HTTP-provider refreshes itself; add-time URL checks cannot prevent a trusted provider host from changing DNS behavior afterward
- The one-click bootstrap, Mihomo binary lookup, and MetaCubeXD download track mutable/default upstream refs unless the operator pins and audits them
- Backups have no automatic retention policy; monitor `/root/mihomo-backups` and prune only after external backups are verified

Do not paste production secrets into GitHub issues, commits, or chat logs.
