"""backtest/results/results.json から HTML レポートを作る。

使い方:
    python backtest/build_report.py RESULTS_JSON OUT_HTML [--summary SUMMARY_HTML]
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

HOLDS = ["1", "3", "5", "10", "rule"]
HOLD_LABEL = {"1": "1日", "3": "3日", "5": "5日", "10": "10日", "rule": "ATRルール"}
COLORS = ["--c1", "--c2", "--c3", "--c4", "--c5", "--c6"]


def cond_defs(p):
    v, b = p["vol_ratio"], p["break_days"]
    return {
        "pullback": ("75日線上向き・株価>75日線・25日線>75日線、25日線乖離 -3〜+1%、20日高値から -5〜-15%、売買代金1億円以上",
                     "ROE 8%以上"),
        "rsi_pullback": ("株価>200日線、RSI(14) 35〜45、前日比プラスの陽線、売買代金1億円以上", "営業利益率 5%以上"),
        "rebound_dev": ("25日線乖離 -10%以下、時価総額300億円以上、売買代金1億円以上",
                        "売上・利益成長率 0%超、ROE 8%以上、PER 0〜20倍"),
        "rebound_rsi": (f"直近5日にRSI(14)≦30、陽線かつ前日比 +1%以上、出来高が20日平均の{v}倍以上",
                        "利益成長率 0%超、ROE 8%以上"),
        "vol_break": (f"出来高が20日平均の{v}倍以上、前日比 +3%以上、高値引け近く(-1%以内)、株価>25日線、当日売買代金3億円以上",
                      "なし"),
        "high_break": (f"終値が直近{b}日の最高値を更新、出来高{v}倍以上、25日線>75日線、売買代金1億円以上", "なし"),
    }


def e(s):
    return html.escape(str(s))


def num(v, d=2, sign=True, unit=""):
    if v is None:
        return '<span class="na">—</span>'
    s = f"{v:+.{d}f}" if sign else f"{v:.{d}f}"
    return s + unit


def tone(v, scale=1.0):
    if v is None:
        return ""
    a = max(-1.0, min(1.0, v / scale))
    return f' class="{"pos" if a > 0 else "neg"}" style="--a:{abs(a) * 0.55:.2f}"'


def pf_tone(v):
    if v is None:
        return ""
    return tone(v - 1, 0.5)


def overview(V, key="net"):
    """期待値ヒートマップ"""
    head = "".join(f"<th>{HOLD_LABEL[N]}</th>" for N in HOLDS)
    rows = []
    for cid, c in V.items():
        cells = []
        for N in HOLDS:
            s = c["hold"][N]
            if not s.get("n"):
                cells.append('<td class="na">—</td>')
                continue
            sig = '<sup title="シグナル日ごとの平均で t値の絶対値が2以上">*</sup>' if abs(s.get("t") or 0) >= 2 else ""
            cells.append(f'<td{tone(s.get(key), 1.5)}><b>{num(s.get(key))}%{sig}</b>'
                         f'<small>PF {num(s.get("pf"), 2, False)} · {s["n"]:,}件</small></td>')
        rows.append(f'<tr><th scope="row">{e(c["name"])}</th>{"".join(cells)}</tr>')
    return (f'<div class="tw"><table class="grid"><thead><tr><th></th>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def detail(c, defs, cid):
    rows = []
    for N in HOLDS:
        s = c["hold"][N]
        p = s.get("portfolio") or {}
        if not s.get("n"):
            rows.append(f'<tr><th scope="row">{HOLD_LABEL[N]}</th><td colspan="8" class="na">取引なし</td></tr>')
            continue
        extra = ""
        if N == "rule":
            ex = s.get("exit", {})
            extra = (f'<small>利確 {ex.get("take", 0):.0f}% · 損切り {ex.get("stop", 0):.0f}% · '
                     f'時間切れ {ex.get("time", 0):.0f}% · 平均{s.get("hold_days", 0):.1f}日</small>')
        rows.append(
            f'<tr><th scope="row">{HOLD_LABEL[N]}{extra}</th>'
            f'<td>{s["n"]:,}</td>'
            f'<td>{num(s["win"], 1, False)}%</td>'
            f'<td>{num(s.get("avg_win"))}%</td>'
            f'<td>{num(s.get("avg_loss"))}%</td>'
            f'<td>{num(s.get("payoff"), 2, False)}</td>'
            f'<td{tone(s["net"], 1.5)}><b>{num(s["net"])}%</b></td>'
            f'<td{pf_tone(s.get("pf"))}>{num(s.get("pf"), 2, False)}</td>'
            f'<td>{num(p.get("max_dd"), 1)}%<small>損益 {num(p.get("total"), 1)}%</small></td></tr>')
    tech, fund = defs[cid]
    return f"""<article class="cond">
  <h3>{e(c["name"])}</h3>
  <p class="def">{e(tech)}</p>
  <div class="tw"><table class="plain"><thead><tr><th>保有</th><th>取引回数</th><th>勝率</th><th>平均利益</th><th>平均損失</th>
  <th>損益比</th><th>期待値</th><th>PF</th><th>最大DD</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>
