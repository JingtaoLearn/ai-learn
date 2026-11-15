#!/usr/bin/env bash
# Ship this project through the ailearn hub and (re)build it on zhlearn and/or Feng.
# Usage: deploy/deploy.sh [serve|train|hermes|all]   (secrets live only in the remote .env files)
set -euo pipefail

target="${1:-all}"
hub="${QUANT_HUB:-jingtao-learn}"
root="$(cd "$(dirname "$0")/.." && pwd)"
bundle="$(mktemp)"
trap 'rm -f "$bundle"' EXIT
tar -C "$root" -czf "$bundle" --exclude=__pycache__ --exclude=.pytest_cache --exclude=.env \
  quant tests deploy pyproject.toml Dockerfile .dockerignore compose.yaml compose.worker.yaml .env.example
scp -q "$bundle" "$hub:/tmp/quant-signal.tgz"

ship() {  # host, compose file, docker prefix
  ssh "$hub" "scp -q /tmp/quant-signal.tgz $1:/tmp/quant-signal.tgz && ssh $1 '
    set -e; mkdir -p ~/quant-signal && tar -xzf /tmp/quant-signal.tgz -C ~/quant-signal && cd ~/quant-signal
    test -f .env || { echo \"missing ~/quant-signal/.env on $1 (see .env.example)\"; exit 1; }
    $3 docker compose -f $2 up -d --build && $3 docker compose -f $2 ps'"
}

case "$target" in
  serve|all) ship zhlearn compose.yaml sudo ;;&
  train|all) ship feng-learn compose.worker.yaml "" ;;&
  hermes|all)
    ssh "$hub" 'set -e; mkdir -p ~/quant-signal-deploy && tar -xzf /tmp/quant-signal.tgz -C ~/quant-signal-deploy deploy
      for n in quant_daily_cn quant_daily_us quant_alerts; do
        install -m 0755 ~/quant-signal-deploy/deploy/hermes/quant_job.py ~/.hermes/scripts/$n.py
      done
      install -D -m 0755 ~/quant-signal-deploy/deploy/ailearn/quant-train-tunnel.sh ~/.local/libexec/quant-train-tunnel.sh
      install -D -m 0644 ~/quant-signal-deploy/deploy/ailearn/quant-train-tunnel.service ~/.config/systemd/user/quant-train-tunnel.service
      systemctl --user daemon-reload && systemctl --user enable --now quant-train-tunnel.service
      systemctl --user restart quant-train-tunnel.service' ;;
  serve|train) ;;
  *) echo "usage: $0 [serve|train|hermes|all]" >&2; exit 2 ;;
esac
