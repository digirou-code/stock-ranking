"""backtest/results/optimize.json から、条件見直しのHTMLレポートを作る。

使い方:
    python backtest/build_opt_report.py OPTIMIZE_JSON OUT_HTML [--summary SUMMARY_HTML]
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

COLORS = ["--c1", "--c3", "--c5", "--c6"]


def e(s):
    return html.escape(str(s))


def num(v, d=2, sign=True, unit=""):
    if v is None:
        return '<span class="na">—</span>'
    return (f"{v:+.{d}f}" if sign else f"{v:.{d}f}") + unit


def tone(v, scale=1.0):
    if v is None:
        return ""
    a = max(-1.0, min(1.0, v / scale))
    return f' class="{"pos" if a > 0 else "neg"}" style="--a:{abs(a) * 0.55:.2f}"'


def metric_cells(r):
    if not r or not r.get("n"):
        return '<td colspan="9" class="na">取引なし</td>'
    return (f'<td>{r["n"]:,}</td><td>{num(r["win"], 1, False)}%</td><td>{num(r.get("avg_win"))}%</td>'
            f'<td>{num(r.get("avg_loss"))}%</td><td>{num(r.get("payoff"), 2, False)}</td>'
            f'<td{tone(r["net"], 1.5)}><b>{num(r["net"])}%</b></td>'
            f'<td{tone((r.get("pf") or 1) - 1, 0.5)}>{num(r.get("pf"), 2, False)}</td>'
            f'<td>{num(r.get("pf_dd"), 1)}%</td><td{tone(r.get("pf_annual"), 30)}>{num(r.get("pf_annual"), 1)}%</td>')


HEAD = ('<thead><tr><th></th><th>取引回数</th><th>勝率</th><th>平均利益</th><th>平均損失</th><th>損益比</th>'
        '<th>期待値</th><th>PF</th><th>最大DD</th><th>年率</th></tr></thead>')


def family_card(fid, F, m, n):
    if "cond" not in F:
        return (f'<article class="fam"><h3>{e(F["name"])}</h3><p class="lead">{F["tested"]}通りを試しましたが、'
                f'探索期間で取引{m["min_trades"]}回以上・PF {m["min_pf"]}以上を満たす条件がありませんでした。</p></article>')
    conds = "".join(f"<li>{e(c)}</li>" for c in F["cond"])
    ex = F["is"].get("exit", {})
    rows = (f'<tr><th scope="row">探索期間<small>{m["is_start"]}〜{m["is_end"]}</small></th>{metric_cells(F["is"])}</tr>'
            f'<tr class="oos"><th scope="row">検証期間<small>{m["oos_start"]}〜{m["oos_end"]}</small></th>{metric_cells(F["oos"])}</tr>'
            f'<tr class="sub"><th scope="row">検証期間・10日保有<small>損切り・利確なし</small></th>{metric_cells(F["oos_hold10"])}</tr>')
    oex = F["oos"].get("exit", {}) if F["oos"].get("n") else {}
    mix = (f'手仕舞いの内訳 (検証期間): 利確 {oex.get("take", 0):.0f}% · 損切り {oex.get("stop", 0):.0f}% · '
           f'時間切れ {oex.get("time", 0):.0f}% · 平均{F["oos"].get("hold_days", 0):.1f}日') if oex else ""
    return f"""<article class="fam">
  <div class="famhead"><span class="dot" style="background:var({COLORS[n % 4]})"></span><h3>{e(F["name"])}</h3>
  <span class="chip">{F["tested"]}通りから選択</span></div>
  <div class="spec">
    <div><h4>買い条件 (すべて満たす)</h4><ul>{conds}</ul></div>
    <div><h4>手仕舞い</h4><p class="exit">{e(F["exit"]["label"])}</p><p class="mix">{mix}</p></div>
  </div>
  <div class="tw"><table>{HEAD}<tbody>{rows}</tbody></table></div>
