from __future__ import annotations

import hashlib
import json
import os
import shutil
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quant_platform.production_bocom import BocomProductionJob
from quant_platform.production_gold import GoldProductionJob
from quant_platform.production_contract import canonical_json_bytes
from quant_platform.production_jobs import (
    CanonicalJsonBytes,
    FormalComputation,
    ProductionInput,
    ProductionJobError,
    ProductionJobs,
    identity_canonical_bytes,
    staged_package_identity,
)
from quant_platform import production_jobs
from quant_platform.production_worker import ProductionWorker


FIXTURES = Path(__file__).parent / "fixtures" / "production"
SCHEDULED = datetime(2026, 3, 9, 0, 40, tzinfo=UTC)


def seal(directory: Path, payloads: dict[str, bytes]) -> str:
    directory.mkdir()
    for name, payload in payloads.items():
        member = directory / name
        member.write_bytes(payload)
        member.chmod(0o444)
    directory.chmod(0o555)
    return staged_package_identity(payloads)


def formal_computation() -> FormalComputation:
    return FormalComputation(
        job_id="focus-job",
        production_manifest_sha256="1" * 64,
        operation="calibrate",
        authority_sha256="2" * 64,
        files={
            "calibration.json": b"calibration",
            "03-CALIBRATION_CLAIMED.json": b"claimed",
            "04-CALIBRATION_SEALED.json": b"sealed",
        },
        experiment_id="3" * 64,
        attempt_id="4" * 64,
    )


@pytest.mark.parametrize(
    ("job", "raw_name", "expected_name"),
    [
        (
            BocomProductionJob(FIXTURES / "bocom-model-manifest.json"),
            "bocom-yahoo-chart.json",
            "expected-bocom-result.json",
        ),
        (
            GoldProductionJob(FIXTURES / "gold-model-manifest.json"),
            "gold-au9999.tsv",
            "expected-gold-result.json",
        ),
    ],
)
def test_synthetic_jobs_preserve_frozen_action_cost_and_route(job, raw_name, expected_name) -> None:
    raw = (FIXTURES / raw_name).read_bytes()
    expected = json.loads((FIXTURES / expected_name).read_bytes())
    url = "fixture://" + raw_name

    first = job.compute(raw, url, SCHEDULED)
    second = job.compute(raw, url, SCHEDULED)

    assert first == second
    for key, value in expected.items():
        if isinstance(value, dict):
            assert all(first.action[key][nested] == nested_value for nested, nested_value in value.items())
        else:
            assert first.action[key] == value
    assert first.action["automatic_ordering"] is False
    assert first.report_uuid.encode() in first.report_html
    assert b"automatic_ordering=false" in first.report_html
    assert len(first.experiment_id) == len(first.attempt_id) == 64


