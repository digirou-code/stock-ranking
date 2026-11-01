"""日次レポート (HTML 1枚 + メール)。"""
from __future__ import annotations

import html
import json
import os
import smtplib
from email.mime.text import MIMEText

from . import config

SLEEVE = {"L": "大型株", "G": "成長株", "R": "急落リバウンド"}


def names() -> dict:
    """銘柄名: 既存サイトの latest.json (4桁コード) と、あれば data/master.parquet"""
    out = {}
    p = os.path.join(config.ROOT, "docs", "data", "latest.json")
    try:
        with open(p, encoding="utf-8") as f:
            for c, s in json.load(f).get("stocks", {}).items():
                out[c] = s.get("n", "")
    except (OSError, ValueError):
        pass
    mp = os.path.join(config.data_dir(), "master.parquet")
    if os.path.exists(mp):
        import pandas as pd
        df = pd.read_parquet(mp)
        col = next((c for c in ("CoName", "CompanyName") if c in df.columns), None)
        if col:
            for c, n in zip(df["Code"], df[col]):
                out[str(c)[:4]] = n
    return out


def _yen(x):
    return "-" if x is None or x != x else f"{x:,.0f}"


def _pct(x, sign=True):
    return "-" if x is None or x != x else (f"{x:+.2f}%" if sign else f"{x:.2f}%")


def _code(c):
    return c[:4]


