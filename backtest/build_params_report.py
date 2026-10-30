"""backtest/results/params.json から HTML レポートを作る。

使い方:
    python backtest/build_params_report.py PARAMS_JSON OUT_HTML [--summary SUMMARY_HTML]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import build_opt_report as ob
import build_rule_report as rr

e, num, tone = ob.e, ob.num, ob.tone


def pct(v, d=1):
    return f"{num(v, d)}%" if v is not None else "—"


LABS = (("current", "現行"), ("pick", "推奨"), ("best", "探索1位"))


def same(a, b):
    return all(a[k] == b[k] for k in ("stop_pct", "take1_pct", "take2_pct", "max_days"))


def grid_table(res):
    G, cur = res["grid"], res["current_index"]
    head = ("<thead><tr><th>ルール</th><th>探索 順位</th><th>探索 年率</th><th>探索 最大DD</th><th>探索 PF</th>"
            "<th>検証 損益</th><th>検証 最大DD</th><th>検証 PF</th></tr></thead>")
    order = {n: k + 1 for k, n in enumerate(res["is_order"])}
    show = list(res["show"])
    pick_n = next(n for n, g in enumerate(G) if same(g["rule"], res["pick"]))
    if pick_n not in show:
        show.append(pick_n)
    rows = []
    for n in sorted(show, key=lambda n: order[n]):
        g = G[n]
        a, b = g["is"]["avg"], g["oos"]["avg"]
        tag = ('<span class="tag">現行</span>' if n == cur else '<span class="tag new">推奨</span>' if n == pick_n
               else '<span class="tag">探索1位</span>' if order[n] == 1 else "")
        cls = ' class="old"' if n == cur else ""
        rows.append(f'<tr{cls}><th scope="row">{tag}{e(g["label"])}</th><td>{order[n]}</td>'
                    f'<td{tone(a["pf_annual"], 30)}>{pct(a["pf_annual"])}</td><td>{pct(a["pf_dd"])}</td><td>{num(a["pf"], 2, False)}</td>'
                    f'<td{tone(b["pf_total"], 30)}>{pct(b["pf_total"])}</td><td>{pct(b["pf_dd"])}</td><td>{num(b["pf"], 2, False)}</td></tr>')
    return f'<div class="tw"><table>{head}<tbody>{"".join(rows)}</tbody></table></div>'


def matrix(res):
    """損切り × 保有日数 (利確13通りの平均)"""
    G, m = res["grid"], res["meta"]
    head = "<thead><tr><th>損切り</th>" + "".join(f"<th>{d}日</th>" for d in m["days"]) + "</tr></thead>"
    rows = []
    for st in m["stops"]:
        cells = []
        for d in m["days"]:
            v = [g for g in G if g["rule"]["stop_pct"] == st and g["rule"]["max_days"] == d]
            a = sum(g["is"]["avg"]["pf_annual"] for g in v) / len(v)
            b = sum(g["oos"]["avg"]["pf_total"] for g in v) / len(v)
            cells.append(f'<td{tone(a, 15)}>{pct(a)}<small>検証 {pct(b)}</small></td>')
        rows.append(f'<tr><th scope="row">{st:.0f}%</th>{"".join(cells)}</tr>')
    return f'<div class="tw"><table>{head}<tbody>{"".join(rows)}</tbody></table></div>'


def take_table(res):
    """推奨の損切り・保有日数で利確だけ変える"""
    p = res["pick"]
    head = "<thead><tr><th>利確</th><th>探索 年率</th><th>探索 最大DD</th><th>検証 損益</th><th>検証 最大DD</th></tr></thead>"
    rows = []
    for g in res["grid"]:
        r = g["rule"]
        if r["stop_pct"] != p["stop_pct"] or r["max_days"] != p["max_days"]:
            continue
        a, b = g["is"]["avg"], g["oos"]["avg"]
        name = g["label"].split(" / ")[1]
        tag = '<span class="tag new">推奨</span>' if same(r, p) else ""
        rows.append(f'<tr><th scope="row">{tag}{e(name)}</th><td{tone(a["pf_annual"], 15)}>{pct(a["pf_annual"])}</td>'
                    f'<td>{pct(a["pf_dd"])}</td><td{tone(b["pf_total"], 30)}>{pct(b["pf_total"])}</td><td>{pct(b["pf_dd"])}</td></tr>')
    return f'<div class="tw"><table>{head}<tbody>{"".join(rows)}</tbody></table></div>'


def compare_table(res, pid):
    head = "<thead><tr><th></th>" + "".join(f"<th>{n} 損益</th><th>最大DD</th><th>PF</th>" for _, n in LABS) + "</tr></thead>"
    rows = []
    for tid, C in res["compare"].items():
        mark = '<span class="tag new">○</span>' if C["clean"] else '<span class="tag">×</span>'
        cells = "".join(f'<td{tone(C[lab][pid].get("pf_total"), 40)}>{pct(C[lab][pid].get("pf_total"))}</td>'
                        f'<td>{pct(C[lab][pid].get("pf_dd"))}</td><td>{num(C[lab][pid].get("pf"), 2, False)}</td>'
                        for lab, _ in LABS)
        rows.append(f'<tr><th scope="row">{mark}{e(C["name"])}</th>{cells}</tr>')
    bch = res["bench"].get(pid)
    if bch:
        rows.append(f'<tr class="old"><th scope="row"><span class="tag">比較</span>日経平均</th>'
                    f'<td>{pct(bch["total"])}</td><td>{pct(bch["max_dd"])}</td><td colspan="7" class="na"></td></tr>')
    return f'<div class="tw"><table>{head}<tbody>{"".join(rows)}</tbody></table></div>'


def exit_table(res):
    head = ("<thead><tr><th></th><th>平均保有日数</th><th>損切り</th><th>半分利確まで</th><th>全部利確</th><th>時間切れ</th></tr></thead>")
    rows = []
    for lab, name in LABS:
        for tid in ("sm", "random"):
            r = res["compare"][tid][lab]["all"]
            x = r.get("exit") or {}
            rows.append(f'<tr><th scope="row">{name}<small>{e(res["compare"][tid]["name"])}</small></th>'
                        f'<td>{num(r.get("hold_days"), 1, False)}日</td>'
                        + "".join(f'<td>{num(x.get(k), 0, False)}%</td>' for k in ("stop", "half", "full", "time")) + "</tr>")
    return f'<div class="tw"><table>{head}<tbody>{"".join(rows)}</tbody></table></div>'


def slots_table(res):
    labs = [(lab, n) for lab, n in LABS if lab in res["slots"]][:2]
    head = "<thead><tr><th>同時保有</th>" + "".join(
        f"<th>{n} 探索 年率</th><th>最大DD</th><th>検証 損益</th><th>最大DD</th>" for _, n in labs) + "</tr></thead>"
    rows = []
    for k in res["slots"]["current"]:
        cells = ""
        for lab, _ in labs:
            v = res["slots"][lab][k]
            cells += (f'<td{tone(v["is"]["pf_annual"], 15)}>{pct(v["is"]["pf_annual"])}</td><td>{pct(v["is"]["pf_dd"])}</td>'
                      f'<td{tone(v["oos"]["pf_total"], 30)}>{pct(v["oos"]["pf_total"])}</td><td>{pct(v["oos"]["pf_dd"])}</td>')
        rows.append(f'<tr><th scope="row">{k}銘柄</th>{cells}</tr>')
    return f'<div class="tw"><table>{head}<tbody>{"".join(rows)}</tbody></table></div>'


def curve(res):
    fake = {"composite": {}, "strategies": {}, "bench": {"oos": res["bench"].get("oos") and {"nikkei": res["bench"]["oos"]}}}
    ids = []
    for tid in ("sm", "random"):
        for lab, name in (("pick", "推奨"), ("current", "現行")):
            k = f"{tid}_{lab}"
            fake["strategies"][k] = {"name": f'{res["compare"][tid]["name"]} {name}',
                                     "oos": {"curve": res["compare"][tid][lab]["oos"].get("curve") or []}}
            ids.append(("s", k))
    return rr.curve(fake, "oos", ids)


def build(res, summary):
    m = res["meta"]
    P, b, c = m["periods"], res["best"], res["current"]
    demo = '<p class="warn">これは乱数のデモデータです。実際の結果ではありません。</p>' if m["demo"] else ""
    tname = "・".join(res["targets"].values())
    t = lambda v: "なし" if v >= 1000 else f"+{v:.0f}%"
    takes = " / ".join(f"{t(a)}" if a == bb else f"{t(a)}→{t(bb)}" for a, bb in m["takes"])
    return f"""<title>取引ルールの見直し</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Zen+Kaku+Gothic+New:wght@400;500;700&family=Shippori+Mincho+B1:wght@600;800&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{rr.CSS}
