"""スイングSCORE (トレンド・押し目度・RSI・売買代金) が、10日前後のスイング取引の銘柄選びに効くかを確かめる。

スイングSCOREの定義は screener/build_data.py の swing_parts と同じ (その日に分かる株価の値だけで作る):
    トレンド   75日線が20日前より上 / 株価>75日線 / 25日線>75日線 の当てはまる割合
    押し目度   25日線乖離が -2〜+2% なら満点 (外れるほど減点、5ポイント外で0)
               20日高値からの下落が -3〜-20% なら満点 (同上)  の平均
    RSI        RSI(14) 40以下で満点、60以上で0
    売買代金   20日平均売買代金のパーセンタイル (1億円以上の銘柄の中で)
    4つの平均を 0〜100 で表す。売買代金1億円未満は対象外。決算の日付は過去分が無いので検証には入れていない。

比べ方 (探索期間で比べ、直近1年 = 検証期間でも同じ並びになるかを見る):
    1. 分位分析: 毎日スコアで10分位に分け、翌日寄りで買い10日後の大引けで売った損益の平均
    2. 上位10銘柄: 毎日スコア上位10銘柄を買う (10枠運用)。ユーザーの売買ルール (損切り1.5ATR・利確2ATR・最大10日)
       と、損切り・利確なしの10日保有の2通り
    3. 優先順位: 改良版押し目・複合のシグナルが10枠を超えたとき、どの順で買うか

使い方:
    python backtest/swing_score_test.py --cache bt_cache/prices.pkl
    python backtest/swing_score_test.py --demo

出力:
    backtest/results/swing_score.json
"""
from __future__ import annotations

import argparse
import datetime as dt
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

TOP_N = 10
HOLD = 10
EXITS = {"atr": ("損切り1.5ATR・利確2ATR・最大10日", 1.5, 2.0), "hold10": ("10日保有 (損切り・利確なし)", None, None)}
RANDOM_SEEDS = 5


def band(x, lo, hi, w):
    """lo〜hi なら1、範囲から w 離れると0になる台形"""
    d = np.maximum(np.maximum(lo - x, x - hi), 0)
    return (1 - d / w).clip(0, 1).where(x.notna())


def swing_parts(D, T):
    C = D["Close"]
    liquid = T["to"] >= 1
    parts = {
        "trend": (T["ma75_up"].astype(float) + T["above75"].astype(float) + T["ma25_over75"].astype(float)) / 3,
        "pullback": (band(T["dev25"], -2, 2, 5) + band(T["off20"], -20, -3, 5)) / 2,
        "rsi": ((60 - T["rsi"]) / 20).clip(0, 1),
        "liq": T["to"].where(liquid).rank(axis=1, pct=True),
    }
    ok = C.rolling(75).count() >= 75  # 75日線が計算できる銘柄だけ
    return {k: v.where(liquid & ok) for k, v in parts.items()}


def swing_scores(parts):
    avg = lambda ks: sum(parts[k] for k in ks) / len(ks) * 100
    return {
        "swing": ("スイングSCORE (4要素)", avg(["trend", "pullback", "rsi", "liq"])),
        "swing_noliq": ("スイングSCORE 売買代金なし (3要素)", avg(["trend", "pullback", "rsi"])),
        "swing_tp": ("トレンド+押し目度だけ", avg(["trend", "pullback"])),
    }


def deciles(score, fwd, mask):
    """日ごとにスコアで10分位に分け、各分位の10日後損益 (コスト前) の平均を出す"""
    s = score[mask]
    f = fwd[mask].where(s.notna())
    q = s.rank(axis=1, pct=True)
    out = []
    for d in range(10):
        m = (q > d / 10) & (q <= (d + 1) / 10)
        v = f.where(m)
        out.append(float(np.nanmean(v.to_numpy())) if m.to_numpy().any() else None)
    return out


def top_signal(score, n):
    return score.rank(axis=1, ascending=False, method="first") <= n


