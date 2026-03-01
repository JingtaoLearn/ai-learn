#!/usr/bin/env bash
set -euo pipefail
# Install Azure CLI and configure Managed Identity login.
#
# Prerequisites:
#   - VM must have System-assigned Managed Identity enabled in Azure Portal
#   - The identity must be granted appropriate role on target resource groups
#
# Usage:
#   sudo bash 05-install-azure-cli.sh
#
# Post-install (as regular user):
#   az login --identity

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
echo "  1. Enable System-assigned Managed Identity on this VM (Azure Portal)"
echo "  2. Assign roles to the identity on target resource groups"
echo "  3. Run: az login --identity"
