"""売買シミュレーション (円建て・100株単位・3枠)。日次バッチとバックテストで同じ判定を使う。

手順 (既存の backtest_all_pit.Sim と同じ流れ):
  当日終値でシグナル → 翌営業日の寄り付きで買う → 損切りは逆指値、利確は指値 (日中の安値・高値で判定、
  寄り付きで飛び越えたら始値) → 最大保有日数の最終日の終値で手仕舞い。
  利確は2本立て (半分は t1、残りは t2)。売った枠は翌営業日から空く。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np

from .store import Panel

MAX_ABS_RET = 200.0
OPEN = 10**9  # まだ手仕舞っていない (データの最後まで保有中)


def cost_pct(tiers, to_oku: float) -> float:
    for th, c in tiers:
        if (to_oku if to_oku == to_oku else 0) >= th:
            return c
    return tiers[-1][1]


@dataclass
class Opts:
    gap_limit: float | None = None   # 寄りが前日終値 x (1+この値) を超えたら買わない
    trail: float | None = None       # 最高値からこの割合の下落で売る (時間切れの代わり)
    ma_exit: bool = False            # 終値が25日線以上で翌朝売る (R枠)
    low_stop: bool = False           # 直近10日安値を損切りにする (R枠、下限は rule の stop)
    cost_add: float = 0.0            # コスト試験: 片道に上乗せ (%)


def leg_px(p: Panel, T: dict, j: int, e: int, sp: float, tp: float, days: int, o: Opts, allow_open: bool = False):
    """1本分の (約定価格(調整後), 手仕舞い日)。データが足りなければ (None, None)、allow_open なら (nan, OPEN)"""
    AO, AH, AL, AC = p.AO[:, j], p.AH[:, j], p.AL[:, j], p.AC[:, j]
    nd = len(AC)
    last_c, last_k = None, e
    hh = None
    limit = days if o.trail is None else 250
    for d in range(limit):
        k = e + d
        if k >= nd:
            return (np.nan, OPEN) if allow_open else (None, None)
        stop = sp
        if o.trail is not None and hh is not None:
            stop = max(sp, hh * (1 - o.trail))
        oo, h, l_, c = AO[k], AH[k], AL[k], AC[k]
        if d > 0 and oo == oo:
            if oo <= stop or oo >= tp:
                return oo, k
        if l_ == l_ and l_ <= stop:
            return stop, k
        if h == h and h >= tp:
            return tp, k
        if c == c:
            last_c, last_k = c, k
            if o.ma_exit and T["ma25"][k, j] == T["ma25"][k, j] and c >= T["ma25"][k, j]:
                if k + 1 < nd and AO[k + 1] == AO[k + 1]:
                    return AO[k + 1], k + 1
                return c, k
        if h == h:
            hh = h if hh is None else max(hh, h)
    return last_c, last_k


def make_trade(p: Panel, T: dict, j: int, i: int, rule: dict, tiers, o: Opts, allow_open: bool = False):
    """シグナル日 i → 翌営業日寄りで買う取引。買えなければ None"""
    e = i + 1
    nd = len(p.dates)
    if e >= nd:
        return None
    ent = p.AO[e, j]
    if not (ent > 0) or not (p.AV[e, j] > 0) or p.AH[e, j] == p.AL[e, j] or not (p.AC[i, j] > 0):
        return None
    if not (0.6 < ent / p.AC[i, j] < 1.6):
        return None
    if o.gap_limit is not None and ent > p.AC[i, j] * (1 + o.gap_limit):
        return None
    sp = ent * (1 + rule["stop"] / 100)
    if o.low_stop and T["low10"][i, j] == T["low10"][i, j]:
        sp = max(sp, T["low10"][i, j])
        if sp >= ent:
            return None
    pa, xa = leg_px(p, T, j, e, sp, ent * (1 + rule["t1"] / 100), rule["days"], o, allow_open)
    pb, xb = leg_px(p, T, j, e, sp, ent * (1 + rule["t2"] / 100), rule["days"], o, allow_open)
    if pa is None or pb is None:
        return None
    c1 = cost_pct(tiers, T["to20"][i, j]) + o.cost_add
    ra, rb = pa / ent - 1, pb / ent - 1
    if abs((ra + rb) / 2) * 100 > MAX_ABS_RET:
        return None
    return {"e": e, "ent": ent, "ent_raw": p.O[e, j], "ra": ra, "rb": rb, "xa": xa, "xb": xb, "c1": c1 / 100,
            "sp": sp, "tp1": ent * (1 + rule["t1"] / 100), "tp2": ent * (1 + rule["t2"] / 100), "i": i}


@dataclass
class Sleeve:
    name: str
    keys: np.ndarray          # 日付 x 銘柄の並び順キー
    rule: dict
    slots: int
    weight: float
    idle: str = "cash"        # cash / etf
    live: bool = True         # False なら買わない (資金は idle のまま)
    opts: Opts = field(default_factory=Opts)
    max_cands: int = 40


@dataclass
class SimConfig:
    initial: float = 30_000_000
    lot: int | None = 100            # None なら端数株 (既存検証の再現用)
    lot_overshoot: float = 1.5
    cash_buffer: float = 0.03
    max_order_pct_of_turnover: float | None = 0.01
    tiers: list = field(default_factory=lambda: [[10, 0.10], [3, 0.15], [1, 0.25], [0, 0.40]])
    dd_half: float | None = -0.15
    dd_stop: float | None = -0.25
    dd_resume: float = -0.20
    etf_code: str = "13210"


def simulate(p: Panel, T: dict, sleeves: list[Sleeve], sc: SimConfig, first: int, last_sig: int,
             end: int | None = None, etf: np.ndarray | None = None, live: bool = False) -> dict:
    """first の終値から end の終値まで。シグナルは first〜last_sig。etf は待機資金の運用先 (配当込み終値)"""
    nd = len(p.dates)
    end = nd - 1 if end is None else end
    if etf is None:
        etf = p.AC[:, p.col(sc.etf_code)]
    cands = []
    for s in sleeves:
        lst = {}
        for i in range(first, last_sig + 1):
            row = s.keys[i]
            ok = np.flatnonzero(np.isfinite(row))
            if ok.size:
                top = ok[np.argsort(-row[ok], kind="stable")][: s.max_cands]
                lst[i] = top.tolist()
        cands.append(lst)
    cache = {}
    cash, prev, peak = sc.initial, sc.initial, sc.initial
    stopped = False
    held: list[dict] = []
    eq, trades = [], []
    etf_amt = 0.0
    for k in range(first + 1, end + 1):
        # 待機資金の日経ETF (売買コストなし、既存検証と同じ扱い)
        if etf_amt > 0 and etf[k] == etf[k] and etf[k - 1] == etf[k - 1]:
            cash += etf_amt * (etf[k] / etf[k - 1] - 1)
        # 手仕舞い (両方の足が終わった翌日に枠と現金が戻る)
        for h in [h for h in held if max(h["t"]["xa"], h["t"]["xb"]) < k]:
            t = h["t"]
            back = (h["na"] * (1 + t["ra"]) + h["nb"] * (1 + t["rb"])) * t["ent_raw"] * (1 - t["c1"])
            cash += back
            trades.append({"sleeve": h["s"], "code": p.codes[h["j"]], "entry": p.dates[t["e"]],
                           "exit": p.dates[max(t["xa"], t["xb"])], "cost": h["cost"], "pnl": back - h["cost"],
                           "ret": back / h["cost"] - 1})
            held.remove(h)
        # DD ブレーキ (前日終値の資産で判定)
        dd = prev / peak - 1
        if sc.dd_stop is not None and dd <= sc.dd_stop:
            stopped = True
        elif stopped and dd > sc.dd_resume:
            stopped = False
        scale = 0.0 if stopped else (0.5 if sc.dd_half is not None and dd <= sc.dd_half else 1.0)
        i = k - 1
        if i <= last_sig and scale > 0:
            codes_held = {h["j"] for h in held}
            for si, s in enumerate(sleeves):
                if not s.live:
                    continue
                n_s = sum(1 for h in held if h["s"] == s.name)
                for j in cands[si].get(i, []):
                    if n_s >= s.slots:
                        break
                    if j in codes_held:
                        continue
                    key = (si, j, i)
                    if key not in cache:
                        cache[key] = make_trade(p, T, j, i, s.rule, sc.tiers, s.opts, live)
                    t = cache[key]
                    if t is None:
                        continue
                    target = prev * s.weight / s.slots * scale
                    if sc.max_order_pct_of_turnover is not None and T["to20"][i, j] == T["to20"][i, j]:
                        target = min(target, T["to20"][i, j] * 1e8 * sc.max_order_pct_of_turnover)
                    px = t["ent_raw"]
                    if sc.lot:
                        sh = int(target // (px * sc.lot)) * sc.lot
                        if sh == 0 and px * sc.lot <= target * sc.lot_overshoot:
                            sh = sc.lot
                        if sh == 0:
                            continue
                        lots = sh // sc.lot
                        na = (lots // 2) * sc.lot
                        nb = sh - na
                    else:
                        sh = min(target, cash / (1 + t["c1"])) / px
                        if sh <= 0:
                            break
                        na = nb = sh / 2
                    cost = (na + nb) * px * (1 + t["c1"])
                    if cost <= 0 or cash - cost < sc.cash_buffer * prev - 1e-6:
                        if sc.lot:
                            continue
                        break
                    cash -= cost
                    held.append({"s": s.name, "j": j, "t": t, "na": na, "nb": nb, "cost": cost})
                    codes_held.add(j)
                    n_s += 1
        # 時価評価
        value = cash
        mv = {s.name: 0.0 for s in sleeves}
        for h in held:
            t, j = h["t"], h["j"]
            cl = p.AC[k, j]
            if cl == cl:
                h["live"] = cl / t["ent"] - 1
            live = h.get("live", 0.0)
            v = 0.0
            for n, x, r in ((h["na"], t["xa"], t["ra"]), (h["nb"], t["xb"], t["rb"])):
                v += n * t["ent_raw"] * (1 + (r if k >= x else live)) * (1 - t["c1"])
            value += v
            mv[h["s"]] += v
        eq.append(value)
        prev = value
        peak = max(peak, value)
        etf_amt = sum(max(0.0, s.weight * value - mv[s.name]) for s in sleeves if s.idle == "etf")
        etf_amt = min(etf_amt, max(0.0, cash - sc.cash_buffer * value))
    return {"dates": p.dates[first + 1: end + 1], "equity": eq, "trades": trades, "held": held, "cash": cash,
            "peak": peak, "stopped": stopped, "etf_amt": etf_amt}


# ---------------------------------------------------------------- 成績
def stats(dates: list[str], eq: list[float], initial: float, trades: list[dict] | None = None) -> dict:
    eq = np.asarray(eq, dtype=float)
    if eq.size == 0:
        return {}
    years = max((dt.date.fromisoformat(dates[-1]) - dt.date.fromisoformat(dates[0])).days / 365.25, 0.1)
    v = np.concatenate([[initial], eq])
    peak = np.maximum.accumulate(v)
    dd = v / peak - 1
    r = v[1:] / v[:-1] - 1
    out = {"total": round((v[-1] / initial - 1) * 100, 1),
           "annual": round(((v[-1] / initial) ** (1 / years) - 1) * 100, 1),
           "max_dd": round(dd.min() * 100, 1),
           "sharpe": round(float(r.mean() / r.std() * np.sqrt(245)), 2) if r.std() > 0 else None}
    if trades is not None:
        pnl = np.array([t["pnl"] for t in trades])
        gain, loss = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
        out.update({"trades": len(trades), "pf": round(gain / loss, 2) if loss > 0 else None,
                    "win": round((pnl > 0).mean() * 100, 1) if len(pnl) else None})
    return out


def yearly(dates: list[str], eq: list[float], initial: float) -> dict:
    out, base, cur = {}, initial, dates[0][:4]
    last = initial
    for d, v in zip(dates, eq):
        y = d[:4]
        if y != cur:
            out[cur] = round((last / base - 1) * 100, 1)
            base, cur = last, y
        last = v
    out[cur] = round((last / base - 1) * 100, 1)
    return out


def bench_curve(c: np.ndarray, first: int, end: int, initial: float) -> list[float]:
    base = c[first]
    out, last = [], initial
    for k in range(first + 1, end + 1):
        if c[k] == c[k]:
            last = initial * c[k] / base
        out.append(last)
    return out
