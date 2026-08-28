from __future__ import annotations

import ast
import json
import os
import shutil
import stat
from pathlib import Path

import pandas as pd
import pytest

import quant_platform.study_datasets as study_datasets_module
from quant_platform.catalog import initialize_catalog
from quant_platform.datasets import (
    _canonical_data_bytes,
    _canonical_json,
    _sha256,
    _verify_snapshot,
    publish_snapshot,
)
from quant_platform.experiment_service import ExperimentService, TaskValidationError
from quant_platform.isolation import IsolationError, build_composed_execution_command
from quant_platform.parameter_study import (
    _fold_window as study_fold_window,
)
from quant_platform.strategy_operators import recursive_log_ema
from quant_platform.study_contracts import (
    INFORMATION_INTERVAL as CANONICAL_INFORMATION_INTERVAL,
    normalize_fold_window,
)
from quant_platform.study_datasets import (
    ExecutionDatasetSliceError,
    ExecutionDatasetSliceFactory,
)

METADATA = {
    "instrument": "SYNTH.SS",
    "provider": "synthetic",
    "market": "XSHG",
    "currency": "CNY",
    "adjustment": "unadjusted",
}
INFORMATION_INTERVAL = {
    "signal_time": "SESSION_CLOSE",
    "earliest_execution_time": "NEXT_SESSION_OPEN",
    "return_or_label_end_time": "EXECUTION_SESSION_CLOSE",
}


def test_dataset_protocol_does_not_depend_on_the_slice_factory():
    source = (
        Path(__file__).parents[1] / "src" / "quant_platform" / "datasets.py"
    ).read_text(encoding="utf-8")
    imports = {
        (node.level, node.module)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
    }

    assert (1, "study_datasets") not in imports


def test_slice_factory_public_api_remains_narrow():
    assert study_datasets_module.__all__ == [
        "ExecutionDatasetSliceError",
        "ExecutionDatasetSliceFactory",
    ]


def test_parameter_study_fold_window_obeys_the_neutral_merge_contract():
    sessions = [f"2026-01-{day:02d}" for day in range(1, 9)]
    window = study_fold_window(
        sessions,
        5,
        6,
        allowed_start=sessions[0],
        purge_sessions=1,
        role="INNER_SCORE",
        account_policy="FORCE_FLAT_WITH_COST",
    )

    assert normalize_fold_window(window, sessions) == window
    assert window["information_interval"] == CANONICAL_INFORMATION_INTERVAL


def _daily_frame(dates: list[str]) -> pd.DataFrame:
    closes = [10.0 + index for index in range(len(dates))]
    return pd.DataFrame(
        {
            "Date": pd.to_datetime(dates),
            "Open": [value - 0.1 for value in closes],
            "High": [value + 0.2 for value in closes],
            "Low": [value - 0.2 for value in closes],
            "Close": closes,
            "Volume": [1_000 + index for index in range(len(dates))],
        }
    )


def _parent_snapshot(root: Path, dates: list[str]) -> dict:
    published = publish_snapshot(_daily_frame(dates), root, METADATA)
    manifest = json.loads((Path(published["path"]) / "manifest.json").read_text(encoding="utf-8"))
    return {
        "instrument": METADATA["instrument"],
        "snapshot_id": published["snapshot_id"],
        "canonical_sha256": manifest["canonical_sha256"],
        "lineage": {"kind": "legacy_snapshot"},
        "path": published["path"],
    }


def _fold_window() -> dict:
    return {
        "allowed_start": "2026-01-01",
        "training_through": "2026-01-04",
        "available_through": "2026-01-07",
        "scoring_start": "2026-01-06",
        "scoring_end": "2026-01-07",
        "role": "INNER_SCORE",
        "information_interval": INFORMATION_INTERVAL,
        "account_policy": "FORCE_FLAT_WITH_COST",
    }


