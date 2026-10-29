"""backtest/results/results.json から HTML レポートを作る。

使い方:
    python backtest/build_report.py RESULTS_JSON OUT_HTML [--summary SUMMARY_HTML]
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

HOLD_LABEL = {"1": "1日", "3": "3日", "5": "5日", "10": "10日", "rule": "ルール"}
STRAT_OF = {"pullback": "①", "rsi_pullback": "①", "rebound_dev": "②", "rebound_rsi": "②",
            "vol_break": "③", "high_break": "③"}
COND_DEF = {
    "pullback": "75日線上向き・株価>75日線・25日線>75日線、25日線乖離 -3〜+1%、20日高値から -5〜-15%、売買代金1億円以上",
    "rsi_pullback": "株価>200日線、RSI(14) 35〜45、前日比プラスの陽線、売買代金1億円以上",
    "rebound_dev": "25日線乖離 -10%以下、時価総額300億円以上、売買代金1億円以上",
    "rebound_rsi": "直近5日にRSI(14)≦30、陽線かつ前日比 +1%以上、出来高が20日平均の1.2倍以上",
    "vol_break": "出来高が20日平均の2倍以上、前日比 +3%以上、高値引け近く(-1%以内)、株価>25日線、当日売買代金3億円以上",
    "high_break": "終値が直近60日高値を更新、出来高1.5倍以上、25日線>75日線、売買代金1億円以上",
}
FUND_DEF = {
    "pullback": "ROE 8%以上",
    "rsi_pullback": "営業利益率 5%以上",
    "rebound_dev": "売上・利益成長率 0%超、ROE 8%以上、PER 0〜20倍",
    "rebound_rsi": "利益成長率 0%超、ROE 8%以上",
    "vol_break": "なし",
    "high_break": "なし",
}
COLORS = ["--c1", "--c2", "--c3", "--c4", "--c5", "--c6"]


def e(s):
    return html.escape(str(s))


def num(v, d=2, sign=True, unit=""):
    if v is None:
        return '<span class="na">—</span>'
    s = f"{v:+.{d}f}" if sign else f"{v:.{d}f}"
    return s + unit


def tone(v, scale=1.0):
    """平均リターンをセルの色の強さに"""
    if v is None:
        return ""
    a = max(-1.0, min(1.0, v / scale))
    cls = "pos" if a > 0 else "neg"
    return f' class="{cls}" style="--a:{abs(a) * 0.55:.2f}"'


def hold_cells(h, key, scale):
    out = []
    for N in ["1", "3", "5", "10", "rule"]:
        s = h.get(N, {})
        if not s or s.get("n", 0) == 0:
            out.append('<td class="na">—</td>')
            continue
        sig = ""
        t = s.get("t")
        if t is not None and abs(t) >= 2:
            sig = '<sup title="シグナル日ごとの平均で t値の絶対値が2以上">*</sup>'
        out.append(f'<td{tone(s.get(key), scale)}><b>{num(s.get(key))}</b>{sig}'
                   f'<small>勝率 {num(s.get("win"), 0, False)}% · {s["n"]:,}件</small></td>')
    return "".join(out)


def main_table(V, items, key="net", scale=1.5, label_fn=None):
    rows = []
    for cid, c in items:
        rows.append(f'<tr><th scope="row">{label_fn(cid, c)}</th>{hold_cells(c["hold"], key, scale)}</tr>')
    head = "".join(f"<th>{HOLD_LABEL[N]}</th>" for N in ["1", "3", "5", "10", "rule"])
    return (f'<div class="tw"><table class="grid"><thead><tr><th></th>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def cond_label(cid, c):
    return f'<span class="tag">{STRAT_OF[cid]}</span>{e(c["name"])}'


def strat_label(sid, s):
    return e(s["name"])


def excess_table(V):
    rows = []
    for cid, c in V["conds"].items():
        cells = []
        for N in ["1", "3", "5", "10"]:
            s = c["hold"][N]
            if not s.get("n"):
                cells.append('<td class="na">—</td>')
                continue
            cells.append(f'<td{tone(s.get("excess"), 1.5)}><b>{num(s.get("excess"))}</b>'
                         f'<small>市場超え {num(s.get("excess_win"), 0, False)}%</small></td>')
        rows.append(f'<tr><th scope="row">{cond_label(cid, c)}</th>{"".join(cells)}</tr>')
    head = "".join(f"<th>{HOLD_LABEL[N]}</th>" for N in ["1", "3", "5", "10"])
    return (f'<div class="tw"><table class="grid"><thead><tr><th></th>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def rule_table(V):
    rows = []
    items = list(V["conds"].items()) + [(k, v) for k, v in V["strategies"].items()]
    for cid, c in items:
        s = c["hold"]["rule"]
        p = c.get("portfolio") or {}
        lab = cond_label(cid, c) if cid in STRAT_OF else f'<b>{e(c["name"])}</b>'
        if not s.get("n"):
            rows.append(f'<tr><th scope="row">{lab}</th><td colspan="8" class="na">シグナルなし</td></tr>')
            continue
        ex = s.get("exit", {})
        bar = (f'<span class="mix" title="利確 {ex.get("take", 0):.0f}% / 損切り {ex.get("stop", 0):.0f}% / '
               f'時間切れ {ex.get("time", 0):.0f}%">'
               f'<i class="tk" style="width:{ex.get("take", 0):.1f}%"></i>'
               f'<i class="st" style="width:{ex.get("stop", 0):.1f}%"></i>'
               f'<i class="tm" style="width:{ex.get("time", 0):.1f}%"></i></span>'
               f'<small>{ex.get("take", 0):.0f} / {ex.get("stop", 0):.0f} / {ex.get("time", 0):.0f}</small>')
        cls = ' class="sum"' if cid not in STRAT_OF else ""
        rows.append(
            f'<tr{cls}><th scope="row">{lab}</th><td>{s["n"]:,}</td><td>{num(s["win"], 1, False)}%</td>'
            f'<td{tone(s["net"], 1.5)}>{num(s["net"])}%</td><td>{num(s.get("pf"), 2, False)}</td>'
            f'<td>{num(s.get("hold_days"), 1, False)}日</td><td class="mixc">{bar}</td>'
            f'<td{tone(p.get("annual"), 15)}>{num(p.get("annual"), 1)}%</td>'
            f'<td>{num(p.get("max_dd"), 1)}%</td></tr>')
    return ('<div class="tw"><table class="plain"><thead><tr><th></th><th>件数</th><th>勝率</th>'
            '<th>平均(コスト後)</th><th>PF</th><th>平均保有</th><th>利確/損切り/時間切れ %</th>'
            '<th>10枠運用 年率</th><th>最大DD</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def year_table(V, N):
    years = sorted({y for c in V["conds"].values() for y in c["hold"][N].get("by_year", {})})
    head = "".join(f"<th>{y}</th>" for y in years)
    rows = []
    for cid, c in V["conds"].items():
        by = c["hold"][N].get("by_year", {})
        cells = []
        for y in years:
            s = by.get(y)
            if not s:
                cells.append('<td class="na">—</td>')
            else:
                cells.append(f'<td{tone(s["net"], 1.5)}><b>{num(s["net"])}</b><small>{s["n"]:,}件</small></td>')
        rows.append(f'<tr><th scope="row">{cond_label(cid, c)}</th>{"".join(cells)}</tr>')
    return (f'<div class="tw"><table class="grid"><thead><tr><th></th>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def curve_svg(V):
    """10枠運用の累積損益 (確定ベース, 資金比 %)"""
    series = [(cid, c["name"], (c.get("portfolio") or {}).get("curve") or []) for cid, c in V["conds"].items()]
    months = sorted({m for _, _, cv in series for m, _ in cv})
    if not months:
        return ""
    vals = [v for _, _, cv in series for _, v in cv]
    lo, hi = min(0, min(vals)), max(0, max(vals))
    span = hi - lo or 1
    step = 10 ** max(0, len(str(int(span))) - 1)
    step = step if span / step >= 3 else step / 2
    lo_t = (lo // step) * step
    hi_t = -((-hi) // step) * step
    W, H, L, R, T, B = 760, 320, 52, 16, 14, 34
    x = lambda k: L + (W - L - R) * k / max(1, len(months) - 1)
    y = lambda v: T + (H - T - B) * (hi_t - v) / (hi_t - lo_t)
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="10枠運用の累積損益">']
    v = lo_t
    while v <= hi_t + 1e-9:
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="{"zero" if v == 0 else "gl"}"/>'
                   f'<text x="{L - 6}" y="{y(v) + 4:.1f}" class="ax" text-anchor="end">{v:+.0f}%</text>')
        v += step
    for k, m in enumerate(months):
        if m.endswith("-01") or k == 0:
            out.append(f'<text x="{x(k):.1f}" y="{H - 12}" class="ax" text-anchor="middle">{m[:4]}</text>')
    pos = {m: k for k, m in enumerate(months)}
    for n, (cid, name, cv) in enumerate(series):
        if not cv:
            continue
        pts = " ".join(f"{x(pos[m]):.1f},{y(v):.1f}" for m, v in cv)
        out.append(f'<polyline points="{pts}" fill="none" style="stroke:var({COLORS[n]})" class="ln"/>')
        m, v = cv[-1]
        out.append(f'<circle cx="{x(pos[m]):.1f}" cy="{y(v):.1f}" r="3" style="fill:var({COLORS[n]})"/>')
    out.append("</svg>")
    legend = "".join(f'<span><i style="background:var({COLORS[n]})"></i>{e(name)} '
                     f'<b>{num(cv[-1][1], 0) if cv else "—"}%</b></span>'
                     for n, (_, name, cv) in enumerate(series))
    return f'<div class="chart">{"".join(out)}</div><div class="legend">{legend}</div>'


def build(res, summary_html):
    m = res["meta"]
    A, B = res["variants"]["price"], res["variants"]["fund"]
    tiers = " / ".join(f"{t[0]}億円以上 {t[1]:.2f}%" if t[0] else f"1億円未満 {t[1]:.2f}%"
                       for t in m["cost_tiers"])
    defs = "".join(f'<tr><th scope="row">{cond_label(cid, A["conds"][cid])}</th><td>{e(COND_DEF[cid])}</td>'
                   f'<td>{e(FUND_DEF[cid])}</td></tr>' for cid in A["conds"])
    bench = " / ".join(f'{HOLD_LABEL[k]} {num(v)}%' for k, v in m["bench_mean"].items())
    demo = '<p class="warn">これは乱数のデモデータです。実際の結果ではありません。</p>' if m["demo"] else ""
    return f"""<title>スイング6条件バックテスト</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Zen+Kaku+Gothic+New:wght@400;500;700&family=Shippori+Mincho+B1:wght@600;800&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* 板読みノート: 紙の帳面に罫線、数値は等幅。上昇=朱、下落=藍 (国内の板・チャートの慣習) */
