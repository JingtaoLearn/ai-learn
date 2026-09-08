from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .production_client import (
    ClientTLS,
    ProductionClient,
    ProductionClientError,
    StdlibMTLSTransport,
)
from .production_contract import ProductionContractError, ProductionRequest, canonical_scheduled_fire


SCHEDULE = "40 8 * * 1-5"
TIMEZONE = ZoneInfo("Asia/Shanghai")
DELIVERY = "feishu:oc_33bdb4845220ee3788fe50c50cf333ed"
DEFAULT_BASE_URL = "https://127.0.0.1:8443"


@dataclass(frozen=True)
class ScheduledJob:
    job_id: str
    name: str
    script: str
    model_id: str
    production_manifest_sha256: str
    report_filename: str
    audit_prompt: str


def _audit_prompt(
    script: str, job_id: str, model_id: str, production_manifest_sha256: str
) -> str:
    return (
        f"PRODUCTION AUDIT COPY — execute only {script}. "
        f"job_id={job_id}; model_id={model_id}; schedule={SCHEDULE} Asia/Shanghai; "
        "transport=loopback HTTPS mTLS via host-managed tunnel; "
        "behavior=trigger, verify immutable result/files, emit notification only; "
        "local_compute=false; flearn_fallback=false; automatic_ordering=false; "
        f"production_manifest_sha256={production_manifest_sha256}."
    )


_GOLD_MANIFEST = "8153c89ba46ce4a52b39b914a706aa3a321caacf01366e3f2e4e7087a2e3e745"
_BOCOM_MANIFEST = "6f9f10ed235c6229582ca2843c8b983a887dbdc0ac289170ca834e580bcae969"

JOBS = {
    "1cd5557264db": ScheduledJob(
        job_id="1cd5557264db",
        name="gold-production-daily-action",
        script="gold_production_api_action.py",
        model_id="gold-au9999-ols55-ema1-b0175-s0275-v1",
        production_manifest_sha256=_GOLD_MANIFEST,
        report_filename="f642b386-74c0-4e9f-92e6-563e7c6a5d69.html",
        audit_prompt=_audit_prompt(
            "gold_production_api_action.py",
            "1cd5557264db",
            "gold-au9999-ols55-ema1-b0175-s0275-v1",
            _GOLD_MANIFEST,
        ),
    ),
    "297c11cad0dc": ScheduledJob(
        job_id="297c11cad0dc",
        name="bocom-production-daily-action",
        script="bocom_production_api_action.py",
        model_id="bocom-20d-ema5-hysteresis-crossing-v1.2",
        production_manifest_sha256=_BOCOM_MANIFEST,
        report_filename="8991e9a8-1caa-41f5-b76b-6368259db5b4.html",
        audit_prompt=_audit_prompt(
            "bocom_production_api_action.py",
            "297c11cad0dc",
            "bocom-20d-ema5-hysteresis-crossing-v1.2",
            _BOCOM_MANIFEST,
        ),
    ),
}


def scheduled_fire_for(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ProductionContractError("current time must be timezone-aware")
    local = now.astimezone(TIMEZONE)
    if local.weekday() >= 5:
        raise ProductionContractError("scheduled client cannot derive a weekend fire")
    fire = local.replace(hour=8, minute=40, second=0, microsecond=0)
    if local < fire:
        raise ProductionContractError("scheduled client cannot run before its canonical fire")
    return canonical_scheduled_fire(fire.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))


def _read_jobs(path: Path) -> list[Mapping[str, Any]]:
    if not path.is_absolute():
        raise ProductionClientError("Cron jobs path must be absolute")
    metadata = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
        or metadata.st_size > 1_048_576
    ):
        raise ProductionClientError("Cron jobs file is unsafe")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionClientError("Cron jobs file is invalid") from exc
    jobs = value.get("jobs") if isinstance(value, dict) else None
    if not isinstance(jobs, list) or not all(isinstance(item, dict) for item in jobs):
        raise ProductionClientError("Cron jobs configuration is invalid")
    return jobs


