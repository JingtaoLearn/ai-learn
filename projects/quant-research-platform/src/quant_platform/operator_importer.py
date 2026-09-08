from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

from .operator_service import _normalize_submission
from .postgres_persistence import ArtifactInput, PostgresOperatorPersistence
from .schemas import canonical_json_bytes


SOURCE_MANIFEST_SCHEMA = "quantresearch-operator-source-digests/v1"
RECEIPT_SCHEMA = "quantresearch-operator-import-parity/v1"
BUNDLE_MEDIA_TYPES = {
    "documentation.md": "text/markdown; charset=utf-8",
    "evidence.json": "application/json",
    "manifest.json": "application/json",
    "operator.py": "text/x-python; charset=utf-8",
    "tests.json": "application/json",
}
CUSTOM_BUNDLE_MEMBERS = set(BUNDLE_MEDIA_TYPES)


class OperatorImportError(ValueError):
    """Raised when frozen SQLite Operator evidence cannot be imported exactly."""


def _regular_file(path: Path, label: str) -> None:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise OperatorImportError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OperatorImportError(f"{label} must be a regular file")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_connection(database: Path) -> sqlite3.Connection:
    _regular_file(database, "frozen catalog SQLite")
    connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _source_rows(source_root: Path) -> list[dict[str, Any]]:
    database = source_root / "catalog.sqlite3"
    with _source_connection(database) as connection:
        rows = connection.execute(
            """
            SELECT o.operator_id, o.slot, o.title_zh, o.summary_zh,
                   o.created_at AS operator_created_at,
                   v.version, v.content_digest, v.parameter_schema_json,
                   v.defaults_json, v.documentation, v.bundle_path,
                   v.validation_evidence_json, v.status, v.created_at
            FROM operators AS o
            JOIN operator_versions AS v USING (operator_id)
            ORDER BY o.operator_id, v.version
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _safe_bundle(source_root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise OperatorImportError("Operator bundle path is invalid")
    bundle = source_root / relative
    try:
        bundle.relative_to(source_root)
    except ValueError as exc:
        raise OperatorImportError("Operator bundle escapes the frozen source") from exc
    if bundle.is_symlink() or not bundle.is_dir():
        raise OperatorImportError("Operator bundle must be a directory")
    members = list(bundle.iterdir())
    if not members:
        raise OperatorImportError("Operator bundle member set is empty")
    for path in members:
        _regular_file(path, f"Operator bundle member {path.name}")
    return bundle


def build_source_manifest(source_root: Path) -> dict[str, Any]:
    source_root = source_root.absolute()
    database = source_root / "catalog.sqlite3"
    rows = _source_rows(source_root)
    members: list[dict[str, Any]] = []
    for row in rows:
        bundle = _safe_bundle(source_root, row["bundle_path"])
        for path in sorted(bundle.iterdir()):
            relative = path.relative_to(source_root).as_posix()
            payload = path.read_bytes()
            members.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "byte_size": len(payload),
                }
            )
    return {
        "schema": SOURCE_MANIFEST_SCHEMA,
        "catalog_sqlite_sha256": _sha256(database),
        "operator_version_count": len(rows),
        "members": sorted(members, key=lambda item: item["path"]),
    }


def _write_create_only(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(canonical_json_bytes(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _load_manifest(path: Path) -> dict[str, Any]:
    _regular_file(path, "source digest manifest")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OperatorImportError("source digest manifest is unreadable") from exc
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "catalog_sqlite_sha256",
        "operator_version_count",
        "members",
    }:
        raise OperatorImportError("source digest manifest fields are invalid")
    if value["schema"] != SOURCE_MANIFEST_SCHEMA:
        raise OperatorImportError("source digest manifest schema is unsupported")
    return value


def _verify_source_manifest(source_root: Path, expected: dict[str, Any]) -> None:
    actual = build_source_manifest(source_root)
    if actual != expected:
        raise OperatorImportError("frozen Operator source digest manifest mismatch")


def _import_record(row: dict[str, Any], source_root: Path) -> tuple[dict[str, Any], list[ArtifactInput]]:
    if row["status"] != "PUBLISHED":
        raise OperatorImportError("non-published Operator versions require a later migration mapping")
    bundle = _safe_bundle(source_root, row["bundle_path"])
    try:
        parameter_schema = json.loads(row["parameter_schema_json"])
        defaults = json.loads(row["defaults_json"])
        database_evidence = json.loads(row["validation_evidence_json"])
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OperatorImportError("Operator source record is unreadable") from exc
    names = {path.name for path in bundle.iterdir()}
    digest = row["content_digest"]
    if names == CUSTOM_BUNDLE_MEMBERS:
        try:
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            tests = json.loads((bundle / "tests.json").read_text(encoding="utf-8"))
            evidence = json.loads((bundle / "evidence.json").read_text(encoding="utf-8"))
            source = (bundle / "operator.py").read_text(encoding="utf-8")
            documentation = (bundle / "documentation.md").read_text(encoding="utf-8")
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise OperatorImportError("custom Operator bundle is unreadable") from exc
        submission = _normalize_submission(
            {
                "operator_id": row["operator_id"],
                "slot": row["slot"],
                "version": row["version"],
                "source": source,
                "parameter_schema": parameter_schema,
                "defaults": defaults,
                "title_zh": row["title_zh"],
                "summary_zh": row["summary_zh"],
                "documentation": documentation,
                "tests": tests,
            }
        )
        digest = hashlib.sha256(canonical_json_bytes(submission)).hexdigest()
        expected_manifest = {
            key: submission[key]
            for key in (
                "operator_id",
                "slot",
                "version",
                "parameter_schema",
                "defaults",
                "title_zh",
                "summary_zh",
                "documentation",
            )
        } | {"content_digest": digest}
        if (
            digest != row["content_digest"]
            or manifest != expected_manifest
            or documentation != row["documentation"]
            or evidence != database_evidence
        ):
            raise OperatorImportError("custom Operator row and bundle identity mismatch")
    elif "manifest.json" in names:
        try:
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise OperatorImportError("Operator manifest is unreadable") from exc
        if manifest.get("content_digest") != digest:
            raise OperatorImportError("Operator manifest content digest mismatch")
    artifacts = [
        ArtifactInput(
            path.name,
            BUNDLE_MEDIA_TYPES.get(
                path.name,
                "application/json" if path.suffix == ".json" else "application/octet-stream",
            ),
            path.read_bytes(),
        )
        for path in sorted(bundle.iterdir())
    ]
    projected = {
        "operator_id": row["operator_id"],
        "slot": row["slot"],
        "version": row["version"],
        "content_digest": digest,
        "parameter_schema": parameter_schema,
        "defaults": defaults,
        "title_zh": row["title_zh"],
        "summary_zh": row["summary_zh"],
        "documentation": row["documentation"],
        "validation_evidence": database_evidence,
        "status": "PUBLISHED",
        "operator_created_at": row["operator_created_at"],
        "created_at": row["created_at"],
    }
    return projected, artifacts


def _project_target(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        key: detail[key]
        for key in (
            "operator_id",
            "slot",
            "version",
            "content_digest",
            "parameter_schema",
            "defaults",
            "title_zh",
            "summary_zh",
            "documentation",
            "validation_evidence",
            "status",
            "operator_created_at",
            "created_at",
        )
    }


def import_operators(
    source_root: Path,
    manifest_path: Path,
    persistence: PostgresOperatorPersistence,
) -> dict[str, Any]:
    source_root = source_root.absolute()
    expected_manifest = _load_manifest(manifest_path)
    _verify_source_manifest(source_root, expected_manifest)
    if not persistence.is_empty():
        raise OperatorImportError("Operator import target must be empty")
    rows = _source_rows(source_root)
    source_projection: list[dict[str, Any]] = []
    member_projection: list[dict[str, Any]] = []
    for row in rows:
        projected, artifacts = _import_record(row, source_root)
        source_projection.append(projected)
        for artifact in artifacts:
            member_projection.append(
                {
                    "operator_id": row["operator_id"],
                    "version": row["version"],
                    "logical_name": artifact.logical_name,
                    "sha256": hashlib.sha256(artifact.payload).hexdigest(),
                    "byte_size": len(artifact.payload),
                }
            )
        persistence.publish_operator(
            operator_id=projected["operator_id"],
            slot=projected["slot"],
            version=projected["version"],
            title_zh=projected["title_zh"],
            summary_zh=projected["summary_zh"],
            content_digest=projected["content_digest"],
            parameter_schema=projected["parameter_schema"],
            defaults=projected["defaults"],
            documentation=projected["documentation"],
            validation_evidence=projected["validation_evidence"],
            artifacts=artifacts,
            created_at=row["created_at"],
            operator_created_at=row["operator_created_at"],
        )
    target_projection = [
        _project_target(persistence.operator_detail(item["operator_id"], item["version"]))
        for item in source_projection
    ]
    target_members: list[dict[str, Any]] = []
    for item in source_projection:
        detail = persistence.operator_detail(item["operator_id"], item["version"])
        for artifact in detail["artifacts"]:
            payload, _, digest = persistence.read_artifact(
                item["operator_id"], item["version"], artifact["logical_name"]
            )
            target_members.append(
                {
                    "operator_id": item["operator_id"],
                    "version": item["version"],
                    "logical_name": artifact["logical_name"],
                    "sha256": digest,
                    "byte_size": len(payload),
                }
            )
    source_digest = hashlib.sha256(canonical_json_bytes(source_projection)).hexdigest()
    target_digest = hashlib.sha256(canonical_json_bytes(target_projection)).hexdigest()
    source_member_digest = hashlib.sha256(canonical_json_bytes(member_projection)).hexdigest()
    target_member_digest = hashlib.sha256(canonical_json_bytes(target_members)).hexdigest()
    if source_digest != target_digest or source_member_digest != target_member_digest:
        raise OperatorImportError("Operator import read-back parity mismatch")
    return {
        "schema": RECEIPT_SCHEMA,
        "status": "PASS",
        "source_catalog_sha256": expected_manifest["catalog_sqlite_sha256"],
        "operator_version_count": len(source_projection),
        "artifact_member_count": len(member_projection),
        "ordered_operator_digest": source_digest,
        "ordered_artifact_member_digest": source_member_digest,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline create-only Operator PostgreSQL importer")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inventory = subparsers.add_parser("inventory")
    inventory.add_argument("--source-root", type=Path, required=True)
    inventory.add_argument("--output", type=Path, required=True)
    run = subparsers.add_parser("import")
    run.add_argument("--source-root", type=Path, required=True)
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "inventory":
        _write_create_only(args.output, build_source_manifest(args.source_root))
    else:
        receipt = import_operators(
            args.source_root,
            args.manifest,
            PostgresOperatorPersistence.from_environment(),
        )
        _write_create_only(args.receipt, receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
