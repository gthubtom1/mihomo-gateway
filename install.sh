#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
source "${ROOT_DIR}/scripts/common.sh"

require_root
detect_public_ip
prepare_dirs
install_dependencies
install_mihomo_binary
generate_secrets
render_runtime_config
install_panel_api
install_nginx_site
install_systemd_units
enable_firewall
start_services
print_summary
