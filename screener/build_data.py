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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import holdings as hd  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
HIST_DIR = DATA_DIR / "history"
BARS_DIR = DATA_DIR / "bars"
BAR_DAYS = 130  # 詳細チャートに出す日数 (約6ヶ月)
CACHE_FILE = ROOT / "cache" / "fundamentals.json"
JST = dt.timezone(dt.timedelta(hours=9))

# 東証上場銘柄一覧のページ。ファイルのURLは変わることがあるので、毎回ページからリンクを探す
JPX_LIST_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
UA = {"User-Agent": "Mozilla/5.0 (stock-ranking; +https://github.com/digirou-code/stock-ranking)"}
# 対象市場 (カンマ区切りで上書き可)
MARKETS = os.environ.get("MARKETS", "プライム,スタンダード,グロース").split(",")
# 1回の実行で財務指標を取り直す銘柄数の上限 (Yahooのレート制限対策)
FUND_REFRESH_PER_RUN = int(os.environ.get("FUND_REFRESH_PER_RUN", "4000"))
FUND_MAX_AGE_DAYS = int(os.environ.get("FUND_MAX_AGE_DAYS", "7"))
STMT_MAX_AGE_DAYS = 30  # 自己資本比率・利益成長率 (決算書から計算) を取り直す間隔
TOP_N = 50
NO_SELL = 9999  # 信用倍率: 売残0・買残ありの印
MACD_GC_DAYS = 3  # MACDのゴールデンクロスをこの営業日数以内に見る (1 = 当日のみ)

# ---------------------------------------------------------------- 取引ルール (スイング)
# 損切り・利確はエントリー価格からの%、時間切れは営業日数。WebUIの目安価格と決算警告もこれを使う
RULES = {
    "atr_n": 14,          # ATRの期間 (値動きの目安として表示)
    "stop_pct": 6,        # 損切り: エントリー価格から6%下落
    "take1_pct": 20,      # 利確1: +20%で半分売却
    "take2_pct": 40,      # 利確2: +40%で残り半分を売却
    "hold_days": 25,      # 時間切れ: 25営業日経過で手仕舞い
    "max_pos": 10,        # 同時保有数の上限
    "lot": 100,           # 売買単位
}

# ---------------------------------------------------------------- スコア定義
# SCOREは「業績」と「勢い」の2本立て (2026-10-30 ユーザー指定)。各項目を0〜1に換算して平均し、0〜100で表す。
# 項目: (キー, ラベル, 換算方法)。換算方法は "hi"/"lo" = 全銘柄内の順位 (高い方/低い方が良い)、
# それ以外は score_stocks 内の個別の換算 (理想の範囲にどれだけ近いか等)
SCORES = {
    "sf": {
        "name": "SCORE業績",
        "short": "業績",
        "items": [
            ("per", "PER", "per"),
            ("roe", "ROE", "hi"),
            ("er", "自己資本比率", "hi"),
            ("dy", "配当利回り", "hi"),
            ("rg", "売上成長率", "hi"),
            ("egf", "利益成長予想", "hi"),
        ],
        "note": "PERは低いほど高評価 (赤字は最下位)。売上成長率は直近四半期の前年同期比 (予想が無料で取れないため実績)。"
                "利益成長予想はアナリスト予想EPS ÷ 実績EPS、予想がない銘柄は直近年度の純利益の前年比で代用。",
    },
    "sm": {
        "name": "SCORE勢い",
        "short": "勢い",
        "items": [
            ("stage75", "ステージ(75)", "stage"),
            ("pullback", "押し目度", "pullback"),
            ("r3m", "3ヶ月騰落率", "hi"),
            ("to", "売買代金", "hi"),
            ("rsi", "RSI (低め加点)", "rsi"),
        ],
        "note": "ステージ(75)は 2上昇=100 / 1底固め=67 / 3天井圏=33 / 4下降=0。押し目度は25日線乖離 -2〜+2%・20日高値から -3〜-20% にどれだけ近いか。"
                "RSIは40以下で満点、60以上で0。それ以外は全銘柄内の順位。",
    },
}
MIN_COVERAGE = 4  # 項目のうちこの数以上そろった銘柄だけSCOREを出す (欠けた項目は中央の0.5で補う)
STAGE_PTS = {2: 1.0, 1: 2 / 3, 3: 1 / 3, 4: 0.0}


