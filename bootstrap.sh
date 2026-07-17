#!/usr/bin/env bash
set -euo pipefail

REPOSITORY="${MIHOMO_GATEWAY_REPOSITORY:-gthubtom1/mihomo-gateway}"
REF="${MIHOMO_GATEWAY_REF:-main}"
ARCHIVE_URL="${MIHOMO_GATEWAY_ARCHIVE_URL:-https://github.com/${REPOSITORY}/archive/refs/heads/${REF}.tar.gz}"

command -v curl >/dev/null 2>&1 || { echo "[x] curl is required" >&2; exit 1; }
command -v tar >/dev/null 2>&1 || { echo "[x] tar is required" >&2; exit 1; }

tmp="$(mktemp -d)"
cleanup() { rm -rf -- "${tmp}"; }
trap cleanup EXIT

echo "[*] downloading Mihomo Gateway (${REF})"
curl -fsSL --retry 3 --connect-timeout 15 "${ARCHIVE_URL}" | tar -xzf - -C "${tmp}"

installer="$(find "${tmp}" -mindepth 2 -maxdepth 2 -type f -name install.sh -print -quit)"
[[ -n "${installer}" ]] || { echo "[x] install.sh not found in downloaded archive" >&2; exit 1; }

bash "${installer}"
