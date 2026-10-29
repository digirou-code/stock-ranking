"""保有銘柄ページ用のデータ (決算予定日・信用残・ステージ・その日の評価) を作る。"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parent.parent
HOLDINGS_FILE = ROOT / "holdings.csv"
OUT_FILE = ROOT / "docs" / "data" / "holdings.json"
HIST_DIR = ROOT / "docs" / "data" / "holdings_history"

UA = {"User-Agent": "Mozilla/5.0 (stock-ranking; +https://github.com/digirou-code/stock-ranking)"}
JPX = "https://www.jpx.co.jp"
EARNINGS_PAGE = JPX + "/listing/event-schedules/financial-announcement/index.html"
MARGIN_PAGE = JPX + "/markets/statistics-equities/margin/01.html"

CODE_RE = re.compile(r"^([0-9][0-9A-Z]{2}[0-9A-Z])\s+(.+)$")

STAGES = {
    1: "底固め",
    2: "上昇",
    3: "天井圏",
    4: "下降",
}


# ---------------------------------------------------------------- 保有銘柄
def load_holdings():
    """SBI証券のポートフォリオCSV (Shift-JIS/UTF-8) から個別株と投資信託を読む"""
    if not HOLDINGS_FILE.exists():
        return [], []
    raw = HOLDINGS_FILE.read_bytes()
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    stocks, funds, section = [], [], ""
    for row in csv.reader(io.StringIO(text)):
        cells = [c.strip() for c in row if c.strip()]
        if not cells:
            continue
        head = cells[0]
        if len(cells) == 1 and ("株式（" in head or "投資信託（" in head):
            section = head
            continue
        acct = account_label(section)
        m = CODE_RE.match(head)
        if m and "株式" in section:
            if not any(s["c"] == m.group(1) for s in stocks):
                stocks.append({"c": m.group(1), "n": m.group(2), "acct": acct})
            else:
                next(s for s in stocks if s["c"] == m.group(1))["acct"] += " / " + acct
        elif "投資信託" in section and len(cells) > 2 and head not in ("ファンド名",):
            if not any(f["n"] == head for f in funds):
                funds.append({"n": head, "acct": acct})
            else:
                next(f for f in funds if f["n"] == head)["acct"] += " / " + acct
    return stocks, funds


def account_label(section: str) -> str:
    if "NISA" in section:
        return "NISA成長" if "成長" in section else "NISAつみたて"
    if "特定" in section:
        return "特定"
    if "一般" in section:
        return "一般"
    return ""


# ---------------------------------------------------------------- 決算発表予定日 (JPX)
def load_earnings_schedule():
    """JPXの決算発表予定日一覧 (複数のxlsx) をコード→[(日付, 種別)] にまとめる"""
    import pandas as pd
    import requests
    html = requests.get(EARNINGS_PAGE, headers=UA, timeout=30).text
    urls = sorted(set(re.findall(r'href="([^"]*kessan[^"]*\.xlsx?)"', html)))
    out: dict[str, list] = {}
    for u in urls:
        try:
            b = requests.get(urljoin(EARNINGS_PAGE, u), headers=UA, timeout=60).content
            df = pd.read_excel(io.BytesIO(b), header=None)
        except Exception as e:
            print(f"  earnings file failed {u}: {e}")
            continue
        hdr = next((i for i, r in df.iterrows() if any("コード" in str(v) for v in r.values)), None)
        if hdr is None:
            continue
        cols = [str(v) for v in df.iloc[hdr].values]
        c_date = next(i for i, v in enumerate(cols) if "決算発表予定日" in v)
        c_code = next(i for i, v in enumerate(cols) if "コード" in v)
        c_kind = next((i for i, v in enumerate(cols) if "種別" in v), None)
        for _, r in df.iloc[hdr + 1:].iterrows():
            d, code = r.iloc[c_date], str(r.iloc[c_code]).strip()
            if not re.fullmatch(r"[0-9][0-9A-Z]{3}", code):
                continue
            try:
                d = pd.Timestamp(d).date()
            except Exception:
                continue
            kind = str(r.iloc[c_kind]).strip() if c_kind is not None else ""
            out.setdefault(code, []).append((d, kind))
    print(f"earnings schedule: {len(urls)} files, {len(out)} codes")
    return out


# ---------------------------------------------------------------- 銘柄別信用取引残高 (JPX PDF)
MARGIN_RE = re.compile(
    r"\b([0-9][0-9A-Z]{3})0 JP[0-9A-Z]{10}\s*株数 Shs\.\s+"
    r"([\d,]+) (▲ )?([\d,]+) (\S+) ([\d,]+) (▲ )?([\d,]+) (\S+)"
)


def load_margin(codes):
    """最新の銘柄別信用取引残高PDFから、指定コードの売残・買残を取り出す"""
    import pdfplumber
    import requests
    html = requests.get(MARGIN_PAGE, headers=UA, timeout=30).text
    urls = re.findall(r'href="([^"]*_mtall\.pdf)"', html)
    if not urls:
        print("  margin: PDFが見つかりません")
        return {}, None
    url = urljoin(MARGIN_PAGE, sorted(urls)[-1])
    date = re.search(r"(\d{8})_mtall", url).group(1)
    date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    pdf_bytes = requests.get(url, headers=UA, timeout=120).content
    want, out = set(codes), {}
    num = lambda s: int(s.replace(",", ""))
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for m in MARGIN_RE.finditer(text):
                code = m.group(1)
                if code not in want or code in out:
                    continue
                sell, buy = num(m.group(2)), num(m.group(6))
                out[code] = {
                    "sell": sell,
                    "sell_chg": -num(m.group(4)) if m.group(3) else num(m.group(4)),
                    "sell_ratio": m.group(5),
                    "buy": buy,
                    "buy_chg": -num(m.group(8)) if m.group(7) else num(m.group(8)),
                    "buy_ratio": m.group(9),
                    "ratio": round(buy / sell, 2) if sell else None,  # 信用倍率
                }
            if len(out) == len(want):
                break
    print(f"margin {date}: {len(out)}/{len(want)} codes")
    return out, date


# ---------------------------------------------------------------- ステージ判定
def stage_of(close, pos52, n=150):
    """ワインスタインのステージ分析 (n日移動平均の傾きと株価の位置)。
    150日≒30週が本来の形。75日は同じ判定を半分の期間で行う短期版"""
    lag = n * 2 // 15          # 傾きを測る間隔 (150日なら20日、75日なら10日)
    th = n / 150               # 横ばいとみなす傾きの幅 (150日なら±1%、75日なら±0.5%)
    if len(close) < n + lag:
        return None
    ma = float(close.iloc[-n:].mean())
    ma_prev = float(close.iloc[-n - lag:-lag].mean())
    slope = (ma / ma_prev - 1) * 100
    price = float(close.iloc[-1])
    if slope >= th:
        st = 2 if price > ma else 3
    elif slope <= -th:
        st = 4 if price < ma else 1
    else:
        st = 3 if (pos52 or 0) >= 50 else 1
    return {"stage": st, "n": n, "ma150": round(ma, 1), "slope": round(slope, 2),
            "dev": round((price / ma - 1) * 100, 1)}


# ---------------------------------------------------------------- 出力
def write_holdings(holdings, funds, stocks_by_code, screens, asof, earnings, margin, margin_date):
    today = dt.date.fromisoformat(asof)
    prev = load_prev(asof)
    rows = []
    for h in holdings:
        s = stocks_by_code.get(h["c"], {})
        ranks = [{"id": sc["id"], "name": sc["name"], "r": row["r"]}
                 for sc in screens for row in sc["rows"] if row["c"] == h["c"]]
        rows.append({
            **{k: s.get(k) for k in ("sec", "mkt", "p", "chg", "mc", "per", "pbr", "dy", "roe",
                                     "opm", "rg", "eg", "de", "r1m", "r3m", "r6m", "pos52", "to",
                                     "score", "sub", "items", "stage", "stage75", "vol", "vchg")},
            "c": h["c"],
            "n": s.get("n") or h["n"],
            "acct": h["acct"],
            "score_prev": prev.get(h["c"]),
            "ranks": ranks,
            "earn": next_earnings(h["c"], earnings, s.get("earn_ts"), today),
            "margin": margin.get(h["c"]),
        })
    out = {"asof": asof, "margin_date": margin_date, "stages": STAGES, "rows": rows, "funds": funds}
    text = json.dumps(tidy(out), ensure_ascii=False, separators=(",", ":"))
    OUT_FILE.write_text(text, encoding="utf-8")
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    (HIST_DIR / f"{asof}.json").write_text(text, encoding="utf-8")
    for p in sorted(HIST_DIR.glob("*.json"))[:-90]:
        p.unlink()
    print(f"holdings: {len(rows)} stocks, {len(funds)} funds")


def tidy(v):
    """浮動小数を丸め、NaNをnullにする"""
    if isinstance(v, float):
        return None if v != v else round(v, 2)
    if isinstance(v, dict):
        return {k: tidy(x) for k, x in v.items()}
    if isinstance(v, list):
        return [tidy(x) for x in v]
    return v


def next_earnings(code, earnings, earn_ts, today):
    """次回の決算発表日。JPXの予定を優先し、なければYahooの予定日を使う"""
    sched = sorted((d, k) for d, k in earnings.get(code, []) if d >= today - dt.timedelta(days=3))
    if sched:
        d, k = sched[0]
        return {"date": d.isoformat(), "kind": k, "src": "JPX"}
    if earn_ts:
        d = dt.datetime.fromtimestamp(earn_ts, dt.timezone(dt.timedelta(hours=9))).date()
        if d >= today - dt.timedelta(days=3):
            return {"date": d.isoformat(), "kind": "", "src": "Yahoo"}
    return None


def load_prev(asof):
    if not HIST_DIR.exists():
        return {}
    files = sorted(p for p in HIST_DIR.glob("*.json") if p.stem < asof)
    if not files:
        return {}
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    return {r["c"]: r.get("score") for r in data["rows"]}
