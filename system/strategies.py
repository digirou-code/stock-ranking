"""3枠のユニバースとスコア。戻り値はどれも日付 x 銘柄の「並び順キー」行列 (大きいほど優先、NaN は対象外)。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .fins import FinPanel
from .store import Panel


def pct_rank(x: np.ndarray, mask: np.ndarray, higher: bool = True) -> np.ndarray:
    """行ごとに mask 内の順位を 0〜1 に (build_data.percentiles と同じ: 順位 / (n-1))"""
    df = pd.DataFrame(np.where(mask, x, np.nan))
    r = df.rank(axis=1, method="first") - 1
    n = df.notna().sum(axis=1).to_numpy()[:, None]
    with np.errstate(divide="ignore", invalid="ignore"):
        p = np.where(n > 1, r.to_numpy() / (n - 1), 0.5)
    p[np.isnan(r.to_numpy())] = np.nan
    return p if higher else 1 - p


def combine(items: list[np.ndarray], weights: list[float] | None = None, min_cover: int = 4) -> np.ndarray:
    """欠けた項目は0.5で補って加重平均 x 100。そろっている項目が min_cover 未満なら NaN"""
    weights = weights or [1.0] * len(items)
    have = sum((~np.isnan(x)).astype(int) for x in items)
    tot = sum(w * np.where(np.isnan(x), 0.5, x) for x, w in zip(items, weights)) / sum(weights) * 100
    return np.where(have >= min_cover, tot, np.nan)


class Strategies:
    def __init__(self, p: Panel, T: dict, fp: FinPanel, cfg: dict, exclude_no_fins: bool | None = None):
        self.p, self.T, self.fp, self.cfg = p, T, fp, cfg
        u = cfg["universe"]
        ex = u["exclude_no_fins"] if exclude_no_fins is None else exclude_no_fins
        self.pv = fp.price_values(p.C)
        self.live = T["nbar"] >= u["min_bars"]
        self.liquid = self.live & (T["to20"] >= u["min_turnover_oku"])
        if ex:
            self.liquid &= fp.has_stmt[None, :]

    # ------------------------------------------------ L: 売買代金順 (B2 と同じ)
    def L(self) -> np.ndarray:
        return np.where(self.liquid, self.T["to20"], np.nan)

    # ------------------------------------------------ G0: SCORE業績 (既存と同じ5項目、順位は live 全体で取る)
    def score_g0(self) -> np.ndarray:
        per = self.pv["per"]
        per_k = np.where(np.isnan(per) | (per > 0), per, 9999)
        items = [pct_rank(per_k, self.live, higher=False), pct_rank(self.pv["roe"], self.live),
                 pct_rank(self.pv["dy"], self.live), pct_rank(self.fp.m["rgf"], self.live),
                 pct_rank(self.fp.m["egf"], self.live)]
        return combine(items)

    def g_universe(self) -> np.ndarray:
        return self.liquid & (self.pv["mc"] >= self.cfg["sleeves"]["G"]["min_mcap_oku"])

    def G0(self) -> np.ndarray:
        return np.where(self.g_universe(), self.score_g0(), np.nan)

    # ------------------------------------------------ G1: 成長寄り (設計書の重み)
    def G1(self) -> np.ndarray:
        m, pv, T = self.fp.m, self.pv, self.T
        per = pv["per"]
        with np.errstate(invalid="ignore", divide="ignore"):
            peg = np.where((per > 0) & (m["npf_growth"] > 0), per / m["npf_growth"], np.nan)
        uni = self.g_universe() & (m["rg"] > 0) & (m["opg"] > 0) & (per > 0) & (per <= 60) & (self.p.AC > T["ma200"])
        items = [pct_rank(m["rg"], uni), pct_rank(m["opg"], uni), pct_rank(m["npf_growth"], uni),
                 pct_rank(pv["roe"], uni), pct_rank(peg, uni, higher=False), pct_rank(T["pos250"], uni)]
        s = combine(items, [20, 20, 15, 15, 15, 15], min_cover=4)
        return np.where(uni, s, np.nan)

    # ------------------------------------------------ R: 急落 + 業績の下方修正なし + 下げ止まり
    def R(self) -> np.ndarray:
        c = self.cfg["sleeves"]["R"]
        m, pv, T, AC = self.fp.m, self.pv, self.T, self.p.AC
        cut20 = pd.DataFrame(self.fp.cut).rolling(20, min_periods=1).max().to_numpy() > 0
        uni = (self.live & (T["to20"] >= c["min_turnover_oku"]) & (pv["mc"] >= c["min_mcap_oku"])
               & (m["er"] >= c["min_equity_ratio"]) & (m["op_ttm"] > 0) & self.fp.has_stmt[None, :])
        crash = (T["off20"] <= -20) | (T["dev25"] <= -12)
        turn = (T["bull"] > 0) & (AC > T["prev_high"]) & (T["vr"] >= 1.2)
        sig = uni & crash & ~cut20 & turn
        return np.where(sig, -T["off20"], np.nan)

    # ------------------------------------------------ 旧「乖離リバウンド」(比較用、build_data.SCREENS と同じ条件)
    def old_rebound(self) -> np.ndarray:
        m, pv, T = self.fp.m, self.pv, self.T
        per = pv["per"]
        sig = (self.liquid & (T["dev25"] <= -10) & (m["rg"] > 0) & (m["eg"] > 0) & (pv["roe"] >= 8)
               & (per > 0) & (per <= 20) & (pv["mc"] >= 300))
        return np.where(sig, -T["dev25"], np.nan)

    def coverage_start(self) -> int:
        """SCORE業績・売上と利益の前年同期比が、売買対象の半数以上で出せる最初の日 (既存検証と同じ考え方)"""
        sf = self.score_g0()
        rg, eg = self.fp.m["rg"], self.fp.m["eg"]
        n = self.liquid.sum(axis=1)
        for i in range(200, len(self.p.dates)):
            if n[i] == 0:
                continue
            mk = self.liquid[i]
            if (np.isfinite(sf[i][mk]).mean() >= 0.5 and np.isfinite(rg[i][mk]).mean() >= 0.5
                    and np.isfinite(eg[i][mk]).mean() >= 0.5):
                return i
        return len(self.p.dates) - 1
