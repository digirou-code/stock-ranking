"""日経平均に勝てる「スクリーニング × 取引ルール」の組み合わせを探す。

これまでの検証で日経平均に負けた主な理由は3つ:
    1. 資金が遊んでいる (10枠が埋まらない日が多く、空いた分は現金のまま)
    2. 小型株に偏る (この数年は大型株が強く、日経平均はその恩恵を受けた)
    3. 下げ相場でも買い続ける
そこで、次の5つを組み合わせて総当たりする:
    条件       株価だけで判定できるもの (SCORE勢い・モメンタム系・押し目・ブレイクなど)
    銘柄の範囲 全体 (売買代金1億円以上) / 大型 (売買代金上位300)
    相場       いつでも買う / 日経平均が50日線より上の時だけ / 200日線より上の時だけ
    空き資金   現金 / 日経平均に連動するETFで持つ (買うときに必要な分だけ売る)
    ルール     現行 / 推奨 (損切り-8%・40日) / 長め (損切り-10%・利確なし・60日)
探索期間 (直近1年より前) の「日経平均に対する年率の上乗せ」で選び、直近1年で確かめる。

使い方:
    python backtest/combo_test.py --cache bt_cache/prices.pkl
    python backtest/combo_test.py --demo
出力:
    backtest/results/combo.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import optimize as op  # noqa: E402
import rule_test as rt  # noqa: E402
import run_backtest as bt  # noqa: E402
import score_momentum_test as smt  # noqa: E402
import screens_test as sc  # noqa: E402

RULES = {
    "current": ("現行 (-6% / +20%半分・+40% / 25日)", {"stop_pct": -6.0, "take1_pct": 20.0, "take2_pct": 40.0, "max_days": 25}),
    "pick": ("推奨 (-8% / +20%半分・+40% / 40日)", {"stop_pct": -8.0, "take1_pct": 20.0, "take2_pct": 40.0, "max_days": 40}),
    "long": ("長め (-10% / 利確なし / 60日)", {"stop_pct": -10.0, "take1_pct": 1000.0, "take2_pct": 1000.0, "max_days": 60}),
}
UNIVERSES = {"all": "全体", "large": "大型 (売買代金上位300)"}
REGIMES = {"none": "いつでも", "ma50": "日経が50日線の上", "ma200": "日経が200日線の上"}
PARKS = {"cash": "現金", "nikkei": "日経平均で持つ"}
SLOTS = 10
TOP_N = 10
LARGE_N = 300
TOP_SHOW = 20


def build_signals(D, T, F, sm, sf, U):
    """(id, 名前, シグナル, 並び順)。スコア型は範囲の中の上位 TOP_N"""
    C, H = D["Close"], D["High"]
    by = {s[0]: s for s in sc.screens(D, T, F, sm, sf)}
    gc = sc.macd_gc_age(C)
    hi250 = H.rolling(250, min_periods=120).max()
    mom12_1 = C.shift(21) / C.shift(250) - 1
    to = T["to"]

    def top(score):
        s = score.where(U)
        return (s.rank(axis=1, ascending=False, method="first") <= TOP_N) & s.notna()

    return [
        ("sm", "SCORE勢い 上位10", top(sm), sm),
        ("r6m", "6ヶ月騰落率 上位10", top(T["r6m"]), T["r6m"]),
        ("mom12_1", "12ヶ月騰落率 (直近1ヶ月除く) 上位10", top(mom12_1), mom12_1),
        ("near_hi", "52週高値に近い 上位10 (上昇トレンド中)", top((C / hi250).where(T["ma75_up"] & T["above75"])), C / hi250),
        ("pullback", "25日線押し目 (株価条件)", by["pullback"][4] & U, T["r3m"]),
        ("high_break", "高値ブレイク", by["high_break"][3] & U, T["vr"]),
        ("macd_rsi", "MACD・RSI (株価条件)", gc.notna() & (T["rsi"] >= 50) & (to >= 1) & U, -gc * 1000 + to.clip(0, 999)),
    ]


class ComboSim(rt.RuleSim):
    def portfolio_park(self, t, k0, k1, years, park_ret):
        """SLOTS枠・複利。空き資金は park_ret (日次騰落率、None なら現金) で運用。期間の最初から最後まで評価する"""
        M = self.M
        by_day = {}
        if t is not None:
            for n in range(len(t["i"])):
                by_day.setdefault(int(t["i"][n]), []).append(n)
        cash, held, eq, prev = 100.0, [], {}, 100.0
        for k in range(k0, k1 + 1):
            if t is not None:
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
            if park_ret is not None and k > k0:
                cash *= 1 + park_ret[k]
            value = cash
            for n, amt in held:
                j, ent = t["j"][n], M.O[t["i"][n] + 1, t["j"][n]]
                c = M.C[k, j]
                cost = t["g"][n] - t["net"][n]
                live = ((c / ent - 1) * 100 - cost) if np.isfinite(c) else 0.0
                for x, r in ((t["xa"][n], t["ra"][n]), (t["xb"][n], t["rb"][n])):
                    value += amt / 2 * (1 + (r if k >= x else live) / 100)
            eq[k] = prev = value
        s = pd.Series(eq)
        s.index = M.idx[s.index]
        weekly = s.resample("W-FRI").last().dropna()
        return {
            "total": float(s.iloc[-1] - 100),
            "annual": float(((s.iloc[-1] / 100) ** (1 / years) - 1) * 100) if s.iloc[-1] > 0 else -100.0,
            "max_dd": float((s / s.cummax().clip(lower=100) - 1).min() * 100),
            "curve": [[d.strftime("%Y-%m-%d"), round(float(v) - 100, 2)] for d, v in weekly.items()],
        }


def run(D, funds, demo):
    t0 = time.time()
    rt.SLOTS = SLOTS
    T = bt.indicators(D, funds)
    T, mkt = op.extra_indicators(D, T)
    F = sc.fund_frames(D, funds)
    live = sc.alive(D)
    sm = smt.momentum_scores(smt.momentum_parts(D, T))["sm"][1]
    sf = sc.sf_score(F, live)
    rt.RULE = dict(RULES["pick"][1])
    S = ComboSim(D, T)
    M = S.M
    nk = rt.nikkei(M.idx[0].strftime("%Y-%m-%d"), None, demo)
    nk_s = (nk if nk is not None else mkt).reindex(M.idx).ffill()
    park = nk_s.pct_change().fillna(0).to_numpy()
    first_valid = M.idx[min(250, len(M.idx) - 1)]
    split = M.idx[-1] - pd.DateOffset(years=1)
    periods = {"is": (M.idx >= first_valid) & (M.idx < split), "oos": M.idx >= split, "all": M.idx >= first_valid}
    rng = {k: (int(np.nonzero(m)[0].min()), int(np.nonzero(m)[0].max())) for k, m in periods.items()}
    years = {k: max((M.idx[b] - M.idx[a]).days / 365.25, 0.1) for k, (a, b) in rng.items()}
    bench = {k: rt.bench(nk_s, M.idx[a], M.idx[b]) for k, (a, b) in rng.items()}
    regime = {"none": pd.Series(True, index=M.idx),
              "ma50": nk_s > nk_s.rolling(50).mean(), "ma200": nk_s > nk_s.rolling(200).mean()}
    universes = {"all": (T["to"] >= 1) & live,
                 "large": (T["to"].where(live).rank(axis=1, ascending=False) <= LARGE_N) & (T["to"] >= 1)}
    print(f"ready {time.time() - t0:.0f}s nikkei={'yes' if nk is not None else 'equal-weight fallback'}")

    sigs = {u: build_signals(D, T, F, sm, sf, U) for u, U in universes.items()}
    names = {sid: name for sid, name, _, _ in sigs["all"]}
    near = {}
    rows = []
    combos = list(itertools.product(sigs["all"], UNIVERSES, REGIMES, RULES))
    for (sid, _, _, _), uni, reg, rid in combos:
        _, _, sig, key = next(x for x in sigs[uni] if x[0] == sid)
        rule = RULES[rid][1]
        rt.RULE = dict(rule)
        days = rule["max_days"]
        if days not in near:
            near[days] = ~sc.earnings_near(D, funds, days)[0]
        s = sig.fillna(False).astype(bool) & near[days] & regime[reg].to_numpy()[:, None]
        trades = {}
        for pid, (a, b) in rng.items():
            mask = np.zeros(len(M.idx), bool)
            mask[a:b + 1] = True
            mask[max(a, b - days):] = False  # 期間の終わりまでに手仕舞える分だけ
            trades[pid] = S.rule_trades(s, mask, key)
        for pk in PARKS:
            row = {"sig": sid, "uni": uni, "reg": reg, "rule": rid, "park": pk}
            for pid, (a, b) in rng.items():
                t = trades[pid]
                p = S.portfolio_park(t, a, b, years[pid], park if pk == "nikkei" else None)
                n = 0 if t is None else len(t["net"])
                net = t["net"] if n else np.array([0.0])
                row[pid] = {"total": p["total"], "annual": p["annual"], "max_dd": p["max_dd"], "n": n,
                            "net": float(net.mean()), "win": float((net > 0).mean() * 100),
                            "excess": p["annual"] - bench[pid]["annual"]}
                if pid in ("oos", "all"):
                    row[pid]["curve"] = p["curve"]
            rows.append(row)
        r = rows[-1]
        print(f"{len(rows) // 2}/{len(combos)} {sid}/{uni}/{reg}/{rid}: IS ex={rows[-2]['is']['excess']:+.1f}|{r['is']['excess']:+.1f} "
              f"OOS ex={rows[-2]['oos']['excess']:+.1f}|{r['oos']['excess']:+.1f} ({time.time() - t0:.0f}s)")

    order = sorted(range(len(rows)), key=lambda n: -rows[n]["is"]["excess"])
    for k, n in enumerate(order):
        rows[n]["is_rank"] = k + 1
    oos_order = sorted(range(len(rows)), key=lambda n: -rows[n]["oos"]["excess"])
    for k, n in enumerate(oos_order):
        rows[n]["oos_rank"] = k + 1
    # 要素ごとの効果 (探索・検証で、ほかを全部そろえた平均)
    effects = {}
    for dim in ("sig", "uni", "reg", "rule", "park"):
        effects[dim] = {}
        for v in sorted({r[dim] for r in rows}):
            rs = [r for r in rows if r[dim] == v]
            effects[dim][v] = {pid: {"excess": float(np.mean([r[pid]["excess"] for r in rs])),
                                     "max_dd": float(np.mean([r[pid]["max_dd"] for r in rs])),
                                     "beat": float(np.mean([r[pid]["excess"] > 0 for r in rs]) * 100)}
                               for pid in ("is", "oos", "all")}
    for r in rows:  # 曲線は上位と通常の表示分だけ残す
        if r["is_rank"] > TOP_SHOW:
            for pid in ("oos", "all"):
                r[pid].pop("curve", None)
    return {
        "rows": rows, "is_order": order[:TOP_SHOW], "effects": effects, "bench": bench,
        "labels": {"sig": names, "uni": UNIVERSES, "reg": REGIMES, "rule": {k: v[0] for k, v in RULES.items()}, "park": PARKS},
        "meta": {
            "demo": demo, "generated_at": dt.datetime.now(bt.JST).isoformat(timespec="minutes"),
            "periods": {k: [M.idx[a].strftime("%Y-%m-%d"), M.idx[b].strftime("%Y-%m-%d")] for k, (a, b) in rng.items()},
            "tickers": len(M.cols), "combos": len(rows), "slots": SLOTS, "top_n": TOP_N, "large_n": LARGE_N,
            "nikkei": nk is not None, "cost_tiers": bt.COST_TIERS, "tax": bt.TAX, "elapsed_s": round(time.time() - t0),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--cache", default=None)
    args = ap.parse_args()
    D = bt.load_prices_demo(n_days=1100) if args.demo else bt.load_prices_real(Path(args.cache) if args.cache else None)
    D = bt.drop_partial_day(D)
    funds = bt.load_fundamentals(D["Close"].columns, args.demo)
    if args.demo:
        rng = np.random.default_rng(3)
        for c, f in funds.items():
            f.update({"er": rng.normal(45, 15), "div_trail": abs(rng.normal(20, 15)),
                      "earn_ts": (dt.datetime(2025, 8, 10) + dt.timedelta(days=int(rng.integers(0, 90)))).timestamp()})
    res = run(D, funds, args.demo)
    name = "combo_demo.json" if args.demo else "combo.json"
    (bt.OUT_DIR / name).write_text(json.dumps(bt.clean(res), ensure_ascii=False, indent=1, allow_nan=False),
                                   encoding="utf-8")
    print(f"wrote {bt.OUT_DIR / name}")


if __name__ == "__main__":
    main()
