"""事前登録した検証を順に1回ずつ回す (設計書「バックテスト」の検証0〜6)。

  python -m system.backtest.run            → system/backtest/results/backtest_system.json / .md

検証0 再現: 売買代金上位10・R2・現金 (端数株・1枠=資産/10) を既存検証と比べる
検証1 G0 と G1 (成長株枠、単独・10枠・R4・待機資金は日経ETF)
検証2 R枠 (単独) をランダム10通り・旧「乖離リバウンド」と比べる
検証3 3枠合計 (3,000万円・100株単位) を DDブレーキあり・なしで
検証4 窓開けフィルタ (寄りが前日終値+5%超なら買わない)
検証5 トレーリングストップ (時間切れの代わりに最高値から L -10%・G -15%)
検証6 合格した設定のコスト試験 (片道+0.2%) と開始日ずらし (最初の12か月の月初)
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import os
import random
import time

import numpy as np

from .. import benchmark, build, config, engine, strategies

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "results")
START = "2017-11-30"


def sim_cfg(cfg, **kw) -> engine.SimConfig:
    c, r = cfg["capital"], cfg["risk"]
    base = dict(initial=float(c["initial_yen"]), lot=c["lot"], lot_overshoot=c["lot_overshoot"],
                cash_buffer=c["cash_buffer"], max_order_pct_of_turnover=c["max_order_pct_of_turnover"],
                tiers=cfg["costs"]["tiers"], dd_half=r["dd_half"], dd_stop=r["dd_stop"], dd_resume=r["dd_resume"],
                etf_code=cfg["benchmark"]["nikkei_etf"])
    base.update(kw)
    return engine.SimConfig(**base)


def research_cfg(cfg) -> engine.SimConfig:
    """既存検証と同じ前提 (端数株・1枠=資産/枠数・現金の余りなし・DDブレーキなし)"""
    return sim_cfg(cfg, initial=100.0, lot=None, cash_buffer=0.0, max_order_pct_of_turnover=None,
                   dd_half=None, dd_stop=None)


class Ctx:
    def __init__(self, cfg, end=None):
        t0 = time.time()
        self.cfg = cfg
        self.p, self.T, self.fp = build.build(end=end)
        self.st = strategies.Strategies(self.p, self.T, self.fp, cfg)
        self.first = bisect.bisect_left(self.p.dates, START)
        self.end = len(self.p.dates) - 1
        self.last_sig = self.end - 1
        self.nk, self.nk_div = benchmark.total_return_close(self.p, cfg["benchmark"]["nikkei_etf"])
        self.tp, self.tp_div = benchmark.total_return_close(self.p, cfg["benchmark"]["topix_etf"])
        self.keys = {}
        print(f"data {self.p.dates[0]}〜{self.p.dates[-1]} {len(self.p.codes)}銘柄 {time.time() - t0:.0f}s", flush=True)

    def key(self, name):
        if name not in self.keys:
            self.keys[name] = getattr(self.st, name)()
        return self.keys[name]

    def sleeve(self, sid, keys=None, weight=None, slots=None, opts=None, live=None):
        s = self.cfg["sleeves"][sid]
        rule = dict(self.cfg["rules"][s["rule"]])
        if keys is None:
            keys = self.key(s.get("variant", sid) if sid == "G" else sid)
        o = opts or engine.Opts()
        if sid == "R":
            o = engine.Opts(**{**o.__dict__, "low_stop": True, "ma_exit": True})
        return engine.Sleeve(sid, keys, rule, slots or s["slots"], s["weight"] if weight is None else weight,
                             s["idle"], s.get("live", True) if live is None else live, o)

    def run(self, sleeves, sc, first=None, end=None):
        first = self.first if first is None else first
        end = self.end if end is None else end
        r = engine.simulate(self.p, self.T, sleeves, sc, first, min(self.last_sig, end - 1), end, etf=self.nk)
        return self.summarize(r, sc.initial, first, end)

    def summarize(self, r, initial, first, end):
        d, eq = r["dates"], r["equity"]
        s = engine.stats(d, eq, initial, r["trades"])
        nk = engine.stats(d, engine.bench_curve(self.nk, first, end, initial), initial)
        s["vs_nikkei"] = round(s["annual"] - nk["annual"], 1)
        s["yearly"] = engine.yearly(d, eq, initial)
        s["rolling"] = self.rolling(d, eq, first)
        s["sleeves"] = {k: sum(1 for t in r["trades"] if t["sleeve"] == k) for k in {t["sleeve"] for t in r["trades"]}}
        s["_eq"] = eq
        return s

    def rolling(self, d, eq, first):
        """毎年1月始まりの5年 (最後は途中まで、2.5年以上あるもの)。日経超えなら True"""
        out = {}
        eq = np.asarray(eq)
        nkc = np.asarray(engine.bench_curve(self.nk, first, first + len(eq), 1.0))
        years = sorted({x[:4] for x in d})
        for y in years[1:]:
            a = bisect.bisect_left(d, f"{y}-01-01")
            b = bisect.bisect_right(d, f"{int(y) + 4}-12-31") - 1
            if a < 1 or b <= a:
                continue
            span = (dt.date.fromisoformat(d[b]) - dt.date.fromisoformat(d[a - 1])).days / 365.25
            if span < 2.5:
                continue
            mine = eq[b] / eq[a - 1]
            nk = nkc[b] / nkc[a - 1]
            out[f"{y}-"] = round((mine ** (1 / span) - nk ** (1 / span)) * 100, 1)
        return out


def bench_row(ctx, first, end, initial):
    d = ctx.p.dates[first + 1:end + 1]
    return {"nikkei_1321": engine.stats(d, engine.bench_curve(ctx.nk, first, end, initial), initial),
            "topix_1306": engine.stats(d, engine.bench_curve(ctx.tp, first, end, initial), initial),
            "dividends_included": ctx.nk_div}


def passes(cfg, s, nk_dd):
    pc = cfg["pass"]
    roll = list(s["rolling"].values())
    checks = {"日経比+3pt": s["vs_nikkei"] >= pc["excess_annual_pt"],
              "PF1.3": (s.get("pf") or 0) >= pc["pf"],
              "DD<日経": s["max_dd"] > nk_dd,
              "ローリング過半": bool(roll) and sum(1 for x in roll if x > 0) * 2 > len(roll)}
    return checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", help="この日までのデータで回す")
    args = ap.parse_args()
    cfg = config.load()
    # 検証0〜6 は事前登録した3枠の配分で回す (config.toml は紙上運用用に大型株100%へ変えた)
    for k, w in {"L": 0.50, "G": 0.35, "R": 0.15}.items():
        cfg["sleeves"][k]["weight"] = w
    ctx = Ctx(cfg, args.end)
    res = {"meta": {"run_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "data": [ctx.p.dates[0], ctx.p.dates[-1]], "start": ctx.p.dates[ctx.first],
                    "dividends_in_benchmark": ctx.nk_div}, "tests": {}}
    res["bench"] = bench_row(ctx, ctx.first, ctx.end, 100.0)
    nk_dd = res["bench"]["nikkei_1321"]["max_dd"]
    rc = research_cfg(cfg)
    T = res["tests"]

    def log(name, s):
        print(f"{name}: 年率 {s['annual']}% 日経比 {s['vs_nikkei']:+}pt DD {s['max_dd']}% PF {s.get('pf')} 取引 {s.get('trades')}",
              flush=True)

    # 検証0
    s = ctx.run([ctx.sleeve("L", weight=1.0, slots=10)], rc)
    T["0_L_R2_cash"] = s
    log("検証0 売買代金上位R2", s)
    # 検証1
    g = {}
    for v in ("G0", "G1"):
        s = ctx.run([ctx.sleeve("G", keys=ctx.key(v), weight=1.0, slots=10)], rc)
        T[f"1_{v}"] = g[v] = s
        log(f"検証1 {v}", s)
    g_pick = "G1" if (g["G1"]["annual"] > g["G0"]["annual"] and g["G1"]["max_dd"] >= g["G0"]["max_dd"] - 3) else "G0"
    T["1_pick"] = g_pick
    cfg["sleeves"]["G"]["variant"] = g_pick
    # 検証2
    s = ctx.run([ctx.sleeve("R", weight=1.0, slots=10, live=True)], rc)
    T["2_R"] = s
    log("検証2 R", s)
    old = ctx.run([ctx.sleeve("R", keys=ctx.key("old_rebound"), weight=1.0, slots=10, live=True)], rc)
    T["2_old_rebound"] = old
    uni = np.isfinite(ctx.key("R")) | False
    rk = []
    base = ctx.st.R  # 母集団: R枠の条件のうち、急落・下げ止まりを除いたもの
    c = cfg["sleeves"]["R"]
    mask = (ctx.st.live & (ctx.T["to20"] >= c["min_turnover_oku"]) & (ctx.st.pv["mc"] >= c["min_mcap_oku"])
            & (ctx.fp.m["er"] >= c["min_equity_ratio"]) & (ctx.fp.m["op_ttm"] > 0))
    for seed in range(10):
        rnd = np.random.default_rng(seed).random(mask.shape)
        rk.append(ctx.run([ctx.sleeve("R", keys=np.where(mask, rnd, np.nan), weight=1.0, slots=10, live=True)], rc))
    T["2_random"] = {k: round(float(np.mean([r[k] for r in rk])), 1) for k in ("annual", "max_dd")}
    r_pass = s.get("pf") is not None and s["pf"] >= 1.2 and s["annual"] >= T["2_random"]["annual"] + 3
    T["2_R_live"] = r_pass
    cfg["sleeves"]["R"]["live"] = r_pass
    # 検証3
    sc = sim_cfg(cfg)
    full = lambda opts=None: [ctx.sleeve(k, opts=opts) for k in ("L", "G", "R")]  # noqa: E731
    s3 = ctx.run(full(), sc)
    T["3_combined_brake"] = s3
    log("検証3 3枠 (ブレーキあり)", s3)
    s3n = ctx.run(full(), sim_cfg(cfg, dd_half=None, dd_stop=None))
    T["3_combined_nobrake"] = s3n
    log("検証3 3枠 (ブレーキなし)", s3n)
    brake = s3["annual"] >= s3n["annual"] - 1.0
    T["3_brake_kept"] = brake
    sc = sc if brake else sim_cfg(cfg, dd_half=None, dd_stop=None)
    best = s3 if brake else s3n
    sL = ctx.run([ctx.sleeve("L", weight=1.0, slots=10)], sc)
    T["3_L_only_30M"] = sL
    log("検証3 L枠だけで3,000万円", sL)
    # 検証4
    s4 = ctx.run(full(engine.Opts(gap_limit=0.05)), sc)
    T["4_gap"] = s4
    log("検証4 窓開けフィルタ", s4)
    gap_ok = s4["annual"] >= best["annual"] and s4["max_dd"] > best["max_dd"]
    T["4_gap_kept"] = gap_ok
    # 検証5
    sl = [ctx.sleeve("L", opts=engine.Opts(trail=0.10)), ctx.sleeve("G", opts=engine.Opts(trail=0.15)), ctx.sleeve("R")]
    s5 = ctx.run(sl, sc)
    T["5_trail"] = s5
    log("検証5 トレーリング", s5)
    trail_ok = all(passes(cfg, s5, nk_dd).values()) and s5["annual"] > best["annual"]
    T["5_trail_kept"] = trail_ok
    # 検証6
    final = s5 if trail_ok else (s4 if gap_ok else best)
    T["final_checks"] = passes(cfg, final, nk_dd)
    stress = ctx.run(full(engine.Opts(cost_add=cfg["costs"]["stress_add_pct"])), sc)
    T["6_stress"] = {k: stress[k] for k in ("annual", "vs_nikkei", "max_dd", "pf")}
    shifts = []
    for mth in range(12):
        y, m = 2017 + (11 + mth) // 12, (11 + mth) % 12 + 1
        a = bisect.bisect_left(ctx.p.dates, f"{y}-{m:02d}-01")
        shifts.append(ctx.run(full(), sc, first=a)["vs_nikkei"])
    T["6_start_shift_vs_nikkei"] = shifts
    T["final_pass"] = (all(T["final_checks"].values()) and T["6_stress"]["vs_nikkei"] >= cfg["pass"]["stress_excess_pt"]
                       and sum(1 for x in shifts if x > 0) >= cfg["pass"]["start_shift_wins"])
    os.makedirs(OUT, exist_ok=True)
    eqs = {k: v.pop("_eq") for k, v in T.items() if isinstance(v, dict) and "_eq" in v}
    for v in T.values():
        if isinstance(v, list):
            for x in v:
                if isinstance(x, dict):
                    x.pop("_eq", None)
    res["equity"] = {"dates": ctx.p.dates[ctx.first + 1:ctx.end + 1], "final": eqs.get("3_combined_brake")}
    with open(os.path.join(OUT, "backtest_system.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=float)
    write_md(res)
    print("done", flush=True)


def write_md(res):
    T, b = res["tests"], res["bench"]
    L = [f"# 3枠システムの検証\n",
         f"データ {res['meta']['data'][0]}〜{res['meta']['data'][1]}、検証開始 {res['meta']['start']}。"
         f"日経1321 {'配当込み' if res['meta']['dividends_in_benchmark'] else '配当なし'}: 年率 {b['nikkei_1321']['annual']}%・"
         f"最大DD {b['nikkei_1321']['max_dd']}%。TOPIX1306: 年率 {b['topix_1306']['annual']}%。\n",
         "| 検証 | 年率% | 日経比pt | 最大DD% | PF | 勝率% | 取引 |", "|---|---|---|---|---|---|---|"]
    for k, v in T.items():
        if isinstance(v, dict) and "annual" in v and "vs_nikkei" in v:
            L.append(f"| {k} | {v['annual']} | {v['vs_nikkei']:+} | {v['max_dd']} | {v.get('pf')} | {v.get('win')} | {v.get('trades')} |")
    L += ["", f"- 検証1で選んだ成長株スコア: {T.get('1_pick')}",
          f"- R枠を実資金に入れるか: {T.get('2_R_live')} (ランダム平均 年率 {T.get('2_random', {}).get('annual')}%)",
          f"- DDブレーキを残すか: {T.get('3_brake_kept')}", f"- 窓開けフィルタを採用: {T.get('4_gap_kept')}",
          f"- トレーリングを採用: {T.get('5_trail_kept')}",
          f"- 合格基準: {T.get('final_checks')}", f"- コスト試験: {T.get('6_stress')}",
          f"- 開始日ずらし (日経比pt): {T.get('6_start_shift_vs_nikkei')}",
          f"- **最終判定: {'合格' if T.get('final_pass') else '不合格'}**"]
    yl = T.get("3_combined_brake", {}).get("yearly", {})
    if yl:
        L += ["", "## 年別 (3枠合計)", "", "| " + " | ".join(yl) + " |", "|" + "---|" * len(yl), "| " + " | ".join(str(x) for x in yl.values()) + " |"]
    with open(os.path.join(OUT, "backtest_system.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