def _pullback_base(s, max_off):
    """好業績押し目・信用過多押し目で共通の条件 (10日高値からの下落は -10%〜max_off%)"""
    return (ok(s.get("rsi")) and 35 <= s["rsi"] <= 50
            and ok(s.get("rsi_min10")) and s["rsi_min10"] <= 35
            and ok(s.get("off10")) and max_off <= s["off10"] <= -10
            and ok(s.get("eg")) and s["eg"] > 15
            and ok(s.get("mc")) and s["mc"] >= 300
            and ok(s.get("to")) and s["to"] >= 1)


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
        "id": "sf",
        "name": "SCORE業績",
        "tab": "業績",
        "desc": "PER・ROE・自己資本比率・配当利回り・売上成長率・利益成長予想の6項目の評価が高い順",
        "cond": [f"6項目中{MIN_COVERAGE}項目以上のデータあり"],
        "filter": lambda s: s.get("sf") is not None,
        "sort": lambda s: s["sf"],
    },
    {
        "id": "sm",
        "name": "SCORE勢い",
        "tab": "勢い",
        "desc": "ステージ(75)・押し目度・3ヶ月騰落率・売買代金・RSIの5項目の評価が高い順",
        "cond": [f"5項目中{MIN_COVERAGE}項目以上のデータあり"],
        "filter": lambda s: s.get("sm") is not None,
        "sort": lambda s: s["sm"],
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
        "id": "high_roe",
        "name": "高ROE",
        "tab": "収益",
        "desc": "自己資本を効率よく利益に変えている銘柄",
        "cond": ["ROE 15%以上", "自己資本比率 30%以上"],
        "filter": lambda s: ok(s.get("roe")) and s["roe"] >= 15
        and (not ok(s.get("er")) or s["er"] >= 30),
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
        "id": "growth_div",
        "name": "成長＋高配当",
        "tab": "配当",
        "desc": "売上と利益が伸びていて、配当利回りも高い銘柄",
        "cond": ["配当利回り 3.5%以上", "売上成長率 10%以上", "利益成長率 10%以上", "ROE 5%以上", "時価総額 300億円以上"],
        "filter": lambda s: ok(s.get("dy")) and s["dy"] >= 3.5
        and ok(s.get("rg")) and s["rg"] >= 10
        and ok(s.get("eg")) and s["eg"] >= 10
        and ok(s.get("roe")) and s["roe"] >= 5
        and ok(s.get("mc")) and s["mc"] >= 300,
        "sort": lambda s: s["dy"],
    },
    # ---- スイング向け (短期〜数週間)。テクニカル指標は fetch_prices で日足から計算
    {
        "id": "pullback",
        "name": "25日線押し目",
        "tab": "押し目",
        "desc": "上昇トレンド中に25日移動平均線まで調整してきた優良株",
        "cond": ["75日線が上向き・株価が75日線より上", "25日線が75日線より上", "25日線乖離 -3%〜+1%",
                 "20日高値から5〜15%下落", "ROE 8%以上", "売買代金 1億円以上"],
        "filter": lambda s: s.get("ma75_up") and s.get("above75") and s.get("ma25_over75")
        and ok(s.get("dev25")) and -3 <= s["dev25"] <= 1
        and ok(s.get("off20")) and -15 <= s["off20"] <= -5
        and ok(s.get("roe")) and s["roe"] >= 8
        and ok(s.get("to")) and s["to"] >= 1,
        "sort": lambda s: s["r3m"] if ok(s.get("r3m")) else -999,
    },
    {
        "id": "rsi_pullback",
        "name": "RSI押し目",
        "tab": "押し目",
        "desc": "長期の上昇トレンド中にRSIが冷え、陽線で反発し始めた銘柄",
        "cond": ["株価が200日線より上", "RSI(14) 35〜45", "前日比プラスかつ陽線",
                 "営業利益率 5%以上", "売買代金 1億円以上"],
        "filter": lambda s: s.get("above200")
        and ok(s.get("rsi")) and 35 <= s["rsi"] <= 45
        and ok(s.get("chg")) and s["chg"] > 0 and s.get("bull")
        and ok(s.get("opm")) and s["opm"] >= 5
        and ok(s.get("to")) and s["to"] >= 1,
        "sort": lambda s: s["r6m"] if ok(s.get("r6m")) else -999,
    },
    {
        "id": "rebound_dev",
        "name": "乖離リバウンド",
        "tab": "リバウンド",
        "desc": "好業績なのに短期間で売られ、25日線から大きく下に離れた銘柄",
        "cond": ["25日線乖離 -10%以下", "売上・利益成長率 0%超", "ROE 8%以上", "PER 0〜20倍",
                 "時価総額 300億円以上", "売買代金 1億円以上"],
        "filter": lambda s: ok(s.get("dev25")) and s["dev25"] <= -10
        and ok(s.get("rg")) and s["rg"] > 0 and ok(s.get("eg")) and s["eg"] > 0
        and ok(s.get("roe")) and s["roe"] >= 8
        and ok(s.get("per")) and 0 < s["per"] <= 20
        and ok(s.get("mc")) and s["mc"] >= 300
        and ok(s.get("to")) and s["to"] >= 1,
        "sort": lambda s: -s["dev25"],
    },
    {
        "id": "rebound_rsi",
        "name": "RSI売られすぎ反発",
        "tab": "リバウンド",
        "desc": "好業績なのにRSI30以下まで売られ、出来高を伴う陽線で反発した銘柄",
        "cond": ["直近5日以内にRSI(14) 30以下", "陽線かつ前日比 +1%以上", "出来高 20日平均の1.2倍以上",
                 "利益成長率 0%超", "ROE 8%以上"],
        "filter": lambda s: ok(s.get("rsi_min5")) and s["rsi_min5"] <= 30
        and s.get("bull") and ok(s.get("chg")) and s["chg"] >= 1
        and ok(s.get("vr")) and s["vr"] >= 1.2
        and ok(s.get("eg")) and s["eg"] > 0
        and ok(s.get("roe")) and s["roe"] >= 8,
        "sort": lambda s: s["chg"],
    },
    {
        "id": "vol_break",
        "name": "出来高急増",
        "tab": "初動",
        "desc": "出来高を伴って大きく上げ、高値引けに近い銘柄 (上昇初動の兆し)",
        "cond": ["出来高 20日平均の2倍以上", "前日比 +3%以上", "終値が当日高値から1%以内",
                 "株価が25日線より上", "当日売買代金 3億円以上"],
        "filter": lambda s: ok(s.get("vr")) and s["vr"] >= 2
        and ok(s.get("chg")) and s["chg"] >= 3
        and ok(s.get("near_hi")) and s["near_hi"] >= -1
        and ok(s.get("dev25")) and s["dev25"] > 0
        and ok(s.get("to1")) and s["to1"] >= 3,
        "sort": lambda s: s["vr"],
    },
    {
        "id": "high_break",
        "name": "高値ブレイク",
        "tab": "初動",
        "desc": "終値で直近60日の高値を更新し、出来高も増えた銘柄",
        "cond": ["終値が直近60日の最高値を更新", "出来高 20日平均の1.5倍以上", "25日線が75日線より上",
                 "売買代金 1億円以上"],
        "filter": lambda s: s.get("hi60_break")
        and ok(s.get("vr")) and s["vr"] >= 1.5
        and s.get("ma25_over75")
        and ok(s.get("to")) and s["to"] >= 1,
        "sort": lambda s: s["vr"],
    },
    {
        "id": "macd_rsi",
        "name": "MACD・RSI",
        "tab": "初動",
        "desc": "MACDがシグナルを上抜け、RSIも50以上で上向きの勢いが出てきた好業績株",
        "cond": [f"MACD(12,26,9)が当日を含む直近{MACD_GC_DAYS}営業日以内にゴールデンクロス (今もシグナルより上)", "RSI(14) 50以上",
                 "利益成長率 15%超", "時価総額 300億円以上"],
        "filter": lambda s: s.get("macd_gc") is not None
        and ok(s.get("rsi")) and s["rsi"] >= 50
        and ok(s.get("eg")) and s["eg"] > 15
        and ok(s.get("mc")) and s["mc"] >= 300,
        "sort": lambda s: (-s["macd_gc"], s["eg"]),  # クロスが新しい順、同じ日なら利益成長率の高い順
    },
    {
        "id": "eg_pullback",
        "name": "好業績押し目",
        "tab": "押し目",
        "desc": "好業績株が直近で売られすぎ、陽線で反発し始めたところ",
        "cond": ["陽線かつ前日比 +2%以上", "RSI(14) 35〜50", "直近10日以内にRSI(14) 35以下", "10日高値から10〜20%下落",
                 "利益成長率 15%超", "時価総額 300億円以上", "売買代金 1億円以上"],
        "filter": lambda s: s.get("bull") and ok(s.get("chg")) and s["chg"] >= 2
        and _pullback_base(s, -20),
        "sort": lambda s: s["eg"],
    },
    {
        "id": "margin_pullback",
        "name": "信用過多押し目",
        "tab": "押し目",
        "desc": "信用買いが積み上がった好業績株が大きく調整し、売られすぎ圏から戻りかけたところ",
        "cond": ["信用倍率 10倍以上 (売残0で買残ありも含む。JPX公表の銘柄別信用残)", "RSI(14) 35〜50", "直近10日以内にRSI(14) 35以下",
                 "10日高値から10〜30%下落", "利益成長率 15%超", "時価総額 300億円以上", "売買代金 1億円以上"],
        "filter": lambda s: ok(s.get("mratio")) and s["mratio"] >= 10
        and _pullback_base(s, -30),
        "sort": lambda s: s["eg"],
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


def _band(x, lo, hi, w):
    """lo〜hi なら1、範囲から w 離れると0になる台形"""
    if not ok(x):
        return None
    return min(1.0, max(0.0, 1 - max(lo - x, x - hi, 0) / w))


def score_inputs(s):
    """SCOREの項目のうち、取得した値から作るもの"""
    eg = s.get("eg")
    ef, et = s.get("eps_fwd"), s.get("eps_trail")
    s["egf"] = (ef / et - 1) * 100 if ok(ef) and ok(et) and et > 0 else eg
    dev, off = _band(s.get("dev25"), -2, 2, 5), _band(s.get("off20"), -20, -3, 5)
    s["pullback"] = (dev + off) / 2 if dev is not None and off is not None else None


def score_stocks(stocks):
    for s in stocks:
        score_inputs(s)
    for sid, sd in SCORES.items():
        cols = {}
        for key, _, how in sd["items"]:
            vals = [s.get(key) for s in stocks]
            if how == "per":  # 赤字(負のPER)は最下位扱い
                cols[key] = percentiles([v if ok(v) and v > 0 else (9999 if ok(v) else None) for v in vals], False)
            elif how in ("hi", "lo"):
                cols[key] = percentiles(vals, how == "hi")
            elif how == "stage":
                cols[key] = [STAGE_PTS.get(v.get("stage")) if v else None for v in vals]
            elif how == "rsi":
                cols[key] = [min(1.0, max(0.0, (60 - v) / 20)) if ok(v) else None for v in vals]
            else:  # すでに0〜1の値 (押し目度)
                cols[key] = vals
        for i, s in enumerate(stocks):
            items = {k: cols[k][i] for k, *_ in sd["items"]}
            s.setdefault("items", {}).update({k: (round(v * 100) if v is not None else None) for k, v in items.items()})
            have = [v for v in items.values() if v is not None]
            if len(have) >= MIN_COVERAGE:
                filled = [v if v is not None else 0.5 for v in items.values()]
                s[sid] = round(sum(filled) / len(filled) * 100, 1)
            else:
                s[sid] = None


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


def jp_business_days(start: dt.date, n: int):
    """start の翌営業日から n 営業日分の日付 (土日・祝日・年末年始を除く)"""
    try:
        import jpholiday
        holiday = jpholiday.is_holiday
    except ImportError:  # 祝日判定なしでも土日だけは除く
        holiday = lambda d: False
    out, d = [], start
    while len(out) < n:
        d += dt.timedelta(days=1)
        if d.weekday() < 5 and not holiday(d) and not (d.month == 12 and d.day == 31) \
                and not (d.month == 1 and d.day <= 3):
            out.append(d)
    return out


def add_trade_plan(stocks, asof: str, earnings):
    """決算発表日を付け、保有想定期間 (翌営業日のエントリーから hold_days 営業日) に入るか調べる"""
    today = dt.date.fromisoformat(asof)
    days = jp_business_days(today, RULES["hold_days"] + 1)  # [0]がエントリー日、[-1]が時間切れの日
    for s in stocks:
        e = hd.next_earnings(s["c"], earnings, s.get("earn_ts"), today)
        if not e:
            continue
        d = dt.date.fromisoformat(e["date"])
        s["earn"] = e["date"]
        if today < d <= days[-1]:
            s["earn_bd"] = next(i for i, x in enumerate(days, 1) if x >= d)  # 何営業日目か
    return {"entry": days[0].isoformat(), "exit_by": days[-1].isoformat()}


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


def write_output(stocks, asof: str, demo: bool, universe: int, earnings=None):
    score_stocks(stocks)
    plan = add_trade_plan(stocks, asof, earnings or {})
    prev_ranks, prev_asof = load_prev_ranks(asof)
    screens = run_screens(stocks, prev_ranks)
    used = {row["c"] for sc in screens for row in sc["rows"]}
    out = {
        "asof": asof,
        "prev_asof": prev_asof,
        "generated_at": dt.datetime.now(JST).isoformat(timespec="minutes"),
        "demo": demo,
        "universe": universe,
        "scored": sum(1 for s in stocks if s.get("sf") is not None or s.get("sm") is not None),
        "scores": {sid: {"name": sd["name"], "short": sd["short"], "note": sd["note"],
                         "items": [{"k": k, "label": l} for k, l, _ in sd["items"]]} for sid, sd in SCORES.items()},
        "rules": {**RULES, **plan},
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
    return screens


def compact(s):
    keys = ["c", "n", "sec", "mkt", "p", "chg", "mc", "per", "pbr", "dy", "roe",
            "opm", "rg", "eg", "er", "r1m", "r3m", "r6m", "pos52", "to", "sf", "sm", "items", "egf",
            "stage", "stage75", "stage25", "dev25", "rsi", "vr", "vol", "vchg", "off10", "off20", "mratio", "atr", "earn", "earn_bd"]
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
    every = {str(r["コード"]).strip(): {"n": str(r["銘柄名"]).strip(),
                                    "sec": str(r["33業種区分"]).strip().replace("-", "") or "ETF等",
                                    "mkt": str(r["市場・商品区分"]).replace("（内国株式）", "").strip()}
             for _, r in df.iterrows()}
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
    return rows, every


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
            pos52 = (last - lo) / (hi - lo) * 100 if hi > lo else None
            out[t[:-2]] = {
                **technicals(h),
                "stage": hd.stage_of(close, pos52),
                "stage75": hd.stage_of(close, pos52, 75),
                "stage25": hd.stage_of(close, pos52, 25),
                "p": last,
                "chg": ret(1),
                "r1m": ret(21),
                "r3m": ret(63),
                "r6m": ret(126),
                "pos52": pos52,
                "to": float((h["Close"] * h["Volume"]).tail(20).mean()) / 1e8,  # 億円
                "date": h.index[-1].strftime("%Y-%m-%d"),
                "_close": close,  # セクター平均の計算用 (出力しない)
                "bars": [[i.strftime("%Y-%m-%d"), *(round(float(r[k]), 1) for k in ("Open", "High", "Low", "Close")),
                          int(r["Volume"]) if ok(float(r["Volume"])) else 0]
                         for i, r in h.tail(BAR_DAYS).iterrows()],
            }
        time.sleep(1)
    return out


def write_sectors(prices, universe):
    """33業種ごとの等ウェイト平均株価指数 (初日=100) を docs/data/sectors.json に書く"""
    import pandas as pd
    sec_of = {u["c"]: u["sec"] for u in universe if u.get("sec")}
    rets = {}
    for c, pr in prices.items():
        if c in sec_of and "_close" in pr:
            r = pr["_close"].pct_change()
            r.index = r.index.strftime("%Y-%m-%d")
            rets[c] = r.clip(-0.3, 0.3)  # 分割の取り漏れなど異常値を抑える
    df = pd.DataFrame(rets).sort_index()
    df = df[df.count(axis=1) >= df.count(axis=1).max() * 0.5]  # 銘柄がそろっていない日は除く
    series, counts = {}, {}
    for sec in sorted(set(sec_of.values())):
        cols = [c for c in df.columns if sec_of.get(c) == sec]
        if len(cols) < 3:
            continue
        idx = (1 + df[cols].mean(axis=1, skipna=True).fillna(0)).cumprod() * 100
        idx.iloc[0] = 100.0
        series[sec] = [round(float(v), 2) for v in idx.values]
        counts[sec] = len(cols)
    out = {"dates": list(df.index), "series": series, "counts": counts}
    (DATA_DIR / "sectors.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"sectors: {len(series)} sectors, {len(df)} days")


def write_bars(prices, codes):
    """詳細画面のチャート用に、銘柄ごとの日足を docs/data/bars/<コード>.json に書く"""
    BARS_DIR.mkdir(parents=True, exist_ok=True)
    keep = set()
    for c in codes:
        pr = prices.get(c)
        if not pr:
            continue
        (BARS_DIR / f"{c}.json").write_text(json.dumps({"c": c, "bars": pr["bars"]}, separators=(",", ":")))
        keep.add(f"{c}.json")
    for p in BARS_DIR.glob("*.json"):  # ランキングから外れた銘柄は消す
        if p.name not in keep:
            p.unlink()
    print(f"bars: {len(keep)} files")


def rsi_series(close, n=14):
    """RSI (Wilder の平滑化)"""
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.where(dn > 0))


def technicals(h):
    """スイング向けスクリーニングで使う指標を日足 (Open/High/Low/Close/Volume) から計算する"""
    import pandas as pd
    close, vol = h["Close"], h["Volume"]
    last = float(close.iloc[-1])
    f = lambda v: float(v) if ok(float(v)) else None
    ma25 = close.rolling(25).mean()
    ma75 = close.rolling(75).mean()
    t = {}
    if len(close) >= 25:
        t["dev25"] = (last / float(ma25.iloc[-1]) - 1) * 100
    if len(close) >= 95:
        t["ma75_up"] = bool(ma75.iloc[-1] > ma75.iloc[-21])
        t["above75"] = bool(last > ma75.iloc[-1])
        t["ma25_over75"] = bool(ma25.iloc[-1] > ma75.iloc[-1])
    if len(close) >= 200:
        t["above200"] = bool(last > close.tail(200).mean())
    # ATR (Wilder の平滑化)。損切り・利確の幅と株数目安に使う
    prev = close.shift(1)
    tr = pd.concat([h["High"] - h["Low"], (h["High"] - prev).abs(), (h["Low"] - prev).abs()], axis=1).max(axis=1)
    if len(close) > RULES["atr_n"]:
        t["atr"] = f(tr.ewm(alpha=1 / RULES["atr_n"], adjust=False).mean().iloc[-1])
    # MACD(12,26,9)。macd_gc = シグナルを上抜けてから何営業日前か (0 = 当日)。上抜け中でなければ None
    if len(close) >= 60:
        macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
        above = (macd > macd.ewm(span=9, adjust=False).mean()).tolist()
        if above[-1]:
            k = next((i for i in range(MACD_GC_DAYS) if not above[-2 - i]), None)  # 遡って最初に下だった日
            t["macd_gc"] = k
    rsi = rsi_series(close)
    if len(close) >= 30:
        t["rsi"] = f(rsi.iloc[-1])
        t["rsi_min5"] = f(rsi.tail(5).min())
        t["rsi_min10"] = f(rsi.tail(10).min())
    hi10 = float(h["High"].tail(10).max())
    t["off10"] = (last / hi10 - 1) * 100 if hi10 > 0 else None
    hi20 = float(h["High"].tail(20).max())
    t["off20"] = (last / hi20 - 1) * 100 if hi20 > 0 else None
    avg_vol = float(vol.iloc[-21:-1].mean())
    t["vr"] = float(vol.iloc[-1]) / avg_vol if avg_vol > 0 else None
    t["to1"] = last * float(vol.iloc[-1]) / 1e8  # 当日売買代金 (億円)
    t["vol"] = f(vol.iloc[-1])
    if len(vol) >= 2 and float(vol.iloc[-2]) > 0:
        t["vchg"] = (float(vol.iloc[-1]) / float(vol.iloc[-2]) - 1) * 100  # 出来高の前日比 (%)
    day_hi = float(h["High"].iloc[-1])
    t["near_hi"] = (last / day_hi - 1) * 100 if day_hi > 0 else None
    t["bull"] = bool(last > float(h["Open"].iloc[-1]))
    if len(close) >= 61:
        t["hi60_break"] = bool(last > float(close.iloc[-61:-1].max()))
    return t


def fetch_fundamental(code, prev=None):
    import yfinance as yf
    prev = prev or {}
    for attempt in range(3):
        try:
            t = yf.Ticker(f"{code}.T")
            info = t.info
            # レート制限中は例外にならず中身の乏しい info が返るので、取り直す
            if not any(info.get(k) for k in ("sharesOutstanding", "marketCap", "priceToBook")):
                raise ValueError("empty info")
            # 決算書 (自己資本比率・利益成長率) は四半期ごとにしか変わらないので、30日は前回の値を使う
            if time.time() - prev.get("stmt_ts", 0) < STMT_MAX_AGE_DAYS * 86400:
                stmt = {k: prev.get(k) for k in ("eg", "eg_fy", "er", "stmt_ts")}
            else:
                stmt = {**earnings_growth(t), "er": equity_ratio(t), "stmt_ts": time.time()}
            pct = lambda v: v * 100 if ok(v) else None
            return {
                "per": info.get("trailingPE"),
                "pbr": info.get("priceToBook"),
                # 日本株の配当は Yahoo の各項目がそれぞれ外れることがあるので、
                # 複数を取っておき dividend_yield() で突き合わせる
                "div_fwd": info.get("dividendRate"),
                "last_div_val": info.get("lastDividendValue"),
                "div_trail": info.get("trailingAnnualDividendRate"),
                "div_yield": info.get("dividendYield"),  # % 表記 (3.96 = 3.96%)
                "last_div_ts": info.get("lastDividendDate"),
                "split_ts": info.get("lastSplitDate"),
                "roe": pct(info.get("returnOnEquity")),
                "opm": pct(info.get("operatingMargins")),
                "rg": pct(info.get("revenueGrowth")),
                # earningsGrowth は四半期の前年同期比で、前年が小さいと数千%に跳ねるので使わない
                **stmt,
                "shares": info.get("sharesOutstanding"),
                "mc_raw": info.get("marketCap"),
                "eps_fwd": info.get("forwardEps"),  # アナリスト予想EPS (利益成長予想に使う)
                "eps_trail": info.get("trailingEps"),
                "earn_ts": info.get("earningsTimestamp") or info.get("earningsTimestampStart"),
                "ts": time.time(),
            }
        except Exception as e:  # レート制限など
            time.sleep(10 * (attempt + 1))
            err = e
    print(f"  fundamentals failed {code}: {err}", file=sys.stderr)
    return None


def earnings_growth(t):
    """利益成長率 (%) = 直近年度の純利益の前年比。前年が赤字・ゼロなら出さない"""
    try:
        df = t.income_stmt
        row = next(df.loc[k] for k in ("Net Income Common Stockholders", "Net Income") if k in df.index)
        row = row.sort_index(ascending=False)
        cur, prev = float(row.iloc[0]), float(row.iloc[1])
        fy = row.index[0].strftime("%Y-%m")
    except Exception:
        return {"eg": None, "eg_fy": None}
    if not ok(cur) or not ok(prev) or prev <= 0:
        return {"eg": None, "eg_fy": fy}
    return {"eg": (cur / prev - 1) * 100, "eg_fy": fy}


def equity_ratio(t):
    """自己資本比率 (%) = 株主資本 / 総資産。直近四半期、なければ直近年度の貸借対照表から"""
    for bs in ("quarterly_balance_sheet", "balance_sheet"):
        try:
            df = getattr(t, bs)
            eq = next(df.loc[k] for k in ("Stockholders Equity", "Common Stock Equity") if k in df.index)
            col = next(c for c in df.columns if ok(eq[c]) and ok(df.loc["Total Assets", c]))
            ta = float(df.loc["Total Assets", col])
            if ta > 0:
                return float(eq[col]) / ta * 100
        except Exception:
            continue
    return None


def dividend_yield(f, price):
    """配当利回り (%)。Yahoo の配当は項目ごとに外れ方が違うため、次の候補のうち
    いちばん小さい年間配当を使う (高すぎる利回りで上位に出ないよう控えめに見積もる)。
      - 予想配当 dividendRate (分割直後や特別配当で数倍になることがある)
      - 直近1回の配当 × 2 (年2回配当とみなす)
      - 実績配当 trailingAnnualDividendRate (直近1年に分割があると分割前の額のまま)
    """
    if not price or "last_div_val" not in f:  # 古い形式のキャッシュは次の取り直しまで出さない
        return None
    now, year = time.time(), 400 * 86400
    if not f.get("last_div_ts") or now - f["last_div_ts"] > year:
        return 0.0 if f.get("last_div_ts") or f.get("div_yield") == 0 else None  # 1年以上配当がない
    cands = [f.get("div_fwd")]
    if ok(f.get("last_div_val")):
        cands.append(f["last_div_val"] * 2)
    if now - (f.get("split_ts") or 0) > year:
        cands.append(f.get("div_trail"))
    cands = [v for v in cands if ok(v) and v > 0]
    return min(cands) / price * 100 if cands else None


def refresh_fundamentals(codes):
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    now = time.time()
    stale = [c for c in codes if now - cache.get(c, {}).get("ts", 0) > FUND_MAX_AGE_DAYS * 86400
             or "last_div_val" not in cache.get(c, {})  # 配当の取り方を変える前の古い形式も取り直す
             or "er" not in cache.get(c, {})  # 自己資本比率を追加する前の形式も取り直す
             or "eg_fy" not in cache.get(c, {})  # 利益成長率を年次決算から計算する前の形式も取り直す
             or not (cache.get(c, {}).get("shares") or cache.get(c, {}).get("mc_raw"))]  # レート制限で空だったもの
    stale.sort(key=lambda c: cache.get(c, {}).get("ts", 0))  # 古い順
    todo = stale[:FUND_REFRESH_PER_RUN]
    print(f"fundamentals: {len(todo)} to refresh / {len(stale)} stale / {len(codes)} total")
    with ThreadPoolExecutor(max_workers=2) as ex:  # 4並列だと Yahoo のレート制限にかかる
        futs = {ex.submit(fetch_fundamental, c, cache.get(c)): c for c in todo}
        for f in as_completed(futs):
            r = f.result()
            if r:
                cache[futs[f]] = r
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, separators=(",", ":")))
    return cache


