from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from quant_platform.full_persistence import FullPostgresPersistence, PersistenceConflict
from quant_platform.postgres_persistence import PersistenceUnavailableError


class _Rows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, object]]:
        return self._rows

    def fetchone(self) -> dict[str, object] | None:
        return None if not self._rows else self._rows[0]


class _Connection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def execute(self, statement: str, parameters: tuple[object, ...]) -> _Rows:
        assert "FROM qr.residual_artifacts" in statement
        assert len(parameters) == 3
        return _Rows(self._rows)


class _RuntimeFileSetConnection:
    def __init__(self, paths: list[str]) -> None:
        self._paths = paths

    def execute(self, statement: str, parameters: tuple[object, ...]) -> _Rows:
        assert "SELECT relative_path FROM qr.source_files" in statement
        assert parameters == ("platform/datasets/601328.SS/snapshot/%",)
        return _Rows([{"relative_path": path} for path in self._paths])


class _RuntimeFileConnection:
    def __init__(self, row: dict[str, object]) -> None:
        self._row = row

    def execute(self, statement: str, parameters: tuple[object, ...]) -> _Rows:
        assert "FROM qr.source_files" in statement
        assert parameters == ("platform/datasets/601328.SS/parent/manifest.json",)
        return _Rows([self._row])


class _RejectingConnection:
    @contextmanager
    def transaction(self) -> Iterator[_RejectingConnection]:
        yield self

    def execute(self, statement: str, parameters: tuple[object, ...]) -> _Rows:
        if "FROM qr.accepted_evidence_packages" in statement:
            return _Rows([])
        assert "pg_advisory_xact_lock" in statement
        assert len(parameters) == 1
        return _Rows([])


class _RejectingConfig:
    def validated(self) -> _RejectingConfig:
        return self

    @contextmanager
    def connect(self) -> Iterator[_RejectingConnection]:
        yield _RejectingConnection()


class _ExistingAdmissionConnection:
    def execute(self, statement: str, parameters: tuple[object, ...]) -> _Rows:
        assert "FROM qr.accepted_evidence_packages" in statement
        assert len(parameters) == 1
        return _Rows([{"present": 1}])


class _ExistingAdmissionConfig:
    def validated(self) -> _ExistingAdmissionConfig:
        return self

    @contextmanager
    def connect(self) -> Iterator[_ExistingAdmissionConnection]:
        yield _ExistingAdmissionConnection()


class _ValidationBoundaryConnection:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    @contextmanager
    def transaction(self) -> Iterator[_ValidationBoundaryConnection]:
        self._events.append("transaction-enter")
        try:
            yield self
        finally:
            self._events.append("transaction-exit")

    def execute(self, statement: str, parameters: tuple[object, ...]) -> _Rows:
        assert statement == "SELECT qr.lock_bocom_admission_readback()"
        assert parameters == ()
        self._events.append("writer-lock")
        return _Rows([])


class _ValidationBoundaryConfig:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def validated(self) -> _ValidationBoundaryConfig:
        return self

    @contextmanager
    def connect(self) -> Iterator[_ValidationBoundaryConnection]:
        yield _ValidationBoundaryConnection(self._events)


def _identity(payload: bytes, key: str) -> dict[str, object]:
    return {
        "artifact_sha256": hashlib.sha256(payload).hexdigest(),
        "byte_size": len(payload),
        "media_type": "application/octet-stream",
        "residual_class": "DATASET_PARQUET",
        "residual_key": key,
    }


def _write_residual(root: Path, key: str, payload: bytes) -> None:
    target = root / key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