</article>"""


def curve_svg(V, N="rule"):
    series = [(c["name"], ((c["hold"][N].get("portfolio") or {}).get("curve") or [])) for c in V.values()]
    dates = sorted({d for _, cv in series for d, _ in cv})
    if not dates:
        return ""
    vals = [v for _, cv in series for _, v in cv]
    lo, hi = min(0, min(vals)), max(0, max(vals))
    span = hi - lo or 1
    step = next(s for s in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000) if span / s <= 8)
    lo_t, hi_t = (lo // step) * step, -((-hi) // step) * step
    W, H, L, R, T, B = 760, 320, 52, 16, 14, 30
    x = lambda k: L + (W - L - R) * k / max(1, len(dates) - 1)
    y = lambda v: T + (H - T - B) * (hi_t - v) / ((hi_t - lo_t) or 1)
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="10枠運用の損益推移">']
    v = lo_t
    while v <= hi_t + 1e-9:
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="{"zero" if v == 0 else "gl"}"/>'
                   f'<text x="{L - 6}" y="{y(v) + 4:.1f}" class="ax" text-anchor="end">{v:+.0f}%</text>')
        v += step
    seen = set()
    long_span = dates[-1][:4] != dates[0][:4] and len(dates) > 80
    for k, d in enumerate(dates):
        lab = d[:4] if long_span else d[:7]
        if lab in seen or (not long_span and int(d[5:7]) % 2):
            continue
        seen.add(lab)
        out.append(f'<text x="{x(k):.1f}" y="{H - 10}" class="ax" text-anchor="middle">{lab.replace("-", "/")}</text>')
    pos = {d: k for k, d in enumerate(dates)}
    for n, (name, cv) in enumerate(series):
        if not cv:
            continue
        pts = " ".join(f"{x(pos[d]):.1f},{y(v):.1f}" for d, v in cv)
        out.append(f'<polyline points="{pts}" fill="none" style="stroke:var({COLORS[n]})" class="ln"/>')
        d, v = cv[-1]
        out.append(f'<circle cx="{x(pos[d]):.1f}" cy="{y(v):.1f}" r="3.5" style="fill:var({COLORS[n]})"/>')
    out.append("</svg>")
    legend = "".join(f'<span><i style="background:var({COLORS[n]})"></i>{e(name)} '
                     f'<b>{num(cv[-1][1], 1) if cv else "—"}%</b></span>' for n, (name, cv) in enumerate(series))
    return f'<div class="chart">{"".join(out)}</div><div class="legend">{legend}</div>'


def excess_table(V, bench):
    head = "".join(f"<th>{HOLD_LABEL[N]}</th>" for N in HOLDS[:4])
    rows = []
    for c in V.values():
        cells = []
        for N in HOLDS[:4]:
            s = c["hold"][N]
            if not s.get("n"):
                cells.append('<td class="na">—</td>')
                continue
            cells.append(f'<td{tone(s.get("excess"), 1.5)}><b>{num(s.get("excess"))}%</b>'
                         f'<small>市場超え {num(s.get("excess_win"), 0, False)}%</small></td>')
        rows.append(f'<tr><th scope="row">{e(c["name"])}</th>{"".join(cells)}</tr>')
    brow = "".join(f'<td>{num(bench.get(N))}%</td>' for N in HOLDS[:4])
    rows.append(f'<tr class="sum"><th scope="row">市場平均 (コスト前)</th>{brow}</tr>')
    return (f'<div class="tw"><table class="grid"><thead><tr><th></th>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def build(res, summary_html):
    m = res["meta"]
    P1, PA = res["periods"]["1y"], res["periods"]["all"]
    A = P1["variants"]["price"]
    defs = cond_defs(m["params"])
    r = m["rule"]
    tiers = " / ".join(f"{t[0]}億円以上 {t[1]:.2f}%" if t[0] else f"1億円未満 {t[1]:.2f}%" for t in m["cost_tiers"])
    def_rows = "".join(f'<tr><th scope="row">{e(A[cid]["name"])}</th><td>{e(t)}</td><td>{e(f)}</td></tr>'
                       for cid, (t, f) in defs.items())
    demo = '<p class="warn">これは乱数のデモデータです。実際の結果ではありません。</p>' if m["demo"] else ""
    details = "".join(detail(c, defs, cid) for cid, c in A.items())
    return f"""<title>スイング6条件バックテスト</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Zen+Kaku+Gothic+New:wght@400;500;700&family=Shippori+Mincho+B1:wght@600;800&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* 売買帳: 紙の帳面に細い罫、数値は等幅。利益=朱、損失=藍 (国内の値動き表示の慣習) */
