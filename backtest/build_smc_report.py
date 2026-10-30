"""backtest/results/smc.json から HTML レポートを作る。

使い方:
    python backtest/build_smc_report.py SMC_JSON OUT_HTML [--summary SUMMARY_HTML]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import build_opt_report as ob
import build_rule_report as rr

e, num, tone = ob.e, ob.num, ob.tone

DEFS = [
    ("スイング高値・安値", "前後 k 本より高い (安い) 足。右側の k 本が出そろった日に確定とみなす (k は2・3・5本から探索期間で選択)"),
    ("BOS", "直近2つのスイング高値・安値が切り上がっている上昇構造のまま、終値が直近のスイング高値をはじめて上抜けた日"),
    ("CHoCH", "高値・安値が切り下がっている下降構造だったところで、終値が直近のスイング高値をはじめて上抜けた日"),
    ("流動性スイープ", "安値が直近のスイング安値を割り込んだのに、終値はその上に戻した陽線"),
    ("スイープ → CHoCH", "スイープから10日以内に CHoCH が出た日"),
    ("FVG", "3本の足で1本目の高値 < 3本目の安値 になった空白。BOS から20日以内にできたものに、その後はじめて安値が届き、空白の下端より上で終わった陽線"),
    ("オーダーブロック", "BOS の直前にあった最後の陰線の値幅。BOS から20日以内に、はじめて安値が届き、陰線の安値より上で終わった陽線"),
    ("ディスカウント", "BOS から30日以内に、BOS 前のスイング安値〜その後の高値 の下半分まで押し、スイング安値は割らずに陽線"),
]


def pct(v, d=1):
    return f"{num(v, d)}%" if v is not None else "—"


def cells(r):
    return (f'<td>{int(r["is"]["n"]):,}</td><td{tone(r["is"]["net"], 2)}>{pct(r["is"]["net"], 2)}</td><td>{num(r["is"]["pf"], 2, False)}</td>'
            f'<td{tone(r["is"]["cash"]["annual"], 30)}>{pct(r["is"]["cash"]["annual"])}</td>'
            f'<td>{int(r["oos"]["n"]):,}</td><td{tone(r["oos"]["net"], 2)}>{pct(r["oos"]["net"], 2)}</td><td>{num(r["oos"]["pf"], 2, False)}</td>'
            f'<td{tone(r["oos"]["cash"]["total"], 30)}>{pct(r["oos"]["cash"]["total"])}</td><td>{pct(r["oos"]["cash"]["max_dd"])}</td>'
            f'<td{tone(r["oos"]["nikkei"]["total"], 30)}>{pct(r["oos"]["nikkei"]["total"])}</td>')


def main_table(res):
    head = ("<thead><tr><th></th><th>探索 取引</th><th>期待値</th><th>PF</th><th>年率</th>"
            "<th>検証 取引</th><th>期待値</th><th>PF</th><th>損益</th><th>最大DD</th><th>空き資金を日経で</th></tr></thead>")
    S = res["signals"]
    order = sorted(S, key=lambda s: -S[s]["best"]["is"]["cash"]["annual"])
    rows = [f'<tr><th scope="row">{e(res["names"][s])}<small>スイング k={S[s]["best"]["k"]}</small></th>{cells(S[s]["best"])}</tr>'
            for s in order]
    rows.append(f'<tr class="old"><th scope="row"><span class="tag">比較</span>SCORE勢い 上位10</th>{cells(res["sm"])}</tr>')
    rows.append(f'<tr class="old"><th scope="row"><span class="tag">比較</span>ランダム<small>売買代金1億円以上から無作為</small></th>{cells(res["random"])}</tr>')
    B = res["bench"]
    rows.append(f'<tr class="old"><th scope="row"><span class="tag">比較</span>日経平均</th><td colspan="3"></td><td>{pct(B["is"]["annual"])}</td>'
                f'<td colspan="3"></td><td>{pct(B["oos"]["total"])}</td><td>{pct(B["oos"]["max_dd"])}</td><td></td></tr>')
    return f'<div class="tw"><table>{head}<tbody>{"".join(rows)}</tbody></table></div>'


def k_table(res):
    ks = [str(k) for k in res["meta"]["ks"]]
    head = "<thead><tr><th></th>" + "".join(f"<th>k={k} 探索</th><th>検証</th>" for k in ks) + "</tr></thead>"
    rows = []
    for s, by in res["signals"].items():
        c = "".join(f'<td{tone(by[k]["is"]["net"], 2)}>{pct(by[k]["is"]["net"], 2)}</td>'
                    f'<td{tone(by[k]["oos"]["net"], 2)}>{pct(by[k]["oos"]["net"], 2)}</td>' for k in ks)
        rows.append(f'<tr><th scope="row">{e(res["names"][s])}</th>{c}</tr>')
    return f'<div class="tw"><table>{head}<tbody>{"".join(rows)}</tbody></table></div>'


def curve(res):
    S = res["signals"]
    top = sorted(S, key=lambda s: -S[s]["best"]["is"]["cash"]["annual"])[:2]
    fake = {"composite": {}, "strategies": {}, "bench": {"oos": {"nikkei": res["bench"]["oos"]}}}
    for s in top:
        fake["strategies"][s] = {"name": res["names"][s], "oos": {"curve": S[s]["best"]["oos"]["cash"].get("curve") or []}}
    fake["strategies"]["sm"] = {"name": "SCORE勢い", "oos": {"curve": res["sm"]["oos"]["cash"].get("curve") or []}}
    fake["strategies"]["rnd"] = {"name": "ランダム", "oos": {"curve": res["random"]["oos"]["cash"].get("curve") or []}}
    return rr.curve(fake, "oos", [("s", s) for s in top + ["sm", "rnd"]])


def build(res, summary):
    m = res["meta"]
    P, r = m["periods"], m["rule"]
    demo = '<p class="warn">これは乱数のデモデータです。実際の結果ではありません。</p>' if m["demo"] else ""
    defs = "".join(f'<tr><th scope="row">{e(a)}</th><td class="txt">{e(b)}</td></tr>' for a, b in DEFS)
    return f"""<title>SMC・ICT 条件の検証</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Zen+Kaku+Gothic+New:wght@400;500;700&family=Shippori+Mincho+B1:wght@600;800&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{rr.CSS}
