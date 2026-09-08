# Ailearn production-client cutover

This file is an operator recipe only. Running it is outside this source-correction action. The reviewed configuration is `ailearn-schedule-cutover.json`. The existing jobs keep their IDs, weekday 08:40 Asia/Shanghai schedule, enabled count, and Feishu destination.

## Install the reviewed client without third-party runtime dependencies

Run from the exact reviewed `projects/quant-research-platform` tree on ailearn:

```bash
set -euo pipefail
project_root="$PWD"
test -f "$project_root/production/ailearn-schedule-cutover.json"
runtime="$HOME/.hermes/lib/quantresearch-production-client/quant_platform"
install -d -m 0755 "$runtime" "$HOME/.hermes/scripts"
install -m 0444 /dev/null "$runtime/__init__.py"
for name in production_contract.py production_client.py production_schedule_client.py; do
  install -m 0444 "$project_root/src/quant_platform/$name" "$runtime/$name"
done
install -m 0555 "$project_root/scripts/gold_production_api_action.py" "$HOME/.hermes/scripts/gold_production_api_action.py"
install -m 0555 "$project_root/scripts/bocom_production_api_action.py" "$HOME/.hermes/scripts/bocom_production_api_action.py"
python3 -m compileall -q "$HOME/.hermes/lib/quantresearch-production-client"
sha256sum "$runtime"/*.py "$HOME/.hermes/scripts/gold_production_api_action.py" "$HOME/.hermes/scripts/bocom_production_api_action.py"
```

The host-managed tunnel must listen only on `127.0.0.1:8443` and forward to zhlearn `127.0.0.1:8443`. Install host-local mTLS files without printing their contents:

- `/home/jingtao/.hermes/secrets/quantresearch-production/client.crt`, regular single-link file, mode `0600` or `0644`;
- `/home/jingtao/.hermes/secrets/quantresearch-production/client.key`, regular single-link file, mode `0600`;
- `/home/jingtao/.hermes/secrets/quantresearch-production/server-ca.crt`, regular single-link file, mode `0600` or `0644`.

The client rejects non-loopback URLs, symlink components, hardlinked files, writable group/world bits, and group/world-readable private keys.

## Pre-cutover disabled-copy validation

Create a run-local jobs file from the reviewed desired records, then call the module directly with an explicit canonical fire. This does not edit Cron and must be used only after the disabled-copy API/tunnel gate is admitted:

```bash
set -euo pipefail
project_root="$PWD"
tmp_jobs="$(mktemp)"
trap 'rm -f "$tmp_jobs"' EXIT
python3 - "$project_root/production/ailearn-schedule-cutover.json" "$tmp_jobs" <<'PY'
import json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
jobs = []
for item in value["jobs"]:
    jobs.append({
        "id": item["id"], "name": item["name"], "enabled": item["enabled"],
        "no_agent": item["no_agent"], "schedule": {"kind": "cron", "expr": item["schedule"]},
        "deliver": item["deliver"], "script": item["script"], "prompt": item["prompt"],
    })
pathlib.Path(sys.argv[2]).write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
PY
PYTHONPATH="$HOME/.hermes/lib/quantresearch-production-client" python3 -m quant_platform.production_schedule_client --job-id 1cd5557264db --scheduled-for 2026-03-09T00:40:00Z --jobs-file "$tmp_jobs"
PYTHONPATH="$HOME/.hermes/lib/quantresearch-production-client" python3 -m quant_platform.production_schedule_client --job-id 297c11cad0dc --scheduled-for 2026-03-09T00:40:00Z --jobs-file "$tmp_jobs"
```

Use a newly admitted weekday 08:40 Asia/Shanghai fire instead of the example fire when performing the real disabled-copy validation. Both calls must return byte-identical verified `notification.txt` payloads from zhlearn and exit zero. Any `UNKNOWN`, TLS, tunnel, schedule, result, file-identity, model, or notification error fails closed; there is no local computation or alternate endpoint.

## Cut over the two existing records

Extract the exact reviewed prompts without shell re-encoding, pause both records, edit them in place, then resume both:

```bash
set -euo pipefail
config="$PWD/production/ailearn-schedule-cutover.json"
gold_prompt="$(python3 -c 'import json,sys; v=json.load(open(sys.argv[1])); print(next(x["prompt"] for x in v["jobs"] if x["id"]=="1cd5557264db"))' "$config")"
bocom_prompt="$(python3 -c 'import json,sys; v=json.load(open(sys.argv[1])); print(next(x["prompt"] for x in v["jobs"] if x["id"]=="297c11cad0dc"))' "$config")"
hermes cron pause 1cd5557264db
hermes cron pause 297c11cad0dc
hermes cron edit 1cd5557264db --name gold-production-daily-action --schedule '40 8 * * 1-5' --deliver feishu:oc_33bdb4845220ee3788fe50c50cf333ed --script gold_production_api_action.py --prompt "$gold_prompt" --no-agent
hermes cron edit 297c11cad0dc --name bocom-production-daily-action --schedule '40 8 * * 1-5' --deliver feishu:oc_33bdb4845220ee3788fe50c50cf333ed --script bocom_production_api_action.py --prompt "$bocom_prompt" --no-agent
hermes cron resume 1cd5557264db
hermes cron resume 297c11cad0dc
hermes cron list --all
```

Read back and require exactly one enabled record for each ID, with every field equal to `ailearn-schedule-cutover.json`, before any canary.

## Rollback

On any failed or ambiguous cutover/canary gate, pause both records and restore the sealed preimage fields from `rollback` in the same configuration. The old scripts are not overwritten by installation.

```bash
set -euo pipefail
config="$PWD/production/ailearn-schedule-cutover.json"
gold_prompt="$(python3 -c 'import json,sys; v=json.load(open(sys.argv[1])); print(next(x["prompt"] for x in v["rollback"] if x["id"]=="1cd5557264db"))' "$config")"
bocom_prompt="$(python3 -c 'import json,sys; v=json.load(open(sys.argv[1])); print(next(x["prompt"] for x in v["rollback"] if x["id"]=="297c11cad0dc"))' "$config")"
hermes cron pause 1cd5557264db
hermes cron pause 297c11cad0dc
sha256sum "$HOME/.hermes/scripts/gold_slope_daily_action.py" "$HOME/.hermes/scripts/bocom_trend_daily_action.py"
hermes cron edit 1cd5557264db --name gold-production-daily-action --schedule '40 8 * * 1-5' --deliver feishu:oc_33bdb4845220ee3788fe50c50cf333ed --script gold_slope_daily_action.py --prompt "$gold_prompt" --no-agent
hermes cron edit 297c11cad0dc --name bocom-production-daily-action --schedule '40 8 * * 1-5' --deliver feishu:oc_33bdb4845220ee3788fe50c50cf333ed --script bocom_trend_daily_action.py --prompt "$bocom_prompt" --no-agent
hermes cron resume 1cd5557264db
hermes cron resume 297c11cad0dc
hermes cron list --all
```

Before resuming, require old script SHA-256 values `0eb7fc82a384c52bda310044594f3b60f1ac857300152e163b2f06266012748b` (Gold) and `a334e275c8852c50ddd1b22b4129ad2817f7af094937cf2b7dcf560857dab1d8` (BOCOM). Preserve all failed-run and rollback evidence.