def run(D, funds, demo):
    t0 = time.time()
    T = bt.indicators(D, funds)
    T, idx = op.extra_indicators(D, T)
    _, score_pit = rt.history_score(D, T, funds)
    P = swing_parts(D, T)
    SW = swing_scores(P)
    S = op.Sim(D, T)
    M = S.M
    first_valid = M.idx[min(250, len(M.idx) - 1)]
    split = M.idx[-1] - pd.DateOffset(years=1)
    periods = {"is": (M.idx >= first_valid) & (M.idx < split), "oos": M.idx >= split}
    last_is = np.nonzero(periods["is"])[0].max()
    periods["is"][max(0, last_is - HOLD):] = False
    years = {k: max((M.idx[m].max() - M.idx[m].min()).days / 365.25, 0.1) for k, m in periods.items()}

    liquid = (T["to"] >= 1) & (D["Close"].rolling(75).count() >= 75)
    rng = np.random.default_rng(0)
    rand = [pd.DataFrame(rng.random(M.C.shape), index=M.idx, columns=M.cols).where(liquid)
            for _ in range(RANDOM_SEEDS)]
    keys = {**SW,
            "score_pit": ("当時スコア (3ヶ月騰落率・52週位置・売買代金)", score_pit.where(liquid)),
            "to": ("売買代金が大きい順", T["to"].where(liquid)),
            "r3m": ("3ヶ月騰落率が高い順", T["r3m"].where(liquid))}
    print(f"ready {time.time() - t0:.0f}s")

    res = {"keys": {k: v[0] for k, v in keys.items()}, "parts": {
        "trend": "トレンド", "pullback": "押し目度", "rsi": "RSI", "liq": "売買代金"}}
    res["keys"]["random"] = f"ランダム ({RANDOM_SEEDS}回の平均)"

    # 1) 分位分析 (翌日寄り→10日後大引け, コスト前)
    O, C = D["Open"], D["Close"]
    fwd = (C.shift(-HOLD) / O.shift(-1) - 1) * 100
    fwd = fwd.where(fwd.abs() <= rt.MAX_ABS_RET)
    res["deciles"] = {}
    for kid, (_, sc) in {**keys, **{f"part_{k}": (None, v) for k, v in P.items()}}.items():
        res["deciles"][kid] = {pid: deciles(sc, fwd, pd.Series(m, index=M.idx)) for pid, m in periods.items()}
        d = res["deciles"][kid]
        print(f"[decile] {kid}: IS top={d['is'][9]} bottom={d['is'][0]} | OOS top={d['oos'][9]} bottom={d['oos'][0]}")

    def evaluate(sig, key, pid, stop, take):
        r = S.evaluate(S.trades(sig, periods[pid], stop, take, HOLD, key), years=years[pid])
        return {m: r.get(m) for m in ("n", "win", "net", "pf", "pf_total", "pf_annual", "pf_dd")}

    def evaluate_random(make_sig, pid, stop, take):
        rs = [evaluate(make_sig(r), r, pid, stop, take) for r in rand]
        return {m: float(np.nanmean([x[m] if x[m] is not None else np.nan for x in rs]))
                for m in rs[0]}

    # 2) スコア上位10銘柄を毎日の候補にする
    res["top"] = {}
    for eid, (_, stop, take) in EXITS.items():
        out = {}
        for kid, (_, sc) in keys.items():
            out[kid] = {pid: evaluate(top_signal(sc, TOP_N), sc, pid, stop, take) for pid in periods}
        out["random"] = {pid: evaluate_random(lambda r: top_signal(r, TOP_N), pid, stop, take) for pid in periods}
        for kid, row in out.items():
            print(f"[top10 {eid}] {kid}: IS net={row['is']['net']} ann={row['is']['pf_annual']} "
                  f"| OOS net={row['oos']['net']} tot={row['oos']['pf_total']} dd={row['oos']['pf_dd']}")
        res["top"][eid] = out

    # 3) 既存シグナルの優先順位
    up = (idx > idx.rolling(rt.REGIME_MA).mean()).to_numpy(bool)
    UP = pd.DataFrame(np.repeat(up[:, None], len(M.cols), axis=1), index=M.idx, columns=M.cols)
    st = rt.strategies(T)
    targets = {
        "pullback_v2": ("改良版 押し目", st["pullback_v2"]["sig"]),
        "composite": ("複合 (上げ: 改良版押し目 / 下げ: 急落リバウンド)",
                      (st["pullback_v2"]["sig"] & UP) | (st["crash_rebound"]["sig"] & ~UP)),
    }
    res["priority"] = {}
    for tid, (tname, sig) in targets.items():
        per_exit = {}
        for eid, (_, stop, take) in EXITS.items():
            out = {kid: {pid: evaluate(sig, sc.fillna(-1), pid, stop, take) for pid in periods}
                   for kid, (_, sc) in keys.items() if kid in ("swing", "swing_noliq", "to", "score_pit")}
            out["random"] = {pid: evaluate_random(lambda r: sig, pid, stop, take) for pid in periods}
            for kid, row in out.items():
                print(f"[{tid} {eid}] {kid}: IS ann={row['is']['pf_annual']} dd={row['is']['pf_dd']} "
                      f"| OOS tot={row['oos']['pf_total']} dd={row['oos']['pf_dd']}")
            per_exit[eid] = out
        res["priority"][tid] = {"name": tname, "exits": per_exit}

    res["meta"] = {
        "demo": demo, "generated_at": dt.datetime.now(bt.JST).isoformat(timespec="minutes"),
        "periods": {k: [M.idx[m].min().strftime("%Y-%m-%d"), M.idx[m].max().strftime("%Y-%m-%d")]
                    for k, m in periods.items()},
        "exits": {k: v[0] for k, v in EXITS.items()}, "top_n": TOP_N, "slots": bt.SLOTS,
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
    name = "swing_score_demo.json" if args.demo else "swing_score.json"
    (bt.OUT_DIR / name).write_text(json.dumps(bt.clean(res), ensure_ascii=False, indent=1, allow_nan=False),
                                   encoding="utf-8")
    print(f"wrote {bt.OUT_DIR / name}")


if __name__ == "__main__":
    main()
