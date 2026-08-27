#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

case "${1:-}" in
  infra-up)
    docker compose up -d --build
    ;;
  infra-health)
    docker compose ps
    python3 scripts/check_health.py
    ;;
  infra-down)
    docker compose down
    ;;
  test)
    repository_root="$(cd ../.. && pwd)"
    docker compose run --rm --no-deps \
      -e PYTHONPATH=/workspace/src \
      -e AI_LEARN_REPOSITORY_ROOT=/repository \
      -v "${repository_root}:/repository:ro" \
      jupyter pytest -q -p no:cacheprovider
    ;;
  lint)
    docker compose run --rm --no-deps jupyter ruff check --no-cache src tests
    ;;
  run)
    docker compose exec -T -e PYTHONPATH=/workspace/src jupyter python -m gold_research.flow \
      --tracking-uri http://mlflow:5000
    ;;
  cmb-snapshot)
    docker compose exec -T -e PYTHONPATH=/workspace/src jupyter python -m gold_research.cmb \
      --output data/cmb
    ;;
  *)
    printf 'Usage: %s {infra-up|infra-health|infra-down|test|lint|run|cmb-snapshot}\n' "$0" >&2
    exit 2
    ;;
esac