@pytest.mark.parametrize(
    ("job", "raw_name", "actions", "required"),
    [
        (
            GoldProductionJob(FIXTURES / "gold-model-manifest.json"),
            "gold-au9999.tsv",
            (
                ("WAIT", 0, 0, "买入"),
                ("BUY", 0, 1, "卖出"),
                ("HOLD", 1, 1, "卖出"),
                ("SELL", 1, 0, "买入"),
            ),
            (
                "黄金生产信号",
                "完成收盘",
                "日涨跌",
                "仓位：当前",
                "斜率：上一",
                "执行时点",
                "SGE_AU9999_PROXY",
                "FIXED_SPREAD_ASSUMPTION_5_CNY_PER_G",
                "市场代理评估，不代表招行实际可成交收益",
                "automatic_ordering=false",
            ),
        ),
        (
            BocomProductionJob(FIXTURES / "bocom-model-manifest.json"),
            "bocom-yahoo-chart.json",
            (
                ("WAIT", 0, 0, "买入"),
                ("BUY", 0, 1, "卖出"),
                ("HOLD", 1, 1, "卖出"),
                ("SELL", 1, 0, "买入"),
            ),
            (
                "交通银行生产信号",
                "完成收盘",
                "日涨跌",
                "仓位：当前",
                "斜率：上一",
                "执行时点",
                "假设无公司行动的原始收盘价",
                "automatic_ordering=false",
            ),
        ),
    ],
)
def test_frozen_actions_render_substantive_deterministic_notifications(
    job, raw_name, actions, required
) -> None:
    computation = job.compute(
        (FIXTURES / raw_name).read_bytes(), f"fixture://{raw_name}", SCHEDULED
    )
    for action_name, current_state, target_state, boundary_name in actions:
        action = deepcopy(computation.action)
        action.update(
            {
                "action": action_name,
                "state_before_next": current_state,
                "target_state": target_state,
            }
        )

        first = job.render_notification(action)
        second = job.render_notification(action)
        text = first.decode("utf-8")

        assert first == second
        assert f"动作：{action_name}" in text
        assert f"下一完整收盘{boundary_name}边界" in text
        assert all(marker in text for marker in required)
        assert len(first) > 300
        assert not any(
            secret in text.casefold()
            for secret in ("authorization:", "cookie:", "private key")
        )


def test_registry_injects_provider_and_never_constructs_local_fallback() -> None:
    jobs = ProductionJobs(
        [
            BocomProductionJob(FIXTURES / "bocom-model-manifest.json"),
            GoldProductionJob(FIXTURES / "gold-model-manifest.json"),
        ]
    )

    class Provider:
        def __init__(self):
            self.urls = []

        def get(self, url, *, headers, maximum_bytes):
            self.urls.append(url)
            fixture = "bocom-yahoo-chart.json" if "yahoo" in url else "gold-au9999.tsv"
            return (FIXTURES / fixture).read_bytes()

    provider = Provider()
    bocom_url, _ = jobs.acquire("297c11cad0dc", provider, SCHEDULED)
    gold_url, _ = jobs.acquire("1cd5557264db", provider, SCHEDULED)

    assert bocom_url.startswith("https://query1.finance.yahoo.com/")
    assert gold_url.startswith("https://vip.stock.finance.sina.com.cn/")
    assert provider.urls == [bocom_url, gold_url]
    assert all("flearn" not in url and "localhost" not in url for url in provider.urls)


@pytest.mark.parametrize(
    ("job", "raw_name"),
    [
        (BocomProductionJob(FIXTURES / "bocom-model-manifest.json"), "bocom-yahoo-chart.json"),
        (GoldProductionJob(FIXTURES / "gold-model-manifest.json"), "gold-au9999.tsv"),
    ],
)
def test_dataset_identity_hashes_canonical_bytes_exactly_once(job, raw_name) -> None:
    computation = job.compute((FIXTURES / raw_name).read_bytes(), f"fixture://{raw_name}", SCHEDULED)
    dataset_domain = b"quantresearch-production-dataset/v1\0"
    snapshot = hashlib.sha256(dataset_domain + computation.normalized_bytes).hexdigest()
    experiment_preimage = canonical_json_bytes(
        {
            "job_id": job.job_id,
            "model": job.production_manifest_sha256,
            "snapshot": snapshot,
        }
    )
    assert computation.experiment_id == hashlib.sha256(
        b"quantresearch-production-experiment/v1\0" + experiment_preimage
    ).hexdigest()

    with pytest.raises(TypeError, match="CanonicalJsonBytes"):
        identity_canonical_bytes(dataset_domain, computation.normalized_bytes)  # type: ignore[arg-type]
    with pytest.raises(ProductionJobError, match="not canonical"):
        CanonicalJsonBytes(b'{"value": 1}')


