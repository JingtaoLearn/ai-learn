from __future__ import annotations

import copy
import csv
import fcntl
import hashlib
import html
import io
import json
import math
import os
import re
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .schemas import canonical_json_bytes


class AttemptReportError(RuntimeError):
    """Raised when canonical report evidence cannot be proved or published."""


class _CapturedAuthorityAttachment(dict[str, Any]):
    """Process-local proof that the broker observed issuer-side authority."""


SHA256 = re.compile(r"^[0-9a-f]{64}$")
REPORT_OPERATOR_ID = "canonical_attempt_report"
REPORT_OPERATOR_VERSION = "1.0.0"
REPORT_OPERATOR_API_VERSION = 2
REPORT_DOCUMENT_SCHEMA_ID = "quant-platform/report-document/v1"
REPORT_MANIFEST_SCHEMA_ID = "quant-platform/report-manifest/v1"
LATEST_POINTER_SCHEMA_ID = "quant-platform/attempt-report-latest-pointer/v1"
AUTHORITY_ATTACHMENT_SCHEMA_ID = "quant-platform/report-authority-attachment/v1"
DOMAIN_BUNDLE = b"quant-platform/attempt-result-bundle/v1\0"
DOMAIN_ATTACHMENT = b"quant-platform/report-authority-attachment/v1\0"
DOMAIN_DOCUMENT = b"quant-platform/report-document/v1\0"
DOMAIN_ARTIFACT = b"quant-platform/attempt-report-artifact/v1\0"
DOMAIN_POINTER = b"quant-platform/attempt-report-latest-pointer/v1\0"
DOMAIN_OPERATOR_CONTENT = b"quant-platform/operator-content/v2\0"
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_REPORT_BYTES = 1_000_000
MATCHED_EXPOSURE_SOURCE_SHA256 = (
    "8cc91f7926bffa99bb07ac14cc30222e9151344da02a310e99d750f0c91d3896"
)
CORE_RESULT_FILES = (
    "daily_replay.csv",
    "events.csv",
    "trades.csv",
    "metrics.json",
    "cost_breakdown.json",
)
BASE_RUN_FILES = frozenset(
    {
        "config.json",
        "run_manifest.json",
        *CORE_RESULT_FILES,
        "report.html",
    }
)
SETTLEMENT_FILES = frozenset({"account_events.csv", "account_trades.csv"})
REPORT_ARTIFACT_FILES = frozenset(
    {"report-document.json", "report-manifest.json", "report.html"}
)
REPORT_CONTENT_FILES = (
    "defaults.json",
    "documentation.md",
    "operator.py",
    "parameter-schema.json",
    "tests.json",
)
REPORT_BUNDLE_FILES = frozenset(
    {*REPORT_CONTENT_FILES, "evidence.json", "manifest.json"}
)
AUTHORITY_TOPOLOGY = {
    "TOTAL_RETURN_FULL": (
        ("ancestry.json", "/ancestry"),
        ("binding.json", "$binding"),
        ("record.json", "/record"),
    ),
    "TOTAL_RETURN_READ_TIME": (
        ("binding.json", "$binding"),
        ("projection.json", "/projection"),
    ),
    "MATCHED_EXPOSURE_TERMINAL": (
        ("ancestry.json", "/ancestry"),
        ("binding.json", "$binding"),
        ("family-record.json", "/family_record"),
        ("study-event.json", "/study_event"),
        ("terminal-record.json", "/terminal_record"),
    ),
    "MATCHED_EXPOSURE_ADMISSION_REJECTED": (
        ("ancestry.json", "/ancestry"),
        ("study-event.json", "/study_event"),
        ("terminal-record.json", "/terminal_record"),
    ),
    "STUDY_TERMINAL_NO_QUALIFIED": (
        ("projection.json", "/projection"),
        ("qualification-attachment-ids.json", "/qualification_attachment_ids"),
        ("study-events.json", "/study_events"),
    ),
}
ATTACHMENT_KEYS = {
    "TOTAL_RETURN_FULL": {
        "kind",
        "attachment_id",
        "attempt_id",
        "experiment_id",
        "bundle_id",
        "result_digest",
        "metric_document_digest",
        "files",
        "record",
        "ancestry",
    },
    "TOTAL_RETURN_READ_TIME": {
        "kind",
        "attachment_id",
        "attempt_id",
        "experiment_id",
        "bundle_id",
        "result_digest",
        "metric_document_digest",
        "files",
        "projection",
    },
    "MATCHED_EXPOSURE_TERMINAL": {
        "kind",
        "attachment_id",
        "attempt_id",
        "experiment_id",
        "bundle_id",
        "result_digest",
        "study_id",
        "candidate_digest",
        "metric_document_digest",
        "terminal_record",
        "ancestry",
        "family_record",
        "study_event",
        "files",
    },
    "MATCHED_EXPOSURE_ADMISSION_REJECTED": {
        "kind",
        "attachment_id",
        "study_id",
        "candidate_digest",
        "terminal_record",
        "ancestry",
        "study_event",
        "files",
    },
    "STUDY_TERMINAL_NO_QUALIFIED": {
        "kind",
        "attachment_id",
        "study_id",
        "qualification_attachment_ids",
        "study_events",
        "projection",
        "files",
    },
}
BINDING_EXCLUDED_KEYS = {
    "attachment_id",
    "files",
    "record",
    "projection",
    "ancestry",
    "terminal_record",
    "family_record",
    "study_event",
    "study_events",
    "qualification_attachment_ids",
}
REPORT_SECTION_FIELDS = (
    ("identity_and_purpose", ("attempt_id", "experiment_id", "run_id", "dataset_snapshot_id", "purpose")),
    ("evidence_status", ("bundle_integrity", "total_return_status", "matched_exposure_status", "ranking_status", "promotion_ready")),
    ("account_summary", ("period_start", "period_end", "initial_capital_cny", "final_equity_cny", "net_profit_cny", "current_position", "closed_trades", "open_trades")),
    ("configuration", ("template_parameters", "operators", "runtime")),
    ("price_equity_path", ("price_equity_rows",)),
    ("events_trades_holdings", ("events", "trades", "holdings")),
    ("costs_and_accounting", ("commission_cny", "transfer_fee_cny", "stamp_tax_cny", "slippage_cny", "total_cost_cny", "gross_dividends_cny", "dividend_tax_cny", "outstanding_tax_cny")),
    ("total_return_claim", ("net_return", "max_drawdown", "total_return_attachment")),
    ("matched_exposure_qualification", ("matched_exposure_attachment", "study_terminal_attachment")),
    ("limitations", ("integrity_not_qualification", "qualification_not_deployment", "no_recomputation")),
    ("provenance", ("bundle_id", "core_result_digest", "operator_id", "operator_version", "operator_source_sha256", "operator_content_digest")),
)
SECTION_LABELS = {
    "identity_and_purpose": "Identity and purpose",
    "evidence_status": "Evidence status",
    "account_summary": "Account summary",
    "configuration": "Configuration",
    "price_equity_path": "Price and equity path",
    "events_trades_holdings": "Events, trades, and holdings",
    "costs_and_accounting": "Costs and accounting",
    "total_return_claim": "Total-return claim",
    "matched_exposure_qualification": "Matched-exposure qualification",
    "limitations": "Limitations",
    "provenance": "Provenance",
}
FIELD_SOURCE_AND_UNIT = {
    "attempt_id": ("attempt-audit", "/attempt_id", "IDENTITY"),
    "experiment_id": ("attempt-audit", "/experiment_id", "IDENTITY"),
    "run_id": ("attempt-audit", "/run_id", "IDENTITY"),
    "dataset_snapshot_id": ("attempt-audit", "/dataset/snapshot_id", "IDENTITY"),
    "purpose": ("contract", "/purpose", "TOKEN"),
    "bundle_integrity": ("bundle-descriptor", "/verification/status", "TOKEN"),
    "total_return_status": ("total-return-attachment", "/", "TOKEN"),
    "matched_exposure_status": ("matched-exposure-attachment", "/terminal_record/state", "TOKEN"),
    "ranking_status": ("matched-exposure-attachment", "/terminal_record/ranking_status", "TOKEN"),
    "promotion_ready": ("total-return-attachment", "/", "BOOLEAN"),
    "period_start": ("bundle/metrics.json", "/period_start", "DATE"),
    "period_end": ("bundle/metrics.json", "/period_end", "DATE"),
    "initial_capital_cny": ("bundle/metrics.json", "/initial_capital_cny", "CNY"),
    "final_equity_cny": ("bundle/metrics.json", "/final_equity_cny", "CNY"),
    "net_profit_cny": ("bundle/metrics.json", "/net_profit_cny", "CNY"),
    "current_position": ("bundle/metrics.json", "/current_position", "TOKEN"),
    "closed_trades": ("bundle/metrics.json", "/closed_trades", "COUNT"),
    "open_trades": ("bundle/metrics.json", "/open_trades", "COUNT"),
    "template_parameters": ("bundle/config.json", "/template/parameters", "QR-CJSON-1"),
    "operators": ("attempt-audit", "/operators", "QR-CJSON-1"),
    "runtime": ("bundle/run_manifest.json", "/runtime", "QR-CJSON-1"),
    "price_equity_rows": ("bundle/daily_replay.csv", "/rows/*/{Date,price,close,equity}", "TABLE"),
    "events": ("bundle/events.csv", "/rows", "TABLE"),
    "trades": ("bundle/trades.csv", "/rows", "TABLE"),
    "holdings": ("bundle/daily_replay.csv", "/rows/*/{Date,holdings,position_after}", "TABLE"),
    "commission_cny": ("bundle/cost_breakdown.json", "/commission_cny", "CNY"),
    "transfer_fee_cny": ("bundle/cost_breakdown.json", "/transfer_fee_cny", "CNY"),
    "stamp_tax_cny": ("bundle/cost_breakdown.json", "/stamp_tax_cny", "CNY"),
    "slippage_cny": ("bundle/cost_breakdown.json", "/slippage_cny", "CNY"),
    "total_cost_cny": ("bundle/cost_breakdown.json", "/total_cost_cny", "CNY"),
    "gross_dividends_cny": ("bundle/metrics.json", "/gross_dividends_cny", "CNY"),
    "dividend_tax_cny": ("bundle/metrics.json", "/dividend_tax_cny", "CNY"),
    "outstanding_tax_cny": ("bundle/metrics.json", "/outstanding_tax_cny", "CNY"),
    "net_return": ("bundle/metrics.json", "/net_return", "FRACTION"),
    "max_drawdown": ("bundle/metrics.json", "/max_drawdown", "FRACTION"),
    "total_return_attachment": ("total-return-attachment", "/", "QR-CJSON-1"),
    "matched_exposure_attachment": ("matched-exposure-attachment", "/", "QR-CJSON-1"),
    "study_terminal_attachment": ("study-terminal-attachment", "/", "QR-CJSON-1"),
    "integrity_not_qualification": ("contract", "/limitations/0", "TOKEN"),
    "qualification_not_deployment": ("contract", "/limitations/1", "TOKEN"),
    "no_recomputation": ("contract", "/limitations/2", "TOKEN"),
    "bundle_id": ("bundle-descriptor", "/bundle_id", "IDENTITY"),
    "core_result_digest": ("attempt-audit", "/result_digest", "IDENTITY"),
    "operator_id": ("operator-manifest", "/operator_id", "IDENTITY"),
    "operator_version": ("operator-manifest", "/semantic_version", "SEMVER"),
    "operator_source_sha256": ("operator-manifest", "/source/sha256", "IDENTITY"),
    "operator_content_digest": ("operator-manifest", "/content_digest", "IDENTITY"),
}

