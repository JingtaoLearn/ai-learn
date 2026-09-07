import copy
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

import quant_platform.attempt_report as attempt_report_module
import quant_platform.resolved_runner as resolved_runner_module
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
    capture_total_return_read_time,
    publish_report_artifact,
    read_report_artifact,
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


def _document_fields(document: dict) -> dict[str, dict]:
    return {
        field["field_id"]: field
        for section in document["sections"]
        for field in section["fields"]
    }


def _reseal_document(document: dict) -> None:
    document["document_id"] = _identity(
        attempt_report_module.DOMAIN_DOCUMENT,
        {key: value for key, value in document.items() if key != "document_id"},
    )


def _embed_attachment(document: dict, field_id: str, attachment: dict) -> None:
    field = _document_fields(document)[field_id]
    field.update(
        availability="AVAILABLE",
        reason=None,
        raw=copy.deepcopy(attachment),
        display="Verified sealed attachment",
    )
    _reseal_document(document)


def _publish_fixture_report(
    state_root: Path,
    *,
    document: dict | None = None,
    fault: str | None = None,
) -> dict:
    report_document = copy.deepcopy(
        document or _fixture()["report_documents"]["TOTAL_RETURN_READ_TIME"]
    )
    fields = _document_fields(report_document)
    descriptor = {
        "attempt_id": fields["attempt_id"]["raw"],
        "bundle_id": fields["bundle_id"]["raw"],
    }
    return publish_report_artifact(
        state_root,
        descriptor,
        report_document,
        render_report_document(report_document),
        fault=fault,
    )


def _valid_total_return_attachment() -> dict:
    return capture_total_return_read_time(
        attempt_id="a" * 64,
        experiment_id="a" * 64,
        bundle_id="a" * 64,
        result_digest="a" * 64,
        metric_document_digest="a" * 64,
        source_issuer="STRATEGY_RUNNER",
        source_total_return_claim="PRICE_RETURN_ONLY",
        coverage_state="UNKNOWN_MISSING",
    )


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


def _install_cross_attempt_artifact(
    state_root: Path,
    path_attempt_id: str,
) -> dict:
    document = copy.deepcopy(
        _fixture()["report_documents"]["TOTAL_RETURN_READ_TIME"]
    )
    fields = _document_fields(document)
    document_attempt_id = (
        "b" * 64 if path_attempt_id != "b" * 64 else "c" * 64
    )
    for field_id in ("attempt_id", "experiment_id", "bundle_id", "core_result_digest"):
        fields[field_id]["raw"] = document_attempt_id
        fields[field_id]["display"] = document_attempt_id
    attachment = _valid_total_return_attachment()
    for binding in ("attempt_id", "experiment_id", "bundle_id", "result_digest"):
        attachment[binding] = document_attempt_id
    _reseal_attachment(attachment)
    _embed_attachment(document, "total_return_attachment", attachment)
    _reseal_document(document)
    validate_report_document(document)
    document_bytes = canonical_json_bytes(document) + b"\n"
    report_html = render_report_document(document)
    manifest = attempt_report_module._build_report_manifest(
        path_attempt_id,
        path_attempt_id,
        fields["operator_content_digest"]["raw"],
        document_bytes,
        report_html,
        document["document_id"],
    )
    artifact_root = (
        state_root
        / "attempt-reports"
        / path_attempt_id
        / "artifacts"
        / manifest["report_artifact_id"]
    )
    artifact_root.mkdir(parents=True)
    for name, payload in {
        "report-document.json": document_bytes,
        "report-manifest.json": canonical_json_bytes(manifest) + b"\n",
        "report.html": report_html,
    }.items():
        path = artifact_root / name
        path.write_bytes(payload)
        path.chmod(0o444)
    artifact_root.chmod(0o555)
    pointer_core = {
        "schema_id": attempt_report_module.LATEST_POINTER_SCHEMA_ID,
        "schema_version": 1,
        "attempt_id": path_attempt_id,
        "report_artifact_id": manifest["report_artifact_id"],
        "report_manifest_sha256": hashlib.sha256(
            canonical_json_bytes(manifest) + b"\n"
        ).hexdigest(),
        "report_document_sha256": manifest["report_document_sha256"],
        "operator_content_digest": manifest["operator_content_digest"],
        "sequence": 1,
        "previous_pointer_id": None,
    }
    pointer = pointer_core | {
        "pointer_id": _identity(DOMAIN_POINTER, pointer_core)
    }
    latest = artifact_root.parents[1] / "latest.json"
    latest.unlink(missing_ok=True)
    latest.write_bytes(canonical_json_bytes(pointer) + b"\n")
    latest.chmod(0o444)
    return manifest