def build_real():
    universe, every = load_universe()
    holdings, funds = hd.load_holdings()
    codes = [u["c"] for u in universe]
    extra = [h["c"] for h in holdings if h["c"] not in set(codes)]  # ETFなど対象市場外の保有銘柄
    prices = fetch_prices(codes + extra)
    funds_cache = refresh_fundamentals([c for c in codes + extra if c in prices])
    stocks, dates, by_code = [], [], {}
    for u in universe + [{"c": c, **every.get(c, {"n": c, "sec": "", "mkt": ""})} for c in extra]:
        pr = prices.get(u["c"])
        if not pr:
            continue
        f = funds_cache.get(u["c"], {})
        s = {**u, **{k: v for k, v in pr.items() if k != "date"}}
        for k in ("per", "pbr", "roe", "opm", "rg", "eg", "er", "eps_fwd", "eps_trail", "earn_ts"):
            s[k] = f.get(k)
        dy = dividend_yield(f, s["p"])
        if dy is not None:
            s["dy"] = dy
        if ok(f.get("shares")):
            s["mc"] = f["shares"] * s["p"] / 1e8
        elif ok(f.get("mc_raw")):
            s["mc"] = f["mc_raw"] / 1e8
        by_code[s["c"]] = s
        if u["c"] in extra:
            continue  # スコアリング対象には含めない
        stocks.append(s)
        dates.append(pr["date"])
    asof = max(set(dates), key=dates.count)  # 最も多い株価基準日
    # 信用残 (JPXの週次PDF) は保有銘柄の表示と「信用過多押し目」の両方で使う
    try:
        margin, margin_date = hd.load_margin(list(by_code))
    except Exception as e:
        print(f"margin failed: {e}")
        margin, margin_date = {}, None
    for c, m in margin.items():
        # 売残0で買残がある銘柄は倍率が出せないので、上限なし (NO_SELL) として扱う
        by_code[c]["mratio"] = m["ratio"] if m["ratio"] is not None else (NO_SELL if m["buy"] > 0 else None)
    try:
        earnings = hd.load_earnings_schedule()
    except Exception as e:
        print(f"earnings schedule failed: {e}")
        earnings = {}
    screens = write_output(stocks, asof, demo=False, universe=len(universe), earnings=earnings)
    if holdings:
        hd.write_holdings(holdings, funds, by_code, screens, asof, earnings, margin, margin_date)
    try:
        write_sectors(prices, universe)
    except Exception as e:
        print(f"sectors failed: {e}")
    write_bars(prices, {row["c"] for sc in screens for row in sc["rows"]} | {h["c"] for h in holdings})


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
            "er": min(90.0, max(3.0, rnd.gauss(50, 18))),
            "r1m": rnd.gauss(1, 7) + day.gauss(0, 2),
            "r3m": rnd.gauss(3, 14) + day.gauss(0, 2),
            "r6m": rnd.gauss(5, 20),
            "pos52": min(100, max(0, rnd.uniform(0, 100) + day.gauss(0, 4))),
            "to": mc * rnd.uniform(0.001, 0.01),
        })
        s = stocks[-1]
        # テクニカル指標 (デモ用の乱数。スイング系スクリーニングにも数件ずつ該当するように)
        dev = day.gauss(0, 6)
        rsi = max(5.0, min(95.0, 50 + dev * 3 + day.gauss(0, 8)))
        s.update({
            "dev25": dev,
            "rsi": rsi,
            "rsi_min5": max(5.0, rsi - abs(day.gauss(0, 8))),
            "rsi_min10": max(5.0, rsi - abs(day.gauss(0, 12))),
            "off10": -abs(day.gauss(0, 9)),
            "mratio": math.exp(rnd.gauss(1.5, 1.2)),
            "macd_gc": day.choice([None] * 6 + [0, 1, 2]),
            "vr": math.exp(day.gauss(0, 0.5)),
            "vol": float(round(math.exp(rnd.gauss(12, 1.5)), -2)),
            "vchg": (math.exp(day.gauss(0, 0.5)) - 1) * 100,
            "stage": {"stage": rnd.randint(1, 4), "n": 150, "ma150": s["p"], "slope": 0.0, "dev": round(dev, 1)},
            "stage75": {"stage": rnd.randint(1, 4), "n": 75, "ma150": s["p"], "slope": 0.0, "dev": round(dev, 1)},
            "stage25": {"stage": rnd.randint(1, 4), "n": 25, "ma150": s["p"], "slope": 0.0, "dev": round(dev, 1)},
            "off20": -abs(day.gauss(0, 7)),
            "near_hi": -abs(day.gauss(0, 1.5)),
            "bull": day.random() < 0.5,
            "ma75_up": s["r3m"] > 0,
            "above75": s["r3m"] > -3,
            "ma25_over75": s["r3m"] > 2,
            "above200": s["r6m"] > 0,
            "hi60_break": s["pos52"] > 92 and day.random() < 0.5,
        })
        s["to1"] = s["to"] * s["vr"]
        s["atr"] = s["p"] * max(0.008, rnd.gauss(0.025, 0.01))
        s["earn_ts"] = time.time() + rnd.uniform(-2, 80) * 86400
    asof = asof or dt.datetime.now(JST).strftime("%Y-%m-%d")
    write_output(stocks, asof, demo=True, universe=len(stocks))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="乱数のデモデータで出力する")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--asof", default=None, help="デモの基準日 (YYYY-MM-DD)")
    args = ap.parse_args()
    build_demo(args.seed, args.asof) if args.demo else build_real()
