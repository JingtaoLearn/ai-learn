#!/usr/bin/env bash
# Configure swap space on the server.
#
# Azure VMs:  uses waagent.conf (ResourceDisk swap)
# Generic:    uses fallocate to create a swap file
#
# Usage:
#   sudo bash 00-set-swap.sh --size 4096
#   sudo bash 00-set-swap.sh --size 4096 --method azure

source "$(dirname "$0")/lib/common.sh"
require_root

FLAG_NAME="set_swap"

if check_flag "${FLAG_NAME}"; then
  echo "Swap has already been configured. Skipping."
  exit 0
fi

# Defaults
SWAP_SIZE_MB=4096
METHOD="generic"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --size)   SWAP_SIZE_MB="$2"; shift 2 ;;
    --method) METHOD="$2"; shift 2 ;;
    *)        echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [ "${METHOD}" = "azure" ]; then
  CONF="/etc/waagent.conf"
  cp "${CONF}" "${CONF}.backup"
  cat >> "${CONF}" <<EOF

# Added by ai-learn setup
ResourceDisk.Format=y
ResourceDisk.EnableSwap=y
ResourceDisk.SwapSizeMB=${SWAP_SIZE_MB}
EOF
  echo "Updated ${CONF} with swap size ${SWAP_SIZE_MB}MB."
  echo "Restarting walinuxagent..."
  systemctl restart walinuxagent
else
  SWAP_FILE="/swapfile"
  fallocate -l "${SWAP_SIZE_MB}M" "${SWAP_FILE}"
  chmod 600 "${SWAP_FILE}"
  mkswap "${SWAP_FILE}"
  swapon "${SWAP_FILE}"
  echo "${SWAP_FILE} none swap sw 0 0" >> /etc/fstab
  echo "Created ${SWAP_SIZE_MB}MB swap file at ${SWAP_FILE}."
fi

set_flag "${FLAG_NAME}"
echo "Done. A reboot may be required for Azure swap changes to take effect."