def test_staged_reconstruction_preserves_regular_input_daily_and_formal_bytes(tmp_path) -> None:
    production_input = ProductionInput(
        "provider-get", {"method": "GET", "provider_url": "fixture://input"}, b"raw"
    )
    input_dir = tmp_path / "input"
    input_identity = seal(input_dir, ProductionJobs.input_payloads(production_input))
    assert (
        ProductionJobs.read_input(input_dir, expected_package_identity=input_identity)
        == production_input
    )

    daily = BocomProductionJob(FIXTURES / "bocom-model-manifest.json").compute(
        (FIXTURES / "bocom-yahoo-chart.json").read_bytes(),
        "fixture://bocom-yahoo-chart.json",
        SCHEDULED,
    )
    daily_dir = tmp_path / "daily"
    daily_identity = seal(daily_dir, ProductionJobs.computation_payloads(daily))
    assert (
        ProductionJobs.read_computation(daily_dir, expected_package_identity=daily_identity)
        == daily
    )

    formal = formal_computation()
    formal_dir = tmp_path / "formal"
    formal_identity = seal(formal_dir, ProductionJobs.computation_payloads(formal))
    assert (
        ProductionJobs.read_computation(formal_dir, expected_package_identity=formal_identity)
        == formal
    )


@pytest.mark.parametrize(
    ("stage", "member"),
    [
        ("input", "raw.bin"),
        ("daily", "action.json"),
        ("daily", "notification.txt"),
        ("daily", "normalized.json"),
        ("daily", "raw.bin"),
        ("daily", "report.html"),
        ("formal", "03-CALIBRATION_CLAIMED.json"),
        ("formal", "04-CALIBRATION_SEALED.json"),
        ("formal", "calibration.json"),
    ],
)
def test_actual_producer_external_identity_rejects_post_seal_mutation_and_reseal(
    tmp_path, stage: str, member: str
) -> None:
    target = tmp_path / "work" / "run" / stage
    if stage == "input":
        payloads = ProductionJobs.input_payloads(
            ProductionInput(
                "provider-get", {"method": "GET", "provider_url": "fixture://old"}, b"old"
            )
        )
        read = ProductionJobs.read_input
    elif stage == "daily":
        computation = BocomProductionJob(FIXTURES / "bocom-model-manifest.json").compute(
            (FIXTURES / "bocom-yahoo-chart.json").read_bytes(),
            "fixture://bocom-yahoo-chart.json",
            SCHEDULED,
        )
        payloads = ProductionJobs.computation_payloads(computation)
        read = ProductionJobs.read_computation
    else:
        payloads = ProductionJobs.computation_payloads(formal_computation())
        read = ProductionJobs.read_computation

    worker = object.__new__(ProductionWorker)
    worker.work_root = tmp_path / "work"
    package_identity = worker._write_generation(target, payloads)
    identity_bytes = (target / "identity.json").read_bytes()
    external_identity_path = worker._generation_identity_path(target)
    external_identity_bytes = external_identity_path.read_bytes()

    member_path = target / member
    old_payload = member_path.read_bytes()
    if member == "action.json":
        changed_action = json.loads(old_payload)
        assert changed_action["action"] == "WAIT"
        changed_action["action"] = "HOLD"
        new_payload = canonical_json_bytes(changed_action)
    else:
        new_payload = bytes([old_payload[0] ^ 1]) + old_payload[1:]
    assert len(new_payload) == len(old_payload)
    assert new_payload != old_payload

    target.chmod(0o755)
    member_path.chmod(0o644)
    member_path.write_bytes(new_payload)
    member_path.chmod(0o444)
    target.chmod(0o555)

    assert (target / "identity.json").read_bytes() == identity_bytes
    assert external_identity_path.read_bytes() == external_identity_bytes
    assert worker._read_generation_identity(target) == package_identity
    changed_payloads = dict(payloads)
    changed_payloads[member] = new_payload
    assert worker._write_generation(target, changed_payloads) == package_identity
    with pytest.raises(ProductionJobError, match="package identity mismatch"):
        read(target, expected_package_identity=package_identity)
    target.chmod(0o700)
    shutil.rmtree(target)
    with pytest.raises(ProductionJobError, match="conflicts with generation"):
        worker._write_generation(target, changed_payloads)


