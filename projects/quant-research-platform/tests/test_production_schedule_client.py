from __future__ import annotations

import ast
import hashlib
import html
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

import quant_platform.production_schedule_client as schedule_client
from quant_platform.production_client import (
    REPORT_EVIDENCE_FILE_NAMES,
    ClientTLS,
    ProductionClientError,
    ProductionClientUnknown,
)
from quant_platform.production_schedule_client import (
    DELIVERY,
    JOBS,
    _parser,
    publish_report,
    run_job,
    scheduled_fire_for,
)


PROJECT = Path(__file__).parents[1]
PACKAGE = PROJECT / "src" / "quant_platform"
SCRIPTS = PROJECT / "scripts"


def jobs_file(tmp_path: Path, job_id: str) -> Path:
    job = JOBS[job_id]
    path = tmp_path / "jobs.json"
    path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": job.job_id,
                        "name": job.name,
                        "script": job.script,
                        "prompt": job.audit_prompt,
                        "deliver": DELIVERY,
                        "no_agent": True,
                        "enabled": True,
                        "schedule": {"kind": "cron", "expr": job.schedule},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, _transport):
        self.requests = []
        self.job = None
        self.__class__.instances.append(self)

    def submit_and_wait(self, request):
        self.requests.append(request)
        self.job = JOBS[request.job_id]
        payload = b"verified production notification"
        action = fake_action(self.job)
        self.evidence = fake_report_evidence(self.job)
        self.report_document = self.evidence["report-document.json"]
        files = {
            name: {"sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
            for name, value in self.evidence.items()
        }
        files["notification.txt"] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
        return {
            "schema": "quantresearch-production-result/v1",
            "job_id": self.job.job_id,
            "model_id": self.job.model_id,
            "production_manifest_sha256": self.job.production_manifest_sha256,
            "report_filename": self.job.report_filename,
            "report_operator": {
                "api_version": 2,
                "content_digest": "275a68f011fe9b45fadc8e1960966f5e7a94809df975507c15f92025b696932f",
                "operator_id": "canonical_attempt_report",
                "source_sha256": "11943915981fd7e50856cc10e12ac9e3c844ea3eebf677d894026618c01c63b8",
                "version": "1.0.0",
            },
            "report_document_sha256": files["report-document.json"]["sha256"],
            "automatic_ordering": False,
            "action_sha256": hashlib.sha256(
                json.dumps(
                    action,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()
            ).hexdigest(),
            "result_id": "a" * 64,
            "files": files,
        }

    def fetch_verified_file(self, _manifest, name):
        if name in REPORT_EVIDENCE_FILE_NAMES:
            return self.evidence[name]
        if name == "report.html":
            action = fake_action(self.job)
            configuration = html.escape(
                json.dumps({"current_action": action}, sort_keys=True, separators=(",", ":"))
            )
            fields = {
                "operator id": "canonical_attempt_report",
                "operator version": "1.0.0",
                "operator source sha256": "11943915981fd7e50856cc10e12ac9e3c844ea3eebf677d894026618c01c63b8",
                "operator content digest": "275a68f011fe9b45fadc8e1960966f5e7a94809df975507c15f92025b696932f",
            }
            rows = [
                '<tr><th scope="row">template parameters</th><td><span>AVAILABLE</span><br>'
                f"<code>{configuration}</code></td></tr>"
            ]
            rows.extend(
                '<tr><th scope="row">'
                f"{field}</th><td><span>AVAILABLE</span><br><code>{value}</code></td></tr>"
                for field, value in fields.items()
            )
            return ("<html><table>" + "".join(rows) + "</table></html>").encode()
        assert name == "notification.txt"
        return fake_source_notification(self.job)


def fake_action(job):
    common = {
        "job_id": job.job_id,
        "model_version": job.model_id,
        "production_manifest_sha256": job.production_manifest_sha256,
        "report_uuid": job.report_filename.removesuffix(".html"),
        "automatic_ordering": False,
        "action": "HOLD",
        "state_before_next": 1,
        "target_state": 1,
        "latest_market_date": "2026-09-09",
        "daily_change_pct": 1.25,
        "previous_slope_pct": 0.31,
        "next_slope_pct": 0.29,
    }
    if job.job_id == "1cd5557264db":
        return common | {
            "latest_close_cny_per_g": 950.5,
            "next_trade_date_estimate": "2026-09-10",
            "parameters": {
                "buy_threshold_pct_per_day": 0.175,
                "sell_threshold_pct_per_day": -0.275,
                "roundtrip_spread_cny_per_g": 5.0,
            },
            "next_completed_close_scenarios": {
                "buy_threshold_equivalent_cny_per_g": 940.0,
                "sell_threshold_equivalent_cny_per_g": 900.0,
            },
        }
    return common | {
        "latest_close": 7.25,
        "next_session_date_estimate": "2026-09-10",
        "rules": {"buy_crossing_pct": 0.2, "sell_crossing_pct": -0.2},
        "costs": {"buy_cost_bps": 8, "sell_cost_bps": 13},
        "next_completed_close_scenarios": {
            "buy_threshold_equivalent_raw_close": 7.4,
            "sell_threshold_equivalent_raw_close": 6.8,
        },
    }


def fake_report_document(job):
    action = fake_action(job)
    raw_fields = {
        "template_parameters": (
            {"current_action": action}, "bundle/config.json", "/template/parameters"
        ),
        "operator_id": ("canonical_attempt_report", "operator-manifest", "/operator_id"),
        "operator_version": ("1.0.0", "operator-manifest", "/semantic_version"),
        "operator_source_sha256": (
            "11943915981fd7e50856cc10e12ac9e3c844ea3eebf677d894026618c01c63b8",
            "operator-manifest",
            "/source/sha256",
        ),
        "operator_content_digest": (
            "275a68f011fe9b45fadc8e1960966f5e7a94809df975507c15f92025b696932f",
            "operator-manifest",
            "/content_digest",
        ),
    }
    core = {
        "schema_id": "quant-platform/report-document/v1",
        "schema_version": 1,
        "sections": [
            {
                "section_id": "fixture",
                "fields": [
                    {
                        "field_id": field_id,
                        "raw": raw,
                        "availability": "AVAILABLE",
                        "source_ref": {"artifact": artifact, "pointer": pointer},
                    }
                    for field_id, (raw, artifact, pointer) in raw_fields.items()
                ],
            }
        ],
    }
    document_id = hashlib.sha256(
        b"quant-platform/report-document/v1\0"
        + json.dumps(
            core,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return json.dumps(
        core | {"document_id": document_id},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def fake_report_evidence(job):
    action = fake_action(job)

    def encoded(value):
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()

    return {
        "attempt-audit.json": encoded(
            {
                "attempt_id": "a" * 64,
                "experiment_id": "b" * 64,
                "run_id": "fixture",
                "dataset": {"snapshot_id": "c" * 64},
                "result_digest": "d" * 64,
                "operators": {},
            }
        ),
        "bundle-descriptor.json": encoded(
            {"bundle_id": "e" * 64, "verification": {"status": "VERIFIED"}}
        ),
        "config.json": encoded({"template": {"parameters": {"current_action": action}}}),
        "contract.json": encoded(
            {
                "purpose": "PRESENTATION_ONLY",
                "limitations": [
                    "INTEGRITY_IS_NOT_QUALIFICATION",
                    "QUALIFICATION_IS_NOT_DEPLOYMENT_OR_TRADING_AUTHORITY",
                    "PRESENTATION_ONLY_NO_RECOMPUTATION",
                ],
            }
        ),
        "cost_breakdown.json": encoded(
            {
                "commission_cny": 0.0,
                "transfer_fee_cny": 0.0,
                "stamp_tax_cny": 0.0,
                "slippage_cny": 0.0,
                "total_cost_cny": 0.0,
            }
        ),
        "daily_replay.csv": b"Date,price,close,equity,holdings,position_after\n",
        "events.csv": b"Date,side,price,quantity,notional_cny,commission_cny,transfer_fee_cny,stamp_tax_cny,slippage_cny,total_cost_cny,cash_before_cny,cash_after_cny,holdings_before,holdings_after,reason\n",
        "metrics.json": encoded(
            {
                "period_start": "2026-09-09",
                "period_end": "2026-09-09",
                "initial_capital_cny": 1_000_000.0,
                "final_equity_cny": 1_000_000.0,
                "net_profit_cny": 0.0,
                "current_position": "LONG",
                "closed_trades": 0,
                "open_trades": 1,
                "net_return": 0.0,
                "max_drawdown": 0.0,
            }
        ),
        "operator-manifest.json": encoded(
            {
                "api_version": 2,
                "operator_id": "canonical_attempt_report",
                "semantic_version": "1.0.0",
                "source": {
                    "sha256": "11943915981fd7e50856cc10e12ac9e3c844ea3eebf677d894026618c01c63b8"
                },
                "content_digest": "275a68f011fe9b45fadc8e1960966f5e7a94809df975507c15f92025b696932f",
            }
        ),
        "report-document.json": fake_report_document(job),
        "run_manifest.json": encoded({"runtime": {}}),
        "trades.csv": b"entry_date,entry_price,quantity,entry_cost_cny,exit_date,exit_price,exit_cost_cny,status,gross_pnl_cny,net_pnl_cny,return\n",
    }


def fake_source_notification(job):
    if job.job_id == "1cd5557264db":
        return (
            "黄金生产信号｜市场日期：2026-09-09\n"
            "完成收盘：950.50 元/克；日涨跌：+1.25%\n"
            "仓位：当前持有（100%） → 目标持有（100%）；动作：HOLD（继续持有）\n"
            "斜率：上一 +0.3100%/日 → 当前 +0.2900%/日\n"
            "下一完整收盘卖出边界：900.00 元/克（斜率 -0.275%/日）\n"
            "如本次动作为 BUY/SELL，执行时点：2026-09-10 下一交易日开盘；仅人工决策，不会自动下单（automatic_ordering=false）\n"
            "口径：SGE_AU9999_PROXY；FIXED_SPREAD_ASSUMPTION_5_CNY_PER_G；市场代理评估，不代表招行实际可成交收益\n"
            "报告：f642b386-74c0-4e9f-92e6-563e7c6a5d69\n"
        ).encode()
    return (
        "交通银行生产信号｜市场日期：2026-09-09\n"
        "完成收盘：7.250 元/股；日涨跌：+1.25%\n"
        "仓位：当前持有（100%） → 目标持有（100%）；动作：HOLD（继续持有）\n"
        "斜率：上一 +0.3100%/日 → 当前 +0.2900%/日\n"
        "下一完整收盘卖出边界：6.800 元/股（斜率 -0.200%/日；假设无公司行动的原始收盘价）\n"
        "如本次动作为 BUY/SELL，执行时点：2026-09-10 下一交易日开盘；仅人工决策，不会自动下单（automatic_ordering=false）\n"
        "成本假设：买入 8 bps；卖出 13 bps\n"
        "报告：8991e9a8-1caa-41f5-b76b-6368259db5b4\n"
    ).encode()


@pytest.mark.parametrize("job_id", sorted(JOBS))
def test_exact_job_mapping_schedule_and_notification_output(tmp_path: Path, job_id: str) -> None:
    FakeClient.instances.clear()
    notification = run_job(
        JOBS[job_id],
        scheduled_for="2026-03-09T00:40:00Z",
        tls=ClientTLS("https://127.0.0.1:8443", Path("/unused"), Path("/unused"), Path("/unused")),
        jobs_path=jobs_file(tmp_path, job_id),
        transport_factory=lambda configuration: configuration,
        client_factory=FakeClient,
        publisher=lambda job, _report, _action: (
            f"https://share.ai.jingtao.fun/{job.report_filename}"
        ),
    )

    text = notification.decode()
    assert text.splitlines()[-1] == (
        f"https://share.ai.jingtao.fun/{JOBS[job_id].report_filename}"
    )
    assert "买卖指数" in text and "不会自动下单" in text
    if job_id == "1cd5557264db":
        assert "SGE_AU9999_PROXY" in text
        assert "FIXED_SPREAD_ASSUMPTION_5_CNY_PER_G" in text
        assert "完整往返价差 5 元/克，不是单边" in text
    else:
        assert "复权收盘信号" in text and "原始收盘情景" in text
        assert "买入 8 bps、卖出 13 bps" in text
    value = FakeClient.instances[-1].requests[0]
    assert value.job_id == job_id
    assert value.production_manifest_sha256 == JOBS[job_id].production_manifest_sha256
    assert value.scheduled_for == "2026-03-09T00:40:00Z"
    assert value.request_id == value.expected_request_id


def test_scheduled_fire_is_deterministic_and_fails_closed() -> None:
    assert scheduled_fire_for(
        JOBS["1cd5557264db"], datetime.fromisoformat("2026-03-09T08:41:17+08:00")
    ) == (
        "2026-03-09T00:40:00Z"
    )
    assert scheduled_fire_for(
        JOBS["297c11cad0dc"], datetime.fromisoformat("2026-03-09T08:46:17+08:00")
    ) == "2026-03-09T00:40:00Z"
    with pytest.raises(Exception, match="before"):
        scheduled_fire_for(
            JOBS["297c11cad0dc"], datetime.fromisoformat("2026-03-09T08:44:59+08:00")
        )
    with pytest.raises(Exception, match="weekend"):
        scheduled_fire_for(
            JOBS["1cd5557264db"], datetime.fromisoformat("2026-03-08T09:00:00+08:00")
        )


def test_report_publication_is_exact_and_restores_previous_page_on_failed_https_readback(
    tmp_path: Path,
) -> None:
    job = JOBS["1cd5557264db"]
    target = tmp_path / job.report_filename
    target.write_bytes(b"previous report")
    report = b"<html>2026-09-09 HOLD current report</html>"
    action = fake_action(job)

    with pytest.raises(ProductionClientError, match="read-back"):
        publish_report(job, report, action, report_root=tmp_path, readback=lambda _url: b"stale")
    assert target.read_bytes() == b"previous report"

    url = publish_report(job, report, action, report_root=tmp_path, readback=lambda _url: report)
    assert target.read_bytes() == report
    assert url == f"https://share.ai.jingtao.fun/{job.report_filename}"


def test_schedule_record_drift_and_unknown_outcome_fail_closed(tmp_path: Path) -> None:
    job = JOBS["1cd5557264db"]
    path = jobs_file(tmp_path, job.job_id)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["jobs"][0]["deliver"] = "wrong"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(Exception, match="differs"):
        run_job(
            job,
            scheduled_for="2026-03-09T00:40:00Z",
            tls=ClientTLS("https://127.0.0.1:8443", Path("/unused"), Path("/unused"), Path("/unused")),
            jobs_path=path,
            transport_factory=lambda configuration: configuration,
            client_factory=FakeClient,
        )

    class UnknownClient(FakeClient):
        def submit_and_wait(self, request):
            raise ProductionClientUnknown(request.request_id, "UNKNOWN")

    with pytest.raises(ProductionClientUnknown, match="UNKNOWN"):
        run_job(
            job,
            scheduled_for="2026-03-09T00:40:00Z",
            tls=ClientTLS("https://127.0.0.1:8443", Path("/unused"), Path("/unused"), Path("/unused")),
            jobs_path=jobs_file(tmp_path, job.job_id),
            transport_factory=lambda configuration: configuration,
            client_factory=UnknownClient,
        )


def test_result_and_report_operator_identity_drift_fail_closed(tmp_path: Path) -> None:
    job = JOBS["1cd5557264db"]

    class WrongResultOperator(FakeClient):
        def submit_and_wait(self, request):
            manifest = super().submit_and_wait(request)
            manifest["report_operator"] = dict(manifest["report_operator"])
            manifest["report_operator"]["source_sha256"] = "0" * 64
            return manifest

    class WrongReportOperator(FakeClient):
        def fetch_verified_file(self, manifest, name):
            payload = super().fetch_verified_file(manifest, name)
            if name == "report.html":
                payload = payload.replace(
                    b"11943915981fd7e50856cc10e12ac9e3c844ea3eebf677d894026618c01c63b8",
                    b"0" * 64,
                )
            return payload

    class WrongActionHash(FakeClient):
        def submit_and_wait(self, request):
            manifest = super().submit_and_wait(request)
            manifest["action_sha256"] = "0" * 64
            return manifest

    class WrongReportDocument(FakeClient):
        def fetch_verified_file(self, manifest, name):
            payload = super().fetch_verified_file(manifest, name)
            if name == "report-document.json":
                document = json.loads(payload)
                document["sections"][0]["fields"][0]["raw"]["current_action"]["action"] = "WAIT"
                return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
            return payload

    class WrongSourceEvidence(FakeClient):
        def fetch_verified_file(self, manifest, name):
            payload = super().fetch_verified_file(manifest, name)
            if name == "config.json":
                configuration = json.loads(payload)
                configuration["template"]["parameters"]["current_action"]["action"] = "WAIT"
                return json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
            return payload

    for client_factory in (
        WrongResultOperator,
        WrongReportOperator,
        WrongActionHash,
        WrongReportDocument,
        WrongSourceEvidence,
    ):
        with pytest.raises(ProductionClientError, match="result|report"):
            run_job(
                job,
                scheduled_for="2026-03-09T00:40:00Z",
                tls=ClientTLS(
                    "https://127.0.0.1:8443",
                    Path("/unused"),
                    Path("/unused"),
                    Path("/unused"),
                ),
                jobs_path=jobs_file(tmp_path, job.job_id),
                transport_factory=lambda configuration: configuration,
                client_factory=client_factory,
            )


def test_cli_rejects_unknown_job() -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(["--job-id", "unknown"])


def test_cli_failure_is_nonzero_without_fabricated_action(monkeypatch, capsys) -> None:
    def fail(*_args, **_kwargs):
        raise ProductionClientError("injected admission failure")

    monkeypatch.setattr(schedule_client, "run_job", fail)

    status = schedule_client.main(
        ["--job-id", "297c11cad0dc", "--scheduled-for", "2026-03-09T00:40:00Z"]
    )
    captured = capsys.readouterr()

    assert status == 1
    assert captured.out == ""
    assert "production client failed closed" in captured.err
    assert not any(action in captured.err for action in ("BUY", "SELL", "HOLD", "WAIT"))


def test_reviewed_cutover_config_matches_the_executable_mapping() -> None:
    value = json.loads((PROJECT / "production" / "ailearn-schedule-cutover.json").read_text())
    assert value["api"]["base_url"] == "https://127.0.0.1:8443"
    assert {item["id"] for item in value["jobs"]} == set(JOBS)
    for item in value["jobs"]:
        job = JOBS[item["id"]]
        assert item == {
            "id": job.job_id,
            "name": job.name,
            "enabled": True,
            "no_agent": True,
            "schedule": job.schedule,
            "timezone": "Asia/Shanghai",
            "deliver": DELIVERY,
            "script": job.script,
            "model_id": job.model_id,
            "production_manifest_sha256": job.production_manifest_sha256,
            "prompt": job.audit_prompt,
        }
    assert len(value["rollback"]) == 2


def test_thin_client_imports_no_provider_or_computation_modules() -> None:
    wrappers = [
        SCRIPTS / "gold_production_api_action.py",
        SCRIPTS / "bocom_production_api_action.py",
    ]
    assert all(path.stat().st_mode & 0o111 for path in wrappers)
    paths = [
        PACKAGE / "production_client.py",
        PACKAGE / "production_schedule_client.py",
        *wrappers,
    ]
    forbidden = {
        "gold_research",
        "pandas",
        "numpy",
        "requests",
        "production_jobs",
        "production_gold",
        "production_bocom",
    }
    for path in paths:
        source = path.read_text(encoding="utf-8")
        modules = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module or "")
        assert not any(any(part in module for part in forbidden) for module in modules)
        assert "feng-learn" not in source.casefold()
        assert "zhlearn:" not in source.casefold()


def test_documented_thin_runtime_imports_schedule_client(tmp_path: Path) -> None:
    package = tmp_path / "quant_platform"
    package.mkdir()
    (package / "__init__.py").write_bytes(b"")
    for name in ("production_contract.py", "production_client.py", "production_schedule_client.py"):
        shutil.copyfile(PACKAGE / name, package / name)

    recipe = (PROJECT / "production" / "AILEARN-SCHEDULE-CUTOVER.md").read_text(
        encoding="utf-8"
    )
    assert 'install -m 0444 /dev/null "$runtime/__init__.py"' in recipe
    assert (
        "for name in production_contract.py production_client.py production_schedule_client.py; do"
        in recipe
    )

    completed = subprocess.run(
        [sys.executable, "-m", "quant_platform.production_schedule_client", "--help"],
        check=False,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Trigger one reviewed QuantResearch production job" in completed.stdout
