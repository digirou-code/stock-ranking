"""サイトにある全スクリーニングを、最新の取引ルールで検証する。

取引ルール (screener/build_data.py の RULES と同じ):
    エントリー  シグナル翌営業日の寄り付き
    損切り      -6%
    利確        +20% で半分、+40% で残り
    時間切れ    25営業日 (25日目の大引けで手仕舞い)
    同時保有    10銘柄まで (資金を10枠に分け、空き枠があれば各タブの並び順の上位から買う)
    決算回避    保有予定の25営業日に決算発表 (推定日) が入る銘柄は買わない

条件は build_data.SCREENS をその日の値で毎日判定して再現する。財務の値 (ROE・成長率・自己資本比率・配当など) は
過去の値が無料で取れないため現在の値で代用する (先読み)。そのため財務条件を含むタブは、
株価だけの条件に絞った版 (並び順は売買代金の大きい順) も合わせて出す。
信用倍率は過去の銘柄別データが無いので、信用過多押し目は信用倍率の条件を外した版しか検証できない。
SCORE業績・SCORE勢いのタブは「その日のスコア上位10銘柄」をシグナルとみなす。

比較対象: ランダム (売買代金1億円以上の銘柄から無作為に買う、5回の平均)、日経平均 (買って持つだけ)。

使い方:
    python backtest/screens_test.py --cache bt_cache/prices.pkl
    python backtest/screens_test.py --demo
出力:
    backtest/results/screens.json
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

RULE = {"stop_pct": -6.0, "take1_pct": 20.0, "take2_pct": 40.0, "max_days": 25}
SLOTS = 10
MACD_GC_DAYS = 3
RANDOM_SEEDS = 5
QUARTER_DAYS = 365.25 / 4


# ---------------------------------------------------------------- 入力
def fund_frames(D, funds):
    """現在の財務値を全期間に広げる。PER・配当利回り・時価総額だけはその日の株価で計算し直す"""
    C = D["Close"]
    cols = C.columns
    f = lambda k: pd.Series({c: (funds.get(c) or {}).get(k) for c in cols}, dtype=float)
    const = lambda s: pd.DataFrame(np.broadcast_to(s.to_numpy(), C.shape), index=C.index, columns=cols)
    px_now = f("mc_raw") / f("shares")
    eps = px_now / f("per")
    div = f("div_trail").fillna(f("div_fwd"))
    F = {k: const(f(k)) for k in ("roe", "opm", "rg", "eg", "er")}
    F["per"] = C / eps
    F["dy"] = C.rdiv(div) * 100
    F["mc"] = C * f("shares") / 1e8
    return F


def earnings_near(D, funds, days):
    """保有予定期間 (翌営業日から days 営業日) に決算発表が入るか。
    過去の決算日は無料で一括取得できないため、次回の決算予定日 (earn_ts) から3ヶ月ごとにさかのぼった日を推定日とする"""
    idx = D["Close"].index
    ev = np.zeros(D["Close"].shape, bool)
    have = 0
    for j, c in enumerate(D["Close"].columns):
        ts = (funds.get(c) or {}).get("earn_ts")
        if not ts:
            continue
        have += 1
        base = pd.Timestamp(dt.datetime.fromtimestamp(ts, bt.JST).date())
        dates = [base + pd.Timedelta(days=QUARTER_DAYS * k) for k in range(-24, 3)]
        pos = idx.searchsorted(pd.DatetimeIndex(dates))
        pos = pos[(pos > 0) & (pos < len(idx))]
        ev[pos, j] = True
    E = pd.DataFrame(ev, index=idx, columns=D["Close"].columns).astype(int)
    near = E[::-1].rolling(days, min_periods=1).max()[::-1].shift(-1).fillna(0).astype(bool)
    return near, have


def screens(D, T, F, sm, sf):
    """(id, 名前, タブ, サイト条件, 株価だけの条件 or None, 並び順キー, 検証できない点)"""
    C, H = D["Close"], D["High"]
    btw = bt.btw
    macd = C.ewm(span=12, adjust=False).mean() - C.ewm(span=26, adjust=False).mean()
    above = macd > macd.ewm(span=9, adjust=False).mean()
    gc_age = pd.DataFrame(np.nan, index=C.index, columns=C.columns)
    for k in reversed(range(MACD_GC_DAYS)):  # k日前に上抜け (k が小さいほど新しい)
        crossed = above & ~above.shift(k + 1, fill_value=False)
        for m in range(k):
            crossed &= above.shift(m + 1, fill_value=False)
        gc_age = gc_age.where(~crossed, k)
    rsi = T["rsi"]
    rsi_min10 = rsi.rolling(10).min()
    off10 = (C / H.rolling(10).max() - 1) * 100
    hi60 = C > C.shift(1).rolling(60).max()
    top = lambda s: (s.rank(axis=1, ascending=False, method="first") <= SLOTS) & s.notna()
    pb_base = lambda max_off: (btw(rsi, 35, 50) & (rsi_min10 <= 35) & btw(off10, max_off, -10)
                               & (F["mc"] >= 300) & (T["to"] >= 1))
    trend = T["ma75_up"] & T["above75"] & T["ma25_over75"]
    eg15 = F["eg"] > 15
    to = T["to"]
    fund = "財務は現在値 (先読み)"
    return [
        ("sf", "SCORE業績", "業績", top(sf), None, sf, "スコアの全項目が財務 (現在値)"),
        ("sm", "SCORE勢い", "勢い", top(sm), top(sm), sm, None),
        ("dividend", "高配当", "配当", (F["dy"] >= 3.5) & (F["roe"] >= 5) & (F["mc"] >= 100), None, F["dy"],
         "配当・ROEは現在値"),
        ("low_per", "低PER", "割安", btw(F["per"], 1e-9, 12) & (F["roe"] >= 8), None, -F["per"], "利益・ROEは現在値"),
        ("high_roe", "高ROE", "収益", (F["roe"] >= 15) & ((F["er"] >= 30) | F["er"].isna()), None, F["roe"],
         "ROE・自己資本比率は現在値"),
        ("growth", "成長株", "成長", (F["rg"] >= 10) & (F["eg"] >= 10) & (F["mc"] >= 300), None,
         F["rg"].rank(axis=1, pct=True) + F["eg"].rank(axis=1, pct=True), "成長率は現在値"),
        ("pullback", "25日線押し目", "押し目",
         trend & btw(T["dev25"], -3, 1) & btw(T["off20"], -15, -5) & (F["roe"] >= 8) & (to >= 1),
         trend & btw(T["dev25"], -3, 1) & btw(T["off20"], -15, -5) & (to >= 1), T["r3m"], fund),
        ("rsi_pullback", "RSI押し目", "押し目",
         T["above200"] & btw(rsi, 35, 45) & (T["chg"] > 0) & T["bull"] & (F["opm"] >= 5) & (to >= 1),
         T["above200"] & btw(rsi, 35, 45) & (T["chg"] > 0) & T["bull"] & (to >= 1), T["r6m"], fund),
        ("eg_pullback", "好業績押し目", "押し目",
         T["bull"] & (T["chg"] >= 2) & pb_base(-20) & eg15,
         T["bull"] & (T["chg"] >= 2) & pb_base(-20), F["eg"], fund),
        ("margin_pullback", "信用過多押し目", "押し目",
         pb_base(-30) & eg15, pb_base(-30), F["eg"], "信用倍率10倍以上の条件は過去データが無く外した。財務は現在値"),
        ("rebound_dev", "乖離リバウンド", "リバウンド",
         (T["dev25"] <= -10) & (F["rg"] > 0) & (F["eg"] > 0) & (F["roe"] >= 8) & btw(F["per"], 1e-9, 20)
         & (F["mc"] >= 300) & (to >= 1),
         (T["dev25"] <= -10) & (F["mc"] >= 300) & (to >= 1), -T["dev25"], fund),
        ("rebound_rsi", "RSI売られすぎ反発", "リバウンド",
         (T["rsi_min5"] <= 30) & T["bull"] & (T["chg"] >= 1) & (T["vr"] >= 1.2) & (F["eg"] > 0) & (F["roe"] >= 8),
         (T["rsi_min5"] <= 30) & T["bull"] & (T["chg"] >= 1) & (T["vr"] >= 1.2), T["chg"], fund),
        ("vol_break", "出来高急増", "初動",
         (T["vr"] >= 2) & (T["chg"] >= 3) & (T["near_hi"] >= -1) & (T["dev25"] > 0) & (T["to1"] >= 3),
         None, T["vr"], None),
        ("high_break", "高値ブレイク", "初動", hi60 & (T["vr"] >= 1.5) & T["ma25_over75"] & (to >= 1),
         None, T["vr"], None),
        ("macd_rsi", "MACD・RSI", "初動", gc_age.notna() & (rsi >= 50) & eg15 & (F["mc"] >= 300),
         gc_age.notna() & (rsi >= 50) & (F["mc"] >= 300), -gc_age * 1000 + F["eg"].fillna(0).clip(-999, 999), fund),
    ]


# ---------------------------------------------------------------- 実行
def run(D, funds, demo):
    t0 = time.time()
    rt.RULE = RULE  # RuleSim は rule_test.RULE を参照する
    T = bt.indicators(D, funds)
    T, idx = op.extra_indicators(D, T)
    F = fund_frames(D, funds)
    sm = smt.momentum_scores(smt.momentum_parts(D, T))["sm"][1]
    live = D["Close"].notna() & (D["Close"].rolling(75).count() >= 75)
    pct = lambda x, hi=True: (x.where(live).rank(axis=1, pct=True) if hi else 1 - x.where(live).rank(axis=1, pct=True))
    per_r = pct(F["per"].where(F["per"] > 0, 9999).where(F["per"].notna()), False)
    sf_items = [per_r, pct(F["roe"]), pct(F["er"]), pct(F["dy"]), pct(F["rg"]), pct(F["eg"])]
    have = sum(x.notna().astype(int) for x in sf_items)
    sf = (sum(x.fillna(0.5) for x in sf_items) / len(sf_items) * 100).where(have >= 4)
    near, have_earn = earnings_near(D, funds, RULE["max_days"])
    S = rt.RuleSim(D, T)
    M = S.M
    first_valid = M.idx[min(250, len(M.idx) - 1)]
    periods = {"oos": M.idx >= M.idx[-1] - pd.DateOffset(years=1), "all": M.idx >= first_valid}
    years = {k: max((M.idx[m].max() - M.idx[m].min()).days / 365.25, 0.1) for k, m in periods.items()}
    print(f"ready {time.time() - t0:.0f}s (earnings dates for {have_earn} tickers)")

    def evaluate(sig, key, pid, curve=False):
        t = S.rule_trades(sig.fillna(False).astype(bool), periods[pid], key)
        r = S.summarize(t, years[pid], curve=curve)
        r.pop("by_year", None)
        return r

    res = {"screens": {}}
    for sid, name, tab, site, price, key, caveat in screens(D, T, F, sm, sf):
        R = {"name": name, "tab": tab, "caveat": caveat, "site": {}, "price": {}, "no_earn": {}}
        avoid = ~near
        for pid in periods:
            R["site"][pid] = evaluate(site & avoid, key, pid, curve=True)
            if price is not None and caveat:
                R["price"][pid] = evaluate(price & avoid, T["to"], pid)
        p = evaluate(site, key, "oos")  # 決算回避なしとの比較 (直近1年)
        R["no_earn"]["oos"] = {k: p.get(k) for k in ("n", "net", "pf", "pf_total", "pf_dd")}
        res["screens"][sid] = R
        s = R["site"]
        print(f"{name}: 1y n={s['oos'].get('n')} net={s['oos'].get('net')} tot={s['oos'].get('pf_total')} "
              f"dd={s['oos'].get('pf_dd')} | all ann={s['all'].get('pf_annual')} dd={s['all'].get('pf_dd')}")

    # ランダム: 売買代金1億円以上・決算回避の銘柄から無作為に買う
    rng = np.random.default_rng(0)
    pool = (T["to"] >= 1) & live & ~near
    res["random"] = {}
    for pid in periods:
        ps = []
        for _ in range(RANDOM_SEEDS):
            key = pd.DataFrame(rng.random(M.C.shape), index=M.idx, columns=M.cols)
            ps.append(S.summarize(S.rule_trades(pool, periods[pid], key), years[pid], curve=True))
        res["random"][pid] = {k: float(np.mean([p[k] for p in ps if p.get(k) is not None]))
                              for k in ("net", "win", "pf", "pf_total", "pf_annual", "pf_dd")}
        res["random"][pid]["curve"] = ps[0].get("curve")
    nk = rt.nikkei(M.idx[0].strftime("%Y-%m-%d"), None, demo)
    res["bench"] = {pid: rt.bench(nk, M.idx[m].min(), M.idx[m].max()) for pid, m in periods.items()}
    res["meta"] = {
        "demo": demo, "generated_at": dt.datetime.now(bt.JST).isoformat(timespec="minutes"),
        "periods": {k: [M.idx[m].min().strftime("%Y-%m-%d"), M.idx[m].max().strftime("%Y-%m-%d")]
                    for k, m in periods.items()},
        "tickers": len(M.cols), "earn_tickers": have_earn, "rule": RULE, "slots": SLOTS,
        "cost_tiers": bt.COST_TIERS, "commission": bt.COMMISSION, "tax": bt.TAX,
        "dropped_trades": int(getattr(S, "dropped", 0)), "elapsed_s": round(time.time() - t0),
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
            f.update({"er": rng.normal(45, 15), "div_trail": abs(rng.normal(20, 15)),
                      "earn_ts": (dt.datetime(2025, 8, 10) + dt.timedelta(days=int(rng.integers(0, 90)))).timestamp()})
    res = run(D, funds, args.demo)
    name = "screens_demo.json" if args.demo else "screens.json"
    (bt.OUT_DIR / name).write_text(json.dumps(bt.clean(res), ensure_ascii=False, indent=1, allow_nan=False),
                                   encoding="utf-8")
    print(f"wrote {bt.OUT_DIR / name}")


if __name__ == "__main__":
    main()
