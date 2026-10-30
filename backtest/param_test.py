"""取引ルールのパラメータ (損切り・利確・保有日数・同時保有数) を見直す。

候補を総当たりし、前半 (探索期間: 直近1年より前) の成績だけで選んで、直近1年 (検証期間) で確かめる。
選ぶときに使うのは先読みのない「株価だけで判定できる」条件だけ:
    SCORE勢い / 25日線押し目 (株価条件) / 乖離リバウンド (株価条件) / 高値ブレイク / MACD・RSI (株価条件)
    ランダム (売買代金1億円以上から無作為、3回の平均)
1つのタブにだけ合う値を選ばないよう、各組み合わせの探索期間の年率を対象ごとに順位にし、その平均順位で選ぶ。
財務条件のあるタブ (成長株・高配当など) は、現行ルールと推奨ルールの比較にだけ参考として載せる。

共通: シグナル翌営業日の寄り付きで買い、決算 (推定日) が保有予定期間に入る銘柄は買わない、コストはスクリーニング検証と同じ。

使い方:
    python backtest/param_test.py --cache bt_cache/prices.pkl
    python backtest/param_test.py --demo
出力:
    backtest/results/params.json
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

CURRENT = {"stop_pct": -6.0, "take1_pct": 20.0, "take2_pct": 40.0, "max_days": 25, "slots": 10}
NONE = 1000.0  # 利確なし
STOPS = [-4.0, -6.0, -8.0, -10.0, -15.0]
TAKES = [(10, 10), (10, 20), (10, 30), (15, 15), (15, 30), (15, 45), (20, 20), (20, 40), (20, 60),
         (30, 30), (30, 60), (30, NONE), (NONE, NONE)]
DAYS = [10, 15, 25, 40, 60]
SLOTS = [5, 8, 10, 15, 20]
RANDOM_SEEDS = 3
RANDOM_POOL = 60
TOP_SHOW = 15


def rule_id(r):
    t = lambda v: "なし" if v >= NONE else f"+{v:.0f}%"
    take = f"{t(r['take1_pct'])}で全部" if r["take1_pct"] == r["take2_pct"] else \
        f"{t(r['take1_pct'])}で半分・{t(r['take2_pct'])}で残り"
    if r["take1_pct"] >= NONE:
        take = "利確なし"
    return f"損切り{r['stop_pct']:.0f}% / {take} / {r['max_days']}日"


def targets(D, T, F, sm, sf):
    """(id, 名前, シグナル, 並び順, 先読みなしか)"""
    by = {s[0]: s for s in sc.screens(D, T, F, sm, sf)}
    to = T["to"]
    gc = sc.macd_gc_age(D["Close"])
    growth_div = ((F["dy"] >= 3.5) & (F["rg"] >= 10) & (F["eg"] >= 10) & (F["roe"] >= 5) & (F["mc"] >= 300))
    return [
        ("sm", "SCORE勢い", by["sm"][3], by["sm"][5], True),
        ("pullback", "25日線押し目 (株価条件)", by["pullback"][4], to, True),
        ("rebound_dev", "乖離リバウンド (株価条件)", by["rebound_dev"][4], to, True),
        ("high_break", "高値ブレイク", by["high_break"][3], by["high_break"][5], True),
        ("macd_rsi", "MACD・RSI (株価条件)", gc.notna() & (T["rsi"] >= 50) & (to >= 1), -gc * 1000 + to.clip(0, 999), True),
        ("sf", "SCORE業績", by["sf"][3], by["sf"][5], False),
        ("growth", "成長株", by["growth"][3], by["growth"][5], False),
        ("dividend", "高配当", by["dividend"][3], by["dividend"][5], False),
        ("growth_div", "成長＋高配当", growth_div, F["dy"], False),
        ("low_per", "低PER", by["low_per"][3], by["low_per"][5], False),
    ]


def run(D, funds, demo):
    t0 = time.time()
    T = bt.indicators(D, funds)
    T, idx = op.extra_indicators(D, T)
    F = sc.fund_frames(D, funds)
    live = sc.alive(D)
    sm = smt.momentum_scores(smt.momentum_parts(D, T))["sm"][1]
    sf = sc.sf_score(F, live)
    rt.RULE = dict(CURRENT)
    S = rt.RuleSim(D, T)
    M = S.M
    first_valid = M.idx[min(250, len(M.idx) - 1)]
    split = M.idx[-1] - pd.DateOffset(years=1)
    periods = {"is": (M.idx >= first_valid) & (M.idx < split), "oos": M.idx >= split, "all": M.idx >= first_valid}
    last_is = np.nonzero(periods["is"])[0].max()
    periods["is"][max(0, last_is - max(DAYS)):] = False  # 探索期間の取引が検証期間にはみ出さない
    years = {k: max((M.idx[m].max() - M.idx[m].min()).days / 365.25, 0.1) for k, m in periods.items()}
    TG = [(tid, name, sig.fillna(False).astype(bool), key, clean) for tid, name, sig, key, clean in targets(D, T, F, sm, sf)]
    sel = [x for x in TG if x[4]]
    rng = np.random.default_rng(0)
    pool = (T["to"] >= 1) & live
    rkeys = [pd.DataFrame(rng.random(M.C.shape), index=M.idx, columns=M.cols).where(pool) for _ in range(RANDOM_SEEDS)]
    # 1日に買えるのは高々10銘柄なので、無作為順の上位だけを候補にして計算を軽くする
    rpools = [k.rank(axis=1, ascending=False) <= RANDOM_POOL for k in rkeys]
    near = {}

    def avoid(days):
        if days not in near:
            near[days] = ~sc.earnings_near(D, funds, days)[0]
        return near[days]

    def evaluate(rule, sig, key, pid, curve=False):
        rt.RULE = {k: rule[k] for k in ("stop_pct", "take1_pct", "take2_pct", "max_days")}
        rt.SLOTS = rule["slots"]
        t = S.rule_trades(sig & avoid(rule["max_days"]), periods[pid], key)
        r = S.summarize(t, years[pid], curve=curve)
        r.pop("by_year", None)
        return r

    def random_eval(rule, pid, curve=False):
        ps = [evaluate(rule, p, k, pid, curve) for p, k in zip(rpools, rkeys)]
        out = {k: float(np.nanmean([p.get(k) if p.get(k) is not None else np.nan for p in ps]))
               for k in ("n", "net", "win", "pf", "pf_total", "pf_annual", "pf_dd", "hold_days")}
        out["exit"] = ps[0].get("exit")
        if curve:
            out["curve"] = ps[0].get("curve")
        return out

    print(f"ready {time.time() - t0:.0f}s")

    # 1) 損切り・利確・保有日数の総当たり (10銘柄)
    grid = []
    for stop, (t1, t2), days in itertools.product(STOPS, TAKES, DAYS):
        rule = {"stop_pct": stop, "take1_pct": float(t1), "take2_pct": float(t2), "max_days": days, "slots": 10}
        row = {"rule": rule, "label": rule_id(rule), "is": {}, "oos": {}}
        for pid in ("is", "oos"):
            for tid, _, sig, key, _ in sel:
                r = evaluate(rule, sig, key, pid)
                row[pid][tid] = {k: r.get(k) for k in ("n", "net", "pf", "pf_annual", "pf_dd", "pf_total")}
            r = random_eval(rule, pid)
            row[pid]["random"] = {k: r.get(k) for k in ("n", "net", "pf", "pf_annual", "pf_dd", "pf_total")}
        grid.append(row)
        print(f"{len(grid)}/{len(STOPS) * len(TAKES) * len(DAYS)} {row['label']}: "
              f"IS ann={np.nanmean([v['pf_annual'] or np.nan for v in row['is'].values()]):+.1f} "
              f"OOS tot={np.nanmean([v['pf_total'] or np.nan for v in row['oos'].values()]):+.1f} "
              f"({time.time() - t0:.0f}s)")

    tids = [x[0] for x in sel] + ["random"]

    def score(pid):
        """各対象の年率を組み合わせ間で順位 (1が最良) にして平均"""
        mat = np.array([[(g[pid][t].get("pf_annual") if g[pid][t].get("pf_annual") is not None else -999)
                         for t in tids] for g in grid])
        ranks = (-mat).argsort(axis=0).argsort(axis=0) + 1
        return ranks.mean(axis=1)

    is_rank, oos_rank = score("is"), score("oos")
    for g, a, b in zip(grid, is_rank, oos_rank):
        g["is_rank"], g["oos_rank"] = float(a), float(b)
        for pid in ("is", "oos"):
            vals = [g[pid][t] for t in tids]
            g[pid]["avg"] = {k: float(np.nanmean([v.get(k) if v.get(k) is not None else np.nan for v in vals]))
                             for k in ("net", "pf", "pf_annual", "pf_dd", "pf_total")}
    order = np.argsort(is_rank)
    best = grid[int(order[0])]["rule"]
    cur_n = next(n for n, g in enumerate(grid) if all(g["rule"][k] == CURRENT[k] for k in ("stop_pct", "take1_pct", "take2_pct", "max_days")))
    print("best IS:", rule_id(best), "| current IS rank", int(np.where(order == cur_n)[0][0]) + 1)

    # 2) 同時保有数 (現行ルールと推奨ルールで)
    slots = {}
    for name, base in (("current", CURRENT), ("best", best)):
        slots[name] = {}
        for n in SLOTS:
            rule = {**base, "slots": n}
            row = {}
            for pid in ("is", "oos"):
                vals = [evaluate(rule, sig, key, pid) for _, _, sig, key, _ in sel] + [random_eval(rule, pid)]
                row[pid] = {k: float(np.nanmean([v.get(k) if v.get(k) is not None else np.nan for v in vals]))
                            for k in ("pf_annual", "pf_dd", "pf_total")}
            slots[name][str(n)] = row
    # 探索期間で年率/最大DD (リスクあたりの伸び) が最も良い数を選ぶ
    mar = {n: v["is"]["pf_annual"] / max(-v["is"]["pf_dd"], 1) for n, v in slots["best"].items()}
    best = {**best, "slots": int(max(mar, key=mar.get))}
    print("best slots:", best["slots"])

    # 3) 現行と推奨の比較 (全対象・直近1年と通し)
    compare = {}
    for tid, name, sig, key, clean in TG:
        compare[tid] = {"name": name, "clean": clean}
        for lab, rule in (("current", CURRENT), ("best", best)):
            compare[tid][lab] = {pid: evaluate(rule, sig, key, pid, curve=(pid == "oos")) for pid in ("is", "oos", "all")}
    compare["random"] = {"name": "ランダム", "clean": True}
    for lab, rule in (("current", CURRENT), ("best", best)):
        compare["random"][lab] = {pid: random_eval(rule, pid, curve=(pid == "oos")) for pid in ("is", "oos", "all")}

    # 4) 推奨値のまわり (1つだけ動かしたときの探索期間・検証期間の平均年率)
    def find(**kw):
        for g in grid:
            if all(g["rule"][k] == v for k, v in kw.items()):
                return {"is": g["is"]["avg"]["pf_annual"], "oos": g["oos"]["avg"]["pf_annual"],
                        "is_dd": g["is"]["avg"]["pf_dd"], "oos_dd": g["oos"]["avg"]["pf_dd"]}
        return None
    b = best
    around = {
        "stop": [{"v": s, **(find(stop_pct=s, take1_pct=b["take1_pct"], take2_pct=b["take2_pct"], max_days=b["max_days"]) or {})} for s in STOPS],
        "take": [{"v": rule_id({**b, "take1_pct": float(t1), "take2_pct": float(t2)}).split(" / ")[1],
                  **(find(stop_pct=b["stop_pct"], take1_pct=float(t1), take2_pct=float(t2), max_days=b["max_days"]) or {})} for t1, t2 in TAKES],
        "days": [{"v": d, **(find(stop_pct=b["stop_pct"], take1_pct=b["take1_pct"], take2_pct=b["take2_pct"], max_days=d) or {})} for d in DAYS],
    }

    nk = rt.nikkei(M.idx[0].strftime("%Y-%m-%d"), None, demo)
    show = [int(n) for n in order[:TOP_SHOW]]
    if cur_n not in show:
        show.append(cur_n)
    return {
        "grid": grid, "show": show, "current_index": cur_n, "is_order": [int(n) for n in order],
        "best": best, "best_label": rule_id(best), "current": CURRENT, "current_label": rule_id(CURRENT),
        "slots": slots, "compare": compare, "around": around,
        "targets": {tid: name for tid, name, *_ in sel} | {"random": "ランダム"},
        "bench": {pid: rt.bench(nk, M.idx[m].min(), M.idx[m].max()) for pid, m in periods.items()},
        "meta": {
            "demo": demo, "generated_at": dt.datetime.now(bt.JST).isoformat(timespec="minutes"),
            "periods": {k: [M.idx[m].min().strftime("%Y-%m-%d"), M.idx[m].max().strftime("%Y-%m-%d")]
                        for k, m in periods.items()},
            "tickers": len(M.cols), "combos": len(grid), "stops": STOPS, "takes": TAKES, "days": DAYS,
            "slot_options": SLOTS, "random_seeds": RANDOM_SEEDS, "cost_tiers": bt.COST_TIERS, "tax": bt.TAX,
            "elapsed_s": round(time.time() - t0),
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
    name = "params_demo.json" if args.demo else "params.json"
    (bt.OUT_DIR / name).write_text(json.dumps(bt.clean(res), ensure_ascii=False, indent=1, allow_nan=False),
                                   encoding="utf-8")
    print(f"wrote {bt.OUT_DIR / name}")


if __name__ == "__main__":
    main()
