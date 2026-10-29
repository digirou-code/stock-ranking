"""backtest/results/rule_test.json から HTML レポートを作る。

使い方:
    python backtest/build_rule_report.py RULE_TEST_JSON OUT_HTML [--summary SUMMARY_HTML]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import build_opt_report as ob

e, num, tone = ob.e, ob.num, ob.tone
SRC = Path(__file__).with_name("build_opt_report.py").read_text(encoding="utf-8")
CSS = re.search(r"<style>(.*?)</style>", SRC, re.S).group(1).replace("{{", "{").replace("}}", "}")
LINE_COLORS = ["--c1", "--c3", "--c5", "--c6", "--mute"]

HEAD = ('<thead><tr><th></th><th>取引回数</th><th>勝率</th><th>平均利益</th><th>平均損失</th><th>損益比</th>'
        '<th>期待値</th><th>PF</th><th>最大DD</th><th>年率</th><th>期間損益</th></tr></thead>')


def cells(r):
    if not r or not r.get("n"):
        return '<td colspan="10" class="na">取引なし</td>'
    return ob.metric_cells(r) + f'<td{tone(r.get("pf_total"), 40)}>{num(r.get("pf_total"), 1)}%</td>'


def bench_row(label, b):
    if not b:
        return ""
    return (f'<tr class="old"><th scope="row"><span class="tag">比較</span>{e(label)}</th>'
            f'<td colspan="7" class="na">買って持ち続けた場合</td><td>{num(b["max_dd"], 1)}%</td>'
            f'<td>{num(b["annual"], 1)}%</td><td>{num(b["total"], 1)}%</td></tr>')


def main_table(res, pid):
    rows = []
    for name, C in res["composite"].items():
        rows.append(f'<tr><th scope="row"><span class="tag new">複合</span>{e(C["name"])}</th>{cells(C[pid])}</tr>')
    for sid, R in res["strategies"].items():
        tag = "改良" if R["group"] == "改良版" else "元"
        rows.append(f'<tr><th scope="row"><span class="tag{" new" if tag == "改良" else ""}">{tag}</span>{e(R["name"])}</th>'
                    f'{cells(R[pid])}</tr>')
    b = res["bench"][pid]
    rows.append(bench_row("日経平均 (配当なし)", b.get("nikkei")))
    rows.append(bench_row("全銘柄の等金額平均", b.get("market")))
    return f'<div class="tw"><table>{HEAD}<tbody>{"".join(rows)}</tbody></table></div>'


def regime_table(res):
    up_pick = {c.get("up") for c in res["composite"].values()}
    down_pick = {c.get("down") for c in res["composite"].values()}
    rows = []
    for sid, R in res["strategies"].items():
        cells_ = []
        for reg, picks in (("up", up_pick), ("down", down_pick)):
            r = R["regime_is"][reg]
            mark = ' <span class="chip">採用</span>' if sid in picks else ""
            if not r.get("n"):
                cells_.append('<td class="na" colspan="3">取引なし</td>')
                continue
            cells_.append(f'<td{tone(r["net"], 3)}><b>{num(r["net"])}%</b>{mark}</td>'
                          f'<td>{num(r.get("pf"), 2, False)}</td><td>{r["n"]:,}</td>')
        rows.append(f'<tr><th scope="row">{e(R["name"])}</th>{"".join(cells_)}</tr>')
    return ('<div class="tw"><table><thead><tr><th></th><th>上げ相場 期待値</th><th>PF</th><th>回数</th>'
            '<th>下げ相場 期待値</th><th>PF</th><th>回数</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def exit_table(res, pid):
    rows = []
    items = [(C["name"], C[pid]) for C in res["composite"].values()] + \
            [(R["name"], R[pid]) for R in res["strategies"].values()]
    for name, r in items:
        if not r.get("n"):
            continue
        x = r["exit"]
        rows.append(f'<tr><th scope="row">{e(name)}</th><td>{x["stop"]:.0f}%</td><td>{x["half"]:.0f}%</td>'
                    f'<td>{x["full"]:.0f}%</td><td>{x["time"]:.0f}%</td><td>{r["hold_days"]:.1f}日</td></tr>')
    return ('<div class="tw"><table><thead><tr><th></th><th>損切り -5%</th><th>+25%で半分利確</th>'
            '<th>+50%まで全部利確</th><th>30日で時間切れ</th><th>平均保有</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def curve(res, pid, ids):
    series = []
    for kind, key in ids:
        src = res["composite"] if kind == "c" else res["strategies"]
        if key in src and src[key][pid].get("curve"):
            series.append((src[key]["name"], src[key][pid]["curve"]))
    nk = (res["bench"][pid] or {}).get("nikkei")
    if nk:
        series.append(("日経平均", nk["curve"]))
    dates = sorted({d for _, cv in series for d, _ in cv})
    if not dates:
        return ""
    vals = [v for _, cv in series for _, v in cv]
    lo, hi = min(0, min(vals)), max(0, max(vals))
    step = next(s for s in (1, 2, 5, 10, 20, 25, 50, 100, 200) if (hi - lo or 1) / s <= 8)
    lo_t, hi_t = (lo // step) * step, -((-hi) // step) * step
    W, H, L, R, T, B = 760, 300, 52, 16, 14, 30
    x = lambda k: L + (W - L - R) * k / max(1, len(dates) - 1)
    y = lambda v: T + (H - T - B) * (hi_t - v) / ((hi_t - lo_t) or 1)
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="損益推移">']
    v = lo_t
    while v <= hi_t + 1e-9:
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="{"zero" if v == 0 else "gl"}"/>'
                   f'<text x="{L - 6}" y="{y(v) + 4:.1f}" class="ax" text-anchor="end">{v:+.0f}%</text>')
        v += step
    long_span = len(dates) > 80
    seen = set()
    for k, d in enumerate(dates):
        lab = d[:4] if long_span else d[:7]
        if lab in seen or (not long_span and int(d[5:7]) % 2):
            continue
        seen.add(lab)
        out.append(f'<text x="{x(k):.1f}" y="{H - 10}" class="ax" text-anchor="middle">{lab.replace("-", "/")}</text>')
    pos = {d: k for k, d in enumerate(dates)}
    for n, (name, cv) in enumerate(series):
        col = "--mute" if name == "日経平均" else LINE_COLORS[n % 4]
        dash = ' stroke-dasharray="5 4"' if name == "日経平均" else ""
        pts = " ".join(f"{x(pos[d]):.1f},{y(v):.1f}" for d, v in cv)
        out.append(f'<polyline points="{pts}" fill="none" style="stroke:var({col})" class="ln"{dash}/>')
        d, v = cv[-1]
        out.append(f'<circle cx="{x(pos[d]):.1f}" cy="{y(v):.1f}" r="3.5" style="fill:var({col})"/>')
    out.append("</svg>")
    legend = "".join(
        f'<span><i style="background:var({"--mute" if name == "日経平均" else LINE_COLORS[n % 4]})"></i>{e(name)} '
        f'<b>{num(cv[-1][1], 1)}%</b></span>' for n, (name, cv) in enumerate(series))
    return f'<div class="chart">{"".join(out)}</div><div class="legend">{legend}</div>'


def cond_list(res):
    rows = []
    for sid, R in res["strategies"].items():
        if R.get("cond"):
            rows.append(f'<tr><th scope="row">{e(R["name"])}</th><td class="txt">{e("、".join(R["cond"]))}</td></tr>')
    return f'<div class="tw"><table><tbody>{"".join(rows)}</tbody></table></div>'


def build(res, summary):
    m = res["meta"]
    P = m["periods"]
    r = m["rule"]
    tiers = " / ".join(f"{t[0]}億円以上 {t[1]:.2f}%" if t[0] else f"1億円未満 {t[1]:.2f}%" for t in m["cost_tiers"])
    demo = '<p class="warn">これは乱数のデモデータです。実際の結果ではありません。</p>' if m["demo"] else ""
    top = [("c", k) for k in res["composite"]]
    best_single = max(res["strategies"], key=lambda s: res["strategies"][s]["oos"].get("pf_total") or -1e9)
    ids = top + [("s", "pullback_v2")] + ([("s", best_single)] if best_single != "pullback_v2" else [])
    return f"""<title>スイング新ルール検証</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Zen+Kaku+Gothic+New:wght@400;500;700&family=Shippori+Mincho+B1:wght@600;800&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{CSS}
