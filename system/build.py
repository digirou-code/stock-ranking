"""ストア → 行列 (日足パネル・特徴量・PIT財務) をまとめて作る。結果は data/_built.pkl に置き、ストアが変わったら作り直す。"""
from __future__ import annotations

import os
import pickle

from . import config, features, fins, store


def build(force: bool = False, end: str | None = None):
    src = [os.path.join(config.data_dir(), f"{n}.parquet") for n in ("bars", "fins")]
    stamp = tuple(os.path.getmtime(s) for s in src) + (end,)
    cache = os.path.join(config.data_dir(), "_built.pkl")
    if not force and os.path.exists(cache):
        with open(cache, "rb") as f:
            obj = pickle.load(f)
        if isinstance(obj, dict) and obj.get("stamp") == stamp:
            return obj["p"], obj["T"], obj["fp"]
    p = store.load_panel(end=end)
    T = features.compute(p)
    fp = fins.FinPanel(p.dates, p.codes, store.load_fins())
    with open(cache, "wb") as f:
        pickle.dump({"stamp": stamp, "p": p, "T": T, "fp": fp}, f, protocol=5)
    return p, T, fp