def _complete_authority_document() -> tuple[dict, dict[str, dict]]:
    fixture = _fixture()
    registry = {
        attachment["attachment_id"]: copy.deepcopy(attachment)
        for key, attachment in fixture["attachments"].items()
        if not key.startswith("TOTAL_RETURN_")
    }
    document = copy.deepcopy(
        fixture["report_documents"]["TOTAL_RETURN_READ_TIME"]
    )
    total = _valid_total_return_attachment()
    registry[total["attachment_id"]] = total
    matched = registry[
        fixture["attachments"]["MATCHED_EXPOSURE_TERMINAL"]["attachment_id"]
    ]
    study = registry[
        fixture["attachments"]["STUDY_TERMINAL_NO_QUALIFIED"]["attachment_id"]
    ]
    _embed_attachment(document, "total_return_attachment", total)
    _embed_attachment(document, "matched_exposure_attachment", matched)
    _embed_attachment(document, "study_terminal_attachment", study)
    fields = _document_fields(document)
    source_name = "projection"
    source = total[source_name]
    fields["total_return_status"].update(
        availability="AVAILABLE",
        reason=None,
        raw=source["claim_state"],
        display=source["claim_state"],
        source_ref={
            "artifact": "total-return-attachment",
            "pointer": f"/{source_name}/claim_state",
        },
    )
    fields["promotion_ready"].update(
        availability="AVAILABLE",
        reason=None,
        raw=source["ranking"]["eligible_for_promotion"],
        display=str(source["ranking"]["eligible_for_promotion"]).lower(),
        source_ref={
            "artifact": "total-return-attachment",
            "pointer": f"/{source_name}/ranking/eligible_for_promotion",
        },
    )
    fields["matched_exposure_status"].update(
        availability="AVAILABLE",
        reason=None,
        raw=matched["terminal_record"]["state"],
        display=matched["terminal_record"]["state"],
    )
    fields["ranking_status"].update(
        availability="AVAILABLE",
        reason=None,
        raw=matched["terminal_record"]["ranking_status"],
        display=matched["terminal_record"]["ranking_status"],
    )
    _reseal_document(document)
    validate_report_document(document, attachment_registry=registry)
    return document, registry


def _authority_snapshot(registry: dict[str, dict]) -> dict[str, tuple[bytes, str]]:
    return {
        attachment_id: (
            payload := canonical_json_bytes(attachment),
            hashlib.sha256(payload).hexdigest(),
        )
        for attachment_id, attachment in registry.items()
    }


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


@pytest.mark.parametrize(
    "binding",
    ["attempt_id", "experiment_id", "bundle_id", "result_digest"],
)
def test_report_document_rejects_self_consistent_cross_bound_attachment(binding: str):
    fixture = _fixture()
    document = copy.deepcopy(fixture["report_documents"]["TOTAL_RETURN_READ_TIME"])
    attachment = _valid_total_return_attachment()
    attachment[binding] = "b" * 64
    _reseal_attachment(attachment)
    _embed_attachment(document, "total_return_attachment", attachment)

    with pytest.raises(AttemptReportError, match=rf"{binding} binding mismatch"):
        validate_report_document(document)


