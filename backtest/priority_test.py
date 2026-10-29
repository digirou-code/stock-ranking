"""10枠が埋まっているときに、どの指標の順で銘柄を選ぶと良いかを比べる。

売買ルールは rule_test.py と同じ (損切り -5% / +25%で半分 / +50%で全部 / 最大30日)。
候補はすべて「その日に分かる値」だけで作る (総合SCOREは参考として並べる)。
探索期間で比べ、直近1年 (検証期間) でも同じ順位が保てるかを見る。

使い方:
    python backtest/priority_test.py --cache bt_cache/prices.pkl
    python backtest/priority_test.py --demo

出力:
    backtest/results/priority.json
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

RANDOM_SEEDS = 5


def keys(D, T, score, score_pit):
    C = D["Close"]
    hi = D["High"].rolling(250, min_periods=60).max()
    lo = D["Low"].rolling(250, min_periods=60).min()
    return {
        "score": ("総合SCORE (参考・財務は現在値)", score),
        "score_pit": ("当時スコア (騰落率・52週位置・売買代金)", score_pit),
        "r3m": ("3ヶ月騰落率が高い順", T["r3m"]),
        "r6m": ("6ヶ月騰落率が高い順", T["r6m"]),
        "mom12_1": ("12ヶ月騰落率 (直近1ヶ月除く) が高い順", C.shift(21) / C.shift(250) - 1),
        "pos52": ("52週レンジ位置が高い順", ((C - lo) / (hi - lo)).where(hi > lo)),
        "near_hi52": ("52週高値に近い順", C / hi),
        "to": ("売買代金が大きい順", T["to"]),
        "vr": ("出来高倍率が高い順", T["vr"]),
        "low_vol": ("値動きが小さい順 (ATR%が低い)", -T["atrp"]),
        "high_vol": ("値動きが大きい順 (ATR%が高い)", T["atrp"]),
        "low_rsi": ("RSIが低い順", -T["rsi"]),
        "deep_dev": ("25日線からの下方乖離が大きい順", -T["dev25"]),
    }


def run(D, funds, demo):
    t0 = time.time()
    T = bt.indicators(D, funds)
    T, idx = op.extra_indicators(D, T)
    score, score_pit = rt.history_score(D, T, funds)
    S = rt.RuleSim(D, T)
    M = S.M
    first_valid = M.idx[min(250, len(M.idx) - 1)]
    split = M.idx[-1] - pd.DateOffset(years=1)
    periods = {"is": (M.idx >= first_valid) & (M.idx < split), "oos": M.idx >= split, "all": M.idx >= first_valid}
    last_is = np.nonzero(periods["is"])[0].max()
    periods["is"][max(0, last_is - rt.RULE["max_days"]):] = False
    years = {k: max((M.idx[m].max() - M.idx[m].min()).days / 365.25, 0.1) for k, m in periods.items()}
    up = (idx > idx.rolling(rt.REGIME_MA).mean()).to_numpy(bool)
    UP = pd.DataFrame(np.repeat(up[:, None], len(M.cols), axis=1), index=M.idx, columns=M.cols)

    st = rt.strategies(T)
    targets = {
        "composite": ("複合 (上げ: 改良版押し目 / 下げ: 急落リバウンド)",
                      (st["pullback_v2"]["sig"] & UP) | (st["crash_rebound"]["sig"] & ~UP)),
        "pullback_v2": ("改良版 押し目", st["pullback_v2"]["sig"]),
        "pullback": ("25日線押し目 (元)", st["pullback"]["sig"]),
        "high_break": ("高値ブレイク (元)", st["high_break"]["sig"]),
    }
    K = keys(D, T, score, score_pit)
    rng = np.random.default_rng(0)
    rand = [pd.DataFrame(rng.random(M.C.shape), index=M.idx, columns=M.cols) for _ in range(RANDOM_SEEDS)]
    print(f"ready {time.time() - t0:.0f}s")

    res = {"targets": {}, "keys": {k: v[0] for k, v in K.items()}}
    res["keys"]["random"] = f"ランダム ({RANDOM_SEEDS}回の平均)"
    for tid, (tname, sig) in targets.items():
        out = {}
        for kid, (_, key) in {**K, "random": (None, None)}.items():
            row = {}
            for pid, mask in periods.items():
                if kid == "random":
                    ps = [S.portfolio(S.rule_trades(sig, mask, r), years[pid]) or {} for r in rand]
                    row[pid] = {m: float(np.mean([p.get(m, np.nan) for p in ps])) for m in ("total", "annual", "max_dd")}
                else:
                    p = S.portfolio(S.rule_trades(sig, mask, key), years[pid]) or {}
                    row[pid] = {m: p.get(m) for m in ("total", "annual", "max_dd")}
            out[kid] = row
            print(f"[{tid}] {kid}: IS ann={row['is']['annual']:+.1f} dd={row['is']['max_dd']:.1f} | "
                  f"OOS tot={row['oos']['total']:+.1f} dd={row['oos']['max_dd']:.1f}")
        res["targets"][tid] = {"name": tname, "keys": out}
    res["meta"] = {
        "demo": demo, "generated_at": dt.datetime.now(bt.JST).isoformat(timespec="minutes"),
        "periods": {k: [M.idx[m].min().strftime("%Y-%m-%d"), M.idx[m].max().strftime("%Y-%m-%d")]
                    for k, m in periods.items()},
        "rule": rt.RULE, "slots": rt.SLOTS, "elapsed_s": round(time.time() - t0),
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
    name = "priority_demo.json" if args.demo else "priority.json"
    (bt.OUT_DIR / name).write_text(json.dumps(bt.clean(res), ensure_ascii=False, indent=1, allow_nan=False),
                                   encoding="utf-8")
    print(f"wrote {bt.OUT_DIR / name}")


if __name__ == "__main__":
    main()