@pytest.mark.parametrize(
    ("stage", "member"),
    [
        ("input", "raw.bin"),
        ("daily", "action.json"),
        ("daily", "notification.txt"),
        ("daily", "normalized.json"),
        ("daily", "raw.bin"),
        ("daily", "report.html"),
        ("formal", "03-CALIBRATION_CLAIMED.json"),
        ("formal", "04-CALIBRATION_SEALED.json"),
        ("formal", "calibration.json"),
    ],
)
def test_staged_reconstruction_rejects_member_mutated_before_its_first_open(
    tmp_path, monkeypatch: pytest.MonkeyPatch, stage: str, member: str
) -> None:
    target = tmp_path / stage
    if stage == "input":
        payloads = ProductionJobs.input_payloads(
            ProductionInput(
                "provider-get", {"method": "GET", "provider_url": "fixture://old"}, b"old"
            )
        )
        read = ProductionJobs.read_input
    elif stage == "daily":
        computation = BocomProductionJob(FIXTURES / "bocom-model-manifest.json").compute(
            (FIXTURES / "bocom-yahoo-chart.json").read_bytes(),
            "fixture://bocom-yahoo-chart.json",
            SCHEDULED,
        )
        payloads = ProductionJobs.computation_payloads(computation)
        read = ProductionJobs.read_computation
    else:
        payloads = ProductionJobs.computation_payloads(formal_computation())
        read = ProductionJobs.read_computation
    package_identity = seal(target, payloads)
    original_open = os.open
    identity_opened = False
    mutated = False

    def mutate_member_before_open(path, flags, *args, **kwargs):
        nonlocal identity_opened, mutated
        name = os.fspath(path)
        if name == "identity.json":
            opened = original_open(path, flags, *args, **kwargs)
            identity_opened = True
            return opened
        if identity_opened and name == member and not mutated:
            mutated = True
            member_path = target / member
            member_path.chmod(0o644)
            member_path.write_bytes(b'{"changed":true}' if member == "action.json" else b"changed")
            member_path.chmod(0o444)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(production_jobs.os, "open", mutate_member_before_open)
    with pytest.raises(ProductionJobError, match="changed during read"):
        read(target, expected_package_identity=package_identity)
    assert identity_opened is True
    assert mutated is True


@pytest.mark.parametrize(
    ("stage", "member"),
    [
        ("input", "raw.bin"),
        ("daily", "action.json"),
        ("daily", "notification.txt"),
        ("daily", "normalized.json"),
        ("daily", "raw.bin"),
        ("daily", "report.html"),
        ("formal", "03-CALIBRATION_CLAIMED.json"),
        ("formal", "04-CALIBRATION_SEALED.json"),
        ("formal", "calibration.json"),
    ],
)
def test_staged_reconstruction_rejects_member_mutated_during_baseline_acquisition(
    tmp_path, monkeypatch: pytest.MonkeyPatch, stage: str, member: str
) -> None:
    target = tmp_path / stage
    if stage == "input":
        payloads = ProductionJobs.input_payloads(
            ProductionInput(
                "provider-get", {"method": "GET", "provider_url": "fixture://old"}, b"old"
            )
        )
        read = ProductionJobs.read_input
    elif stage == "daily":
        computation = BocomProductionJob(FIXTURES / "bocom-model-manifest.json").compute(
            (FIXTURES / "bocom-yahoo-chart.json").read_bytes(),
            "fixture://bocom-yahoo-chart.json",
            SCHEDULED,
        )
        payloads = ProductionJobs.computation_payloads(computation)
        read = ProductionJobs.read_computation
    else:
        payloads = ProductionJobs.computation_payloads(formal_computation())
        read = ProductionJobs.read_computation
    package_identity = seal(target, payloads)
    original_stat = os.stat
    identity_baselined = False
    mutated = False

    def mutate_member_before_baseline(path, *args, **kwargs):
        nonlocal identity_baselined, mutated
        name = os.fspath(path)
        if name == "identity.json":
            result = original_stat(path, *args, **kwargs)
            identity_baselined = True
            return result
        if identity_baselined and name == member and not mutated:
            mutated = True
            member_path = target / member
            old_payload = member_path.read_bytes()
            if member == "action.json":
                changed_action = json.loads(old_payload)
                assert changed_action["action"] == "WAIT"
                changed_action["action"] = "HOLD"
                new_payload = canonical_json_bytes(changed_action)
            else:
                new_payload = bytes([old_payload[0] ^ 1]) + old_payload[1:]
            assert len(new_payload) == len(old_payload)
            assert new_payload != old_payload
            member_path.chmod(0o644)
            member_path.write_bytes(new_payload)
            member_path.chmod(0o444)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(production_jobs.os, "stat", mutate_member_before_baseline)
    with pytest.raises(ProductionJobError, match="changed during read"):
        read(target, expected_package_identity=package_identity)
    assert identity_baselined is True
    assert mutated is True


