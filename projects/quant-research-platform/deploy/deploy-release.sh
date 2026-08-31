#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RELEASE_ID="${1:-}"
RELEASE_ROOT="${QUANT_DEPLOY_RELEASE_ROOT:-/home/feng/quant-platform/releases}"
RUNTIME_ROOT="${QUANT_DEPLOY_RUNTIME_ROOT:-/home/feng/quant-platform/runtime}"
UNIT_PATH="${QUANT_DEPLOY_UNIT_PATH:-/home/feng/.config/systemd/user/quant-research-ui.service}"
ENV_PATH="${QUANT_DEPLOY_ENV_PATH:-/home/feng/.config/quant-research-ui.env}"
STATE_ROOT="${QUANT_DEPLOY_STATE_ROOT:-/home/feng/quant-platform/state/platform}"
ROLLBACK_DIR="${QUANT_DEPLOY_ROLLBACK_DIR:-/home/feng/quant-platform/rollback}"
LEGACY_PROBE_FILE="${QUANT_DEPLOY_PROBE_FILE:-/tmp/quant-research-ui-health.json}"
HEALTH_ATTEMPTS="${QUANT_DEPLOY_HEALTH_ATTEMPTS:-30}"
HEALTH_INTERVAL_SECONDS="${QUANT_DEPLOY_HEALTH_INTERVAL_SECONDS:-1}"
SERVICE_NAME="quant-research-ui.service"
PRODUCTION_HOST="quant.ai.jingtao.fun"
PUBLIC_URL="${QUANT_DEPLOY_PUBLIC_URL:-https://${PRODUCTION_HOST}}"
EXPECTED_SCHEMA_VERSION=9
CATALOG_PATH="${STATE_ROOT}/catalog.sqlite3"
RELEASE_DIR="${RELEASE_ROOT}/${RELEASE_ID}"
RUNTIME_DIR="${RUNTIME_ROOT}/venv-ui-${RELEASE_ID}"
RUNTIME_PYTHON="${RUNTIME_DIR}/bin/python"
UNIT_TEMPLATE="${QUANT_DEPLOY_UNIT_TEMPLATE:-${SCRIPT_DIR}/quant-research-ui.service}"
UNIT_BACKUP="${ROLLBACK_DIR}/quant-research-ui.service"
ENV_BACKUP="${ROLLBACK_DIR}/quant-research-ui.env"
CATALOG_BACKUP="${ROLLBACK_DIR}/catalog.sqlite3"

backup_ready=false
deployment_succeeded=false
staged_unit=""
staged_env=""
probe_file=""

fail() {
  printf 'Deployment failed: %s\n' "$1" >&2
  return 1
}

restore_catalog() {
  local failed=false
  if ! rm -f -- "${CATALOG_PATH}-wal" "${CATALOG_PATH}-shm"; then
    printf 'Rollback step failed: remove catalog sidecars.\n' >&2
    failed=true
  fi
  if ! python3 - "$CATALOG_BACKUP" "$CATALOG_PATH" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as source:
    with sqlite3.connect(sys.argv[2]) as destination:
        source.backup(destination)
PY
  then
    printf 'Rollback step failed: restore catalog backup.\n' >&2
    failed=true
  fi
  if ! chmod 0600 "$CATALOG_PATH"; then
    printf 'Rollback step failed: protect restored catalog.\n' >&2
    failed=true
  fi
  [[ "$failed" == false ]]
}

rollback() {
  local failed=false
  printf 'Deployment verification failed; restoring the previous release.\n' >&2
  if ! systemctl --user stop "$SERVICE_NAME"; then
    printf 'Rollback step failed: stop candidate service.\n' >&2
    failed=true
  fi
  if ! install -m 0644 "$UNIT_BACKUP" "$UNIT_PATH"; then
    printf 'Rollback step failed: restore user unit.\n' >&2
    failed=true
  fi
  if ! install -m 0600 "$ENV_BACKUP" "$ENV_PATH"; then
    printf 'Rollback step failed: restore environment file.\n' >&2
    failed=true
  fi
  if ! restore_catalog; then
    failed=true
  fi
  if ! systemctl --user daemon-reload; then
    printf 'Rollback step failed: reload user units.\n' >&2
    failed=true
  fi
  if ! systemctl --user restart "$SERVICE_NAME"; then
    printf 'Rollback step failed: restart previous service.\n' >&2
    failed=true
  fi
  [[ "$failed" == false ]]
}

