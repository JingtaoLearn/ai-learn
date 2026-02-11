#!/usr/bin/env bash
set -euo pipefail
# Install Python-based tools and applications.
#
# This script installs user-level Python tools using pipx.
# Unlike other scripts, this does NOT require root since pipx installs to user space.
#
# Usage:
#   bash 04-install-python-tools.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FLAG_NAME="install_python_tools"
FLAG_FILE="${SCRIPT_DIR}/flags/$(basename "$0" .sh)/${FLAG_NAME}.done"

# Create flag directory
mkdir -p "$(dirname "$FLAG_FILE")"

if [ -f "$FLAG_FILE" ]; then
  echo "Python tools have already been installed. Skipping."
  exit 0
fi

echo "Installing Python tools with pipx..."

# Ensure PATH includes pipx binaries
export PATH="$HOME/.local/bin:$PATH"

# Verify pipx is available
if ! command -v pipx &> /dev/null; then
  echo "Error: pipx is not installed. Please run 01-install-software.sh first." >&2
  exit 1
fi

# Ensure pipx environment is set up
pipx ensurepath

# Install router-maestro
echo "Installing router-maestro..."
if pipx list | grep -q "router-maestro"; then
  echo "router-maestro is already installed."
else
  pipx install router-maestro
  echo "router-maestro installed successfully."
fi

# Verify installation
if command -v router-maestro &> /dev/null; then
  echo "router-maestro version: $(router-maestro --version)"
else
  echo "Warning: router-maestro command not found in PATH." >&2
fi

# Mark as complete
touch "$FLAG_FILE"
echo "Done. Python tools installed."