def _experiment_task(
    snapshot_id: str,
    *,
    evaluation_start: str = "2026-01-06",
    evaluation_end: str = "2026-01-07",
) -> dict:
    return {
        "schema_version": 1,
        "dataset": {
            "instrument": METADATA["instrument"],
            "snapshot_id": snapshot_id,
        },
        "template": {
            "name": "single_stock_daily_causal",
            "version": "1",
            "parameters": {
                "instrument_display_name": "Synthetic Bank",
                "evaluation_start": evaluation_start,
                "evaluation_end": evaluation_end,
            },
        },
        "operators": {
            "fit": {
                "operator_id": "prior_log_ols",
                "parameters": {"window_sessions": 2},
            },
            "smoothing": {
                "operator_id": "recursive_log_ema",
                "version": "1.0.0",
                "parameters": {"span_sessions": 1},
            },
            "statistic": {
                "operator_id": "adjacent_curve_pct_slope",
                "parameters": {},
            },
            "decision": {
                "operator_id": "post_start_threshold_crossing_hysteresis",
                "parameters": {
                    "buy_threshold_pct_per_day": 1.0,
                    "sell_threshold_abs_pct_per_day": 1.0,
                },
            },
            "sizing": {
                "operator_id": "all_in_all_out_a_share_lots",
                "parameters": {},
            },
            "cost": {
                "operator_id": "cms_china_a_share",
                "parameters": {},
            },
            "report": {
                "operator_id": "concise_chinese_causal_trade",
                "parameters": {},
            },
        },
    }


def test_materialize_writes_only_authorized_rows_and_a_separate_scoring_mask(
    tmp_path: Path,
):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    factory = ExecutionDatasetSliceFactory(tmp_path)

    result = factory.materialize(parent, _fold_window())

    target = Path(result["path"])
    projected = pd.read_parquet(target / "data.parquet")
    scoring_mask = json.loads((target / "scoring_mask.json").read_text(encoding="utf-8"))
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert result["status"] == "CREATED"
    assert target.parent == Path(parent["path"]).parent
    assert projected["Date"].dt.strftime("%Y-%m-%d").tolist() == dates[:7]
    assert scoring_mask == {
        "schema_version": 1,
        "date_column": "Date",
        "rows": [
            {"date": date, "scored": date in {"2026-01-06", "2026-01-07"}} for date in dates[:7]
        ],
    }
    assert set(target.iterdir()) == {
        target / "data.parquet",
        target / "manifest.json",
        target / "scoring_mask.json",
    }
    assert stat.S_IMODE(target.stat().st_mode) == 0o555
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o444 for path in target.iterdir())

    lineage = manifest["lineage"]
    assert lineage["kind"] == "derived_view"
    assert lineage["parent"] == {
        "instrument": METADATA["instrument"],
        "snapshot_id": parent["snapshot_id"],
        "canonical_sha256": parent["canonical_sha256"],
        "lineage": parent["lineage"],
    }
    assert lineage["view_spec"] == _fold_window()
    assert lineage["readable_range"] == {
        "start": "2026-01-01",
        "end": "2026-01-07",
    }
    assert lineage["scoring_mask"]["path"] == "scoring_mask.json"
    assert lineage["scoring_mask"]["scored_rows"] == 2
    assert len(lineage["scoring_mask"]["sha256"]) == 64
    assert lineage["projection_identity"] == {
        "name": "daily_market_data_prefix",
        "version": "1.0.0",
        "date_column": "Date",
        "boundary": "inclusive",
        "serialization": "parquet",
    }
    assert lineage["projected_bytes_sha256"] == manifest["parquet_sha256"]
    assert len(lineage["access_boundary_digest"]) == 64
    assert result["snapshot_id"] == manifest["snapshot_id"] == target.name
    assert "created_at" not in manifest


def test_shared_snapshot_verifier_recognizes_a_derived_slice(tmp_path: Path):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    result = ExecutionDatasetSliceFactory(tmp_path).materialize(
        parent,
        _fold_window(),
    )

    verified = _verify_snapshot(
        Path(result["path"]),
        result["snapshot_id"],
        include_frame=True,
    )

    assert isinstance(verified, tuple)
    manifest, frame = verified
    assert manifest["schema_version"] == 3
    assert manifest["lineage"]["kind"] == "derived_view"
    assert frame["Date"].dt.strftime("%Y-%m-%d").tolist() == dates[:7]