:root {{
  --bg: #f5f5f1; --panel: #ffffff; --fg: #1d2129; --mute: #5d6470; --line: #dadbd3;
  --up: 190, 52, 38; --dn: 36, 86, 160; --accent: #87580f;
  --c1: #c43728; --c2: #d38a1c; --c3: #2a6db5; --c4: #4f9bd6; --c5: #3d8748; --c6: #7a5bb0;
  --display: "Shippori Mincho B1", "Hiragino Mincho ProN", "Yu Mincho", serif;
  --body: "Zen Kaku Gothic New", "Hiragino Sans", "Yu Gothic", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #15181d; --panel: #1c2027; --fg: #e6e7e3; --mute: #9aa1ab; --line: #303641;
  --up: 238, 105, 88; --dn: 104, 158, 230; --accent: #e0b064;
  --c1: #ee6958; --c2: #f0aa48; --c3: #6aa2ea; --c4: #9cc8ee; --c5: #74c083; --c6: #b49ae6;
  color-scheme: dark; }} }}
:root[data-theme="dark"] {{
  --bg: #15181d; --panel: #1c2027; --fg: #e6e7e3; --mute: #9aa1ab; --line: #303641;
  --up: 238, 105, 88; --dn: 104, 158, 230; --accent: #e0b064;
  --c1: #ee6958; --c2: #f0aa48; --c3: #6aa2ea; --c4: #9cc8ee; --c5: #74c083; --c6: #b49ae6;
  color-scheme: dark; }}
