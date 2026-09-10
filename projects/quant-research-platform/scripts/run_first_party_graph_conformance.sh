#!/usr/bin/env bash
set -euo pipefail

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$project_root/src"
exec python3 -S "$project_root/scripts/first_party_graph_conformance.py"
