import json
import os
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from quant_platform.attempt_report import REPORT_OPERATOR_ID, render_report_document
from quant_platform.catalog import initialize_catalog
from quant_platform.datasets import publish_snapshot
from quant_platform.experiment_service import ExperimentService
from quant_platform.operator_service import OperatorService
from quant_platform.resolved_runner import (
    ResolvedAttemptExecutor,
    effective_execution_identity,
)
from quant_platform.schemas import canonical_json_bytes

from test_experiment_service import FIXTURE, _task
from test_operator_submission import _submission


ATTEMPT_REPORT_FIXTURE = (
    Path(__file__).parent / "fixtures" / "attempt_report" / "conformance-v2.json"
)


@pytest.mark.skipif(
    os.environ.get("QUANT_RUN_DOCKER_INTEGRATION") != "1",
    reason="set QUANT_RUN_DOCKER_INTEGRATION=1 for the real isolation acceptance",
)
def test_all_custom_slots_validate_and_execute_in_one_real_docker_launch(
    tmp_path: Path,
):
    runner_image = os.environ["QUANT_TEST_RUNNER_IMAGE"]
    root = tmp_path / "state"
    catalog = initialize_catalog(root)
    frame = pd.read_csv(FIXTURE)
    frame["Date"] = pd.to_datetime(frame["Date"])
    snapshot = publish_snapshot(
        frame,
        root,
        {
            "instrument": "SYNTH.SS",
            "provider": "synthetic",
            "market": "XSHG",
            "currency": "CNY",
            "adjustment": "mixed",
        },
    )
    operators = OperatorService(catalog, runner_image=runner_image)
    task = _task(snapshot["snapshot_id"])
    for slot in task["operators"]:
        published = operators.submit(_submission(slot=slot))
        assert published["status"] == "CREATED"
        task["operators"][slot] = {
            "operator_id": f"fixture_{slot}",
            "version": "1.0.0",
            "parameters": {"window": 2},
        }

    experiments = ExperimentService(
        catalog,
        execution_identity=effective_execution_identity(
            Path(__file__).resolve().parents[1], runner_image
        ),
    )
    created = experiments.submit(task, action_id="docker-create")
    attempt = experiments.claim_next_attempt()
    assert attempt is not None
    result = ResolvedAttemptExecutor(
        catalog,
        output_root=root / "experiment-runs",
        project_root=Path(__file__).resolve().parents[1],
        runner_image=runner_image,
        attempt_controller=experiments,
    )(attempt)
    terminal = experiments.finish_success(
        attempt["attempt_id"],
        result_path=result["result_path"],
        result_digest=result["result_digest"],
    )

    assert terminal["attempt_id"] == created["attempt_id"]
    assert terminal["status"] == "SUCCEEDED"
    assert terminal["launch_count"] == 1
    report = (Path(result["result_path"]) / "report.html").read_text(encoding="utf-8")
    assert "document.body.dataset.ready='true'" in report


@pytest.mark.skipif(
    os.environ.get("QUANT_RUN_DOCKER_INTEGRATION") != "1",
    reason="set QUANT_RUN_DOCKER_INTEGRATION=1 for the real isolation acceptance",
)
def test_canonical_report_renders_in_network_denied_container_without_state_mount(
    tmp_path: Path,
):
    runner_image = os.environ["QUANT_TEST_RUNNER_IMAGE"]
    project_root = Path(__file__).resolve().parents[1]
    catalog = initialize_catalog(tmp_path / "state")
    detail = catalog.operator_detail(REPORT_OPERATOR_ID, "1.0.0")
    bundle = catalog.state_root / detail["bundle_path"]
    document = json.loads(ATTEMPT_REPORT_FIXTURE.read_text(encoding="utf-8"))[
        "report_documents"
    ]["TOTAL_RETURN_READ_TIME"]
    document_path = tmp_path / "report-document.json"
    document_path.write_bytes(canonical_json_bytes(document) + b"\n")
    command = [
        "docker", "run", "--rm", "--pull", "never", "--network", "none",
        "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
        "--cpus", "1.0", "--memory", "256m", "--pids-limit", "64",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--user", "1000:1000", "--env", "PYTHONPATH=/workspace/src",
        "--env", "MPLCONFIGDIR=/tmp/matplotlib",
        "--mount", f"type=bind,src={project_root.resolve()},dst=/workspace,readonly",
        "--mount", f"type=bind,src={bundle.resolve()},dst=/operator,readonly",
        "--mount", f"type=bind,src={document_path.resolve()},dst=/input/report-document.json,readonly",
        "--entrypoint", "python", runner_image, "-m", "quant_platform.operator_worker",
        "render-report", "/operator", "/input/report-document.json",
    ]
    completed = subprocess.run(command, check=True, capture_output=True)
    assert b"Traceback" not in completed.stderr
    assert completed.stdout == render_report_document(document)
    destinations = {
        argument.split(",dst=", 1)[1].split(",", 1)[0]
        for argument in command
        if ",dst=" in argument
    }
    assert destinations == {"/workspace", "/operator", "/input/report-document.json"}
