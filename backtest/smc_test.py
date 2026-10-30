"""SMC / ICT の考え方を日足のスクリーニング条件にして検証する。

日足で機械的に判定できるように、次のように定義する (すべてその日までに分かる値だけを使う):
    スイング高値/安値  前後 k 本より高い (安い) 足。右側 k 本が出そろった日に確定とみなす
    上昇構造 / 下降構造  直近2つのスイング高値・安値が切り上がっている / 切り下がっている
    BOS (上)      上昇構造のまま、終値が直近のスイング高値を初めて上抜けた日
    CHoCH (上)    下降構造だったところで、終値が直近のスイング高値を初めて上抜けた日 (流れの転換)
    流動性スイープ  安値が直近のスイング安値を割り込んだが、終値はその上に戻した陽線
    FVG (上)      3本の足で、1本目の高値 < 3本目の安値 となった空白 (1本目高値〜3本目安値)
    オーダーブロック  BOS の直前にあった最後の陰線の値幅
    ディスカウント  BOS 後のレンジ (BOS 前のスイング安値〜その後の高値) の下半分

条件 (いずれも売買代金1億円以上):
    choch          CHoCH
    bos            BOS
    sweep          流動性スイープ
    sweep_choch    10日以内のスイープのあと CHoCH (ICT の典型的な形)
    fvg            BOS から20日以内にできた FVG に、はじめて戻ってきて陽線
    ob             BOS から20日以内に、オーダーブロックへはじめて戻ってきて陽線
    discount       BOS から30日以内に、ディスカウントまで押してから陽線
    discount_fvg   discount かつ fvg (または ob)
スイング判定の k (2 / 3 / 5) を探索期間で選び、直近1年で確かめる。
取引ルールは推奨 (損切り-8% / +20%で半分・+40%で残り / 40営業日 / 10銘柄 / 決算回避)。
並び順は売買代金の大きい順 (これまでの検証で当時値の中では安定していたため)。

使い方:
    python backtest/smc_test.py --cache bt_cache/prices.pkl
    python backtest/smc_test.py --demo
出力:
    backtest/results/smc.json
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
import combo_test as ct  # noqa: E402
import optimize as op  # noqa: E402
import rule_test as rt  # noqa: E402
import run_backtest as bt  # noqa: E402
import score_momentum_test as smt  # noqa: E402
import screens_test as sc  # noqa: E402

RULE = {"stop_pct": -8.0, "take1_pct": 20.0, "take2_pct": 40.0, "max_days": 40}
SLOTS = 10
KS = [2, 3, 5]
SWEEP_DAYS = 10
ZONE_DAYS = 20
DISCOUNT_DAYS = 30
RANDOM_SEEDS = 3
NAMES = {
    "choch": "CHoCH (下降からの転換)",
    "bos": "BOS (上昇の継続)",
    "sweep": "流動性スイープ (安値割れからの戻し)",
    "sweep_choch": "スイープ → CHoCH",
    "fvg": "BOS後の FVG に押し目",
    "ob": "BOS後のオーダーブロックに押し目",
    "discount": "BOS後のディスカウントで押し目",
    "discount_zone": "ディスカウント かつ FVG/オーダーブロック",
}


def swings(D, k):
    """確定したスイング高値・安値 (直近と1つ前) を、確定した日から先へ広げる"""
    H, L = D["High"], D["Low"]
    w = 2 * k + 1
    piv_h = H.shift(k) == H.rolling(w).max()
    piv_l = L.shift(k) == L.rolling(w).min()
    sh = H.shift(k).where(piv_h)
    sl = L.shift(k).where(piv_l)
    last_sh, last_sl = sh.ffill(), sl.ffill()
    prev_sh = last_sh.shift(1).where(sh.notna()).ffill()
    prev_sl = last_sl.shift(1).where(sl.notna()).ffill()
    return last_sh, prev_sh, last_sl, prev_sl


def first_touch(L, top, event, max_age):
    """event の日にできたゾーン (上端 top) に、event の翌日以降はじめて安値が届いた日。戻り値 (touch, ゾーン下端用の age)"""
    touch = pd.DataFrame(False, index=L.index, columns=L.columns)
    age = pd.DataFrame(np.nan, index=L.index, columns=L.columns)
    for a in range(1, max_age + 1):
        ev = event.shift(a, fill_value=False)
        t = top.shift(a)
        hit = ev & (L <= t)
        if a > 1:
            hit &= L.shift(1).rolling(a - 1).min() > t
        new = hit & ~touch
        age = age.where(~new, a)
        touch |= new
    return touch, age


def signals(D, T, k):
    O, H, L, C = D["Open"], D["High"], D["Low"], D["Close"]
    last_sh, prev_sh, last_sl, prev_sl = swings(D, k)
    up = (last_sh > prev_sh) & (last_sl > prev_sl)
    down = (last_sh < prev_sh) & (last_sl < prev_sl)
    brk = (C > last_sh) & (C.shift(1) <= last_sh.shift(1))
    bull = C > O
    liq = T["to"] >= 1
    choch = brk & down.shift(1, fill_value=False)
    bos = brk & up.shift(1, fill_value=False)
    sweep = (L < last_sl) & (C > last_sl) & bull
    recent = lambda s, n: s.astype(float).rolling(n, min_periods=1).max() > 0
    sweep_choch = choch & recent(sweep, SWEEP_DAYS).shift(1, fill_value=False)

    # FVG: 1本目の高値 < 3本目の安値。BOS から ZONE_DAYS 以内にできたものだけ
    fvg_ev = (L > H.shift(2)) & recent(bos, ZONE_DAYS)
    f_touch, f_age = first_touch(L, L.where(fvg_ev), fvg_ev, ZONE_DAYS)
    f_bot = pd.DataFrame(np.nan, index=C.index, columns=C.columns)
    for a in range(1, ZONE_DAYS + 1):
        f_bot = f_bot.where(f_age != a, H.shift(a + 2))
    fvg = f_touch & (C >= f_bot) & bull

    # オーダーブロック: BOS の直前の最後の陰線
    bear = C < O
    ob_h, ob_l = H.where(bear).shift(1).ffill(), L.where(bear).shift(1).ffill()
    o_touch, o_age = first_touch(L, ob_h.where(bos), bos, ZONE_DAYS)
    o_bot = pd.DataFrame(np.nan, index=C.index, columns=C.columns)
    for a in range(1, ZONE_DAYS + 1):
        o_bot = o_bot.where(o_age != a, ob_l.shift(a))
    ob = o_touch & (C >= o_bot) & bull

    # ディスカウント: BOS 時点のスイング安値〜その後の高値 の下半分
    lo = last_sl.where(bos).ffill()
    ages = pd.DataFrame(np.nan, index=C.index, columns=C.columns)
    for a in range(DISCOUNT_DAYS, -1, -1):
        ages = ages.where(~bos.shift(a, fill_value=False), a)
    hi = pd.DataFrame(np.nan, index=C.index, columns=C.columns)
    for a in range(1, DISCOUNT_DAYS + 1):
        hi = hi.where(ages != a, H.rolling(a + 1).max())
    mid = lo + (hi - lo) / 2
    discount = (ages >= 1) & (L <= mid) & (C > lo) & bull
    out = {"choch": choch, "bos": bos, "sweep": sweep, "sweep_choch": sweep_choch, "fvg": fvg, "ob": ob,
           "discount": discount, "discount_zone": discount & (fvg | ob)}
    return {kk: (v.fillna(False).astype(bool) & liq) for kk, v in out.items()}


def run(D, funds, demo):
    t0 = time.time()
    rt.RULE, rt.SLOTS, ct.SLOTS = dict(RULE), SLOTS, SLOTS
    T = bt.indicators(D, funds)
    T, mkt = op.extra_indicators(D, T)
    live = sc.alive(D)
    sm = smt.momentum_scores(smt.momentum_parts(D, T))["sm"][1]
    S = ct.ComboSim(D, T)
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
    avoid = ~sc.earnings_near(D, funds, RULE["max_days"])[0]
    to = T["to"]
    print(f"ready {time.time() - t0:.0f}s")

    def evaluate(sig, key, curve=False):
        out = {}
        for pid, (a, b) in rng.items():
            mask = np.zeros(len(M.idx), bool)
            mask[a:b + 1] = True
            mask[max(a, b - RULE["max_days"]):] = False
            t = S.rule_trades(sig & avoid, mask, key)
            r = {}
            for pk in ("cash", "nikkei"):
                p = S.portfolio_park(t, a, b, years[pid], park if pk == "nikkei" else None)
                r[pk] = {"total": p["total"], "annual": p["annual"], "max_dd": p["max_dd"],
                         "excess": p["annual"] - bench[pid]["annual"]}
                if curve:
                    r[pk]["curve"] = p["curve"]
            net = t["net"] if t is not None and len(t["net"]) else np.array([])
            wins, losses = net[net > 0], net[net <= 0]
            r.update({"n": int(len(net)), "net": float(net.mean()) if len(net) else None,
                      "win": float((net > 0).mean() * 100) if len(net) else None,
                      "pf": float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else None,
                      "per_day": float(len(net) / max(mask.sum(), 1))})
            out[pid] = r
        return out

    res = {"signals": {}, "names": NAMES}
    for k in KS:
        sigs = signals(D, T, k)
        for sid, sig in sigs.items():
            res["signals"].setdefault(sid, {})[str(k)] = evaluate(sig, to)
            r = res["signals"][sid][str(k)]
            print(f"k={k} {sid}: IS n={r['is']['n']} net={r['is']['net']} ann={r['is']['cash']['annual']:+.1f} | "
                  f"OOS n={r['oos']['n']} net={r['oos']['net']} tot={r['oos']['cash']['total']:+.1f} ({time.time() - t0:.0f}s)")

    # 条件ごとに探索期間の年率 (現金) が一番良い k を選ぶ
    pick = {sid: max(KS, key=lambda k: res["signals"][sid][str(k)]["is"]["cash"]["annual"]) for sid in NAMES}
    best_k = {}
    for k in sorted(set(pick.values())):
        sigs = signals(D, T, k)
        for sid, kk in pick.items():
            if kk == k:
                res["signals"][sid]["best"] = {"k": k, **evaluate(sigs[sid], to, curve=True)}
                best_k[sid] = k

    # 比較: ランダム (売買代金1億円以上から無作為)、SCORE勢い上位10
    pool = (to >= 1) & live
    rkeys = [pd.DataFrame(np.random.default_rng(s).random(M.C.shape), index=M.idx, columns=M.cols).where(pool)
             for s in range(RANDOM_SEEDS)]
    rres = [evaluate(k.rank(axis=1, ascending=False) <= 60, k, curve=(n == 0)) for n, k in enumerate(rkeys)]
    rnd = {}
    for pid in rng:
        rnd[pid] = {pk: {m: float(np.mean([r[pid][pk][m] for r in rres])) for m in ("total", "annual", "max_dd", "excess")}
                    for pk in ("cash", "nikkei")}
        for pk in ("cash", "nikkei"):
            if "curve" in rres[0][pid][pk]:
                rnd[pid][pk]["curve"] = rres[0][pid][pk]["curve"]
        for m in ("n", "net", "win", "pf"):
            vals = [r[pid][m] for r in rres if r[pid][m] is not None]
            rnd[pid][m] = float(np.mean(vals)) if vals else None
    res["random"] = rnd
    smsig = (sm.rank(axis=1, ascending=False, method="first") <= 10) & sm.notna()
    res["sm"] = evaluate(smsig, sm, curve=True)
    res["bench"] = bench
    res["meta"] = {
        "demo": demo, "generated_at": dt.datetime.now(bt.JST).isoformat(timespec="minutes"),
        "periods": {k: [M.idx[a].strftime("%Y-%m-%d"), M.idx[b].strftime("%Y-%m-%d")] for k, (a, b) in rng.items()},
        "tickers": len(M.cols), "rule": RULE, "slots": SLOTS, "ks": KS, "best_k": best_k,
        "sweep_days": SWEEP_DAYS, "zone_days": ZONE_DAYS, "discount_days": DISCOUNT_DAYS,
        "nikkei": nk is not None, "cost_tiers": bt.COST_TIERS, "tax": bt.TAX, "elapsed_s": round(time.time() - t0),
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
    if args.demo:
        rng = np.random.default_rng(3)
        for c, f in funds.items():
            f.update({"earn_ts": (dt.datetime(2025, 8, 10) + dt.timedelta(days=int(rng.integers(0, 90)))).timestamp()})
    res = run(D, funds, args.demo)
    name = "smc_demo.json" if args.demo else "smc.json"
    (bt.OUT_DIR / name).write_text(json.dumps(bt.clean(res), ensure_ascii=False, indent=1, allow_nan=False),
                                   encoding="utf-8")
    print(f"wrote {bt.OUT_DIR / name}")


if __name__ == "__main__":
    main()