def _tree_state(root: Path) -> list[tuple[str, str, str | None]]:
    return [
        (
            path.relative_to(root).as_posix(),
            "directory" if path.is_dir() else "file",
            None if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
    ]


def test_exact_bocom_repeat_reads_back_without_runtime_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant_platform import bocom_admission

    receipt = {"evidence_id": bocom_admission.ACTION_EVIDENCE_SHA256}
    monkeypatch.setattr(
        bocom_admission,
        "prepare_bocom_admission",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        FullPostgresPersistence,
        "_runtime_residual_with_creation",
        lambda *args, **kwargs: pytest.fail("an exact repeat must not touch residual storage"),
    )
    persistence = FullPostgresPersistence(  # type: ignore[arg-type]
        _ExistingAdmissionConfig(), admit_schema=False
    )
    monkeypatch.setattr(persistence, "bocom_admission", lambda: receipt)

    assert persistence.publish_bocom_admission(
        parent_snapshot_path=tmp_path / "parent",
        snapshot_path=tmp_path / "child",
        package=object(),
    ) == receipt


def test_bocom_readback_holds_writer_lock_across_complete_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    receipt = {"accepted": True}
    persistence = FullPostgresPersistence(  # type: ignore[arg-type]
        _ValidationBoundaryConfig(events), admit_schema=False
    )

    def readback() -> dict[str, bool]:
        assert events == ["transaction-enter", "writer-lock"]
        events.append("readback")
        return receipt

    monkeypatch.setattr(persistence, "_bocom_admission_readback", readback)

    assert persistence.bocom_admission() == receipt
    assert events == ["transaction-enter", "writer-lock", "readback", "transaction-exit"]


def test_bocom_runtime_file_set_accepts_only_the_exact_canonical_paths() -> None:
    prefix = "platform/datasets/601328.SS/snapshot"
    expected = {f"{prefix}/data.parquet", f"{prefix}/manifest.json"}
    FullPostgresPersistence._require_runtime_file_set(
        _RuntimeFileSetConnection(sorted(expected)),
        prefix=prefix,
        expected_paths=expected,
    )

    for paths in (
        [f"{prefix}/data.parquet"],
        sorted(expected | {f"{prefix}/alias/manifest.json"}),
        [f"{prefix}/data.parquet", f"{prefix}/manifest.json", f"{prefix}/manifest.json"],
    ):
        with pytest.raises(PersistenceConflict, match="runtime file path set conflicts"):
            FullPostgresPersistence._require_runtime_file_set(
                _RuntimeFileSetConnection(paths),
                prefix=prefix,
                expected_paths=expected,
            )


def test_bocom_parent_metadata_accepts_only_explicit_imported_classification() -> None:
    payload = b"production-shaped imported parent metadata\n"
    digest = hashlib.sha256(payload).hexdigest()
    row: dict[str, object] = {
        "file_class": "DATASET_METADATA",
        "mode": 0o444,
        "byte_size": len(payload),
        "sha256": digest,
        "artifact_sha256": digest,
        "residual_sha256": None,
        "classification": "BYTEA_IMPORTED",
    }
    connection = _RuntimeFileConnection(row)

    FullPostgresPersistence._require_runtime_file(
        connection,
        relative_path="platform/datasets/601328.SS/parent/manifest.json",
        file_class="DATASET_METADATA",
        payload=payload,
        classification="BYTEA_IMPORTED",
    )
    row["classification"] = "BYTEA_RUNTIME"
    with pytest.raises(PersistenceConflict, match="runtime file identity conflicts"):
        FullPostgresPersistence._require_runtime_file(
            connection,
            relative_path="platform/datasets/601328.SS/parent/manifest.json",
            file_class="DATASET_METADATA",
            payload=payload,
            classification="BYTEA_IMPORTED",
        )


@pytest.mark.parametrize("legacy", [False, True])
def test_bocom_residual_accepts_only_exact_current_or_legacy_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    payload = b"verified parent parquet bytes"
    digest = hashlib.sha256(payload).hexdigest()
    key = f"{digest[:2]}/{digest}" if legacy else f"runtime/{digest[:2]}/{digest}"
    _write_residual(tmp_path, key, payload)
    monkeypatch.setenv("QUANT_RESIDUAL_ROOT", str(tmp_path))

    FullPostgresPersistence._require_bocom_dataset_residual(
        _Connection([_identity(payload, key)]), payload=payload
    )


@pytest.mark.parametrize(
    "mutation",
    ["digest", "prefix", "media_type", "class", "size", "bytes", "duplicate_alias"],
)
def test_bocom_residual_rejects_wrong_or_ambiguous_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    payload = b"verified parent parquet bytes"
    digest = hashlib.sha256(payload).hexdigest()
    current = f"runtime/{digest[:2]}/{digest}"
    legacy = f"{digest[:2]}/{digest}"
    row = _identity(payload, current)
    rows = [row]
    stored_payload = payload

    if mutation == "digest":
        row["artifact_sha256"] = "0" * 64
    elif mutation == "prefix":
        row["residual_key"] = f"other/{digest[:2]}/{digest}"
    elif mutation == "media_type":
        row["media_type"] = "application/x-parquet"
    elif mutation == "class":
        row["residual_class"] = "SOURCE_TREE"
    elif mutation == "size":
        row["byte_size"] = len(payload) + 1
    elif mutation == "bytes":
        stored_payload = b"different bytes"
    else:
        rows.append(_identity(b"different artifact", legacy))
        _write_residual(tmp_path, legacy, b"different artifact")

    _write_residual(tmp_path, str(row["residual_key"]), stored_payload)
    monkeypatch.setenv("QUANT_RESIDUAL_ROOT", str(tmp_path))

    with pytest.raises(PersistenceConflict, match="residual registry conflicts"):
        FullPostgresPersistence._require_bocom_dataset_residual(
            _Connection(rows), payload=payload
        )


def test_rejected_first_publication_removes_only_its_new_runtime_residual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant_platform import bocom_admission, datasets

    payload = b"production-shaped parent parquet bytes"
    digest = hashlib.sha256(payload).hexdigest()
    legacy_key = f"{digest[:2]}/{digest}"
    residual_root = tmp_path / "residual"
    parent = tmp_path / "parent"
    child = tmp_path / "child"
    for snapshot in (parent, child):
        snapshot.mkdir()
        (snapshot / "manifest.json").write_bytes(b"{}\n")
        (snapshot / "data.parquet").write_bytes(payload)
    _write_residual(residual_root, legacy_key, payload)
    _write_residual(residual_root, "unrelated/preserve", b"preserve-me")
    monkeypatch.setenv("QUANT_RESIDUAL_ROOT", str(residual_root))
    monkeypatch.setattr(datasets, "_verify_snapshot", lambda *args, **kwargs: {"verified": True})
    monkeypatch.setattr(
        bocom_admission,
        "prepare_bocom_admission",
        lambda *args, **kwargs: SimpleNamespace(
            snapshot_manifest={"verified": True},
            snapshot_path=child,
        ),
    )

    def reject_registry(*args: Any, **kwargs: Any) -> None:
        raise PersistenceConflict("BOCOM dataset residual registry conflicts")

    monkeypatch.setattr(FullPostgresPersistence, "_require_bocom_snapshot_row", reject_registry)
    before = _tree_state(residual_root)

    persistence = FullPostgresPersistence(  # type: ignore[arg-type]
        _RejectingConfig(), admit_schema=False
    )
    with pytest.raises(PersistenceConflict, match="residual registry conflicts"):
        persistence.publish_bocom_admission(
            parent_snapshot_path=parent,
            snapshot_path=child,
            package=object(),
        )

    assert _tree_state(residual_root) == before


def test_cleanup_restores_a_replacement_instead_of_deleting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    residual_root = tmp_path / "residual"
    residual_root.mkdir()
    monkeypatch.setenv("QUANT_RESIDUAL_ROOT", str(residual_root))
    _, _, creation = FullPostgresPersistence._runtime_residual_with_creation(
        b"invocation-created"
    )
    assert creation is not None
    saved = creation.path.with_name("saved-invocation-inode")
    replacement = b"pre-existing-or-concurrent-replacement"
    original_rename = Path.rename
    replacement_installed = False

    def replace_before_capture(source: Path, target: Path) -> Path:
        nonlocal replacement_installed
        if source == creation.path and not replacement_installed:
            original_rename(source, saved)
            source.write_bytes(replacement)
            replacement_installed = True
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", replace_before_capture)

    with pytest.raises(PersistenceUnavailableError, match="changed before cleanup"):
        FullPostgresPersistence._discard_runtime_residual_creation(creation)

    assert replacement_installed
    assert creation.path.read_bytes() == replacement
    assert saved.exists()


@pytest.mark.parametrize("level", ["runtime", "shard"])
def test_concurrently_created_residual_directories_are_not_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    residual_root = tmp_path / "residual"
    residual_root.mkdir()
    monkeypatch.setenv("QUANT_RESIDUAL_ROOT", str(residual_root))
    original_mkdir = Path.mkdir
    foreign_directories: list[Path] = []

    def concurrent_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        is_race_target = (level == "runtime" and path.name == "runtime") or (
            level == "shard" and path.parent.name == "runtime"
        )
        if not foreign_directories and is_race_target:
            original_mkdir(path, mode=0o700)
            foreign_directories.append(path)
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", concurrent_mkdir)
    _, _, creation = FullPostgresPersistence._runtime_residual_with_creation(b"directory-race")
    assert creation is not None
    assert all(path not in creation.directories for path in foreign_directories)

    FullPostgresPersistence._discard_runtime_residual_creation(creation)

    assert all(path.exists() for path in foreign_directories)