def validate_schedule_record(job: ScheduledJob, jobs_path: Path) -> None:
    matches = [item for item in _read_jobs(jobs_path) if item.get("id") == job.job_id]
    if len(matches) != 1:
        raise ProductionClientError("exactly one scheduled job record is required")
    record = matches[0]
    expected = {
        "name": job.name,
        "script": job.script,
        "prompt": job.audit_prompt,
        "deliver": DELIVERY,
        "no_agent": True,
        "enabled": True,
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise ProductionClientError("scheduled job record differs from the reviewed client")
    schedule = record.get("schedule")
    if not isinstance(schedule, dict) or schedule.get("kind") != "cron" or schedule.get("expr") != SCHEDULE:
        raise ProductionClientError("scheduled job timing differs from the reviewed client")


def run_request(
    job: ScheduledJob,
    *,
    request: ProductionRequest,
    tls: ClientTLS,
    transport_factory: Callable[[ClientTLS], Any] = StdlibMTLSTransport,
    client_factory: Callable[[Any], Any] = ProductionClient,
) -> bytes:
    client = client_factory(transport_factory(tls))
    manifest = client.submit_and_wait(request)
    if (
        manifest.get("schema") != "quantresearch-production-result/v1"
        or manifest.get("job_id") != job.job_id
        or manifest.get("model_id") != job.model_id
        or manifest.get("production_manifest_sha256") != job.production_manifest_sha256
        or manifest.get("report_filename") != job.report_filename
        or manifest.get("automatic_ordering") is not False
    ):
        raise ProductionClientError("result does not match the scheduled job contract")
    report = client.fetch_verified_file(manifest, "report.html")
    if not report or b"<html" not in report[:4096].lower():
        raise ProductionClientError("report payload is invalid")
    notification = client.fetch_verified_file(manifest, "notification.txt")
    try:
        text = notification.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProductionClientError("notification payload is not UTF-8") from exc
    if not text or "\x00" in text or len(notification) > 16_384:
        raise ProductionClientError("notification payload is invalid")
    return notification


def run_job(
    job: ScheduledJob,
    *,
    scheduled_for: str,
    tls: ClientTLS,
    jobs_path: Path,
    transport_factory: Callable[[ClientTLS], Any] = StdlibMTLSTransport,
    client_factory: Callable[[Any], Any] = ProductionClient,
) -> bytes:
    validate_schedule_record(job, jobs_path)
    request = ProductionRequest.build(
        job_id=job.job_id,
        scheduled_for=scheduled_for,
        production_manifest_sha256=job.production_manifest_sha256,
    )
    return run_request(
        job,
        request=request,
        tls=tls,
        transport_factory=transport_factory,
        client_factory=client_factory,
    )


def run_validation(
    job: ScheduledJob,
    *,
    validation_for: str,
    validation_id: str,
    tls: ClientTLS,
    transport_factory: Callable[[ClientTLS], Any] = StdlibMTLSTransport,
    client_factory: Callable[[Any], Any] = ProductionClient,
) -> bytes:
    request = ProductionRequest.build_validation(
        job_id=job.job_id,
        validation_for=validation_for,
        validation_id=validation_id,
        production_manifest_sha256=job.production_manifest_sha256,
    )
    return run_request(
        job,
        request=request,
        tls=tls,
        transport_factory=transport_factory,
        client_factory=client_factory,
    )


def _parser() -> argparse.ArgumentParser:
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).absolute()
    secrets = home / "secrets" / "quantresearch-production"
    parser = argparse.ArgumentParser(description="Trigger one reviewed QuantResearch production job")
    parser.add_argument("--job-id", required=True, choices=sorted(JOBS))
    parser.add_argument("--scheduled-for")
    parser.add_argument("--validation-for")
    parser.add_argument("--validation-id")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--client-certificate", type=Path, default=secrets / "client.crt")
    parser.add_argument("--client-private-key", type=Path, default=secrets / "client.key")
    parser.add_argument("--server-ca", type=Path, default=secrets / "server-ca.crt")
    parser.add_argument("--jobs-file", type=Path, default=home / "cron" / "jobs.json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.scheduled_for and (args.validation_for or args.validation_id):
            raise ProductionContractError("scheduled and validation invocations are distinct")
        if bool(args.validation_for) != bool(args.validation_id):
            raise ProductionContractError("validation-for and validation-id are required together")
        tls = ClientTLS(
            base_url=args.base_url,
            client_certificate=args.client_certificate,
            client_private_key=args.client_private_key,
            server_ca=args.server_ca,
        )
        if args.validation_id:
            notification = run_validation(
                JOBS[args.job_id],
                validation_for=args.validation_for,
                validation_id=args.validation_id,
                tls=tls,
            )
        else:
            scheduled_for = args.scheduled_for or scheduled_fire_for(datetime.now(TIMEZONE))
            scheduled_for = canonical_scheduled_fire(scheduled_for)
            notification = run_job(
                JOBS[args.job_id],
                scheduled_for=scheduled_for,
                tls=tls,
                jobs_path=args.jobs_file,
            )
    except (OSError, ProductionClientError, ProductionContractError, ValueError) as exc:
        print(f"production client failed closed: {exc}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(notification)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
