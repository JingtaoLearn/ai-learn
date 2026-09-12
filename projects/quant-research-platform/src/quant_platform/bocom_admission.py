from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

from .corporate_actions import (
    ARTIFACT_DOMAIN,
    BOCOM_SOURCE_PACKAGE_COMPLETE_CONTRACT,
    COVERAGE_DOMAIN,
    REQUEST_DOMAIN,
    RETRIEVAL_DOMAIN,
    REVISION_DOMAIN,
    SERIES_DOMAIN,
    CorporateActionEvidence,
    CorporateActionEvidenceError,
    admit_corporate_action_evidence,
    identity_digest,
    load_strict_json,
)
from .datasets import _canonical_data_bytes, _verify_snapshot, publish_snapshot


ACTION_ID = "bocom-corporate-action-source-correction-185"
STUDY_ID = "35ee96c76e4491e63c7dd42741beae18b04c04d747e8b7d9760135f130206178"
PARENT_SNAPSHOT_ID = "fb7f22fe0f3bf2dce89e98ff3e8c6a66b78492cadfe159cec9d541492f6a03ae"
INTERVAL = ("2025-08-15", "2026-08-28")
SETTLEMENT_POLICY_ID = "SSE-A-CASH-DIVIDEND-DESIGNATED-TRADE-TPLUS1-V1"
TAX_POLICY_ID = "PRC-LISTED-A-DIVIDEND-TAX-MATRIX-2015-101-V1"
EXPECTED_DOCUMENT_SHA256 = {
    "CANONICAL-EVENTS.json": "6c8ac2d85067c20ff0dafbf346c018d281d348e16cb29a467d9fca75d5fd6024",
    "ACCOUNTING-POLICY.json": "a112be88843c3fcfeece50f0afb20dc027deb70dedc3cad41136155475e60a98",
    "SOURCE-MANIFEST.json": "d0ef2a5b5ed668788855ad912b60c4ce77fa0f6b895d511272e45385f83bd06c",
    "SOURCE-SELECTION.json": "580a1b6348414387d8c51692fe648c8e44d779361d4b6b5221d08f7445ff9286",
    "RETRIEVAL-RECEIPTS.json": "2d8894e7e0cf57eebd1a1e0d161679ee96e36502b5b46ae6dc3bbd4c78bf2c16",
}
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class BocomAdmissionPackage:
    evidence: CorporateActionEvidence
    accepted_members: Mapping[str, tuple[str, bytes]]
    package_binding: Mapping[str, str]


class BocomAdmissionError(ValueError):
    """The frozen BOCOM source package cannot be admitted without ambiguity."""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_regular(root: Path, relative: str) -> bytes:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise BocomAdmissionError(f"unsafe source package path: {relative}")
    path = root / relative_path
    metadata = os.stat(path, follow_symlinks=False)
    if not path.is_file() or path.is_symlink() or metadata.st_nlink != 1:
        raise BocomAdmissionError(f"unsafe source package member: {relative}")
    return path.read_bytes()


def _json(root: Path, name: str) -> dict[str, Any]:
    value = load_strict_json(_read_regular(root, name))
    if not isinstance(value, dict):
        raise BocomAdmissionError(f"source package member is not an object: {name}")
    return value


def _verified_package_files(root: Path) -> dict[str, bytes]:
    checksums = _read_regular(root, "CHECKSUMS.sha256")
    entries: dict[str, str] = {}
    for line in checksums.decode("utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        if not separator or _SHA256.fullmatch(digest) is None or relative in entries:
            raise BocomAdmissionError("source package checksum manifest is invalid")
        entries[relative] = digest
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "CHECKSUMS.sha256"
    }
    if actual != set(entries):
        raise BocomAdmissionError("source package checksum manifest is incomplete")
    payloads = {relative: _read_regular(root, relative) for relative in sorted(entries)}
    for relative, payload in payloads.items():
        if _sha256(payload) != entries[relative]:
            raise BocomAdmissionError(f"source package checksum mismatch: {relative}")
    for name, expected in EXPECTED_DOCUMENT_SHA256.items():
        if entries.get(name) != expected:
            raise BocomAdmissionError(f"source package identity mismatch: {name}")
    payloads["CHECKSUMS.sha256"] = checksums
    return payloads