:root {{
  --bg: #f6f6f2; --panel: #ffffff; --fg: #1d2129; --mute: #5f6672; --line: #d9dbd4;
  --up: 196, 55, 40; --dn: 34, 84, 160; --accent: #8a5a12;
  --c1: #c43728; --c2: #d98a1f; --c3: #2a6db5; --c4: #5aa0d8; --c5: #3f8a4a; --c6: #7a5bb0;
  --display: "Shippori Mincho B1", "Hiragino Mincho ProN", "Yu Mincho", serif;
  --body: "Zen Kaku Gothic New", "Hiragino Sans", "Yu Gothic", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #15181d; --panel: #1c2027; --fg: #e6e7e3; --mute: #9aa1ab; --line: #2f3540;
  --up: 238, 105, 88; --dn: 104, 158, 230; --accent: #e0b064;
  --c1: #ee6958; --c2: #f0aa48; --c3: #6aa2ea; --c4: #9cc8ee; --c5: #74c083; --c6: #b49ae6;
  color-scheme: dark; }} }}
:root[data-theme="dark"] {{
  --bg: #15181d; --panel: #1c2027; --fg: #e6e7e3; --mute: #9aa1ab; --line: #2f3540;
  --up: 238, 105, 88; --dn: 104, 158, 230; --accent: #e0b064;
  --c1: #ee6958; --c2: #f0aa48; --c3: #6aa2ea; --c4: #9cc8ee; --c5: #74c083; --c6: #b49ae6;
  color-scheme: dark; }}
