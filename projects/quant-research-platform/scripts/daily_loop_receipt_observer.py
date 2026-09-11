#!/usr/bin/env python3
"""Project bounded Hermes Cron delivery receipts to the zhlearn production API."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import ssl
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener


JOB_IDS = ("1cd5557264db", "297c11cad0dc")
EXPECTED_SCHEDULES = {
    "1cd5557264db": "40 8 * * 1-5",
    "297c11cad0dc": "45 8 * * 1-5",
}
REPORT_URLS = {
    "1cd5557264db": "https://share.ai.jingtao.fun/f642b386-74c0-4e9f-92e6-563e7c6a5d69.html",
    "297c11cad0dc": "https://share.ai.jingtao.fun/8991e9a8-1caa-41f5-b76b-6368259db5b4.html",
}
SCHEMA = "quantresearch-cron-receipts/v2"


class ObservationError(RuntimeError):
    pass


def _regular_file(path: Path, label: str) -> None:
    metadata = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ObservationError(f"{label} is not a regular single-link file")


def _jobs(path: Path) -> dict[str, dict]:
    _regular_file(path, "Cron jobs file")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ObservationError("Cron jobs file is invalid") from exc
    records = value.get("jobs") if isinstance(value, dict) else None
    if not isinstance(records, list):
        raise ObservationError("Cron jobs file has no jobs list")
    selected = {
        item.get("id"): item
        for item in records
        if isinstance(item, dict) and item.get("id") in JOB_IDS
    }
    if set(selected) != set(JOB_IDS):
        raise ObservationError("expected Cron jobs are not uniquely available")
    return selected


def _execution(path: Path, job_id: str) -> dict | None:
    _regular_file(path, "Cron execution ledger")
    uri = f"file:{path}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id, status, claimed_at, started_at, finished_at "
            "FROM executions WHERE job_id = ? ORDER BY claimed_at DESC, id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise ObservationError("Cron execution ledger is unavailable") from exc
    finally:
        if "connection" in locals():
            connection.close()
    return None if row is None else dict(row)


def _canonical_action(document: bytes, job_id: str, report_uuid: str) -> str:
    try:
        text = document.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ObservationError("report read-back is not UTF-8") from exc
    matched = re.search(r'<pre data-action="canonical">(?P<action>.*?)</pre>', text, re.DOTALL)
    if matched is None:
        raise ObservationError("report read-back has no canonical action")
    try:
        action = json.loads(html.unescape(matched.group("action")))
        canonical = json.dumps(
            action,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ObservationError("report canonical action is invalid") from exc
    if (
        not isinstance(action, dict)
        or action.get("job_id") != job_id
        or action.get("report_uuid") != report_uuid
    ):
        raise ObservationError("report canonical action identity is invalid")
    return hashlib.sha256(canonical).hexdigest()


def _report_readback(url: str, job_id: str) -> dict[str, object]:
    opener = build_opener(ProxyHandler({}), HTTPSHandler())
    try:
        request = Request(url, headers={"User-Agent": "quantresearch-receipt-observer/1"})
        with opener.open(request, timeout=20) as response:
            payload = response.read(16 * 1024 * 1024 + 1)
            if (
                response.status != 200
                or len(payload) > 16 * 1024 * 1024
                or b"<html" not in payload[:4096].lower()
            ):
                raise ObservationError("report read-back response is invalid")
            action_sha256 = _canonical_action(
                payload, job_id, REPORT_URLS[job_id].rsplit("/", 1)[-1].removesuffix(".html")
            )
    except (OSError, HTTPError, ObservationError):
        return {"status": "FAILED", "sha256": None, "size": None, "action_sha256": None}
    return {
        "status": "OK",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "action_sha256": action_sha256,
    }


def _delivery_target(value: object) -> dict[str, str | None]:
    if not isinstance(value, str) or not value or len(value) > 4096:
        return {"channel": None, "destination_sha256": None}
    channel = value.partition(":")[0]
    if not channel or len(channel) > 32:
        return {"channel": None, "destination_sha256": None}
    return {
        "channel": channel,
        "destination_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
    }


def _payload(jobs_path: Path, executions_path: Path) -> bytes:
    source = _jobs(jobs_path)
    jobs = []
    for job_id in JOB_IDS:
        record = source[job_id]
        schedule = record.get("schedule")
        if (
            not isinstance(schedule, dict)
            or schedule.get("kind") != "cron"
            or schedule.get("expr") != EXPECTED_SCHEDULES[job_id]
        ):
            raise ObservationError("Cron schedule differs from the reviewed observation contract")
        if type(record.get("enabled")) is not bool:
            raise ObservationError("Cron enabled state is invalid")
        jobs.append(
            {
                "job_id": job_id,
                "enabled": record.get("enabled") is True,
                "schedule": {"kind": "cron", "expr": schedule["expr"]},
                "last_run_at": record.get("last_run_at"),
                "last_status": record.get("last_status"),
                "last_delivery_error": bool(record.get("last_delivery_error")),
                "delivery_target": _delivery_target(record.get("deliver")),
                "execution": _execution(executions_path, job_id),
                "report_readback": _report_readback(REPORT_URLS[job_id], job_id),
            }
        )
    return json.dumps(
        {
            "schema": SCHEMA,
            "observed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "jobs": jobs,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _parser() -> argparse.ArgumentParser:
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).absolute()
    secrets = home / "secrets" / "quantresearch-production"
    parser = argparse.ArgumentParser(description="Observe Gold/BOCOM Cron delivery receipts")
    parser.add_argument("--jobs-file", type=Path, default=home / "cron" / "jobs.json")
    parser.add_argument(
        "--executions-file", type=Path, default=home / "cron" / "executions.db"
    )
    parser.add_argument(
        "--url",
        default="https://127.0.0.1:8443/api/v1/production/daily-loop/cron-receipts",
    )
    parser.add_argument("--client-certificate", type=Path, default=secrets / "client.crt")
    parser.add_argument("--client-private-key", type=Path, default=secrets / "client.key")
    parser.add_argument("--server-ca", type=Path, default=secrets / "server-ca.crt")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        parsed = urlsplit(args.url)
        if parsed.scheme != "https" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ObservationError("receipt endpoint must be loopback HTTPS")
        for path, label in (
            (args.client_certificate, "client certificate"),
            (args.client_private_key, "client private key"),
            (args.server_ca, "server CA"),
        ):
            _regular_file(path, label)
        context = ssl.create_default_context(cafile=str(args.server_ca))
        context.load_cert_chain(str(args.client_certificate), str(args.client_private_key))
        request = Request(
            args.url,
            data=_payload(args.jobs_file.absolute(), args.executions_file.absolute()),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context))
        with opener.open(request, timeout=20) as response:
            body = response.read(4097)
            if response.status != 200 or len(body) > 4096:
                raise ObservationError("receipt endpoint rejected the observation")
        value = json.loads(body)
        if value.get("ok") is not True or not isinstance(value.get("receipt_sha256"), str):
            raise ObservationError("receipt endpoint read-back is invalid")
    except HTTPError as exc:
        detail = exc.read(1024).decode("utf-8", errors="replace")
        print(
            f"daily-loop receipt observation failed closed: HTTP {exc.code}: {detail}",
            file=sys.stderr,
        )
        return 1
    except (OSError, ssl.SSLError, json.JSONDecodeError, ObservationError) as exc:
        print(f"daily-loop receipt observation failed closed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
