"""購入優先度の3パターンを、ユーザー指定の新しい取引ルールで比べる (2026-10-30 ユーザー指定)。

    1. SCORE業績 + ステージ(75)   両者 (0〜100) の平均
    2. SCORE勢い
    3. SCORE業績 + SCORE勢い      両者の平均

売買ルール: 翌営業日の寄りで買い、損切り -6% / +20%で半分・+40%で残り / 最大25営業日 / 最大10銘柄。
使い方は2通り:
    A. 毎日、売買代金1億円以上の銘柄から優先度の上位10銘柄を候補にする
    B. 改良版押し目・複合のシグナルが10枠を超えたときの優先順位
比較: ランダム (5回平均)・売買代金が大きい順・日経平均 (配当なし)。

SCORE業績の注意: 財務の過去値は無料で取れないので、ROE・自己資本比率・売上成長率・利益成長は現在の値で代用する
(先読みあり)。PERと配当利回りだけはその日の株価で計算し直す。SCORE勢いはその日の株価だけで作れる。

使い方:
    python backtest/priority3_test.py --cache bt_cache/prices.pkl
    python backtest/priority3_test.py --demo

出力:
    backtest/results/priority3.json
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
import score_momentum_test as smt  # noqa: E402
from build_data import dividend_yield  # noqa: E402  (run_backtest が screener を path に入れている)

TOP_N = 10
RANDOM_SEEDS = 5
MIN_COVERAGE = 4


def coverage_avg(parts, n_min=MIN_COVERAGE):
    """build_data.score_stocks と同じ: 欠けた項目は0.5で補い、n_min 項目以上そろった銘柄だけ平均 × 100"""
    have = sum(p.notna().astype(int) for p in parts)
    avg = sum(p.fillna(0.5) for p in parts) / len(parts) * 100
    return avg.where(have >= n_min)


def business_score(D, funds, live):
    """SCORE業績: PER(低いほど良い・赤字は最下位)・ROE・自己資本比率・配当利回り・売上成長率・利益成長予想"""
    C = D["Close"]
    cols = C.columns
    f = lambda k: pd.Series({c: (funds.get(c) or {}).get(k) for c in cols}, dtype=float)
    const = lambda s: pd.DataFrame(np.broadcast_to(s.to_numpy(), C.shape), index=C.index, columns=cols)
    px_now = f("mc_raw") / f("shares")
    per = C / (px_now / f("per"))
    per = per.where(per > 0, 9999).where(per.notna())
    div = pd.Series({c: dividend_yield(funds.get(c) or {}, 1.0) for c in cols}, dtype=float) / 100  # 1株の年間配当
    dy = C.rdiv(div)
    ef, et = f("eps_fwd"), f("eps_trail")
    egf = ((ef / et - 1) * 100).where(et > 0).fillna(f("eg"))
    rank = lambda v, hi=True: (lambda r: r if hi else 1 - r)(v.where(live).rank(axis=1, pct=True))
    parts = [rank(per, False), rank(const(f("roe"))), rank(const(f("er"))), rank(dy),
             rank(const(f("rg"))), rank(const(egf))]
    return coverage_avg(parts)


def run(D, funds, demo):
    t0 = time.time()
    T = bt.indicators(D, funds)
    T, idx = op.extra_indicators(D, T)
    rt.RULE = smt.NEW_RULE  # RuleSim は rule_test.RULE を参照する
    S = rt.RuleSim(D, T)
    M = S.M
    C = D["Close"]
    live = C.notna() & (C.rolling(85).count() >= 85)
    liquid = live & (T["to"] >= 1)

    P = smt.momentum_parts(D, T)
    sf = business_score(D, funds, live)
    sm = coverage_avg([P[k] for k in ("stage75", "pullback", "r3m", "to", "rsi")])
    keys = {
        "p1": ("SCORE業績 + ステージ(75)", (sf + P["stage75"] * 100) / 2),
        "p2": ("SCORE勢い", sm),
        "p3": ("SCORE業績 + SCORE勢い", (sf + sm) / 2),
        "to": ("売買代金が大きい順 (参考)", T["to"]),
    }
    keys = {k: (n, v.where(liquid)) for k, (n, v) in keys.items()}
    rng = np.random.default_rng(0)
    rand = [pd.DataFrame(rng.random(M.C.shape), index=M.idx, columns=M.cols).where(liquid) for _ in range(RANDOM_SEEDS)]

    first_valid = M.idx[min(250, len(M.idx) - 1)]
    split = M.idx[-1] - pd.DateOffset(years=1)
    periods = {"is": (M.idx >= first_valid) & (M.idx < split), "oos": M.idx >= split, "all": M.idx >= first_valid}
    last_is = np.nonzero(periods["is"])[0].max()
    periods["is"][max(0, last_is - smt.NEW_RULE["max_days"]):] = False
    years = {k: max((M.idx[m].max() - M.idx[m].min()).days / 365.25, 0.1) for k, m in periods.items()}
    print(f"ready {time.time() - t0:.0f}s")

    def evaluate(sig, key, pid):
        t = S.rule_trades(sig, periods[pid], key)
        if t is None or len(t["i"]) == 0:
            return {"n": 0, "win": None, "net": None, "total": None, "annual": None, "max_dd": None}
        net = t["net"]
        p = S.portfolio(t, years[pid]) or {}
        return {"n": int(len(net)), "win": float((net > 0).mean() * 100), "net": float(net.mean()),
                "total": p.get("total"), "annual": p.get("annual"), "max_dd": p.get("max_dd"),
                "curve": p.get("curve") if pid == "all" else None}

    def evaluate_random(make_sig, pid):
        rs = [evaluate(make_sig(r), r, pid) for r in rand]
        return {m: float(np.nanmean([x[m] if x[m] is not None else np.nan for x in rs]))
                for m in ("n", "win", "net", "total", "annual", "max_dd")}

    up = (idx > idx.rolling(rt.REGIME_MA).mean()).to_numpy(bool)
    UP = pd.DataFrame(np.repeat(up[:, None], len(M.cols), axis=1), index=M.idx, columns=M.cols)
    st = rt.strategies(T)
    uses = {
        "top10": ("毎日 優先度の上位10銘柄", None),
        "pullback_v2": ("改良版 押し目シグナルの中の優先順位", st["pullback_v2"]["sig"]),
        "composite": ("複合シグナル (上げ: 改良版押し目 / 下げ: 急落リバウンド) の中の優先順位",
                      (st["pullback_v2"]["sig"] & UP) | (st["crash_rebound"]["sig"] & ~UP)),
    }
    res = {"keys": {k: v[0] for k, v in keys.items()}, "uses": {}}
    res["keys"]["random"] = f"ランダム ({RANDOM_SEEDS}回の平均)"
    for uid, (uname, sig) in uses.items():
        out = {}
        for kid, (_, key) in keys.items():
            s_ = smt.top_signal(key, TOP_N) if sig is None else sig
            out[kid] = {pid: evaluate(s_, key.fillna(-1), pid) for pid in periods}
        out["random"] = {pid: evaluate_random((lambda r: smt.top_signal(r, TOP_N)) if sig is None else (lambda r: sig), pid)
                         for pid in periods}
        for kid, row in out.items():
            print(f"[{uid}] {kid}: IS ann={row['is']['annual']} dd={row['is']['max_dd']} | OOS tot={row['oos']['total']} "
                  f"dd={row['oos']['max_dd']} | ALL ann={row['all']['annual']} dd={row['all']['max_dd']}")
        res["uses"][uid] = {"name": uname, "keys": out}

    nk = rt.nikkei(M.idx[0].strftime("%Y-%m-%d"), None, demo)
    res["nikkei"] = {pid: rt.bench(nk, M.idx[m].min(), M.idx[m].max()) for pid, m in periods.items()}
    print("nikkei", {pid: (v or {}).get("total") for pid, v in res["nikkei"].items()})
    res["meta"] = {
        "demo": demo, "generated_at": dt.datetime.now(bt.JST).isoformat(timespec="minutes"),
        "periods": {k: [M.idx[m].min().strftime("%Y-%m-%d"), M.idx[m].max().strftime("%Y-%m-%d")]
                    for k, m in periods.items()},
        "rule": smt.NEW_RULE, "slots": rt.SLOTS, "top_n": TOP_N, "elapsed_s": round(time.time() - t0),
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
    name = "priority3_demo.json" if args.demo else "priority3.json"
    (bt.OUT_DIR / name).write_text(json.dumps(bt.clean(res), ensure_ascii=False, indent=1, allow_nan=False),
                                   encoding="utf-8")
    print(f"wrote {bt.OUT_DIR / name}")


if __name__ == "__main__":
    main()