body {{ background: var(--bg); color: var(--fg); font-family: var(--body); font-size: 15px; line-height: 1.75; }}
.wrap {{ max-width: 1040px; margin: 0 auto; padding-block: 36px 64px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  display: grid; gap: 44px; }}
header {{ display: grid; gap: 10px; border-bottom: 2px solid var(--fg); padding-bottom: 18px; }}
.eyebrow {{ font-family: var(--mono); font-size: 12px; letter-spacing: .08em; color: var(--accent); text-transform: uppercase; }}
h1 {{ font-family: var(--display); font-weight: 800; font-size: clamp(28px, 5vw, 40px); line-height: 1.25; margin: 0; text-wrap: balance; }}
h2 {{ font-family: var(--display); font-weight: 800; font-size: 22px; margin: 0; text-wrap: balance; }}
h3 {{ font-size: 15px; margin: 0; }}
section {{ display: grid; gap: 14px; min-width: 0; }}
p {{ margin: 0; max-width: 68ch; }}
.meta {{ display: flex; flex-wrap: wrap; gap: 6px 22px; color: var(--mute); font-size: 13px; }}
.meta b {{ color: var(--fg); font-family: var(--mono); font-weight: 500; }}
.lead {{ color: var(--mute); font-size: 14px; }}
.summary {{ background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 20px 22px; display: grid; gap: 10px; }}
.summary ul {{ margin: 0; padding-left: 1.2em; display: grid; gap: 6px; max-width: 72ch; }}
.warn {{ color: rgb(var(--up)); font-weight: 700; }}
.tw {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); }}
table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
th, td {{ padding: 9px 12px; text-align: right; border-bottom: 1px solid var(--line); white-space: nowrap; vertical-align: top; }}
thead th {{ font-size: 12px; color: var(--mute); font-weight: 500; letter-spacing: .04em; background: var(--bg); }}
tbody th {{ text-align: left; font-weight: 500; }}
tbody tr:last-child > * {{ border-bottom: 0; }}
td {{ font-family: var(--mono); font-size: 14px; }}
td b {{ font-weight: 500; display: block; }}
td small {{ display: block; font-size: 11px; color: var(--mute); font-family: var(--body); }}
td.pos {{ background: rgba(var(--up), var(--a)); }}
td.neg {{ background: rgba(var(--dn), var(--a)); }}
td sup {{ color: var(--accent); font-weight: 700; }}
.na {{ color: var(--mute); }}
.tag {{ display: inline-block; min-width: 1.6em; margin-right: 6px; color: var(--accent); font-weight: 700; }}
tr.sum th, tr.sum td {{ background: var(--bg); }}
.mixc {{ min-width: 150px; }}
.mix {{ display: flex; height: 8px; border-radius: 2px; overflow: hidden; margin-top: 6px; background: var(--line); }}
.mix i {{ display: block; height: 100%; }}
.mix .tk {{ background: rgb(var(--up)); }} .mix .st {{ background: rgb(var(--dn)); }} .mix .tm {{ background: var(--mute); }}
.defs td {{ text-align: left; white-space: normal; font-family: var(--body); font-size: 13px; min-width: 220px; }}
.defs tbody th {{ white-space: nowrap; }}
.chart {{ overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 8px; }}
.chart svg {{ display: block; width: 100%; min-width: 560px; height: auto; }}
.chart .gl {{ stroke: var(--line); stroke-width: 1; }}
.chart .zero {{ stroke: var(--mute); stroke-width: 1; }}
.chart .ax {{ fill: var(--mute); font: 11px var(--mono); }}
.chart .ln {{ stroke-width: 2; stroke-linejoin: round; }}
.legend {{ display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 13px; }}
.legend i {{ display: inline-block; width: 12px; height: 3px; vertical-align: middle; margin-right: 6px; }}
.legend b {{ font-family: var(--mono); font-weight: 500; }}
.notes {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px 28px; }}
.notes div {{ display: grid; gap: 4px; min-width: 0; }}
.notes p {{ font-size: 14px; color: var(--mute); }}
.key {{ font-size: 13px; color: var(--mute); }}
</style>
<div class="wrap">
<header>
  <div class="eyebrow">Backtest · 東証 日足</div>
  <h1>スイング6条件バックテスト</h1>
  <div class="meta">
    <span>検証期間 <b>{m["test_start"]} 〜 {m["test_end"]}</b></span>
    <span>営業日 <b>{m["trading_days"]:,}</b></span>
    <span>銘柄 <b>{m["tickers"]:,}</b></span>
    <span>作成 <b>{m["generated_at"][:16].replace("T", " ")}</b></span>
  </div>
  {demo}
