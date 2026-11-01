"""最小限の確認: 約定の判定順・株数計算・財務の使える日"""
import numpy as np

from system import engine, fins
from system.store import Panel


def panel(o, h, l_, c):
    a = lambda x: np.array(x, dtype=float)[:, None]  # noqa: E731
    O, H, L, C = a(o), a(h), a(l_), a(c)
    n = len(o)
    return Panel([f"2024-01-{d + 1:02d}" for d in range(n)], ["10000"], O, H, L, C, O, H, L, C,
                 np.full((n, 1), 1000.0), np.full((n, 1), 2e9))


T0 = lambda n: {"ma25": np.full((n, 1), np.nan), "low10": np.full((n, 1), np.nan), "to20": np.full((n, 1), 20.0)}  # noqa: E731
RULE = {"stop": -6.0, "t1": 20.0, "t2": 40.0, "days": 3}


def test_stop_before_take_same_day():
    p = panel([100, 100, 100, 100], [101, 130, 101, 101], [99, 90, 99, 99], [100, 100, 100, 100])
    t = engine.make_trade(p, T0(4), 0, 0, RULE, [[0, 0.1]], engine.Opts())
    assert t["xa"] == 1 and abs(t["ra"] - (-0.06)) < 1e-9  # 同じ日に損切りと利確 → 損切りを先に


def test_gap_down_fills_at_open():
    p = panel([100, 100, 80, 80], [101, 101, 81, 81], [99, 99, 79, 79], [100, 100, 80, 80])
    t = engine.make_trade(p, T0(4), 0, 0, RULE, [[0, 0.1]], engine.Opts())
    assert t["xa"] == 2 and abs(t["ra"] - (-0.20)) < 1e-9


def test_time_exit_last_close():
    p = panel([100] * 5, [101] * 5, [99] * 5, [100, 100, 102, 103, 104])
    t = engine.make_trade(p, T0(5), 0, 0, RULE, [[0, 0.1]], engine.Opts())
    assert t["xb"] == 3 and abs(t["rb"] - 0.03) < 1e-9  # 3日目 (e+2) の終値


def test_gap_limit():
    p = panel([100, 110, 110], [101, 111, 111], [99, 109, 109], [100, 110, 110])
    assert engine.make_trade(p, T0(3), 0, 0, RULE, [[0, 0.1]], engine.Opts(gap_limit=0.05)) is None


def test_avail_index():
    dates = ["2024-05-13", "2024-05-14", "2024-05-15", "2024-05-16"]
    assert fins.avail_index(dates, "2024-05-14", "13:00") == 2  # 翌営業日
    assert fins.avail_index(dates, "2024-05-14", "15:30") == 3  # 15:00以降は翌々営業日
