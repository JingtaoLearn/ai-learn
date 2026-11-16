"""Advice follows the user's real position, respects "didn't do it", and re-arms on fresh crossings."""
from quant.portfolio import advise


def adv(held, target, today="2026-11-16", changed="2026-11-16", skips=None, crosses=None, ratio=None):
    return advise(held=held, ratio=ratio, target=target, today=today, changed=changed,
                  skips=skips or {}, crosses=crosses or {})


def test_story_hold_against_a_flat_model():
    # Declared 200 g while the trend is down (model flat): sell.
    a = adv(True, 0, crosses={"SELL": "2026-11-05"})
    assert a["action"] == "SELL" and not a["dismissed"] and not a["new"]

    # "Didn't sell" on 11-16: keep holding, no more nagging on later days.
    a = adv(True, 0, today="2026-11-18", skips={"SELL": "2026-11-16"}, crosses={"SELL": "2026-11-05"})
    assert a["action"] == "HOLD" and a["dismissed"] and a["skipped_on"] == "2026-11-16"

    # The slope recovers and breaks down through the sell line again on 11-25: a fresh reason to sell.
    a = adv(True, 0, today="2026-11-25", skips={"SELL": "2026-11-16"}, crosses={"SELL": "2026-11-25"})
    assert a["action"] == "SELL" and a["new"]

    # Sold on 11-26: now flat like the model.
    assert adv(False, 0, changed="2026-11-26", skips={"SELL": "2026-11-16"})["action"] == "WAIT"


def test_story_flat_while_the_model_is_long():
    a = adv(False, 1, crosses={"BUY": "2026-11-16"})
    assert a["action"] == "BUY" and a["new"] and not a["topup"]
    a = adv(False, 1, today="2026-11-17", skips={"BUY": "2026-11-16"}, crosses={"BUY": "2026-11-16"})
    assert a["action"] == "WAIT" and a["dismissed"]


def test_partial_position_tops_up_and_full_one_holds():
    a = adv(True, 1, ratio=0.5)
    assert a["action"] == "BUY" and a["topup"]
    assert adv(True, 1, ratio=0.95)["action"] == "HOLD"
    assert adv(True, 1)["action"] == "HOLD"  # no full-position definition: holding is enough


def test_a_new_declared_state_overrides_an_old_skip():
    # Skipped a sell, later re-declared holdings (or traded): the old answer no longer applies.
    a = adv(True, 0, today="2026-12-01", changed="2026-11-30", skips={"SELL": "2026-11-16"},
            crosses={"SELL": "2026-11-05"})
    assert a["action"] == "SELL" and not a["dismissed"]


def test_since_start_compares_actual_follow_and_hold():
    import numpy as np
    from quant.compare import since_start
    dates = ["d0", "d1", "d2", "d3", "d4"]
    close = np.array([100, 110, 100, 90, 95.0])
    open_ = np.array([100, 100, 110, 100, 90.0])     # each open = previous close (no gaps)
    model = np.array([1, 1, 0, 0, 0])                 # model exits at the d2 close -> sells at the d3 open
    init = {"id": 1, "date": "d0", "side": "INIT", "qty": 10, "price": 100.0}
    res = since_start(dates, open_, close, model, [init], 1.0, 0, 0, None)
    assert res["capital"] == 1000 and res["days"] == 4 and res["model_switches"] == ["d2"]
    assert res["hold"]["pnl"] == -50 and res["actual"]["pnl"] == -50     # you held through the drop
    assert abs(res["follow"]["pnl"]) < 1e-9                            # following sold at 100 on d3

    sold = {"id": 2, "date": "d3", "side": "SELL", "qty": None, "price": 100.0}
    res = since_start(dates, open_, close, model, [init, sold], 1.0, 0, 0, None)
    assert res["actual"]["pnl"] == 0 and res["actual"]["pnl"] == res["follow"]["pnl"]


def test_unfinished_session_covers_the_lunch_break_only_on_trading_hours():
    from datetime import UTC, datetime
    from quant.data import unfinished_session
    at = lambda h, m: datetime(2026, 11, 16, h, m, tzinfo=UTC)   # Monday; CN = UTC+8
    assert unfinished_session("cn", at(4, 0)) == "2026-11-16"     # 12:00 Beijing, lunch break
    assert unfinished_session("cn", at(7, 30)) is None            # 15:30 Beijing, closed
    assert unfinished_session("cn", at(0, 30)) is None            # 08:30 Beijing, not open yet
    assert unfinished_session("us", at(15, 0)) == "2026-11-16"    # 10:00 New York
