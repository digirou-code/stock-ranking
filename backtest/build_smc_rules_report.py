"""backtest/results/smc_rules.json (3つの取引ルールでの SMC/ICT 検証) から HTML レポートを作る。

使い方:
    python backtest/build_smc_rules_report.py SMC_RULES_JSON OUT_HTML [--summary SUMMARY_HTML]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import build_opt_report as ob
import build_rule_report as rr
import build_smc_report as sr

e, num, tone, pct = ob.e, ob.num, ob.tone, sr.pct


def rule_label(r):
    t2 = "なしで残り" if r["take2_pct"] >= 1000 else f'+{r["take2_pct"]:.0f}%で残り'
    return f'損切り{r["stop_pct"]:.0f}% / +{r["take1_pct"]:.0f}%で半分・{t2} / {r["max_days"]}日'


def short(r):
    t2 = "残りなし" if r["take2_pct"] >= 1000 else f'+{r["take2_pct"]:.0f}%'
    return f'{r["stop_pct"]:.0f}% / +{r["take1_pct"]:.0f}%・{t2} / {r["max_days"]}日'


def overview(res, pid):
    rids = list(res)
    first = res[rids[0]]
    head = "<thead><tr><th></th>" + "".join(
        f'<th>{e(short(res[r]["meta"]["rule"]))}<small>損益 · 最大DD · 期待値</small></th>' for r in rids) + "</tr></thead>"

    def cell(x):
        v = x[pid]["cash"]["annual" if pid != "oos" else "total"]
        return (f'<td{tone(v, 30)}>{pct(v)}<small>DD {pct(x[pid]["cash"]["max_dd"])} · '
                f'{pct(x[pid]["net"], 2)}/回</small></td>')

    rows = []
    for s in first["signals"]:
        rows.append(f'<tr><th scope="row">{e(first["names"][s])}</th>'
                    + "".join(cell(res[r]["signals"][s]["best"]) for r in rids) + "</tr>")
    for key, name in (("sm", "SCORE勢い 上位10"), ("random", "ランダム")):
        rows.append(f'<tr class="old"><th scope="row"><span class="tag">比較</span>{name}</th>'
                    + "".join(cell(res[r][key]) for r in rids) + "</tr>")
    b = first["bench"][pid]
    v = b["annual"] if pid != "oos" else b["total"]
    rows.append(f'<tr class="old"><th scope="row"><span class="tag">比較</span>日経平均</th>'
                + "".join(f'<td>{pct(v)}<small>DD {pct(b["max_dd"])}</small></td>' for _ in rids) + "</tr>")
    return f'<div class="tw"><table>{head}<tbody>{"".join(rows)}</tbody></table></div>'


def beat_table(res):
    """日経平均に勝った条件の数 (ルール別)"""
    head = "<thead><tr><th>取引ルール</th><th>探索 (年率)</th><th>直近1年</th><th>通し (年率)</th><th>3期間とも</th></tr></thead>"
    rows = []
    for rid, R in res.items():
        B = R["bench"]
        c = {pid: 0 for pid in ("is", "oos", "all")}
        both = 0
        for s in R["signals"].values():
            x = s["best"]
            w = {pid: x[pid]["cash"]["annual"] > B[pid]["annual"] for pid in c}
            for pid in c:
                c[pid] += w[pid]
            both += all(w.values())
        n = len(R["signals"])
        rows.append(f'<tr><th scope="row">{e(rule_label(R["meta"]["rule"]))}</th>'
                    + "".join(f"<td>{c[p]} / {n}</td>" for p in ("is", "oos", "all")) + f"<td>{both} / {n}</td></tr>")
    return f'<div class="tw"><table>{head}<tbody>{"".join(rows)}</tbody></table></div>'


def curve(res, pid="oos"):
    fake = {"composite": {}, "strategies": {}, "bench": {pid: {"nikkei": next(iter(res.values()))["bench"][pid]}}}
    ids = []
    for rid, R in res.items():
        k = f"choch_{rid}"
        fake["strategies"][k] = {"name": f'CHoCH {rule_label(R["meta"]["rule"])}',
                                 pid: {"curve": R["signals"]["choch"]["best"][pid]["cash"].get("curve") or []}}
        ids.append(("s", k))
    return rr.curve(fake, pid, ids)


def build(res, summary):
    first = next(iter(res.values()))
    m = first["meta"]
    P = m["periods"]
    demo = '<p class="warn">これは乱数のデモデータです。実際の結果ではありません。</p>' if m["demo"] else ""
    details = "".join(f'<h3>{e(rule_label(R["meta"]["rule"]))}</h3>{sr.main_table(R)}' for R in res.values())
    defs = "".join(f'<tr><th scope="row">{e(a)}</th><td class="txt">{e(b)}</td></tr>' for a, b in sr.DEFS)
    return f"""<title>SMC・ICT ルール別検証</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Zen+Kaku+Gothic+New:wght@400;500;700&family=Shippori+Mincho+B1:wght@600;800&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{rr.CSS}