on_exit() {
  status=$?
  trap - EXIT
  set +e
  cleanup_failed=false
  if [[ -n "$staged_unit" ]]; then
    if ! rm -f -- "$staged_unit"; then
      cleanup_failed=true
    fi
  fi
  if [[ -n "$staged_env" ]]; then
    if ! rm -f -- "$staged_env"; then
      cleanup_failed=true
    fi
  fi
  if [[ "$cleanup_failed" == true ]]; then
    printf 'Staged-file cleanup failed; continuing rollback.\n' >&2
  fi
  if [[ -n "$probe_file" ]]; then
    if ! rm -f -- "$probe_file"; then
      printf 'Secure probe cleanup failed.\n' >&2
    fi
  fi
  if ((status != 0)) \
    && [[ "$backup_ready" == true ]] \
    && [[ "$deployment_succeeded" != true ]]; then
    if ! rollback; then
      printf 'Rollback completed with errors; manual recovery is required.\n' >&2
    fi
  fi
  exit "$status"
}
trap on_exit EXIT

if [[ -z "$RELEASE_ID" ]] \
  || [[ ! "$RELEASE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  fail "release ID has invalid syntax"
fi
if [[ ! -d "$RELEASE_DIR" ]] || [[ -L "$RELEASE_DIR" ]]; then
  fail "release directory is missing or is a symlink"
fi
if [[ ! -d "$RUNTIME_DIR" ]] || [[ -L "$RUNTIME_DIR" ]]; then
  fail "release runtime directory is missing or is a symlink"
fi
if [[ ! -x "$RUNTIME_PYTHON" ]]; then
  fail "release runtime Python executable is missing"
fi
for required_file in "$UNIT_TEMPLATE" "$UNIT_PATH" "$ENV_PATH" "$CATALOG_PATH"; do
  if [[ ! -f "$required_file" ]] || [[ -L "$required_file" ]]; then
    fail "required deployment file is missing or is a symlink"
  fi
done
if [[ -e "$ROLLBACK_DIR" ]]; then
  fail "rollback backup already exists"
fi
if [[ -e "$LEGACY_PROBE_FILE" ]] || [[ -L "$LEGACY_PROBE_FILE" ]]; then
  if ! rm -f -- "$LEGACY_PROBE_FILE"; then
    fail "legacy health probe could not be removed"
  fi
fi

mkdir -m 0700 "$ROLLBACK_DIR"
install -m 0644 "$UNIT_PATH" "$UNIT_BACKUP"
install -m 0600 "$ENV_PATH" "$ENV_BACKUP"
python3 - "$CATALOG_PATH" "$CATALOG_BACKUP" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as source:
    with sqlite3.connect(sys.argv[2]) as destination:
        source.backup(destination)
PY
chmod 0600 "$CATALOG_BACKUP"
backup_ready=true
probe_file="$(mktemp "${ROLLBACK_DIR}/health-response.XXXXXX")"
chmod 0600 "$probe_file"

staged_unit="$(mktemp "${UNIT_PATH}.new.XXXXXX")"
python3 - "$UNIT_TEMPLATE" "$staged_unit" "$RELEASE_ID" <<'PY'
import sys
from pathlib import Path

source = Path(sys.argv[1]).read_text(encoding="utf-8")
placeholder = "REPLACE_WITH_RELEASE_ID"
if source.count(placeholder) != 2:
    raise SystemExit("unit template must contain exactly two release placeholders")
Path(sys.argv[2]).write_text(source.replace(placeholder, sys.argv[3]), encoding="utf-8")
PY

staged_env="$(mktemp "${ENV_PATH}.new.XXXXXX")"
python3 - "$ENV_PATH" "$staged_env" "$RELEASE_DIR" <<'PY'
import sys
from pathlib import Path

lines = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
indexes = [index for index, line in enumerate(lines) if line.startswith("QUANT_PROJECT_ROOT=")]
if len(indexes) != 1:
    raise SystemExit("environment file must contain exactly one project root")
lines[indexes[0]] = f"QUANT_PROJECT_ROOT={sys.argv[3]}"
Path(sys.argv[2]).write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

install -m 0644 "$staged_unit" "$UNIT_PATH"
install -m 0600 "$staged_env" "$ENV_PATH"
rm -f -- "$staged_unit" "$staged_env"
staged_unit=""
staged_env=""
systemctl --user daemon-reload
systemctl --user restart "$SERVICE_NAME"

health_succeeded=false
for ((attempt = 1; attempt <= HEALTH_ATTEMPTS; attempt++)); do
  current_request_succeeded=false
  : > "$probe_file"
  if curl \
    --fail \
    --silent \
    --show-error \
    --header "Host: ${PRODUCTION_HOST}" \
    --output "$probe_file" \
    "http://127.0.0.1:8090/health"; then
    current_request_succeeded=true
  fi
  if [[ "$current_request_succeeded" == true ]]; then
    if grep -Fxq '{"status":"ok"}' "$probe_file"; then
      health_succeeded=true
      break
    fi
  fi
  if ((attempt < HEALTH_ATTEMPTS)); then
    sleep "$HEALTH_INTERVAL_SECONDS"
  fi
done
if [[ "$health_succeeded" != true ]]; then
  fail "local health check did not receive a current successful response"
fi

python3 - "$CATALOG_PATH" "$EXPECTED_SCHEMA_VERSION" <<'PY'
import sqlite3
import sys

expected = list(range(1, int(sys.argv[2]) + 1))
with sqlite3.connect(sys.argv[1]) as connection:
    recorded = [
        row[0]
        for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
    ]
if recorded != expected:
    raise SystemExit("catalog schema migration verification failed")
PY

working_directory="$(
  systemctl --user show "$SERVICE_NAME" --property=WorkingDirectory --value
)"
if [[ "$working_directory" != "$RELEASE_DIR" ]]; then
  fail "systemd WorkingDirectory does not match the release"
fi

exec_start="$(
  systemctl --user show "$SERVICE_NAME" --property=ExecStart --value
)"
expected_exec_start_prefix="{ path=${RUNTIME_PYTHON} ; argv[]=${RUNTIME_PYTHON} -m quant_platform.web ; "
if [[ "$exec_start" != "$expected_exec_start_prefix"* ]]; then
  fail "systemd ExecStart does not match the release runtime"