body {{ background: var(--bg); color: var(--fg); font-family: var(--body); font-size: 15px; line-height: 1.75; }}
.wrap {{ max-width: 1040px; margin: 0 auto; padding-block: 36px 64px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  display: grid; gap: 48px; }}
header {{ display: grid; gap: 10px; border-bottom: 2px solid var(--fg); padding-bottom: 18px; }}
.eyebrow {{ font-family: var(--mono); font-size: 12px; letter-spacing: .08em; color: var(--accent); text-transform: uppercase; }}
h1 {{ font-family: var(--display); font-weight: 800; font-size: clamp(28px, 5vw, 40px); line-height: 1.25; margin: 0; text-wrap: balance; }}
h2 {{ font-family: var(--display); font-weight: 800; font-size: 22px; margin: 0; text-wrap: balance; }}
h3 {{ font-size: 16px; margin: 0; }}
section {{ display: grid; gap: 14px; min-width: 0; }}
p {{ margin: 0; max-width: 70ch; }}
.meta {{ display: flex; flex-wrap: wrap; gap: 6px 22px; color: var(--mute); font-size: 13px; }}
.meta b {{ color: var(--fg); font-family: var(--mono); font-weight: 500; }}
.lead {{ color: var(--mute); font-size: 14px; }}
.summary {{ background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 20px 22px; display: grid; gap: 10px; }}
.summary ul {{ margin: 0; padding-left: 1.2em; display: grid; gap: 6px; max-width: 74ch; }}
.warn {{ color: rgb(var(--up)); font-weight: 700; }}
.tw {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); }}
table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
th, td {{ padding: 9px 12px; text-align: right; border-bottom: 1px solid var(--line); white-space: nowrap; vertical-align: top; }}
thead th {{ font-size: 12px; color: var(--mute); font-weight: 500; letter-spacing: .04em; background: var(--bg); }}
tbody th {{ text-align: left; font-weight: 500; }}
tbody th small {{ display: block; font-size: 11px; color: var(--mute); font-weight: 400; }}
tbody tr:last-child > * {{ border-bottom: 0; }}
td {{ font-family: var(--mono); font-size: 14px; }}
td b {{ font-weight: 500; display: block; }}
td small {{ display: block; font-size: 11px; color: var(--mute); font-family: var(--body); }}
td.pos {{ background: rgba(var(--up), var(--a)); }}
td.neg {{ background: rgba(var(--dn), var(--a)); }}
sup {{ color: var(--accent); font-weight: 700; }}
.na {{ color: var(--mute); }}
tr.sum > * {{ background: var(--bg); color: var(--mute); }}
.conds {{ display: grid; gap: 28px; }}
.cond {{ display: grid; gap: 8px; min-width: 0; }}
.def {{ font-size: 13px; color: var(--mute); }}
.defs td {{ text-align: left; white-space: normal; font-family: var(--body); font-size: 13px; min-width: 200px; }}
.chart {{ overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 8px; }}
.chart svg {{ display: block; width: 100%; min-width: 560px; height: auto; }}
.chart .gl {{ stroke: var(--line); stroke-width: 1; }}
.chart .zero {{ stroke: var(--mute); stroke-width: 1; }}
.chart .ax {{ fill: var(--mute); font: 11px var(--mono); }}
.chart .ln {{ stroke-width: 2; stroke-linejoin: round; }}
.legend {{ display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 13px; }}
.legend i {{ display: inline-block; width: 12px; height: 3px; vertical-align: middle; margin-right: 6px; }}
.legend b {{ font-family: var(--mono); font-weight: 500; }}
.glossary {{ display: flex; flex-wrap: wrap; gap: 4px 20px; font-size: 13px; color: var(--mute); }}
.glossary b {{ color: var(--fg); font-weight: 500; }}
.notes {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px 28px; }}
.notes div {{ display: grid; gap: 4px; min-width: 0; }}
.notes p {{ font-size: 14px; color: var(--mute); }}
.key {{ font-size: 13px; color: var(--mute); }}
</style>
<div class="wrap">
<header>
  <div class="eyebrow">Backtest · 東証 日足</div>
  <h1>スイング6条件バックテスト</h1>
  <div class="meta">
    <span>検証期間 <b>{P1["start"]} 〜 {m["test_end"]}</b></span>
    <span>営業日 <b>{P1["trading_days"]:,}</b></span>
    <span>銘柄 <b>{m["tickers"]:,}</b></span>
    <span>損切り <b>-{r["stop_atr"]}ATR</b></span>
    <span>利確 <b>+{r["take_atr"]}ATR</b></span>
    <span>最大保有 <b>{r["max_days"]}日</b></span>
  </div>
  {demo}
</header>

<section class="summary">
  <h2>結論</h2>
  {summary_html}
</section>

<section>
  <h2>期待値の一覧 (コスト控除後, 1回あたり)</h2>
  <p class="lead">シグナル翌営業日の寄り付きで買い、N営業日目の大引けで売った場合の1回あたり平均損益 (%)。「ATRルール」は損切り -{r["stop_atr"]}ATR・利確 +{r["take_atr"]}ATR・最大{r["max_days"]}営業日で手仕舞い。判定はその時点で分かる株価と出来高だけで行い、財務条件は外しています (理由は下の注意点)。</p>
  {overview(A)}
  <p class="key">朱は利益、藍は損失で、色が濃いほど大きい。<sup>*</sup> はシグナル日ごとに平均した損益の t値が ±2 を超えたもの (偶然では出にくい差)。</p>
</section>

<section>
  <h2>条件ごとの評価指標</h2>
  <div class="glossary">
    <span><b>勝率</b> 利益で終わった取引の割合</span>
    <span><b>損益比</b> 平均利益 ÷ 平均損失</span>
    <span><b>期待値</b> 1回あたり平均損益 (コスト後)</span>
    <span><b>PF</b> 総利益 ÷ 総損失</span>
    <span><b>最大DD</b> 資金を{m["slots"]}枠に分けて運用した場合の、資産のピークからの最大下落率 (下段は期間の損益)</span>
  </div>
  <div class="conds">{details}</div>