</article>"""


def compare_table(res):
    rows = []
    for fid, F in res["families"].items():
        if "cond" in F:
            rows.append(f'<tr><th scope="row"><span class="tag new">改良</span>{e(F["name"])}</th>{metric_cells(F["oos"])}</tr>')
    for cid, B in res["baseline"].items():
        rows.append(f'<tr class="old"><th scope="row"><span class="tag">元</span>{e(B["name"])}</th>{metric_cells(B["oos"])}</tr>')
    return f'<div class="tw"><table>{HEAD}<tbody>{"".join(rows)}</tbody></table></div>'


def robust_table(res):
    out = []
    for fid, F in res["families"].items():
        rb = F.get("robust") or []
        if not rb:
            continue
        pos = sum(1 for r in rb if (r.get("oos_net") or -1) > 0)
        rows = "".join(
            f'<tr><td class="txt">{e(" / ".join(r["cond"][:1] + r["cond"][3:] if fid == "rebound" else r["cond"][1:]))}</td>'
            f'<td{tone(r.get("is_net"), 1.5)}>{num(r.get("is_net"))}%</td>'
            f'<td{tone(r.get("oos_net"), 1.5)}>{num(r.get("oos_net"))}%</td>'
            f'<td>{num(r.get("oos_pf"), 2, False)}</td><td>{r.get("oos_n") or 0:,}</td></tr>' for r in rb)
        out.append(f'<h3>{e(F["name"])} <span class="chip">検証期間でプラス {pos}/{len(rb)}</span></h3>'
                   f'<div class="tw"><table><thead><tr><th class="txt">条件 (探索期間の上位順)</th><th>探索 期待値</th>'
                   f'<th>検証 期待値</th><th>検証 PF</th><th>検証 回数</th></tr></thead><tbody>{rows}</tbody></table></div>')
    return "".join(out)


def curve_svg(res):
    series = [(F["name"], (F["oos"] or {}).get("curve") or []) for F in res["families"].values() if "cond" in F]
    dates = sorted({d for _, cv in series for d, _ in cv})
    if not dates:
        return ""
    vals = [v for _, cv in series for _, v in cv]
    lo, hi = min(0, min(vals)), max(0, max(vals))
    span = hi - lo or 1
    step = next(s for s in (1, 2, 5, 10, 20, 25, 50, 100) if span / s <= 8)
    lo_t, hi_t = (lo // step) * step, -((-hi) // step) * step
    W, H, L, R, T, B = 760, 300, 52, 16, 14, 30
    x = lambda k: L + (W - L - R) * k / max(1, len(dates) - 1)
    y = lambda v: T + (H - T - B) * (hi_t - v) / ((hi_t - lo_t) or 1)
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="検証期間の10枠運用の損益推移">']
    v = lo_t
    while v <= hi_t + 1e-9:
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="{"zero" if v == 0 else "gl"}"/>'
                   f'<text x="{L - 6}" y="{y(v) + 4:.1f}" class="ax" text-anchor="end">{v:+.0f}%</text>')
        v += step
    seen = set()
    for k, d in enumerate(dates):
        lab = d[:7]
        if lab in seen or int(d[5:7]) % 2:
            continue
        seen.add(lab)
        out.append(f'<text x="{x(k):.1f}" y="{H - 10}" class="ax" text-anchor="middle">{lab.replace("-", "/")}</text>')
    pos = {d: k for k, d in enumerate(dates)}
    for n, (name, cv) in enumerate(series):
        if not cv:
            continue
        pts = " ".join(f"{x(pos[d]):.1f},{y(v):.1f}" for d, v in cv)
        out.append(f'<polyline points="{pts}" fill="none" style="stroke:var({COLORS[n % 4]})" class="ln"/>')
        d, v = cv[-1]
        out.append(f'<circle cx="{x(pos[d]):.1f}" cy="{y(v):.1f}" r="3.5" style="fill:var({COLORS[n % 4]})"/>')
    out.append("</svg>")
    legend = "".join(f'<span><i style="background:var({COLORS[n % 4]})"></i>{e(name)} <b>{num(cv[-1][1], 1) if cv else "—"}%</b></span>'
                     for n, (name, cv) in enumerate(series))
    return f'<div class="chart">{"".join(out)}</div><div class="legend">{legend}</div>'


def build(res, summary):
    m = res["meta"]
    tiers = " / ".join(f"{t[0]}億円以上 {t[1]:.2f}%" if t[0] else f"1億円未満 {t[1]:.2f}%" for t in m["cost_tiers"])
    cards = "".join(family_card(fid, F, m, n) for n, (fid, F) in enumerate(res["families"].items()))
    total = sum(F["tested"] for F in res["families"].values())
    demo = '<p class="warn">これは乱数のデモデータです。実際の結果ではありません。</p>' if m["demo"] else ""
    stops = "・".join("なし" if s is None else f"{s}ATR" for s in m["stops"])
    takes = "・".join("なし" if s is None else f"{s}ATR" for s in m["takes"])
    days = "・".join(f"{d}日" for d in m["max_days"])
    return f"""<title>スイング条件 改良版</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Zen+Kaku+Gothic+New:wght@400;500;700&family=Shippori+Mincho+B1:wght@600;800&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* 売買帳の続き: 紙の帳面に細い罫、数値は等幅。利益=朱、損失=藍 */
