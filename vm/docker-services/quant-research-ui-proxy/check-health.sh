#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/../../scripts/lib/common.sh"

gateway_env="${1:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/quant-research-gateway.env}"
if [ ! -f "${gateway_env}" ]; then
  echo "Error: tunnel-generated gateway environment file is unavailable." >&2
  exit 1
fi

response="$(
  docker compose \
    --env-file "${gateway_env}" \
    exec -T quant-research-ui-proxy \
    wget -q -O - \
      --header='Host: quant.ai.jingtao.fun' \
      http://127.0.0.1/health
)"

if [ "${response}" != '{"status":"ok"}' ]; then
  echo "Error: end-to-end quant research health response is invalid." >&2
  exit 1
fi

printf '%s\n' "${response}"
