"""スイング向けスクリーニング6種のバックテスト。

使い方:
    python backtest/run_backtest.py            # 実データ (JPX銘柄一覧 + Yahoo Finance 日足)
    python backtest/run_backtest.py --demo     # ネット不要の乱数データで動作確認

出力:
    backtest/results/results.json   集計結果 (レポートはここから作る)

前提:
    - シグナルは screener/build_data.py の6条件を日足で毎日判定したもの
    - エントリーはシグナル翌営業日の寄り付き。保有N営業日 = エントリー日を1日目としてN日目の大引けで手仕舞い
    - 「ルール」はユーザー指定の損切り -1.5ATR / 利確 +2.5ATR / 最大10営業日
    - 同じ銘柄は保有中に出た次のシグナルを数えない (重複カウント防止)
    - 対象は現在の上場銘柄のみ (無料データでは上場廃止銘柄の株価が取れないため = 生存者バイアス)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "screener"))
OUT_DIR = ROOT / "backtest" / "results"
FUND_FILE = ROOT / "cache" / "fundamentals.json"
JST = dt.timezone(dt.timedelta(hours=9))

START = os.environ.get("BT_START", "2022-01-01")  # 200日線などの計算期間を含めた取得開始日
HOLDS = [1, 3, 5, 10]
RULE = {"stop_atr": 1.5, "take_atr": 2.5, "max_days": 10}
# 2026-10-29 ユーザー指定: 出来高倍率は全条件で1.5倍、高値突破は直近10日
PARAMS = {"vol_ratio": 1.5, "break_days": 10}
SLOTS = 10  # ルール売買のポートフォリオ試算で同時に持つ銘柄数

# 売買コスト (片道, %)。SBI証券などの国内株手数料0円コースを前提に手数料は0、
# スプレッド+スリッページを直近20日の平均売買代金で段階的に見積もる
COMMISSION = 0.0
COST_TIERS = [(10, 0.10), (3, 0.15), (1, 0.25), (0, 0.40)]  # (売買代金 億円以上, 片道%)
TAX = 20.315  # 譲渡益課税 (%)。年間の損益通算後の利益にかかる



# ---------------------------------------------------------------- データ
def load_prices_real(cache_path: Path | None):
    """JPXの現上場銘柄 (プライム・スタンダード・グロース) の日足を取得"""
    if cache_path and cache_path.exists():
        print(f"prices: cache {cache_path}")
        return pickle.loads(cache_path.read_bytes())
    import yfinance as yf
    import build_data as bd
    universe, _ = bd.load_universe()
    codes = [u["c"] for u in universe]
    names = {u["c"]: u["n"] for u in universe}
    frames = {k: {} for k in ("Open", "High", "Low", "Close", "Volume")}
    tickers = [f"{c}.T" for c in codes]
    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        for attempt in range(3):
            try:
                df = yf.download(chunk, start=START, interval="1d", group_by="ticker",
                                 auto_adjust=True, threads=True, progress=False)
                break
            except Exception as e:
                print(f"  retry {i}: {e}")
                time.sleep(10 * (attempt + 1))
        else:
            continue
        for t in chunk:
            try:
                h = df[t].dropna(subset=["Close"])
            except KeyError:
                continue
            if len(h) < 60:
                continue
            for k in frames:
                frames[k][t[:-2]] = h[k]
        print(f"  {min(i + 200, len(tickers))}/{len(tickers)} tickers, got {len(frames['Close'])}")
        time.sleep(1)
    data = {k: pd.DataFrame(v).sort_index() for k, v in frames.items()}
    data["names"] = names
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(pickle.dumps(data))
    return data


def load_prices_demo(n_stocks=300, n_days=900, seed=1):
    """動作確認用の乱数データ"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=n_days)
    cols = [f"D{i:03d}" for i in range(n_stocks)]
    mkt = rng.normal(0.0003, 0.01, n_days)
    ret = mkt[:, None] + rng.normal(0, 0.02, (n_days, n_stocks))
    close = 1000 * np.exp(np.cumsum(ret, axis=0))
    opn = close * np.exp(rng.normal(0, 0.008, close.shape))
    opn[1:] = close[:-1] * np.exp(rng.normal(0, 0.008, (n_days - 1, n_stocks)))
    high = np.maximum(opn, close) * np.exp(np.abs(rng.normal(0, 0.008, close.shape)))
    low = np.minimum(opn, close) * np.exp(-np.abs(rng.normal(0, 0.008, close.shape)))
    vol = np.exp(rng.normal(12, 0.6, close.shape)) * (1 + 3 * (np.abs(ret) > 0.03))
    mk = lambda a: pd.DataFrame(a, index=idx, columns=cols)
    return {"Open": mk(opn), "High": mk(high), "Low": mk(low), "Close": mk(close),
            "Volume": mk(vol), "names": {c: f"デモ{c}" for c in cols}}