def write_html(ctx: dict, path: str) -> None:
    nm = names()
    e = html.escape
    nmof = lambda c: e(nm.get(_code(c), ""))  # noqa: E731
    rows_buy = "".join(
        f"<tr><td>{SLEEVE[b['sleeve']]}{'' if b['live'] else '（紙上のみ）'}</td><td>{_code(b['code'])}</td><td>{nmof(b['code'])}</td>"
        f"<td class=n>{b['shares']:,}</td><td class=n>{_yen(b['ref_close'])}</td><td class=n>{_yen(b['amount'])}</td>"
        f"<td class=n>{_yen(b['limit']) if b['limit'] else '寄成'}</td><td class=n>{b['score']}</td></tr>"
        for b in ctx["orders"]["buy"]) or "<tr><td colspan=8>なし</td></tr>"
    rows_sell = "".join(
        f"<tr><td>{SLEEVE[h['sleeve']]}</td><td>{_code(h['code'])}</td><td>{nmof(h['code'])}</td>"
        f"<td class=n>{h['remaining']:,}</td><td>時間切れ（大引けで引成）</td></tr>"
        for h in ctx["orders"]["sell_time"]) or "<tr><td colspan=5>なし</td></tr>"
    rows_held = "".join(
        f"<tr class='{'warn' if h['near_stop'] else ''}'><td>{SLEEVE[h['sleeve']]}</td><td>{_code(h['code'])}</td><td>{nmof(h['code'])}</td>"
        f"<td>{h['entry']}</td><td class=n>{h['remaining']:,}</td><td class=n>{_yen(h['entry_px'])}</td><td class=n>{_yen(h['last'])}</td>"
        f"<td class=n>{_pct(h['pnl_pct'])}</td><td class=n>{_yen(h['stop'])}</td><td class=n>{_yen(h['take1']) if h['take1'] else '-'}</td>"
        f"<td class=n>{_yen(h['take2']) if h['take2'] else '-'}</td><td class=n>{h['days_left']}</td><td>{h['earnings'] or ''}</td></tr>"
        for h in ctx["held"]) or "<tr><td colspan=13>なし</td></tr>"
    perf = "".join(f"<tr><td>{r['label']}</td><td class=n>{_pct(r['mine'])}</td><td class=n>{_pct(r['nikkei'])}</td>"
                   f"<td class=n>{_pct(r['diff'])}</td></tr>" for r in ctx["perf"]) or "<tr><td colspan=4>開始前</td></tr>"
    cands = ""
    for k, lst in ctx["candidates"].items():
        body = "".join(f"<tr><td>{_code(c['code'])}</td><td>{nmof(c['code'])}</td><td class=n>{c['score']}</td>"
                       f"<td class=n>{'-' if c['d1'] is None else f'{c[chr(100)+chr(49)]:+}'}</td>"
                       f"<td class=n>{'-' if c['d5'] is None else f'{c[chr(100)+chr(53)]:+}'}</td>"
                       f"<td class=n>{_yen(c['close'])}</td><td class=n>{c['to20']}</td></tr>" for c in lst) or "<tr><td colspan=7>該当なし</td></tr>"
        cands += (f"<h3>{SLEEVE[k]}</h3><table><tr><th>コード</th><th>銘柄</th><th>スコア</th><th>前日比</th><th>5日前比</th>"
                  f"<th>終値</th><th>売買代金(億円)</th></tr>{body}</table>")
    warns = "".join(f"<li>{e(w)}</li>" for w in ctx["warnings"]) or "<li>なし</li>"
    blocked = "<p class=alert>データ点検に失敗したため、買い注文を出していません。</p>" if ctx.get("orders_blocked") else ""
    mv = ctx["sleeve_mv"]
    v = ctx["value"]
    alloc = "".join(f"<tr><td>{SLEEVE[k]}</td><td class=n>{ctx['config'][k]['weight'] * 100:.0f}%</td>"
                    f"<td class=n>{mv.get(k, 0) / v * 100:.1f}%</td></tr>" for k in ("L", "G", "R"))
    page = f"""<!doctype html><html lang=ja><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>システム日次レポート {ctx['asof']}</title><style>
:root{{--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#e5e5ea;--warn:#fff4e5;--alert:#c62828}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--muted:#a1a1a6;--line:#333;--warn:#3a2a10;--alert:#ff6b6b}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,"Hiragino Sans","Noto Sans JP",sans-serif;margin:0 auto;max-width:1100px;padding:16px}}
h1{{font-size:20px}}h2{{font-size:16px;margin-top:28px;border-bottom:1px solid var(--line)}}h3{{font-size:14px}}
table{{border-collapse:collapse;width:100%;margin:8px 0;display:block;overflow-x:auto}}th,td{{padding:4px 8px;border-bottom:1px solid var(--line);white-space:nowrap;text-align:left}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}}tr.warn td{{background:var(--warn)}}.muted{{color:var(--muted)}}.alert{{color:var(--alert);font-weight:600}}
</style></head><body>
<h1>システム日次レポート {ctx['asof']}（{ctx['mode']}）</h1>
<p class=muted>作成 {ctx['generated_at']}・紙上運用の開始 {ctx['paper_start']}・総資産 {_yen(v)}円・現金 {_yen(ctx['cash'])}円</p>
{blocked}
<h2>1. 明日の注文</h2><p class=muted>買いは寄りで約定。指値列がある場合は「前日終値+5%」の寄指（それを超えて始まったら買わない）。損切りは逆指値、利確は指値で発注しておく（保有一覧の価格）。</p>
<h3>買い</h3><table><tr><th>枠</th><th>コード</th><th>銘柄</th><th>株数</th><th>終値</th><th>概算金額</th><th>指値</th><th>スコア</th></tr>{rows_buy}</table>
<h3>売り</h3><table><tr><th>枠</th><th>コード</th><th>銘柄</th><th>株数</th><th>理由</th></tr>{rows_sell}</table>
<h2>2. 成績（日経1321 配当込みとの比較）</h2><table><tr><th>期間</th><th>システム</th><th>日経</th><th>差</th></tr>{perf}</table>
<h2>3. リスク</h2><table>
<tr><td>過去最高からの下落</td><td class=n>{ctx['dd'] * 100:.1f}%</td></tr>
<tr><td>新規買いの倍率（DDブレーキ）</td><td class=n>{ctx['brake_scale']:.1f}</td></tr>
<tr><td>全保有が損切りになった場合の損失合計</td><td class=n>{ctx['open_risk_pct'] * 100:.1f}%（警告 {ctx['risk']['open_risk_warn'] * 100:.0f}%）</td></tr>
</table><table><tr><th>枠</th><th>配分</th><th>いまの比率</th></tr>{alloc}</table>
<h3>警告</h3><ul>{warns}</ul>
<h2>4. 保有一覧</h2><p class=muted>色付きの行は損切りまで3%以内。</p>
<table><tr><th>枠</th><th>コード</th><th>銘柄</th><th>買付日</th><th>株数</th><th>買値</th><th>現値</th><th>損益</th><th>損切り(逆指値)</th><th>利確1</th><th>利確2</th><th>残り日数</th><th>決算予定</th></tr>{rows_held}</table>
<h2>5. 候補ランキング</h2>{cands}
<p class=muted>このレポートは投資判断の材料で、売買を推奨するものではありません。</p>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)


def mail_text(ctx: dict) -> tuple[str, str]:
    nm = names()
    perf = {r["label"]: r for r in ctx["perf"]}
    ytd = perf.get("年初来")
    tag = "[要確認] " if ctx.get("orders_blocked") else ""
    subj = (f"{tag}[システム] {ctx['asof']} 注文 売{len(ctx['orders']['sell_time'])} 買{len(ctx['orders']['buy'])} | "
            f"資産 {ctx['value'] / 1e4:,.0f}万円" + (f" 年初来 {ytd['mine']:+.1f}%（日経比 {ytd['diff']:+.1f}pt）" if ytd else "")
            + f" | DD {ctx['dd'] * 100:.1f}%")
    lines = []
    for h in ctx["orders"]["sell_time"]:
        lines.append(f"売り  {h['code'][:4]} {nm.get(h['code'][:4], '')}  {h['remaining']}株  時間切れ(引成)")
    for b in ctx["orders"]["buy"]:
        lines.append(f"買い  {SLEEVE[b['sleeve']]}  {b['code'][:4]} {nm.get(b['code'][:4], '')}  {b['shares']}株  "
                     f"{'寄指 ' + format(b['limit'], ',') if b['limit'] else '寄成'}{'' if b['live'] else '  (紙上のみ)'}")
    lines.append("")
    lines += [f"警告: {w}" for w in ctx["warnings"]]
    return subj, "\n".join(lines) or "注文なし"


def send_mail(ctx: dict) -> None:
    """Gmail の SMTP。環境変数 MAIL_USER・MAIL_APP_PASSWORD・MAIL_TO が無ければ何もしない"""
    user, pw, to = (os.environ.get(k) for k in ("MAIL_USER", "MAIL_APP_PASSWORD", "MAIL_TO"))
    if not (user and pw and to):
        print("mail: 設定が無いので送りません")
        return
    subj, body = mail_text(ctx)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subj, user, to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)