td small, th small {{ display: block; font-size: 11px; color: var(--mute); }}
.tw table {{ font-size: 13px; }}
.rules {{ display: grid; grid-template-columns: max-content 1fr; gap: 6px 14px; font-size: 14px; }}
.rules b {{ font-family: var(--mono); font-weight: 500; }}
.grid2 {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }}
</style>
<div class="wrap">
<header>
  <div class="eyebrow">Backtest · 取引ルール</div>
  <h1>取引ルールの見直し</h1>
  <div class="rules">
    <span>現行</span><b>{e(res["current_label"])} / {c["slots"]}銘柄</b>
    <span>推奨</span><b>{e(res["pick_label"])} / {res["pick"]["slots"]}銘柄</b>
    <span>探索1位</span><b>{e(res["best_label"])} / {b["slots"]}銘柄</b>
  </div>
  <div class="meta">
    <span>探索 <b>{P["is"][0]} 〜 {P["is"][1]}</b></span>
    <span>検証 <b>{P["oos"][0]} 〜 {P["oos"][1]}</b></span>
    <span>組み合わせ <b>{m["combos"]}</b></span>
    <span>銘柄 <b>{m["tickers"]:,}</b></span>
  </div>
  {demo}
</header>

<section class="summary">
  <h2>結論</h2>
  {summary}
