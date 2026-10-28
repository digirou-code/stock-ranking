"""WebUIのHTMLを組み立てる。

    python screener/build_page.py              # docs/index.html (data/latest.json を読む版)
    python screener/build_page.py --embed OUT  # データを埋め込んだ単体HTML
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "screener" / "ui_template.html"
HEAD = ('<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}'
        'body{margin:0}[hidden]{display:none!important}img{max-width:100%}</style></head><body>')

ap = argparse.ArgumentParser()
ap.add_argument("--embed", help="データ埋め込み版の出力先 (骨組みタグなし)")
args = ap.parse_args()
tpl = TEMPLATE.read_text(encoding="utf-8")
if args.embed:
    data = (ROOT / "docs" / "data" / "latest.json").read_text(encoding="utf-8")
    data = data.replace("</", "<\\/")
    hold = ROOT / "docs" / "data" / "holdings.json"
    hold = hold.read_text(encoding="utf-8").replace("</", "<\\/") if hold.exists() else "null"
    tpl = tpl.replace("/*__DATA__*/null", data).replace("/*__HOLDINGS__*/null", hold)
    Path(args.embed).write_text(tpl, encoding="utf-8")
else:
    (ROOT / "docs" / "index.html").write_text(HEAD + tpl + "</body></html>", encoding="utf-8")