def _request_parts(url: str) -> tuple[str, dict[str, str]]:
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, strict_parsing=True)
    if set(query) != {"fileId"} or len(query["fileId"]) != 1:
        raise BocomAdmissionError("issuer source URL query is invalid")
    base = parsed._replace(query="").geturl()
    return base, {"fileId": query["fileId"][0]}


def _package_members(payloads: Mapping[str, bytes]) -> dict[str, tuple[str, bytes]]:
    media_types = {
        ".json": "application/json",
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".html": "text/html",
        ".txt": "text/plain",
        ".sha256": "text/plain",
        ".md": "text/markdown",
    }
    members: dict[str, tuple[str, bytes]] = {}
    index: list[dict[str, Any]] = []
    for ordinal, relative in enumerate(sorted(payloads)):
        logical_name = f"{ordinal:03d}-{Path(relative).name}"
        payload = payloads[relative]
        members[logical_name] = (
            media_types.get(Path(relative).suffix.lower(), "application/octet-stream"),
            payload,
        )
        index.append(
            {
                "logical_name": logical_name,
                "relative_path": relative,
                "byte_size": len(payload),
                "sha256": _sha256(payload),
            }
        )
    index_payload = json.dumps(
        {"schema_version": 1, "action_id": ACTION_ID, "members": index},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8") + b"\n"
    members["PACKAGE-INDEX.json"] = ("application/json", index_payload)
    return members


def load_bocom_admission_package(source_root: Path | str) -> BocomAdmissionPackage:
    """Verify source package 185 and adapt it to corporate-action contract v2."""

    root = Path(source_root).resolve()
    payloads = _verified_package_files(root)
    events_document = _json(root, "CANONICAL-EVENTS.json")
    policy = _json(root, "ACCOUNTING-POLICY.json")
    source_manifest = _json(root, "SOURCE-MANIFEST.json")
    source_selection = _json(root, "SOURCE-SELECTION.json")
    retrieval_receipts = _json(root, "RETRIEVAL-RECEIPTS.json")

    if (
        events_document.get("action_id") != ACTION_ID
        or events_document.get("study_id") != STUDY_ID
        or events_document.get("event_interval")
        != {"start_inclusive": INTERVAL[0], "end_inclusive": INTERVAL[1]}
        or events_document.get("inventory_status") != "COMPLETE_POSITIVE_INVENTORY"
    ):
        raise BocomAdmissionError("canonical event package identity or interval is invalid")
    events = events_document.get("events")
    if not isinstance(events, list) or len(events) != 2:
        raise BocomAdmissionError("canonical event inventory must contain exactly two events")
    if [event.get("gross_amount", {}).get("value") for event in events] != ["0.1563", "0.1684"]:
        raise BocomAdmissionError("canonical event amounts do not match the accepted inventory")
    if any(
        event.get("event_kind") != "CASH_DIVIDEND"
        or event.get("settlement_policy_id") != SETTLEMENT_POLICY_ID
        or event.get("tax_policy_id") != TAX_POLICY_ID
        for event in events
    ):
        raise BocomAdmissionError("canonical event kind or policy binding is invalid")

    if (
        policy.get("action_id") != ACTION_ID
        or policy.get("policy_id") != TAX_POLICY_ID
        or policy.get("settlement", {}).get("policy_id") != SETTLEMENT_POLICY_ID
        or policy.get("source_package_missing_or_ambiguous_fields") != []
    ):
        raise BocomAdmissionError("accounting policy identity is invalid")
    required_inputs = policy.get("consumer_required_inputs")
    if required_inputs != [
        "supported_account_profile",
        "whether the account has designated trading",
        "FIFO acquisition lots and dates",
        "shares held at each record-date close",
        "subsequent share-transfer settlement dates and quantities",
    ]:
        raise BocomAdmissionError("downstream consumer inputs are not explicit")

    source_rows = {
        item["path"]: item for item in source_manifest.get("files", []) if isinstance(item, dict)
    }
    receipt_rows = {
        item["source_id"]: item
        for item in retrieval_receipts.get("accepted_byte_retrievals", [])
        if isinstance(item, dict) and "source_id" in item
    }
    if source_selection.get("coverage", {}).get("official_sse_disclosures_enumerated") != 241:
        raise BocomAdmissionError("official disclosure inventory count is invalid")
    if source_selection.get("coverage", {}).get("pagination_complete") is not True:
        raise BocomAdmissionError("official disclosure inventory is incomplete")

    requests: list[dict[str, Any]] = []
    retrievals: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    revisions: list[dict[str, Any]] = []
    artifact_bytes: dict[str, bytes] = {}
    for event in events:
        source = event.get("source")
        if not isinstance(source, dict):
            raise BocomAdmissionError("event source is missing")
        relative = source.get("local_path")
        if not isinstance(relative, str):
            raise BocomAdmissionError("event source path is invalid")
        source_row = source_rows.get(relative)
        if source_row is None or source_row.get("sha256") != source.get("sha256"):
            raise BocomAdmissionError("event source manifest binding is invalid")
        source_id = source_row.get("source_id")
        receipt = receipt_rows.get(source_id) if isinstance(source_id, str) else None
        if receipt is None or receipt.get("sha256") != source.get("sha256"):
            raise BocomAdmissionError("event retrieval receipt binding is invalid")
        source_url = source.get("url")
        source_sha256 = source.get("sha256")
        if not isinstance(source_url, str) or not isinstance(source_sha256, str):
            raise BocomAdmissionError("event source identity is invalid")
        payload = payloads[relative]
        if _sha256(payload) != source_sha256 or not payload.startswith(b"%PDF-"):
            raise BocomAdmissionError("event source bytes are invalid")
        artifact_id = identity_digest(ARTIFACT_DOMAIN, payload)
        artifact_bytes[artifact_id] = payload
        base_url, query = _request_parts(source_url)
        request_payload = {
            "schema_version": 1,
            "method": "GET",
            "url": base_url,
            "query": query,
            "headers": {"accept": "application/pdf"},
        }
        request_id = identity_digest(REQUEST_DOMAIN, request_payload)
        requests.append({"request_id": request_id, "payload": request_payload})
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "body_sha256": source_sha256,
                "byte_length": len(payload),
                "media_type": "application/pdf",
                "path": f"corporate-action-{artifact_id}.bin",
                "source_url": source_url,
            }
        )
        retrieval_payload = {
            "schema_version": 1,
            "request_id": request_id,
            "attempt": 1,
            "started_at": receipt["retrieved_at"],
            "completed_at": receipt["retrieved_at"],
            "redirects": [],
            "final_url": source_url,
            "final_status": 200,
            "media_type": "application/pdf",
            "artifact_id": artifact_id,
        }
        retrieval_id = identity_digest(RETRIEVAL_DOMAIN, retrieval_payload)
        retrievals.append({"retrieval_id": retrieval_id, "payload": retrieval_payload})

        series = {
            "schema_version": 1,
            "instrument": "601328.SS",
            "market": "XSHG",
            "event_class": "CASH_DIVIDEND",
            "root_notice_id": event["event_id"],
        }
        revision_payload = {
            "schema_version": 1,
            "logical_event_id": identity_digest(SERIES_DOMAIN, series),
            "instrument": "601328.SS",
            "market": "XSHG",
            "event_class": "CASH_DIVIDEND",
            "contributing_notice_ids": [event["event_id"]],
            "record_date": event["record_date"],
            "ex_date": event["ex_date"],
            "pay_date": event["payment_date"],
            "gross_cash_per_share": event["gross_amount"]["value"],
            "normalized_currency": "CNY",
            "source_artifact_ids": [artifact_id],
            "parser_version": "accepted-source-package@1",
            "correction_links": [],
        }
        revision_id = identity_digest(REVISION_DOMAIN, revision_payload)
        revisions.append(
            {
                "event_revision_id": revision_id,
                "event_series": series,
                "payload": revision_payload,
                "available_at": receipt["retrieved_at"],
                "use_role": "ACCOUNTING_OUTCOME",
                "source_url": source_url,
                "acceptance_state": "ACCEPTED",
                "normalization_digest": revision_id,
                "findings": [],
            }
        )

    package_binding = {
        "action_id": ACTION_ID,
        "checksums_sha256": _sha256(payloads["CHECKSUMS.sha256"]),
        "canonical_events_sha256": EXPECTED_DOCUMENT_SHA256["CANONICAL-EVENTS.json"],
        "accounting_policy_sha256": EXPECTED_DOCUMENT_SHA256["ACCOUNTING-POLICY.json"],
        "source_manifest_sha256": EXPECTED_DOCUMENT_SHA256["SOURCE-MANIFEST.json"],
        "source_selection_sha256": EXPECTED_DOCUMENT_SHA256["SOURCE-SELECTION.json"],
        "retrieval_receipts_sha256": EXPECTED_DOCUMENT_SHA256["RETRIEVAL-RECEIPTS.json"],
        "settlement_policy_id": SETTLEMENT_POLICY_ID,
        "tax_policy_id": TAX_POLICY_ID,
    }
    coverage_payload = {
        "schema_version": 1,
        "instrument": "601328.SS",
        "market": "XSHG",
        "interval_start": INTERVAL[0],
        "interval_end": INTERVAL[1],
        "checked_as_of": source_manifest["generated_at"],
        "event_revision_ids": [item["event_revision_id"] for item in revisions],
        "query_retrieval_ids": [item["retrieval_id"] for item in retrievals],
        "coverage_state": "VERIFIED_COMPLETE_INTERVAL",
        "limitations": [],
    }
    evidence_document = {
        "schema_version": 1,
        "collector_version": "accepted-source-package-import@1",
        "source_contract_version": "bocom-xshg-dividend@2",
        "complete_enumeration_contract": True,
        "complete_contract_id": identity_digest(
            "quant-platform/complete-enumeration-contract/v1",
            BOCOM_SOURCE_PACKAGE_COMPLETE_CONTRACT,
        ),
        "source_package": package_binding,
        "requests": requests,
        "retrievals": retrievals,
        "artifacts": artifacts,
        "revisions": revisions,
        "coverage": {
            "coverage_id": identity_digest(COVERAGE_DOMAIN, coverage_payload),
            "payload": coverage_payload,
        },
        "findings": ["ACCEPTED_CHECKSUM_BOUND_FIRST_PARTY_SOURCE_PACKAGE"],
        "total_return_claim": "FORBIDDEN",
    }
    try:
        evidence = admit_corporate_action_evidence(evidence_document, artifact_bytes)
    except CorporateActionEvidenceError as exc:
        raise BocomAdmissionError(f"source package cannot satisfy the accepted contract: {exc}") from exc
    return BocomAdmissionPackage(
        evidence=evidence,
        accepted_members=_package_members(payloads),
        package_binding=package_binding,
    )


