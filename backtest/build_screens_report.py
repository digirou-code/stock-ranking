"""backtest/results/screens.json から HTML レポートを作る。

使い方:
    python backtest/build_screens_report.py SCREENS_JSON OUT_HTML [--summary SUMMARY_HTML]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import build_opt_report as ob
import build_rule_report as rr

e, num, tone = ob.e, ob.num, ob.tone

HEAD = ('<thead><tr><th></th><th>取引回数</th><th>勝率</th><th>平均利益</th><th>平均損失</th><th>損益比</th>'
        '<th>期待値</th><th>PF</th><th>最大DD</th><th>年率</th><th>期間損益</th><th>株価だけの条件</th></tr></thead>')


def mark(R):
    if not R["caveat"]:
        return '<span class="tag new">○</span>'
    if R["price"]:
        return '<span class="tag">△</span>'
    return '<span class="tag">×</span>'


def row(R, pid):
    r = R["site"][pid]
    p = R["price"].get(pid) if R["price"] else None
    last = (f'<td>{num(p.get("pf_total"), 1)}%<small>PF {num(p.get("pf"), 2, False)} · DD {num(p.get("pf_dd"), 1)}%</small></td>'
            if p and p.get("n") else '<td class="na">—</td>')
    name = f'<th scope="row">{mark(R)}{e(R["name"])}<small>{e(R["tab"])}</small></th>'
    if not r.get("n"):
        return f'<tr>{name}<td colspan="10" class="na">取引なし</td>{last}</tr>'
    return (f'<tr>{name}{ob.metric_cells(r)}<td{tone(r.get("pf_total"), 40)}>{num(r.get("pf_total"), 1)}%</td>{last}</tr>')


def table(res, pid):
    order = sorted(res["screens"].items(), key=lambda kv: -(kv[1]["site"][pid].get("pf_total") or -1e9))
    rows = [row(R, pid) for _, R in order]
    rnd = res["random"][pid]
    rows.append(f'<tr class="old"><th scope="row"><span class="tag">比較</span>ランダム<small>売買代金1億円以上から無作為・同じルール・5回の平均</small></th>'
                f'<td class="na">—</td><td>{num(rnd.get("win"), 1, False)}%</td><td class="na">—</td><td class="na">—</td>'
                f'<td class="na">—</td><td>{num(rnd.get("net"))}%</td><td>{num(rnd.get("pf"), 2, False)}</td>'
                f'<td>{num(rnd.get("pf_dd"), 1)}%</td><td>{num(rnd.get("pf_annual"), 1)}%</td>'
                f'<td>{num(rnd.get("pf_total"), 1)}%</td><td></td></tr>')
    b = res["bench"].get(pid)
    if b:
        rows.append(f'<tr class="old"><th scope="row"><span class="tag">比較</span>日経平均<small>買って持つだけ (配当なし)</small></th>'
                    f'<td colspan="7" class="na"></td><td>{num(b["max_dd"], 1)}%</td><td>{num(b["annual"], 1)}%</td>'
                    f'<td>{num(b["total"], 1)}%</td><td></td></tr>')
    return f'<div class="tw"><table>{HEAD}<tbody>{"".join(rows)}</tbody></table></div>'


def curve(res, pid, top_ids):
    series = [(res["screens"][s]["name"], res["screens"][s]["site"][pid].get("curve") or []) for s in top_ids]
    series.append(("ランダム (5回のうち1回)", res["random"][pid].get("curve") or []))
    if res["bench"].get(pid):
        series.append(("日経平均", res["bench"][pid]["curve"]))
    fake = {"composite": {}, "strategies": {}, "bench": {pid: None}}
    for n, (name, cv) in enumerate(series):
        fake["strategies"][f"s{n}"] = {"name": name, pid: {"curve": cv}}
    return rr.curve(fake, pid, [("s", f"s{n}") for n in range(len(series))])


def caveat_table(res):
    rows = []
    for R in res["screens"].values():
        rows.append(f'<tr><th scope="row">{mark(R)}{e(R["name"])}</th>'
                    f'<td class="txt">{e(R["caveat"] or "株価と出来高だけで判定できるので、そのまま検証")}</td></tr>')
    return f'<div class="tw"><table><tbody>{"".join(rows)}</tbody></table></div>'


def earn_table(res):
    rows = []
    for R in res["screens"].values():
        a, b = R["site"]["oos"], R["no_earn"]["oos"]
        if not a.get("n") and not b.get("n"):
            continue
        rows.append(f'<tr><th scope="row">{e(R["name"])}</th><td>{num(a.get("pf_total"), 1)}%</td><td>{num(a.get("pf_dd"), 1)}%</td>'
                    f'<td>{num(b.get("pf_total"), 1)}%</td><td>{num(b.get("pf_dd"), 1)}%</td></tr>')
    return ('<div class="tw"><table><thead><tr><th></th><th>決算回避あり 損益</th><th>最大DD</th>'
            f'<th>回避なし 損益</th><th>最大DD</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def build(res, summary):
    m = res["meta"]
    P, r = m["periods"], m["rule"]
    tiers = " / ".join(f"{t[0]}億円以上 {t[1]:.2f}%" if t[0] else f"1億円未満 {t[1]:.2f}%" for t in m["cost_tiers"])
    demo = '<p class="warn">これは乱数のデモデータです。実際の結果ではありません。</p>' if m["demo"] else ""
    top = sorted(res["screens"], key=lambda s: -(res["screens"][s]["site"]["oos"].get("pf_total") or -1e9))[:3]
    return f"""<title>全スクリーニング検証</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Zen+Kaku+Gothic+New:wght@400;500;700&family=Shippori+Mincho+B1:wght@600;800&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{rr.CSS}