.rules {{ display: flex; flex-wrap: wrap; gap: 8px; }}
.rules span {{ font-family: var(--mono); font-size: 13px; border: 1px solid var(--line); border-radius: 4px; padding: 2px 10px; background: var(--panel); }}
</style>
<div class="wrap">
<header>
  <div class="eyebrow">Backtest · 新しい売買ルール</div>
  <h1>スイング新ルール検証</h1>
  <div class="rules">
    <span>損切り {r["stop_pct"]:+.0f}%</span><span>+{r["take1_pct"]:.0f}%で半分利確</span>
    <span>+{r["take2_pct"]:.0f}%で全部利確</span><span>最大{r["max_days"]}営業日</span>
    <span>{m["slots"]}枠・総合SCOREの高い順</span>
  </div>
  <div class="meta">
    <span>探索期間 <b>{P["is"][0]} 〜 {P["is"][1]}</b></span>
    <span>検証期間 <b>{P["oos"][0]} 〜 {P["oos"][1]}</b></span>
    <span>銘柄 <b>{m["tickers"]:,}</b></span>
  </div>
  {demo}
</header>

<section class="summary">
  <h2>結論</h2>
  {summary}
</section>

<section>
  <h2>直近1年の成績 (検証期間)</h2>
  <p class="lead">期待値は1回あたりのコスト控除後の平均損益 (半分ずつ売った分を合わせた損益)。最大DD・年率・期間損益は{m["slots"]}枠で運用した場合 (1枠=資産の1/{m["slots"]}、複利、含み損益込み)。複合パターンの組み合わせは探索期間の成績だけで決め、この期間は答え合わせに使っています。</p>
  {main_table(res, "oos")}
  {curve(res, "oos", ids)}
