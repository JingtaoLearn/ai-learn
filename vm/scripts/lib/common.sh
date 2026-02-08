#!/usr/bin/env bash
# Shared library for VM initialization scripts.
# Source this file at the top of every script:
#   source "$(dirname "$0")/lib/common.sh"

set -euo pipefail

WORKING_FOLDER="$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)"
SCRIPT_NAME="$(basename "${BASH_SOURCE[1]}" .sh)"
FLAG_FOLDER="${WORKING_FOLDER}/flags/${SCRIPT_NAME}"

mkdir -p "${FLAG_FOLDER}"

# Ensure the script is running as root.
require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "Error: this script must be run as root." >&2
    exit 1
  fi
}

# Check if a step has already been completed.
# Usage: check_flag "step_name" && return
check_flag() {
  local name="$1"
  [ -f "${FLAG_FOLDER}/${name}.done" ]
}

# Mark a step as completed.
# Usage: set_flag "step_name"
set_flag() {
  local name="$1"
  touch "${FLAG_FOLDER}/${name}.done"
}
