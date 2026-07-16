#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
source "${ROOT_DIR}/scripts/common.sh"

require_root

echo "[*] stopping services"
systemctl stop mihomo-gateway-api 2>/dev/null || true
systemctl stop mihomo 2>/dev/null || true

echo "[*] disable units"
systemctl disable mihomo-gateway-api 2>/dev/null || true
systemctl disable mihomo 2>/dev/null || true

echo "[*] remove unit files"
rm -f /etc/systemd/system/mihomo.service /etc/systemd/system/mihomo-gateway-api.service
systemctl daemon-reload || true

echo "[*] remove nginx site"
rm -f /etc/nginx/sites-enabled/mihomo-gateway /etc/nginx/sites-available/mihomo-gateway
rm -f /etc/nginx/conf.d/mihomo-upgrade-map.conf
if command -v nginx >/dev/null 2>&1; then
  nginx -t && systemctl reload nginx || true
fi

if [[ "${PURGE_DATA:-0}" == "1" ]]; then
  echo "[*] purging data directories"
  rm -rf /etc/mihomo /opt/mihomo-gateway /root/mihomo-gateway
  rm -f /usr/local/bin/mihomo /usr/local/bin/mihomo-gateway
else
  echo "[*] keeping /etc/mihomo and credentials (set PURGE_DATA=1 to remove)"
fi

echo "[+] uninstall complete"
