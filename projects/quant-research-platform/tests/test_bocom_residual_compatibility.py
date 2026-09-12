from __future__ import annotations

import hashlib
from pathlib import Path

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
