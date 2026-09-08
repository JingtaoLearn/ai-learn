"""Phase-gated command surface for the frozen Gold FOCuS candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Sequence

from gold_research.focus_contract import (
    BOOTSTRAP_BLOCK,
    BOOTSTRAP_REPLICATES,
    BOOTSTRAP_SEED,
    COLLECTION_CLASS,
    DISCLAIMER_ZH,
    INSTRUMENT,
    LABEL_PROXY,
    LABEL_SPREAD,
    SYNTHETIC_PATHS,
    SYNTHETIC_SEED,
    ContractViolation,
    Phase,
    canonical_json_bytes,
    contract_document,
    strict_json_loads,
)
from gold_research.focus_evaluation import calibration_plan
from gold_research.sge_daily_report import CONTENT_TYPE, EXPECTED_HEADERS, build_request, parse_version

_PHASE_ORDER = tuple(Phase)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _regular_single_link(path: Path) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ContractViolation(f"{path} must be a regular single-link file")
    return info


def _write_create_only(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    os.chmod(path, 0o444, follow_symlinks=False)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def publish_immutable(root: Path, name: str, document: dict[str, Any]) -> Path:
    """Publish one canonical document create-only and verify it by two reads."""

    if not name or Path(name).name != name:
        raise ContractViolation("published artifact name must be one path component")
    payload = canonical_json_bytes(document) + b"\n"
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    path = root / name
    try:
        _write_create_only(path, payload)
        _regular_single_link(path)
        if path.read_bytes() != payload or path.read_bytes() != payload:
            raise ContractViolation("published bytes failed read-back")
        os.chmod(root, 0o555, follow_symlinks=False)
        return path
    except Exception:
        if root.exists():
            os.chmod(root, 0o700, follow_symlinks=False)
        raise


def _read_phase_claim(path: Path, ordinal: int, phase: Phase) -> str:
    info = _regular_single_link(path)
    if stat.S_IMODE(info.st_mode) != 0o444:
        raise ContractViolation("phase claim must be sealed read-only")
    raw = path.read_bytes()
    document = strict_json_loads(raw)
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "ordinal",
        "phase",
        "evidence_sha256",
    }:
        raise ContractViolation("phase claim has the wrong schema")
    evidence_sha256 = document["evidence_sha256"]
    if (
        document["schema"] != "quant-research/focus-phase-claim/v1"
        or type(document["ordinal"]) is not int
        or document["ordinal"] != ordinal
        or document["phase"] != phase.value
        or not isinstance(evidence_sha256, str)
        or len(evidence_sha256) != 64
        or any(c not in "0123456789abcdef" for c in evidence_sha256)
    ):
        raise ContractViolation("phase claim identity does not match its ledger position")
    if raw != canonical_json_bytes(document) + b"\n":
        raise ContractViolation("phase claim is not canonically encoded")
    return evidence_sha256


def claim_phase(ledger_root: Path, phase: Phase, evidence_sha256: str) -> bool:
    """Append one phase claim after its immediate prerequisite, never overwriting."""

    if len(evidence_sha256) != 64 or any(c not in "0123456789abcdef" for c in evidence_sha256):
        raise ContractViolation("phase evidence must be a lowercase SHA-256")
    index = _PHASE_ORDER.index(phase)
    ledger_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if index:
        prerequisite = ledger_root / f"{index:02d}-{_PHASE_ORDER[index - 1].value}.json"
        if not prerequisite.is_file():
            raise ContractViolation(f"missing phase prerequisite {_PHASE_ORDER[index - 1].value}")
        _read_phase_claim(prerequisite, index, _PHASE_ORDER[index - 1])
    target = ledger_root / f"{index + 1:02d}-{phase.value}.json"
    document = {
        "schema": "quant-research/focus-phase-claim/v1",
        "ordinal": index + 1,
        "phase": phase.value,
        "evidence_sha256": evidence_sha256,
    }
    payload = canonical_json_bytes(document) + b"\n"
    if target.exists():
        existing_evidence = _read_phase_claim(target, index + 1, phase)
        if existing_evidence != evidence_sha256:
            raise ContractViolation("phase was already claimed with different evidence")
        return False
    _write_create_only(target, payload)
    _regular_single_link(target)
    return True


def _fixture(close: str = "500.00") -> bytes:
    headers = "".join(f"<th>{item}</th>" for item in EXPECTED_HEADERS)
    values = ["2026-09-07", INSTRUMENT, "1", "1", "1", close] + [""] * 8
    row = "".join(f"<td>{item}</td>" for item in values)
    return f"<html><table><tr>{headers}</tr><tr>{row}</tr></table></html>".encode()


def _validate_source_contract(document: Any) -> None:
    if not isinstance(document, dict):
        raise ContractViolation("source envelope contract must be an object")
    expected = {
        "schema": "quant-research/sge-au9999-html-envelope-contract/v1",
        "revision": "008",
        "fail_closed_terminal": "INSUFFICIENT_EVIDENCE",
    }
    for key, value in expected.items():
        if document.get(key) != value:
            raise ContractViolation(f"source envelope contract has wrong {key}")
    sections = (
        document.get("request"),
        document.get("response"),
        document.get("html_envelope"),
        document.get("version_and_chronology_binding"),
    )
    if not all(isinstance(item, dict) for item in sections):
        raise ContractViolation("source envelope contract sections are missing")
    request, response, envelope, version = sections
    assert isinstance(request, dict)
    assert isinstance(response, dict)
    assert isinstance(envelope, dict)
    assert isinstance(version, dict)
    if request.get("host") != "en.sge.com.cn" or request.get("path") != (
        "/data/data_daily_international_new"
    ):
        raise ContractViolation("source endpoint identity changed")
    if request.get("ordered_query") != [
        ["start_date", "<D:YYYY-MM-DD>"],
        ["end_date", "<D:YYYY-MM-DD>"],
        ["inst_ids", INSTRUMENT],
    ]:
        raise ContractViolation("source ordered query changed")
    if response.get("http_status_exact") != 200 or response.get("content_type_exact") != CONTENT_TYPE:
        raise ContractViolation("source HTTP envelope changed")
    if envelope.get("matching_table_count_exact") != 1:
        raise ContractViolation("matching-table cardinality changed")
    if envelope.get("body_row_count_exact") != 1:
        raise ContractViolation("body-row cardinality changed")
    if envelope.get("matching_table_first_row_exact") != list(EXPECTED_HEADERS):
        raise ContractViolation("source table header changed")
    if version.get("collection_class") != COLLECTION_CLASS:
        raise ContractViolation("historical collection class changed")
    if version.get("mixing_with_PROSPECTIVE_D10_V1") != "PROHIBITED":
        raise ContractViolation("collection-class mixing is not prohibited")


def verify_implementation(contract_path: Path, output_root: Path) -> dict[str, Any]:
    contract_info = _regular_single_link(contract_path)
    raw_contract = contract_path.read_bytes()
    source_contract = strict_json_loads(raw_contract)
    _validate_source_contract(source_contract)

    parsed = parse_version(_fixture(), "2026-09-07")
    if parsed.value_decimal != "500.00":
        raise ContractViolation("offline accepted fixture did not replay")
    request = build_request("2026-09-07")
    if "&p=" in request.url or not request.url.endswith("inst_ids=Au99.99"):
        raise ContractViolation("request builder emitted a forbidden parameter")

    source_root = Path(__file__).resolve().parent
    source_names = (
        "focus_contract.py",
        "sge_daily_report.py",
        "focus_evaluation.py",
        "focus_runner.py",
    )
    source_hashes = {}
    for name in source_names:
        source = source_root / name
        _regular_single_link(source)
        source_hashes[name] = _sha256(source.read_bytes())

    receipt = {
        "schema": "quant-research/focus-implementation-verification/v1",
        "status": "IMPLEMENTATION_VERIFIED_OFFLINE",
        "source_envelope_contract_sha256": _sha256(raw_contract),
        "source_envelope_contract_size": contract_info.st_size,
        "request_url": request.url,
        "accepted_fixture_raw_sha256": parsed.raw_sha256,
        "contract": contract_document(),
        "calibration_plan": calibration_plan(),
        "source_sha256": source_hashes,
        "boundaries": {
            "network_requests": 0,
            "calibration_paths_executed": 0,
            "evaluation_executions": 0,
            "production_effects": 0,
        },
        "labels": [LABEL_PROXY, LABEL_SPREAD],
        "disclaimer_zh": DISCLAIMER_ZH,
        "frozen_counts": {
            "synthetic_paths": SYNTHETIC_PATHS,
            "synthetic_seed": SYNTHETIC_SEED,
            "bootstrap_replicates": BOOTSTRAP_REPLICATES,
            "bootstrap_block": BOOTSTRAP_BLOCK,
            "bootstrap_seed": BOOTSTRAP_SEED,
        },
    }
    payload = canonical_json_bytes(receipt) + b"\n"
    evidence_sha256 = _sha256(payload)
    published = output_root / "implementation-verification"
    if published.exists():
        existing = published / "receipt.json"
        _regular_single_link(existing)
        if existing.read_bytes() != payload:
            raise ContractViolation("verification target already exists with different bytes")
        status = "NO_CHANGE"
    else:
        publish_immutable(published, "receipt.json", receipt)
        status = "CREATED"
    claim_phase(output_root / "phase-ledger", Phase.IMPLEMENTATION_VERIFIED, evidence_sha256)
    return {
        "status": status,
        "phase": Phase.IMPLEMENTATION_VERIFIED.value,
        "evidence_sha256": evidence_sha256,
        "network_requests": 0,
        "calibration_paths_executed": 0,
        "evaluation_executions": 0,
    }


def _not_admitted(command: str) -> None:
    raise ContractViolation(
        f"{command} is not admitted before independent implementation Review and its next phase gate"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m gold_research.focus_runner")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify-implementation")
    verify.add_argument("--contract", required=True, type=Path)
    verify.add_argument("--output-root", required=True, type=Path)
    for name in ("calibrate-once", "collect", "finalize", "evaluate-once"):
        child = subparsers.add_parser(name)
        child.add_argument("--contract", required=True, type=Path)
        child.add_argument("--output-root", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "verify-implementation":
            result = verify_implementation(args.contract, args.output_root)
        else:
            _not_admitted(args.command)
            raise AssertionError("unreachable")
    except (ContractViolation, OSError) as exc:
        print(json.dumps({"status": "REJECTED", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
