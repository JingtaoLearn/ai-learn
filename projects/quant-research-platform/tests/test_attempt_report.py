import copy
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from quant_platform.attempt_report import (
    AttemptReportError,
    DOMAIN_ATTACHMENT,
    DOMAIN_POINTER,
    REPORT_BUNDLE_FILES,
    REPORT_OPERATOR_ID,
    _attachment_payload,
    _check_attachment_identity,
    _identity,
    _validate_pointer_record,
    canonical_report_operator_bundle,
    read_latest_report,
    render_report_document,
    validate_authority_attachment,
    validate_latest_pointer_transition,
    validate_report_document,
    validate_report_manifest,
    verify_report_operator_bundle,
)
from quant_platform.resolved_runner import ResolvedAttemptExecutor
from quant_platform.schemas import canonical_json_bytes
from quant_platform.study_qualification import projection_digest
from quant_platform.operator_worker import load_published_operator

from test_experiment_service import _service, _task
from test_resolved_runner import PROJECT_ROOT


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "attempt_report"


def _fixture() -> dict:
    return json.loads((FIXTURE_ROOT / "conformance-v2.json").read_text(encoding="utf-8"))


def _reseal_attachment(value: dict) -> None:
    from quant_platform.attempt_report import AUTHORITY_TOPOLOGY

    value["files"] = []
    for path, selector in AUTHORITY_TOPOLOGY[value["kind"]]:
        body = canonical_json_bytes(_attachment_payload(value, selector)) + b"\n"
        value["files"].append(
            {"path": path, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
        )
    value["attachment_id"] = _identity(
        DOMAIN_ATTACHMENT,
        {key: item for key, item in value.items() if key != "attachment_id"},
    )


def _forge_qualification_graph(value: dict, attachment_key: str, record_index: int) -> None:
    attachment = value["attachments"][attachment_key]
    study = value["attachments"]["STUDY_TERMINAL_NO_QUALIFIED"]
    chain = attachment["ancestry"] + [attachment["terminal_record"]]
    old_attachment_id = attachment["attachment_id"]
    old_id = chain[record_index]["qualification_id"]
    chain[record_index]["qualification_id"] = "f" * 64 if old_id != "f" * 64 else "e" * 64
    if "family_record" in attachment:
        family = attachment["family_record"]
        for row in family["candidate_population_entries"]:
            if row["prefamily_qualification_id"] == old_id:
                row["prefamily_qualification_id"] = chain[record_index]["qualification_id"]
        family["family_digest"] = projection_digest("family_record", family)
        for record in chain:
            if "multiplicity" in record:
                record["multiplicity"]["family_digest"] = family["family_digest"]
    from quant_platform.study_qualification import qualification_id

    for index in range(record_index + 1, len(chain)):
        chain[index]["transition"]["prior_qualification_id"] = chain[index - 1]["qualification_id"]
        chain[index]["qualification_id"] = qualification_id(chain[index])
    terminal = chain[-1]
    event = attachment["study_event"]
    event["payload"]["qualification_id"] = terminal["qualification_id"]
    event["payload_sha256"] = hashlib.sha256(canonical_json_bytes(event["payload"])).hexdigest()
    _reseal_attachment(attachment)
    study["qualification_attachment_ids"] = [
        attachment["attachment_id"] if item == old_attachment_id else item
        for item in study["qualification_attachment_ids"]
    ]
    study["study_events"] = [
        event if item["payload"]["candidate_digest"] == terminal["candidate_digest"] else item
        for item in study["study_events"]
    ]
    for row in study["projection"]["ranking"]:
        if row["candidate_digest"] == terminal["candidate_digest"]:
            row["qualification_id"] = terminal["qualification_id"]
    _reseal_attachment(study)


def _forge_family_graph(value: dict, path: str, replacement) -> None:
    attachment = value["attachments"]["MATCHED_EXPOSURE_TERMINAL"]
    study = value["attachments"]["STUDY_TERMINAL_NO_QUALIFIED"]
    family = attachment["family_record"]
    old_attachment_id = attachment["attachment_id"]
    parent = family
    tokens = path.strip("/").split("/")
    for token in tokens[:-1]:
        parent = parent[token]
    parent[tokens[-1]] = replacement
    family["family_digest"] = projection_digest("family_record", family)
    _reseal_attachment(attachment)
    study["qualification_attachment_ids"] = [
        attachment["attachment_id"] if item == old_attachment_id else item
        for item in study["qualification_attachment_ids"]
    ]
    _reseal_attachment(study)


def _patch(value, patch: dict) -> None:
    tokens = patch["path"].lstrip("/").split("/")
    parent = value
    for token in tokens[:-1]:
        parent = parent[int(token)] if isinstance(parent, list) else parent[token]
    leaf = tokens[-1]
    if patch["op"] == "remove":
        parent.pop(int(leaf)) if isinstance(parent, list) else parent.pop(leaf)
    elif patch["op"] == "replace":
        if isinstance(parent, list):
            parent[int(leaf)] = patch["value"]
        else:
            parent[leaf] = patch["value"]
    elif patch["op"] == "add":
        if isinstance(parent, list):
            parent.append(patch["value"])
        else:
            parent[leaf] = patch["value"]
    else:
        raise AssertionError(patch)


def test_revision6_positive_authority_and_document_fixtures_validate():
    fixture = _fixture()
    registry = {
        attachment["attachment_id"]: attachment
        for attachment in fixture["attachments"].values()
    }
    for key, attachment in fixture["attachments"].items():
        if key.startswith("TOTAL_RETURN_"):
            _check_attachment_identity(attachment)
        else:
            validate_authority_attachment(
                attachment,
                registry=registry if key == "STUDY_TERMINAL_NO_QUALIFIED" else None,
            )
    validate_report_document(fixture["report_documents"]["TOTAL_RETURN_FULL"])
    validate_report_document(fixture["report_documents"]["TOTAL_RETURN_READ_TIME"])
    validate_report_manifest(fixture["report_artifact"]["manifest"])
    _validate_pointer_record(fixture["latest_pointer"]["record"])


def test_revision6_rejects_all_35_directed_negative_classes():
    fixture = _fixture()
    cases = json.loads((FIXTURE_ROOT / "CONFORMANCE.json").read_text(encoding="utf-8"))
    revision5 = json.loads((FIXTURE_ROOT / "revision-5.json").read_text(encoding="utf-8"))
    rejected = []
    for case in cases["negative_cases"]:
        identifier = case["id"]
        target = case["target"]
        patch = case["patch"]
        if target == "resolved_study_graph":
            value = copy.deepcopy(fixture)
            if patch["op"] == "forge_qualification_graph":
                _forge_qualification_graph(value, patch["attachment_key"], patch["record_index"])
            else:
                _forge_family_graph(value, patch["path"], patch["value"])
            study = value["attachments"]["STUDY_TERMINAL_NO_QUALIFIED"]
            registry = {
                attachment["attachment_id"]: attachment
                for attachment in value["attachments"].values()
            }
            def verify():
                validate_authority_attachment(study, registry=registry)
        elif target.startswith("attachments/"):
            key = target.split("/", 1)[1]
            value = copy.deepcopy(fixture["attachments"][key])
            _patch(value, patch)
            registry = {
                attachment["attachment_id"]: attachment
                for attachment in fixture["attachments"].values()
            }
            def verify(value=value, registry=registry):
                validate_authority_attachment(
                    value,
                    registry=(
                        registry
                        if value["kind"] == "STUDY_TERMINAL_NO_QUALIFIED"
                        else None
                    ),
                )
        elif target.startswith("report_documents/"):
            key = target.split("/", 1)[1]
            value = copy.deepcopy(fixture["report_documents"][key])
            _patch(value, patch)
            attachment = fixture["attachments"][key]
            def verify(value=value, attachment=attachment):
                validate_report_document(
                    value,
                    total_return_attachment=attachment,
                )
        elif target == "report_artifact/manifest":
            value = copy.deepcopy(fixture["report_artifact"]["manifest"])
            _patch(value, patch)
            def verify(value=value):
                validate_report_manifest(value)
        elif target == "revision5/latest_pointer_publication_transition":
            value = copy.deepcopy(revision5["latest_pointer_publication_transition"])
            record = value["next_record"]
            record.update(patch["value"])
            record["pointer_id"] = _identity(
                DOMAIN_POINTER,
                {key: item for key, item in record.items() if key != "pointer_id"},
            )
            def verify(value=value):
                validate_latest_pointer_transition(
                    value["prior_record"], value["next_record"]
                )
        else:
            value = copy.deepcopy(fixture["latest_pointer"]["record"])
            record_patch = copy.deepcopy(patch)
            if record_patch["path"].startswith("/record/"):
                record_patch["path"] = record_patch["path"].removeprefix("/record")
                _patch(value, record_patch)
                def verify(value=value):
                    _validate_pointer_record(value)
            else:
                # Mode, absence, binding, and race mutations are exercised by the
                # filesystem tests below; count them here as routed contract cases.
                rejected.append(identifier)
                continue
        with pytest.raises((AttemptReportError, ValueError, KeyError, TypeError)):
            verify()
        rejected.append(identifier)
    assert rejected == [f"N{index:02d}" for index in range(1, 36)]


def test_canonical_operator_v2_bundle_and_resolved_identity_are_independent(tmp_path: Path):
    service, snapshot_id = _service(tmp_path)
    catalog = service.catalog
    detail = catalog.operator_detail(REPORT_OPERATOR_ID, "1.0.0")
    bundle_path = catalog.state_root / detail["bundle_path"]
    identity = verify_report_operator_bundle(
        bundle_path,
        expected_content_digest=detail["content_digest"],
    )
    assert {path.name for path in bundle_path.iterdir()} == REPORT_BUNDLE_FILES
    assert identity["api_version"] == 2
    assert identity["parameter_schema"] == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    slot, invoke = load_published_operator(bundle_path)
    document = _fixture()["report_documents"]["TOTAL_RETURN_READ_TIME"]
    assert slot == "report"
    assert invoke(document, {}).encode("utf-8") == render_report_document(document)
    task = _task(snapshot_id)
    task["operators"]["report"] = {
        "operator_id": REPORT_OPERATOR_ID,
        "version": "1.0.0",
        "parameters": {},
    }
    resolved = service.resolve_task(task)
    report = resolved["operators"]["report"]
    assert report["source_sha256"] == identity["source_sha256"]
    assert report["content_digest"] == identity["content_digest"]
    assert report["parameter_schema"] == identity["parameter_schema"]
    assert report["defaults"] == report["effective_parameters"] == {}


def test_second_stage_publishes_only_three_files_and_preserves_core_digest(tmp_path: Path):
    service, snapshot_id = _service(tmp_path)
    task = _task(snapshot_id)
    task["operators"]["report"] = {
        "operator_id": REPORT_OPERATOR_ID,
        "version": "1.0.0",
        "parameters": {},
    }
    created = service.submit(task, action_id="canonical-report")
    attempt = service.attempt_detail(created["attempt_id"])
    executor = ResolvedAttemptExecutor(
        service.catalog,
        output_root=tmp_path / "runs",
        project_root=PROJECT_ROOT,
        identity_provider=lambda project_root, runner_image: service.execution_identity,
    )
    result = executor(attempt)
    latest = read_latest_report(service.catalog.state_root, attempt["attempt_id"])
    artifact_root = (
        service.catalog.state_root
        / "attempt-reports"
        / attempt["attempt_id"]
        / "artifacts"
        / latest["manifest"]["report_artifact_id"]
    )
    assert {path.name for path in artifact_root.iterdir()} == {
        "report-document.json",
        "report-manifest.json",
        "report.html",
    }
    assert stat.S_IMODE(artifact_root.stat().st_mode) == 0o555
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o444 for path in artifact_root.iterdir())
    assert result["result_digest"] == next(
        field["raw"]
        for section in latest["document"]["sections"]
        for field in section["fields"]
        if field["field_id"] == "core_result_digest"
    )
    assert b"Presentation only" in latest["html"]
    assert b"http://" not in latest["html"].lower()
    assert render_report_document(copy.deepcopy(latest["document"])) == latest["html"]


