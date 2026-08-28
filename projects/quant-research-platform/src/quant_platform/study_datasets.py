from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from .datasets import (
    PROJECTION_IDENTITY as _PROJECTION_IDENTITY,
    SAFE_INSTRUMENT,
    _InstrumentLock,
    _canonical_data_bytes,
    _canonical_json,
    _fsync_directory,
    _make_snapshot_removable,
    _parent_verification_attestation,
    _rename_noreplace,
    _scoring_mask,
    _seal_snapshot,
    _sha256,
    _verify_snapshot_seal,
    _verify_snapshot,
    _write_new,
)
from .study_contracts import normalize_fold_window
from .updates import snapshot_update_lineage


__all__ = [
    "ExecutionDatasetSliceError",
    "ExecutionDatasetSliceFactory",
]


class ExecutionDatasetSliceError(RuntimeError):
    """Raised when an Execution Dataset Slice cannot be materialized safely."""


class ExecutionDatasetSliceFactory:
    """Materialize immutable physical dataset prefixes for one Fold Window."""

    def __init__(self, state_root: Path | str):
        self.state_root = Path(state_root).absolute()

    def _validate_state_root(self) -> None:
        for component in (self.state_root, *self.state_root.parents):
            if not component.exists() and not component.is_symlink():
                continue
            metadata = os.stat(component, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise ExecutionDatasetSliceError(
                    f"Execution Dataset Slice state root contains a symlink: {component}"
                )

    def materialize(
        self,
        parent_snapshot: Mapping[str, Any],
        fold_window: Mapping[str, Any],
    ) -> dict[str, str]:
        if not isinstance(parent_snapshot, Mapping):
            raise ExecutionDatasetSliceError("parent_snapshot must be an object")
        instrument = parent_snapshot.get("instrument")
        snapshot_id = parent_snapshot.get("snapshot_id")
        if (
            not isinstance(instrument, str)
            or SAFE_INSTRUMENT.fullmatch(instrument) is None
            or not isinstance(snapshot_id, str)
        ):
            raise ExecutionDatasetSliceError("parent_snapshot identity is invalid")

        root = self.state_root
        self._validate_state_root()
        parent_path = root / "datasets" / instrument / snapshot_id
        instrument_root = parent_path.parent
        try:
            with _InstrumentLock(root, instrument):
                verified = _verify_snapshot(
                    parent_path,
                    snapshot_id,
                    include_frame=True,
                    verify_parent=True,
                )
                if not isinstance(verified, tuple):
                    raise ExecutionDatasetSliceError(
                        "parent snapshot verifier did not return market data"
                    )
                parent_manifest, parent_frame = verified
                parent_lineage = (
                    parent_manifest["lineage"]
                    if parent_manifest["schema_version"] == 3
                    else snapshot_update_lineage(root, instrument, snapshot_id)
                )
                supplied_digest = parent_snapshot.get("canonical_sha256")
                if (
                    supplied_digest is not None
                    and supplied_digest != parent_manifest["canonical_sha256"]
                ):
                    raise ExecutionDatasetSliceError(
                        "parent_snapshot canonical digest does not match"
                    )
                supplied_lineage = parent_snapshot.get("lineage")
                if supplied_lineage is not None and supplied_lineage != parent_lineage:
                    raise ExecutionDatasetSliceError("parent_snapshot lineage does not match")

                dates = parent_frame["Date"].dt.strftime("%Y-%m-%d").tolist()
                normalized_window = normalize_fold_window(fold_window, dates)
                projected = parent_frame.loc[
                    (parent_frame["Date"] >= pd.Timestamp(normalized_window["allowed_start"]))
                    & (parent_frame["Date"] <= pd.Timestamp(normalized_window["available_through"]))
                ].reset_index(drop=True)
                mask = _scoring_mask(projected, normalized_window)
                mask_payload = _canonical_json(mask) + b"\n"

                temporary = Path(
                    tempfile.mkdtemp(prefix=".execution-dataset-slice.", dir=instrument_root)
                )
                try:
                    parquet_path = temporary / "data.parquet"
                    projected.to_parquet(parquet_path, index=False)
                    with parquet_path.open("rb") as stream:
                        os.fsync(stream.fileno())
                    parquet_payload = parquet_path.read_bytes()
                    _write_new(temporary / "scoring_mask.json", mask_payload)
                    parent_identity = {
                        "instrument": instrument,
                        "snapshot_id": snapshot_id,
                        "canonical_sha256": parent_manifest["canonical_sha256"],
                        "lineage": parent_lineage,
                    }
                    parent_verification = _parent_verification_attestation(
                        parent_identity,
                        parent_manifest,
                    )
                    access_boundary = {
                        "schema_version": 1,
                        "parent": parent_identity,
                        "parent_verification": parent_verification,
                        "view_spec": normalized_window,
                        "projection_identity": _PROJECTION_IDENTITY,
                        "projected_bytes_sha256": _sha256(parquet_payload),
                        "scoring_mask_sha256": _sha256(mask_payload),
                    }
                    lineage = {
                        "kind": "derived_view",
                        "parent": parent_identity,
                        "parent_verification": parent_verification,
                        "view_spec": normalized_window,
                        "readable_range": {
                            "start": normalized_window["allowed_start"],
                            "end": normalized_window["available_through"],
                        },
                        "scoring_mask": {
                            "path": "scoring_mask.json",
                            "sha256": _sha256(mask_payload),
                            "rows": len(projected),
                            "scored_rows": sum(row["scored"] for row in mask["rows"]),
                        },
                        "projection_identity": deepcopy(_PROJECTION_IDENTITY),
                        "projected_bytes_sha256": _sha256(parquet_payload),
                        "access_boundary_digest": _sha256(_canonical_json(access_boundary)),
                    }
                    canonical_sha256 = _sha256(_canonical_data_bytes(projected))
                    identity = {
                        "schema_version": 3,
                        "metadata": parent_manifest["metadata"],
                        "canonical_sha256": canonical_sha256,
                        "lineage": lineage,
                    }
                    derived_snapshot_id = _sha256(_canonical_json(identity))
                    manifest = identity | {
                        "snapshot_id": derived_snapshot_id,
                        "rows": len(projected),
                        "data_start": str(projected["Date"].min().date()),
                        "data_end": str(projected["Date"].max().date()),
                        "parquet_sha256": _sha256(parquet_payload),
                        "scoring_mask_sha256": _sha256(mask_payload),
                        "columns": list(projected.columns),
                        "files": {
                            "data": "data.parquet",
                            "scoring_mask": "scoring_mask.json",
                        },
                    }
                    _write_new(
                        temporary / "manifest.json",
                        json.dumps(
                            manifest,
                            indent=2,
                            sort_keys=True,
                            ensure_ascii=False,
                            allow_nan=False,
                        ).encode("utf-8")
                        + b"\n",
                    )
                    _seal_snapshot(
                        temporary,
                        frozenset(
                            {"manifest.json", "data.parquet", "scoring_mask.json"}
                        ),
                    )
                    _verify_snapshot_seal(
                        temporary,
                        frozenset(
                            {"manifest.json", "data.parquet", "scoring_mask.json"}
                        ),
                    )
                    _fsync_directory(temporary)
                    _verify_snapshot(
                        temporary,
                        derived_snapshot_id,
                        require_name=False,
                        verify_parent=True,
                    )
                    target = instrument_root / derived_snapshot_id
                    if target.exists():
                        status = "NO_CHANGE"
                    else:
                        try:
                            _rename_noreplace(temporary, target)
                        except FileExistsError:
                            status = "NO_CHANGE"
                        else:
                            _fsync_directory(instrument_root)
                            status = "CREATED"
                    _verify_snapshot(
                        target,
                        derived_snapshot_id,
                        verify_parent=True,
                    )
                finally:
                    if temporary.exists():
                        _make_snapshot_removable(temporary)
                        shutil.rmtree(temporary)
            _verify_snapshot(
                target,
                derived_snapshot_id,
                verify_parent=True,
            )
        except ExecutionDatasetSliceError:
            raise
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ExecutionDatasetSliceError(
                f"cannot materialize Execution Dataset Slice: {exc}"
            ) from exc

        return {
            "status": status,
            "snapshot_id": derived_snapshot_id,
            "path": str(target),
        }