def test_derived_slice_runtime_verification_is_self_contained_without_parent_mount(
    tmp_path: Path,
):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    result = ExecutionDatasetSliceFactory(tmp_path).materialize(
        parent,
        _fold_window(),
    )
    parent_path = Path(parent["path"])
    parent_path.chmod(0o755)
    for path in parent_path.iterdir():
        path.chmod(0o644)
    shutil.rmtree(parent_path)

    manifest = _verify_snapshot(
        Path(result["path"]),
        result["snapshot_id"],
        verify_parent=False,
    )

    attestation = manifest["lineage"]["parent_verification"]
    assert attestation["protocol"] == "recursive_snapshot_verification"
    assert attestation["parent_identity_sha256"]
    assert attestation["parent_manifest_sha256"]


def test_snapshot_verification_defaults_to_recursive_host_parent_checks(
    tmp_path: Path,
):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    result = ExecutionDatasetSliceFactory(tmp_path).materialize(
        parent,
        _fold_window(),
    )
    parent_path = Path(parent["path"])
    parent_path.chmod(0o755)
    for path in parent_path.iterdir():
        path.chmod(0o644)
    shutil.rmtree(parent_path)

    with pytest.raises(RuntimeError, match="parent|snapshot"):
        _verify_snapshot(Path(result["path"]), result["snapshot_id"])


def test_materialize_is_deterministic_and_idempotent(tmp_path: Path):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    factory = ExecutionDatasetSliceFactory(tmp_path)

    first = factory.materialize(parent, _fold_window())
    target = Path(first["path"])
    before = {path.name: path.read_bytes() for path in target.iterdir()}
    second = factory.materialize(parent, _fold_window())

    assert second == {
        "status": "NO_CHANGE",
        "snapshot_id": first["snapshot_id"],
        "path": first["path"],
    }
    assert {path.name: path.read_bytes() for path in target.iterdir()} == before


def test_same_id_existing_target_rejects_forged_derived_publication_timestamp(
    tmp_path: Path,
):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    factory = ExecutionDatasetSliceFactory(tmp_path)
    first = factory.materialize(parent, _fold_window())
    target = Path(first["path"])
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["created_at"] = "2099-01-01T00:00:00Z"
    target.chmod(0o755)
    manifest_path.chmod(0o644)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o444)
    target.chmod(0o555)

    with pytest.raises(
        ExecutionDatasetSliceError,
        match="unexpected or missing manifest fields",
    ):
        factory.materialize(parent, _fold_window())


def test_materialize_rejects_final_target_substitution_before_return(
    tmp_path: Path,
    monkeypatch,
):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    original_fsync = study_datasets_module._fsync_directory
    substituted = False

    def substitute_after_rename(directory: Path):
        nonlocal substituted
        original_fsync(directory)
        targets = [
            path
            for path in directory.iterdir()
            if len(path.name) == 64 and path.name != parent["snapshot_id"]
        ]
        if directory.name == METADATA["instrument"] and targets and not substituted:
            target = targets[0]
            replacement = tmp_path / "replacement-manifest.json"
            replacement.write_text("{}", encoding="utf-8")
            target.chmod(0o755)
            (target / "manifest.json").unlink()
            (target / "manifest.json").symlink_to(replacement)
            target.chmod(0o555)
            substituted = True

    monkeypatch.setattr(
        study_datasets_module,
        "_fsync_directory",
        substitute_after_rename,
    )

    with pytest.raises(ExecutionDatasetSliceError, match="snapshot|file set"):
        ExecutionDatasetSliceFactory(tmp_path).materialize(
            parent,
            _fold_window(),
        )

    assert substituted is True


def test_fold_window_cannot_drop_the_earliest_parent_history(tmp_path: Path):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    narrowed = _fold_window() | {"allowed_start": "2026-01-02"}

    with pytest.raises(ExecutionDatasetSliceError, match="earliest parent history"):
        ExecutionDatasetSliceFactory(tmp_path).materialize(parent, narrowed)


def test_derived_snapshot_enters_existing_experiment_identity_without_new_task_fields(
    tmp_path: Path,
):
    catalog = initialize_catalog(tmp_path)
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    derived = ExecutionDatasetSliceFactory(tmp_path).materialize(
        parent,
        _fold_window(),
    )
    manifest = _verify_snapshot(Path(derived["path"]), derived["snapshot_id"])
    experiments = ExperimentService(catalog, execution_identity={"runner": "test"})
    task = _experiment_task(derived["snapshot_id"])

    resolved = experiments.resolve_task(task)
    preview = experiments.preview_task(task)

    assert set(task) == {"schema_version", "dataset", "template", "operators"}
    assert task["dataset"] == {
        "instrument": METADATA["instrument"],
        "snapshot_id": derived["snapshot_id"],
    }
    assert resolved["dataset"] == {
        "instrument": METADATA["instrument"],
        "snapshot_id": derived["snapshot_id"],
        "canonical_sha256": manifest["canonical_sha256"],
        "lineage": manifest["lineage"],
    }
    assert resolved["dataset"]["lineage"]["kind"] == "derived_view"
    assert preview["experiment_id"]


