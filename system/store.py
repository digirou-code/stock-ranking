"""データストア (Parquet)。

data/bars.parquet      日足 (Date, Code, O, H, L, C, Vo, Va, AdjFactor, retrieved_at)
data/fins.parquet      財務サマリー (API の列をそのまま文字列で + retrieved_at)
data/earnings.parquet  決算発表予定

調整後価格は AdjFactor (権利落ち日の分割係数) から毎回計算する: 調整後 = 価格 x (その日より後の AdjFactor の積)。
こうすると、取得元 (一括ファイル・日ごとのファイル・API) が混ざっても基準がそろう。
"""
from __future__ import annotations

import datetime as dt
import glob
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import config

BAR_COLS = ["Date", "Code", "O", "H", "L", "C", "Vo", "Va", "AdjFactor"]
NUM = ["O", "H", "L", "C", "Vo", "Va", "AdjFactor"]


def _path(name: str) -> str:
    return os.path.join(config.data_dir(), f"{name}.parquet")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _read_csvs(paths: list[str], usecols=None) -> pd.DataFrame:
    frames = [pd.read_csv(p, dtype=str, usecols=usecols, keep_default_na=False) for p in paths if os.path.getsize(p) > 0]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=usecols or [])


def _bars_frame(df: pd.DataFrame) -> pd.DataFrame:
    df = df[[c for c in BAR_COLS if c in df.columns]].copy()
    for c in NUM:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["AdjFactor"] = df["AdjFactor"].fillna(1.0)
    return df


def _merge(name: str, new: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    p = _path(name)
    if os.path.exists(p):
        new = pd.concat([pd.read_parquet(p), new], ignore_index=True)
    new = new.drop_duplicates(keys, keep="last").sort_values(keys).reset_index(drop=True)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    new.to_parquet(p, index=False)
    return new


# ---------------------------------------------------------------- 取り込み
def import_local(jq_dir: str) -> None:
    """PC の jquants フォルダ (jquants-data/_bulk と jquants-data/<種類>/YYYY/YYYY-MM-DD.csv) を取り込む"""
    base = os.path.join(jq_dir, "jquants-data")
    now = _now()
    for kind, name, keys in (("daily_bars", "bars", ["Date", "Code"]),
                             ("fins_summary", "fins", ["DiscNo"]),
                             ("earnings_date", "earnings", None)):
        paths = sorted(glob.glob(os.path.join(base, "_bulk", kind, "*.csv.gz"))) + \
            sorted(glob.glob(os.path.join(base, kind, "*", "*.csv")))
        if not paths:
            continue
        df = _read_csvs(paths)
        if name == "bars":
            df = _bars_frame(df)
        df["retrieved_at"] = now
        if keys is None:
            keys = [c for c in df.columns if c != "retrieved_at"]
        _merge(name, df, keys)
        print(f"{name}: {len(paths)} files", flush=True)


def append_api(client, dates: list[str]) -> None:
    """API から日付ごとに日足・財務・決算予定を取って追加する"""
    now = _now()
    bars, fins, earn = [], [], []
    for d in dates:
        bars += client.get("bars", date=d)
        fins += client.get("fins", date=d)
        earn += client.get("earnings", date=d)
    if bars:
        df = _bars_frame(pd.DataFrame(bars).astype(str))
        df["retrieved_at"] = now
        _merge("bars", df, ["Date", "Code"])
    if fins:
        df = pd.DataFrame(fins).astype(str).replace({"None": "", "nan": ""})
        df["retrieved_at"] = now
        _merge("fins", df, ["DiscNo"])
    if earn:
        df = pd.DataFrame(earn).astype(str)
        df["retrieved_at"] = now
        _merge("earnings", df, [c for c in df.columns if c != "retrieved_at"])


def last_date() -> str | None:
    p = _path("bars")
    if not os.path.exists(p):
        return None
    return pd.read_parquet(p, columns=["Date"])["Date"].max()


# ---------------------------------------------------------------- 読み出し (日付 x 銘柄の行列)
@dataclass
class Panel:
    dates: list[str]
    codes: list[str]
    O: np.ndarray   # 実株価 (株数・PER・時価総額に使う)
    H: np.ndarray
    L: np.ndarray
    C: np.ndarray
    AO: np.ndarray  # 調整後 (リターン・テクニカルに使う)
    AH: np.ndarray
    AL: np.ndarray
    AC: np.ndarray
    AV: np.ndarray  # 調整後出来高
    Va: np.ndarray  # 売買代金 (円)

    def col(self, code: str) -> int:
        return self.codes.index(code)


def load_panel(start: str | None = None, end: str | None = None) -> Panel:
    df = pd.read_parquet(_path("bars"), columns=BAR_COLS)
    if end:
        df = df[df["Date"] <= end]
    dates = sorted(df["Date"].unique())
    codes = sorted(df["Code"].unique())
    wide = {c: df.pivot(index="Date", columns="Code", values=c).reindex(index=dates, columns=codes)
            for c in NUM}
    fac = wide["AdjFactor"].fillna(1.0).to_numpy()
    # その日より後の係数の積 = 後ろからの累積積を1日ずらしたもの
    rev = np.cumprod(fac[::-1], axis=0)[::-1]
    mult = np.vstack([rev[1:], np.ones((1, fac.shape[1]))])
    m = {c: wide[c].to_numpy(dtype=np.float64, copy=True) for c in ("O", "H", "L", "C", "Vo", "Va")}
    for k in ("O", "H", "L", "C"):
        m[k][~(m[k] > 0)] = np.nan
    p = Panel(dates, codes, m["O"], m["H"], m["L"], m["C"],
              m["O"] * mult, m["H"] * mult, m["L"] * mult, m["C"] * mult, m["Vo"] / mult, m["Va"])
    if start:
        k = next((i for i, d in enumerate(dates) if d >= start), len(dates))
        for f in ("O", "H", "L", "C", "AO", "AH", "AL", "AC", "AV", "Va"):
            setattr(p, f, getattr(p, f)[k:])
        p.dates = dates[k:]
    return p


def load_fins() -> pd.DataFrame:
    return pd.read_parquet(_path("fins"))


def load_earnings() -> pd.DataFrame:
    p = _path("earnings")
    return pd.read_parquet(p) if os.path.exists(p) else pd.DataFrame()