:root {{
  --bg: #f5f5f1; --panel: #ffffff; --fg: #1d2129; --mute: #5d6470; --line: #dadbd3;
  --up: 190, 52, 38; --dn: 36, 86, 160; --accent: #87580f;
  --c1: #c43728; --c3: #2a6db5; --c5: #3d8748; --c6: #7a5bb0;
  --display: "Shippori Mincho B1", "Hiragino Mincho ProN", "Yu Mincho", serif;
  --body: "Zen Kaku Gothic New", "Hiragino Sans", "Yu Gothic", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #15181d; --panel: #1c2027; --fg: #e6e7e3; --mute: #9aa1ab; --line: #303641;
  --up: 238, 105, 88; --dn: 104, 158, 230; --accent: #e0b064;
  --c1: #ee6958; --c3: #6aa2ea; --c5: #74c083; --c6: #b49ae6; color-scheme: dark; }} }}
:root[data-theme="dark"] {{
  --bg: #15181d; --panel: #1c2027; --fg: #e6e7e3; --mute: #9aa1ab; --line: #303641;
  --up: 238, 105, 88; --dn: 104, 158, 230; --accent: #e0b064;
  --c1: #ee6958; --c3: #6aa2ea; --c5: #74c083; --c6: #b49ae6; color-scheme: dark; }}
body {{ background: var(--bg); color: var(--fg); font-family: var(--body); font-size: 15px; line-height: 1.75; }}
.wrap {{ max-width: 1040px; margin: 0 auto; padding-block: 36px 64px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  display: grid; gap: 48px; }}
header {{ display: grid; gap: 10px; border-bottom: 2px solid var(--fg); padding-bottom: 18px; }}
.eyebrow {{ font-family: var(--mono); font-size: 12px; letter-spacing: .08em; color: var(--accent); text-transform: uppercase; }}
h1 {{ font-family: var(--display); font-weight: 800; font-size: clamp(28px, 5vw, 40px); line-height: 1.25; margin: 0; text-wrap: balance; }}
h2 {{ font-family: var(--display); font-weight: 800; font-size: 22px; margin: 0; text-wrap: balance; }}
h3 {{ font-size: 16px; margin: 0; }}
h4 {{ font-size: 12px; margin: 0 0 4px; color: var(--mute); font-weight: 500; letter-spacing: .04em; }}
section {{ display: grid; gap: 14px; min-width: 0; }}
p {{ margin: 0; max-width: 70ch; }}
.meta {{ display: flex; flex-wrap: wrap; gap: 6px 22px; color: var(--mute); font-size: 13px; }}
.meta b {{ color: var(--fg); font-family: var(--mono); font-weight: 500; }}
.lead {{ color: var(--mute); font-size: 14px; }}
.summary {{ background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 20px 22px; display: grid; gap: 10px; }}
.summary ul {{ margin: 0; padding-left: 1.2em; display: grid; gap: 6px; max-width: 74ch; }}
.warn {{ color: rgb(var(--up)); font-weight: 700; }}
.steps {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }}
.steps div {{ border-top: 2px solid var(--fg); padding-top: 8px; display: grid; gap: 4px; min-width: 0; }}
.steps b {{ font-family: var(--mono); font-weight: 500; color: var(--accent); font-size: 12px; }}
.steps p {{ font-size: 14px; color: var(--mute); }}
.fams {{ display: grid; gap: 32px; }}
.fam {{ display: grid; gap: 10px; min-width: 0; }}
.famhead {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
.dot {{ width: 10px; height: 10px; border-radius: 50%; }}
.chip {{ font-size: 12px; color: var(--mute); border: 1px solid var(--line); border-radius: 999px; padding: 0 10px; font-weight: 400; }}
.spec {{ display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); gap: 16px 28px; }}
@media (max-width: 640px) {{ .spec {{ grid-template-columns: 1fr; }} }}
.spec ul {{ margin: 0; padding-left: 1.2em; font-size: 14px; }}
.exit {{ font-family: var(--mono); font-size: 14px; }}
.mix {{ font-size: 12px; color: var(--mute); }}
.tw {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); }}
table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
th, td {{ padding: 9px 12px; text-align: right; border-bottom: 1px solid var(--line); white-space: nowrap; vertical-align: top; }}
thead th {{ font-size: 12px; color: var(--mute); font-weight: 500; letter-spacing: .04em; background: var(--bg); }}
tbody th {{ text-align: left; font-weight: 500; }}
th small {{ display: block; font-size: 11px; color: var(--mute); font-weight: 400; font-family: var(--mono); }}
tbody tr:last-child > * {{ border-bottom: 0; }}
td {{ font-family: var(--mono); font-size: 14px; }}
td b {{ font-weight: 500; }}
td.pos {{ background: rgba(var(--up), var(--a)); }}
td.neg {{ background: rgba(var(--dn), var(--a)); }}
.txt {{ text-align: left; white-space: normal; font-family: var(--body); font-size: 13px; min-width: 260px; }}
tr.oos th {{ color: var(--accent); }}
tr.sub > * {{ color: var(--mute); }}
tr.old > * {{ background: var(--bg); }}
.tag {{ display: inline-block; font-size: 11px; border: 1px solid var(--line); border-radius: 3px; padding: 0 5px; margin-right: 8px; color: var(--mute); }}
.tag.new {{ border-color: var(--accent); color: var(--accent); }}
.na {{ color: var(--mute); }}
.chart {{ overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 8px; }}
.chart svg {{ display: block; width: 100%; min-width: 560px; height: auto; }}
.chart .gl {{ stroke: var(--line); stroke-width: 1; }}
.chart .zero {{ stroke: var(--mute); stroke-width: 1; }}
.chart .ax {{ fill: var(--mute); font: 11px var(--mono); }}
.chart .ln {{ stroke-width: 2; stroke-linejoin: round; }}
.legend {{ display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 13px; }}
.legend i {{ display: inline-block; width: 12px; height: 3px; vertical-align: middle; margin-right: 6px; }}
.legend b {{ font-family: var(--mono); font-weight: 500; }}
.robust {{ display: grid; gap: 12px; }}
.notes {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px 28px; }}
.notes div {{ display: grid; gap: 4px; min-width: 0; }}
.notes p {{ font-size: 14px; color: var(--mute); }}
</style>
<div class="wrap">
<header>
  <div class="eyebrow">Backtest · 条件の見直し</div>
  <h1>スイング条件 改良版</h1>
  <div class="meta">
    <span>探索期間 <b>{m["is_start"]} 〜 {m["is_end"]}</b></span>
    <span>検証期間 <b>{m["oos_start"]} 〜 {m["oos_end"]}</b></span>
    <span>銘柄 <b>{m["tickers"]:,}</b></span>
    <span>試した条件 <b>{total}通り</b></span>
  </div>
  {demo}