def test_experiment_rejects_evaluation_start_before_the_committed_scoring_mask(
    tmp_path: Path,
):
    catalog = initialize_catalog(tmp_path)
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    derived = ExecutionDatasetSliceFactory(tmp_path).materialize(
        parent,
        _fold_window(),
    )
    experiments = ExperimentService(catalog, execution_identity={"runner": "test"})

    with pytest.raises(TaskValidationError, match="evaluation_start.*scoring_start"):
        experiments.resolve_task(
            _experiment_task(
                derived["snapshot_id"],
                evaluation_start="2026-01-05",
            )
        )


def test_experiment_rejects_evaluation_end_after_the_committed_scoring_mask(
    tmp_path: Path,
):
    catalog = initialize_catalog(tmp_path)
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    derived = ExecutionDatasetSliceFactory(tmp_path).materialize(
        parent,
        _fold_window(),
    )
    experiments = ExperimentService(catalog, execution_identity={"runner": "test"})

    with pytest.raises(TaskValidationError, match="evaluation_end.*scoring_end"):
        experiments.resolve_task(
            _experiment_task(
                derived["snapshot_id"],
                evaluation_end="2026-01-08",
            )
        )


def test_experiment_resolution_recursively_verifies_the_derived_parent(
    tmp_path: Path,
):
    catalog = initialize_catalog(tmp_path)
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    derived = ExecutionDatasetSliceFactory(tmp_path).materialize(
        parent,
        _fold_window(),
    )
    parent_path = Path(parent["path"])
    parquet_path = parent_path / "data.parquet"
    parent_path.chmod(0o755)
    parquet_path.chmod(0o644)
    parquet_path.write_bytes(parquet_path.read_bytes() + b"tampered")
    parquet_path.chmod(0o444)
    parent_path.chmod(0o555)
    experiments = ExperimentService(catalog, execution_identity={"runner": "test"})

    with pytest.raises(TaskValidationError, match="parent|checksum|verification"):
        experiments.resolve_task(_experiment_task(derived["snapshot_id"]))


def test_host_verification_rejects_self_consistent_bytes_not_projected_from_parent(
    tmp_path: Path,
):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    derived = ExecutionDatasetSliceFactory(tmp_path).materialize(
        parent,
        _fold_window(),
    )
    target = Path(derived["path"])
    manifest_path = target / "manifest.json"
    parquet_path = target / "data.parquet"
    target.chmod(0o755)
    manifest_path.chmod(0o644)
    parquet_path.chmod(0o644)
    frame = pd.read_parquet(parquet_path)
    frame.loc[0, "Close"] += 0.01
    frame.to_parquet(parquet_path, index=False)
    parquet_payload = parquet_path.read_bytes()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["canonical_sha256"] = _sha256(_canonical_data_bytes(frame))
    manifest["parquet_sha256"] = _sha256(parquet_payload)
    lineage = manifest["lineage"]
    lineage["projected_bytes_sha256"] = manifest["parquet_sha256"]
    access_boundary = {
        "schema_version": 1,
        "parent": lineage["parent"],
        "parent_verification": lineage["parent_verification"],
        "view_spec": lineage["view_spec"],
        "projection_identity": lineage["projection_identity"],
        "projected_bytes_sha256": manifest["parquet_sha256"],
        "scoring_mask_sha256": manifest["scoring_mask_sha256"],
    }
    lineage["access_boundary_digest"] = _sha256(_canonical_json(access_boundary))
    identity = {
        "schema_version": 3,
        "metadata": manifest["metadata"],
        "canonical_sha256": manifest["canonical_sha256"],
        "lineage": lineage,
    }
    forged_snapshot_id = _sha256(_canonical_json(identity))
    manifest["snapshot_id"] = forged_snapshot_id
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o444)
    parquet_path.chmod(0o444)
    forged_target = target.parent / forged_snapshot_id
    target.rename(forged_target)
    forged_target.chmod(0o555)

    with pytest.raises(RuntimeError, match="parent|projection"):
        _verify_snapshot(
            forged_target,
            forged_snapshot_id,
            verify_parent=True,
        )


