"""市場環境 (強気・中立・弱気) の判定。設計書 STEP1。

4つの指標をそれぞれ +1 (強気) / 0 / -1 (弱気) で採点し、合計 +2 以上を強気、
-2 以下を弱気、その間を中立とする。数値はすべて初期値で、検証で見直す。

| 指標       | +1                            | -1                 |
|------------|-------------------------------|--------------------|
| 日経平均   | 50日線と200日線の上           | 200日線の下        |
| TOPIX      | 50日線が上向き (10日前より上) | 50日線が下向き     |
| 騰落銘柄数 | 25日騰落レシオ 110% 以上      | 90% 以下           |
| 売買代金   | 上昇日の平均が下落日の1.1倍以上 | 0.9倍以下        |

TOPIX は TOPIX連動ETF (1306) の終値で代用する。売買代金は全銘柄の合計で、
上昇日・下落日は TOPIX の前日比で分ける (直近20営業日)。
"""
from __future__ import annotations

import math

import pandas as pd

STATES = {1: "強気", 0: "中立", -1: "弱気"}
INVEST = {1: 100, 0: 60, -1: 30}  # 投資資金の比率 (%)。弱気は総合80点以上だけ新規買い
P = {
    "nk_fast": 50, "nk_slow": 200,
    "tp_ma": 50, "tp_slope": 10,
    "adr_days": 25, "adr_hi": 110.0, "adr_lo": 90.0,
    "vol_days": 20, "vol_hi": 1.1, "vol_lo": 0.9,
    "state_hi": 2, "state_lo": -2,
}
HISTORY_DAYS = 250


