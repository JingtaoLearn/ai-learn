#!/usr/bin/env bash
set -euo pipefail
# Install Azure CLI for managing Azure resources.
#
# Supports two authentication methods:
#   1. Managed Identity — for resources in the same tenant as this VM
#   2. Service Principal — for cross-tenant access (current setup)
#
# Usage:
#   sudo bash 05-install-azure-cli.sh
#
# Post-install (Service Principal login):
#   See vm/docs/azure-cross-tenant-auth.md for setup details.

source "$(dirname "$0")/lib/common.sh"
require_root

FLAG_NAME="install_azure_cli"

if check_flag "${FLAG_NAME}"; then
  echo "Azure CLI has already been installed. Skipping."
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive

# Install Azure CLI via Microsoft's official script
curl -sL https://aka.ms/InstallAzureCLIDeb | bash

set_flag "${FLAG_NAME}"
echo "Done. Azure CLI installed."
echo ""
echo "Next steps:"
echo "  See vm/docs/azure-cross-tenant-auth.md for authentication setup."