fi

public_request_succeeded=false
: > "$probe_file"
if curl \
  --fail \
  --silent \
  --show-error \
  --output "$probe_file" \
  "${PUBLIC_URL}/health"; then
  public_request_succeeded=true
fi
if [[ "$public_request_succeeded" != true ]]; then
  fail "public health request failed"
fi
if ! grep -Fxq '{"status":"ok"}' "$probe_file"; then
  fail "public health response was not healthy"
fi

if ! root_status="$(
  curl \
    --silent \
    --show-error \
    --output /dev/null \
    --write-out '%{http_code}' \
    "${PUBLIC_URL}/"
)"; then
  fail "public root auth-boundary request failed"
fi
if [[ "$root_status" != "303" ]]; then
  fail "public root did not preserve the unauthenticated redirect"
fi

if ! api_status="$(
  curl \
    --silent \
    --show-error \
    --output /dev/null \
    --write-out '%{http_code}' \
    "${PUBLIC_URL}/api/operators"
)"; then
  fail "public API auth-boundary request failed"
fi
if [[ "$api_status" != "401" ]]; then
  fail "public API did not reject an unauthenticated request"
fi

restart_count="$(
  systemctl --user show "$SERVICE_NAME" --property=NRestarts --value
)"
if [[ "$restart_count" != "0" ]]; then
  fail "systemd restart count is not zero"
fi

deployment_succeeded=true
printf 'Release %s passed deployment verification.\n' "$RELEASE_ID"