td small {{ display: block; font-size: 11px; color: var(--mute); }}
.rules {{ display: flex; flex-wrap: wrap; gap: 8px; }}
.rules span {{ font-family: var(--mono); font-size: 13px; border: 1px solid var(--line); border-radius: 4px; padding: 2px 10px; background: var(--panel); }}
.tw table {{ font-size: 13px; }}
.tw td, .tw th {{ padding-left: 7px; padding-right: 7px; }}
.legendrow {{ display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 13px; color: var(--mute); }}
</style>
<div class="wrap">
<header>
  <div class="eyebrow">Backtest · 全タブ</div>
  <h1>全スクリーニング検証</h1>
  <div class="rules">
    <span>翌営業日の寄り付きで買い</span><span>損切り {r["stop_pct"]:+.0f}%</span>
    <span>+{r["take1_pct"]:.0f}%で半分</span><span>+{r["take2_pct"]:.0f}%で残り</span>
    <span>{r["max_days"]}営業日で手仕舞い</span><span>最大{m["slots"]}銘柄</span><span>決算回避</span>
  </div>
  <div class="meta">
    <span>直近1年 <b>{P["oos"][0]} 〜 {P["oos"][1]}</b></span>
    <span>通し <b>{P["all"][0]} 〜 {P["all"][1]}</b></span>
    <span>銘柄 <b>{m["tickers"]:,}</b></span>
  </div>
  {demo}
</header>

<section class="summary">
  <h2>結論</h2>
  {summary}
</section>

<section>
  <h2>直近1年の成績</h2>
  <p class="lead">期待値は1回あたりのコスト控除後の平均損益 (半分ずつ売った分を合わせた損益)。最大DD・年率・期間損益は資金を{m["slots"]}枠に分けて運用した場合 (1枠=資産の1/{m["slots"]}、複利、含み損益込み)。10枠が埋まっているときは、サイトの各タブの並び順で上位から買います。期間損益の高い順に並べています。</p>
  <div class="legendrow"><span><span class="tag new">○</span>株価だけで判定できる</span><span><span class="tag">△</span>財務は現在値 (右端に株価だけの条件で検証した版)</span><span><span class="tag">×</span>財務だけのタブ (先読みを外せない)</span></div>
  {table(res, "oos")}
  {curve(res, "oos", top)}
</section>

<section>
  <h2>通しの成績</h2>
  <p class="lead">{P["all"][0]} 〜 {P["all"][1]}。1年だけの相場の偏りを除いて見るためのものです。</p>
  {table(res, "all")}
</section>

<section>
  <h2>検証できた範囲</h2>
  {caveat_table(res)}
</section>

<section>
  <h2>決算回避の効果 (直近1年)</h2>
  <p class="lead">過去の決算日は一括で取れないため、次回の決算予定日から3ヶ月ごとにさかのぼった日を推定日として使いました (決算予定日がわかる銘柄 {m["earn_tickers"]:,} 社)。実際の発表日とは数日ずれることがあります。</p>
  {earn_table(res)}
</section>

<section>
  <h2>前提と注意点</h2>
  <div class="notes">
    <div><h3>財務の先読み</h3><p>ROE・利益率・成長率・自己資本比率・配当は過去の値が無料で取れず、現在の値をさかのぼって当てています。PER・配当利回り・時価総額だけは、その日の株価で計算し直しました。今の決算が良い銘柄を過去に選んでいることになるので、△と×のタブは実際より良く出やすいです。</p></div>
    <div><h3>信用倍率</h3><p>銘柄別の信用残の過去データが無料で集められないため、信用過多押し目は信用倍率10倍以上の条件を外して検証しました。実際のタブより候補が多い状態の成績です。</p></div>
    <div><h3>SCOREのタブ</h3><p>SCORE業績・SCORE勢いは、その日のスコア上位{m["slots"]}銘柄を買い候補としました。SCORE勢いは株価だけで作れるので先読みはありません。SCORE業績は全項目が財務です。</p></div>
    <div><h3>データ</h3><p>Yahoo Finance の日足 (調整済み)。現在上場している{m["tickers"]:,}銘柄のみで、上場廃止銘柄は含みません (生存者バイアス)。株価データの誤りと思われる取引 {m["dropped_trades"]} 件は除外しました。</p></div>
    <div><h3>売買コスト・税</h3><p>手数料0円、スプレッドとスリッページは片道 {tiers}。数値は税引き前で、利益には年間の損益通算後に{m["tax"]}%の税がかかります。為替コストはありません。</p></div>
    <div><h3>約定</h3><p>寄り付きで買えなかった日 (ストップ高・安張り付きなど) は除外。損切り・利確の価格に場中で届いたらその価格、寄り付きで越えていたら始値で約定。同じ日に両方に届いた場合は損切りを先とみなします。</p></div>
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
