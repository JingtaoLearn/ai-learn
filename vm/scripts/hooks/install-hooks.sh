#!/usr/bin/env bash
set -euo pipefail

# Install Git hooks from repo to .git/hooks/

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
GIT_HOOKS_DIR="$REPO_ROOT/.git/hooks"

echo "Installing Git hooks..."

# Install pre-commit hook
if [[ -f "$SCRIPT_DIR/pre-commit" ]]; then
  cp "$SCRIPT_DIR/pre-commit" "$GIT_HOOKS_DIR/pre-commit"
  chmod +x "$GIT_HOOKS_DIR/pre-commit"
  echo "✅ Installed pre-commit hook"
else
  echo "⚠️  pre-commit hook not found"
fi

echo ""
echo "Done! Hooks installed in .git/hooks/"
echo ""
echo "💡 Tip: Run this script after cloning the repo to set up hooks"
