"""テクニカル特徴量 (日付 x 銘柄)。

既存検証 (backtest_all_pit.tech) と同じく、銘柄ごとに「データのある日だけ」を並べて計算し、元の日付に戻す。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .store import Panel

KEYS = ("nbar", "to20", "ma25", "ma200", "dev25", "hi20", "off20", "hi250", "pos250", "vr", "low10",
        "bull", "prev_high")


def _roll(x: np.ndarray, w: int, fn: str) -> np.ndarray:
    s = pd.Series(x)
    r = getattr(s.rolling(w, min_periods=w), fn)()
    return r.to_numpy()


def compute(p: Panel) -> dict[str, np.ndarray]:
    nd, nc = p.AC.shape
    out = {k: np.full((nd, nc), np.nan) for k in KEYS}
    for j in range(nc):
        o, h, l_, c = p.AO[:, j], p.AH[:, j], p.AL[:, j], p.AC[:, j]
        ok = ~(np.isnan(c) | np.isnan(o) | np.isnan(h) | np.isnan(l_))
        ix = np.flatnonzero(ok)
        if ix.size == 0:
            continue
        oc, hc, lc, cc = o[ix], h[ix], l_[ix], c[ix]
        v = np.nan_to_num(p.AV[ix, j])
        va = np.nan_to_num(p.Va[ix, j]) / 1e8
        n = ix.size
        out["nbar"][ix, j] = np.arange(1, n + 1)
        # 既存: to = sum(va[p-19:p+1]) / 20 (20本未満は None)
        out["to20"][ix, j] = _roll(va, 20, "mean")
        m25 = _roll(cc, 25, "mean")
        out["ma25"][ix, j] = m25
        out["ma200"][ix, j] = _roll(cc, 200, "mean")
        out["dev25"][ix, j] = (cc / m25 - 1) * 100
        hi20 = _roll(hc, 20, "max")
        out["hi20"][ix, j] = hi20
        out["off20"][ix, j] = (cc / hi20 - 1) * 100
        hi250 = pd.Series(hc).rolling(250, min_periods=60).max().to_numpy()
        out["hi250"][ix, j] = hi250
        out["pos250"][ix, j] = cc / hi250
        # 既存: vr = 当日出来高 / 前日までの20日平均
        vprev = np.concatenate([[np.nan], _roll(v, 20, "mean")[:-1]])
        with np.errstate(divide="ignore", invalid="ignore"):
            out["vr"][ix, j] = np.where(vprev > 0, v / vprev, np.nan)
        out["low10"][ix, j] = _roll(lc, 10, "min")
        out["bull"][ix, j] = (cc > oc).astype(float)
        out["prev_high"][ix, j] = np.concatenate([[np.nan], hc[:-1]])
    return out