</header>

<section class="summary">
  <h2>結論</h2>
  {summary}
</section>

<section>
  <h2>進め方</h2>
  <div class="steps">
    <div><b>1 · 条件</b><p>4系統で計{total}通りの条件を、探索期間に10営業日保有で比べました。取引{m["min_trades"]}回以上・PF {m["min_pf"]}以上を満たすものだけが対象です。</p></div>
    <div><b>2 · 手仕舞い</b><p>系統ごとの上位3条件について、損切り {stops}、利確 {takes}、最大保有 {days} の組み合わせを比べました。</p></div>
    <div><b>3 · 選び方</b><p>{m["slots"]}枠で運用したときの「年率リターン ÷ 最大ドローダウン」が探索期間で最も高いものを、系統ごとに1つ選びました。</p></div>
    <div><b>4 · 検証</b><p>直近1年は探索に一切使っていません。選んだ条件をそのまま当てた成績が、実際に期待できる成績に近い数字です。</p></div>
  </div>
  <p class="lead">期間中の市場全体 (売買代金1億円以上の等金額平均) は、探索期間 {num(m["mkt_is"], 1)}%、検証期間 {num(m["mkt_oos"], 1)}% でした。</p>
</section>

<section>
  <h2>選ばれた条件</h2>
  <p class="lead">期待値は1回あたりのコスト控除後の平均損益。最大DDと年率は、資金を{m["slots"]}枠に分けて (1枠=資産の1/{m["slots"]}、複利) 運用した場合の試算です。</p>
  <div class="fams">{cards}</div>
