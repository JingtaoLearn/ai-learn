#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/../../scripts/lib/common.sh"

: "${QUANT_FENG_SSH_TARGET:?Set QUANT_FENG_SSH_TARGET in the service environment file}"

gateway_output="$(docker network inspect nginx-proxy --format '{{range .IPAM.Config}}{{println .Gateway}}{{end}}')"
mapfile -t gateways < <(printf '%s\n' "${gateway_output}" | sed '/^[[:space:]]*$/d')

if [ "${#gateways[@]}" -ne 1 ]; then
  echo "Error: nginx-proxy must resolve to exactly one bridge gateway." >&2
  exit 1
fi

NGINX_PROXY_GATEWAY="${gateways[0]}"
if [[ ! "${NGINX_PROXY_GATEWAY}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] \
  || [[ "${NGINX_PROXY_GATEWAY}" == 0.* ]] \
  || [[ "${NGINX_PROXY_GATEWAY}" == 127.* ]] \
  || [[ "${NGINX_PROXY_GATEWAY}" == 255.* ]]; then
  echo "Error: nginx-proxy bridge gateway is unsafe: ${NGINX_PROXY_GATEWAY}" >&2
  exit 1
fi
IFS='.' read -r octet1 octet2 octet3 octet4 <<<"${NGINX_PROXY_GATEWAY}"
for octet in "${octet1}" "${octet2}" "${octet3}" "${octet4}"; do
  if [ "${octet}" -gt 255 ]; then
    echo "Error: nginx-proxy bridge gateway contains an invalid octet." >&2
    exit 1
  fi
done
if [ "${octet1}" -ge 224 ]; then
  echo "Error: nginx-proxy bridge gateway cannot be multicast or reserved." >&2
  exit 1
fi

gateway_env="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/quant-research-gateway.env"
umask 077
printf 'NGINX_PROXY_GATEWAY=%s\n' "${NGINX_PROXY_GATEWAY}" >"${gateway_env}"

exec ssh \
  -N \
  -T \
  -o BatchMode=yes \
  -o ExitOnForwardFailure=yes \
  -o StrictHostKeyChecking=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -L "${NGINX_PROXY_GATEWAY}:18090:127.0.0.1:8090" \
  "${QUANT_FENG_SSH_TARGET}"