def _num(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


def nikkei_frame(close: pd.Series) -> pd.DataFrame:
    fast = close.rolling(P["nk_fast"]).mean()
    slow = close.rolling(P["nk_slow"]).mean()
    pt = pd.Series(0, index=close.index, dtype=float)
    pt[(close > fast) & (close > slow)] = 1
    pt[close < slow] = -1
    pt[slow.isna()] = float("nan")
    return pd.DataFrame({"nk": close, "nk_ma50": fast, "nk_ma200": slow, "nk_pt": pt})


def topix_frame(close: pd.Series) -> pd.DataFrame:
    ma = close.rolling(P["tp_ma"]).mean()
    slope = (ma / ma.shift(P["tp_slope"]) - 1) * 100
    pt = pd.Series(0, index=close.index, dtype=float)
    pt[slope > 0] = 1
    pt[slope < 0] = -1
    pt[slope.isna()] = float("nan")
    return pd.DataFrame({"tp": close, "tp_ma50": ma, "tp_slope": slope, "tp_pt": pt})


def breadth_frame(closes: pd.DataFrame) -> pd.DataFrame:
    """closes: 列=銘柄、行=日付の終値。25日騰落レシオ (値上がり数÷値下がり数) を出す"""
    chg = closes.pct_change(fill_method=None)
    adv = (chg > 0).sum(axis=1).astype(float)
    dec = (chg < 0).sum(axis=1).astype(float)
    n = P["adr_days"]
    adr = adv.rolling(n).sum() / dec.rolling(n).sum().replace(0, float("nan")) * 100
    pt = pd.Series(0, index=closes.index, dtype=float)
    pt[adr >= P["adr_hi"]] = 1
    pt[adr <= P["adr_lo"]] = -1
    pt[adr.isna()] = float("nan")
    return pd.DataFrame({"adv": adv, "dec": dec, "adr": adr, "adr_pt": pt})


def volume_frame(turnover: pd.Series, topix_close: pd.Series) -> pd.DataFrame:
    """turnover: 全銘柄合計の売買代金。TOPIX の上げ下げで分けて平均を比べる"""
    df = pd.DataFrame({"to": turnover, "tpc": topix_close.pct_change()}).dropna(subset=["to"])
    n = P["vol_days"]
    up = df["to"].where(df["tpc"] > 0)
    dn = df["to"].where(df["tpc"] < 0)
    up_m = up.rolling(n, min_periods=3).mean()
    dn_m = dn.rolling(n, min_periods=3).mean()
    ratio = up_m / dn_m
    ok = df["tpc"].rolling(n).count() >= n
    ratio = ratio.where(ok)
    pt = pd.Series(0, index=df.index, dtype=float)
    pt[ratio >= P["vol_hi"]] = 1
    pt[ratio <= P["vol_lo"]] = -1
    pt[ratio.isna()] = float("nan")
    return pd.DataFrame({"mkt_to": df["to"], "vol_ratio": ratio, "vol_pt": pt})


def state_of(score: float) -> int:
    return 1 if score >= P["state_hi"] else -1 if score <= P["state_lo"] else 0


def compute(nk_close: pd.Series, tp_close: pd.Series, closes: pd.DataFrame, turnover: pd.Series) -> dict | None:
    """各系列は日付 (DatetimeIndex か 'YYYY-MM-DD') を index に持つ。判定結果の辞書を返す"""
    def norm(x):
        x = x.copy()
        x.index = pd.to_datetime(x.index).strftime("%Y-%m-%d")
        return x[~x.index.duplicated(keep="last")].sort_index()
    nk_close, tp_close, closes, turnover = norm(nk_close.dropna()), norm(tp_close.dropna()), norm(closes), norm(turnover)
    df = (nikkei_frame(nk_close)
          .join(topix_frame(tp_close), how="inner")
          .join(breadth_frame(closes), how="left")
          .join(volume_frame(turnover, tp_close), how="left"))
    pts = ["nk_pt", "tp_pt", "adr_pt", "vol_pt"]
    df = df.dropna(subset=["nk_pt", "tp_pt"])  # 指数がそろわない日は判定しない
    if df.empty:
        return None
    df["score"] = df[pts].fillna(0).sum(axis=1)
    df["known"] = df[pts].notna().sum(axis=1)
    df["state"] = df["score"].map(state_of)
    last = df.iloc[-1]
    date = df.index[-1]
    st = int(last["state"])
    # 同じ判定が何営業日続いているか
    run = 0
    for v in reversed(df["state"].tolist()):
        if v != st:
            break
        run += 1

    def pt(k):
        v = _num(last[k])
        return None if v is None else int(v)

    ind = [
        {"key": "nk", "name": "日経平均", "pt": pt("nk_pt"),
         "value": _num(last["nk"]), "ma50": _num(last["nk_ma50"]), "ma200": _num(last["nk_ma200"]),
         "rule": "強気: 50日線と200日線の上 / 弱気: 200日線の下"},
        {"key": "tp", "name": "TOPIX (1306で代用)", "pt": pt("tp_pt"),
         "value": _num(last["tp"]), "ma50": _num(last["tp_ma50"]), "slope": _num(last["tp_slope"]),
         "rule": f"強気: 50日線が{P['tp_slope']}営業日前より上 / 弱気: 下"},
        {"key": "adr", "name": "騰落銘柄数", "pt": pt("adr_pt"),
         "value": _num(last["adr"]), "adv": _num(last["adv"]), "dec": _num(last["dec"]),
         "rule": f"強気: {P['adr_days']}日騰落レシオ{P['adr_hi']:.0f}%以上 / 弱気: {P['adr_lo']:.0f}%以下"},
        {"key": "vol", "name": "売買代金", "pt": pt("vol_pt"),
         "value": _num(last["vol_ratio"]), "mkt_to": _num(last["mkt_to"]),
         "rule": f"強気: 上昇日の平均が下落日の{P['vol_hi']}倍以上 / 弱気: {P['vol_lo']}倍以下 (直近{P['vol_days']}日)"},
    ]
    hist = df[df["known"] >= 3].tail(HISTORY_DAYS)  # 騰落・売買代金が出せない古い日は除く
    return {
        "date": date,
        "state": st, "label": STATES[st], "score": int(last["score"]), "known": int(last["known"]),
        "invest_pct": INVEST[st], "run_days": run,
        "indicators": ind,
        "params": P, "invest": {STATES[k]: v for k, v in INVEST.items()},
        "history": [[d, int(r["score"]), int(r["state"])] for d, r in hist.iterrows()],
    }