def test_derived_slice_verification_rejects_unsafe_permission_bits(tmp_path: Path):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    derived = ExecutionDatasetSliceFactory(tmp_path).materialize(
        parent,
        _fold_window(),
    )
    manifest_path = Path(derived["path"]) / "manifest.json"
    manifest_path.chmod(0o444 | stat.S_ISUID)

    with pytest.raises(RuntimeError, match="unsafe permissions"):
        _verify_snapshot(Path(derived["path"]), derived["snapshot_id"])


def test_custom_operator_launch_rejects_a_tampered_slice_before_execution(
    tmp_path: Path,
):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    derived = ExecutionDatasetSliceFactory(tmp_path).materialize(
        parent,
        _fold_window(),
    )
    target = Path(derived["path"])
    mask_path = target / "scoring_mask.json"
    target.chmod(0o755)
    mask_path.chmod(0o644)
    mask = json.loads(mask_path.read_text(encoding="utf-8"))
    mask["rows"][-1]["scored"] = False
    mask_path.write_text(json.dumps(mask), encoding="utf-8")
    mask_path.chmod(0o444)
    target.chmod(0o555)
    output = tmp_path / "output"
    output.mkdir()
    composition = tmp_path / "composition.json"
    composition.write_text("{}", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")

    with pytest.raises(IsolationError, match="dataset.*integrity"):
        build_composed_execution_command(
            dataset_dir=target,
            output_root=output,
            composition_file=composition,
            config_file=config,
            cidfile=tmp_path / "container.cid",
            operator_bundles={},
            runner_image="sha256:" + "a" * 64,
        )


def test_validated_study_reuse_rejects_legacy_full_snapshot_experiments(
    tmp_path: Path,
):
    catalog = initialize_catalog(tmp_path)
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    derived = ExecutionDatasetSliceFactory(tmp_path).materialize(
        parent,
        _fold_window(),
    )
    experiments = ExperimentService(catalog, execution_identity={"runner": "test"})
    legacy = experiments.submit(
        _experiment_task(parent["snapshot_id"]),
        action_id="legacy",
    )
    bounded = experiments.submit(
        _experiment_task(derived["snapshot_id"]),
        action_id="bounded",
    )

    with pytest.raises(TaskValidationError, match="access-boundary.*validated Study"):
        experiments.require_validated_study_dataset(legacy["experiment_id"])

    verified = experiments.require_validated_study_dataset(bounded["experiment_id"])
    assert verified["snapshot_id"] == derived["snapshot_id"]
    assert verified["lineage"]["kind"] == "derived_view"


def test_slice_factory_rejects_a_symlinked_state_root(tmp_path: Path):
    real_root = tmp_path / "real"
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(real_root, dates)
    alias = tmp_path / "alias"
    alias.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(ExecutionDatasetSliceError, match="symlink"):
        ExecutionDatasetSliceFactory(alias).materialize(parent, _fold_window())


def test_custom_operator_mount_exposes_only_the_derived_slice(tmp_path: Path):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    derived = ExecutionDatasetSliceFactory(tmp_path).materialize(
        parent,
        _fold_window(),
    )
    parent_path = Path(parent["path"])
    parent_path.chmod(0o755)
    for path in parent_path.iterdir():
        path.chmod(0o644)
    shutil.rmtree(parent_path)
    output = tmp_path / "output"
    output.mkdir()
    composition = tmp_path / "composition.json"
    composition.write_text("{}", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")

    command = build_composed_execution_command(
        dataset_dir=derived["path"],
        output_root=output,
        composition_file=composition,
        config_file=config,
        cidfile=tmp_path / "container.cid",
        operator_bundles={},
        runner_image="sha256:" + "a" * 64,
    )

    dataset_mounts = [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--mount" and "dst=/platform/datasets/" in command[index + 1]
    ]
    assert dataset_mounts == [
        (
            f"type=bind,src={derived['path']},"
            f"dst=/platform/datasets/{METADATA['instrument']}/{derived['snapshot_id']},"
            "readonly"
        )
    ]
    assert parent["snapshot_id"] not in " ".join(command)


def test_appending_future_parent_rows_preserves_projected_prefix_bytes(
    tmp_path: Path,
):
    original_dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    original_parent = _parent_snapshot(tmp_path, original_dates)
    factory = ExecutionDatasetSliceFactory(tmp_path)
    first = factory.materialize(original_parent, _fold_window())

    extended_parent = _parent_snapshot(
        tmp_path,
        [*original_dates, "2026-01-09"],
    )
    second = factory.materialize(extended_parent, _fold_window())

    first_path = Path(first["path"])
    second_path = Path(second["path"])
    first_manifest = json.loads((first_path / "manifest.json").read_text(encoding="utf-8"))
    second_manifest = json.loads((second_path / "manifest.json").read_text(encoding="utf-8"))
    assert first["snapshot_id"] != second["snapshot_id"]
    assert (first_path / "data.parquet").read_bytes() == (second_path / "data.parquet").read_bytes()
    assert (first_path / "scoring_mask.json").read_bytes() == (
        second_path / "scoring_mask.json"
    ).read_bytes()
    assert (
        first_manifest["lineage"]["projected_bytes_sha256"]
        == second_manifest["lineage"]["projected_bytes_sha256"]
    )
    assert first_manifest["canonical_sha256"] == second_manifest["canonical_sha256"]


def test_derived_view_lineage_recursively_binds_each_parent(tmp_path: Path):
    dates = [f"2026-01-{day:02d}" for day in range(1, 11)]
    original_parent = _parent_snapshot(tmp_path, dates)
    factory = ExecutionDatasetSliceFactory(tmp_path)
    outer = factory.materialize(
        original_parent,
        _fold_window()
        | {
            "training_through": "2026-01-06",
            "scoring_start": "2026-01-08",
            "scoring_end": "2026-01-09",
            "available_through": "2026-01-09",
            "role": "OUTER_AUDIT",
        },
    )
    outer_manifest = _verify_snapshot(Path(outer["path"]), outer["snapshot_id"])
    nested_parent = {
        "instrument": METADATA["instrument"],
        "snapshot_id": outer["snapshot_id"],
        "canonical_sha256": outer_manifest["canonical_sha256"],
        "lineage": outer_manifest["lineage"],
    }

    inner = factory.materialize(nested_parent, _fold_window())
    inner_manifest = _verify_snapshot(Path(inner["path"]), inner["snapshot_id"])

    assert inner_manifest["lineage"]["parent"] == nested_parent
    assert (
        inner_manifest["lineage"]["parent"]["lineage"]["parent"]["snapshot_id"]
        == original_parent["snapshot_id"]
    )


def test_derived_slice_verification_rejects_a_writable_output_seal(tmp_path: Path):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    derived = ExecutionDatasetSliceFactory(tmp_path).materialize(
        parent,
        _fold_window(),
    )
    target = Path(derived["path"])
    target.chmod(0o755)

    with pytest.raises(RuntimeError, match="writable"):
        _verify_snapshot(target, derived["snapshot_id"])


def test_derived_slice_verification_rejects_a_symlinked_file(tmp_path: Path):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    derived = ExecutionDatasetSliceFactory(tmp_path).materialize(
        parent,
        _fold_window(),
    )
    target = Path(derived["path"])
    mask_path = target / "scoring_mask.json"
    outside = tmp_path / "outside-mask.json"
    outside.write_bytes(mask_path.read_bytes())
    target.chmod(0o755)
    mask_path.unlink()
    mask_path.symlink_to(outside)
    target.chmod(0o555)

    with pytest.raises(RuntimeError, match="regular file|symlink"):
        _verify_snapshot(target, derived["snapshot_id"])


def test_derived_slice_verification_rejects_a_hardlinked_file(tmp_path: Path):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    derived = ExecutionDatasetSliceFactory(tmp_path).materialize(
        parent,
        _fold_window(),
    )
    target = Path(derived["path"])
    os.link(target / "data.parquet", tmp_path / "linked.parquet")

    with pytest.raises(RuntimeError, match="hard link"):
        _verify_snapshot(target, derived["snapshot_id"])


def test_host_verification_fails_when_the_derived_parent_is_tampered(
    tmp_path: Path,
):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    derived = ExecutionDatasetSliceFactory(tmp_path).materialize(
        parent,
        _fold_window(),
    )
    parent_path = Path(parent["path"])
    parquet_path = parent_path / "data.parquet"
    parent_path.chmod(0o755)
    parquet_path.chmod(0o644)
    parquet_path.write_bytes(parquet_path.read_bytes() + b"tampered")
    parquet_path.chmod(0o444)
    parent_path.chmod(0o555)

    with pytest.raises(RuntimeError, match="parent|Parquet checksum"):
        _verify_snapshot(
            Path(derived["path"]),
            derived["snapshot_id"],
            verify_parent=True,
        )


def test_scoring_mask_is_separate_from_readable_bytes_and_committed_by_identity(
    tmp_path: Path,
):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    factory = ExecutionDatasetSliceFactory(tmp_path)
    first = factory.materialize(parent, _fold_window())
    second = factory.materialize(
        parent,
        _fold_window() | {"scoring_start": "2026-01-05"},
    )

    first_path = Path(first["path"])
    second_path = Path(second["path"])
    first_manifest = json.loads((first_path / "manifest.json").read_text(encoding="utf-8"))
    second_manifest = json.loads((second_path / "manifest.json").read_text(encoding="utf-8"))
    assert (first_path / "data.parquet").read_bytes() == (second_path / "data.parquet").read_bytes()
    assert (first_path / "scoring_mask.json").read_bytes() != (
        second_path / "scoring_mask.json"
    ).read_bytes()
    assert first["snapshot_id"] != second["snapshot_id"]
    assert (
        first_manifest["lineage"]["access_boundary_digest"]
        != second_manifest["lineage"]["access_boundary_digest"]
    )


def test_recursive_ema_keeps_parent_history_while_warmup_is_not_scored(
    tmp_path: Path,
):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    derived = ExecutionDatasetSliceFactory(tmp_path).materialize(
        parent,
        _fold_window(),
    )
    parent_frame = pd.read_parquet(Path(parent["path"]) / "data.parquet").iloc[:7]
    slice_frame = pd.read_parquet(Path(derived["path"]) / "data.parquet")
    mask = json.loads((Path(derived["path"]) / "scoring_mask.json").read_text(encoding="utf-8"))

    expected = recursive_log_ema(parent_frame["Close"], span_sessions=4)
    actual = recursive_log_ema(slice_frame["Close"], span_sessions=4)

    pd.testing.assert_series_equal(actual, expected)
    assert [row["date"] for row in mask["rows"] if row["scored"]] == [
        "2026-01-06",
        "2026-01-07",
    ]
    assert all(not row["scored"] for row in mask["rows"][:5])


def test_custom_operator_rejects_output_mount_containing_the_parent_store(
    tmp_path: Path,
):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    derived = ExecutionDatasetSliceFactory(tmp_path).materialize(
        parent,
        _fold_window(),
    )
    composition = tmp_path / "composition.json"
    composition.write_text("{}", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")

    with pytest.raises(IsolationError, match="output.*dataset store"):
        build_composed_execution_command(
            dataset_dir=derived["path"],
            output_root=tmp_path,
            composition_file=composition,
            config_file=config,
            cidfile=tmp_path / "container.cid",
            operator_bundles={},
            runner_image="sha256:" + "a" * 64,
        )


def test_custom_operator_bundle_cannot_remount_a_parent_snapshot(tmp_path: Path):
    dates = [f"2026-01-{day:02d}" for day in range(1, 9)]
    parent = _parent_snapshot(tmp_path, dates)
    derived = ExecutionDatasetSliceFactory(tmp_path).materialize(
        parent,
        _fold_window(),
    )
    output = tmp_path / "output"
    output.mkdir()
    composition = tmp_path / "composition.json"
    composition.write_text("{}", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")

    with pytest.raises(IsolationError, match="operator bundle.*dataset store"):
        build_composed_execution_command(
            dataset_dir=derived["path"],
            output_root=output,
            composition_file=composition,
            config_file=config,
            cidfile=tmp_path / "container.cid",
            operator_bundles={"fit": Path(parent["path"])},
            runner_image="sha256:" + "a" * 64,
        )
