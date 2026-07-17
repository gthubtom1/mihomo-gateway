#!/usr/bin/env bash
# shellcheck disable=SC2034
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

GATEWAY_NAME="mihomo-gateway"
INSTALL_ROOT="/opt/${GATEWAY_NAME}"
RUNTIME_ROOT="/etc/mihomo"
STATE_ROOT="/root/${GATEWAY_NAME}"
BACKUP_ROOT="${BACKUP_ROOT:-/root/mihomo-backups}"
CRED_FILE="${STATE_ROOT}/credentials.txt"
ENV_FILE="${STATE_ROOT}/env"
PANEL_API_PORT_DEFAULT=9092
PANEL_PORT_DEFAULT=9090
SOCKS_PORT_DEFAULT=1080

log()  { echo -e "[*] $*"; }
ok()   { echo -e "[+] $*"; }
warn() { echo -e "[!] $*" >&2; }
die()  { echo -e "[x] $*" >&2; exit 1; }

require_root() {
  [[ "$(id -u)" -eq 0 ]] || die "please run as root"
}

detect_public_ip() {
  if [[ -n "${PUBLIC_IP:-}" ]]; then
    return
  fi
  PUBLIC_IP="$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
  if [[ -z "${PUBLIC_IP}" ]]; then
    PUBLIC_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi
  [[ -n "${PUBLIC_IP}" ]] || die "cannot detect public IP; export PUBLIC_IP=x.x.x.x"
}

prepare_dirs() {
  mkdir -p "${INSTALL_ROOT}" "${RUNTIME_ROOT}/providers" "${RUNTIME_ROOT}/ui" "${STATE_ROOT}" "${BACKUP_ROOT}"
}

install_dependencies() {
  log "installing dependencies"
  apt-get update -qq
  apt-get install -y -qq curl ca-certificates gzip tar unzip python3 python3-yaml nginx ufw jq >/dev/null
}

install_mihomo_binary() {
  if command -v mihomo >/dev/null 2>&1 && [[ "${FORCE_MIHOMO_REINSTALL:-0}" != "1" ]]; then
    ok "mihomo already installed: $(mihomo -v | head -n1)"
    return
  fi
  log "installing mihomo"
  local arch asset_arch url
  arch="$(uname -m)"
  case "${arch}" in
    x86_64|amd64) asset_arch="amd64-compatible" ;;
    aarch64|arm64) asset_arch="arm64" ;;
    armv7l) asset_arch="armv7" ;;
    *) die "unsupported arch: ${arch}" ;;
  esac
  url="$(curl -fsSL https://api.github.com/repos/MetaCubeX/mihomo/releases/latest \
    | grep browser_download_url \
    | grep "linux-${asset_arch}" \
    | grep '\.gz"' \
    | head -n1 \
    | cut -d '"' -f4)"
  [[ -n "${url}" ]] || die "cannot resolve mihomo download url"
  local tmp
  tmp="$(mktemp -d)"
  curl -fL "${url}" -o "${tmp}/mihomo.gz"
  gzip -d "${tmp}/mihomo.gz"
  install -m 755 "${tmp}/mihomo" /usr/local/bin/mihomo
  rm -rf "${tmp}"
  ok "installed $(mihomo -v | head -n1)"
}

rand_secret() {
  # url-safe-ish
  openssl rand -base64 24 2>/dev/null | tr -d '\n' | tr '+/' '-_' | tr -d '='
}

generate_secrets() {
  PANEL_PORT="${PANEL_PORT:-$PANEL_PORT_DEFAULT}"
  PANEL_API_PORT="${PANEL_API_PORT:-$PANEL_API_PORT_DEFAULT}"
  SOCKS_PORT="${SOCKS_PORT:-$SOCKS_PORT_DEFAULT}"
  SOCKS_USER="${SOCKS_USER:-socks_$(openssl rand -hex 3)}"
  SOCKS_PASS="${SOCKS_PASS:-$(rand_secret)}"
  MIHOMO_SECRET="${MIHOMO_SECRET:-$(rand_secret)}"

  {
    printf 'PUBLIC_IP=%q\n' "${PUBLIC_IP}"
    printf 'PANEL_PORT=%q\n' "${PANEL_PORT}"
    printf 'PANEL_API_PORT=%q\n' "${PANEL_API_PORT}"
    printf 'SOCKS_PORT=%q\n' "${SOCKS_PORT}"
    printf 'SOCKS_USER=%q\n' "${SOCKS_USER}"
    printf 'SOCKS_PASS=%q\n' "${SOCKS_PASS}"
    printf 'MIHOMO_SECRET=%q\n' "${MIHOMO_SECRET}"
  } > "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
}

