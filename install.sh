#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/common.sh
source "${ROOT_DIR}/scripts/common.sh"

require_root
if has_existing_state; then
  require_complete_existing_install
  upgrade_existing_install
  exit 0
fi
detect_public_ip
prepare_dirs
install_dependencies
install_mihomo_binary
install_panel_api
install_node_runtime
install_subscription_converter
generate_secrets
render_runtime_config
install_nginx_site
install_systemd_units
enable_firewall
start_services
import_initial_subscriptions
migrate_initial_socks
print_summary
