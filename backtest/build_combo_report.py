"""backtest/results/combo.json から HTML レポートを作る。

使い方:
    python backtest/build_combo_report.py COMBO_JSON OUT_HTML [--summary SUMMARY_HTML] [--pick N,N]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import build_opt_report as ob
import build_rule_report as rr

e, num, tone = ob.e, ob.num, ob.tone
DIMS = (("sig", "条件"), ("uni", "銘柄の範囲"), ("reg", "相場フィルター"), ("park", "空き資金"), ("rule", "取引ルール"))


def pct(v, d=1):
    return f"{num(v, d)}%" if v is not None else "—"


def desc(res, r):
    L = res["labels"]
    return (f'{e(L["sig"][r["sig"]])}<small>{e(L["uni"][r["uni"]])} · {e(L["reg"][r["reg"]])} · '
            f'空き資金は{e(L["park"][r["park"]])} · {e(L["rule"][r["rule"]])}</small>')


def effects_table(res):
    head = ("<thead><tr><th></th><th>探索 日経との差 (年率)</th><th>日経に勝った割合</th>"
            "<th>検証 日経との差</th><th>日経に勝った割合</th><th>通し 最大DD</th></tr></thead>")
    rows = []
    for dim, title in DIMS:
        rows.append(f'<tr class="old"><th scope="row" colspan="6">{title}</th></tr>')
        for v, x in sorted(res["effects"][dim].items(), key=lambda kv: -kv[1]["is"]["excess"]):
            rows.append(f'<tr><th scope="row">{e(res["labels"][dim][v])}</th>'
                        f'<td{tone(x["is"]["excess"], 15)}>{pct(x["is"]["excess"])}</td><td>{num(x["is"]["beat"], 0, False)}%</td>'
                        f'<td{tone(x["oos"]["excess"], 15)}>{pct(x["oos"]["excess"])}</td><td>{num(x["oos"]["beat"], 0, False)}%</td>'
                        f'<td>{pct(x["all"]["max_dd"])}</td></tr>')
    return f'<div class="tw"><table>{head}<tbody>{"".join(rows)}</tbody></table></div>'


def top_table(res, picks):
    head = ("<thead><tr><th>組み合わせ</th><th>探索 順位</th><th>探索 日経との差</th><th>探索 最大DD</th>"
            "<th>検証 損益</th><th>日経との差</th><th>最大DD</th><th>通し 年率</th><th>最大DD</th></tr></thead>")
    rows = []
    B = res["bench"]
    for n in list(res["is_order"]) + [p for p in picks if p not in res["is_order"]]:
        r = res["rows"][n]
        tag = '<span class="tag new">通しで日経超え</span>' if n in picks else ""
        rows.append(f'<tr><th scope="row">{tag}{desc(res, r)}</th><td>{r["is_rank"]}</td>'
                    f'<td{tone(r["is"]["excess"], 15)}>{pct(r["is"]["excess"])}</td><td>{pct(r["is"]["max_dd"])}</td>'
                    f'<td>{pct(r["oos"]["total"])}</td><td{tone(r["oos"]["excess"], 15)}>{pct(r["oos"]["excess"])}</td>'
                    f'<td>{pct(r["oos"]["max_dd"])}</td><td>{pct(r["all"]["annual"])}</td><td>{pct(r["all"]["max_dd"])}</td></tr>')
    rows.append(f'<tr class="old"><th scope="row"><span class="tag">比較</span>日経平均 (買って持つだけ)</th><td></td>'
                f'<td>±0</td><td>{pct(B["is"]["max_dd"])}</td><td>{pct(B["oos"]["total"])}</td><td>±0</td>'
                f'<td>{pct(B["oos"]["max_dd"])}</td><td>{pct(B["all"]["annual"])}</td><td>{pct(B["all"]["max_dd"])}</td></tr>')
    return f'<div class="tw"><table>{head}<tbody>{"".join(rows)}</tbody></table></div>'


def curve(res, ids, pid):
    fake = {"composite": {}, "strategies": {}, "bench": {pid: {"nikkei": res["bench"][pid]}}}
    for n in ids:
        r = res["rows"][n]
        fake["strategies"][str(n)] = {"name": f'{res["labels"]["sig"][r["sig"]]} ({res["labels"]["park"][r["park"]]})',
                                      pid: {"curve": r[pid].get("curve") or []}}
    return rr.curve(fake, pid, [("s", str(n)) for n in ids])


def build(res, summary, picks):
    m, B = res["meta"], res["bench"]
    P = m["periods"]
    demo = '<p class="warn">これは乱数のデモデータです。実際の結果ではありません。</p>' if m["demo"] else ""
    show = picks or res["is_order"][:3]
    return f"""<title>日経平均に勝つ組み合わせ</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Zen+Kaku+Gothic+New:wght@400;500;700&family=Shippori+Mincho+B1:wght@600;800&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{rr.CSS}
