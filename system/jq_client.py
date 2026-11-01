"""J-Quants API V2 の最小クライアント。

- 認証: 環境変数 JQUANTS_API_KEY を x-api-key ヘッダーで送る (V2 の API キー方式)。
- 429 と 5xx は待ち時間を倍にしながら最大8回やり直す (既存 fetch_bulk.py と同じ考え方)。
- ページングは応答の pagination_key を次の要求に付けて続ける。
エンドポイントのパスは ENDPOINTS にまとめた。V2 の仕様書 (https://jpx-jquants.com/spec) で名前が違えばここだけ直す。
"""
from __future__ import annotations

import os
import time

import requests

BASE = os.environ.get("JQUANTS_BASE", "https://api.jquants.com/v2")
ENDPOINTS = {
    "bars": "/equities/bars/daily",          # 既存 fetch_bulk.py で確認済み
    "fins": "/fins/summary",                 # 同上
    "earnings": "/fins/earnings-date",       # 同上
    "master": "/equities/master",            # 要確認
    "calendar": "/markets/calendar",         # 要確認
}


class JQuants:
    def __init__(self, api_key: str | None = None, session: requests.Session | None = None):
        self.key = api_key or os.environ.get("JQUANTS_API_KEY")
        if not self.key:
            raise RuntimeError("JQUANTS_API_KEY が設定されていません")
        self.s = session or requests.Session()

    def get(self, name: str, **params) -> list[dict]:
        url = BASE + ENDPOINTS[name]
        rows: list[dict] = []
        while True:
            body = self._request(url, params)
            data = body.get("data")
            if data is None:  # 応答のキー名が違う場合は、最初のリスト値を使う
                data = next((v for v in body.values() if isinstance(v, list)), [])
            rows.extend(data)
            pk = body.get("pagination_key")
            if not pk:
                return rows
            params = {**params, "pagination_key": pk}

    def _request(self, url: str, params: dict) -> dict:
        for k in range(8):
            r = self.s.get(url, params=params, headers={"x-api-key": self.key}, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(120, 5 * 2 ** k))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}