def test_report_document_rejects_cross_metric_document_and_study_candidate_joins():
    fixture = _fixture()
    document = copy.deepcopy(fixture["report_documents"]["TOTAL_RETURN_READ_TIME"])
    total = _valid_total_return_attachment()
    matched = copy.deepcopy(fixture["attachments"]["MATCHED_EXPOSURE_TERMINAL"])
    study = copy.deepcopy(fixture["attachments"]["STUDY_TERMINAL_NO_QUALIFIED"])
    registry = {
        attachment["attachment_id"]: attachment
        for attachment in fixture["attachments"].values()
    }
    _embed_attachment(document, "total_return_attachment", total)
    _embed_attachment(document, "matched_exposure_attachment", matched)
    fields = _document_fields(document)
    fields["matched_exposure_status"]["raw"] = matched["terminal_record"]["state"]
    fields["matched_exposure_status"]["display"] = matched["terminal_record"]["state"]
    fields["matched_exposure_status"]["availability"] = "AVAILABLE"
    fields["matched_exposure_status"]["reason"] = None
    fields["ranking_status"]["raw"] = matched["terminal_record"]["ranking_status"]
    fields["ranking_status"]["display"] = matched["terminal_record"]["ranking_status"]
    fields["ranking_status"]["availability"] = "AVAILABLE"
    fields["ranking_status"]["reason"] = None
    _reseal_document(document)

    cross_metric = copy.deepcopy(total)
    cross_metric["metric_document_digest"] = "b" * 64
    _reseal_attachment(cross_metric)
    _embed_attachment(document, "total_return_attachment", cross_metric)
    with pytest.raises(AttemptReportError, match="MetricDocument binding mismatch"):
        validate_report_document(document)

    cross_candidate = copy.deepcopy(matched)
    cross_candidate["metric_document_digest"] = "c" * 64
    _reseal_attachment(cross_candidate)
    cross_study = copy.deepcopy(study)
    cross_study["qualification_attachment_ids"] = [
        cross_candidate["attachment_id"]
        if item == matched["attachment_id"]
        else item
        for item in cross_study["qualification_attachment_ids"]
    ]
    _reseal_attachment(cross_study)
    registry[cross_candidate["attachment_id"]] = cross_candidate
    registry[cross_study["attachment_id"]] = cross_study
    document = copy.deepcopy(fixture["report_documents"]["TOTAL_RETURN_READ_TIME"])
    _embed_attachment(document, "matched_exposure_attachment", matched)
    fields = _document_fields(document)
    fields["matched_exposure_status"].update(
        availability="AVAILABLE",
        reason=None,
        raw=matched["terminal_record"]["state"],
        display=matched["terminal_record"]["state"],
    )
    fields["ranking_status"].update(
        availability="AVAILABLE",
        reason=None,
        raw=matched["terminal_record"]["ranking_status"],
        display=matched["terminal_record"]["ranking_status"],
    )
    _embed_attachment(document, "study_terminal_attachment", cross_study)
    with pytest.raises(AttemptReportError, match="Study/candidate attachment binding mismatch"):
        validate_report_document(document, attachment_registry=registry)


def test_revision6_rejects_all_35_directed_negative_classes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
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
                case_root = tmp_path / identifier
                published = _publish_fixture_report(case_root)
                attempt_id = "a" * 64
                latest = case_root / "attempt-reports" / attempt_id / "latest.json"
                if identifier == "N19":
                    latest.chmod(0o644)
                    def verify(case_root=case_root, attempt_id=attempt_id):
                        read_latest_report(case_root, attempt_id)
                elif identifier == "N21":
                    artifact = (
                        case_root
                        / "attempt-reports"
                        / attempt_id
                        / "artifacts"
                        / published["artifact_id"]
                    )
                    artifact.chmod(0o700)
                    os.rename(
                        artifact / "report-manifest.json",
                        case_root / "missing-report-manifest.json",
                    )
                    artifact.chmod(0o555)
                    def verify(case_root=case_root, attempt_id=attempt_id):
                        read_latest_report(case_root, attempt_id)
                else:
                    payload = latest.read_bytes()
                    original_stat = attempt_report_module.os.stat
                    observations = 0

                    def race_stat(path, *args, **kwargs):
                        nonlocal observations
                        result = original_stat(path, *args, **kwargs)
                        if Path(path) == latest:
                            observations += 1
                            if observations == 2:
                                replacement = latest.with_suffix(".race")
                                replacement.write_bytes(payload)
                                replacement.chmod(0o444)
                                os.replace(replacement, latest)
                                return original_stat(path, *args, **kwargs)
                        return result

                    def verify(case_root=case_root, attempt_id=attempt_id):
                        with monkeypatch.context() as scoped:
                            scoped.setattr(
                                attempt_report_module.os,
                                "stat",
                                race_stat,
                            )
                            read_latest_report(case_root, attempt_id)
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


