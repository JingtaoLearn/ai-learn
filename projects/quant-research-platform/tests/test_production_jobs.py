from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quant_platform.production_bocom import BocomProductionJob
from quant_platform.production_gold import GoldProductionJob
from quant_platform.production_jobs import ProductionJobs


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