def load_fundamentals(cols, demo):
    """現在の財務指標 (cache/fundamentals.json)。過去時点の値は無料で取れないので現在値で代用する"""
    if demo:
        rng = np.random.default_rng(2)
        return {c: {"roe": rng.normal(9, 6), "opm": rng.normal(8, 6), "rg": rng.normal(5, 10),
                    "eg": rng.normal(5, 20), "per": abs(rng.normal(15, 8)),
                    "shares": 3e7, "mc_raw": None} for c in cols}
    return json.loads(FUND_FILE.read_text()) if FUND_FILE.exists() else {}


# ---------------------------------------------------------------- 指標
def rsi_frame(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.where(dn > 0))


def indicators(D, funds):
    O, H, L, C, V = (D[k] for k in ("Open", "High", "Low", "Close", "Volume"))
    ma25, ma75 = C.rolling(25).mean(), C.rolling(75).mean()
    rsi = rsi_frame(C)
    tv = C * V / 1e8  # 売買代金 (億円)
    vavg = V.shift(1).rolling(20).mean()
    prev = C.shift(1)
    tr = np.fmax(np.fmax(H - L, (H - prev).abs()), (L - prev).abs())
    T = {
        "ma75_up": ma75 > ma75.shift(20),
        "above75": C > ma75,
        "ma25_over75": ma25 > ma75,
        "dev25": (C / ma25 - 1) * 100,
        "above200": C > C.rolling(200).mean(),
        "rsi": rsi,
        "rsi_min5": rsi.rolling(5).min(),
        "off20": (C / H.rolling(20).max() - 1) * 100,
        "vr": V / vavg.where(vavg > 0),
        "to": tv.rolling(20).mean(),
        "to1": tv,
        "near_hi": (C / H - 1) * 100,
        "bull": C > O,
        "chg": (C / prev - 1) * 100,
        "hi_break": C > C.shift(1).rolling(PARAMS["break_days"]).max(),
        "r3m": (C / C.shift(63) - 1) * 100,
        "r6m": (C / C.shift(126) - 1) * 100,
        "atr": tr.ewm(alpha=1 / 14, adjust=False).mean(),
    }
    # 時価総額 = 現在の発行済株式数 × その日の株価 (億円)
    shares = pd.Series({c: (funds.get(c) or {}).get("shares") for c in C.columns}, dtype=float)
    T["mc"] = C * shares / 1e8
    # 財務指標は現在値の定数列 (先読みあり)。PERだけはその日の株価に合わせて換算する
    for k in ("roe", "opm", "rg", "eg"):
        s = pd.Series({c: (funds.get(c) or {}).get(k) for c in C.columns}, dtype=float)
        T[k] = pd.DataFrame(np.broadcast_to(s.values, C.shape), index=C.index, columns=C.columns)
    px_now = pd.Series({c: ((funds.get(c) or {}).get("mc_raw") or np.nan)
                        / ((funds.get(c) or {}).get("shares") or np.nan) for c in C.columns}, dtype=float)
    per_now = pd.Series({c: (funds.get(c) or {}).get("per") for c in C.columns}, dtype=float)
    eps = px_now / per_now  # 現在の1株利益 (近似)
    T["per"] = C / eps.where(eps.notna(), np.nan)
    if px_now.isna().all():  # デモなど株価情報が無いときは現在PERをそのまま使う
        T["per"] = pd.DataFrame(np.broadcast_to(per_now.values, C.shape), index=C.index, columns=C.columns)
    return T


def btw(x, lo, hi):
    return (x >= lo) & (x <= hi)


