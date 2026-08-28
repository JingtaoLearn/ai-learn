import ast
import json
import os
import shutil
from pathlib import Path

import pandas as pd
import pytest

import quant_platform.datasets as datasets_module
from quant_platform.datasets import (
    DatasetValidationError,
    _verify_snapshot,
    publish_snapshot,
    snapshot_status,
)


def test_dataset_lineage_import_graph_is_acyclic():
    package = Path(datasets_module.__file__).parent

    def relative_imports(module: str) -> set[str]:
        tree = ast.parse((package / f"{module}.py").read_text(encoding="utf-8"))
        return {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.level == 1
            and node.module is not None
        }

    assert "updates" not in relative_imports("datasets")
    assert {"datasets", "updates"}.isdisjoint(relative_imports("dataset_lineage"))
    assert "dataset_lineage" in relative_imports("datasets")
    assert "dataset_lineage" in relative_imports("updates")


def _daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-08-17", "2026-08-18", "2026-08-19"]),
            "Open": [6.10, 6.12, 6.18],
            "High": [6.15, 6.20, 6.24],
            "Low": [6.05, 6.08, 6.14],
            "Close": [6.12, 6.18, 6.20],
            "Volume": [1000, 1200, 1100],
        }
    )


def _metadata() -> dict[str, str]:
    return {
        "instrument": "601288.SS",
        "provider": "synthetic",
        "market": "XSHG",
        "currency": "CNY",
        "adjustment": "unadjusted",
    }


def test_publish_snapshot_is_content_addressed_atomic_and_idempotent(tmp_path: Path):
    first = publish_snapshot(_daily_frame(), tmp_path, _metadata())
    second = publish_snapshot(_daily_frame().iloc[::-1], tmp_path, _metadata())

    assert first["status"] == "CREATED"
    assert second["status"] == "NO_CHANGE"
    assert first["snapshot_id"] == second["snapshot_id"]

    snapshot_dir = Path(first["path"])
    manifest = json.loads((snapshot_dir / "manifest.json").read_text())
    assert (snapshot_dir / "data.parquet").exists()
    assert len(manifest["canonical_sha256"]) == 64
    assert len(manifest["parquet_sha256"]) == 64
    assert manifest["rows"] == 3
    assert manifest["data_start"] == "2026-08-17"
    assert manifest["data_end"] == "2026-08-19"
    assert (snapshot_dir.stat().st_mode & 0o777) == 0o555
    assert ((snapshot_dir / "manifest.json").stat().st_mode & 0o777) == 0o444
    assert ((snapshot_dir / "data.parquet").stat().st_mode & 0o777) == 0o444
    assert (snapshot_dir.parent.stat().st_mode & 0o777) == 0o755
    assert (
        (snapshot_dir.parent / "latest.json").stat().st_mode & 0o777
    ) == 0o644

    status = snapshot_status(tmp_path, "601288.SS")
    assert status["snapshot_id"] == first["snapshot_id"]
    assert status["path"] == first["path"]


def test_publish_rejects_a_post_seal_hardlink_before_rename(
    tmp_path: Path,
    monkeypatch,
):
    original_seal = datasets_module._seal_snapshot

    def hardlink_after_seal(directory: Path, *args, **kwargs):
        original_seal(directory, *args, **kwargs)
        os.link(directory / "data.parquet", tmp_path / "escaped.parquet")

    monkeypatch.setattr(datasets_module, "_seal_snapshot", hardlink_after_seal)

    with pytest.raises((DatasetValidationError, RuntimeError), match="hard link"):
        publish_snapshot(_daily_frame(), tmp_path, _metadata())

    instrument_root = tmp_path / "datasets" / _metadata()["instrument"]
    assert not any(
        len(path.name) == 64 for path in instrument_root.iterdir()
    )


def test_publish_rejects_post_seal_mutation_before_rename(
    tmp_path: Path,
    monkeypatch,
):
    original_seal = datasets_module._seal_snapshot

    def mutate_after_seal(directory: Path, *args, **kwargs):
        original_seal(directory, *args, **kwargs)
        parquet = directory / "data.parquet"
        parquet.chmod(0o644)
        parquet.write_bytes(parquet.read_bytes() + b"post-seal mutation")
        parquet.chmod(0o444)

    monkeypatch.setattr(datasets_module, "_seal_snapshot", mutate_after_seal)

    with pytest.raises(RuntimeError, match="checksum"):
        publish_snapshot(_daily_frame(), tmp_path, _metadata())

    instrument_root = tmp_path / "datasets" / _metadata()["instrument"]
    assert not any(
        len(path.name) == 64 for path in instrument_root.iterdir()
    )


