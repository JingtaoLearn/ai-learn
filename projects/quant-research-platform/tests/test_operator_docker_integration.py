import os
from pathlib import Path

import pandas as pd
import pytest

from quant_platform.catalog import initialize_catalog
from quant_platform.datasets import publish_snapshot
from quant_platform.experiment_service import ExperimentService
from quant_platform.operator_service import OperatorService
from quant_platform.resolved_runner import (
    ResolvedAttemptExecutor,
    effective_execution_identity,
)

from test_experiment_service import FIXTURE, _task
from test_operator_submission import _submission


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