td small, th small {{ display: block; font-size: 11px; color: var(--mute); font-weight: 400; }}
.tw table {{ font-size: 13px; }}
.tw td, .tw th {{ padding-left: 6px; padding-right: 6px; }}
td.txt {{ text-align: left; white-space: normal; font-family: inherit; }}
</style>
<div class="wrap">
<header>
  <div class="eyebrow">Backtest · SMC / ICT</div>
  <h1>SMC・ICT 条件の検証</h1>
  <div class="meta">
    <span>探索 <b>{P["is"][0]} 〜 {P["is"][1]}</b></span>
    <span>検証 <b>{P["oos"][0]} 〜 {P["oos"][1]}</b></span>
    <span>ルール <b>損切り{r["stop_pct"]:.0f}% / +{r["take1_pct"]:.0f}%で半分・+{r["take2_pct"]:.0f}%で残り / {r["max_days"]}日 / {m["slots"]}銘柄</b></span>
  </div>
  {demo}
</header>

<section class="summary">
  <h2>結論</h2>
  {summary}
</section>

<section>
  <h2>条件の定義 (日足・買いのみ)</h2>
  <div class="tw"><table><tbody>{defs}</tbody></table></div>
</section>

<section>
  <h2>成績</h2>
  <p class="lead">各条件のスイング判定の幅 k は、探索期間の年率が一番良いものを選びました。期待値はコスト控除後の1回あたりの平均損益です。年率・損益・最大DDは資金を{m["slots"]}枠に分けて運用した場合で、空き資金は現金です。右端は、空き資金を日経平均で持った場合の直近1年の損益です。探索期間の年率の高い順に並べています。</p>
  {main_table(res)}
  {curve(res)}
</section>

<section>
  <h2>スイング判定の幅による違い (期待値)</h2>
  <p class="lead">k を変えても期待値の符号が変わらなければ、たまたま合った値ではないと見られます。</p>
  {k_table(res)}
</section>

<section>
  <h2>前提と注意点</h2>
  <div class="notes">
    <div><h3>日足で機械化したもの</h3><p>SMC・ICT は本来、複数の時間足を見て裁量で判断する手法です。ここでは日足だけで判定できる形にしたので、使う人の見方とは違う場合があります。とくにオーダーブロックや FVG の選び方は人によって幅があります。</p></div>
    <div><h3>並び順</h3><p>10枠が埋まっているときは、売買代金の大きい順に買います。これまでの検証で、当時分かる値の中では安定していた順番です。</p></div>
    <div><h3>共通の条件</h3><p>シグナル翌営業日の寄り付きで買い、決算 (推定日) が保有予定期間に入る銘柄は買いません。売買代金1億円以上の銘柄だけが対象です。売買コストは売買代金に応じて片道0.10〜0.40%で、税引き前です。上場廃止銘柄は含みません。</p></div>
    <div><h3>先読み</h3><p>すべて株価と出来高だけで判定しています。スイングは右側の足が出そろうまで確定させていないので、先読みはありません。</p></div>
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