</section>

<section>
  <h2>検証期間の損益推移 ({m["slots"]}枠運用)</h2>
  {curve_svg(res)}
</section>

<section>
  <h2>元の条件との比較 (検証期間)</h2>
  <p class="lead">元の6条件は、ご指定の取引ルール (損切り -1.5ATR / 利確 +2.5ATR / 最大10日) で同じ期間を計算したものです。</p>
  {compare_table(res)}
</section>

<section class="robust">
  <h2>探索期間の上位は検証期間でも通用したか</h2>
  <p class="lead">探索期間で上位10位までに入った条件 (10日保有) を、そのまま検証期間に当てた結果です。上位の多くが検証期間でもプラスなら、選んだ条件は偶然ではなく系統として効いている可能性が高くなります。</p>
  {robust_table(res)}
</section>

<section>
  <h2>使わなかった指標と注意点</h2>
  <div class="notes">
    <div><h3>信用倍率</h3><p>銘柄別の信用残の過去データは、JPXのサイトに直近数週分しか無く、数年分をさかのぼって無料で集められません。過去の各時点の値を使えないため、検証から外しました。現在の値は保有銘柄ページで確認できます。</p></div>
    <div><h3>収益予測 (会社予想・コンセンサス)</h3><p>過去の各時点で公表されていた予想値を無料で集める手段がありません。現在の予想値をさかのぼって当てると将来を知っている状態になり、成績が実際より良く見えるため使っていません。</p></div>
    <div><h3>MACD・相場環境</h3><p>日足から計算できるので、その時点で分かる値だけで検証しています。相場環境は、売買代金1億円以上の全銘柄の等金額平均を独自の指数として使いました。</p></div>
    <div><h3>生存者バイアス</h3><p>上場廃止銘柄は無料データで取れないため含めていません。下げた銘柄を拾う乖離リバウンドなどは、実際より良く出ている可能性があります。</p></div>
    <div><h3>売買コスト・税</h3><p>手数料0円、スプレッドとスリッページは片道 {tiers}。数値は税引き前で、利益には年間の損益通算後に{m["tax"]}%の税がかかります。為替コストはありません。</p></div>
    <div><h3>決算またぎ</h3><p>過去の決算日を一括で取れないため、決算回避は反映していません。</p></div>
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
