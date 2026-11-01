"""検証7 配分見直し (事前登録・1回だけ): 大型株70%・成長株30%・リバウンド枠なし。

検証0〜6 で決まった設定をそのまま使う: 成長株スコア G0、DDブレーキなし、窓開けフィルタあり。
合格基準・コスト試験・開始日ずらしは検証6と同じ。合格したときだけ config.toml の配分を変える。

  python -m system.backtest.realloc   → system/backtest/results/backtest_realloc.{json,md}
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import os

from .. import config, engine
from .run import OUT, Ctx, bench_row, passes, sim_cfg

WEIGHTS = {"L": 0.70, "G": 0.30}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", help="この日までのデータで回す")
    args = ap.parse_args()
    cfg = config.load()
    cfg["sleeves"]["G"]["variant"] = "G0"
    ctx = Ctx(cfg, args.end)
    bench = bench_row(ctx, ctx.first, ctx.end, 100.0)
    nk_dd = bench["nikkei_1321"]["max_dd"]
    sc = sim_cfg(cfg, dd_half=None, dd_stop=None)

    def full(cost_add=0.0):
        o = engine.Opts(gap_limit=cfg["orders"]["gap_limit"], cost_add=cost_add)
        return [ctx.sleeve(k, weight=w, opts=o) for k, w in WEIGHTS.items()]

    s = ctx.run(full(), sc)
    s.pop("_eq")
    checks = passes(cfg, s, nk_dd)
    st = ctx.run(full(cfg["costs"]["stress_add_pct"]), sc)
    stress = {k: st[k] for k in ("annual", "vs_nikkei", "max_dd", "pf")}
    shifts = []
    for mth in range(12):
        y, m = 2017 + (11 + mth) // 12, (11 + mth) % 12 + 1
        a = bisect.bisect_left(ctx.p.dates, f"{y}-{m:02d}-01")
        shifts.append(ctx.run(full(), sc, first=a)["vs_nikkei"])
    ok = (all(checks.values()) and stress["vs_nikkei"] >= cfg["pass"]["stress_excess_pt"]
          and sum(1 for x in shifts if x > 0) >= cfg["pass"]["start_shift_wins"])
    res = {"meta": {"run_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "data": [ctx.p.dates[0], ctx.p.dates[-1]], "start": ctx.p.dates[ctx.first], "weights": WEIGHTS},
           "bench": bench, "result": s, "checks": {k: bool(v) for k, v in checks.items()}, "stress": stress,
           "start_shift_vs_nikkei": shifts, "pass": bool(ok)}
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "backtest_realloc.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=float)
    b = bench["nikkei_1321"]
    md = [f"# 検証7 配分見直し (大型株70%・成長株30%)\n",
          f"データ {res['meta']['data'][0]}〜{res['meta']['data'][1]}、検証開始 {res['meta']['start']}。"
          f"日経1321: 年率 {b['annual']}%・最大DD {b['max_dd']}%。\n",
          f"- 年率 {s['annual']}% / 日経比 {s['vs_nikkei']:+}pt / 最大DD {s['max_dd']}% / PF {s.get('pf')} / 取引 {s.get('trades')}",
          f"- 合格基準: " + ", ".join(f"{k} {'○' if v else '×'}" for k, v in res["checks"].items()),
          f"- コスト試験: 年率 {stress['annual']}% / 日経比 {stress['vs_nikkei']:+}pt",
          f"- 開始日ずらし (日経比pt): {', '.join(f'{x:+}' for x in shifts)} → {sum(1 for x in shifts if x > 0)}/12 勝ち",
          f"- 年別: " + ", ".join(f"{k} {v}" for k, v in s["yearly"].items()),
          f"- **判定: {'合格' if ok else '不合格'}**"]
    with open(os.path.join(OUT, "backtest_realloc.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    print("\n".join(md), flush=True)


if __name__ == "__main__":
    main()
