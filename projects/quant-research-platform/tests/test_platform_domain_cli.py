import json
from pathlib import Path

import pandas as pd

from quant_platform.cli import main
from quant_platform.datasets import publish_snapshot

from test_experiment_service import FIXTURE, _task


def _invoke(capsys, arguments):
    status = main(arguments)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    return status, json.loads(lines[0])


def _state(tmp_path: Path):
    root = tmp_path / "state"
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
    return root, snapshot["snapshot_id"]


def test_domain_cli_lists_seeded_catalog_and_template_as_json(tmp_path: Path, capsys):
    root, _ = _state(tmp_path)

    status, operators = _invoke(
        capsys, ["operator", "list", "--root", str(root)]
    )
    _, template = _invoke(
        capsys,
        [
            "template",
            "detail",
            "--root",
            str(root),
            "--name",
            "single_stock_daily_causal",
            "--version",
            "1",
        ],
    )

    assert status == 0
    assert operators["ok"] is True
    assert len(operators["operators"]) == 7
    assert template["template"]["slots"][0] == "fit"


def test_domain_cli_resolves_submits_duplicates_reruns_and_reads_history(
    tmp_path: Path, capsys
):
    root, snapshot_id = _state(tmp_path)
    spec = tmp_path / "task.json"
    spec.write_text(json.dumps(_task(snapshot_id)), encoding="utf-8")

    _, resolved = _invoke(
        capsys, ["task", "resolve", "--root", str(root), "--spec", str(spec)]
    )
    _, created = _invoke(
        capsys,
        [
            "task",
            "submit",
            "--root",
            str(root),
            "--spec",
            str(spec),
            "--action-id",
            "create",
        ],
    )
    _, duplicate = _invoke(
        capsys,
        [
            "task",
            "submit",
            "--root",
            str(root),
            "--spec",
            str(spec),
            "--action-id",
            "duplicate",
        ],
    )
    _, rerun = _invoke(
        capsys,
        [
            "task",
            "rerun",
            "--root",
            str(root),
            "--experiment-id",
            created["experiment_id"],
            "--action-id",
            "rerun",
        ],
    )
    _, experiments = _invoke(
        capsys, ["experiment", "list", "--root", str(root)]
    )
    _, attempts = _invoke(
        capsys,
        [
            "attempt",
            "list",
            "--root",
            str(root),
            "--experiment-id",
            created["experiment_id"],
        ],
    )

    assert resolved["resolved"]["operators"]["fit"]["resolved_version"] == "1.0.0"
    assert created["status"] == "CREATED"
    assert duplicate["status"] == "DUPLICATE"
    assert rerun["status"] == "CREATED"
    assert len(experiments["experiments"]) == 1
    assert len(attempts["attempts"]) == 2


def test_domain_cli_errors_are_single_json_objects(tmp_path: Path, capsys):
    root, _ = _state(tmp_path)

    status, result = _invoke(
        capsys,
        [
            "operator",
            "detail",
            "--root",
            str(root),
            "--operator-id",
            "missing",
        ],
    )

    assert status == 2
    assert result["ok"] is False
    assert "unknown" in result["error"]


def test_domain_cli_cannot_preclaim_a_study_internal_experiment_action(
    tmp_path: Path,
    capsys,
):
    root, snapshot_id = _state(tmp_path)
    spec = tmp_path / "task.json"
    spec.write_text(json.dumps(_task(snapshot_id)), encoding="utf-8")

    status, rejected = _invoke(
        capsys,
        [
            "task",
            "submit",
            "--root",
            str(root),
            "--spec",
            str(spec),
            "--action-id",
            f"study-internal:effect:{'a' * 64}",
        ],
    )
    _, experiments = _invoke(
        capsys,
        ["experiment", "list", "--root", str(root)],
    )

    assert status == 2
    assert "reserved internal namespace" in rejected["error"]
    assert experiments["experiments"] == []


def test_domain_cli_recovery_creates_a_distinct_attempt(tmp_path: Path, capsys):
    root, snapshot_id = _state(tmp_path)
    spec = tmp_path / "task.json"
    spec.write_text(json.dumps(_task(snapshot_id)), encoding="utf-8")
    _, created = _invoke(
        capsys,
        [
            "task",
            "submit",
            "--root",
            str(root),
            "--spec",
            str(spec),
            "--action-id",
            "create",
        ],
    )
    from quant_platform.cli import _domain_services

    _, service = _domain_services(str(root))
    claimed = service.claim_next_attempt()
    service.recover_abandoned_attempts(container_reconciler=lambda cidfile: True)
    _, replacement = _invoke(
        capsys,
        [
            "attempt",
            "recover",
            "--root",
            str(root),
            "--attempt-id",
            claimed["attempt_id"],
            "--action-id",
            "replacement",
        ],
    )

    assert replacement["status"] == "CREATED"
    assert replacement["attempt_id"] != created["attempt_id"]


def test_study_cli_uses_shared_composition_and_preserves_domain_statuses(
    tmp_path: Path,
    capsys,
    monkeypatch,
):
    root, _ = _state(tmp_path)
    from quant_platform.cli import _study_service

    composed = _study_service(str(root))
    assert composed.catalog is composed.experiments.catalog
    assert composed.datasets is composed.experiments.datasets

    class FakeStudies:
        def detail(self, study_id):
            return {"study_id": study_id, "phase": "FROZEN"}

        def advance(self, study_id):
            return {"status": study_id, "study_id": study_id}

        def control(self, study_id, operation, *, action_id):
            return {
                "status": operation,
                "study_id": study_id,
                "action_id": action_id,
            }

    monkeypatch.setattr(
        "quant_platform.cli._study_service",
        lambda root: FakeStudies(),
    )
    _, detail = _invoke(
        capsys,
        [
            "study",
            "detail",
            "--root",
            str(root),
            "--study-id",
            "detail-study",
        ],
    )
    assert detail["study"] == {
        "study_id": "detail-study",
        "phase": "FROZEN",
    }

    for status in ("LEASE_BUSY", "EXECUTION_IDENTITY_DRIFT"):
        exit_code, response = _invoke(
            capsys,
            [
                "study",
                "advance",
                "--root",
                str(root),
                "--study-id",
                status,
            ],
        )
        assert exit_code == 0
        assert response["status"] == status

    for status in ("ACTION_CONFLICT", "INVALID_TRANSITION"):
        exit_code, response = _invoke(
            capsys,
            [
                "study",
                "control",
                "--root",
                str(root),
                "--study-id",
                "controlled-study",
                "--operation",
                status,
                "--action-id",
                "control-action",
            ],
        )
        assert exit_code == 0
        assert response["status"] == status
