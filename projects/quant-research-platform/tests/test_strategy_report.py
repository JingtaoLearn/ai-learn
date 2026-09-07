from pathlib import Path
from types import MappingProxyType

import pandas as pd
import pytest

import quant_platform.strategy_report as report_module
from quant_platform.strategy_config import validate_strategy_config
from quant_platform.strategy_replay import replay_strategy
from quant_platform.strategy_report import (
    ReportError,
    execution_marker_points,
    render_report,
)


FIXTURE = Path(__file__).parent / "fixtures" / "strategy" / "daily.csv"


def _config():
    return validate_strategy_config(
        {
            "schema_version": 1,
            "dataset": {
                "root": "state",
                "instrument": "SYNTH.SS",
                "snapshot_id": "a" * 64,
            },
            "output_root": "runs",
            "template": {
                "name": "single_stock_daily_causal",
                "version": "1",
                "parameters": {
                    "instrument_display_name": 'Bank <script>alert("x")</script>',
                    "evaluation_start": "2026-01-06",
                    "evaluation_end": "2026-01-12",
                    "initial_capital_cny": 100000.0,
                    "initial_state": "flat",
                    "terminal_handling": "mark_to_market",
                    "cost_assumption_label": "Research <b>assumption</b>",
                },
            },
            "operators": {
                "fit": {
                    "name": "prior_log_ols",
                    "version": "1",
                    "parameters": {
                        "window_sessions": 2,
                        "price_column": "AdjustedClose",
                    },
                },
                "smoothing": {
                    "name": "recursive_log_ema",
                    "version": "1",
                    "parameters": {"span_sessions": 1},
                },
                "statistic": {
                    "name": "adjacent_curve_pct_slope",
                    "version": "1",
                    "parameters": {},
                },
                "decision": {
                    "name": "post_start_threshold_crossing_hysteresis",
                    "version": "1",
                    "parameters": {
                        "buy_threshold_pct_per_day": 1.0,
                        "sell_threshold_abs_pct_per_day": 1.0,
                    },
                },
                "sizing": {
                    "name": "all_in_all_out_a_share_lots",
                    "version": "1",
                    "parameters": {"lot_size": 100, "target_fraction": 1.0},
                },
                "cost": {
                    "name": "cms_china_a_share",
                    "version": "1",
                    "parameters": {
                        "commission_rate": 0.0003,
                        "minimum_commission_cny": 5.0,
                        "transfer_fee_rate": 0.00001,
                        "sell_stamp_tax_rate": 0.0005,
                        "buy_slippage_bps": 2.0,
                        "sell_slippage_bps": 3.0,
                    },
                },
                "report": {
                    "name": "concise_chinese_causal_trade",
                    "version": "1",
                    "parameters": {},
                },
            },
        }
    )


def _result():
    frame = pd.read_csv(FIXTURE)
    frame["Date"] = pd.to_datetime(frame["Date"])
    return replay_strategy(frame, _config())


def _provenance(**extra):
    return {
        "config_sha256": "b" * 64,
        "dataset_instrument": "SYNTH.SS",
        "source_sha256": "c" * 64,
        **extra,
    }


def test_report_fails_closed_without_verified_cjk_font(monkeypatch):
    monkeypatch.setattr(
        report_module,
        "CJK_FONT_PATH",
        Path("/definitely/missing/wqy-zenhei.ttc"),
    )

    with pytest.raises(ReportError, match="CJK font"):
        render_report(
            _result(),
            _config(),
            _provenance(),
        )


def test_render_report_contract():
    fixture = (
        _result(),
        _config(),
        MappingProxyType(_provenance(dataset_snapshot_id="a" * 64)),
    )
    html = render_report(*fixture)

    for literal in (
        'Bank &lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;',
        "Research &lt;b&gt;assumption&lt;/b&gt;",
        "data:image/png;base64,",
        "向上穿越 +1.00%/日",
        "向下穿越 -1.00%/日",
        "仅使用前一交易日及更早的收盘信号",
        "下一交易日原始开盘价成交",
        "初始空仓，首个有效斜率只初始化区域",
        "目标仓位 100%，按 100 股整手向下取整",
        "价格收益账户",
        "不代表包含分红现金流的股东总回报",
        "原始价格、因果趋势与实际成交",
        "斜率与配置阈值",
        "累计权益",
        "初始资金",
        "最终权益",
        "买入并持有收益",
        "零成本策略收益",
        "最大回撤",
        "佣金",
        "过户费",
        "印花税",
        "滑点",
        "事件明细",
        "交易明细",
        "可复现来源",
        "config_sha256",
        "dataset_snapshot_id",
        "source_sha256",
        "仓位收益率",
        "不包含账户剩余现金",
        "html,body{max-width:100%;overflow-x:hidden}",
        ".scroll{position:relative;max-width:100%;min-width:0;overflow-x:auto",
        "scrollbar-width:thin",
        ".scroll::-webkit-scrollbar",
        "border-right:4px solid",
        ">买入<",
        ">卖出<",
        ">已平仓<",
        ">未平仓<",
        ">向上穿越买入阈值<",
        ">向下穿越卖出阈值<",
        'id="chart-open"',
        'aria-label="放大图表"',
        'id="chart-lightbox"',
        'role="dialog"',
        'aria-modal="true"',
        'aria-label="关闭放大图表"',
        "event.target===lightbox",
        "event.key==='Escape'",
    ):
        assert literal in html
    assert html.count('class="scroll-hint"') == 3
    assert html.count("左右滑动查看完整表格") == 3
    for machine_value in (
        '<script>alert("x")</script>',
        "http://",
        "https://",
        "financial-term",
        ">BUY<",
        ">SELL<",
        ">CLOSED<",
        ">OPEN<",
        "BUY_THRESHOLD_CROSSING",
        "SELL_THRESHOLD_CROSSING",
        "BUY/SELL 标记",
    ):
        assert machine_value not in html


def test_report_displays_frozen_partial_accounting_without_upgrading_it():
    result = _result()
    result.metrics["accounting_status"] = "KNOWN_EVENT_CORRECTED_PARTIAL"
    result.metrics["accounting_accounts"] = {
        "strategy": {
            "gross_dividend_fen": 100,
            "collected_tax_fen": 10,
            "outstanding_tax_fen": 0,
        }
    }
    rendered = render_report(result, _config(), _provenance())
    assert "冻结的公司行动账户事实" in rendered
    assert "KNOWN_EVENT_CORRECTED_PARTIAL" in rendered
    assert "gross_dividend_fen" in rendered
    assert "AFTER_TAX_TOTAL_RETURN_VERIFIED" not in rendered


def test_execution_markers_are_actual_event_open_dates_and_prices():
    result = _result()

    buys, sells = execution_marker_points(result.events)

    assert buys["Date"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-01-07",
        "2026-01-09",
    ]
    assert buys["price"].tolist() == [10.0, 9.0]
    assert sells["Date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-01-08"]
    assert sells["price"].tolist() == [11.0]


def test_report_rejects_dataset_instrument_provenance_mismatch():
    with pytest.raises(ReportError, match="instrument"):
        render_report(
            _result(),
            _config(),
            _provenance(dataset_instrument="OTHER.SS"),
        )
