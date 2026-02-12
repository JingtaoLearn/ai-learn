#!/usr/bin/env bash
set -euo pipefail

# Setup script for Open Claw host service.
# Validates environment, installs git hooks, and enables systemd services.
#
# NOTE: openclaw.example.json in this directory is a reference copy only.
# The actual config lives at ~/.openclaw/openclaw.json and is managed by
# the openclaw CLI (openclaw config / openclaw configure).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

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

# --- Install git hooks ---

echo "Installing git hooks..."
HOOK_SRC="$REPO_ROOT/vm/scripts/hooks/pre-commit"
HOOK_DST="$REPO_ROOT/.git/hooks/pre-commit"

if [[ -f "$HOOK_SRC" ]]; then
  cp "$HOOK_SRC" "$HOOK_DST"
  chmod +x "$HOOK_DST"
  echo "  [ok] pre-commit hook installed"
else
  echo "  [skip] pre-commit hook source not found at $HOOK_SRC"
fi

# --- Enable systemd services ---

echo ""
echo "Reloading systemd user daemon..."
systemctl --user daemon-reload

# Gateway service is managed by openclaw CLI
if systemctl --user list-unit-files openclaw-gateway.service &>/dev/null; then
  systemctl --user enable --now openclaw-gateway.service
  echo "  [ok] openclaw-gateway enabled and started"
else
  echo "  [skip] openclaw-gateway.service not found — run 'openclaw gateway install' first"
fi

echo ""
echo "Done. Check status with:"
echo "  openclaw status"
echo "  systemctl --user status openclaw-gateway"