</section>

<section>
  <h2>上げ相場と下げ相場での成績 (探索期間)</h2>
  <p class="lead">シグナルが出た日に、市場全体 (売買代金1億円以上の等金額平均) が200日移動平均より上なら上げ相場、下なら下げ相場としました。探索期間の上げ相場は全体の{m["up_days_pct"]["is"]:.0f}%、検証期間は{m["up_days_pct"]["oos"]:.0f}%です。複合パターンは、それぞれの相場で取引{100}回以上・PF 1超えのうち期待値が最も高い戦略を組み合わせています。</p>
  {regime_table(res)}
</section>

<section>
  <h2>約4年通しの成績</h2>
  <p class="lead">{P["all"][0]} 〜 {P["all"][1]}。探索期間を含むので、複合パターンは実際より良く出ます。個別の戦略は今回の売買ルールで計算し直しただけなので、比較の目安になります。</p>
  {main_table(res, "all")}
  {curve(res, "all", ids)}
</section>

<section>
  <h2>手仕舞いの内訳 (直近1年)</h2>
  {exit_table(res, "oos")}
</section>

<section>
  <h2>改良版の条件</h2>
  {cond_list(res)}
  <p class="lead">元の6条件は最初のレポートと同じ判定です (出来高倍率1.5倍、高値突破10日)。</p>
</section>

<section>
  <h2>前提と注意点</h2>
  <div class="notes">
    <div><h3>総合SCOREの再現</h3><p>3ヶ月騰落率・52週レンジ位置・売買代金と、PER・PBR・配当利回りの株価部分はその日の値で計算しました。1株利益・純資産・配当・ROE・利益率・成長率・負債比率は過去の値が無料で取れないため現在の値で代用しています。優先順位にだけ使い、買うかどうかの判定には使っていません。</p></div>
    <div><h3>約定</h3><p>エントリーは翌営業日の寄り付き。損切り・利確の価格に場中で届いたらその価格で約定、寄り付きで越えていたら始値で約定。同じ日に損切りと利確の両方に届いた場合は損切りが先とみなしています。</p></div>
    <div><h3>売買コスト・税</h3><p>手数料0円、スプレッドとスリッページは片道 {tiers}。半分ずつ売るときも同じ率で計算。数値は税引き前で、利益には年間の損益通算後に{m["tax"]}%の税がかかります。日経平均は配当を含まない指数そのものです。</p></div>
    <div><h3>生存者バイアス</h3><p>上場廃止銘柄は無料データで取れないため含めていません。下げた銘柄を買う戦略ほど、実際より良く出ている可能性があります。</p></div>
    <div><h3>決算またぎ</h3><p>最大30日保有なので決算をまたぐ取引が増えますが、過去の決算日を一括で取れないため反映していません。</p></div>
    <div><h3>信用倍率・収益予測</h3><p>過去の各時点の値を無料で集められないため、今回も使っていません。</p></div>
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
