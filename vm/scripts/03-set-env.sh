#!/usr/bin/env bash
set -euo pipefail
# Set server environment variables in /etc/environment.
#
# Usage:
#   sudo bash 03-set-env.sh --domain example.com --email admin@example.com
#   sudo bash 03-set-env.sh --domain example.com --email admin@example.com \
#     --machine-type cloud --active-folder /data_active --static-folder /data_static \
#     --llm-api-url https://your-upstream.example.com

source "$(dirname "$0")/lib/common.sh"
require_root

FLAG_NAME="set_env"

if check_flag "${FLAG_NAME}"; then
  echo "Environment variables have already been set. Skipping."
  exit 0
fi

# Defaults
S_DOMAIN=""
S_EMAIL=""
S_MACHINE_TYPE="cloud"
S_CONTAINER_FOLDER_ACTIVE="/data_active"
S_CONTAINER_FOLDER_STATIC="/data_static"
S_LLM_API_URL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain)              S_DOMAIN="$2"; shift 2 ;;
    --email)               S_EMAIL="$2"; shift 2 ;;
    --machine-type)        S_MACHINE_TYPE="$2"; shift 2 ;;
    --active-folder)       S_CONTAINER_FOLDER_ACTIVE="$2"; shift 2 ;;
    --static-folder)       S_CONTAINER_FOLDER_STATIC="$2"; shift 2 ;;
    --llm-api-url)   S_LLM_API_URL="$2"; shift 2 ;;
    *)                     echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [ -z "${S_DOMAIN}" ] || [ -z "${S_EMAIL}" ]; then
  echo "Error: --domain and --email are required." >&2
  echo "Usage: sudo bash 03-set-env.sh --domain example.com --email admin@example.com" >&2
  exit 1
fi

ENV_FILE="/etc/environment"
cp "${ENV_FILE}" "${ENV_FILE}.backup"

cat >> "${ENV_FILE}" <<EOF

# Added by ai-learn setup
S_DOMAIN=${S_DOMAIN}
S_EMAIL=${S_EMAIL}
S_MACHINE_TYPE=${S_MACHINE_TYPE}
S_CONTAINER_FOLDER_ACTIVE=${S_CONTAINER_FOLDER_ACTIVE}
S_CONTAINER_FOLDER_STATIC=${S_CONTAINER_FOLDER_STATIC}
S_LLM_API_URL=${S_LLM_API_URL}
EOF

set_flag "${FLAG_NAME}"
echo "Done. Environment variables written to ${ENV_FILE}."
echo "Run 'source ${ENV_FILE}' or log out and back in to apply."
