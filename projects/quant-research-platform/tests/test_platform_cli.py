import json
from pathlib import Path

import pandas as pd

from quant_platform.cli import main


def _write_market_csv(path: Path) -> None:
    pd.DataFrame(
        {
            "Date": ["2026-08-18", "2026-08-19"],
            "Open": [6.12, 6.18],
            "High": [6.20, 6.24],
            "Low": [6.08, 6.14],
            "Close": [6.18, 6.20],
            "Volume": [1200, 1100],
        }
    ).to_csv(path, index=False)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "src" / "main.py").write_text("def run():\n    return 1\n")
    (project / "tests" / "test_main.py").write_text("def test_true():\n    assert True\n")
    (project / "pyproject.toml").write_text("[project]\nname='cli-test'\nversion='0.1'\n")
    (project / "requirements.in").write_text("")
    (project / "requirements.lock").write_text("")
    return project


def _json_output(capsys) -> dict:
    output = capsys.readouterr()
    assert output.err == ""
    lines = output.out.strip().splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


def test_cli_snapshot_status_submit_and_show(tmp_path: Path, capsys):
    root = tmp_path / "state"
    market_csv = tmp_path / "abc.csv"
    _write_market_csv(market_csv)

    exit_code = main(
        [
            "data",
            "snapshot",
            "--input",
            str(market_csv),
            "--root",
            str(root),
            "--instrument",
            "601288.SS",
            "--provider",
            "synthetic",
            "--market",
            "XSHG",
            "--currency",
            "CNY",
            "--adjustment",
            "unadjusted",
        ]
    )
    snapshot = _json_output(capsys)
    assert exit_code == 0
    assert snapshot["status"] == "CREATED"

    assert main(["data", "status", "--root", str(root), "--instrument", "601288.SS"]) == 0
    status = _json_output(capsys)
    assert status["snapshot_id"] == snapshot["snapshot_id"]

    project = _project(tmp_path)
    spec_path = tmp_path / "experiment.json"
    spec_path.write_text(
        json.dumps(
            {
                "name": "cli-baseline",
                "entrypoint": "src/main.py",
                "dataset_snapshot_id": snapshot["snapshot_id"],
                "runner_image": "quant-runner@sha256:" + "a" * 64,
                "config": {"window": 20},
                "seed": 7,
            }
        )
    )
    assert (
        main(
            [
                "submit",
                "--spec",
                str(spec_path),
                "--project-root",
                str(project),
                "--root",
                str(root),
            ]
        )
        == 0
    )
    submission = _json_output(capsys)
    assert submission["status"] == "CREATED"

    assert (
        main(
            [
                "submission",
                "show",
                "--root",
                str(root),
                "--submission-id",
                submission["submission_id"],
            ]
        )
        == 0
    )
    shown = _json_output(capsys)
    assert shown["submission_id"] == submission["submission_id"]


def test_cli_invalid_input_returns_one_json_error_without_environment(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setenv("SENSITIVE_SENTINEL", "must-not-appear")
    code = main(
        [
            "data",
            "snapshot",
            "--input",
            str(tmp_path / "missing.csv"),
            "--root",
            str(tmp_path / "state"),
            "--instrument",
            "601288.SS",
            "--provider",
            "synthetic",
            "--market",
            "XSHG",
            "--currency",
            "CNY",
            "--adjustment",
            "unadjusted",
        ]
    )
    result = _json_output(capsys)
    assert code != 0
    assert result["ok"] is False
    assert "must-not-appear" not in json.dumps(result)
