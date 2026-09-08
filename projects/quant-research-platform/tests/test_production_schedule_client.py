from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from quant_platform.production_client import ClientTLS, ProductionClientUnknown
from quant_platform.production_schedule_client import (
    DELIVERY,
    JOBS,
    SCHEDULE,
    _parser,
    run_job,
    scheduled_fire_for,
)


PROJECT = Path(__file__).parents[1]
PACKAGE = PROJECT / "src" / "quant_platform"
SCRIPTS = PROJECT / "scripts"


def jobs_file(tmp_path: Path, job_id: str) -> Path:
    job = JOBS[job_id]
    path = tmp_path / "jobs.json"
    path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": job.job_id,
                        "name": job.name,
                        "script": job.script,
                        "prompt": job.audit_prompt,
                        "deliver": DELIVERY,
                        "no_agent": True,
                        "enabled": True,
                        "schedule": {"kind": "cron", "expr": SCHEDULE},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, _transport):
        self.requests = []
        self.job = None
        self.__class__.instances.append(self)

    def submit_and_wait(self, request):
        self.requests.append(request)
        self.job = JOBS[request.job_id]
        payload = b"verified production notification"
        return {
            "schema": "quantresearch-production-result/v1",
            "job_id": self.job.job_id,
            "model_id": self.job.model_id,
            "production_manifest_sha256": self.job.production_manifest_sha256,
            "report_filename": self.job.report_filename,
            "automatic_ordering": False,
            "result_id": "a" * 64,
            "files": {
                "notification.txt": {
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                }
            },
        }

    def fetch_verified_file(self, _manifest, name):
        assert name == "notification.txt"
        return b"verified production notification"


@pytest.mark.parametrize("job_id", sorted(JOBS))
def test_exact_job_mapping_schedule_and_notification_output(tmp_path: Path, job_id: str) -> None:
    FakeClient.instances.clear()
    notification = run_job(
        JOBS[job_id],
        scheduled_for="2026-03-09T00:40:00Z",
        tls=ClientTLS("https://127.0.0.1:8443", Path("/unused"), Path("/unused"), Path("/unused")),
        jobs_path=jobs_file(tmp_path, job_id),
        transport_factory=lambda configuration: configuration,
        client_factory=FakeClient,
    )

    assert notification == b"verified production notification"
    value = FakeClient.instances[-1].requests[0]
    assert value.job_id == job_id
    assert value.production_manifest_sha256 == JOBS[job_id].production_manifest_sha256
    assert value.scheduled_for == "2026-03-09T00:40:00Z"
    assert value.request_id == value.expected_request_id


def test_scheduled_fire_is_deterministic_and_fails_closed() -> None:
    assert scheduled_fire_for(datetime.fromisoformat("2026-03-09T08:41:17+08:00")) == (
        "2026-03-09T00:40:00Z"
    )
    with pytest.raises(Exception, match="before"):
        scheduled_fire_for(datetime.fromisoformat("2026-03-09T08:39:59+08:00"))
    with pytest.raises(Exception, match="weekend"):
        scheduled_fire_for(datetime.fromisoformat("2026-03-08T09:00:00+08:00"))


def test_schedule_record_drift_and_unknown_outcome_fail_closed(tmp_path: Path) -> None:
    job = JOBS["1cd5557264db"]
    path = jobs_file(tmp_path, job.job_id)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["jobs"][0]["deliver"] = "wrong"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(Exception, match="differs"):
        run_job(
            job,
            scheduled_for="2026-03-09T00:40:00Z",
            tls=ClientTLS("https://127.0.0.1:8443", Path("/unused"), Path("/unused"), Path("/unused")),
            jobs_path=path,
            transport_factory=lambda configuration: configuration,
            client_factory=FakeClient,
        )

    class UnknownClient(FakeClient):
        def submit_and_wait(self, request):
            raise ProductionClientUnknown(request.request_id, "UNKNOWN")

    with pytest.raises(ProductionClientUnknown, match="UNKNOWN"):
        run_job(
            job,
            scheduled_for="2026-03-09T00:40:00Z",
            tls=ClientTLS("https://127.0.0.1:8443", Path("/unused"), Path("/unused"), Path("/unused")),
            jobs_path=jobs_file(tmp_path, job.job_id),
            transport_factory=lambda configuration: configuration,
            client_factory=UnknownClient,
        )


def test_cli_rejects_unknown_job() -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(["--job-id", "unknown"])


def test_reviewed_cutover_config_matches_the_executable_mapping() -> None:
    value = json.loads((PROJECT / "production" / "ailearn-schedule-cutover.json").read_text())
    assert value["api"]["base_url"] == "https://127.0.0.1:8443"
    assert {item["id"] for item in value["jobs"]} == set(JOBS)
    for item in value["jobs"]:
        job = JOBS[item["id"]]
        assert item == {
            "id": job.job_id,
            "name": job.name,
            "enabled": True,
            "no_agent": True,
            "schedule": SCHEDULE,
            "timezone": "Asia/Shanghai",
            "deliver": DELIVERY,
            "script": job.script,
            "model_id": job.model_id,
            "production_manifest_sha256": job.production_manifest_sha256,
            "prompt": job.audit_prompt,
        }
    assert len(value["rollback"]) == 2


def test_thin_client_imports_no_provider_or_computation_modules() -> None:
    wrappers = [
        SCRIPTS / "gold_production_api_action.py",
        SCRIPTS / "bocom_production_api_action.py",
    ]
    assert all(path.stat().st_mode & 0o111 for path in wrappers)
    paths = [
        PACKAGE / "production_client.py",
        PACKAGE / "production_schedule_client.py",
        *wrappers,
    ]
    forbidden = {
        "gold_research",
        "pandas",
        "numpy",
        "requests",
        "production_jobs",
        "production_gold",
        "production_bocom",
    }
    for path in paths:
        source = path.read_text(encoding="utf-8")
        modules = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module or "")
        assert not any(any(part in module for part in forbidden) for module in modules)
        assert "feng-learn" not in source.casefold()
        assert "zhlearn:" not in source.casefold()


def test_documented_thin_runtime_imports_schedule_client(tmp_path: Path) -> None:
    package = tmp_path / "quant_platform"
    package.mkdir()
    (package / "__init__.py").write_bytes(b"")
    for name in ("production_contract.py", "production_client.py", "production_schedule_client.py"):
        shutil.copyfile(PACKAGE / name, package / name)

    recipe = (PROJECT / "production" / "AILEARN-SCHEDULE-CUTOVER.md").read_text(
        encoding="utf-8"
    )
    assert 'install -m 0444 /dev/null "$runtime/__init__.py"' in recipe
    assert (
        "for name in production_contract.py production_client.py production_schedule_client.py; do"
        in recipe
    )

    completed = subprocess.run(
        [sys.executable, "-m", "quant_platform.production_schedule_client", "--help"],
        check=False,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Trigger one reviewed QuantResearch production job" in completed.stdout
