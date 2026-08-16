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
    docker compose run --rm --no-deps -e PYTHONPATH=/workspace/src jupyter pytest -q -p no:cacheprovider
    ;;
  lint)
    docker compose run --rm --no-deps jupyter ruff check --no-cache src tests
    ;;
  run)
    docker compose exec -T -e PYTHONPATH=/workspace/src jupyter python -m gold_research.flow \
      --tracking-uri http://mlflow:5000
    ;;
  *)
    printf 'Usage: %s {infra-up|infra-health|infra-down|test|lint|run}\n' "$0" >&2
    exit 2
    ;;
esac
