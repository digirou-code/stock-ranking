"""日次バッチ: データ更新 → 点検 → スコア → 紙上運用の再計算 → 明日の注文 → レポート。

  python -m system.daily                 API から差分を取って全部やる (JQUANTS_API_KEY が必要)
  python -m system.daily --no-fetch      手元のストアだけで作る
  python -m system.daily --asof 2024-08-30 --start 2024-06-03   過去の日で試す

紙上運用は「開始日から今日まで、バックテストと同じエンジンで毎日回し直す」方式。注文はバックテストと必ず一致する。
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import os

import numpy as np
import pandas as pd

from . import benchmark, build, config, engine, report, store, strategies
from .backtest.run import sim_cfg


def fetch_updates(days_back: int = 420) -> list[str]:
    from .jq_client import JQuants
    cli = JQuants()
    last = store.last_date()
    start = dt.date.fromisoformat(last) + dt.timedelta(days=1) if last else dt.date.today() - dt.timedelta(days=days_back * 7 // 5)
    dates = [(start + dt.timedelta(days=n)).isoformat() for n in range((dt.date.today() - start).days + 1)]
    dates = [d for d in dates if dt.date.fromisoformat(d).weekday() < 5]
    if dates:
        store.append_api(cli, dates)
    return dates


def data_checks(p, held_codes: list[str], stale_check: bool = True) -> list[str]:
    warn = []
    n = np.isfinite(p.C).sum(axis=1)
    if len(n) >= 2 and n[-1] < n[-2] * 0.95:
        warn.append(f"当日の銘柄数 {n[-1]} が前日 {n[-2]} より5%以上少ない")
    for c in held_codes:
        if not np.isfinite(p.C[-1, p.col(c)]):
            warn.append(f"保有 {c} の当日の日足が無い")
    r = p.C[-1] / p.C[-2] - 1
    adj = p.AC[-1] / p.AC[-2] - 1
    bad = np.flatnonzero((np.abs(r) > 0.5) & (np.abs(adj) > 0.5))
    if bad.size:
        warn.append("前日比±50%超で分割調整のない銘柄: " + ", ".join(p.codes[j] for j in bad[:10]))
    today = dt.date.today()
    last = dt.date.fromisoformat(p.dates[-1])
    if stale_check and (today - last).days > 4:
        warn.append(f"最新の日付 {p.dates[-1]} が古い")
    return warn


def plan_buys(p, T, sleeves, sc, res, i, gap_limit):
    """明日の寄りで買う注文。エンジンの買い手順と同じ (並び順・株数・現金・ブレーキ)"""
    prev = res["equity"][-1] if res["equity"] else sc.initial
    dd = prev / res["peak"] - 1
    stopped = res["stopped"] or (sc.dd_stop is not None and dd <= sc.dd_stop)
    if stopped and dd > sc.dd_resume:
        stopped = False
    scale = 0.0 if stopped else (0.5 if sc.dd_half is not None and dd <= sc.dd_half else 1.0)
    cash = res["cash"]
    held = {h["j"] for h in res["held"]}
    out = []
    if scale == 0:
        return out, scale
    for s in sleeves:
        n_s = sum(1 for h in res["held"] if h["s"] == s.name)
        row = s.keys[i]
        ok = np.flatnonzero(np.isfinite(row))
        for j in ok[np.argsort(-row[ok], kind="stable")][: s.max_cands]:
            if n_s >= s.slots:
                break
            if j in held:
                continue
            px = p.C[i, j]
            if not (px > 0):
                continue
            target = prev * s.weight / s.slots * scale
            if sc.max_order_pct_of_turnover is not None and T["to20"][i, j] == T["to20"][i, j]:
                target = min(target, T["to20"][i, j] * 1e8 * sc.max_order_pct_of_turnover)
            sh = int(target // (px * sc.lot)) * sc.lot
            if sh == 0 and px * sc.lot <= target * sc.lot_overshoot:
                sh = sc.lot
            if sh == 0:
                continue
            c1 = engine.cost_pct(sc.tiers, T["to20"][i, j]) / 100
            cost = sh * px * (1 + c1)
            if s.live and cash - cost < sc.cash_buffer * prev:
                continue
            if s.live:
                cash -= cost
            held.add(j)
            n_s += 1
            out.append({"sleeve": s.name, "code": p.codes[j], "shares": sh, "ref_close": px, "amount": round(sh * px),
                        "limit": round(px * (1 + gap_limit), 1) if gap_limit else None, "score": round(float(row[j]), 2),
                        "live": s.live})
    return out, scale


def describe_held(p, T, res, sleeves, i, earn):
    rules = {s.name: s.rule for s in sleeves}
    rows = []
    for h in res["held"]:
        t, j = h["t"], h["j"]
        if max(t["xa"], t["xb"]) <= i:
            continue  # 今日すべて手仕舞った
        k = (t["ent_raw"] / t["ent"])  # 調整後 → 実株価
        held_days = i - t["e"] + 1
        rule = rules[h["s"]]
        last = p.C[i, j]
        sh = h["na"] + h["nb"]
        sold_a = t["xa"] <= i
        rows.append({"sleeve": h["s"], "code": p.codes[j], "entry": p.dates[t["e"]], "shares": sh,
                     "remaining": h["nb"] if sold_a else sh, "entry_px": round(t["ent_raw"], 1), "last": last,
                     "pnl_pct": round((last / t["ent_raw"] - 1) * 100, 1) if last == last else None,
                     "stop": round(t["sp"] * k, 1), "take1": None if sold_a else round(t["tp1"] * k, 1),
                     "take2": round(t["tp2"] * k, 1) if rule["t2"] < 500 else None,
                     "days_left": rule["days"] - held_days, "time_exit_tomorrow": rule["days"] - held_days == 1,
                     "near_stop": last == last and last <= t["sp"] * k * 1.03,
                     "earnings": earn.get(p.codes[j])})
    return rows


def upcoming_earnings(asof: str) -> dict:
    df = store.load_earnings()
    if df.empty or "SchDate" not in df.columns:
        return {}
    end = (dt.date.fromisoformat(asof) + dt.timedelta(days=35)).isoformat()
    df = df[(df["SchDate"] > asof) & (df["SchDate"] <= end)]
    return df.groupby("Code")["SchDate"].min().to_dict()


def candidates(p, T, st, keys, i):
    out = {}
    for name, K in keys.items():
        row = K[i]
        ok = np.flatnonzero(np.isfinite(row))
        top = ok[np.argsort(-row[ok], kind="stable")][:10]
        lst = []
        for j in top:
            prev1 = K[i - 1, j] if i >= 1 else np.nan
            prev5 = K[i - 5, j] if i >= 5 else np.nan
            lst.append({"code": p.codes[j], "score": round(float(row[j]), 2),
                        "d1": round(float(row[j] - prev1), 2) if prev1 == prev1 else None,
                        "d5": round(float(row[j] - prev5), 2) if prev5 == prev5 else None,
                        "close": p.C[i, j], "to20": round(float(T["to20"][i, j]), 1)})
        out[name] = lst
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--asof")
    ap.add_argument("--start")
    ap.add_argument("--out", default=os.path.join(config.ROOT, "docs", "system"))
    ap.add_argument("--mail", action="store_true")
    args = ap.parse_args()
    cfg = config.load()
    fetched = [] if args.no_fetch else fetch_updates()
    p, T, fp = build.build(end=args.asof)
    st = strategies.Strategies(p, T, fp, cfg)
    i = len(p.dates) - 1
    start = args.start or cfg["paper"]["start_date"]
    first = min(bisect.bisect_left(p.dates, start), i)
    sc = sim_cfg(cfg) if cfg["risk"].get("brake", True) else sim_cfg(cfg, dd_half=None, dd_stop=None)
    nk, _ = benchmark.total_return_close(p, cfg["benchmark"]["nikkei_etf"])
    gap = cfg["orders"].get("gap_limit")
    keys = {"L": st.L(), "G": getattr(st, cfg["sleeves"]["G"]["variant"])(), "R": st.R()}
    sleeves = []
    for sid in ("L", "G", "R"):
        s = cfg["sleeves"][sid]
        o = engine.Opts(gap_limit=gap, low_stop=sid == "R", ma_exit=sid == "R")
        # live=false の枠 (検証に合格していない R枠) は資金を日経ETFのまま置き、候補だけ「紙上のみ」で出す
        sleeves.append(engine.Sleeve(sid, keys[sid], dict(cfg["rules"][s["rule"]]), s["slots"], s["weight"], s["idle"],
                                     s.get("live", True), o))
    res = engine.simulate(p, T, sleeves, sc, first, i, i, etf=nk, live=True) if first < i else \
        {"equity": [], "trades": [], "held": [], "cash": sc.initial, "peak": sc.initial, "stopped": False, "dates": []}
    buys, scale = plan_buys(p, T, sleeves, sc, res, i, gap)
    earn = upcoming_earnings(p.dates[i])
    held = describe_held(p, T, res, sleeves, i, earn)
    eq = res["equity"]
    nk_curve = engine.bench_curve(nk, first, i, sc.initial) if first < i else []
    value = eq[-1] if eq else sc.initial
    mv = {}
    for h in held:
        mv[h["sleeve"]] = mv.get(h["sleeve"], 0) + (h["last"] or 0) * h["remaining"]
    open_risk = sum(max(0.0, (h["last"] - h["stop"]) * h["remaining"]) for h in held if h["last"] == h["last"])
    warns = data_checks(p, [h["code"] for h in held], stale_check=not args.asof)
    ctx = {
        "asof": p.dates[i], "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": "実資金" if cfg["paper"]["live"] else "紙上運用", "paper_start": p.dates[first],
        "value": value, "initial": sc.initial, "cash": res["cash"],
        "perf": perf_rows(res.get("dates", []), eq, nk_curve, sc.initial),
        "dd": value / res["peak"] - 1, "brake_scale": scale,
        "open_risk_pct": open_risk / value if value else 0, "sleeve_mv": mv,
        "orders": {"buy": buys,
                   "sell_time": [h for h in held if h["time_exit_tomorrow"]],
                   "standing": held},
        "held": held, "candidates": candidates(p, T, st, keys, i),
        "warnings": warns, "fetched": fetched, "config": {k: cfg["sleeves"][k] for k in ("L", "G", "R")},
        "risk": cfg["risk"],
    }
    if eq and len(eq) >= 2 and eq[-1] / eq[-2] - 1 <= cfg["risk"]["daily_loss_warn"]:
        warns.append(f"1日の損失 {(eq[-1] / eq[-2] - 1) * 100:.1f}%")
    if ctx["open_risk_pct"] > cfg["risk"]["open_risk_warn"]:
        warns.append(f"損切りになった場合の損失合計が総資産の {ctx['open_risk_pct'] * 100:.1f}%")
    for h in held:
        if h["last"] == h["last"] and h["last"] * h["remaining"] > value * cfg["risk"]["single_warn"]:
            warns.append(f"{h['code']} が総資産の10%超")
    os.makedirs(args.out, exist_ok=True)
    if warns:  # 点検に失敗した日は注文を出さない
        if any("日足が無い" in w or "銘柄数" in w or "古い" in w for w in warns):
            ctx["orders"]["buy"] = []
            ctx["orders_blocked"] = True
    with open(os.path.join(args.out, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(ctx, f, ensure_ascii=False, indent=1, default=_js)
    os.makedirs(os.path.join(config.ROOT, "state", "orders"), exist_ok=True)
    pd.DataFrame([{"side": "買", **b} for b in ctx["orders"]["buy"]] +
                 [{"side": "売(引成)", "sleeve": h["sleeve"], "code": h["code"], "shares": h["remaining"]}
                  for h in ctx["orders"]["sell_time"]]).to_csv(
        os.path.join(config.ROOT, "state", "orders", f"{p.dates[i]}.csv"), index=False, encoding="utf-8-sig")
    report.write_html(ctx, os.path.join(args.out, "index.html"))
    if args.mail:
        report.send_mail(ctx)
    print(f"{p.dates[i]} 資産 {value:,.0f} 買い {len(ctx['orders']['buy'])} 時間切れ売り {len(ctx['orders']['sell_time'])} "
          f"警告 {len(warns)}")


def perf_rows(dates, eq, nk, initial):
    if not eq:
        return []
    out = []
    last = dates[-1]
    for label, since in (("当日", None), ("月初来", last[:7]), ("年初来", last[:4]), ("開始来", "")):
        if since is None:
            a = len(eq) - 2
        else:
            a = next((n - 1 for n, d in enumerate(dates) if d.startswith(since)), -1)
        b0 = eq[a] if a >= 0 else initial
        n0 = nk[a] if a >= 0 else initial
        mine, bench = eq[-1] / b0 - 1, nk[-1] / n0 - 1
        out.append({"label": label, "mine": round(mine * 100, 2), "nikkei": round(bench * 100, 2),
                    "diff": round((mine - bench) * 100, 2)})
    return out


def _js(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if o != o else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return str(o)


if __name__ == "__main__":
    main()
