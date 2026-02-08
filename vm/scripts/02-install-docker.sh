#!/usr/bin/env bash
# Install Docker CE and Docker Compose V2 plugin on Ubuntu.
#
# Usage:
#   sudo bash 02-install-docker.sh

source "$(dirname "$0")/lib/common.sh"
require_root

FLAG_NAME="install_docker"

if check_flag "${FLAG_NAME}"; then
  echo "Docker has already been installed. Skipping."
  exit 0
fi

# Add Docker GPG key
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

# Add Docker repository
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "${VERSION_CODENAME}") stable" \
  | tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker Engine and Compose plugin
apt-get update
apt-get install -y \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin

set_flag "${FLAG_NAME}"
echo "Done. Docker and Compose V2 installed."
echo "Verify with: docker compose version"
