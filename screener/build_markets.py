"""マーケット指標 (日足) と経済スケジュールを生成する。

    python screener/build_markets.py

出力:
    docs/data/markets.json     指標ごとの1年分の日足 (始値・高値・安値・終値)
    docs/data/calendar.json    過去3日〜10日先の経済イベント (日本時間)
    cache/calendar_store.json  取得済みイベントの蓄積 (週をまたいで過去分を残すため)
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
STORE = ROOT / "cache" / "calendar_store.json"
JST = dt.timezone(dt.timedelta(hours=9))

# (ティッカー, 表示名, グループ, 単位)
INDICES = [
    ("^N225", "日経平均", "日本", "円"),
    ("NIY=F", "日経平均先物 (CME円建て)", "日本", "円"),
    ("^DJI", "NYダウ", "米国", "ドル"),
    ("^GSPC", "S&P500", "米国", ""),
    ("^IXIC", "ナスダック総合", "米国", ""),
    ("^SOX", "SOX指数 (半導体)", "米国", ""),
    ("^VIX", "VIX (恐怖指数)", "米国", ""),
    ("^TNX", "米10年債利回り", "金利", "%"),
    ("JPY=X", "ドル円", "為替", "円"),
    ("EURJPY=X", "ユーロ円", "為替", "円"),
    ("CL=F", "WTI原油先物", "商品", "ドル"),
    ("GC=F", "金先物", "商品", "ドル"),
]

# Forex Factory の週間カレンダー (公開JSON)
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
COUNTRY = {
    "JPY": "日本", "USD": "米国", "EUR": "ユーロ圏", "GBP": "英国", "CNY": "中国",
    "AUD": "豪州", "CAD": "カナダ", "CHF": "スイス", "NZD": "NZ",
}
IMPACT = {"High": 3, "Medium": 2, "Low": 1, "Holiday": 0}

# よく出るイベント名の日本語訳 (1回の走査で、左から最初に合うものを置き換える)
TERMS = [
    (r"Federal Funds Rate", "政策金利 (FF金利)"),
    (r"FOMC Statement", "FOMC声明"),
    (r"FOMC Press Conference", "FOMC議長会見"),
    (r"FOMC Meeting Minutes", "FOMC議事要旨"),
    (r"BOJ Policy Rate", "日銀 政策金利"),
    (r"Monetary Policy Statement", "金融政策声明"),
    (r"BOJ Press Conference", "日銀総裁会見"),
    (r"BOJ Outlook Report", "日銀 展望レポート"),
    (r"Main Refinancing Rate", "ECB 政策金利"),
    (r"ECB Press Conference", "ECB総裁会見"),
    (r"Official Bank Rate", "英中銀 政策金利"),
    (r"Non-Farm Employment Change", "非農業部門雇用者数"),
    (r"ADP Non-Farm Employment Change", "ADP雇用統計"),
    (r"Unemployment Claims", "新規失業保険申請件数"),
    (r"Unemployment Rate", "失業率"),
    (r"Average Hourly Earnings", "平均時給"),
    (r"JOLTS Job Openings", "JOLTS求人件数"),
    (r"Tokyo Core CPI", "東京都区部コアCPI"),
    (r"National Core CPI", "全国コアCPI"),
    (r"BOJ Core CPI", "日銀版コアCPI"),
    (r"Core CPI", "コアCPI"),
    (r"CPI", "消費者物価指数 (CPI)"),
    (r"Core PCE Price Index", "コアPCE価格指数"),
    (r"PCE Price Index", "PCE価格指数"),
    (r"Core PPI", "コアPPI"),
    (r"PPI", "生産者物価指数 (PPI)"),
    (r"SPPI", "企業向けサービス価格指数"),
    (r"Advance GDP Price Index", "GDP価格指数 (速報)"),
    (r"Advance GDP", "GDP (速報)"),
    (r"Prelim GDP", "GDP (改定)"),
    (r"Final GDP", "GDP (確報)"),
    (r"GDP", "GDP"),
    (r"Core Retail Sales", "小売売上高 (除自動車)"),
    (r"Retail Sales", "小売売上高"),
    (r"ISM Manufacturing PMI", "ISM製造業景況指数"),
    (r"ISM Services PMI", "ISM非製造業景況指数"),
    (r"Flash Manufacturing PMI", "製造業PMI (速報)"),
    (r"Flash Services PMI", "サービス業PMI (速報)"),
    (r"Final Manufacturing PMI", "製造業PMI (確報)"),
    (r"Final Services PMI", "サービス業PMI (確報)"),
    (r"Manufacturing PMI", "製造業PMI"),
    (r"Services PMI", "サービス業PMI"),
    (r"Tankan Manufacturing Index", "日銀短観 大企業製造業"),
    (r"Tankan Non-Manufacturing Index", "日銀短観 大企業非製造業"),
    (r"Prelim Industrial Production", "鉱工業生産 (速報)"),
    (r"Industrial Production", "鉱工業生産"),
    (r"Household Spending", "家計支出"),
    (r"Trade Balance", "貿易収支"),
    (r"Current Account", "経常収支"),
    (r"Consumer Confidence", "消費者信頼感"),
    (r"CB Consumer Confidence", "コンファレンスボード消費者信頼感"),
    (r"Prelim UoM Consumer Sentiment", "ミシガン大消費者信頼感 (速報)"),
    (r"Revised UoM Consumer Sentiment", "ミシガン大消費者信頼感 (確報)"),
    (r"Durable Goods Orders", "耐久財受注"),
    (r"Core Durable Goods Orders", "耐久財受注 (除輸送機器)"),
    (r"Building Permits", "建設許可件数"),
    (r"Housing Starts", "住宅着工件数"),
    (r"Existing Home Sales", "中古住宅販売件数"),
    (r"New Home Sales", "新築住宅販売件数"),
    (r"Pending Home Sales", "中古住宅販売保留"),
    (r"Crude Oil Inventories", "原油在庫"),
    (r"Natural Gas Storage", "天然ガス在庫"),
    (r"Empire State Manufacturing Index", "NY連銀製造業景気指数"),
    (r"Philly Fed Manufacturing Index", "フィラデルフィア連銀製造業景気指数"),
    (r"Employment Cost Index", "雇用コスト指数"),
    (r"Bank Holiday", "祝日"),
    (r"German ", "ドイツ "),
    (r"French ", "フランス "),
    (r"Italian ", "イタリア "),
    (r"Spanish ", "スペイン "),
    (r"Chinese ", "中国 "),
    (r"BOJ ", "日銀 "),
    (r"Prelim ", "速報 "),
    (r"Flash ", "速報 "),
    (r"Speaks", "発言"),
    (r"Gov ", "総裁 "),
    (r"Fed Chair", "FRB議長"),
    (r"President", "総裁"),
    (r" y/y", " 前年比"),
    (r" m/m", " 前月比"),
    (r" q/q", " 前期比"),
]


_TERM_RE = re.compile("|".join(f"({p})" for p, _ in TERMS))


def ja_title(title: str) -> str:
    return _TERM_RE.sub(lambda m: TERMS[m.lastindex - 1][1], title)


def tidy(v):
    if isinstance(v, float):
        return None if math.isnan(v) else round(v, 4 if abs(v) < 10 else 2)
    if isinstance(v, dict):
        return {k: tidy(x) for k, x in v.items()}
    if isinstance(v, list):
        return [tidy(x) for x in v]
    return v


# ---------------------------------------------------------------- 指標
def build_markets():
    import yfinance as yf
    syms = [s for s, *_ in INDICES]
    df = yf.download(syms, period="1y", interval="1d", group_by="ticker",
                     auto_adjust=False, threads=True, progress=False)
    items = []
    for sym, name, group, unit in INDICES:
        try:
            h = df[sym].dropna(subset=["Close"])
        except KeyError:
            h = []
        if len(h) < 2:  # 一括取得で欠けた銘柄は個別に取り直す
            h = yf.Ticker(sym).history(period="1y", interval="1d", auto_adjust=False).dropna(subset=["Close"])
        if len(h) < 2:
            print(f"  market missing {sym}")
            continue
        bars = []
        for idx, r in h.iterrows():
            o, hi, lo, c = (float(r[k]) for k in ("Open", "High", "Low", "Close"))
            if any(math.isnan(x) for x in (o, hi, lo)):
                o = hi = lo = c
            bars.append([idx.strftime("%Y-%m-%d"), o, hi, lo, c])
        last, prev = bars[-1][4], bars[-2][4]
        items.append({
            "sym": sym, "name": name, "group": group, "unit": unit,
            "date": bars[-1][0], "last": last, "chg": last - prev,
            "chgp": (last / prev - 1) * 100 if prev else None,
            "hi52": max(b[2] for b in bars), "lo52": min(b[3] for b in bars),
            "bars": bars,
        })
    out = {"generated_at": dt.datetime.now(JST).isoformat(timespec="minutes"), "items": items}
    (DATA_DIR / "markets.json").write_text(
        json.dumps(tidy(out), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"markets: {len(items)}/{len(INDICES)}")


# ---------------------------------------------------------------- 経済スケジュール
def build_calendar():
    import requests
    store = json.loads(STORE.read_text(encoding="utf-8")) if STORE.exists() else {}
    try:
        res = requests.get(FF_URL, timeout=30)
        res.raise_for_status()
        events = res.json()
    except Exception as e:
        print(f"  calendar fetch failed: {e}")
        events = []
    for e in events:
        t = dt.datetime.fromisoformat(e["date"]).astimezone(JST)
        key = f'{e["country"]}|{e["title"]}|{t.date().isoformat()}'
        store[key] = {
            "t": t.isoformat(timespec="minutes"),
            "allday": t.strftime("%H:%M") == "00:00" and e.get("impact") == "Holiday",
            "cc": e["country"],
            "country": COUNTRY.get(e["country"], e["country"]),
            "title": e["title"],
            "ja": ja_title(e["title"]),
            "impact": IMPACT.get(e.get("impact"), 0),
            "forecast": e.get("forecast") or "",
            "previous": e.get("previous") or "",
        }
    now = dt.datetime.now(JST)
    keep_from = (now - dt.timedelta(days=30)).isoformat()
    store = {k: v for k, v in store.items() if v["t"] >= keep_from}
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(store, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    start = (now - dt.timedelta(days=3)).date().isoformat()
    end = (now + dt.timedelta(days=11)).date().isoformat()
    rows = sorted((v for v in store.values() if start <= v["t"][:10] < end), key=lambda v: v["t"])
    covered = max((v["t"][:10] for v in store.values()), default=None)
    out = {
        "generated_at": now.isoformat(timespec="minutes"),
        "from": start, "to": (now + dt.timedelta(days=10)).date().isoformat(),
        "covered_until": covered,  # 取得できている最終日 (公開フィードは今週分のみ)
        "source": "Forex Factory",
        "events": rows,
    }
    (DATA_DIR / "calendar.json").write_text(
        json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"calendar: {len(rows)} events ({start}〜), covered until {covered}")


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        build_markets()
    except Exception as e:
        print(f"markets failed: {e}")
    build_calendar()