def build_bocom_action_snapshot(
    parent_snapshot: Path | str,
    output_root: Path | str,
    package: BocomAdmissionPackage,
) -> dict[str, Any]:
    """Create one schema-4 snapshot with byte-equivalent parent market data."""

    parent = Path(parent_snapshot).resolve()
    if parent.name != PARENT_SNAPSHOT_ID:
        raise BocomAdmissionError("parent snapshot identity is not the frozen holdout")
    verified = _verify_snapshot(
        parent,
        PARENT_SNAPSHOT_ID,
        include_frame=True,
        verify_parent=False,
    )
    if not isinstance(verified, tuple):
        raise BocomAdmissionError("parent snapshot verification did not return market data")
    parent_manifest, frame = verified
    lineage = parent_manifest.get("lineage")
    if (
        parent_manifest.get("schema_version") != 3
        or not isinstance(lineage, dict)
        or lineage.get("view_spec", {}).get("scoring_start") != INTERVAL[0]
        or lineage.get("view_spec", {}).get("scoring_end") != INTERVAL[1]
    ):
        raise BocomAdmissionError("parent snapshot is not the frozen holdout view")

    published = publish_snapshot(
        frame,
        output_root,
        parent_manifest["metadata"],
        update_latest=False,
        corporate_action_evidence=package.evidence,
    )
    child = Path(published["path"])
    child_verified = _verify_snapshot(child, published["snapshot_id"], include_frame=True)
    if not isinstance(child_verified, tuple):
        raise BocomAdmissionError("action-aware snapshot verification did not return market data")
    child_manifest, child_frame = child_verified
    if (
        child_manifest["schema_version"] != 4
        or child_manifest["canonical_sha256"] != parent_manifest["canonical_sha256"]
        or _canonical_data_bytes(child_frame) != _canonical_data_bytes(frame)
        or child_manifest["corporate_action_evidence_sha256"] != package.evidence.digest
    ):
        raise BocomAdmissionError("action-aware snapshot changed parent market data or identity")
    return {
        "parent_snapshot_id": PARENT_SNAPSHOT_ID,
        "snapshot_id": child_manifest["snapshot_id"],
        "schema_version": child_manifest["schema_version"],
        "canonical_sha256": child_manifest["canonical_sha256"],
        "corporate_action_evidence_sha256": package.evidence.digest,
        "data_start": child_manifest["data_start"],
        "data_end": child_manifest["data_end"],
        "event_count": len(package.evidence.document["revisions"]),
        "path": str(child),
    }