def test_publish_never_replaces_a_racing_identity_directory(
    tmp_path: Path,
    monkeypatch,
):
    original_fsync = datasets_module._fsync_directory
    raced_target: Path | None = None

    def inject_empty_target(directory: Path):
        nonlocal raced_target
        original_fsync(directory)
        manifest_path = directory / "manifest.json"
        if directory.name.startswith(".") and manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            target = directory.parent / manifest["snapshot_id"]
            if not target.exists():
                target.mkdir()
                raced_target = target

    monkeypatch.setattr(datasets_module, "_fsync_directory", inject_empty_target)

    with pytest.raises(RuntimeError, match="snapshot|directory|file set"):
        publish_snapshot(_daily_frame(), tmp_path, _metadata())

    assert raced_target is not None
    assert raced_target.is_dir()
    assert not any(raced_target.iterdir())


def test_historical_revision_creates_new_snapshot_without_overwriting_old(tmp_path: Path):
    first = publish_snapshot(_daily_frame(), tmp_path, _metadata())
    revised = _daily_frame()
    revised.loc[1, "Close"] = 6.17
    second = publish_snapshot(revised, tmp_path, _metadata())

    assert second["status"] == "CREATED"
    assert second["snapshot_id"] != first["snapshot_id"]
    assert Path(first["path"]).exists()
    assert Path(second["path"]).exists()
    assert snapshot_status(tmp_path, "601288.SS")["snapshot_id"] == second["snapshot_id"]


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda frame: frame.drop(columns=["Open"]), "missing required columns"),
        (lambda frame: pd.concat([frame, frame.iloc[[0]]]), "duplicate dates"),
        (lambda frame: frame.assign(Close=[6.12, float("nan"), 6.20]), "non-finite"),
        (lambda frame: frame.assign(Open=[True, False, True]), "boolean"),
        (lambda frame: frame.assign(Open=[6.10, 0.0, 6.18]), "strictly positive"),
        (lambda frame: frame.assign(Volume=[1000, -1, 1100]), "non-negative"),
        (lambda frame: frame.assign(High=[6.15, 6.10, 6.24]), "High"),
        (lambda frame: frame.assign(Low=[6.05, 6.30, 6.14]), "Low"),
    ],
)
def test_snapshot_rejects_invalid_market_data(tmp_path: Path, mutator, message: str):
    with pytest.raises(DatasetValidationError, match=message):
        publish_snapshot(mutator(_daily_frame()), tmp_path, _metadata())


def test_snapshot_rejects_unsafe_instrument_and_unknown_metadata(tmp_path: Path):
    unsafe = _metadata() | {"instrument": "../601288"}
    unknown = _metadata() | {"retrieved_at": "caller-controlled"}

    with pytest.raises(DatasetValidationError, match="instrument"):
        publish_snapshot(_daily_frame(), tmp_path, unsafe)
    with pytest.raises(DatasetValidationError, match="metadata fields"):
        publish_snapshot(_daily_frame(), tmp_path, unknown)


def test_existing_snapshot_corruption_fails_closed(tmp_path: Path):
    published = publish_snapshot(_daily_frame(), tmp_path, _metadata())
    parquet = Path(published["path"]) / "data.parquet"
    parquet.chmod(0o644)
    parquet.write_bytes(b"corrupted")

    with pytest.raises(RuntimeError, match="corrupt snapshot"):
        publish_snapshot(_daily_frame(), tmp_path, _metadata())


def test_latest_pointer_cannot_redirect_outside_instrument_store(tmp_path: Path):
    published = publish_snapshot(_daily_frame(), tmp_path, _metadata())
    pointer = tmp_path / "datasets" / "601288.SS" / "latest.json"
    pointer.write_text(
        json.dumps({"snapshot_id": published["snapshot_id"], "path": str(tmp_path)})
    )

    with pytest.raises(RuntimeError, match="latest snapshot pointer"):
        snapshot_status(tmp_path, "601288.SS")