CANONICAL_REPORT_OPERATOR_SOURCE = """OPERATOR_API_VERSION = 2
SLOT = \"report\"

def _escape(value):
    text = str(value)
    return (text.replace(\"&\", \"&amp;\").replace(\"<\", \"&lt;\")
                .replace(\">\", \"&gt;\").replace(chr(34), \"&quot;\")
                .replace(chr(39), \"&#x27;\"))

def apply(payload, parameters):
    if parameters != {}:
        raise ValueError(\"canonical report parameters must be empty\")
    labels = {
        \"identity_and_purpose\": \"Identity and purpose\",
        \"evidence_status\": \"Evidence status\",
        \"account_summary\": \"Account summary\",
        \"configuration\": \"Configuration\",
        \"price_equity_path\": \"Price and equity path\",
        \"events_trades_holdings\": \"Events, trades, and holdings\",
        \"costs_and_accounting\": \"Costs and accounting\",
        \"total_return_claim\": \"Total-return claim\",
        \"matched_exposure_qualification\": \"Matched-exposure qualification\",
        \"limitations\": \"Limitations\",
        \"provenance\": \"Provenance\",
    }
    parts = [\"<!doctype html><html lang=\\\"en\\\"><head><meta charset=\\\"utf-8\\\">\",
             \"<meta name=\\\"viewport\\\" content=\\\"width=device-width,initial-scale=1\\\">\",
             \"<title>Canonical Attempt Report</title><style>\",
             \"body{font:16px system-ui;margin:0;padding:1rem;color:#17202a;background:#fff}main{max-width:76rem;margin:auto}h1{font-size:1.5rem}section{margin:1rem 0;padding:1rem;border:1px solid #ccd6dd;border-radius:.5rem}.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%}th,td{text-align:left;vertical-align:top;padding:.5rem;border-bottom:1px solid #dde4e8}th{width:18rem}code,pre{white-space:pre-wrap;word-break:break-word}@media(max-width:420px){body{padding:.5rem}section{padding:.65rem}th{width:auto}}\",
             \"</style></head><body><main><h1>Canonical Attempt Report</h1>\",
             \"<p>This document presents sealed evidence. It does not recompute research facts or authorize deployment or trading.</p>\"]
    for section in payload[\"sections\"]:
        section_id = section[\"section_id\"]
        parts.append(\"<section aria-labelledby=\\\"section-\" + _escape(section_id) + \"\\\"><h2 id=\\\"section-\" + _escape(section_id) + \"\\\">\" + _escape(labels[section_id]) + \"</h2><div class=\\\"table-wrap\\\"><table><tbody>\")
        for field in section[\"fields\"]:
            raw = field[\"raw\"]
            shown = field[\"display\"] if field[\"display\"] is not None else raw
            if type(shown) in (dict, list):
                shown = str(shown)
            parts.append(\"<tr><th scope=\\\"row\\\">\" + _escape(field[\"field_id\"].replace(\"_\", \" \")) + \"</th><td><span>\" + _escape(field[\"availability\"]) + \"</span><br><code>\" + _escape(shown) + \"</code></td></tr>\")
        parts.append(\"</tbody></table></div></section>\")
    parts.append(\"</main></body></html>\\n\")
    return \"\".join(parts)
"""
REPORT_PARAMETER_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}
REPORT_DEFAULTS: dict[str, Any] = {}
REPORT_DOCUMENTATION = (
    "# Canonical Attempt Report\n\n"
    "Renders a closed, source-bound ReportDocument as deterministic offline HTML. "
    "It is presentation only and confers no qualification, ranking, deployment, signal, "
    "order, or trading authority.\n"
)


def _canonical_file(value: Any) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _identity(domain: bytes, value: Mapping[str, Any]) -> str:
    return _sha256(domain + canonical_json_bytes(dict(value)))


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256.fullmatch(value) is None:
        raise AttemptReportError(f"{label} must be a SHA-256 value")
    return value


