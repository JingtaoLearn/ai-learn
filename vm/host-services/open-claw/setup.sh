#!/usr/bin/env bash
set -euo pipefail

# Setup script for Open Claw host service.
# Creates symlinks from system locations to repo files and enables systemd services.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Validate environment variables ---

missing=()
for var in S_LITELLM_API_KEY OPENCLAW_GATEWAY_TOKEN; do
  if [[ -z "${!var:-}" ]]; then
    missing+=("$var")
  fi
done

if [[ ${#missing[@]} -gt 0 ]]; then
  echo "Error: required environment variables are not set:" >&2
  printf '  %s\n' "${missing[@]}" >&2
  echo "Set them in /etc/environment and re-login, or export them before running this script." >&2
  exit 1
fi

# --- Helper: create symlink with backup ---

link_file() {
  local src="$1"   # repo file (link target)
  local dst="$2"   # system location (link name)

  mkdir -p "$(dirname "$dst")"

  if [[ -L "$dst" ]]; then
    local current_target
    current_target="$(readlink -f "$dst")"
    if [[ "$current_target" == "$src" ]]; then
      echo "  [ok]   $dst -> $src (already linked)"
      return
    fi
    echo "  [link] $dst -> $src (was -> $current_target)"
    rm "$dst"
  elif [[ -e "$dst" ]]; then
    local backup="${dst}.bak.$(date +%Y%m%d%H%M%S)"
    echo "  [bak]  $dst -> $backup"
    mv "$dst" "$backup"
    echo "  [link] $dst -> $src"
  else
    echo "  [link] $dst -> $src"
  fi

  ln -s "$src" "$dst"
}

# --- Create symlinks ---

echo "Creating symlinks..."

link_file "$SCRIPT_DIR/openclaw.json" \
          "$HOME/.openclaw/openclaw.json"

link_file "$SCRIPT_DIR/openai-compat-proxy.js" \
          "$HOME/.openclaw/openai-compat-proxy.js"

link_file "$SCRIPT_DIR/systemd/openclaw-proxy.service" \
          "$HOME/.config/systemd/user/openclaw-proxy.service"

# --- Reload and enable systemd services ---

echo ""
echo "Reloading systemd user daemon..."
systemctl --user daemon-reload

echo "Enabling and starting services..."
systemctl --user enable --now openclaw-proxy.service

# Gateway service is managed by openclaw CLI — only start if it exists
if systemctl --user list-unit-files openclaw-gateway.service &>/dev/null; then
  systemctl --user enable --now openclaw-gateway.service
  echo "  [ok] openclaw-gateway enabled and started"
else
  echo "  [skip] openclaw-gateway.service not found — run 'openclaw gateway install' first"
fi

echo ""
echo "Done. Check status with:"
echo "  systemctl --user status openclaw-gateway openclaw-proxy"
