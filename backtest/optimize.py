"""バックテスト結果をもとに、スクリーニング条件と手仕舞いルールを探し直す。

使い方:
    python backtest/optimize.py --cache bt_cache/prices.pkl   # 実データ (run_backtest.py と同じキャッシュ)
    python backtest/optimize.py --demo                        # 乱数データで動作確認

手順 (過剰最適化を避けるため、直近1年は探索に使わない):
    1. 探索期間 (直近1年より前) で、4系統の条件の組み合わせを10日保有で比べる
    2. 系統ごとの上位3つについて、損切り・利確・最大保有日数の組み合わせを比べる
    3. 探索期間の成績で系統ごとに1つ選び、直近1年 (検証期間) の成績を出す
    評価は10枠運用 (run_backtest.portfolio) の年率リターン ÷ 最大ドローダウン。

出力:
    backtest/results/optimize.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_backtest as bt  # noqa: E402

OUT_DIR = bt.OUT_DIR
MIN_TRADES = 150   # 探索期間でこれ未満の取引しかない条件は採用しない
MIN_PF = 1.1
TOP_PER_FAMILY = 3
MAX_DAYS = [5, 10]  # ユーザーの取引ルール (最大10営業日) の範囲で探す
STOPS = [None, 1.5, 2.0, 3.0]
TAKES = [None, 2.5, 3.0, 4.0]


# ---------------------------------------------------------------- 追加指標
def extra_indicators(D, T):
    C = D["Close"]
    ema = lambda x, n: x.ewm(span=n, adjust=False).mean()
    macd = ema(C, 12) - ema(C, 26)
    sig = ema(macd, 9)
    hist = macd - sig
    T["macd"] = macd
    T["hist_up"] = hist > hist.shift(1)
    T["macd_cross"] = (hist > 0) & (hist.shift(1) <= 0)
    T["atrp"] = T["atr"] / C * 100
    # 市場全体: 売買代金1億円以上の銘柄の等金額平均指数
    liquid = T["to"] >= 1
    r = (C / C.shift(1) - 1).clip(-0.3, 0.3).where(liquid)
    idx = (1 + r.mean(axis=1).fillna(0)).cumprod()
    mk = lambda s: pd.DataFrame(np.broadcast_to(s.to_numpy()[:, None], C.shape), index=C.index, columns=C.columns)
    m = {
        "mkt_above200": idx > idx.rolling(200).mean(),
        "mkt_above50": idx > idx.rolling(50).mean(),
        "mkt_dip": (idx / idx.rolling(25).mean() - 1) * 100 <= -3,
    }
    for k, v in m.items():
        T[k] = mk(v)
    T["mkt_r3m"] = mk((idx / idx.shift(63) - 1) * 100)
    T["rs3m"] = T["r3m"] - T["mkt_r3m"]
    T["hi_break20"] = C > C.shift(1).rolling(20).max()
    T["hi_break60"] = C > C.shift(1).rolling(60).max()
    T["hi_break10"] = C > C.shift(1).rolling(10).max()
    T["up"] = T["chg"] > 0
    return T, idx


# ---------------------------------------------------------------- 条件の候補
def families(T):
    """系統ごとに (名前, 条件の説明, シグナル, 並び順キー) の候補を返す"""
    true = T["up"] | ~T["up"]
    regimes = {
        "なし": true,
        "市場が200日線より上": T["mkt_above200"],
        "市場が50日線より上": T["mkt_above50"],
        "市場が25日線から-3%以上下落中": T["mkt_dip"],
        "市場が急落中ではない": ~T["mkt_dip"],
    }
    out = {"rebound": [], "pullback": [], "breakout": [], "macd": []}

    # ① 乖離リバウンド
    confirms = {"なし": true, "陽線": T["bull"], "前日比プラス": T["up"], "MACDヒストグラム上向き": T["hist_up"]}
    for dev, (cn, cm), (rn, rm), to in itertools.product(
            [-8, -10, -12, -15], confirms.items(), regimes.items(), [1, 5]):
        sig = (T["dev25"] <= dev) & (T["mc"] >= 300) & (T["to"] >= to) & cm & rm
        out["rebound"].append({
            "cond": [f"25日線乖離 {dev}%以下", "時価総額300億円以上", f"売買代金{to}億円以上",
                     f"確認: {cn}", f"相場: {rn}"],
            "sig": sig, "key": -T["dev25"]})

    # ② 25日線押し目
    triggers = {"なし": true, "陽線": T["bull"], "MACDヒストグラム上向き": T["hist_up"], "RSI 50未満": T["rsi"] < 50}
    trend = T["ma75_up"] & T["above75"] & T["ma25_over75"] & (T["to"] >= 1)
    for (dlo, dhi), (olo, ohi), (tn, tm), (rn, rm) in itertools.product(
            [(-3, 1), (-5, 0), (-2, 2)], [(-15, -5), (-20, -3)], triggers.items(),
            [(k, v) for k, v in regimes.items() if k != "市場が25日線から-3%以上下落中"]):
        sig = trend & bt.btw(T["dev25"], dlo, dhi) & bt.btw(T["off20"], olo, ohi) & tm & rm
        out["pullback"].append({
            "cond": ["75日線上向き・株価>75日線・25日線>75日線", f"25日線乖離 {dlo}〜{dhi}%",
                     f"20日高値から {olo}〜{ohi}%", "売買代金1億円以上", f"確認: {tn}", f"相場: {rn}"],
            "sig": sig, "key": T["r3m"]})

    # ③ 高値ブレイク
    for b, v, (rn, rm), rsi_cap, use_macd in itertools.product(
            [10, 20, 60], [1.0, 1.5, 2.0],
            [(k, regimes[k]) for k in ("なし", "市場が200日線より上", "市場が50日線より上")],
            [None, 75], [False, True]):
        sig = T[f"hi_break{b}"] & T["ma25_over75"] & (T["vr"] >= v) & (T["to"] >= 1) & rm
        cond = [f"終値が直近{b}日の最高値を更新", "25日線>75日線", f"出来高 20日平均の{v}倍以上",
                "売買代金1億円以上", f"相場: {rn}"]
        if rsi_cap:
            sig = sig & (T["rsi"] < rsi_cap)
            cond.append(f"RSI(14) {rsi_cap}未満")
        if use_macd:
            sig = sig & (T["macd"] > 0)
            cond.append("MACD 0より上")
        out["breakout"].append({"cond": cond, "sig": sig, "key": T["vr"]})

    # ④ MACDゴールデンクロス (新規)
    trends = {"なし": true, "株価>200日線": T["above200"], "75日線上向き": T["ma75_up"]}
    for below0, (tn, tm), (rn, rm), v in itertools.product(
            [False, True], trends.items(),
            [(k, regimes[k]) for k in ("なし", "市場が200日線より上")], [None, 1.5]):
        sig = T["macd_cross"] & (T["to"] >= 1) & tm & rm
        cond = ["MACDがシグナル線を上抜け", "売買代金1億円以上", f"トレンド: {tn}", f"相場: {rn}"]
        if below0:
            sig = sig & (T["macd"] < 0)
            cond.append("MACD 0未満 (底からの反転)")
        if v:
            sig = sig & (T["vr"] >= v)
            cond.append(f"出来高 20日平均の{v}倍以上")
        out["macd"].append({"cond": cond, "sig": sig, "key": T["rs3m"]})
    return out


FAMILY_NAMES = {"rebound": "乖離リバウンド", "pullback": "25日線押し目", "breakout": "高値ブレイク",
                "macd": "MACD反転 (新規)"}


# ---------------------------------------------------------------- 売買の計算 (ベクトル化)
class Sim:
    def __init__(self, D, T):
        self.M = bt.Market(D, T)
        M = self.M
        self.n = len(M.idx)
        self.cost_tab = np.full(M.to.shape, bt.COST_TIERS[-1][1])
        to = np.nan_to_num(M.to, nan=0)
        for th, c in reversed(bt.COST_TIERS):
            self.cost_tab[to >= th] = c
        self.cost_tab = 2 * (self.cost_tab + bt.COMMISSION)
        e = np.vstack([M.O[1:], np.full((1, M.O.shape[1]), np.nan)])
        eh = np.vstack([M.H[1:], np.full((1, M.O.shape[1]), np.nan)])
        el = np.vstack([M.L[1:], np.full((1, M.O.shape[1]), np.nan)])
        ev = np.vstack([M.V[1:], np.full((1, M.O.shape[1]), np.nan)])
        with np.errstate(invalid="ignore"):
            self.enterable = np.isfinite(e) & (e > 0) & (ev > 0) & (eh != el)

    def trades(self, sig, period, stop=None, take=None, days=10, key=None):
        """シグナル→トレード。戻り値: dict of arrays (i, j, gross, net, exit_idx, why)"""
        M = self.M
        s = sig.to_numpy(bool) & period[:, None] & self.enterable
        s[self.n - days:, :] = False  # 手仕舞い日がデータの外になるもの
        ii, jj = np.nonzero(s)
        if len(ii) == 0:
            return None
        e = ii + 1
        ent = M.O[e, jj]
        atr = M.atr[ii, jj]
        sp = ent - stop * atr if stop else np.full(len(ii), -np.inf)
        tp = ent + take * atr if take else np.full(len(ii), np.inf)
        px = np.full(len(ii), np.nan)
        xi = np.full(len(ii), -1)
        why = np.zeros(len(ii), np.int8)  # 0=時間 1=損切り 2=利確
        open_ = np.ones(len(ii), bool)
        for d in range(days):
            k = e + d
            o, h, l, c = M.O[k, jj], M.H[k, jj], M.L[k, jj], M.C[k, jj]
            with np.errstate(invalid="ignore"):
                if d > 0:
                    m = open_ & (o <= sp)
                    px[m], xi[m], why[m], open_[m] = o[m], k[m], 1, False
                    m = open_ & (o >= tp)
                    px[m], xi[m], why[m], open_[m] = o[m], k[m], 2, False
                m = open_ & (l <= sp)
                px[m], xi[m], why[m], open_[m] = sp[m], k[m], 1, False
                m = open_ & (h >= tp)
                px[m], xi[m], why[m], open_[m] = tp[m], k[m], 2, False
        k = e + days - 1
        px[open_], xi[open_] = M.C[k[open_], jj[open_]], k[open_]
        ok = np.isfinite(px) & np.isfinite(ent)
        g = (px / ent - 1) * 100
        net = g - self.cost_tab[ii, jj]
        kv = key.to_numpy(float)[ii, jj] if key is not None else np.zeros(len(ii))
        t = {"i": ii[ok], "j": jj[ok], "g": g[ok], "net": net[ok], "x": xi[ok], "why": why[ok], "key": kv[ok]}
        return dedupe(t)

    def evaluate(self, t, with_curve=False):
        if t is None or len(t["i"]) == 0:
            return {"n": 0}
        net = t["net"]
        wins, losses = net[net > 0], net[net <= 0]
        pf = wins.sum() / -losses.sum() if losses.sum() < 0 else None
        rows = list(zip(t["i"], t["j"], t["g"], t["net"], [0] * len(net), [0] * len(net), t["key"], t["x"]))
        p = bt.portfolio(rows, self.M) or {}
        day = pd.Series(net).groupby(t["i"]).mean()
        out = {
            "n": int(len(net)),
            "win": float((net > 0).mean() * 100),
            "avg_win": float(wins.mean()) if len(wins) else None,
            "avg_loss": float(losses.mean()) if len(losses) else None,
            "payoff": float(wins.mean() / -losses.mean()) if len(wins) and len(losses) and losses.mean() < 0 else None,
            "net": float(net.mean()),
            "pf": float(pf) if pf is not None else None,
            "day_net": float(day.mean()),
            "exit": {k: float((t["why"] == v).mean() * 100) for k, v in (("time", 0), ("stop", 1), ("take", 2))},
            "hold_days": float((t["x"] - t["i"]).mean()),
            "pf_total": p.get("total"),
            "pf_annual": p.get("annual"),
            "pf_dd": p.get("max_dd"),
            "pf_trades": p.get("trades"),
        }
        years = pd.Series(net, index=self.M.idx[t["i"]].year)
        out["by_year"] = {str(y): {"n": int(len(v)), "net": float(v.mean())} for y, v in years.groupby(level=0)}
        if with_curve:
            out["curve"] = p.get("curve")
        return out


def dedupe(t):
    """同じ銘柄を保有中に出たシグナルは数えない"""
    order = np.lexsort((t["i"], t["j"]))
    keep = np.zeros(len(order), bool)
    last_j, busy = -1, -1
    ii, jj, xx = t["i"][order], t["j"][order], t["x"][order]
    for n in range(len(order)):
        if jj[n] != last_j:
            last_j, busy = jj[n], -1
        if ii[n] > busy:
            keep[n] = True
            busy = xx[n] - 1
    sel = order[keep]
    return {k: v[sel] for k, v in t.items()}


def score(r):
    """探索期間での評価: 10枠運用の年率 ÷ 最大DD (DDは最低10%として扱う)。条件を満たさなければ -inf"""
    if r.get("n", 0) < MIN_TRADES or (r.get("pf") or 0) < MIN_PF or r.get("pf_annual") is None:
        return -math.inf
    return r["pf_annual"] / max(abs(r["pf_dd"]), 10)


def exit_label(stop, take, days):
    parts = [f"損切り -{stop}ATR" if stop else "損切りなし", f"利確 +{take}ATR" if take else "利確なし",
             f"最大{days}営業日"]
    return " / ".join(parts)


def run(D, funds, demo):
    t0 = time.time()
    T = bt.indicators(D, funds)
    T, idx = extra_indicators(D, T)
    S = Sim(D, T)
    M = S.M
    first_valid = M.idx[min(200, len(M.idx) - 1)]
    split = M.idx[-1] - pd.DateOffset(years=1)
    is_mask = (M.idx >= first_valid) & (M.idx < split)
    # 探索期間の最後のシグナルは、手仕舞いが検証期間に入らないよう10日手前で止める
    last_is = np.nonzero(is_mask)[0].max()
    is_mask[max(0, last_is - 10):] = False
    oos_mask = M.idx >= split
    fams = families(T)
    print(f"indicators ready {time.time() - t0:.0f}s; candidates "
          + " ".join(f"{k}={len(v)}" for k, v in fams.items()))

    res = {"families": {}, "baseline": {}}
    for fam, cands in fams.items():
        # 1) 条件の探索 (10日保有)
        scored = []
        for c in cands:
            r = S.evaluate(S.trades(c["sig"], is_mask, days=10, key=c["key"]))
            scored.append((score(r), c, r))
        scored.sort(key=lambda x: -x[0])
        # 2) 上位について手仕舞いルールの探索
        best = []
        for sc, c, _ in scored[:TOP_PER_FAMILY]:
            if sc == -math.inf:
                continue
            for stop, take, days in itertools.product(STOPS, TAKES, MAX_DAYS):
                r = S.evaluate(S.trades(c["sig"], is_mask, stop, take, days, c["key"]))
                best.append((score(r), c, (stop, take, days), r))
        best.sort(key=lambda x: -x[0])
        # 頑健性: 探索期間の上位10候補 (10日保有) が検証期間でどうだったか
        robust = []
        for sc, c, r_is in scored[:10]:
            if sc == -math.inf:
                continue
            r_oos = S.evaluate(S.trades(c["sig"], oos_mask, days=10, key=c["key"]))
            robust.append({"cond": c["cond"], "is_net": r_is.get("net"), "is_pf": r_is.get("pf"),
                           "oos_net": r_oos.get("net"), "oos_pf": r_oos.get("pf"), "oos_n": r_oos.get("n")})
        F = {"name": FAMILY_NAMES[fam], "tested": len(cands), "passed": sum(1 for s in scored if s[0] > -math.inf),
             "robust": robust}
        if best:
            sc, c, (stop, take, days), r_is = best[0]
            r_is = S.evaluate(S.trades(c["sig"], is_mask, stop, take, days, c["key"]), with_curve=True)
            r_oos = S.evaluate(S.trades(c["sig"], oos_mask, stop, take, days, c["key"]), with_curve=True)
            r_oos10 = S.evaluate(S.trades(c["sig"], oos_mask, days=10, key=c["key"]))
            F.update({"cond": c["cond"], "exit": {"stop": stop, "take": take, "days": days,
                                                  "label": exit_label(stop, take, days)},
                      "score": sc, "is": r_is, "oos": r_oos, "oos_hold10": r_oos10,
                      "exit_grid": [{"label": exit_label(*ex), "score": s, "net": r.get("net"), "pf": r.get("pf"),
                                     "annual": r.get("pf_annual"), "dd": r.get("pf_dd")}
                                    for s, cc, ex, r in best[:8] if cc is c]})
            print(f"[{fam}] best IS score={sc:.2f} {c['cond']} {exit_label(stop, take, days)}\n"
                  f"   IS net={r_is['net']:+.2f} pf={r_is['pf']:.2f} ann={r_is['pf_annual']:+.1f} dd={r_is['pf_dd']:.1f}"
                  f" | OOS n={r_oos.get('n')} net={r_oos.get('net')} pf={r_oos.get('pf')} "
                  f"ann={r_oos.get('pf_annual')} dd={r_oos.get('pf_dd')}")
        res["families"][fam] = F

    # 元の6条件 + ユーザーのATRルール (損切り1.5 / 利確2.5 / 10日) を同じ期間で
    for cid, (tech, _, key) in bt.conditions(T).items():
        sig = tech.fillna(False).astype(bool)
        r_is = S.evaluate(S.trades(sig, is_mask, 1.5, 2.5, 10, key))
        r_oos = S.evaluate(S.trades(sig, oos_mask, 1.5, 2.5, 10, key))
        r_oos10 = S.evaluate(S.trades(sig, oos_mask, days=10, key=key))
        res["baseline"][cid] = {"name": bt.COND_NAMES[cid], "is": r_is, "oos": r_oos, "oos_hold10": r_oos10}

    mkt = pd.Series(idx.to_numpy(), index=M.idx)
    res["meta"] = {
        "demo": demo,
        "generated_at": dt.datetime.now(bt.JST).isoformat(timespec="minutes"),
        "is_start": M.idx[is_mask].min().strftime("%Y-%m-%d"),
        "is_end": M.idx[is_mask].max().strftime("%Y-%m-%d"),
        "oos_start": M.idx[oos_mask].min().strftime("%Y-%m-%d"),
        "oos_end": M.idx[-1].strftime("%Y-%m-%d"),
        "tickers": len(M.cols),
        "min_trades": MIN_TRADES, "min_pf": MIN_PF, "slots": bt.SLOTS,
        "stops": STOPS, "takes": TAKES, "max_days": MAX_DAYS,
        "cost_tiers": bt.COST_TIERS, "commission": bt.COMMISSION, "tax": bt.TAX,
        "mkt_is": float(mkt[is_mask].iloc[-1] / mkt[is_mask].iloc[0] * 100 - 100),
        "mkt_oos": float(mkt[oos_mask].iloc[-1] / mkt[oos_mask].iloc[0] * 100 - 100),
        "elapsed_s": round(time.time() - t0),
    }
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--cache", default=None)
    args = ap.parse_args()
    D = bt.load_prices_demo(n_days=1100) if args.demo else bt.load_prices_real(Path(args.cache) if args.cache else None)
    D = bt.drop_partial_day(D)
    funds = bt.load_fundamentals(D["Close"].columns, args.demo)
    res = run(D, funds, args.demo)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    name = "optimize_demo.json" if args.demo else "optimize.json"
    (OUT_DIR / name).write_text(json.dumps(bt.clean(res), ensure_ascii=False, indent=1, allow_nan=False),
                                encoding="utf-8")
    print(f"wrote {OUT_DIR / name}")


if __name__ == "__main__":
    main()
