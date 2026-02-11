#!/usr/bin/env bash
set -euo pipefail
# Install essential system tools on a fresh Ubuntu server.
#
# Usage:
#   sudo bash 01-install-software.sh

source "$(dirname "$0")/lib/common.sh"
require_root

FLAG_NAME="install_software"

if check_flag "${FLAG_NAME}"; then
  echo "Software has already been installed. Skipping."
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y \
  htop \
  iftop \
  vim \
  tree \
  git \
  gh \
  net-tools \
  iperf3 \
  ca-certificates \
  curl \
  gnupg \
  lsb-release \
  python3-pip \
  pipx

set_flag "${FLAG_NAME}"
echo "Done. System tools installed."
