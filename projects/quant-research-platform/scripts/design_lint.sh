#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
exec npx --yes --package=@google/design.md@0.4.0 design.md lint DESIGN.md
