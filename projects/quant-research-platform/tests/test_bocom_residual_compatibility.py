from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from quant_platform.full_persistence import FullPostgresPersistence, PersistenceConflict


class _Rows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, object]]:
        return self._rows


class _Connection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def execute(self, statement: str, parameters: tuple[object, ...]) -> _Rows:
        assert "FROM qr.residual_artifacts" in statement
        assert len(parameters) == 3
        return _Rows(self._rows)


class _RejectingConnection:
    @contextmanager
    def transaction(self) -> Iterator[_RejectingConnection]:
        yield self

    def execute(self, statement: str, parameters: tuple[object, ...]) -> None:
        assert "pg_advisory_xact_lock" in statement
        assert len(parameters) == 1


class _RejectingConfig:
    def validated(self) -> _RejectingConfig:
        return self

    @contextmanager
    def connect(self) -> Iterator[_RejectingConnection]:
        yield _RejectingConnection()


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