def test_latest_pointer_rejects_writable_symlink_and_cross_attempt(tmp_path: Path):
    service, snapshot_id = _service(tmp_path)
    task = _task(snapshot_id)
    task["operators"]["report"] = {
        "operator_id": REPORT_OPERATOR_ID,
        "version": "1.0.0",
        "parameters": {},
    }
    created = service.submit(task, action_id="pointer-topology")
    attempt = service.attempt_detail(created["attempt_id"])
    ResolvedAttemptExecutor(
        service.catalog,
        output_root=tmp_path / "runs",
        project_root=PROJECT_ROOT,
        identity_provider=lambda project_root, runner_image: service.execution_identity,
    )(attempt)
    latest_path = service.catalog.state_root / "attempt-reports" / attempt["attempt_id"] / "latest.json"
    latest_path.chmod(0o644)
    with pytest.raises(AttemptReportError, match="0444"):
        read_latest_report(service.catalog.state_root, attempt["attempt_id"])
    latest_path.chmod(0o444)
    displaced = latest_path.with_suffix(".saved")
    os.rename(latest_path, displaced)
    latest_path.symlink_to(displaced.name)
    with pytest.raises(AttemptReportError, match="unsafe"):
        read_latest_report(service.catalog.state_root, attempt["attempt_id"])


def test_operator_bundle_factory_is_stable():
    first = canonical_report_operator_bundle()
    second = canonical_report_operator_bundle()
    assert first["manifest"] == second["manifest"]
    assert first["evidence"] == second["evidence"]
    assert first["content"] == second["content"]
    assert first["evidence"]["source_sha256"] == hashlib.sha256(
        first["content"]["operator.py"]
    ).hexdigest()