prepare_provider_storage() {
  local provider_dir="${RUNTIME_ROOT}/providers"
  mkdir -p "${provider_dir}" "${BACKUP_ROOT}"

  local files=()
  shopt -s nullglob
  files=("${provider_dir}"/*.yaml "${provider_dir}"/*.yml "${provider_dir}"/*.YAML "${provider_dir}"/*.YML)
  shopt -u nullglob
  [[ "${#files[@]}" -gt 0 || -f "${RUNTIME_ROOT}/config.yaml" ]] || return 0

  local backup_dir="${BACKUP_ROOT}/reinstall-$(date +%Y%m%d-%H%M%S)-$$"
  mkdir -p "${backup_dir}/providers"
  if [[ -f "${RUNTIME_ROOT}/config.yaml" ]]; then
    cp -a -- "${RUNTIME_ROOT}/config.yaml" "${backup_dir}/config.yaml"
  fi
  if [[ "${#files[@]}" -gt 0 ]]; then
    cp -a -- "${files[@]}" "${backup_dir}/providers/"
    rm -f -- "${files[@]}"
  fi
  ok "backed up runtime config and cleared ${#files[@]} stale provider file(s)"
}

render_runtime_config() {
  log "rendering mihomo config"
  prepare_provider_storage
  python3 "${ROOT_DIR}/scripts/render-config.py" \
    --template "${ROOT_DIR}/config/config.template.yaml" \
    --output "${RUNTIME_ROOT}/config.yaml" \
    --public-ip "${PUBLIC_IP}" \
    --socks-port "${SOCKS_PORT}" \
    --socks-user "${SOCKS_USER}" \
    --socks-pass "${SOCKS_PASS}" \
    --secret "${MIHOMO_SECRET}" \
    --sub-urls "${SUB_URLS:-}"

  chmod 600 "${RUNTIME_ROOT}/config.yaml"
  mihomo -t -d "${RUNTIME_ROOT}" >/dev/null
}

import_initial_subscriptions() {
  local raw="${SUB_URLS:-}"
  [[ -n "${raw}" ]] || return 0

  local root records record name url
  root="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  if ! records="$(python3 "${root}/scripts/render-config.py" --emit-sub-urls "${raw}")"; then
    die "failed to parse initial subscriptions"
  fi
  while IFS= read -r record; do
    [[ -n "${record}" ]] || continue
    name="$(printf '%s' "${record}" | jq -r '.name')"
    url="$(printf '%s' "${record}" | jq -r '.url')"
    log "importing initial subscription: ${name}"
    if ! mihomo-gateway provider add "${name}" "${url}" 3600 >/dev/null; then
      die "failed to import initial subscription: ${name}"
    fi
  done <<< "${records}"
}

install_panel_api() {
  log "installing panel API"
  mkdir -p "${INSTALL_ROOT}/panel"
  install -m 755 "${ROOT_DIR}/panel/app.py" "${INSTALL_ROOT}/panel/app.py"
  install -m 644 "${ROOT_DIR}/panel/inject.html" "${INSTALL_ROOT}/panel/inject.html"
  install -m 755 "${ROOT_DIR}/scripts/mihomo-gateway" /usr/local/bin/mihomo-gateway

  # download metacubexd UI if missing
  if [[ ! -f "${RUNTIME_ROOT}/ui/index.html" ]]; then
    log "downloading MetaCubeXD UI"
    local tmp
    tmp="$(mktemp -d)"
    curl -fL "https://github.com/MetaCubeX/metacubexd/archive/refs/heads/gh-pages.zip" -o "${tmp}/ui.zip"
    unzip -q "${tmp}/ui.zip" -d "${tmp}"
    local src
    src="$(find "${tmp}" -maxdepth 1 -type d -name 'metacubexd-*' | head -n1)"
    rm -rf "${RUNTIME_ROOT}/ui"
    mkdir -p "${RUNTIME_ROOT}/ui"
    cp -a "${src}/." "${RUNTIME_ROOT}/ui/"
    rm -rf "${tmp}"
  fi

  # inject management UI helpers (no hardcoded secret)
  python3 - <<PY
from pathlib import Path
import re
inject = Path("${INSTALL_ROOT}/panel/inject.html").read_text(encoding="utf-8")
for name in ["index.html", "200.html", "404.html"]:
    p = Path("${RUNTIME_ROOT}/ui") / name
    if not p.exists():
        continue
    t = p.read_text(encoding="utf-8", errors="ignore")
    t = re.sub(r'<script id="mx-gateway-inject">.*?</script>\s*', "", t, flags=re.S)
    t = re.sub(r'<style id="mx-gateway-style">.*?</style>\s*', "", t, flags=re.S)
    if "</head>" in t:
        t = t.replace("</head>", inject + "</head>")
    else:
        t = inject + t
    p.write_text(t, encoding="utf-8")
Path("${RUNTIME_ROOT}/ui/config.js").write_text(
    "window.__METACUBEXD_CONFIG__={defaultBackendURL: window.location.origin, githubToken:''}\n",
    encoding="utf-8",
)
print("ui inject ok")
PY
}

install_nginx_site() {
  log "configuring nginx"
  sed -e "s/__PANEL_PORT__/${PANEL_PORT}/g" \
      -e "s/__PANEL_API_PORT__/${PANEL_API_PORT}/g" \
      "${ROOT_DIR}/panel/nginx.conf.template" > /etc/nginx/sites-available/mihomo-gateway
  cat > /etc/nginx/conf.d/mihomo-upgrade-map.conf <<'EOF'
map $http_upgrade $mihomo_connection_upgrade {
    default upgrade;
    '' close;
}
EOF
  ln -sfn /etc/nginx/sites-available/mihomo-gateway /etc/nginx/sites-enabled/mihomo-gateway
  nginx -t
}

install_systemd_units() {
  log "installing systemd units"
  cat > /etc/systemd/system/mihomo.service <<EOF
[Unit]
Description=mihomo daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Restart=always
RestartSec=3
LimitNOFILE=1000000
WorkingDirectory=${RUNTIME_ROOT}
ExecStart=/usr/local/bin/mihomo -d ${RUNTIME_ROOT}
ExecReload=/bin/kill -HUP \$MAINPID

[Install]
WantedBy=multi-user.target
EOF

  sed -e "s|__INSTALL_ROOT__|${INSTALL_ROOT}|g" \
      "${ROOT_DIR}/panel/mihomo-gateway-api.service" > /etc/systemd/system/mihomo-gateway-api.service

  systemctl daemon-reload
  systemctl enable mihomo mihomo-gateway-api nginx >/dev/null
}

enable_firewall() {
  if ! command -v ufw >/dev/null 2>&1; then
    return
  fi
  log "configuring ufw"
  ufw allow 22/tcp >/dev/null || true
  ufw allow "${PANEL_PORT}/tcp" >/dev/null || true
  ufw allow "${SOCKS_PORT}/tcp" >/dev/null || true
  # do not force-enable if user disabled it; only enable when inactive? safer: if active already, just add rules
  if ufw status | grep -q "Status: active"; then
    ok "ufw active; rules ensured"
  else
    warn "ufw installed but not active (not auto-enabled)"
  fi
}

start_services() {
  log "starting services"
  systemctl restart mihomo
  sleep 1
  systemctl restart mihomo-gateway-api
  systemctl restart nginx
  sleep 1
  systemctl is-active --quiet mihomo || die "mihomo failed"
  systemctl is-active --quiet mihomo-gateway-api || die "panel api failed"
  systemctl is-active --quiet nginx || die "nginx failed"
}

print_summary() {
  cat > "${CRED_FILE}" <<EOF
public_ip: ${PUBLIC_IP}
panel: http://${PUBLIC_IP}:${PANEL_PORT}/
secret: ${MIHOMO_SECRET}
socks_user: ${SOCKS_USER}
socks_pass: ${SOCKS_PASS}
socks_port: ${SOCKS_PORT}
socks_url: socks5://${SOCKS_USER}:${SOCKS_PASS}@${PUBLIC_IP}:${SOCKS_PORT}
config: ${RUNTIME_ROOT}/config.yaml
env: ${ENV_FILE}
EOF
  chmod 600 "${CRED_FILE}"

  echo
  ok "install complete"
  echo "Panel : http://${PUBLIC_IP}:${PANEL_PORT}/"
  echo "Creds : ${CRED_FILE}"
  echo
  echo "Run 'mihomo-gateway credentials' as root to view the Secret and SOCKS URL."
  echo "Open panel, set backend to the panel URL and paste the Secret if prompted."
  echo "Use left sidebar SOCKS5 to manage ports and subscription URLs."
}