td small, th small {{ display: block; font-size: 11px; color: var(--mute); font-weight: 400; }}
.tw table {{ font-size: 13px; }}
.tw td, .tw th {{ padding-left: 6px; padding-right: 6px; }}
.tw tbody th {{ min-width: 240px; max-width: 330px; white-space: normal; }}
</style>
<div class="wrap">
<header>
  <div class="eyebrow">Backtest · 組み合わせ</div>
  <h1>日経平均に勝つ組み合わせ</h1>
  <div class="meta">
    <span>探索 <b>{P["is"][0]} 〜 {P["is"][1]}</b> 日経 年率 <b>{pct(B["is"]["annual"])}</b></span>
    <span>検証 <b>{P["oos"][0]} 〜 {P["oos"][1]}</b> 日経 <b>{pct(B["oos"]["total"])}</b></span>
    <span>組み合わせ <b>{m["combos"]}</b></span>
  </div>
  {demo}
</header>

<section class="summary">
  <h2>結論</h2>
  {summary}
</section>

<section>
  <h2>何が効いたか</h2>
  <p class="lead">要素ごとに、ほかの要素をすべての組み合わせで平均した「日経平均との年率の差」です。探索期間で効いた要素が、選ぶときに使っていない直近1年でも効いているかを見ます。</p>
  {effects_table(res)}
</section>

<section>
  <h2>探索期間の上位</h2>
  <p class="lead">探索期間の「日経平均との年率の差」で並べた上位{len(res["is_order"])}件。直近1年の列は選ぶときに使っていません。</p>
  {top_table(res, picks)}
</section>

<section>
  <h2>資産の推移 (直近1年)</h2>
  {curve(res, show, "oos")}
  <h2>資産の推移 (通し)</h2>
  {curve(res, show, "all")}
</section>

<section>
  <h2>前提と注意点</h2>
  <div class="notes">
    <div><h3>空き資金を日経平均で持つ</h3><p>10枠のうち使っていない分を、日経平均に連動するETF (1321など) で持つ想定です。銘柄を買う日に必要な分だけETFを売ります。ETFの売買コストと信託報酬は入れておらず、日経平均の配当 (年2%前後) も入れていないので、その2つはおおむね相殺されます。</p></div>
    <div><h3>銘柄の範囲</h3><p>大型は、その日までの20日平均売買代金が上位{m["large_n"]}位の銘柄です (当日に分かる値なので先読みなし)。全体は売買代金1億円以上の全銘柄です。</p></div>
    <div><h3>相場フィルター</h3><p>シグナルの日に日経平均が50日線 (または200日線) より上のときだけ新しく買います。持っている銘柄は、ルールどおりに手仕舞います。</p></div>
    <div><h3>共通の条件</h3><p>シグナル翌営業日の寄り付きで買い、決算 (推定日) が保有予定期間に入る銘柄は買いません。同時保有は{m["slots"]}銘柄で、売買コストは売買代金に応じて片道0.10〜0.40%、税引き前です。上場廃止銘柄は含みません。</p></div>
  </div>
</section>
</div>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("out")
    ap.add_argument("--summary", default=None)
    ap.add_argument("--pick", default="")
    a = ap.parse_args()
    res = json.loads(Path(a.results).read_text(encoding="utf-8"))
    summary = Path(a.summary).read_text(encoding="utf-8") if a.summary else "<p>(まとめは結果を見て記入)</p>"
    picks = [int(x) for x in a.pick.split(",") if x]
    Path(a.out).write_text(build(res, summary, picks), encoding="utf-8")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