td small, th small {{ display: block; font-size: 11px; color: var(--mute); font-weight: 400; }}
.tw table {{ font-size: 13px; }}
.tw td, .tw th {{ padding-left: 6px; padding-right: 6px; }}
td.txt {{ text-align: left; white-space: normal; font-family: inherit; }}
section h3 {{ margin-top: 28px; }}
</style>
<div class="wrap">
<header>
  <div class="eyebrow">Backtest · SMC / ICT</div>
  <h1>SMC・ICT ルール別検証</h1>
  <div class="meta">
    <span>探索 <b>{P["is"][0]} 〜 {P["is"][1]}</b></span>
    <span>直近1年 <b>{P["oos"][0]} 〜 {P["oos"][1]}</b></span>
    <span>銘柄 <b>{m["tickers"]:,}</b></span>
  </div>
  {demo}
</header>

<section class="summary">
  <h2>結論</h2>
  {summary}
</section>

<section>
  <h2>日経平均に勝った条件の数</h2>
  <p class="lead">8条件のうち、10枠運用 (空き資金は現金) の成績が日経平均を上回った数です。</p>
  {beat_table(res)}
</section>

<section>
  <h2>直近1年の成績</h2>
  <p class="lead">ルールごとにスイング判定の幅 k を探索期間で選び直しています。直近1年は選ぶときに使っていません。</p>
  {overview(res, "oos")}
  {curve(res, "oos")}
</section>

<section>
  <h2>探索期間の年率</h2>
  {overview(res, "is")}
</section>

<section>
  <h2>通しの年率 ({P["all"][0]} 〜 {P["all"][1]})</h2>
  {overview(res, "all")}
  {curve(res, "all")}
</section>

<section>
  <h2>ルール別の詳細</h2>
  <p class="lead">期待値はコスト控除後の1回あたりの平均損益。右端は空き資金を日経平均で持った場合の直近1年の損益です。</p>
  {details}
</section>

<section>
  <h2>条件の定義 (日足・買いのみ)</h2>
  <div class="tw"><table><tbody>{defs}</tbody></table></div>
</section>

<section>
  <h2>前提と注意点</h2>
  <div class="notes">
    <div><h3>共通の条件</h3><p>シグナル翌営業日の寄り付きで買い、決算 (推定日) が保有予定期間に入る銘柄は買いません。同時保有は{m["slots"]}銘柄、売買代金1億円以上の銘柄だけが対象で、10枠が埋まっているときは売買代金の大きい順に買います。</p></div>
    <div><h3>コスト・データ</h3><p>売買コストは売買代金に応じて片道0.10〜0.40%、税引き前です。Yahoo Finance の日足 (調整済み) で、上場廃止銘柄は含みません。日経平均は配当なしの指数です。</p></div>
    <div><h3>日足で機械化したもの</h3><p>SMC・ICT は本来、複数の時間足を見て裁量で判断する手法です。ここでは日足だけで判定できる形にしたため、使う人の見方とは違う場合があります。</p></div>
  </div>
</section>
</div>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("out")
    ap.add_argument("--summary", default=None)
    a = ap.parse_args()
    res = json.loads(Path(a.results).read_text(encoding="utf-8"))
    summary = Path(a.summary).read_text(encoding="utf-8") if a.summary else "<p>(まとめは結果を見て記入)</p>"
    Path(a.out).write_text(build(res, summary), encoding="utf-8")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