def test_second_stage_publishes_only_three_files_and_preserves_all_source_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
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
    observed: dict[str, object] = {}
    publish = resolved_runner_module.publish_attempt_report

    def capture_semantic_invariance(state_root, run_dir, audit_path, operator, **kwargs):
        run_root = Path(run_dir)
        audit_file = Path(audit_path)
        source_before = {
            path.relative_to(run_root).as_posix(): (
                path.read_bytes(),
                stat.S_IMODE(path.stat().st_mode),
                path.stat().st_nlink,
            )
            for path in run_root.iterdir()
        }
        audit_before = audit_file.read_bytes()
        state_before = {
            path.relative_to(state_root).as_posix(): path.read_bytes()
            for path in Path(state_root).rglob("*")
            if path.is_file() and "attempt-reports" not in path.parts
        }
        published = publish(
            state_root,
            run_dir,
            audit_path,
            operator,
            **kwargs,
        )
        source_after = {
            path.relative_to(run_root).as_posix(): (
                path.read_bytes(),
                stat.S_IMODE(path.stat().st_mode),
                path.stat().st_nlink,
            )
            for path in run_root.iterdir()
        }
        state_after = {
            path.relative_to(state_root).as_posix(): path.read_bytes()
            for path in Path(state_root).rglob("*")
            if path.is_file() and "attempt-reports" not in path.parts
        }
        assert source_after == source_before
        assert audit_file.read_bytes() == audit_before
        assert state_after == state_before
        observed["source"] = source_before
        return published

    monkeypatch.setattr(
        resolved_runner_module,
        "publish_attempt_report",
        capture_semantic_invariance,
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
    source = observed["source"]
    assert isinstance(source, dict)
    assert {
        "config.json",
        "run_manifest.json",
        "daily_replay.csv",
        "events.csv",
        "trades.csv",
        "metrics.json",
        "cost_breakdown.json",
        "report.html",
    }.issubset(source)


def test_authority_bytes_and_digests_are_invariant_across_publication_reload_and_invocation(
    tmp_path: Path,
):
    document, registry = _complete_authority_document()
    before = _authority_snapshot(registry)
    document_before = canonical_json_bytes(document)
    service, _ = _service(tmp_path / "catalog")
    detail = service.catalog.operator_detail(REPORT_OPERATOR_ID, "1.0.0")
    bundle_path = service.catalog.state_root / detail["bundle_path"]
    slot, invoke = load_published_operator(bundle_path)
    assert slot == "report"
    report_html = invoke(
        copy.deepcopy(document),
        {},
        attachment_registry=registry,
    ).encode("utf-8")
    assert canonical_json_bytes(document) == document_before
    assert report_html == render_report_document(
        document,
        attachment_registry=registry,
    )
    fields = _document_fields(document)
    state_root = tmp_path / "publication"
    published = publish_report_artifact(
        state_root,
        {
            "attempt_id": fields["attempt_id"]["raw"],
            "bundle_id": fields["bundle_id"]["raw"],
        },
        document,
        report_html,
        attachment_registry=registry,
    )
    reloaded = read_report_artifact(
        state_root,
        fields["attempt_id"]["raw"],
        published["artifact_id"],
        attachment_registry=registry,
    )
    latest = read_latest_report(
        state_root,
        fields["attempt_id"]["raw"],
        attachment_registry=registry,
    )
    reloaded_before = canonical_json_bytes(reloaded["document"])
    assert (
        invoke(
            reloaded["document"],
            {},
            attachment_registry=registry,
        ).encode("utf-8")
        == reloaded["html"]
    )
    assert canonical_json_bytes(reloaded["document"]) == reloaded_before
    assert latest["document"] == reloaded["document"]
    assert _authority_snapshot(registry) == before
    assert canonical_json_bytes(document) == document_before
    reloaded_fields = _document_fields(reloaded["document"])
    for field_id in (
        "total_return_attachment",
        "matched_exposure_attachment",
        "study_terminal_attachment",
    ):
        attachment = reloaded_fields[field_id]["raw"]
        assert canonical_json_bytes(attachment) == before[attachment["attachment_id"]][0]


def test_latest_pointer_rejects_writable_symlink_and_hardlink(tmp_path: Path):
    result = _publish_fixture_report(tmp_path)
    attempt_id = "a" * 64
    latest_path = tmp_path / "attempt-reports" / attempt_id / "latest.json"
    latest_path.chmod(0o644)
    with pytest.raises(AttemptReportError, match="0444"):
        read_latest_report(tmp_path, attempt_id)
    latest_path.chmod(0o444)
    displaced = latest_path.with_suffix(".saved")
    os.rename(latest_path, displaced)
    latest_path.symlink_to(displaced.name)
    with pytest.raises(AttemptReportError, match="unsafe"):
        read_latest_report(tmp_path, attempt_id)
    latest_path.unlink()
    os.rename(displaced, latest_path)
    outside = tmp_path / "pointer-hardlink.json"
    os.link(latest_path, outside)
    with pytest.raises(AttemptReportError, match="unsafe"):
        read_latest_report(tmp_path, attempt_id)
    assert result["sequence"] == 1


@pytest.mark.parametrize(
    "mutation",
    ["directory-mode", "file-mode", "extra", "missing", "symlink", "hardlink"],
)
def test_report_artifact_rejects_unsafe_topology(tmp_path: Path, mutation: str):
    published = _publish_fixture_report(tmp_path)
    attempt_id = "a" * 64
    artifact_id = published["artifact_id"]
    root = tmp_path / "attempt-reports" / attempt_id / "artifacts" / artifact_id
    document = root / "report-document.json"
    if mutation == "directory-mode":
        root.chmod(0o755)
    elif mutation == "file-mode":
        document.chmod(0o644)
    elif mutation == "extra":
        root.chmod(0o755)
        (root / "extra").write_bytes(b"extra")
        (root / "extra").chmod(0o444)
        root.chmod(0o555)
    elif mutation == "missing":
        root.chmod(0o755)
        os.rename(document, tmp_path / "missing-document.json")
        root.chmod(0o555)
    elif mutation == "symlink":
        root.chmod(0o755)
        os.rename(document, tmp_path / "real-document.json")
        document.symlink_to(tmp_path / "real-document.json")
        root.chmod(0o555)
    else:
        os.link(document, tmp_path / "document-hardlink.json")

    with pytest.raises(AttemptReportError):
        read_report_artifact(tmp_path, attempt_id, artifact_id)


def test_report_paths_reject_traversal_cross_attempt_and_stale_pointer(tmp_path: Path):
    published = _publish_fixture_report(tmp_path)
    attempt_id = "a" * 64
    artifact_id = published["artifact_id"]
    with pytest.raises(AttemptReportError, match="SHA-256"):
        read_report_artifact(tmp_path, "../" + attempt_id, artifact_id)
    with pytest.raises(AttemptReportError):
        read_report_artifact(tmp_path, "b" * 64, artifact_id)
    artifact_root = tmp_path / "attempt-reports" / attempt_id / "artifacts" / artifact_id
    artifact_root.chmod(0o700)
    os.rename(
        artifact_root / "report-manifest.json",
        tmp_path / "stale-report-manifest.json",
    )
    artifact_root.chmod(0o555)
    with pytest.raises(AttemptReportError, match="unexpected topology"):
        read_latest_report(tmp_path, attempt_id)


def test_report_artifact_and_latest_reject_resealed_cross_attempt_document(
    tmp_path: Path,
):
    attempt_id = "a" * 64
    manifest = _install_cross_attempt_artifact(tmp_path, attempt_id)
    with pytest.raises(AttemptReportError, match="artifact bindings"):
        read_report_artifact(
            tmp_path,
            attempt_id,
            manifest["report_artifact_id"],
        )
    with pytest.raises(AttemptReportError, match="artifact bindings"):
        read_latest_report(tmp_path, attempt_id)


def test_latest_pointer_reader_detects_inode_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _publish_fixture_report(tmp_path)
    attempt_id = "a" * 64
    latest = tmp_path / "attempt-reports" / attempt_id / "latest.json"
    payload = latest.read_bytes()
    original_stat = attempt_report_module.os.stat
    observations = 0

    def race_stat(path, *args, **kwargs):
        nonlocal observations
        result = original_stat(path, *args, **kwargs)
        if Path(path) == latest:
            observations += 1
            if observations == 2:
                replacement = latest.with_suffix(".race")
                replacement.write_bytes(payload)
                replacement.chmod(0o444)
                os.replace(replacement, latest)
                return original_stat(path, *args, **kwargs)
        return result

    monkeypatch.setattr(attempt_report_module.os, "stat", race_stat)
    with pytest.raises(AttemptReportError, match="changed while reading"):
        read_latest_report(tmp_path, attempt_id)


@pytest.mark.parametrize("fault", ["before_pointer", "during_pointer"])
def test_pointer_faults_preserve_prior_pointer_and_sealed_artifacts(
    tmp_path: Path, fault: str
):
    first = _publish_fixture_report(tmp_path)
    attempt_id = "a" * 64
    latest = tmp_path / "attempt-reports" / attempt_id / "latest.json"
    prior_bytes = latest.read_bytes()
    prior_artifacts = {
        path.name
        for path in (tmp_path / "attempt-reports" / attempt_id / "artifacts").iterdir()
    }
    replacement = copy.deepcopy(
        _fixture()["report_documents"]["TOTAL_RETURN_READ_TIME"]
    )
    _document_fields(replacement)["purpose"]["display"] = "Presentation only revision"
    _reseal_document(replacement)

    with pytest.raises(AttemptReportError, match="injected failure"):
        _publish_fixture_report(tmp_path, document=replacement, fault=fault)

    assert latest.read_bytes() == prior_bytes
    current = read_latest_report(tmp_path, attempt_id)
    assert current["pointer"]["pointer_id"] == first["pointer_id"]
    assert prior_artifacts.issubset(
        {
            path.name
            for path in (
                tmp_path / "attempt-reports" / attempt_id / "artifacts"
            ).iterdir()
        }
    )


def test_pointer_first_and_replacement_publication_transition(tmp_path: Path):
    first = _publish_fixture_report(tmp_path)
    replacement = copy.deepcopy(
        _fixture()["report_documents"]["TOTAL_RETURN_READ_TIME"]
    )
    _document_fields(replacement)["purpose"]["display"] = "Presentation only revision"
    _reseal_document(replacement)
    second = _publish_fixture_report(tmp_path, document=replacement)
    current = read_latest_report(tmp_path, "a" * 64)["pointer"]

    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert current["previous_pointer_id"] == first["pointer_id"]
    assert current["pointer_id"] == second["pointer_id"]


def test_operator_bundle_factory_is_stable():
    first = canonical_report_operator_bundle()
    second = canonical_report_operator_bundle()
    assert first["manifest"] == second["manifest"]
    assert first["evidence"] == second["evidence"]
    assert first["content"] == second["content"]
    assert first["evidence"]["source_sha256"] == hashlib.sha256(
        first["content"]["operator.py"]
    ).hexdigest()
