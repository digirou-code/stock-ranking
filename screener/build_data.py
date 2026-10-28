"""日次スクリーニングデータを生成する。

使い方:
    python screener/build_data.py            # 実データ (JPX銘柄一覧 + Yahoo Finance)
    python screener/build_data.py --demo     # ネット不要のデモデータ

出力:
    docs/data/latest.json             WebUIが読むデータ
    docs/data/history/YYYY-MM-DD.json 日次スナップショット
    cache/fundamentals.json           財務指標キャッシュ (数日おきに順次更新)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
HIST_DIR = DATA_DIR / "history"
CACHE_FILE = ROOT / "cache" / "fundamentals.json"
JST = dt.timezone(dt.timedelta(hours=9))

# 東証上場銘柄一覧のページ。ファイルのURLは変わることがあるので、毎回ページからリンクを探す
JPX_LIST_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
UA = {"User-Agent": "Mozilla/5.0 (stock-ranking; +https://github.com/digirou-code/stock-ranking)"}
# 対象市場 (カンマ区切りで上書き可)
MARKETS = os.environ.get("MARKETS", "プライム,スタンダード,グロース").split(",")
# 1回の実行で財務指標を取り直す銘柄数の上限 (Yahooのレート制限対策)
FUND_REFRESH_PER_RUN = int(os.environ.get("FUND_REFRESH_PER_RUN", "700"))
FUND_MAX_AGE_DAYS = int(os.environ.get("FUND_MAX_AGE_DAYS", "7"))
TOP_N = 50

# ---------------------------------------------------------------- スコア定義
# 総合SCOREの定量11項目: (キー, ラベル, カテゴリ, 高い方が良いか)
SCORE_ITEMS = [
    ("per", "PER", "value", False),
    ("pbr", "PBR", "value", False),
    ("dy", "配当利回り", "income", True),
    ("roe", "ROE", "profit", True),
    ("opm", "営業利益率", "profit", True),
    ("rg", "売上成長率", "growth", True),
    ("eg", "利益成長率", "growth", True),
    ("de", "負債資本倍率", "health", False),
    ("r3m", "3ヶ月騰落率", "momentum", True),
    ("pos52", "52週レンジ位置", "momentum", True),
    ("to", "売買代金", "momentum", True),
]
CATEGORIES = {
    "value": "割安",
    "income": "配当",
    "profit": "収益",
    "growth": "成長",
    "health": "健全",
    "momentum": "勢い",
}
MIN_COVERAGE = 8  # 11項目中この数以上そろった銘柄だけ総合SCOREに載せる


def ok(v) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v))


# ---------------------------------------------------------------- スクリーニング定義
def _rank_sum(stocks, keys_dirs):
    """複数指標のパーセンタイル合計で並べるためのキー関数を作る"""
    pct = {}
    for k, hi in keys_dirs:
        pct[k] = percentiles([s.get(k) for s in stocks], hi)
    idx = {id(s): i for i, s in enumerate(stocks)}
    return lambda s: sum(pct[k][idx[id(s)]] or 0 for k, _ in keys_dirs)


SCREENS = [
    {
        "id": "score",
        "name": "総合SCORE",
        "tab": "総合",
        "desc": "定量11項目の総合評価が高い順",
        "cond": [f"11項目中{MIN_COVERAGE}項目以上のデータあり"],
        "filter": lambda s: s.get("score") is not None,
        "sort": lambda s: s["score"],
    },
    {
        "id": "dividend",
        "name": "高配当",
        "tab": "配当",
        "desc": "配当利回りが高く、稼ぐ力も最低限ある銘柄",
        "cond": ["配当利回り 3.5%以上", "ROE 5%以上", "時価総額 100億円以上"],
        "filter": lambda s: ok(s.get("dy")) and s["dy"] >= 3.5
        and ok(s.get("roe")) and s["roe"] >= 5
        and ok(s.get("mc")) and s["mc"] >= 100,
        "sort": lambda s: s["dy"],
    },
    {
        "id": "div_pbr",
        "name": "高配当×低PBR",
        "tab": "配当×割安",
        "desc": "配当利回りとPBRの割安さを合わせて評価",
        "cond": ["配当利回り 3%以上", "PBR 1.0倍以下"],
        "filter": lambda s: ok(s.get("dy")) and s["dy"] >= 3
        and ok(s.get("pbr")) and 0 < s["pbr"] <= 1.0,
        "sort_multi": [("dy", True), ("pbr", False)],
    },
    {
        "id": "low_per",
        "name": "低PER",
        "tab": "割安",
        "desc": "利益に対して株価が安い銘柄 (赤字・低収益を除外)",
        "cond": ["PER 0〜12倍", "ROE 8%以上"],
        "filter": lambda s: ok(s.get("per")) and 0 < s["per"] <= 12
        and ok(s.get("roe")) and s["roe"] >= 8,
        "sort": lambda s: -s["per"],
    },
    {
        "id": "low_pbr",
        "name": "低PBR",
        "tab": "資産割安",
        "desc": "純資産に対して株価が安い銘柄 (黒字のみ)",
        "cond": ["PBR 0.8倍以下", "ROE 0%超"],
        "filter": lambda s: ok(s.get("pbr")) and 0 < s["pbr"] <= 0.8
        and ok(s.get("roe")) and s["roe"] > 0,
        "sort": lambda s: -s["pbr"],
    },
    {
        "id": "high_roe",
        "name": "高ROE",
        "tab": "収益",
        "desc": "自己資本を効率よく利益に変えている銘柄",
        "cond": ["ROE 15%以上", "負債資本倍率 100%未満"],
        "filter": lambda s: ok(s.get("roe")) and s["roe"] >= 15
        and (not ok(s.get("de")) or s["de"] < 100),
        "sort": lambda s: s["roe"],
    },
    {
        "id": "growth",
        "name": "成長株",
        "tab": "成長",
        "desc": "売上と利益がともに伸びている銘柄",
        "cond": ["売上成長率 10%以上", "利益成長率 10%以上", "時価総額 300億円以上"],
        "filter": lambda s: ok(s.get("rg")) and s["rg"] >= 10
        and ok(s.get("eg")) and s["eg"] >= 10
        and ok(s.get("mc")) and s["mc"] >= 300,
        "sort_multi": [("rg", True), ("eg", True)],
    },
    {
        "id": "small_growth",
        "name": "小型成長",
        "tab": "発掘",
        "desc": "まだ小さいが売上の伸びが大きい銘柄",
        "cond": ["時価総額 300億円未満", "売上成長率 15%以上", "営業利益率 0%超"],
        "filter": lambda s: ok(s.get("mc")) and s["mc"] < 300
        and ok(s.get("rg")) and s["rg"] >= 15
        and ok(s.get("opm")) and s["opm"] > 0,
        "sort_multi": [("rg", True), ("opm", True)],
    },
    {
        "id": "momentum",
        "name": "モメンタム",
        "tab": "勢い",
        "desc": "直近の上昇が強く、52週高値圏にある銘柄",
        "cond": ["52週レンジ位置 80%以上", "売買代金 1億円以上"],
        "filter": lambda s: ok(s.get("pos52")) and s["pos52"] >= 80
        and ok(s.get("r3m")) and ok(s.get("to")) and s["to"] >= 1,
        "sort": lambda s: s["r3m"],
    },
]


# ---------------------------------------------------------------- 共通計算
def percentiles(values, higher_is_better=True):
    """各値の分位 (0〜1)。欠損は None"""
    pairs = [(v, i) for i, v in enumerate(values) if ok(v)]
    out = [None] * len(values)
    if not pairs:
        return out
    pairs.sort(key=lambda x: x[0])
    n = len(pairs)
    for r, (_, i) in enumerate(pairs):
        p = r / (n - 1) if n > 1 else 0.5
        out[i] = p if higher_is_better else 1 - p
    return out


def score_stocks(stocks):
    cols = {}
    for key, _, _, hi in SCORE_ITEMS:
        vals = [s.get(key) for s in stocks]
        if key == "per":  # 赤字(負のPER)は最下位扱い
            vals = [v if ok(v) and v > 0 else (9999 if ok(v) else None) for v in vals]
        if key == "pbr":
            vals = [v if ok(v) and v > 0 else None for v in vals]
        cols[key] = percentiles(vals, hi)
    for i, s in enumerate(stocks):
        items = {k: cols[k][i] for k, *_ in SCORE_ITEMS}
        have = [v for v in items.values() if v is not None]
        s["items"] = {k: (round(v * 100) if v is not None else None) for k, v in items.items()}
        sub = {}
        for cat in CATEGORIES:
            vs = [items[k] for k, _, c, _ in SCORE_ITEMS if c == cat and items[k] is not None]
            sub[cat] = round(sum(vs) / len(vs) * 100) if vs else None
        s["sub"] = sub
        if len(have) >= MIN_COVERAGE:
            # 欠損項目は中央値(0.5)で補い、全11項目の平均を0〜100で表示
            filled = [v if v is not None else 0.5 for v in items.values()]
            s["score"] = round(sum(filled) / len(filled) * 100, 1)
        else:
            s["score"] = None


def run_screens(stocks, prev_ranks):
    result = []
    for sc in SCREENS:
        hits = [s for s in stocks if sc["filter"](s)]
        if "sort_multi" in sc:
            key = _rank_sum(hits, sc["sort_multi"])
        else:
            key = sc["sort"]
        hits.sort(key=key, reverse=True)
        prev = prev_ranks.get(sc["id"], {})
        rows = []
        for r, s in enumerate(hits[:TOP_N], 1):
            rows.append({"c": s["c"], "r": r, "pr": prev.get(s["c"])})
        result.append({
            "id": sc["id"], "name": sc["name"], "tab": sc["tab"],
            "desc": sc["desc"], "cond": sc["cond"],
            "hits": len(hits), "rows": rows,
        })
    return result


def load_prev_ranks(today: str):
    """前営業日のスナップショットから各スクリーニングの順位を読む"""
    if not HIST_DIR.exists():
        return {}, None
    files = sorted(p for p in HIST_DIR.glob("*.json") if p.stem < today)
    if not files:
        return {}, None
    prev = json.loads(files[-1].read_text(encoding="utf-8"))
    ranks = {sc["id"]: {row["c"]: row["r"] for row in sc["rows"]} for sc in prev["screens"]}
    return ranks, prev["asof"]


def write_output(stocks, asof: str, demo: bool, universe: int):
    score_stocks(stocks)
    prev_ranks, prev_asof = load_prev_ranks(asof)
    screens = run_screens(stocks, prev_ranks)
    used = {row["c"] for sc in screens for row in sc["rows"]}
    out = {
        "asof": asof,
        "prev_asof": prev_asof,
        "generated_at": dt.datetime.now(JST).isoformat(timespec="minutes"),
        "demo": demo,
        "universe": universe,
        "scored": sum(1 for s in stocks if s.get("score") is not None),
        "items": [{"k": k, "label": l, "cat": c, "hi": h} for k, l, c, h in SCORE_ITEMS],
        "categories": CATEGORIES,
        "screens": screens,
        # ランキングに出た銘柄だけ詳細を持たせて軽くする
        "stocks": {s["c"]: compact(s) for s in stocks if s["c"] in used},
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    (DATA_DIR / "latest.json").write_text(text, encoding="utf-8")
    (HIST_DIR / f"{asof}.json").write_text(text, encoding="utf-8")
    keep = sorted(HIST_DIR.glob("*.json"))[:-90]  # 直近90日分だけ残す
    for p in keep:
        p.unlink()
    print(f"wrote {asof}: universe={universe} scored={out['scored']} "
          + " ".join(f"{s['id']}={s['hits']}" for s in screens))


def compact(s):
    keys = ["c", "n", "sec", "mkt", "p", "chg", "mc", "per", "pbr", "dy", "roe",
            "opm", "rg", "eg", "de", "r3m", "r6m", "pos52", "to", "score", "sub", "items"]
    out = {}
    for k in keys:
        v = s.get(k)
        if isinstance(v, float):
            v = None if math.isnan(v) else round(v, 2)
        out[k] = v
    return out


# ---------------------------------------------------------------- 実データ取得
def jpx_list_url():
    import re
    from urllib.parse import urljoin
    import requests
    html = requests.get(JPX_LIST_PAGE, headers=UA, timeout=30).text
    m = re.search(r'href="([^"]*data_j\.xlsx?)"', html)
    if not m:
        raise RuntimeError(f"銘柄一覧ファイルへのリンクが {JPX_LIST_PAGE} に見つかりません")
    return urljoin(JPX_LIST_PAGE, m.group(1))


def load_universe():
    import io
    import pandas as pd
    import requests
    url = jpx_list_url()
    print(f"universe: {url}")
    res = requests.get(url, headers=UA, timeout=60)
    res.raise_for_status()
    df = pd.read_excel(io.BytesIO(res.content), dtype={"コード": str})
    df = df[df["市場・商品区分"].astype(str).apply(lambda m: any(x in m for x in MARKETS))]
    df = df[~df["市場・商品区分"].astype(str).str.contains("外国")]
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "c": str(r["コード"]).strip(),
            "n": str(r["銘柄名"]).strip(),
            "sec": str(r["33業種区分"]).strip(),
            "mkt": str(r["市場・商品区分"]).replace("（内国株式）", "").strip(),
        })
    return rows


def fetch_prices(codes):
    """1年分の日足をまとめて取得し、価格系指標を計算する"""
    import yfinance as yf
    tickers = [f"{c}.T" for c in codes]
    out = {}
    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        df = yf.download(chunk, period="1y", interval="1d", group_by="ticker",
                         auto_adjust=True, threads=True, progress=False)
        for t in chunk:
            try:
                h = df[t].dropna(subset=["Close"])
            except KeyError:
                continue
            if len(h) < 30:
                continue
            close = h["Close"]
            last = float(close.iloc[-1])
            hi, lo = float(h["High"].max()), float(h["Low"].min())
            def ret(n):
                return (last / float(close.iloc[-n - 1]) - 1) * 100 if len(close) > n else None
            out[t[:-2]] = {
                "p": last,
                "chg": ret(1),
                "r3m": ret(63),
                "r6m": ret(126),
                "pos52": (last - lo) / (hi - lo) * 100 if hi > lo else None,
                "to": float((h["Close"] * h["Volume"]).tail(20).mean()) / 1e8,  # 億円
                "date": h.index[-1].strftime("%Y-%m-%d"),
            }
        time.sleep(1)
    return out


def fetch_fundamental(code):
    import yfinance as yf
    for attempt in range(3):
        try:
            info = yf.Ticker(f"{code}.T").info
            pct = lambda v: v * 100 if ok(v) else None
            return {
                "per": info.get("trailingPE"),
                "pbr": info.get("priceToBook"),
                "div_rate": info.get("trailingAnnualDividendRate") or info.get("dividendRate"),
                "roe": pct(info.get("returnOnEquity")),
                "opm": pct(info.get("operatingMargins")),
                "rg": pct(info.get("revenueGrowth")),
                "eg": pct(info.get("earningsGrowth")),
                "de": info.get("debtToEquity"),
                "shares": info.get("sharesOutstanding"),
                "mc_raw": info.get("marketCap"),
                "ts": time.time(),
            }
        except Exception as e:  # レート制限など
            time.sleep(5 * (attempt + 1))
            err = e
    print(f"  fundamentals failed {code}: {err}", file=sys.stderr)
    return None


def refresh_fundamentals(codes):
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    now = time.time()
    stale = [c for c in codes if now - cache.get(c, {}).get("ts", 0) > FUND_MAX_AGE_DAYS * 86400]
    stale.sort(key=lambda c: cache.get(c, {}).get("ts", 0))  # 古い順
    todo = stale[:FUND_REFRESH_PER_RUN]
    print(f"fundamentals: {len(todo)} to refresh / {len(stale)} stale / {len(codes)} total")
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(fetch_fundamental, c): c for c in todo}
        for f in as_completed(futs):
            r = f.result()
            if r:
                cache[futs[f]] = r
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, separators=(",", ":")))
    return cache


def build_real():
    universe = load_universe()
    codes = [u["c"] for u in universe]
    prices = fetch_prices(codes)
    funds = refresh_fundamentals([c for c in codes if c in prices])
    stocks, dates = [], []
    for u in universe:
        pr = prices.get(u["c"])
        if not pr:
            continue
        f = funds.get(u["c"], {})
        s = {**u, **{k: v for k, v in pr.items() if k != "date"}}
        dates.append(pr["date"])
        for k in ("per", "pbr", "roe", "opm", "rg", "eg", "de"):
            s[k] = f.get(k)
        if ok(f.get("div_rate")) and s["p"]:
            s["dy"] = f["div_rate"] / s["p"] * 100
        if ok(f.get("shares")):
            s["mc"] = f["shares"] * s["p"] / 1e8
        elif ok(f.get("mc_raw")):
            s["mc"] = f["mc_raw"] / 1e8
        stocks.append(s)
    asof = max(set(dates), key=dates.count)  # 最も多い株価基準日
    write_output(stocks, asof, demo=False, universe=len(universe))


# ---------------------------------------------------------------- デモデータ
def build_demo(seed=None, asof=None):
    """ネットにつながらない環境での動作確認用。値はすべて乱数"""
    rnd = random.Random(seed)
    day = random.Random(f"{seed}-{asof}")  # 日ごとの値動き
    sectors = ["電気機器", "情報・通信業", "機械", "化学", "銀行業", "小売業", "サービス業",
               "輸送用機器", "卸売業", "建設業", "医薬品", "不動産業", "食料品", "陸運業"]
    stocks = []
    for i in range(1, 801):
        mc = math.exp(rnd.gauss(5.6, 1.5))  # 億円
        roe = rnd.gauss(8, 7)
        pbr = 0.15 + math.exp(rnd.gauss(0, 0.7))
        per = pbr / (roe / 100) if roe > 0.5 else -rnd.uniform(5, 50)
        rg = rnd.gauss(6, 12)
        stocks.append({
            "c": f"D{i:03d}",
            "n": f"デモ銘柄{i:03d}",
            "sec": rnd.choice(sectors),
            "mkt": rnd.choices(["プライム", "スタンダード", "グロース"], [5, 3, 2])[0],
            "p": round(math.exp(rnd.gauss(7.3, 0.9))),
            "chg": day.gauss(0.1, 1.8),
            "mc": mc,
            "per": per,
            "pbr": pbr,
            "dy": max(0.0, rnd.gauss(2.6, 1.4)),
            "roe": roe,
            "opm": rnd.gauss(9, 7),
            "rg": rg,
            "eg": rg * 1.4 + rnd.gauss(0, 15),
            "de": abs(rnd.gauss(60, 50)),
            "r3m": rnd.gauss(3, 14) + day.gauss(0, 2),
            "r6m": rnd.gauss(5, 20),
            "pos52": min(100, max(0, rnd.uniform(0, 100) + day.gauss(0, 4))),
            "to": mc * rnd.uniform(0.001, 0.01),
        })
    asof = asof or dt.datetime.now(JST).strftime("%Y-%m-%d")
    write_output(stocks, asof, demo=True, universe=len(stocks))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="乱数のデモデータで出力する")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--asof", default=None, help="デモの基準日 (YYYY-MM-DD)")
    args = ap.parse_args()
    build_demo(args.seed, args.asof) if args.demo else build_real()
