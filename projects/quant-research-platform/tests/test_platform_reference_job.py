import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

from quant_platform.datasets import publish_snapshot
from quant_platform.isolation import build_docker_command
from quant_platform.reference_job import ReferenceJobError, run_reference_job
from quant_platform.submissions import publish_submission


def _foundation(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    root = tmp_path / "state"
    dataset = publish_snapshot(
        pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-08-18", "2026-08-19", "2026-08-20"]),
                "Open": [6.12, 6.18, 6.20],
                "High": [6.20, 6.24, 6.28],
                "Low": [6.08, 6.14, 6.18],
                "Close": [6.18, 6.20, 6.25],
                "Volume": [1200, 1100, 1300],
            }
        ),
        root,
        {
            "instrument": "601288.SS",
            "provider": "synthetic",
            "market": "XSHG",
            "currency": "CNY",
            "adjustment": "unadjusted",
        },
    )
    project = tmp_path / "project"
    (project / "src" / "quant_platform").mkdir(parents=True)
    (project / "tests").mkdir()
    source = Path(__file__).parents[1] / "src" / "quant_platform" / "reference_job.py"
    shutil.copyfile(source, project / "src" / "quant_platform" / "reference_job.py")
    (project / "src" / "quant_platform" / "__init__.py").write_text("")
    (project / "tests" / "test_reference.py").write_text(
        "def test_reference_source_is_frozen():\n    assert True\n"
    )
    (project / "pyproject.toml").write_text("[project]\nname='reference-job'\nversion='0.1'\n")
    (project / "requirements.in").write_text("pandas==2.3.1\npyarrow==21.0.0\n")
    (project / "requirements.lock").write_text("# synthetic test lock\n")
    submission = publish_submission(
        {
            "name": "reference-integrity-job",
            "entrypoint": "src/quant_platform/reference_job.py",
            "dataset_snapshot_id": dataset["snapshot_id"],
            "runner_image": "sha256:" + "a" * 64,
            "config": {},
            "seed": 20260826,
        },
        project,
        root,
    )
    attempt = root / "artifacts" / submission["submission_id"] / "attempt-001"
    attempt.mkdir(parents=True)
    artifacts = attempt / "payload"
    artifacts.mkdir()
    command = build_docker_command(
        Path(submission["path"]), Path(dataset["path"]), attempt
    )
    run_mount = next(value for value in command if "dst=/run-contract/run.json" in value)
    run_contract = Path(run_mount.split("src=", 1)[1].split(",dst=", 1)[0])
    return (
        Path(dataset["path"]),
        Path(submission["path"]) / "submission.json",
        run_contract,
        artifacts,
        Path(submission["path"]) / "source",
    )


def test_reference_job_writes_deterministic_snapshot_derived_outputs(tmp_path: Path):
    dataset, submission, run_contract, artifacts, workspace = _foundation(tmp_path)

    first = run_reference_job(
        dataset, submission, run_contract, artifacts, workspace=workspace
    )
    daily_bytes = (artifacts / "daily.csv").read_bytes()
    result_bytes = (artifacts / "result.json").read_bytes()
    daily = pd.read_csv(artifacts / "daily.csv")
    result = json.loads(result_bytes)

    assert first == {
        "result": str(artifacts / "result.json"),
        "daily": str(artifacts / "daily.csv"),
    }
    assert daily.columns.tolist() == ["Date", "Close", "DailyReturn", "NormalizedClose"]
    assert daily["Date"].tolist() == ["2026-08-18", "2026-08-19", "2026-08-20"]
    assert result["rows"] == 3
    assert result["data_start"] == "2026-08-18"
    assert result["data_end"] == "2026-08-20"
    assert "created_at" not in result
    assert "orders" not in result
    assert "broker" not in result

    replay_artifacts = tmp_path / "replay-artifacts"
    replay_artifacts.mkdir()
    run_reference_job(
        dataset,
        submission,
        run_contract,
        replay_artifacts,
        workspace=workspace,
    )
    assert (replay_artifacts / "daily.csv").read_bytes() == daily_bytes
    assert (replay_artifacts / "result.json").read_bytes() == result_bytes


def test_reference_job_rejects_tampered_dataset(tmp_path: Path):
    dataset, submission, run_contract, artifacts, workspace = _foundation(tmp_path)
    (dataset / "data.parquet").write_bytes(b"tampered")

    with pytest.raises(ReferenceJobError, match="dataset.*checksum"):
        run_reference_job(
            dataset, submission, run_contract, artifacts, workspace=workspace
        )

    assert not (artifacts / "result.json").exists()


def test_reference_job_rejects_contract_identity_mismatch(tmp_path: Path):
    dataset, submission, run_contract, artifacts, workspace = _foundation(tmp_path)
    contract = json.loads(run_contract.read_text())
    contract["dataset_snapshot_id"] = "f" * 64
    run_contract.chmod(0o644)
    run_contract.write_text(json.dumps(contract))

    with pytest.raises(ReferenceJobError, match="run contract identity"):
        run_reference_job(
            dataset, submission, run_contract, artifacts, workspace=workspace
        )
