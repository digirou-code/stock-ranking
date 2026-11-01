"""config.toml の読み込み"""
from __future__ import annotations

import os
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load(path: str | None = None) -> dict:
    with open(path or os.path.join(HERE, "config.toml"), "rb") as f:
        return tomllib.load(f)


def data_dir() -> str:
    """Parquet ストアの場所。環境変数 SYSTEM_DATA で変えられる (既定: リポジトリ直下の data/、git 管理外)"""
    return os.environ.get("SYSTEM_DATA", os.path.join(ROOT, "data"))
