"""ベンチマーク (配当込み)。日経ETF 1321・TOPIX ETF 1306 を Yahoo の配当イベントから配当込みにする。

既存 jquants/backtest_5patterns.fetch_bench と同じ作り方で、data/bench/<sym>.json に
[日付, 始値(配当込み指数), 終値(配当込み指数), 終値] を保存する。取れなければ J-Quants の調整後終値 (配当なし) を使う。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.parse
import urllib.request

import numpy as np

from . import config

SYMS = {"13210": "1321.T", "13060": "1306.T"}


def _path(sym: str) -> str:
    return os.path.join(config.data_dir(), "bench", sym.replace("^", "_") + ".json")


def fetch(sym: str, refresh: bool = False) -> list:
    path = _path(sym)
    if os.path.exists(path) and not refresh:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    p1, p2 = int(dt.datetime(2015, 1, 1).timestamp()), int(time.time())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?period1={p1}&period2={p2}"
           f"&interval=1d&events=div%2Csplits")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    js = json.load(urllib.request.urlopen(req, timeout=30))
    r = js["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    jst = dt.timezone(dt.timedelta(hours=9))
    day = lambda ts: dt.datetime.fromtimestamp(int(ts), jst).date().isoformat()  # noqa: E731
    evs = r.get("events") or {}
    splits = [(day(e["date"]), e["numerator"] / e["denominator"]) for e in evs.get("splits", {}).values()]
    divs: dict[str, float] = {}
    for e in evs.get("dividends", {}).values():
        divs[day(e["date"])] = divs.get(day(e["date"]), 0.0) + e["amount"]
    raw = [(day(ts), q["open"][n], q["close"][n]) for n, ts in enumerate(r["timestamp"])
           if q["close"][n] is not None and q["open"][n] is not None and q["close"][n] > 0]
    good = []
    for n, (d, o, c) in enumerate(raw):
        if good and not 0.5 < c / good[-1][2] < 2 and (n + 1 >= len(raw) or not 0.5 < c / raw[min(n + 3, len(raw) - 1)][2] < 2):
            continue
        good.append((d, o, c))
    rows, tr, pc = [], 1.0, None
    pend = sorted(divs.items())
    for d, o, c in good:
        dv = 0.0
        while pend and pend[0][0] <= d:
            dv += pend.pop(0)[1]
        if pc is not None:
            for _, ratio in splits:
                if dv / pc > 0.2:
                    dv /= ratio
            tr *= (c + dv) / pc
        k = tr / c
        rows.append([d, o * k * 100, tr * 100, c])
        pc = c
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, separators=(",", ":"))
    return rows


def total_return_close(p, code: str) -> tuple[np.ndarray, bool]:
    """パネルの日付にそろえた配当込み終値。(値, 配当込みか)"""
    sym = SYMS.get(code)
    try:
        rows = fetch(sym) if sym else None
    except Exception:  # noqa: BLE001  ネットワークが無いときは配当なしで続ける
        rows = None
    if rows:
        m = {r[0]: r[2] for r in rows}
        v = np.array([m.get(d, np.nan) for d in p.dates], dtype=float)
        # 欠けた日は前の値で埋める
        for i in range(1, len(v)):
            if v[i] != v[i]:
                v[i] = v[i - 1]
        if np.isfinite(v).mean() > 0.9:
            return v, True
    return p.AC[:, p.col(code)].copy(), False
