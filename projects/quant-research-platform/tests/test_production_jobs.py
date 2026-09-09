from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quant_platform.production_bocom import BocomProductionJob
from quant_platform.production_gold import GoldProductionJob
from quant_platform.production_contract import canonical_json_bytes
from quant_platform.production_jobs import (
    CanonicalJsonBytes,
    ProductionJobError,
    ProductionJobs,
    identity_canonical_bytes,
)


FIXTURES = Path(__file__).parent / "fixtures" / "production"
SCHEDULED = datetime(2026, 3, 9, 0, 40, tzinfo=UTC)


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
