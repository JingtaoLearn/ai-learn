#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  exit 2
fi
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$project_root/src"
exec python3 -S "$project_root/scripts/first_party_graph_network_conformance.py" "$1"
