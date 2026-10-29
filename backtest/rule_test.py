"""ユーザー指定の売買ルールで各戦略を検証し、上げ相場・下げ相場で戦略を切り替える複合パターンも作る。

売買ルール (2026-10-29 ユーザー指定):
    エントリー  シグナル翌営業日の寄り付き
    損切り      エントリー価格の -5%
    利確        +25% で半分、+50% で残り全部
    最大保有    30営業日 (残りは30日目の大引けで手仕舞い)
    優先順位    10枠が埋まっているときは総合SCOREの高い銘柄から買う

使い方:
    python backtest/rule_test.py --cache bt_cache/prices.pkl
    python backtest/rule_test.py --demo

出力:
    backtest/results/rule_test.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import optimize as op  # noqa: E402
import run_backtest as bt  # noqa: E402

RULE = {"stop_pct": -5.0, "take1_pct": 25.0, "take2_pct": 50.0, "max_days": 30}
SLOTS = bt.SLOTS
MIN_REGIME_TRADES = 100  # 相場別の戦略選びで、その相場での取引がこれ未満なら候補にしない


# ---------------------------------------------------------------- 総合SCORE (過去の各時点で再現)
def history_score(D, T, funds):
    """build_data.score_stocks と同じ11項目・同じ向きのパーセンタイル平均を日ごとに計算する。
    株価を使う項目 (3ヶ月騰落率・52週レンジ位置・売買代金、PER・PBR・配当利回りの株価部分) はその日の値。
    1株利益・1株純資産・配当・ROE・利益率・成長率・負債比率は現在の値しか無いので現在値で代用する。"""
    C = D["Close"]
    cols = C.columns
    f = lambda k: pd.Series({c: (funds.get(c) or {}).get(k) for c in cols}, dtype=float)
    px_now = f("mc_raw") / f("shares")
    eps, bps = px_now / f("per"), px_now / f("pbr")
    const = lambda s: pd.DataFrame(np.broadcast_to(s.to_numpy(), C.shape), index=C.index, columns=cols)
    per = C / eps
    per = per.where(per > 0, 9999).where(per.notna())  # 赤字は最下位扱い
    pbr = (C / bps).where(lambda x: x > 0)
    dy = C.rdiv(f("div_rate")) * 100
    hi, lo = D["High"].rolling(250, min_periods=60).max(), D["Low"].rolling(250, min_periods=60).min()
    pos52 = ((C - lo) / (hi - lo) * 100).where(hi > lo)
    items = [(per, False), (pbr, False), (dy, True), (const(f("roe")), True), (const(f("opm")), True),
             (const(f("rg")), True), (const(f("eg")), True), (const(f("de")), False),
             (T["r3m"], True), (pos52, True), (T["to"], True)]
    live = C.notna()
    ranks, have = [], None
    for v, hi_good in items:
        v = v.where(live)
        r = v.rank(axis=1, pct=True)
        r = r if hi_good else 1 - r
        ranks.append(r)
        h = r.notna().astype(int)
        have = h if have is None else have + h
    filled = sum(r.fillna(0.5) for r in ranks) / len(ranks) * 100
    return filled.where(have >= 8)


# ---------------------------------------------------------------- 戦略
def strategies(T):
    c = bt.conditions(T)
    out = {cid: {"name": bt.COND_NAMES[cid], "group": "元の6条件", "sig": tech.fillna(False).astype(bool)}
           for cid, (tech, _, _) in c.items()}
    trend = T["ma75_up"] & T["above75"] & T["ma25_over75"] & (T["to"] >= 1)
    out["pullback_v2"] = {
        "name": "改良版 押し目", "group": "改良版",
        "cond": ["75日線上向き・株価>75日線・25日線>75日線", "25日線乖離 -2〜+2%", "20日高値から -3〜-20%",
                 "RSI(14) 50未満", "売買代金1億円以上"],
        "sig": trend & bt.btw(T["dev25"], -2, 2) & bt.btw(T["off20"], -20, -3) & (T["rsi"] < 50)}
    out["crash_rebound"] = {
        "name": "急落リバウンド", "group": "改良版",
        "cond": ["市場全体が25日線から -3%以上下落中", "25日線乖離 -10%以下", "前日比プラス",
                 "時価総額300億円以上", "売買代金5億円以上"],
        "sig": T["mkt_dip"] & (T["dev25"] <= -10) & T["up"] & (T["mc"] >= 300) & (T["to"] >= 5)}
    out["macd_turn"] = {
        "name": "MACD反転", "group": "改良版",
        "cond": ["MACDがシグナル線を上抜け", "MACD 0未満", "株価>200日線", "市場が200日線より上", "売買代金1億円以上"],
        "sig": T["macd_cross"] & (T["macd"] < 0) & T["above200"] & T["mkt_above200"] & (T["to"] >= 1)}
    for s in out.values():
        s["sig"] = s["sig"].fillna(False).astype(bool)
    return out


# ---------------------------------------------------------------- 売買の計算
class RuleSim(op.Sim):
    def legs(self, ii, jj, take_pct):
        """損切り -5% / 利確 take_pct / 最大30日 の1本分。戻り値 (約定価格, 手仕舞い日, 理由 0時間 1損切り 2利確)"""
        M, days = self.M, RULE["max_days"]
        e = ii + 1
        ent = M.O[e, jj]
        sp, tp = ent * (1 + RULE["stop_pct"] / 100), ent * (1 + take_pct / 100)
        px, xi = np.full(len(ii), np.nan), np.full(len(ii), -1)
        why, open_ = np.zeros(len(ii), np.int8), np.ones(len(ii), bool)
        last_c = np.full(len(ii), np.nan)
        last_k = e.copy()
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
            fin = np.isfinite(c)
            last_c[fin], last_k[fin] = c[fin], k[fin]
        px[open_], xi[open_] = last_c[open_], last_k[open_]
        return px, xi, why

    def rule_trades(self, sig, period, key):
        M, days = self.M, RULE["max_days"]
        s = sig.to_numpy(bool) & period[:, None] & self.enterable
        s[self.n - days:, :] = False
        ii, jj = np.nonzero(s)
        if len(ii) == 0:
            return None
        ent = M.O[ii + 1, jj]
        pa, xa, wa = self.legs(ii, jj, RULE["take1_pct"])
        pb, xb, wb = self.legs(ii, jj, RULE["take2_pct"])
        cost = self.cost_tab[ii, jj]
        ra, rb = (pa / ent - 1) * 100, (pb / ent - 1) * 100
        g = (ra + rb) / 2
        ok = np.isfinite(g)
        # 手仕舞いの内訳: 損切り / 半分利確 / 全部利確 / 時間切れ
        why = np.where(wb == 2, 3, np.where(wa == 2, 2, np.where((wa == 1) | (wb == 1), 1, 0)))
        kv = key.to_numpy(float)[ii, jj]
        t = {"i": ii[ok], "j": jj[ok], "g": g[ok], "net": (g - cost)[ok], "x": np.maximum(xa, xb)[ok],
             "why": why[ok], "key": kv[ok], "xa": xa[ok], "xb": xb[ok], "ra": (ra - cost)[ok], "rb": (rb - cost)[ok]}
        return op.dedupe(t)

    def portfolio(self, t, years):
        """10枠・複利・毎日の終値で評価。半分利確した分はその日に確定させる"""
        if t is None or len(t["i"]) == 0:
            return None
        M = self.M
        by_day = {}
        for n in range(len(t["i"])):
            by_day.setdefault(int(t["i"][n]), []).append(n)
        cash, held, taken, eq, prev = 100.0, [], 0, {}, 100.0
        first, last_sig = min(by_day) + 1, max(by_day) + 1
        for k in range(first, len(M.idx)):
            for h in [h for h in held if t["x"][h[0]] < k]:
                n, amt = h
                cash += amt / 2 * (1 + t["ra"][n] / 100) + amt / 2 * (1 + t["rb"][n] / 100)
                held.remove(h)
            codes = {t["j"][h[0]] for h in held}
            cands = sorted(by_day.get(k - 1, []), key=lambda n: -(t["key"][n] if np.isfinite(t["key"][n]) else -1e9))
            for n in cands:
                if len(held) >= SLOTS:
                    break
                if t["j"][n] in codes:
                    continue
                amt = min(prev / SLOTS, cash)
                if amt <= 0:
                    break
                cash -= amt
                held.append((n, amt))
                codes.add(t["j"][n])
                taken += 1
            value = cash
            for n, amt in held:
                j, ent = t["j"][n], M.O[t["i"][n] + 1, t["j"][n]]
                c = M.C[k, j]
                cost = t["g"][n] - t["net"][n]
                live = ((c / ent - 1) * 100 - cost) if np.isfinite(c) else 0.0
                for x, r in ((t["xa"][n], t["ra"][n]), (t["xb"][n], t["rb"][n])):
                    value += amt / 2 * (1 + (r if k >= x else live) / 100)
            eq[k] = prev = value
            if not held and k > last_sig:
                break
        s = pd.Series(eq)
        s.index = M.idx[s.index]
        total = s.iloc[-1] - 100
        weekly = s.resample("W-FRI").last().dropna()
        return {
            "trades": taken,
            "total": float(total),
            "annual": float(((s.iloc[-1] / 100) ** (1 / years) - 1) * 100) if s.iloc[-1] > 0 else -100.0,
            "max_dd": float((s / s.cummax().clip(lower=100) - 1).min() * 100),
            "curve": [[d.strftime("%Y-%m-%d"), round(float(v) - 100, 2)] for d, v in weekly.items()],
        }

    def summarize(self, t, years, curve=False):
        if t is None or len(t["i"]) == 0:
            return {"n": 0}
        net = t["net"]
        wins, losses = net[net > 0], net[net <= 0]
        p = self.portfolio(t, years) or {}
        out = {
            "n": int(len(net)),
            "win": float((net > 0).mean() * 100),
            "avg_win": float(wins.mean()) if len(wins) else None,
            "avg_loss": float(losses.mean()) if len(losses) else None,
            "payoff": float(wins.mean() / -losses.mean()) if len(wins) and len(losses) and losses.mean() < 0 else None,
            "net": float(net.mean()),
            "pf": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else None,
            "hold_days": float((t["x"] - t["i"]).mean()),
            "exit": {k: float((t["why"] == v).mean() * 100) for k, v in
                     (("stop", 1), ("half", 2), ("full", 3), ("time", 0))},
            "pf_total": p.get("total"), "pf_annual": p.get("annual"), "pf_dd": p.get("max_dd"),
            "pf_trades": p.get("trades"),
        }
        years_s = pd.Series(net, index=self.M.idx[t["i"]].year)
        out["by_year"] = {str(y): {"n": int(len(v)), "net": float(v.mean())} for y, v in years_s.groupby(level=0)}
        if curve:
            out["curve"] = p.get("curve")
        return out


def regime_stats(t, up_arr):
    """シグナル日の相場 (上げ/下げ) ごとの1回あたり成績"""
    out = {}
    if t is None:
        return {"up": {"n": 0}, "down": {"n": 0}}
    reg = up_arr[t["i"]]
    for name, m in (("up", reg), ("down", ~reg)):
        net = t["net"][m]
        if len(net) == 0:
            out[name] = {"n": 0}
            continue
        w, l = net[net > 0], net[net <= 0]
        out[name] = {"n": int(len(net)), "net": float(net.mean()), "win": float((net > 0).mean() * 100),
                     "pf": float(w.sum() / -l.sum()) if l.sum() < 0 else None}
    return out


def nikkei(start, end, demo):
    """比較用に日経平均の終値を取得 (配当なし)"""
    if demo:
        return None
    try:
        import yfinance as yf
        h = yf.download("^N225", start=start, interval="1d", auto_adjust=True, progress=False)
        c = h["Close"]
        c = c.iloc[:, 0] if isinstance(c, pd.DataFrame) else c
        return c.dropna()
    except Exception as e:
        print(f"nikkei failed: {e}")
        return None


def bench(series, start, end):
    if series is None:
        return None
    s = series[(series.index >= start) & (series.index <= end)]
    if len(s) < 2:
        return None
    yrs = max((s.index[-1] - s.index[0]).days / 365.25, 0.1)
    w = (s / s.iloc[0] * 100 - 100).resample("W-FRI").last().dropna()
    return {"total": float(s.iloc[-1] / s.iloc[0] * 100 - 100),
            "annual": float(((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1) * 100),
            "max_dd": float((s / s.cummax() - 1).min() * 100),
            "curve": [[d.strftime("%Y-%m-%d"), round(float(v), 2)] for d, v in w.items()]}


def run(D, funds, demo):
    t0 = time.time()
    T = bt.indicators(D, funds)
    T, idx = op.extra_indicators(D, T)
    score = history_score(D, T, funds)
    S = RuleSim(D, T)
    M = S.M
    first_valid = M.idx[min(250, len(M.idx) - 1)]
    split = M.idx[-1] - pd.DateOffset(years=1)
    periods = {
        "is": (M.idx >= first_valid) & (M.idx < split),
        "oos": M.idx >= split,
        "all": M.idx >= first_valid,
    }
    # 探索期間のシグナルは、30日後の手仕舞いが検証期間に入らないよう手前で止める
    last_is = np.nonzero(periods["is"])[0].max()
    periods["is"][max(0, last_is - RULE["max_days"]):] = False
    years = {k: max((M.idx[m].max() - M.idx[m].min()).days / 365.25, 0.1) for k, m in periods.items()}
    up = T["mkt_above200"].iloc[:, 0].to_numpy(bool)
    print(f"ready {time.time() - t0:.0f}s")

    strats = strategies(T)
    res = {"strategies": {}, "composite": {}}
    trades = {}
    for sid, s in strats.items():
        R = {"name": s["name"], "group": s["group"], "cond": s.get("cond")}
        for pid, mask in periods.items():
            t = S.rule_trades(s["sig"], mask, score)
            trades[(sid, pid)] = t
            R[pid] = S.summarize(t, years[pid], curve=(pid != "is"))
        R["regime_is"] = regime_stats(trades[(sid, "is")], up)
        R["regime_all"] = regime_stats(trades[(sid, "all")], up)
        res["strategies"][sid] = R
        print(f"{s['name']}: IS net={R['is'].get('net')} OOS net={R['oos'].get('net')} "
              f"OOS ann={R['oos'].get('pf_annual')} dd={R['oos'].get('pf_dd')}")

    # 複合: 探索期間で、上げ相場・下げ相場それぞれ1回あたり期待値が最も高い戦略を選ぶ
    def pick(regime):
        cands = [(sid, R["regime_is"][regime]) for sid, R in res["strategies"].items()
                 if R["regime_is"][regime].get("n", 0) >= MIN_REGIME_TRADES and (R["regime_is"][regime].get("pf") or 0) > 1]
        cands.sort(key=lambda x: -x[1]["net"])
        return cands
    up_c, down_c = pick("up"), pick("down")
    combos = []
    if up_c and down_c:
        combos.append(("best", up_c[0][0], down_c[0][0]))
    if up_c:
        combos.append(("up_only", up_c[0][0], None))  # 下げ相場では買わない
    for name, u, d in combos:
        sig = strats[u]["sig"] & T["mkt_above200"]
        label = f"上げ相場: {strats[u]['name']} / 下げ相場: " + (strats[d]["name"] if d else "買わない")
        if d:
            sig = sig | (strats[d]["sig"] & ~T["mkt_above200"])
        C = {"name": label, "up": u, "down": d}
        for pid, mask in periods.items():
            C[pid] = S.summarize(S.rule_trades(sig, mask, score), years[pid], curve=(pid != "is"))
        res["composite"][name] = C
        print(f"composite {label}: IS net={C['is'].get('net')} OOS net={C['oos'].get('net')} "
              f"OOS ann={C['oos'].get('pf_annual')} dd={C['oos'].get('pf_dd')}")
    res["regime_rank"] = {"up": [[sid, r] for sid, r in up_c], "down": [[sid, r] for sid, r in down_c]}

    # 比較: 日経平均と、売買代金1億円以上の全銘柄の等金額平均
    nk = nikkei(M.idx[0].strftime("%Y-%m-%d"), None, demo)
    mk = pd.Series(idx.to_numpy(), index=M.idx)
    res["bench"] = {}
    for pid, mask in periods.items():
        a, b = M.idx[mask].min(), M.idx[mask].max()
        res["bench"][pid] = {"nikkei": bench(nk, a, b), "market": bench(mk, a, b)}

    res["meta"] = {
        "demo": demo,
        "generated_at": dt.datetime.now(bt.JST).isoformat(timespec="minutes"),
        "periods": {k: [M.idx[m].min().strftime("%Y-%m-%d"), M.idx[m].max().strftime("%Y-%m-%d")]
                    for k, m in periods.items()},
        "tickers": len(M.cols), "rule": RULE, "slots": SLOTS,
        "up_days_pct": {k: float(up[m].mean() * 100) for k, m in periods.items()},
        "cost_tiers": bt.COST_TIERS, "commission": bt.COMMISSION, "tax": bt.TAX,
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
    name = "rule_test_demo.json" if args.demo else "rule_test.json"
    (bt.OUT_DIR / name).write_text(json.dumps(bt.clean(res), ensure_ascii=False, indent=1, allow_nan=False),
                                   encoding="utf-8")
    print(f"wrote {bt.OUT_DIR / name}")


if __name__ == "__main__":
    main()
