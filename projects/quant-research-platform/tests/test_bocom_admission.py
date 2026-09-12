from __future__ import annotations

import copy
import hashlib
import os
import shutil
from pathlib import Path

import pytest

from quant_platform.bocom_admission import (
    ACTION_EVIDENCE_SHA256,
    ACTION_SNAPSHOT_ID,
    BocomAdmissionError,
    EVIDENCE_CLASS,
    INTERVAL,
    PARENT_SNAPSHOT_ID,
    build_bocom_action_snapshot,
    load_bocom_admission_package,
    prepare_bocom_admission,
)
from quant_platform.corporate_actions import (
    ARTIFACT_DOMAIN,
    COVERAGE_DOMAIN,
    RETRIEVAL_DOMAIN,
    REVISION_DOMAIN,
    CorporateActionEvidenceError,
    accounting_cash_dividends,
    admit_corporate_action_evidence,
    identity_digest,
)


SOURCE_PACKAGE = os.environ.get("QUANT_BOCOM_SOURCE_PACKAGE")
PARENT_SNAPSHOT = os.environ.get("QUANT_BOCOM_PARENT_SNAPSHOT")


def _copy_source_package(tmp_path: Path) -> Path:
    assert SOURCE_PACKAGE is not None
    return Path(shutil.copytree(SOURCE_PACKAGE, tmp_path / "package", symlinks=True))


def _rehash_first_revision(document: dict) -> None:
    revision = document["revisions"][0]
    revision_id = identity_digest(REVISION_DOMAIN, revision["payload"])
    revision["event_revision_id"] = revision_id
    revision["normalization_digest"] = revision_id
    coverage = document["coverage"]
    coverage["payload"]["event_revision_ids"][0] = revision_id
    coverage["coverage_id"] = identity_digest(COVERAGE_DOMAIN, coverage["payload"])


@pytest.mark.skipif(SOURCE_PACKAGE is None, reason="exact BOCOM source package is not mounted")
def test_exact_source_package_adapts_to_two_action_complete_evidence():
    assert SOURCE_PACKAGE is not None
    package = load_bocom_admission_package(Path(SOURCE_PACKAGE))

    actions = accounting_cash_dividends(package.evidence)
    assert [str(action.gross_cash_per_share) for action in actions] == ["0.1563", "0.1684"]
    assert [(action.record_date.isoformat(), action.pay_date.isoformat()) for action in actions] == [
        ("2025-12-24", "2025-12-25"),
        ("2026-07-09", "2026-07-10"),
    ]
    coverage = package.evidence.document["coverage"]["payload"]
    assert (coverage["interval_start"], coverage["interval_end"]) == INTERVAL
    assert coverage["coverage_state"] == "VERIFIED_COMPLETE_INTERVAL"
    assert package.evidence.document["source_package"] == package.package_binding


@pytest.mark.skipif(SOURCE_PACKAGE is None, reason="exact BOCOM source package is not mounted")
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gross_cash_per_share", "9.9999"),
        ("record_date", "2025-12-23"),
        ("contributing_notice_ids", ["REHASHED-WRONG-NOTICE"]),
    ],
)
def test_rehashed_v2_event_terms_cannot_claim_exact_package(field: str, value):
    assert SOURCE_PACKAGE is not None
    package = load_bocom_admission_package(Path(SOURCE_PACKAGE))
    document = copy.deepcopy(package.evidence.document)
    document["revisions"][0]["payload"][field] = value
    _rehash_first_revision(document)

    with pytest.raises(CorporateActionEvidenceError, match="exact accepted source package"):
        admit_corporate_action_evidence(document, package.evidence.artifact_bytes)


@pytest.mark.skipif(SOURCE_PACKAGE is None, reason="exact BOCOM source package is not mounted")
def test_rehashed_v2_artifact_identity_cannot_claim_exact_package():
    assert SOURCE_PACKAGE is not None
    package = load_bocom_admission_package(Path(SOURCE_PACKAGE))
    document = copy.deepcopy(package.evidence.document)
    artifact = document["artifacts"][0]
    old_artifact_id = artifact["artifact_id"]
    forged_bytes = package.evidence.artifact_bytes[old_artifact_id] + b"forged"
    artifact_id = identity_digest(ARTIFACT_DOMAIN, forged_bytes)
    artifact["artifact_id"] = artifact_id
    artifact["body_sha256"] = hashlib.sha256(forged_bytes).hexdigest()
    artifact["byte_length"] = len(forged_bytes)
    artifact["path"] = f"corporate-action-{artifact_id}.bin"
    retrieval = document["retrievals"][0]
    retrieval["payload"]["artifact_id"] = artifact_id
    retrieval_id = identity_digest(RETRIEVAL_DOMAIN, retrieval["payload"])
    retrieval["retrieval_id"] = retrieval_id
    revision = document["revisions"][0]
    revision["payload"]["source_artifact_ids"] = [artifact_id]
    _rehash_first_revision(document)
    document["coverage"]["payload"]["query_retrieval_ids"][0] = retrieval_id
    document["coverage"]["coverage_id"] = identity_digest(
        COVERAGE_DOMAIN, document["coverage"]["payload"]
    )
    artifact_bytes = dict(package.evidence.artifact_bytes)
    artifact_bytes.pop(old_artifact_id)
    artifact_bytes[artifact_id] = forged_bytes

    with pytest.raises(CorporateActionEvidenceError, match="exact accepted source package"):
        admit_corporate_action_evidence(document, artifact_bytes)


