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

## Known residual risks

- HTTP panel still exposes secret in browser localStorage after login (MetaCubeXD design)
- Public SOCKS ports can be scanned; use strong passwords
- Adding subscription restarts mihomo briefly

Do not paste production secrets into GitHub issues, commits, or chat logs.