def conditions(T):
    """各スクリーニングの (価格・出来高のみの条件, 財務条件, 並び順キー)。
    build_data.SCREENS と同じ基準。ただし出来高倍率と高値突破期間は PARAMS で上書き"""
    return {
        "pullback": (
            T["ma75_up"] & T["above75"] & T["ma25_over75"] & btw(T["dev25"], -3, 1)
            & btw(T["off20"], -15, -5) & (T["to"] >= 1),
            T["roe"] >= 8,
            T["r3m"]),
        "rsi_pullback": (
            T["above200"] & btw(T["rsi"], 35, 45) & (T["chg"] > 0) & T["bull"] & (T["to"] >= 1),
            T["opm"] >= 5,
            T["r6m"]),
        "rebound_dev": (
            (T["dev25"] <= -10) & (T["mc"] >= 300) & (T["to"] >= 1),
            (T["rg"] > 0) & (T["eg"] > 0) & (T["roe"] >= 8) & btw(T["per"], 1e-9, 20),
            -T["dev25"]),
        "rebound_rsi": (
            (T["rsi_min5"] <= 30) & T["bull"] & (T["chg"] >= 1) & (T["vr"] >= PARAMS["vol_ratio"]),
            (T["eg"] > 0) & (T["roe"] >= 8),
            T["chg"]),
        "vol_break": (
            (T["vr"] >= PARAMS["vol_ratio"]) & (T["chg"] >= 3) & (T["near_hi"] >= -1) & (T["dev25"] > 0) & (T["to1"] >= 3),
            None,
            T["vr"]),
        "high_break": (
            T["hi_break"] & (T["vr"] >= PARAMS["vol_ratio"]) & T["ma25_over75"] & (T["to"] >= 1),
            None,
            T["vr"]),
    }


COND_NAMES = {
    "pullback": "25日線押し目", "rsi_pullback": "RSI押し目", "rebound_dev": "乖離リバウンド",
    "rebound_rsi": "RSI売られすぎ反発", "vol_break": "出来高急増", "high_break": "高値ブレイク",
}


# ---------------------------------------------------------------- トレード生成
def cost_pct(to):
    """片道コスト (%)"""
    for th, c in COST_TIERS:
        if to >= th:
            return COMMISSION + c
    return COMMISSION + COST_TIERS[-1][1]