@pytest.mark.parametrize("stage", ["input", "formal"])
def test_staged_reconstruction_rejects_symlinked_members(tmp_path, stage) -> None:
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside-bytes")
    target = tmp_path / stage
    if stage == "input":
        payloads = ProductionJobs.input_payloads(
            ProductionInput(
                "provider-get", {"method": "GET", "provider_url": "fixture://input"}, b"raw"
            )
        )
        package_identity = seal(target, payloads)
        target.chmod(0o755)
        (target / "raw.bin").unlink()
        (target / "raw.bin").symlink_to(outside)
        target.chmod(0o555)
        read = ProductionJobs.read_input
    else:
        payloads = ProductionJobs.computation_payloads(formal_computation())
        package_identity = seal(target, payloads)
        target.chmod(0o755)
        for name in formal_computation().files:
            (target / name).unlink()
            (target / name).symlink_to(outside)
        target.chmod(0o555)
        read = ProductionJobs.read_computation

    with pytest.raises(ProductionJobError, match="unsafe"):
        read(target, expected_package_identity=package_identity)


def test_staged_reconstruction_rejects_unsafe_formal_name_before_lookup(tmp_path) -> None:
    target = tmp_path / "formal"
    payloads = ProductionJobs.computation_payloads(formal_computation())
    identity = json.loads(payloads["identity.json"])
    identity["files"] = ["../outside", *identity["files"][1:]]
    payloads["identity.json"] = canonical_json_bytes(identity)
    package_identity = seal(target, payloads)

    with pytest.raises(ProductionJobError, match="member set"):
        ProductionJobs.read_computation(
            target, expected_package_identity=package_identity
        )


def test_staged_reconstruction_rejects_hard_link_alias(tmp_path) -> None:
    target = tmp_path / "input"
    target.mkdir()
    identity = ProductionJobs.input_payloads(
        ProductionInput(
            "provider-get", {"method": "GET", "provider_url": "fixture://input"}, b"raw"
        )
    )["identity.json"]
    (target / "identity.json").write_bytes(identity)
    (target / "identity.json").chmod(0o444)
    outside = tmp_path / "outside"
    outside.write_bytes(b"raw")
    os.link(outside, target / "raw.bin")
    (target / "raw.bin").chmod(0o444)
    target.chmod(0o555)

    with pytest.raises(ProductionJobError, match="unsafe"):
        ProductionJobs.read_input(
            target,
            expected_package_identity=staged_package_identity(
                {"identity.json": identity, "raw.bin": b"raw"}
            ),
        )