</header>

<section class="summary">
  <h2>結論</h2>
  {summary_html}
</section>

<section>
  <h2>条件ごとの成績 (コスト控除後の平均リターン)</h2>
  <p class="lead">シグナル翌営業日の寄り付きで買い、N営業日目の大引けで売った場合の1トレード平均 (%)。「ルール」は損切り -{m["rule"]["stop_atr"]}ATR・利確 +{m["rule"]["take_atr"]}ATR・{m["rule"]["max_days"]}営業日で手仕舞い。財務条件は外し、その時点で分かる価格と出来高だけで判定しています。</p>
  {main_table(A, A["conds"].items(), label_fn=cond_label)}
  <p class="key">朱は利益、藍は損失で、色が濃いほど大きい。<sup>*</sup> はシグナル日ごとに平均したリターンの t値が ±2 を超えたもの (偶然では出にくい差)。</p>
</section>

<section>
  <h2>戦略ごとの成績</h2>
  <p class="lead">戦略① 押し目 = 25日線押し目 + RSI押し目、② リバウンド = 乖離リバウンド + RSI売られすぎ反発、③ 初動 = 出来高急増 + 高値ブレイク。同じ日に同じ銘柄が両方に出たら1件と数えています。</p>
  {main_table(A, A["strategies"].items(), label_fn=strat_label)}