</section>

<section>
  <h2>ATRルールで{m["slots"]}枠運用した場合の損益推移</h2>
  <p class="lead">資金を{m["slots"]}等分し、空き枠があればその日のシグナルを各条件の並び順 (スクリーニング画面の順位) の上位から買った試算。1枠の投資額は前日時点の資産の1/{m["slots"]} (複利)、含み損益込み、コスト控除後。</p>
  {curve_svg(A)}
</section>

<section>
  <h2>市場平均との差</h2>
  <p class="lead">同じ日に同じ方法で売買代金1億円以上の全銘柄を買った場合の平均との差 (コスト前, %)。相場全体の上げ下げを除いた、条件そのものの効き目です。</p>
  {excess_table(A, P1["bench_mean"])}
</section>

<section>
  <h2>参考: 約4年に延ばした場合</h2>
  <p class="lead">{PA["start"]} 〜 {m["test_end"]} ({PA["trading_days"]:,}営業日)。直近1年の結果がその年の相場に特有のものかを確かめるためのものです。</p>
  {overview(PA["variants"]["price"])}
</section>

<section>
  <h2>参考: 財務条件も入れた場合 (直近1年)</h2>
  <p class="lead">ROEや利益成長率は過去時点の値が無料で取れないため、現在の値をさかのぼって当てています。将来の決算を知っている状態になるので、成績は実際より良く出やすい点に注意してください。出来高急増と高値ブレイクは財務条件が無いので上と同じです。</p>
  {overview(P1["variants"]["fund"])}
</section>

<section>
  <h2>判定条件</h2>
  <div class="tw"><table class="plain defs"><thead><tr><th></th><th>株価・出来高 (検証に使用)</th><th>財務 (参考のみ)</th></tr></thead><tbody>{def_rows}</tbody></table></div>
</section>

<section>
  <h2>前提と注意点</h2>
  <div class="notes">
    <div><h3>データ</h3><p>Yahoo Finance の日足 (分割・配当調整済みの始値・高値・安値・終値と出来高)。{m["data_start"]} から取得し、200日線などが計算できる日から判定。</p></div>
    <div><h3>対象銘柄と生存者バイアス</h3><p>現在のプライム・スタンダード・グロース上場の{m["tickers"]:,}銘柄。上場廃止銘柄の株価は無料データで取れないため含められませんでした。倒産や業績悪化で消えた銘柄が抜けるので、特にリバウンド系の成績は実際より良く出やすいです。</p></div>
    <div><h3>売買コスト</h3><p>手数料 {m["commission"]:.0f}円 (SBI証券などの国内株手数料無料コース)。スプレッドとスリッページは直近20日の平均売買代金で片道 {tiers} と見積もり、往復分を差し引いています。</p></div>
    <div><h3>税・為替</h3><p>表の数値は税引き前。利益には年間の損益通算後に {m["tax"]}% の税がかかるので、手取りはプラスの年の利益の約8割になります。円建ての国内株のみのため為替コストはありません。</p></div>
    <div><h3>ATRルールの約定</h3><p>ATRはシグナル日の14日ATR。損切りと利確の両方に同じ日に届いた場合は損切りが先とみなす保守的な扱い。寄り付きで損切り・利確ラインを越えて始まった日は始値で約定。</p></div>
    <div><h3>約定できない日</h3><p>エントリー日の高値と安値が同じ (ストップ高・安張り付きの可能性) 場合は買えなかったとして除外。</p></div>
    <div><h3>重複の扱い</h3><p>同じ銘柄を保有中に出た次のシグナルは数えていません。急落の翌日などにシグナルが集中する偏りを見るため、日ごとに平均した t値も出しています。</p></div>
    <div><h3>決算またぎ</h3><p>過去の決算発表日を一括で取れないため、決算回避は反映していません。実運用で決算をまたがなければ、損益のばらつきはこれより小さくなる見込みです。</p></div>
    <div><h3>過去の成績</h3><p>検証期間の相場環境に依存します。1年は期間として短く、将来も同じ成績になる保証はありません。</p></div>
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