@pytest.mark.skipif(SOURCE_PACKAGE is None, reason="exact BOCOM source package is not mounted")
def test_v2_source_binding_must_name_exact_package():
    assert SOURCE_PACKAGE is not None
    package = load_bocom_admission_package(Path(SOURCE_PACKAGE))
    document = copy.deepcopy(package.evidence.document)
    document["source_package"]["checksums_sha256"] = "0" * 64

    with pytest.raises(CorporateActionEvidenceError, match="accepted package"):
        admit_corporate_action_evidence(document, package.evidence.artifact_bytes)


@pytest.mark.skipif(SOURCE_PACKAGE is None, reason="exact BOCOM source package is not mounted")
def test_rewritten_checksum_manifest_cannot_admit_altered_official_member(tmp_path: Path):
    package_root = _copy_source_package(tmp_path)
    relative = "sources/official/chinatax-cai-shui-2015-101.html"
    forged = b"forged official policy bytes"
    (package_root / relative).write_bytes(forged)
    manifest = package_root / "CHECKSUMS.sha256"
    manifest.write_text(
        manifest.read_text().replace(
            "9eadaaa4693282e4a9b65ca5f02b0b2ab4720e6b0cfe6da97be4c1f1faa9845b"
            f"  {relative}",
            f"{hashlib.sha256(forged).hexdigest()}  {relative}",
        )
    )

    with pytest.raises(BocomAdmissionError, match="checksum manifest identity"):
        load_bocom_admission_package(package_root)


@pytest.mark.skipif(SOURCE_PACKAGE is None, reason="exact BOCOM source package is not mounted")
@pytest.mark.parametrize("attack", ["unmanifested-file", "nested-manifest", "directory-symlink"])
def test_unmanifested_and_symlink_entries_fail_closed(tmp_path: Path, attack: str):
    package_root = _copy_source_package(tmp_path)
    if attack == "unmanifested-file":
        (package_root / "extra.txt").write_text("not manifested")
    elif attack == "nested-manifest":
        extra = package_root / "extra"
        extra.mkdir()
        (extra / "CHECKSUMS.sha256").write_text("not the root manifest")
    else:
        (package_root / "unmanifested-link").symlink_to(
            package_root / "sources", target_is_directory=True
        )

    with pytest.raises(BocomAdmissionError, match="source package|unmanifested|unexpected"):
        load_bocom_admission_package(package_root)


@pytest.mark.skipif(
    SOURCE_PACKAGE is None or PARENT_SNAPSHOT is None,
    reason="exact BOCOM package and parent Snapshot are not mounted",
)
def test_exact_parent_produces_schema4_action_aware_snapshot(tmp_path: Path):
    assert SOURCE_PACKAGE is not None
    assert PARENT_SNAPSHOT is not None
    package = load_bocom_admission_package(Path(SOURCE_PACKAGE))

    result = build_bocom_action_snapshot(Path(PARENT_SNAPSHOT), tmp_path, package)

    assert result["parent_snapshot_id"] == PARENT_SNAPSHOT_ID
    assert result["schema_version"] == 4
    assert result["event_count"] == 2
    assert result["data_start"] == "2019-05-06"
    assert result["data_end"] == INTERVAL[1]


@pytest.mark.skipif(
    SOURCE_PACKAGE is None or PARENT_SNAPSHOT is None,
    reason="exact BOCOM package and parent Snapshot are not mounted",
)
def test_exact_package_prepares_additive_authoritative_admission(tmp_path: Path):
    assert SOURCE_PACKAGE is not None
    assert PARENT_SNAPSHOT is not None
    package = load_bocom_admission_package(Path(SOURCE_PACKAGE))
    built = build_bocom_action_snapshot(Path(PARENT_SNAPSHOT), tmp_path, package)

    publication = prepare_bocom_admission(
        Path(PARENT_SNAPSHOT), Path(built["path"]), package
    )

    assert publication.evidence_id == ACTION_EVIDENCE_SHA256
    assert publication.evidence_class == EVIDENCE_CLASS
    assert publication.snapshot_manifest["snapshot_id"] == ACTION_SNAPSHOT_ID
    assert publication.receipt["parent_snapshot_id"] == PARENT_SNAPSHOT_ID
    assert publication.receipt["parent_child_byte_equal"] is True
    assert publication.receipt["dataset_current_changed"] is False
    assert publication.receipt["replay_performed"] is False
    assert publication.receipt["signal_or_trading_effect"] is False
    assert {
        "ADMISSION.json",
        "evidence.json",
        "corporate-action-142783d88318f144acc4ad097032546d00a6728f46a3d6c5317c62b50f95458a.bin",
        "corporate-action-3396a8e1ced94445f6e6ca732eaf55c9d6977ce19282eafacf525b59df0663f3.bin",
    }.issubset(publication.evidence_members)