def test_latest_lock_rejects_a_symlinked_descendant_without_path_escape(
    tmp_path: Path,
):
    state = tmp_path / "state"
    locks = state / ".locks"
    locks.mkdir(parents=True)
    locks.chmod(0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    (locks / "latest").symlink_to(outside, target_is_directory=True)

    with pytest.raises(DatasetValidationError, match="lock.*symlink|symlink.*lock"):
        publish_snapshot(_daily_frame(), state, _metadata())

    assert not (outside / "601288.SS.lock").exists()


def test_latest_lock_rejects_a_hardlinked_lock_file(tmp_path: Path):
    latest = tmp_path / "state" / ".locks" / "latest"
    latest.mkdir(parents=True)
    latest.parent.chmod(0o700)
    latest.chmod(0o700)
    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"shared lock identity")
    outside.chmod(0o600)
    os.link(outside, latest / "601288.SS.lock")

    with pytest.raises(DatasetValidationError, match="hard link"):
        publish_snapshot(_daily_frame(), tmp_path / "state", _metadata())


def test_snapshot_pointer_survives_restore_under_a_different_root(tmp_path: Path):
    original = tmp_path / "original"
    published = publish_snapshot(_daily_frame(), original, _metadata())
    pointer = json.loads(
        (original / "datasets" / "601288.SS" / "latest.json").read_text()
    )
    assert pointer["path"] == published["snapshot_id"]

    restored = tmp_path / "restored"
    shutil.copytree(original, restored)
    status = snapshot_status(restored, "601288.SS")
    assert status["snapshot_id"] == published["snapshot_id"]
    assert Path(status["path"]).is_relative_to(restored)


def test_snapshot_identity_preserves_full_float64_precision(tmp_path: Path):
    first_frame = _daily_frame()
    first_frame.loc[2, "Close"] = 6.20000000001
    second_frame = _daily_frame()
    second_frame.loc[2, "Close"] = 6.20000000002

    first = publish_snapshot(first_frame, tmp_path, _metadata())
    second = publish_snapshot(second_frame, tmp_path, _metadata())

    assert first["snapshot_id"] != second["snapshot_id"]
    assert second["status"] == "CREATED"


def test_daily_snapshot_rejects_timezone_aware_or_intraday_dates(tmp_path: Path):
    timezone_aware = _daily_frame()
    timezone_aware["Date"] = pd.to_datetime(timezone_aware["Date"]).dt.tz_localize(
        "Asia/Shanghai"
    )
    intraday = _daily_frame()
    intraday.loc[1, "Date"] = pd.Timestamp("2026-08-18 09:30:00")

    with pytest.raises(DatasetValidationError, match="timezone-naive"):
        publish_snapshot(timezone_aware, tmp_path, _metadata())
    with pytest.raises(DatasetValidationError, match="midnight"):
        publish_snapshot(intraday, tmp_path, _metadata())


def test_daily_snapshot_rejects_empty_data(tmp_path: Path):
    with pytest.raises(DatasetValidationError, match="at least one row"):
        publish_snapshot(_daily_frame().iloc[0:0], tmp_path, _metadata())


def test_adjusted_close_is_normalized_preserved_and_bound_to_v2_identity(tmp_path: Path):
    legacy = _daily_frame().assign(**{"Adj Close": [3.01, 3.04, 3.07]})
    first = publish_snapshot(legacy, tmp_path, _metadata())
    canonical = _daily_frame().assign(AdjustedClose=[3.01, 3.04, 3.07])
    unchanged = publish_snapshot(canonical, tmp_path, _metadata())
    revised = canonical.copy()
    revised.loc[1, "AdjustedClose"] = 3.05
    second = publish_snapshot(revised, tmp_path, _metadata())

    manifest = json.loads((Path(first["path"]) / "manifest.json").read_text())
    persisted = pd.read_parquet(Path(first["path"]) / "data.parquet")
    assert manifest["schema_version"] == 2
    assert manifest["columns"] == [
        "Date",
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
        "AdjustedClose",
    ]
    assert persisted.columns.tolist() == manifest["columns"]
    assert "Adj Close" not in persisted
    assert unchanged["snapshot_id"] == first["snapshot_id"]
    assert second["snapshot_id"] != first["snapshot_id"]


def test_required_only_snapshot_remains_legacy_v1(tmp_path: Path):
    published = publish_snapshot(_daily_frame(), tmp_path, _metadata())
    manifest = json.loads((Path(published["path"]) / "manifest.json").read_text())

    assert manifest["schema_version"] == 1
    assert "columns" not in manifest