def _strict_json(payload: bytes, label: str) -> Any:
    if len(payload) > MAX_JSON_BYTES:
        raise AttemptReportError(f"{label} exceeds the JSON size limit")
    if payload.startswith(b"\xef\xbb\xbf"):
        raise AttemptReportError(f"{label} contains a UTF-8 BOM")

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise AttemptReportError(f"{label} contains duplicate key {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeError, ValueError) as exc:
        if isinstance(exc, AttemptReportError):
            raise
        raise AttemptReportError(f"{label} is not strict JSON") from exc


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_regular(
    path: Path,
    *,
    immutable: bool = True,
    maximum: int = MAX_JSON_BYTES,
) -> bytes:
    try:
        before = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise AttemptReportError(f"missing evidence file: {path.name}") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise AttemptReportError(f"unsafe evidence file: {path.name}")
    if immutable and (stat.S_IMODE(before.st_mode) != 0o444):
        raise AttemptReportError(f"evidence file is not mode 0444: {path.name}")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if _stat_identity(opened) != _stat_identity(before):
            raise AttemptReportError(f"evidence file changed while opening: {path.name}")
        if opened.st_size > maximum:
            raise AttemptReportError(f"evidence file is too large: {path.name}")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if _stat_identity(after) != _stat_identity(opened) or len(payload) != opened.st_size:
            raise AttemptReportError(f"evidence file changed while reading: {path.name}")
    finally:
        os.close(descriptor)
    current = os.stat(path, follow_symlinks=False)
    if _stat_identity(current) != _stat_identity(before):
        raise AttemptReportError(f"evidence path changed while reading: {path.name}")
    return payload


def _read_sealed_directory(
    root: Path,
    expected_names: frozenset[str],
    *,
    maximum: int = MAX_JSON_BYTES,
) -> dict[str, bytes]:
    try:
        metadata = os.stat(root, follow_symlinks=False)
    except OSError as exc:
        raise AttemptReportError("sealed evidence directory is missing") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o555:
        raise AttemptReportError("sealed evidence directory is unsafe or writable")
    names = {entry.name for entry in root.iterdir()}
    if names != expected_names:
        raise AttemptReportError("sealed evidence directory has an unexpected topology")
    return {
        name: _read_regular(root / name, maximum=maximum)
        for name in sorted(expected_names)
    }


def _core_result_digest(payloads: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in CORE_RESULT_FILES:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payloads[name])
        digest.update(b"\0")
    return digest.hexdigest()


def construct_bundle_descriptor(
    run_dir: Path | str,
    audit_path: Path | str,
) -> tuple[dict[str, Any], dict[str, bytes], dict[str, Any]]:
    run_root = Path(run_dir)
    audit_file = Path(audit_path)
    actual_names = frozenset(entry.name for entry in run_root.iterdir())
    if actual_names not in {BASE_RUN_FILES, BASE_RUN_FILES | SETTLEMENT_FILES}:
        raise AttemptReportError("sealed run has an unexpected artifact topology")
    payloads = _read_sealed_directory(run_root, actual_names)
    manifest = _strict_json(payloads["run_manifest.json"], "run manifest")
    config = _strict_json(payloads["config.json"], "run configuration")
    audit_bytes = _read_regular(audit_file)
    audit = _strict_json(audit_bytes, "Attempt audit")
    if not isinstance(manifest, dict) or not isinstance(config, dict) or not isinstance(audit, dict):
        raise AttemptReportError("bundle JSON roots must be objects")
    expected_hashed = actual_names - {"run_manifest.json"}
    if set(manifest.get("files", {})) != expected_hashed:
        raise AttemptReportError("run manifest artifact set is incomplete")
    for name in sorted(expected_hashed):
        expected = {"sha256": _sha256(payloads[name]), "size": len(payloads[name])}
        if manifest["files"].get(name) != expected:
            raise AttemptReportError(f"run artifact digest mismatch: {name}")
    if _sha256(canonical_json_bytes(config)) != manifest.get("config_sha256"):
        raise AttemptReportError("run configuration digest mismatch")
    core_digest = _core_result_digest(payloads)
    if (
        audit.get("result_digest") != core_digest
        or audit.get("run_id") != manifest.get("run_id")
        or audit.get("result_path") != str(run_root)
        or audit.get("dataset", {}).get("snapshot_id") != manifest.get("dataset_snapshot_id")
    ):
        raise AttemptReportError("Attempt audit does not bind the sealed run")
    for field in ("attempt_id", "experiment_id", "result_digest"):
        _require_sha256(audit.get(field), f"Attempt audit {field}")
    descriptor_core = {
        "schema_id": "quant-platform/attempt-result-bundle/v1",
        "schema_version": 1,
        "attempt_id": audit["attempt_id"],
        "experiment_id": audit["experiment_id"],
        "run_id": audit["run_id"],
        "core_result_digest": core_digest,
        "attempt_audit": {
            "path": "attempt-audit.json",
            "size": len(audit_bytes),
            "sha256": _sha256(audit_bytes),
        },
        "files": [
            {"path": name, "size": len(payloads[name]), "sha256": _sha256(payloads[name])}
            for name in sorted(payloads)
        ],
        "verification": {"status": "VERIFIED"},
    }
    return descriptor_core | {"bundle_id": _identity(DOMAIN_BUNDLE, descriptor_core)}, payloads, audit


def verify_bundle_descriptor(
    descriptor: Mapping[str, Any],
    run_dir: Path | str,
    audit_path: Path | str,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    current, payloads, audit = construct_bundle_descriptor(run_dir, audit_path)
    if canonical_json_bytes(dict(descriptor)) != canonical_json_bytes(current):
        raise AttemptReportError("bundle descriptor does not match sealed evidence")
    return payloads, audit


def _pointer(value: Any, pointer: str) -> Any:
    current = value
    if pointer == "/":
        return current
    for token in pointer.lstrip("/").split("/"):
        decoded = token.replace("~1", "/").replace("~0", "~")
        current = current[int(decoded)] if isinstance(current, list) else current[decoded]
    return current


def _attachment_payload(value: Mapping[str, Any], selector: str) -> Any:
    if selector == "$binding":
        return {key: item for key, item in value.items() if key not in BINDING_EXCLUDED_KEYS}
    return _pointer(value, selector)


def _check_event(event: Mapping[str, Any]) -> None:
    if set(event) != {"study_id", "sequence", "event_type", "payload", "payload_sha256"}:
        raise AttemptReportError("Study event fields are invalid")
    if (
        event["event_type"] != "CANDIDATE_EVALUATED"
        or type(event["sequence"]) is not int
        or event["sequence"] < 1
        or event["payload_sha256"] != _sha256(canonical_json_bytes(event["payload"]))
    ):
        raise AttemptReportError("Study event payload identity is invalid")


def _check_attachment_identity(value: Mapping[str, Any]) -> None:
    kind = value.get("kind")
    if kind not in AUTHORITY_TOPOLOGY or set(value) != ATTACHMENT_KEYS[kind]:
        raise AttemptReportError("authority attachment fields are invalid")
    expected = []
    for path, selector in AUTHORITY_TOPOLOGY[kind]:
        body = _canonical_file(_attachment_payload(value, selector))
        expected.append({"path": path, "size": len(body), "sha256": _sha256(body)})
    if value["files"] != expected:
        raise AttemptReportError("authority attachment payload hashes are invalid")
    core = {key: item for key, item in value.items() if key != "attachment_id"}
    if value["attachment_id"] != _identity(DOMAIN_ATTACHMENT, core):
        raise AttemptReportError("authority attachment identity is invalid")


def _reissue_qualification_chain(
    ancestry: Sequence[Mapping[str, Any]], terminal: Mapping[str, Any]
) -> list[dict[str, Any]]:
    from .study_qualification import (
        _issue_capability,
        validate_qualification,
        validate_schema,
    )

    records = [*ancestry, terminal]
    issued = []
    prior = None
    for supplied in records:
        validate_schema(dict(supplied))
        if supplied["implementation_source_sha256"] != MATCHED_EXPOSURE_SOURCE_SHA256:
            raise AttemptReportError("matched-exposure implementation identity mismatch")
        validate_qualification(dict(supplied), prior, require_live_runtime=False)
        current = _issue_capability(dict(supplied))
        if canonical_json_bytes(current) != canonical_json_bytes(dict(supplied)):
            raise AttemptReportError("matched-exposure source qualification identity mismatch")
        issued.append(dict(current))
        prior = current
    return issued


def _check_family(family: Mapping[str, Any], chain: Sequence[Mapping[str, Any]]) -> None:
    from .study_qualification import projection_digest, validate_schema

    validate_schema(dict(family), "#/$defs/family_record")
    if projection_digest("family_record", dict(family)) != family["family_digest"]:
        raise AttemptReportError("matched-exposure family identity mismatch")
    inspected = family["inspected_candidate_digests"]
    covered = family["covered_candidate_digests"]
    population = family["candidate_population_entries"]
    multiplicity = family["multiplicity_entries"]
    population_ids = [row["candidate_digest"] for row in population]
    multiplicity_ids = [row["candidate_digest"] for row in multiplicity]
    if any(items != sorted(items) or len(items) != len(set(items)) for items in (inspected, covered, population_ids, multiplicity_ids)):
        raise AttemptReportError("matched-exposure family sets are not canonical")
    if not (inspected == covered == population_ids == multiplicity_ids):
        raise AttemptReportError("matched-exposure family coverage is inconsistent")
    terminal = chain[-1]
    expected_multiplicity = {
        "method": "HOLM_STATIONARY_BLOCK_BOOTSTRAP",
        "version": "1",
        "implementation_source_sha256": family["implementation_source_sha256"],
        "numerical_runtime_manifest_sha256": family["numerical_runtime_manifest_sha256"],
        "family_digest": family["family_digest"],
        "inspected_candidate_digests": family["inspected_candidate_digests"],
        "covered_candidate_digests": family["covered_candidate_digests"],
        "family_closed": True,
        "alpha": family["policy"]["family_wise_alpha"],
        "entries": family["multiplicity_entries"],
    }
    if family["policy"] != terminal["policy"] or terminal["multiplicity"] != expected_multiplicity:
        raise AttemptReportError("matched-exposure terminal-family binding mismatch")
    for key in ("study_id", "study_plan_digest", "selection_run_id", "implementation_source_sha256"):
        if family[key] != terminal[key]:
            raise AttemptReportError("matched-exposure family source binding mismatch")
    if family["numerical_runtime_manifest_sha256"] != terminal["numerical_runtime"]["manifest_sha256"]:
        raise AttemptReportError("matched-exposure family runtime binding mismatch")
    hits = [row for row in population if row["candidate_digest"] == terminal["candidate_digest"]]
    if (
        len(hits) != 1
        or hits[0]["prefamily_state"] != "ADMITTED"
        or hits[0]["prefamily_qualification_id"] != chain[1]["qualification_id"]
        or hits[0]["scored_session_set_digest"] != terminal["scored_session_set_digest"]
    ):
        raise AttemptReportError("matched-exposure family candidate binding mismatch")
    multiplicity_hits = [row for row in multiplicity if row["candidate_digest"] == terminal["candidate_digest"]]
    if multiplicity_hits != [terminal["candidate_multiplicity"]]:
        raise AttemptReportError("matched-exposure candidate multiplicity mismatch")


def _validate_candidate_attachment(value: Mapping[str, Any]) -> None:
    kind = value["kind"]
    expected_state = "ADMISSION_REJECTED" if kind == "MATCHED_EXPOSURE_ADMISSION_REJECTED" else "REJECTED"
    terminal = value["terminal_record"]
    chain = _reissue_qualification_chain(value["ancestry"], terminal)
    expected_states = (
        ["UNADMITTED", "ADMISSION_REJECTED"]
        if expected_state == "ADMISSION_REJECTED"
        else ["UNADMITTED", "ADMITTED", "QUALIFICATION_EVALUATED", "REJECTED"]
    )
    if [record["state"] for record in chain] != expected_states:
        raise AttemptReportError("matched-exposure ancestry is invalid")
    if terminal["study_id"] != value["study_id"] or terminal["candidate_digest"] != value["candidate_digest"]:
        raise AttemptReportError("matched-exposure attachment identity does not join")
    _check_event(value["study_event"])
    expected_payload = {
        "candidate_digest": terminal["candidate_digest"],
        "qualification_id": terminal["qualification_id"],
        "state": terminal["state"],
    }
    if value["study_event"]["study_id"] != terminal["study_id"] or value["study_event"]["payload"] != expected_payload:
        raise AttemptReportError("matched-exposure Study event does not join")
    if expected_state == "REJECTED":
        _check_family(value["family_record"], chain)


def _validate_total_return_attachment(value: Mapping[str, Any]) -> None:
    from .total_return_claims import (
        _validate_record as validate_total_return_record,
        read_time_classification,
    )

    if value["kind"] == "TOTAL_RETURN_FULL":
        prior_bytes = None
        for record in [*value["ancestry"], value["record"]]:
            validate_total_return_record(dict(record), prior_bytes)
            prior_bytes = canonical_json_bytes(record)
        record = value["record"]
        if record["bindings"]["result_digest"] != value["result_digest"]:
            raise AttemptReportError("total-return result binding mismatch")
    else:
        projection = value["projection"]
        current = read_time_classification(
            source_issuer=projection["source_issuer"],
            source_total_return_claim=projection["source_total_return_claim"],
            coverage_state=projection["coverage_state"],
            attempted_after_tax=projection["claim_state"] == "AFTER_TAX_TOTAL_RETURN_UNVERIFIED",
        )
        if canonical_json_bytes(current) != canonical_json_bytes(projection):
            raise AttemptReportError("read-time total-return projection mismatch")


def _validate_study_attachment(
    value: Mapping[str, Any], registry: Mapping[str, Mapping[str, Any]]
) -> None:
    from .study_qualification import no_qualified_candidate

    references = value["qualification_attachment_ids"]
    resolved = []
    for reference in references:
        item = registry.get(reference)
        if item is None:
            raise AttemptReportError("Study qualification attachment is missing")
        if item["kind"] not in {"MATCHED_EXPOSURE_TERMINAL", "MATCHED_EXPOSURE_ADMISSION_REJECTED"}:
            raise AttemptReportError("Study qualification attachment kind is invalid")
        validate_authority_attachment(item, registry=registry)
        resolved.append(item)
    if any(item["study_id"] != value["study_id"] for item in resolved):
        raise AttemptReportError("Study attachment crosses Study identity")
    events = value["study_events"]
    for event in events:
        _check_event(event)
    if events != [item["study_event"] for item in resolved]:
        raise AttemptReportError("Study attachment event order does not join")
    records = [item["terminal_record"] for item in resolved]
    source_projection = no_qualified_candidate(records)
    for key, expected in source_projection.items():
        if value["projection"].get(key) != expected:
            raise AttemptReportError("Study no-qualified projection mismatch")
    if (
        value["projection"].get("claim") != "REJECTED_NO_EDGE"
        or value["projection"].get("statistical_significance") != "NOT_ESTABLISHED"
        or not value["projection"].get("rationale")
    ):
        raise AttemptReportError("Study no-qualified report fields are invalid")
    by_candidate = {item["candidate_digest"]: item for item in resolved}
    if len(by_candidate) != len(resolved):
        raise AttemptReportError("Study candidate identities are not unique")
    for item in resolved:
        if item["kind"] != "MATCHED_EXPOSURE_TERMINAL":
            continue
        population = {
            row["candidate_digest"]: row
            for row in item["family_record"]["candidate_population_entries"]
        }
        if set(population) != set(by_candidate):
            raise AttemptReportError("Study family population does not cover attachments")
        for candidate, peer in by_candidate.items():
            source = peer["terminal_record"] if peer["kind"] == "MATCHED_EXPOSURE_ADMISSION_REJECTED" else peer["ancestry"][1]
            row = population[candidate]
            if row["prefamily_state"] != source["state"] or row["prefamily_qualification_id"] != source["qualification_id"]:
                raise AttemptReportError("Study family predecessor binding mismatch")


def validate_authority_attachment(
    value: Mapping[str, Any],
    *,
    registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AttemptReportError("authority attachment must be an object")
    attachment = copy.deepcopy(dict(value))
    _check_attachment_identity(attachment)
    kind = attachment["kind"]
    if kind.startswith("TOTAL_RETURN_"):
        _validate_total_return_attachment(attachment)
    elif kind.startswith("MATCHED_EXPOSURE_"):
        _validate_candidate_attachment(attachment)
    else:
        if registry is None:
            raise AttemptReportError("Study attachment requires resolved attachment registry")
        _validate_study_attachment(attachment, registry)
    return attachment


def read_authority_attachment(
    state_root: Path | str,
    attachment_id: str,
    *,
    registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    _require_sha256(attachment_id, "attachment_id")
    root = Path(state_root) / "attempt-report-authority" / attachment_id
    manifest_payload = _read_regular(root / "manifest.json")
    value = _strict_json(manifest_payload, "authority attachment manifest")
    if not isinstance(value, dict) or value.get("attachment_id") != attachment_id:
        raise AttemptReportError("authority attachment path identity mismatch")
    topology = AUTHORITY_TOPOLOGY.get(value.get("kind"))
    if topology is None:
        raise AttemptReportError("authority attachment kind is invalid")
    payloads = _read_sealed_directory(
        root,
        frozenset({"manifest.json", *(path for path, _ in topology)}),
    )
    if payloads["manifest.json"] != _canonical_file(value):
        raise AttemptReportError("authority attachment manifest is not canonical")
    for path, selector in topology:
        if payloads[path] != _canonical_file(_attachment_payload(value, selector)):
            raise AttemptReportError(f"authority attachment payload mismatch: {path}")
    return validate_authority_attachment(value, registry=registry)


def _build_attachment(
    kind: str,
    values: Mapping[str, Any],
) -> dict[str, Any]:
    attachment = _CapturedAuthorityAttachment(
        {"kind": kind, **copy.deepcopy(dict(values))}
    )
    attachment["files"] = [
        {
            "path": path,
            "size": len(body := _canonical_file(_attachment_payload(attachment, selector))),
            "sha256": _sha256(body),
        }
        for path, selector in AUTHORITY_TOPOLOGY[kind]
    ]
    attachment["attachment_id"] = _identity(DOMAIN_ATTACHMENT, attachment)
    return attachment


def capture_total_return_full(
    *,
    attempt_id: str,
    experiment_id: str,
    bundle_id: str,
    result_digest: str,
    metric_document_digest: str,
    record: Any,
    ancestry: Sequence[Any] = (),
) -> dict[str, Any]:
    from .total_return_claims import (
        is_trusted_qualification,
        qualification_record,
    )

    if not is_trusted_qualification(record) or any(not is_trusted_qualification(item) for item in ancestry):
        raise AttemptReportError("total-return capture requires live pristine authority")
    return _build_attachment(
        "TOTAL_RETURN_FULL",
        {
            "attempt_id": attempt_id,
            "experiment_id": experiment_id,
            "bundle_id": bundle_id,
            "result_digest": result_digest,
            "metric_document_digest": metric_document_digest,
            "record": qualification_record(record),
            "ancestry": [qualification_record(item) for item in ancestry],
        },
    )


def capture_total_return_read_time(
    *,
    attempt_id: str,
    experiment_id: str,
    bundle_id: str,
    result_digest: str,
    metric_document_digest: str,
    source_issuer: str,
    source_total_return_claim: str,
    coverage_state: str,
    attempted_after_tax: bool = False,
) -> dict[str, Any]:
    from .total_return_claims import read_time_classification

    return _build_attachment(
        "TOTAL_RETURN_READ_TIME",
        {
            "attempt_id": attempt_id,
            "experiment_id": experiment_id,
            "bundle_id": bundle_id,
            "result_digest": result_digest,
            "metric_document_digest": metric_document_digest,
            "projection": read_time_classification(
                source_issuer=source_issuer,
                source_total_return_claim=source_total_return_claim,
                coverage_state=coverage_state,
                attempted_after_tax=attempted_after_tax,
            ),
        },
    )


def capture_matched_exposure_terminal(
    *,
    attempt_id: str,
    experiment_id: str,
    bundle_id: str,
    result_digest: str,
    study_id: str,
    candidate_digest: str,
    metric_document_digest: str,
    terminal_record: Any,
    ancestry: Sequence[Any],
    family_record: Any,
    study_event: Mapping[str, Any],
) -> dict[str, Any]:
    from .study_qualification import is_pristine_family, is_pristine_qualification

    if (
        not is_pristine_qualification(terminal_record)
        or any(not is_pristine_qualification(item) for item in ancestry)
        or not is_pristine_family(family_record)
    ):
        raise AttemptReportError("matched-exposure capture requires live pristine authority")
    attachment = _build_attachment(
        "MATCHED_EXPOSURE_TERMINAL",
        {
            "attempt_id": attempt_id,
            "experiment_id": experiment_id,
            "bundle_id": bundle_id,
            "result_digest": result_digest,
            "study_id": study_id,
            "candidate_digest": candidate_digest,
            "metric_document_digest": metric_document_digest,
            "terminal_record": dict(terminal_record),
            "ancestry": [dict(item) for item in ancestry],
            "family_record": dict(family_record),
            "study_event": dict(study_event),
        },
    )
    validate_authority_attachment(attachment)
    return attachment


def capture_matched_exposure_admission_rejected(
    *,
    study_id: str,
    candidate_digest: str,
    terminal_record: Any,
    ancestry: Sequence[Any],
    study_event: Mapping[str, Any],
) -> dict[str, Any]:
    from .study_qualification import is_pristine_qualification

    if not is_pristine_qualification(terminal_record) or any(
        not is_pristine_qualification(item) for item in ancestry
    ):
        raise AttemptReportError("matched-exposure capture requires live pristine authority")
    attachment = _build_attachment(
        "MATCHED_EXPOSURE_ADMISSION_REJECTED",
        {
            "study_id": study_id,
            "candidate_digest": candidate_digest,
            "terminal_record": dict(terminal_record),
            "ancestry": [dict(item) for item in ancestry],
            "study_event": dict(study_event),
        },
    )
    validate_authority_attachment(attachment)
    return attachment


def capture_study_no_qualified(
    *,
    study_id: str,
    qualification_attachments: Sequence[Mapping[str, Any]],
    study_events: Sequence[Mapping[str, Any]],
    projection: Mapping[str, Any],
    current_events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if canonical_json_bytes(list(study_events)) != canonical_json_bytes(list(current_events)):
        raise AttemptReportError("Study capture does not match the append-only event reader")
    registry = {item["attachment_id"]: item for item in qualification_attachments}
    attachment = _build_attachment(
        "STUDY_TERMINAL_NO_QUALIFIED",
        {
            "study_id": study_id,
            "qualification_attachment_ids": [
                item["attachment_id"] for item in qualification_attachments
            ],
            "study_events": [dict(item) for item in study_events],
            "projection": dict(projection),
        },
    )
    validate_authority_attachment(attachment, registry=registry)
    return attachment


def publish_authority_attachment(
    state_root: Path | str,
    attachment: Mapping[str, Any],
    *,
    registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    if not isinstance(attachment, _CapturedAuthorityAttachment):
        raise AttemptReportError(
            "authority attachment publication requires an issuer-side capture"
        )
    value = validate_authority_attachment(attachment, registry=registry)
    root = Path(state_root) / "attempt-report-authority"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / value["attachment_id"]
    if target.exists():
        read_authority_attachment(state_root, value["attachment_id"], registry=registry)
        return value["attachment_id"]
    staging = Path(tempfile.mkdtemp(prefix=".attachment-", dir=root))
    try:
        for path, selector in AUTHORITY_TOPOLOGY[value["kind"]]:
            _write_new(staging / path, _canonical_file(_attachment_payload(value, selector)))
        _write_new(staging / "manifest.json", _canonical_file(value))
        _seal_and_sync(staging)
        os.rename(staging, target)
        _fsync_directory(root)
    finally:
        if staging.exists():
            _remove_staging(staging)
    read_authority_attachment(state_root, value["attachment_id"], registry=registry)
    return value["attachment_id"]


def _number(value: str, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise AttemptReportError(f"{label} is not numeric") from exc
    if not math.isfinite(result):
        raise AttemptReportError(f"{label} is not finite")
    return result


def _integer(value: str, label: str) -> int:
    if not isinstance(value, str) or re.fullmatch(r"-?[0-9]+", value) is None:
        raise AttemptReportError(f"{label} is not an integer")
    return int(value)


def _csv_rows(payload: bytes, label: str) -> list[dict[str, str]]:
    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise AttemptReportError(f"{label} is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None or len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise AttemptReportError(f"{label} header is invalid")
    rows = list(reader)
    if any(None in row for row in rows):
        raise AttemptReportError(f"{label} has malformed rows")
    return rows


def _project_rows(
    rows: Sequence[Mapping[str, str]],
    fields: Sequence[tuple[str, str, bool]],
    label: str,
) -> list[dict[str, Any]]:
    result = []
    required = {source for _, source, _ in fields}
    for index, row in enumerate(rows):
        if not required.issubset(row):
            raise AttemptReportError(f"{label} row {index} lacks required columns")
        projected: dict[str, Any] = {}
        for target, source, nullable in fields:
            raw = row[source]
            if nullable and raw == "":
                value: Any = None
            elif target in {"Date", "date", "entry_date", "exit_date", "side", "reason", "status"}:
                value = raw
            elif target in {"quantity", "holdings", "position_after", "holdings_before", "holdings_after"}:
                value = _integer(raw, f"{label}.{target}")
            else:
                value = _number(raw, f"{label}.{target}")
            projected[target] = value
        result.append(projected)
    return result


def _available(
    field_id: str,
    raw: Any,
    *,
    source: tuple[str, str, str] | None = None,
    display: str | None = None,
) -> dict[str, Any]:
    artifact, pointer, unit = source or FIELD_SOURCE_AND_UNIT[field_id]
    return {
        "field_id": field_id,
        "source_ref": {"artifact": artifact, "pointer": pointer},
        "availability": "AVAILABLE",
        "reason": None,
        "raw": copy.deepcopy(raw),
        "display": display,
        "unit": unit,
    }


def _unavailable(field_id: str, reason: str, display: str = "Not evaluated") -> dict[str, Any]:
    artifact, pointer, unit = FIELD_SOURCE_AND_UNIT[field_id]
    return {
        "field_id": field_id,
        "source_ref": {"artifact": artifact, "pointer": pointer},
        "availability": "NOT_EVALUATED",
        "reason": reason,
        "raw": None,
        "display": display,
        "unit": unit,
    }


def _display(value: Any) -> str:
    if isinstance(value, float):
        return format(value, ".12g")
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (dict, list)):
        return canonical_json_bytes(value).decode("utf-8")
    return str(value)


def _bound_attachment(
    value: Mapping[str, Any] | None,
    descriptor: Mapping[str, Any],
    audit: Mapping[str, Any],
    expected_kinds: set[str],
    *,
    registry: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    if value is None:
        return None
    attachment = validate_authority_attachment(value, registry=registry)
    if attachment["kind"] not in expected_kinds:
        raise AttemptReportError("authority attachment kind does not match report field")
    _validate_attachment_bindings(
        attachment,
        {
            "attempt_id": audit["attempt_id"],
            "experiment_id": audit["experiment_id"],
            "bundle_id": descriptor["bundle_id"],
            "result_digest": audit["result_digest"],
        },
    )
    return attachment


def _validate_attachment_bindings(
    attachment: Mapping[str, Any], expected: Mapping[str, Any]
) -> None:
    for key, expected_value in expected.items():
        if key in attachment and attachment[key] != expected_value:
            raise AttemptReportError(f"authority attachment {key} binding mismatch")


def build_report_document(
    descriptor: Mapping[str, Any],
    payloads: Mapping[str, bytes],
    audit: Mapping[str, Any],
    operator: Mapping[str, Any],
    *,
    total_return_attachment: Mapping[str, Any] | None = None,
    matched_exposure_attachment: Mapping[str, Any] | None = None,
    study_terminal_attachment: Mapping[str, Any] | None = None,
    attachment_registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    registry = dict(attachment_registry or {})
    for item in (total_return_attachment, matched_exposure_attachment, study_terminal_attachment):
        if item is not None and isinstance(item.get("attachment_id"), str):
            registry[item["attachment_id"]] = item
    total = _bound_attachment(
        total_return_attachment,
        descriptor,
        audit,
        {"TOTAL_RETURN_FULL", "TOTAL_RETURN_READ_TIME"},
        registry=registry,
    )
    matched = _bound_attachment(
        matched_exposure_attachment,
        descriptor,
        audit,
        {"MATCHED_EXPOSURE_TERMINAL"},
        registry=registry,
    )
    study = _bound_attachment(
        study_terminal_attachment,
        descriptor,
        audit,
        {"STUDY_TERMINAL_NO_QUALIFIED"},
        registry=registry,
    )
    metrics = _strict_json(payloads["metrics.json"], "metrics")
    costs = _strict_json(payloads["cost_breakdown.json"], "cost breakdown")
    config = _strict_json(payloads["config.json"], "run configuration")
    run_manifest = _strict_json(payloads["run_manifest.json"], "run manifest")
    if not all(isinstance(value, dict) for value in (metrics, costs, config, run_manifest)):
        raise AttemptReportError("report JSON sources must be objects")
    daily = _csv_rows(payloads["daily_replay.csv"], "daily replay")
    events = _csv_rows(payloads["events.csv"], "events")
    trades = _csv_rows(payloads["trades.csv"], "trades")
    price_rows = _project_rows(
        daily,
        (("date", "Date", False), ("price", "price", False), ("close", "close", False), ("equity", "equity", False)),
        "daily replay",
    )
    holding_rows = _project_rows(
        daily,
        (("date", "Date", False), ("holdings", "holdings", False), ("position_after", "position_after", False)),
        "daily replay",
    )
    event_rows = _project_rows(
        events,
        (
            ("Date", "Date", False), ("side", "side", False), ("price", "price", False),
            ("quantity", "quantity", False), ("notional_cny", "notional_cny", False),
            ("commission_cny", "commission_cny", False), ("transfer_fee_cny", "transfer_fee_cny", False),
            ("stamp_tax_cny", "stamp_tax_cny", False), ("slippage_cny", "slippage_cny", False),
            ("total_cost_cny", "total_cost_cny", False), ("cash_before_cny", "cash_before_cny", False),
            ("cash_after_cny", "cash_after_cny", False), ("holdings_before", "holdings_before", False),
            ("holdings_after", "holdings_after", False), ("reason", "reason", False),
        ),
        "events",
    )
    trade_rows = _project_rows(
        trades,
        (
            ("entry_date", "entry_date", False), ("entry_price", "entry_price", False),
            ("quantity", "quantity", False), ("entry_cost_cny", "entry_cost_cny", False),
            ("exit_date", "exit_date", True), ("exit_price", "exit_price", True),
            ("exit_cost_cny", "exit_cost_cny", False), ("status", "status", False),
            ("gross_pnl_cny", "gross_pnl_cny", False), ("net_pnl_cny", "net_pnl_cny", False),
            ("return", "return", False),
        ),
        "trades",
    )

    def metric(name: str) -> Any:
        if name not in metrics:
            raise AttemptReportError(f"metrics is missing {name}")
        return metrics[name]

    def cost(name: str) -> Any:
        if name not in costs:
            raise AttemptReportError(f"cost breakdown is missing {name}")
        return costs[name]

    optional_accounting = []
    for name in ("gross_dividends_cny", "dividend_tax_cny", "outstanding_tax_cny"):
        optional_accounting.append(
            _available(name, metrics[name], display=_display(metrics[name]))
            if name in metrics
            else _unavailable(name, f"{name} is absent from this sealed run")
        )
    if total is None:
        total_status = _unavailable("total_return_status", "No total-return authority attachment was supplied")
        promotion = _unavailable("promotion_ready", "No total-return authority attachment was supplied")
        total_field = _unavailable("total_return_attachment", "No total-return authority attachment was supplied")
    else:
        source_name = "record" if total["kind"] == "TOTAL_RETURN_FULL" else "projection"
        source = total[source_name]
        total_status = _available(
            "total_return_status",
            source["claim_state"],
            source=("total-return-attachment", f"/{source_name}/claim_state", "TOKEN"),
            display=source["claim_state"],
        )
        promotion = _available(
            "promotion_ready",
            source["ranking"]["eligible_for_promotion"],
            source=("total-return-attachment", f"/{source_name}/ranking/eligible_for_promotion", "BOOLEAN"),
            display=_display(source["ranking"]["eligible_for_promotion"]),
        )
        total_field = _available("total_return_attachment", total, display="Verified sealed attachment")
    if matched is None:
        matched_status = _unavailable("matched_exposure_status", "No matched-exposure authority attachment was supplied")
        ranking_status = _unavailable("ranking_status", "No matched-exposure authority attachment was supplied")
        matched_field = _unavailable("matched_exposure_attachment", "No matched-exposure authority attachment was supplied")
    else:
        matched_status = _available("matched_exposure_status", matched["terminal_record"]["state"], display=matched["terminal_record"]["state"])
        ranking_status = _available("ranking_status", matched["terminal_record"]["ranking_status"], display=matched["terminal_record"]["ranking_status"])
        matched_field = _available("matched_exposure_attachment", matched, display="Verified sealed attachment")
    study_field = (
        _available("study_terminal_attachment", study, display="Verified sealed no-qualified Study terminal")
        if study is not None
        else _unavailable("study_terminal_attachment", "No no-qualified Study terminal attachment was supplied")
    )
    source_sha = operator.get("source_sha256")
    content_digest = operator.get("content_digest")
    _require_sha256(source_sha, "report operator source_sha256")
    _require_sha256(content_digest, "report operator content_digest")
    sections = [
        {"section_id": "identity_and_purpose", "fields": [
            _available("attempt_id", audit["attempt_id"], display=audit["attempt_id"]),
            _available("experiment_id", audit["experiment_id"], display=audit["experiment_id"]),
            _available("run_id", audit["run_id"], display=audit["run_id"]),
            _available("dataset_snapshot_id", audit["dataset"]["snapshot_id"], display=audit["dataset"]["snapshot_id"]),
            _available("purpose", "PRESENTATION_ONLY", display="Presentation only"),
        ]},
        {"section_id": "evidence_status", "fields": [
            _available("bundle_integrity", "VERIFIED", display="Verified"), total_status,
            matched_status, ranking_status, promotion,
        ]},
        {"section_id": "account_summary", "fields": [
            _available(name, metric(name), display=_display(metric(name)))
            for name in ("period_start", "period_end", "initial_capital_cny", "final_equity_cny", "net_profit_cny", "current_position", "closed_trades", "open_trades")
        ]},
        {"section_id": "configuration", "fields": [
            _available("template_parameters", config["template"]["parameters"], display=_display(config["template"]["parameters"])),
            _available("operators", audit["operators"], display=_display(audit["operators"])),
            _available("runtime", run_manifest["runtime"], display=_display(run_manifest["runtime"])),
        ]},
        {"section_id": "price_equity_path", "fields": [_available("price_equity_rows", price_rows, display=f"{len(price_rows)} rows")]},
        {"section_id": "events_trades_holdings", "fields": [
            _available("events", event_rows, display=f"{len(event_rows)} rows"),
            _available("trades", trade_rows, display=f"{len(trade_rows)} rows"),
            _available("holdings", holding_rows, display=f"{len(holding_rows)} rows"),
        ]},
        {"section_id": "costs_and_accounting", "fields": [
            *[_available(name, cost(name), display=_display(cost(name))) for name in ("commission_cny", "transfer_fee_cny", "stamp_tax_cny", "slippage_cny", "total_cost_cny")],
            *optional_accounting,
        ]},
        {"section_id": "total_return_claim", "fields": [
            _available("net_return", metric("net_return"), display=_display(metric("net_return"))),
            _available("max_drawdown", metric("max_drawdown"), display=_display(metric("max_drawdown"))),
            total_field,
        ]},
        {"section_id": "matched_exposure_qualification", "fields": [matched_field, study_field]},
        {"section_id": "limitations", "fields": [
            _available("integrity_not_qualification", "INTEGRITY_IS_NOT_QUALIFICATION", display="Integrity is not qualification"),
            _available("qualification_not_deployment", "QUALIFICATION_IS_NOT_DEPLOYMENT_OR_TRADING_AUTHORITY", display="Qualification is not deployment or trading authority"),
            _available("no_recomputation", "PRESENTATION_ONLY_NO_RECOMPUTATION", display="Presentation only; no recomputation"),
        ]},
        {"section_id": "provenance", "fields": [
            _available("bundle_id", descriptor["bundle_id"], display=descriptor["bundle_id"]),
            _available("core_result_digest", audit["result_digest"], display=audit["result_digest"]),
            _available("operator_id", REPORT_OPERATOR_ID, display=REPORT_OPERATOR_ID),
            _available("operator_version", REPORT_OPERATOR_VERSION, display=REPORT_OPERATOR_VERSION),
            _available("operator_source_sha256", source_sha, display=source_sha),
            _available("operator_content_digest", content_digest, display=content_digest),
        ]},
    ]
    core = {
        "schema_id": REPORT_DOCUMENT_SCHEMA_ID,
        "schema_version": 1,
        "sections": sections,
    }
    document = core | {"document_id": _identity(DOMAIN_DOCUMENT, core)}
    validate_report_document(
        document,
        total_return_attachment=total,
        matched_exposure_attachment=matched,
        study_terminal_attachment=study,
        attachment_registry=registry,
    )
    return document


def _field_map(document: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        field["field_id"]: field
        for section in document["sections"]
        for field in section["fields"]
    }


def validate_report_document(
    value: Mapping[str, Any],
    *,
    total_return_attachment: Mapping[str, Any] | None = None,
    matched_exposure_attachment: Mapping[str, Any] | None = None,
    study_terminal_attachment: Mapping[str, Any] | None = None,
    attachment_registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"schema_id", "schema_version", "document_id", "sections"}:
        raise AttemptReportError("ReportDocument fields are invalid")
    document = copy.deepcopy(dict(value))
    if document["schema_id"] != REPORT_DOCUMENT_SCHEMA_ID or document["schema_version"] != 1:
        raise AttemptReportError("ReportDocument schema identity is invalid")
    sections = document["sections"]
    if not isinstance(sections, list) or len(sections) != len(REPORT_SECTION_FIELDS):
        raise AttemptReportError("ReportDocument section count is invalid")
    for section, (section_id, field_ids) in zip(sections, REPORT_SECTION_FIELDS, strict=True):
        if not isinstance(section, dict) or set(section) != {"section_id", "fields"} or section["section_id"] != section_id:
            raise AttemptReportError("ReportDocument section order is invalid")
        fields = section["fields"]
        if not isinstance(fields, list) or [item.get("field_id") for item in fields if isinstance(item, dict)] != list(field_ids):
            raise AttemptReportError(f"ReportDocument field order is invalid: {section_id}")
        for field in fields:
            if set(field) != {"field_id", "source_ref", "availability", "reason", "raw", "display", "unit"}:
                raise AttemptReportError("ReportDocument field shape is invalid")
            if field["availability"] == "AVAILABLE":
                if field["raw"] is None or field["reason"] is not None or field["display"] is not None and not isinstance(field["display"], str):
                    raise AttemptReportError(f"available field is invalid: {field['field_id']}")
            elif field["availability"] in {"UNAVAILABLE", "NOT_EVALUATED", "NOT_QUALIFIED"}:
                if field["raw"] is not None or not isinstance(field["reason"], str) or not field["reason"] or not isinstance(field["display"], str) or not field["display"]:
                    raise AttemptReportError(f"unavailable field is invalid: {field['field_id']}")
            else:
                raise AttemptReportError(f"field availability is invalid: {field['field_id']}")
            expected = FIELD_SOURCE_AND_UNIT[field["field_id"]]
            source = field["source_ref"]
            if not isinstance(source, dict) or set(source) != {"artifact", "pointer"} or field["unit"] != expected[2]:
                raise AttemptReportError(f"field source/unit is invalid: {field['field_id']}")
            if field["field_id"] in {"total_return_status", "promotion_ready"}:
                allowed_pointers = {
                    "total_return_status": {"/record/claim_state", "/projection/claim_state", "/"},
                    "promotion_ready": {"/record/ranking/eligible_for_promotion", "/projection/ranking/eligible_for_promotion", "/"},
                }
                if source["artifact"] != "total-return-attachment" or source["pointer"] not in allowed_pointers[field["field_id"]]:
                    raise AttemptReportError(f"field source is invalid: {field['field_id']}")
            elif (source["artifact"], source["pointer"]) != expected[:2]:
                raise AttemptReportError(f"field source is invalid: {field['field_id']}")
    fields = _field_map(document)
    for field_id, expected in (
        ("attempt_id", SHA256), ("experiment_id", SHA256), ("dataset_snapshot_id", SHA256),
        ("bundle_id", SHA256), ("core_result_digest", SHA256), ("operator_source_sha256", SHA256),
        ("operator_content_digest", SHA256),
    ):
        if fields[field_id]["availability"] == "AVAILABLE" and (not isinstance(fields[field_id]["raw"], str) or expected.fullmatch(fields[field_id]["raw"]) is None):
            raise AttemptReportError(f"ReportDocument identity field is invalid: {field_id}")
    if fields["purpose"]["raw"] != "PRESENTATION_ONLY" or fields["bundle_integrity"]["raw"] != "VERIFIED":
        raise AttemptReportError("ReportDocument authority labels are invalid")
    if fields["operator_id"]["raw"] != REPORT_OPERATOR_ID or fields["operator_version"]["raw"] != REPORT_OPERATOR_VERSION:
        raise AttemptReportError("ReportDocument operator identity is invalid")
    registry = dict(attachment_registry or {})
    attachment_specs = (
        (
            "total_return_attachment",
            total_return_attachment,
            {"TOTAL_RETURN_FULL", "TOTAL_RETURN_READ_TIME"},
        ),
        (
            "matched_exposure_attachment",
            matched_exposure_attachment,
            {"MATCHED_EXPOSURE_TERMINAL"},
        ),
        (
            "study_terminal_attachment",
            study_terminal_attachment,
            {"STUDY_TERMINAL_NO_QUALIFIED"},
        ),
    )
    for field_id, supplied, _ in attachment_specs:
        embedded = fields[field_id]["raw"] if fields[field_id]["availability"] == "AVAILABLE" else None
        for attachment in (embedded, supplied):
            if isinstance(attachment, Mapping) and isinstance(attachment.get("attachment_id"), str):
                registry[attachment["attachment_id"]] = attachment
    expected_bindings = {
        "attempt_id": fields["attempt_id"]["raw"],
        "experiment_id": fields["experiment_id"]["raw"],
        "bundle_id": fields["bundle_id"]["raw"],
        "result_digest": fields["core_result_digest"]["raw"],
    }
    resolved_attachments: dict[str, dict[str, Any]] = {}
    for field_id, supplied, expected_kinds in attachment_specs:
        embedded = fields[field_id]["raw"] if fields[field_id]["availability"] == "AVAILABLE" else None
        if supplied is not None and embedded is not None and canonical_json_bytes(dict(supplied)) != canonical_json_bytes(dict(embedded)):
            raise AttemptReportError(
                f"ReportDocument supplied attachment differs from embedded attachment: {field_id}"
            )
        selected = supplied if supplied is not None else embedded
        if selected is None:
            continue
        attachment = validate_authority_attachment(selected, registry=registry)
        if attachment["kind"] not in expected_kinds:
            raise AttemptReportError(f"ReportDocument attachment kind is invalid: {field_id}")
        _validate_attachment_bindings(attachment, expected_bindings)
        resolved_attachments[field_id] = attachment
    total = resolved_attachments.get("total_return_attachment")
    if total is not None:
        source_name = "record" if total["kind"] == "TOTAL_RETURN_FULL" else "projection"
        if (
            fields["total_return_status"]["raw"] != total[source_name]["claim_state"]
            or fields["promotion_ready"]["raw"]
            != total[source_name]["ranking"]["eligible_for_promotion"]
        ):
            raise AttemptReportError("ReportDocument total-return source mismatch")
    matched = resolved_attachments.get("matched_exposure_attachment")
    if matched is not None:
        terminal = matched["terminal_record"]
        if (
            fields["matched_exposure_status"]["raw"] != terminal["state"]
            or fields["ranking_status"]["raw"] != terminal["ranking_status"]
        ):
            raise AttemptReportError("ReportDocument matched-exposure source mismatch")
    study = resolved_attachments.get("study_terminal_attachment")
    if total is not None and matched is not None and total["metric_document_digest"] != matched["metric_document_digest"]:
        raise AttemptReportError("ReportDocument authority MetricDocument binding mismatch")
    if study is not None and matched is not None and (
        study["study_id"] != matched["study_id"]
        or matched["attachment_id"] not in study["qualification_attachment_ids"]
    ):
        raise AttemptReportError("ReportDocument Study/candidate attachment binding mismatch")
    core = {key: item for key, item in document.items() if key != "document_id"}
    if document["document_id"] != _identity(DOMAIN_DOCUMENT, core):
        raise AttemptReportError("ReportDocument identity is invalid")
    return document


def _native_render(document: Mapping[str, Any]) -> str:
    parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>Canonical Attempt Report</title><style>',
        "body{font:16px system-ui;margin:0;padding:1rem;color:#17202a;background:#fff}main{max-width:76rem;margin:auto}h1{font-size:1.5rem}section{margin:1rem 0;padding:1rem;border:1px solid #ccd6dd;border-radius:.5rem}.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%}th,td{text-align:left;vertical-align:top;padding:.5rem;border-bottom:1px solid #dde4e8}th{width:18rem}code,pre{white-space:pre-wrap;word-break:break-word}@media(max-width:420px){body{padding:.5rem}section{padding:.65rem}th{width:auto}}",
        "</style></head><body><main><h1>Canonical Attempt Report</h1>",
        "<p>This document presents sealed evidence. It does not recompute research facts or authorize deployment or trading.</p>",
    ]
    for section in document["sections"]:
        section_id = section["section_id"]
        parts.append(
            f'<section aria-labelledby="section-{html.escape(section_id)}"><h2 id="section-{html.escape(section_id)}">{html.escape(SECTION_LABELS[section_id])}</h2><div class="table-wrap"><table><tbody>'
        )
        for field in section["fields"]:
            shown = field["display"] if field["display"] is not None else field["raw"]
            if isinstance(shown, (dict, list)):
                shown = str(shown)
            parts.append(
                f'<tr><th scope="row">{html.escape(field["field_id"].replace("_", " "))}</th><td><span>{html.escape(field["availability"])}</span><br><code>{html.escape(str(shown))}</code></td></tr>'
            )
        parts.append("</tbody></table></div></section>")
    parts.append("</main></body></html>\n")
    return "".join(parts)


def render_report_document(value: Mapping[str, Any], parameters: Mapping[str, Any] | None = None) -> bytes:
    if dict(parameters or {}) != {}:
        raise AttemptReportError("canonical report parameters must be empty")
    document = validate_report_document(value)
    before = canonical_json_bytes(document)
    first = _native_render(copy.deepcopy(document)).encode("utf-8")
    second = _native_render(copy.deepcopy(document)).encode("utf-8")
    if canonical_json_bytes(document) != before:
        raise AttemptReportError("canonical report renderer mutated its input")
    if first != second:
        raise AttemptReportError("canonical report renderer is nondeterministic")
    if not first or len(first) > MAX_REPORT_BYTES:
        raise AttemptReportError("canonical report HTML is empty or too large")
    lowered = first.lower()
    if b"http://" in lowered or b"https://" in lowered:
        raise AttemptReportError("canonical report HTML contains a remote resource")
    return first


def canonical_report_operator_bundle() -> dict[str, Any]:
    source = CANONICAL_REPORT_OPERATOR_SOURCE.encode("utf-8")
    tests = [
        {
            "input_contract": REPORT_DOCUMENT_SCHEMA_ID,
            "parameters": {},
            "expectations": ["deterministic", "no-input-mutation", "offline-html"],
        }
    ]
    content = {
        "defaults.json": _canonical_file(REPORT_DEFAULTS),
        "documentation.md": REPORT_DOCUMENTATION.encode("utf-8"),
        "operator.py": source,
        "parameter-schema.json": _canonical_file(REPORT_PARAMETER_SCHEMA),
        "tests.json": _canonical_file(tests),
    }
    descriptor = {
        "api_version": REPORT_OPERATOR_API_VERSION,
        "operator_id": REPORT_OPERATOR_ID,
        "slot": "report",
        "semantic_version": REPORT_OPERATOR_VERSION,
        "files": [
            {"path": name, "size": len(content[name]), "sha256": _sha256(content[name])}
            for name in REPORT_CONTENT_FILES
        ],
    }
    content_digest = _identity(DOMAIN_OPERATOR_CONTENT, descriptor)
    manifest = descriptor | {"content_digest": content_digest}
    evidence = {
        "schema_version": 2,
        "passed": True,
        "candidate_digest": content_digest,
        "source_sha256": _sha256(source),
        "conformance_digest": _sha256(content["tests.json"]),
        "render_only": True,
    }
    return {
        "content": content,
        "manifest": manifest,
        "evidence": evidence,
        "content_digest": content_digest,
        "source_sha256": _sha256(source),
        "parameter_schema": copy.deepcopy(REPORT_PARAMETER_SCHEMA),
        "defaults": {},
    }


def verify_report_operator_bundle(
    bundle_dir: Path | str,
    *,
    expected_content_digest: str | None = None,
) -> dict[str, Any]:
    payloads = _read_sealed_directory(Path(bundle_dir), REPORT_BUNDLE_FILES)
    manifest = _strict_json(payloads["manifest.json"], "report operator manifest")
    evidence = _strict_json(payloads["evidence.json"], "report operator evidence")
    if not isinstance(manifest, dict) or set(manifest) != {
        "api_version", "operator_id", "slot", "semantic_version", "files", "content_digest"
    }:
        raise AttemptReportError("report operator manifest fields are invalid")
    descriptor = {key: item for key, item in manifest.items() if key != "content_digest"}
    expected_files = [
        {"path": name, "size": len(payloads[name]), "sha256": _sha256(payloads[name])}
        for name in REPORT_CONTENT_FILES
    ]
    digest = _identity(DOMAIN_OPERATOR_CONTENT, descriptor)
    if (
        manifest["api_version"] != 2
        or manifest["operator_id"] != REPORT_OPERATOR_ID
        or manifest["slot"] != "report"
        or manifest["semantic_version"] != REPORT_OPERATOR_VERSION
        or manifest["files"] != expected_files
        or manifest["content_digest"] != digest
        or expected_content_digest is not None and digest != expected_content_digest
    ):
        raise AttemptReportError("report operator content identity mismatch")
    if payloads["manifest.json"] != _canonical_file(manifest) or payloads["evidence.json"] != _canonical_file(evidence):
        raise AttemptReportError("report operator JSON bytes are not canonical")
    source_sha256 = _sha256(payloads["operator.py"])
    if (
        not isinstance(evidence, dict)
        or set(evidence) != {"schema_version", "passed", "candidate_digest", "source_sha256", "conformance_digest", "render_only"}
        or evidence != {
            "schema_version": 2,
            "passed": True,
            "candidate_digest": digest,
            "source_sha256": source_sha256,
            "conformance_digest": _sha256(payloads["tests.json"]),
            "render_only": True,
        }
    ):
        raise AttemptReportError("report operator conformance evidence is invalid")
    schema = _strict_json(payloads["parameter-schema.json"], "report parameter schema")
    defaults = _strict_json(payloads["defaults.json"], "report defaults")
    if schema != REPORT_PARAMETER_SCHEMA or defaults != {}:
        raise AttemptReportError("report operator parameters are not the empty contract")
    return {
        "operator_id": REPORT_OPERATOR_ID,
        "semantic_version": REPORT_OPERATOR_VERSION,
        "api_version": 2,
        "content_digest": digest,
        "source_sha256": source_sha256,
        "parameter_schema": schema,
        "defaults": defaults,
        "conformance_digest": evidence["conformance_digest"],
    }


def _write_new(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _seal_and_sync(path: Path) -> None:
    for child in path.iterdir():
        metadata = os.stat(child, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise AttemptReportError(f"unsafe publication file: {child.name}")
        child.chmod(0o444)
    _fsync_directory(path)
    path.chmod(0o555)


def _remove_staging(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise AttemptReportError("refusing to remove unsafe staging path")
    path.chmod(0o700)
    for child in path.iterdir():
        if child.is_symlink() or not child.is_file():
            raise AttemptReportError("refusing to remove unsafe staging entry")
        child.chmod(0o600)
    shutil.rmtree(path)


def _build_report_manifest(
    attempt_id: str,
    bundle_id: str,
    operator_content_digest: str,
    document_bytes: bytes,
    report_html: bytes,
    document_id: str,
) -> dict[str, Any]:
    core = {
        "schema_id": REPORT_MANIFEST_SCHEMA_ID,
        "schema_version": 1,
        "attempt_id": attempt_id,
        "bundle_id": bundle_id,
        "document_id": document_id,
        "report_document_sha256": _sha256(document_bytes),
        "report_html_sha256": _sha256(report_html),
        "operator_content_digest": operator_content_digest,
        "files": [
            {"path": "report-document.json", "size": len(document_bytes), "sha256": _sha256(document_bytes)},
            {"path": "report.html", "size": len(report_html), "sha256": _sha256(report_html)},
        ],
    }
    return core | {"report_artifact_id": _identity(DOMAIN_ARTIFACT, core)}


def validate_report_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema_id", "schema_version", "attempt_id", "bundle_id", "document_id",
        "report_document_sha256", "report_html_sha256", "operator_content_digest",
        "files", "report_artifact_id",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise AttemptReportError("report manifest fields are invalid")
    manifest = copy.deepcopy(dict(value))
    if manifest["schema_id"] != REPORT_MANIFEST_SCHEMA_ID or manifest["schema_version"] != 1:
        raise AttemptReportError("report manifest schema identity is invalid")
    for field in ("attempt_id", "bundle_id", "document_id", "report_document_sha256", "report_html_sha256", "operator_content_digest", "report_artifact_id"):
        _require_sha256(manifest[field], f"report manifest {field}")
    if not isinstance(manifest["files"], list) or [item.get("path") for item in manifest["files"] if isinstance(item, dict)] != ["report-document.json", "report.html"]:
        raise AttemptReportError("report manifest file order is invalid")
    core = {key: item for key, item in manifest.items() if key != "report_artifact_id"}
    if manifest["report_artifact_id"] != _identity(DOMAIN_ARTIFACT, core):
        raise AttemptReportError("report artifact identity is invalid")
    return manifest


def read_report_artifact(
    state_root: Path | str,
    attempt_id: str,
    report_artifact_id: str,
) -> dict[str, Any]:
    _require_sha256(attempt_id, "attempt_id")
    _require_sha256(report_artifact_id, "report_artifact_id")
    root = Path(state_root) / "attempt-reports" / attempt_id / "artifacts" / report_artifact_id
    payloads = _read_sealed_directory(root, REPORT_ARTIFACT_FILES, maximum=MAX_REPORT_BYTES)
    manifest = validate_report_manifest(_strict_json(payloads["report-manifest.json"], "report manifest"))
    document = validate_report_document(_strict_json(payloads["report-document.json"], "ReportDocument"))
    expected = [
        {"path": "report-document.json", "size": len(payloads["report-document.json"]), "sha256": _sha256(payloads["report-document.json"])},
        {"path": "report.html", "size": len(payloads["report.html"]), "sha256": _sha256(payloads["report.html"])},
    ]
    if (
        payloads["report-manifest.json"] != _canonical_file(manifest)
        or manifest["attempt_id"] != attempt_id
        or manifest["report_artifact_id"] != report_artifact_id
        or manifest["document_id"] != document["document_id"]
        or manifest["files"] != expected
        or manifest["report_document_sha256"] != expected[0]["sha256"]
        or manifest["report_html_sha256"] != expected[1]["sha256"]
    ):
        raise AttemptReportError("report artifact bindings are invalid")
    return {"manifest": manifest, "document": document, "html": payloads["report.html"]}


def _validate_pointer_record(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema_id", "schema_version", "attempt_id", "report_artifact_id",
        "report_manifest_sha256", "report_document_sha256", "operator_content_digest",
        "sequence", "previous_pointer_id", "pointer_id",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise AttemptReportError("latest pointer fields are invalid")
    pointer = copy.deepcopy(dict(value))
    if pointer["schema_id"] != LATEST_POINTER_SCHEMA_ID or pointer["schema_version"] != 1:
        raise AttemptReportError("latest pointer schema identity is invalid")
    for field in ("attempt_id", "report_artifact_id", "report_manifest_sha256", "report_document_sha256", "operator_content_digest", "pointer_id"):
        _require_sha256(pointer[field], f"latest pointer {field}")
    if type(pointer["sequence"]) is not int or pointer["sequence"] < 1:
        raise AttemptReportError("latest pointer sequence is invalid")
    previous = pointer["previous_pointer_id"]
    if pointer["sequence"] == 1:
        if previous is not None:
            raise AttemptReportError("first latest pointer has a predecessor")
    else:
        _require_sha256(previous, "latest pointer predecessor")
    core = {key: item for key, item in pointer.items() if key != "pointer_id"}
    if pointer["pointer_id"] != _identity(DOMAIN_POINTER, core):
        raise AttemptReportError("latest pointer identity is invalid")
    return pointer


def validate_latest_pointer_transition(
    prior: Mapping[str, Any], current: Mapping[str, Any]
) -> None:
    left = _validate_pointer_record(prior)
    right = _validate_pointer_record(current)
    if (
        right["attempt_id"] != left["attempt_id"]
        or right["sequence"] != left["sequence"] + 1
        or right["previous_pointer_id"] != left["pointer_id"]
    ):
        raise AttemptReportError("latest pointer publication transition is invalid")


def read_latest_report(state_root: Path | str, attempt_id: str) -> dict[str, Any]:
    _require_sha256(attempt_id, "attempt_id")
    path = Path(state_root) / "attempt-reports" / attempt_id / "latest.json"
    payload = _read_regular(path)
    pointer = _validate_pointer_record(_strict_json(payload, "latest report pointer"))
    if payload != _canonical_file(pointer) or pointer["attempt_id"] != attempt_id:
        raise AttemptReportError("latest report pointer bytes or Attempt binding are invalid")
    artifact = read_report_artifact(state_root, attempt_id, pointer["report_artifact_id"])
    manifest_bytes = _canonical_file(artifact["manifest"])
    manifest = artifact["manifest"]
    if (
        pointer["report_manifest_sha256"] != _sha256(manifest_bytes)
        or pointer["report_document_sha256"] != manifest["report_document_sha256"]
        or pointer["operator_content_digest"] != manifest["operator_content_digest"]
    ):
        raise AttemptReportError("latest report pointer artifact binding is invalid")
    return {"pointer": pointer, **artifact}


@contextmanager
def _attempt_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        root / ".publication.lock",
        os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def publish_report_artifact(
    state_root: Path | str,
    descriptor: Mapping[str, Any],
    document: Mapping[str, Any],
    report_html: bytes,
    *,
    fault: str | None = None,
) -> dict[str, Any]:
    checked_document = validate_report_document(document)
    attempt_id = checked_document["sections"][0]["fields"][0]["raw"]
    fields = _field_map(checked_document)
    if descriptor["attempt_id"] != attempt_id or fields["bundle_id"]["raw"] != descriptor["bundle_id"]:
        raise AttemptReportError("report publication identities do not join")
    document_bytes = _canonical_file(checked_document)
    if report_html != render_report_document(checked_document):
        raise AttemptReportError("report HTML does not match the closed document")
    manifest = _build_report_manifest(
        attempt_id,
        descriptor["bundle_id"],
        fields["operator_content_digest"]["raw"],
        document_bytes,
        report_html,
        checked_document["document_id"],
    )
    artifact_id = manifest["report_artifact_id"]
    reports_root = Path(state_root) / "attempt-reports"
    reports_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    attempt_root = reports_root / attempt_id
    artifacts_root = attempt_root / "artifacts"
    artifacts_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = artifacts_root / artifact_id
    if not target.exists():
        staging = Path(tempfile.mkdtemp(prefix=".artifact-", dir=artifacts_root))
        try:
            _write_new(staging / "report-document.json", document_bytes)
            _write_new(staging / "report.html", report_html)
            _write_new(staging / "report-manifest.json", _canonical_file(manifest))
            _seal_and_sync(staging)
            os.rename(staging, target)
            _fsync_directory(artifacts_root)
        finally:
            if staging.exists():
                _remove_staging(staging)
    artifact = read_report_artifact(state_root, attempt_id, artifact_id)
    if fault == "before_pointer":
        raise AttemptReportError("injected failure before latest-pointer replacement")
    with _attempt_lock(attempt_root):
        latest = attempt_root / "latest.json"
        prior = None
        if latest.exists():
            prior = read_latest_report(state_root, attempt_id)["pointer"]
        core = {
            "schema_id": LATEST_POINTER_SCHEMA_ID,
            "schema_version": 1,
            "attempt_id": attempt_id,
            "report_artifact_id": artifact_id,
            "report_manifest_sha256": _sha256(_canonical_file(artifact["manifest"])),
            "report_document_sha256": artifact["manifest"]["report_document_sha256"],
            "operator_content_digest": artifact["manifest"]["operator_content_digest"],
            "sequence": 1 if prior is None else prior["sequence"] + 1,
            "previous_pointer_id": None if prior is None else prior["pointer_id"],
        }
        pointer = core | {"pointer_id": _identity(DOMAIN_POINTER, core)}
        _validate_pointer_record(pointer)
        if prior is not None:
            validate_latest_pointer_transition(prior, pointer)
        descriptor_fd, temporary_name = tempfile.mkstemp(prefix=".latest-", dir=attempt_root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor_fd, "wb") as stream:
                stream.write(_canonical_file(pointer))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o444)
            _fsync_directory(attempt_root)
            if fault == "during_pointer":
                raise AttemptReportError("injected failure during latest-pointer publication")
            os.replace(temporary, latest)
            _fsync_directory(attempt_root)
        finally:
            if temporary.exists():
                temporary.unlink()
        current = read_latest_report(state_root, attempt_id)
        if current["pointer"]["pointer_id"] != pointer["pointer_id"]:
            raise AttemptReportError("latest pointer read-back did not match publication")
    return {"artifact_id": artifact_id, "pointer_id": pointer["pointer_id"], "sequence": pointer["sequence"]}


def publish_attempt_report(
    state_root: Path | str,
    run_dir: Path | str,
    audit_path: Path | str,
    operator: Mapping[str, Any],
    *,
    total_return_attachment: Mapping[str, Any] | None = None,
    matched_exposure_attachment: Mapping[str, Any] | None = None,
    study_terminal_attachment: Mapping[str, Any] | None = None,
    attachment_registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    descriptor, payloads, audit = construct_bundle_descriptor(run_dir, audit_path)
    document = build_report_document(
        descriptor,
        payloads,
        audit,
        operator,
        total_return_attachment=total_return_attachment,
        matched_exposure_attachment=matched_exposure_attachment,
        study_terminal_attachment=study_terminal_attachment,
        attachment_registry=attachment_registry,
    )
    report_html = render_report_document(document, operator.get("effective_parameters", {}))
    result = publish_report_artifact(state_root, descriptor, document, report_html)
    return {**result, "bundle_id": descriptor["bundle_id"], "document_id": document["document_id"]}