</section>

<section>
  <h2>選び方</h2>
  <p class="lead">損切り {" / ".join(f"{s:.0f}%" for s in m["stops"])}、利確 {takes}、保有 {" / ".join(f"{d}日" for d in m["days"])} を総当たりしました (同時保有は10銘柄で固定)。対象は先読みのない {e(tname)} の6つです。探索期間の年率を組み合わせごとに順位にし、6つの平均順位が一番良いものを「探索1位」としました。1つの条件にだけ合う値を避けるためです。「推奨」は、探索期間で一貫して良かった「40日保有・損切りを広めに」を、1回の損失が大きくなりすぎない -8% で取り入れた実用案です (探索期間 {e(str(res["is_order"].index(next(n for n, g in enumerate(res["grid"]) if same(g["rule"], res["pick"]))) + 1))}位)。</p>
  <p class="lead">下の表は探索の上位{len(res["show"]) - 1}件と現行・推奨です。損益と最大DDは6つの平均で、最大DDは資金を10枠に分けて運用した場合の値です。</p>
  {grid_table(res)}
</section>

<section>
  <h2>ルール別の成績 (直近1年)</h2>
  <p class="lead">○は株価だけで判定できる条件、×は財務を現在値で使うタブ (先読みあり、参考)。</p>
  {compare_table(res, "oos")}
  {curve(res)}
</section>

<section>
  <h2>ルール別の成績 (通し {P["all"][0]} 〜 {P["all"][1]})</h2>
  {compare_table(res, "all")}
</section>

<section>
  <h2>損切りと保有日数</h2>
  <p class="lead">損切り幅と保有日数ごとに、利確13通りの平均をとった探索期間の年率 (小さい数字は検証期間の損益)。表の右下ほど良く、40日・損切り広めの傾向はどの損切り幅でも同じでした。</p>
  {matrix(res)}
  <h2>利確の水準</h2>
  <p class="lead">損切り {res["pick"]["stop_pct"]:.0f}%・{res["pick"]["max_days"]}日で、利確だけを変えた場合。差は損切り・保有日数より小さめです。</p>
  {take_table(res)}
</section>

<section>
  <h2>同時保有数</h2>
  <p class="lead">現行ルールと推奨ルールで、同時に持つ銘柄数だけを変えた場合 (6つの平均)。</p>
  {slots_table(res)}
</section>

<section>
  <h2>手仕舞いの内訳 (通し)</h2>
  {exit_table(res)}
</section>

<section>
  <h2>前提と注意点</h2>
  <div class="notes">
    <div><h3>共通の条件</h3><p>シグナル翌営業日の寄り付きで買い、決算 (推定日) が保有予定期間に入る銘柄は買いません。利確・損切りの価格に場中で届いたらその価格、寄り付きで越えていたら始値で約定し、同じ日に両方に届いた場合は損切りを先とみなします。</p></div>
    <div><h3>売買コスト</h3><p>手数料0円、スプレッドとスリッページは売買代金に応じて片道0.10〜0.40%。税引き前です。</p></div>
    <div><h3>ランダム</h3><p>売買代金1億円以上の銘柄から無作為に買う形を{m["random_seeds"]}回試した平均です。どの条件にも使えるルールかを見るため、選ぶ対象に入れています。</p></div>
    <div><h3>データ</h3><p>Yahoo Finance の日足 (調整済み)。現在上場している銘柄のみで、上場廃止銘柄は含みません。財務は現在値のため、×のタブは実際より良く出やすいです。</p></div>
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