@pytest.mark.parametrize("member_kind", ["fifo", "writable"])
def test_staged_reconstruction_rejects_non_regular_or_writable_member(
    tmp_path, member_kind
) -> None:
    target = tmp_path / "input"
    payloads = ProductionJobs.input_payloads(
        ProductionInput(
            "provider-get", {"method": "GET", "provider_url": "fixture://input"}, b"raw"
        )
    )
    package_identity = seal(target, payloads)
    target.chmod(0o755)
    raw = target / "raw.bin"
    if member_kind == "fifo":
        raw.unlink()
        os.mkfifo(raw, 0o444)
    else:
        raw.chmod(0o644)
    target.chmod(0o555)

    with pytest.raises(ProductionJobError, match="unsafe"):
        ProductionJobs.read_input(target, expected_package_identity=package_identity)


def test_staged_reconstruction_rejects_bytes_mutated_between_reads(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "input"
    payloads = ProductionJobs.input_payloads(
        ProductionInput(
            "provider-get", {"method": "GET", "provider_url": "fixture://input"}, b"raw"
        )
    )
    package_identity = seal(target, payloads)
    raw = target / "raw.bin"
    raw_inode = raw.stat().st_ino
    original_read = os.read
    mutated = False

    def mutate_after_first_read(fd, count):
        nonlocal mutated
        chunk = original_read(fd, count)
        if not chunk and not mutated and os.fstat(fd).st_ino == raw_inode:
            mutated = True
            raw.chmod(0o644)
            raw.write_bytes(b"new")
            raw.chmod(0o444)
        return chunk

    monkeypatch.setattr(production_jobs.os, "read", mutate_after_first_read)
    with pytest.raises(ProductionJobError, match="changed during read"):
        ProductionJobs.read_input(target, expected_package_identity=package_identity)
    assert mutated is True


def test_staged_input_rejects_identity_mutated_while_raw_is_read(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "input"
    payloads = ProductionJobs.input_payloads(
        ProductionInput(
            "provider-get", {"method": "GET", "provider_url": "fixture://input"}, b"raw"
        )
    )
    package_identity = seal(target, payloads)
    identity = target / "identity.json"
    raw_inode = (target / "raw.bin").stat().st_ino
    original_read = os.read
    mutated = False

    def mutate_identity_when_raw_is_read(fd, count):
        nonlocal mutated
        chunk = original_read(fd, count)
        if not mutated and os.fstat(fd).st_ino == raw_inode:
            mutated = True
            identity.chmod(0o644)
            identity.write_bytes(
                canonical_json_bytes(
                    {
                        "kind": "provider-get",
                        "method": "GET",
                        "provider_url": "fixture://changed",
                    }
                )
            )
            identity.chmod(0o444)
        return chunk

    monkeypatch.setattr(production_jobs.os, "read", mutate_identity_when_raw_is_read)
    with pytest.raises(ProductionJobError, match="changed during read"):
        ProductionJobs.read_input(target, expected_package_identity=package_identity)
    assert mutated is True


def test_staged_computation_rejects_member_mutated_while_another_is_read(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "formal"
    package_identity = seal(
        target, ProductionJobs.computation_payloads(formal_computation())
    )
    claimed = target / "03-CALIBRATION_CLAIMED.json"
    sealed_inode = (target / "04-CALIBRATION_SEALED.json").stat().st_ino
    original_read = os.read
    mutated = False

    def mutate_claimed_when_sealed_is_read(fd, count):
        nonlocal mutated
        chunk = original_read(fd, count)
        if not mutated and os.fstat(fd).st_ino == sealed_inode:
            mutated = True
            claimed.chmod(0o644)
            claimed.write_bytes(b"changed-claimed")
            claimed.chmod(0o444)
        return chunk

    monkeypatch.setattr(production_jobs.os, "read", mutate_claimed_when_sealed_is_read)
    with pytest.raises(ProductionJobError, match="changed during read"):
        ProductionJobs.read_computation(
            target, expected_package_identity=package_identity
        )
    assert mutated is True
