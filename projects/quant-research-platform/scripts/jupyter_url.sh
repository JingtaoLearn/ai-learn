#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -f .env ]]; then
  echo "Missing .env; generate JUPYTER_TOKEN as documented in README.md" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
source .env
set +a
printf 'http://127.0.0.1:8888/lab?token=%s\n' "$JUPYTER_TOKEN"
