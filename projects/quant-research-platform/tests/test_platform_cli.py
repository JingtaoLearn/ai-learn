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


def _write_sessions_csv(path: Path, dates: list[str]) -> None:
    pd.DataFrame({"Date": dates}).to_csv(path, index=False)


def _update_args(
    root: Path, market_csv: Path, sessions_csv: Path, start: str, end: str
) -> list[str]:
    return [
        "data",
        "update",
        "--input",
        str(market_csv),
        "--expected-sessions",
        str(sessions_csv),
        "--start",
        start,
        "--end",
        end,
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


def test_cli_data_update_backfill_idempotency_and_revision_smoke(tmp_path: Path, capsys):
    root = tmp_path / "state"
    market_csv = tmp_path / "bars.csv"
    sessions_csv = tmp_path / "sessions.csv"
    _write_market_csv(market_csv)
    _write_sessions_csv(sessions_csv, ["2026-08-18", "2026-08-19"])
    args = _update_args(
        root, market_csv, sessions_csv, "2026-08-18", "2026-08-19"
    )

    assert main(args) == 0
    first = _json_output(capsys)
    assert first["ok"] is True
    assert first["status"] == "CREATED"
    assert set(first) == {
        "ok",
        "status",
        "snapshot_id",
        "path",
        "update_id",
        "update_path",
    }

    assert main(args) == 0
    unchanged = _json_output(capsys)
    assert unchanged["status"] == "NO_CHANGE"
    assert unchanged["snapshot_id"] == first["snapshot_id"]

    revised = pd.read_csv(market_csv)
    revised.loc[1, "Close"] = 6.21
    revised.loc[1, "High"] = 6.25
    revised.to_csv(market_csv, index=False)
    assert main(args) == 0
    revision = _json_output(capsys)
    assert revision["status"] == "CREATED"
    assert revision["snapshot_id"] != first["snapshot_id"]
    assert Path(first["path"]).is_dir()


def test_cli_incomplete_update_returns_json_and_preserves_latest(tmp_path: Path, capsys):
    root = tmp_path / "state"
    market_csv = tmp_path / "bars.csv"
    sessions_csv = tmp_path / "sessions.csv"
    _write_market_csv(market_csv)
    _write_sessions_csv(sessions_csv, ["2026-08-18", "2026-08-19"])
    args = _update_args(
        root, market_csv, sessions_csv, "2026-08-18", "2026-08-19"
    )
    assert main(args) == 0
    _json_output(capsys)
    latest = root / "datasets" / "601288.SS" / "latest.json"
    before = latest.read_bytes()

    _write_sessions_csv(
        sessions_csv, ["2026-08-18", "2026-08-19", "2026-08-20"]
    )
    failed_args = _update_args(
        root, market_csv, sessions_csv, "2026-08-18", "2026-08-20"
    )
    assert main(failed_args) == 2
    failure = _json_output(capsys)

    assert failure["ok"] is False
    assert "missing expected sessions" in failure["error"]
    assert latest.read_bytes() == before


def test_cli_update_requires_exactly_one_expected_session_column(tmp_path: Path, capsys):
    market_csv = tmp_path / "bars.csv"
    sessions_csv = tmp_path / "sessions.csv"
    _write_market_csv(market_csv)
    pd.DataFrame(
        {"Date": ["2026-08-18"], "source": ["calendar"]}
    ).to_csv(sessions_csv, index=False)

    code = main(
        _update_args(
            tmp_path / "state",
            market_csv,
            sessions_csv,
            "2026-08-18",
            "2026-08-19",
        )
    )
    failure = _json_output(capsys)

    assert code == 2
    assert "exactly one column named Date" in failure["error"]


def test_cli_update_rejects_headerless_sessions_without_moving_latest(
    tmp_path: Path, capsys
):
    root = tmp_path / "state"
    market_csv = tmp_path / "bars.csv"
    sessions_csv = tmp_path / "sessions.csv"
    _write_market_csv(market_csv)
    _write_sessions_csv(sessions_csv, ["2026-08-18"])
    initial_args = _update_args(
        root, market_csv, sessions_csv, "2026-08-18", "2026-08-18"
    )
    assert main(initial_args) == 0
    _json_output(capsys)
    latest = root / "datasets" / "601288.SS" / "latest.json"
    before = latest.read_bytes()

    pd.read_csv(market_csv).iloc[[1]].to_csv(market_csv, index=False)
    sessions_csv.write_text("2026-08-18\n2026-08-19\n", encoding="utf-8")
    failed_args = _update_args(
        root, market_csv, sessions_csv, "2026-08-18", "2026-08-19"
    )

    assert main(failed_args) == 2
    failure = _json_output(capsys)
    assert failure["ok"] is False
    assert "exactly one column named Date" in failure["error"]
    assert latest.read_bytes() == before


def test_cli_update_requires_case_sensitive_date_session_header(tmp_path: Path, capsys):
    market_csv = tmp_path / "bars.csv"
    sessions_csv = tmp_path / "sessions.csv"
    _write_market_csv(market_csv)
    pd.DataFrame({"date": ["2026-08-18", "2026-08-19"]}).to_csv(
        sessions_csv, index=False
    )

    code = main(
        _update_args(
            tmp_path / "state",
            market_csv,
            sessions_csv,
            "2026-08-18",
            "2026-08-19",
        )
    )
    failure = _json_output(capsys)

    assert code == 2
    assert "exactly one column named Date" in failure["error"]
    assert not (tmp_path / "state" / "datasets").exists()


def test_cli_run_returns_only_sealed_attempt_identity(
    tmp_path: Path, capsys, monkeypatch
):
    captured: dict = {}

    def fake_run(root, submission_id, attempt_id, timeout_seconds):
        captured.update(
            {
                "root": root,
                "submission_id": submission_id,
                "attempt_id": attempt_id,
                "timeout_seconds": timeout_seconds,
            }
        )
        return {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "run_id": "a" * 64,
            "submission_id": submission_id,
            "dataset_snapshot_id": "b" * 64,
            "outcome": "FAILED",
            "path": str(Path(root) / "artifacts" / submission_id / attempt_id),
            "stdout": "must not be emitted",
        }

    monkeypatch.setattr("quant_platform.cli.run_submission", fake_run)

    code = main(
        [
            "run",
            "--root",
            str(tmp_path / "state"),
            "--submission-id",
            "c" * 64,
            "--attempt-id",
            "attempt-001",
            "--timeout-seconds",
            "45.5",
        ]
    )
    result = _json_output(capsys)

    assert code == 0
    assert result == {
        "ok": True,
        "attempt_id": "attempt-001",
        "run_id": "a" * 64,
        "outcome": "FAILED",
        "path": str(
            tmp_path / "state" / "artifacts" / ("c" * 64) / "attempt-001"
        ),
    }
    assert captured["timeout_seconds"] == 45.5