</section>

<section>
  <h2>市場平均との差</h2>
  <p class="lead">同じ日に同じ方法で売買代金1億円以上の全銘柄を買った場合の平均との差 (コスト前, %)。相場全体の上げ下げを取り除いた、条件そのものの効き目です。期間中の市場平均は {bench}。</p>
  {excess_table(A)}
</section>

<section>
  <h2>取引ルールで運用した場合</h2>
  <p class="lead">損切り -{m["rule"]["stop_atr"]}ATR / 利確 +{m["rule"]["take_atr"]}ATR / {m["rule"]["max_days"]}営業日。同じ日に損切りと利確の両方に届いた日は損切りを先とみなしています。「10枠運用」は資金を{m["slots"]}等分し、空き枠があればその日の並び順上位から買った試算 (単利・確定損益ベース)。</p>
  {rule_table(A)}
  <h3>10枠運用の累積損益 (資金比 %)</h3>
  {curve_svg(A)}
</section>

<section>
  <h2>年ごとの成績 (5営業日保有)</h2>
  {year_table(A, "5")}
  <h2>年ごとの成績 (取引ルール)</h2>
  {year_table(A, "rule")}
</section>

<section>
  <h2>参考: 財務条件も入れた場合</h2>
  <p class="lead">ROEや利益成長率などは過去時点の値が無料で取れないため、現在の値をさかのぼって当てています。将来の決算を知っている状態なので、成績は実際より良く出やすい点に注意してください。出来高急増と高値ブレイクは財務条件が無いので上と同じです。</p>
  {main_table(B, B["conds"].items(), label_fn=cond_label)}
  {rule_table(B)}
</section>

<section>
  <h2>判定条件</h2>
  <div class="tw"><table class="plain defs"><thead><tr><th></th><th>価格・出来高 (検証に使用)</th><th>財務 (参考のみ)</th></tr></thead><tbody>{defs}</tbody></table></div>
</section>

<section>
  <h2>前提と注意点</h2>
  <div class="notes">
    <div><h3>データ</h3><p>Yahoo Finance の日足 (分割・配当調整済みの始値・高値・安値・終値と出来高)。{m["data_start"]} から取得し、200日線が計算できる日から検証。</p></div>
    <div><h3>対象銘柄と生存者バイアス</h3><p>現在のプライム・スタンダード・グロース上場銘柄のみ。上場廃止銘柄の株価は無料データで取れないため含めていません。倒産や業績悪化で消えた銘柄が抜けるので、特にリバウンド系の成績は実際より良く出やすいです。</p></div>
    <div><h3>売買コスト</h3><p>手数料 {m["commission"]:.0f}円 (SBI証券などの国内株手数料無料コース)。スプレッドとスリッページは直近20日の平均売買代金で片道 {tiers} と見積もり、往復分を差し引いています。</p></div>
    <div><h3>税・為替</h3><p>表の数値は税引き前。利益には年間の損益通算後に {m["tax"]}% の税がかかるので、手取りはプラスの年の利益の約8割です。円建ての国内株のみのため為替コストはありません。</p></div>
    <div><h3>約定できない日</h3><p>エントリー日の高値と安値が同じ (ストップ高・安張り付きの可能性) 場合は買えなかったとして除外。寄り付きの気配次第で実際には買えないケースが他にもあり得ます。</p></div>
    <div><h3>重複の扱い</h3><p>同じ銘柄を保有中に出た次のシグナルは数えていません。同じ日に大量のシグナルが出る日 (急落の翌日など) の偏りを見るため、日ごとに平均した t値も出しています。</p></div>
    <div><h3>決算またぎ</h3><p>過去の決算発表日を一括で取れないため、決算回避のルールは反映していません。実際の運用では決算をまたぐトレードを避けるので、損益のばらつきはこれより小さくなる見込みです。</p></div>
    <div><h3>過去の成績</h3><p>検証期間の相場環境に依存します。将来も同じ成績になる保証はありません。</p></div>
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