def test_adjusted_close_snapshot_rejects_alias_collision_and_invalid_values(tmp_path: Path):
    collision = _daily_frame().assign(
        AdjustedClose=[3.01, 3.04, 3.07],
        **{"Adj Close": [3.01, 3.04, 3.07]},
    )
    invalid = _daily_frame().assign(AdjustedClose=[3.01, float("inf"), 3.07])

    with pytest.raises(DatasetValidationError, match="both AdjustedClose and Adj Close"):
        publish_snapshot(collision, tmp_path, _metadata())
    with pytest.raises(DatasetValidationError, match="non-finite"):
        publish_snapshot(invalid, tmp_path, _metadata())


@pytest.mark.parametrize("tamper", ["manifest", "parquet"])
def test_adjusted_close_snapshot_tampering_fails_closed(tmp_path: Path, tamper: str):
    published = publish_snapshot(
        _daily_frame().assign(AdjustedClose=[3.01, 3.04, 3.07]),
        tmp_path,
        _metadata(),
    )
    target = Path(published["path"])
    target.chmod(0o755)
    if tamper == "manifest":
        manifest = json.loads((target / "manifest.json").read_text())
        manifest["columns"] = manifest["columns"][:-1]
        (target / "manifest.json").chmod(0o644)
        (target / "manifest.json").write_text(json.dumps(manifest))
    else:
        frame = pd.read_parquet(target / "data.parquet")
        frame = frame.drop(columns=["AdjustedClose"])
        (target / "data.parquet").chmod(0o644)
        frame.to_parquet(target / "data.parquet", index=False)

    with pytest.raises(RuntimeError, match="corrupt snapshot"):
        _verify_snapshot(target, published["snapshot_id"])


@pytest.mark.parametrize("component", ["directory", "manifest", "parquet"])
def test_snapshot_verification_rejects_writable_components(
    tmp_path: Path, component: str
):
    published = publish_snapshot(_daily_frame(), tmp_path, _metadata())
    target = Path(published["path"])
    path = target if component == "directory" else target / (
        "manifest.json" if component == "manifest" else "data.parquet"
    )
    path.chmod(0o755 if component == "directory" else 0o644)

    with pytest.raises(RuntimeError, match="writable"):
        _verify_snapshot(target, published["snapshot_id"])


@pytest.mark.parametrize("topology", ["extra", "missing", "symlink", "hardlink", "fifo"])
def test_snapshot_verification_rejects_unsafe_file_topology(
    tmp_path: Path, topology: str
):
    published = publish_snapshot(_daily_frame(), tmp_path, _metadata())
    target = Path(published["path"])
    parquet = target / "data.parquet"
    target.chmod(0o755)
    if topology == "extra":
        (target / "extra.txt").write_text("unexpected", encoding="utf-8")
    elif topology == "missing":
        parquet.unlink()
    elif topology == "symlink":
        payload = tmp_path / "outside.parquet"
        payload.write_bytes(parquet.read_bytes())
        parquet.unlink()
        parquet.symlink_to(payload)
    elif topology == "hardlink":
        os.link(parquet, tmp_path / "linked.parquet")
    else:
        parquet.unlink()
        os.mkfifo(parquet)
    target.chmod(0o555)

    with pytest.raises(RuntimeError, match="topology|regular|link|file set"):
        _verify_snapshot(target, published["snapshot_id"])


def test_snapshot_verification_rejects_symlinked_instrument_parent(tmp_path: Path):
    published = publish_snapshot(_daily_frame(), tmp_path, _metadata())
    real_instrument = Path(published["path"]).parent
    alias = tmp_path / "alias" / "601288.SS"
    alias.parent.mkdir()
    alias.symlink_to(real_instrument, target_is_directory=True)

    with pytest.raises(RuntimeError, match="topology|symlink"):
        _verify_snapshot(alias / published["snapshot_id"], published["snapshot_id"])


def test_snapshot_verification_returns_frame_from_the_hashed_parquet_payload(
    tmp_path: Path
):
    published = publish_snapshot(_daily_frame(), tmp_path, _metadata())

    manifest, frame = _verify_snapshot(
        Path(published["path"]),
        published["snapshot_id"],
        include_frame=True,
    )

    assert manifest["snapshot_id"] == published["snapshot_id"]
    pd.testing.assert_frame_equal(frame, _daily_frame().astype({"Volume": "float64"}))