class Market:
    def __init__(self, D, T):
        self.idx = D["Close"].index
        self.cols = list(D["Close"].columns)
        self.O, self.H, self.L, self.C = (D[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
        self.V = D["Volume"].to_numpy(float)
        self.atr = T["atr"].to_numpy(float)
        self.to = T["to"].to_numpy(float)
        n = len(self.idx)
        # 売買代金1億円以上の銘柄の単純平均 (同じ日に同じ方法で買った場合の市場平均) = 超過リターンの基準
        liquid = (T["to"] >= 1).to_numpy()
        self.bench = {}
        for N in HOLDS:
            r = np.full(self.C.shape, np.nan)
            if n > N:
                r[:n - N] = self.C[N:] / self.O[1:n - N + 1] - 1
            r = np.where(liquid & np.isfinite(r), np.clip(r, -0.5, 0.5), np.nan)
            with np.errstate(all="ignore"):
                self.bench[N] = np.nanmean(r, axis=1) * 100

    def entry_ok(self, i, j):
        """シグナル日 i の翌日 i+1 に寄り付きで買えるか"""
        e = i + 1
        if e >= len(self.idx):
            return False
        o, h, l, v = self.O[e, j], self.H[e, j], self.L[e, j], self.V[e, j]
        if not (np.isfinite(o) and o > 0 and v > 0):
            return False
        if h == l:  # 1本値 = ストップ高/安張り付きで約定できない可能性が高い
            return False
        return True

    def fixed_exit(self, i, j, N):
        e, x = i + 1, i + N
        if x >= len(self.idx) or not np.isfinite(self.C[x, j]):
            return None
        return (self.C[x, j] / self.O[e, j] - 1) * 100, x

    def rule_exit(self, i, j):
        """損切り / 利確 (ATR倍数) / 最大保有日数。同じ日に両方触れたら損切りを優先 (保守的)"""
        e = i + 1
        atr = self.atr[i, j]
        if not (np.isfinite(atr) and atr > 0):
            return None
        ent = self.O[e, j]
        stop, take = ent - RULE["stop_atr"] * atr, ent + RULE["take_atr"] * atr
        last = min(e + RULE["max_days"] - 1, len(self.idx) - 1)
        if e + RULE["max_days"] - 1 >= len(self.idx):
            return None
        for k in range(e, last + 1):
            o, h, l, c = self.O[k, j], self.H[k, j], self.L[k, j], self.C[k, j]
            if not np.isfinite(c):
                continue
            if k > e and np.isfinite(o):
                if o <= stop:
                    return (o / ent - 1) * 100, k, "stop"
                if o >= take:
                    return (o / ent - 1) * 100, k, "take"
            if l <= stop:
                return (stop / ent - 1) * 100, k, "stop"
            if h >= take:
                return (take / ent - 1) * 100, k, "take"
        c = self.C[last, j]
        return (c / ent - 1) * 100, last, "time"


def make_trades(M, sig, key):
    """シグナル (bool frame) から保有期間ごとのトレードを作る。保有中の同一銘柄の再シグナルは数えない"""
    s = sig.to_numpy(bool)
    keyv = key.to_numpy(float)
    ii, jj = np.nonzero(s)
    order = np.lexsort((ii, jj))  # 銘柄ごと、日付順
    ii, jj = ii[order], jj[order]
    out = {N: [] for N in HOLDS}
    out["rule"] = []
    busy = {N: {} for N in out}
    for i, j in zip(ii, jj):
        if not M.entry_ok(i, j):
            continue
        cost = 2 * cost_pct(M.to[i, j] if np.isfinite(M.to[i, j]) else 0)
        for N in HOLDS:
            if i <= busy[N].get(j, -1):
                continue
            r = M.fixed_exit(i, j, N)
            if r is None:
                continue
            g, x = r
            busy[N][j] = x - 1  # x日目の大引けで売るので、x-1日目のシグナルまでは保有中
            out[N].append((i, j, g, g - cost, g - M.bench[N][i], N, keyv[i, j], x))
        if i > busy["rule"].get(j, -1):
            r = M.rule_exit(i, j)
            if r is not None:
                g, x, why = r
                busy["rule"][j] = x - 1
                out["rule"].append((i, j, g, g - cost, x - i, why, keyv[i, j], x))
    return out


# ---------------------------------------------------------------- 集計
def stats(rows, M, N):
    """rows: (i, j, gross, net, excess, ...)"""
    if not rows:
        return {"n": 0}
    a = np.array([(r[0], r[2], r[3]) for r in rows], float)
    i, g, net = a[:, 0].astype(int), a[:, 1], a[:, 2]
    ex = np.array([r[4] for r in rows], float) if N != "rule" else None
    wins, losses = net[net > 0], net[net <= 0]
    pf = wins.sum() / -losses.sum() if losses.sum() < 0 else None
    # シグナル日ごとに平均してから集計 (同じ日に大量のシグナルが出る偏りを抑える)
    day = pd.DataFrame({"i": i, "net": net}).groupby("i")["net"].mean()
    tstat = (day.mean() / day.std(ddof=1) * math.sqrt(len(day))) if len(day) > 2 and day.std() > 0 else None
    out = {
        "n": int(len(net)),
        "days": int(len(day)),
        "win": float((net > 0).mean() * 100),
        "gross": float(g.mean()),
        "net": float(net.mean()),
        "median": float(np.median(net)),
        "avg_win": float(wins.mean()) if len(wins) else None,
        "avg_loss": float(losses.mean()) if len(losses) else None,
        "pf": float(pf) if pf is not None else None,
        "payoff": float(wins.mean() / -losses.mean()) if len(wins) and len(losses) and losses.mean() < 0 else None,
        "day_net": float(day.mean()),
        "t": float(tstat) if tstat is not None else None,
        "p10": float(np.percentile(net, 10)),
        "p90": float(np.percentile(net, 90)),
    }
    if ex is not None:
        ex = ex[np.isfinite(ex)]
        out["excess"] = float(ex.mean()) if len(ex) else None
        out["excess_win"] = float((ex > 0).mean() * 100) if len(ex) else None
    else:
        out["hold_days"] = float(np.mean([r[4] for r in rows]))
        why = [r[5] for r in rows]
        out["exit"] = {k: float(why.count(k) / len(why) * 100) for k in ("take", "stop", "time")}
    years = pd.Series(net, index=pd.Index(M.idx[i].year, name="y"))
    out["by_year"] = {str(y): {"n": int(len(v)), "net": float(v.mean()), "win": float((v > 0).mean() * 100)}
                      for y, v in years.groupby(level=0)}
    return out


def portfolio(rows, M):
    """資金を SLOTS 等分し、空き枠があればその日のシグナルを並び順キーの高い順に買った場合の試算。
    rows: (i, j, gross, net, _, _, key, exit_idx)。単利・毎日の終値で評価 (含み損益込み)。"""
    if not rows:
        return None
    by_day = {}
    for r in rows:
        by_day.setdefault(r[0], []).append(r)
    held, realized, taken = [], 0.0, 0
    eq = {}
    first = min(by_day) + 1
    for k in range(first, len(M.idx)):
        for h in [h for h in held if h[7] < k]:
            realized += h[3] / SLOTS
            held.remove(h)
        codes = {h[1] for h in held}
        for r in sorted(by_day.get(k - 1, []), key=lambda r: -(r[6] if np.isfinite(r[6]) else -1e9)):
            if len(held) >= SLOTS:
                break
            if r[1] in codes:
                continue
            held.append(r)
            codes.add(r[1])
            taken += 1
        open_pnl = 0.0
        for h in held:
            if h[7] == k:
                open_pnl += h[3] / SLOTS
                continue
            c = M.C[k, h[1]]
            ent = M.O[h[0] + 1, h[1]]
            if np.isfinite(c):
                open_pnl += ((c / ent - 1) * 100 - (h[2] - h[3])) / SLOTS
        eq[k] = 100 + realized + open_pnl
        if not held and k > max(by_day) + 1:
            break
    s = pd.Series(eq)
    s.index = M.idx[s.index]
    dd = (s / s.cummax() - 1).min() * 100
    total = s.iloc[-1] - 100
    yrs = max((s.index[-1] - s.index[0]).days / 365.25, 1 / 12)
    weekly = s.resample("W-FRI").last().dropna()
    return {
        "trades": taken,
        "total": float(total),
        "annual": float(total / yrs),
        "max_dd": float(dd),
        "curve": [[d.strftime("%Y-%m-%d"), round(float(v) - 100, 2)] for d, v in weekly.items()],
    }


def run(D, funds, demo):
    T = indicators(D, funds)
    M = Market(D, T)
    conds = conditions(T)
    first_valid = T["above200"].index[200] if len(T["above200"]) > 200 else T["above200"].index[0]
    one_year = max(first_valid, M.idx[-1] - pd.DateOffset(years=1))
    periods = {"1y": one_year, "all": first_valid}
    res = {"periods": {}}
    for pid, start in periods.items():
        mask = (M.idx >= start)[:, None]
        P = {"start": start.strftime("%Y-%m-%d"), "trading_days": int(mask.sum()), "variants": {}}
        for variant in ("price", "fund"):
            V = {}
            for cid, (tech, fund, key) in conds.items():
                sig = tech.fillna(False).astype(bool)
                if variant == "fund" and fund is not None:
                    sig = sig & fund.fillna(False).astype(bool)
                sig = sig & mask
                trades = make_trades(M, sig, key)
                V[cid] = {"name": COND_NAMES[cid],
                          "signals": int(sig.to_numpy().sum()),
                          "hold": {str(N): {**stats(trades[N], M, N),
                                            "portfolio": portfolio(trades[N], M)} for N in [*HOLDS, "rule"]}}
                h5 = V[cid]["hold"]["5"]
                print(f"[{pid} {variant}] {cid}: signals={V[cid]['signals']} 5d n={h5.get('n')} "
                      f"net={h5.get('net')} rule net={V[cid]['hold']['rule'].get('net')}")
            P["variants"][variant] = V
        P["bench_mean"] = {str(N): float(np.nanmean(np.where(mask[:, 0], M.bench[N], np.nan))) for N in HOLDS}
        res["periods"][pid] = P
    res["meta"] = {
        "demo": demo,
        "generated_at": dt.datetime.now(JST).isoformat(timespec="minutes"),
        "data_start": M.idx[0].strftime("%Y-%m-%d"),
        "test_end": M.idx[-1].strftime("%Y-%m-%d"),
        "tickers": len(M.cols),
        "with_fundamentals": int(sum(1 for c in M.cols if c in funds)),
        "holds": HOLDS,
        "rule": RULE,
        "params": PARAMS,
        "slots": SLOTS,
        "commission": COMMISSION,
        "cost_tiers": COST_TIERS,
        "tax": TAX,
    }
    return res


def clean(o):
    """JSONに出せない NaN/inf を None に、小数は4桁に丸める"""
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (float, np.floating)):
        return round(float(o), 4) if math.isfinite(o) else None
    if isinstance(o, np.integer):
        return int(o)
    return o


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--cache", default=None, help="日足のキャッシュ (pickle) のパス")
    args = ap.parse_args()
    D = load_prices_demo() if args.demo else load_prices_real(Path(args.cache) if args.cache else None)
    print(f"prices: {D['Close'].shape[1]} tickers x {D['Close'].shape[0]} days "
          f"({D['Close'].index[0].date()} - {D['Close'].index[-1].date()})")
    funds = load_fundamentals(D["Close"].columns, args.demo)
    res = run(D, funds, args.demo)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    name = "results_demo.json" if args.demo else "results.json"
    (OUT_DIR / name).write_text(json.dumps(clean(res), ensure_ascii=False, indent=1, allow_nan=False),
                                encoding="utf-8")
    print(f"wrote {OUT_DIR / name}")


if __name__ == "__main__":
    main()
