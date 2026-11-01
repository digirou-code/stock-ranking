# 3枠システム (日経平均を上回る個別株運用)

設計書: https://claude.ai/artifact/BV3A3WJsNiEfJbgoeYgouH

資金3,000万円を L 大型株 (売買代金上位) 50%・G 成長株 35%・R 急落リバウンド 15% に分け、毎営業日の引け後に
明日の注文とレポートを作る。売買ルールは `config.toml` に事前登録する (結果を見てから数値を変えない)。

## 構成

| ファイル | 役割 |
|---|---|
| `config.toml` | 資金・枠・出口ルール・リスク上限・合格基準 |
| `jq_client.py` | J-Quants API V2 (x-api-key、再試行、ページング) |
| `store.py` | Parquet ストア (`data/`、git 管理外)。調整後価格は AdjFactor から毎回計算 |
| `fins.py` | 財務の point-in-time 化 (開示の翌営業日、15:00以降は翌々営業日から) |
| `features.py` | 売買代金20日平均・移動平均・高値からの下落率など |
| `strategies.py` | 3枠のユニバースと並び順 (L / G0 / G1 / R、比較用の旧乖離リバウンド) |
| `engine.py` | 売買シミュレーション。バックテストと日次の紙上運用で共通 |
| `backtest/run.py` | 事前登録した検証0〜6 → `backtest/results/backtest_system.{json,md}` |
| `daily.py` / `report.py` | 日次バッチ、注文CSV (`state/orders/`)、レポート (`docs/system/`)、メール |

約定の判定は既存の検証 (jquants/backtest_all_pit.py) と同じ: 当日終値でシグナル、翌営業日の寄りで買い、
損切りは逆指値・利確は指値 (日中の安値・高値で判定、寄りで飛び越えたら始値)、最大日数の最終日は大引けで手仕舞い。
同じデータなら既存コードと同じ結果になることを確認済み (売買代金上位・R2・2019年: 両方 +24.9%)。

## 使い方

```bash
pip install -r requirements.txt
# 1) PC の jquants フォルダから10年分を取り込む (初回だけ)
python -c "from system import store; store.import_local(r'C:\path\to\jquants')"
# 2) 事前登録した検証を回す
python -m system.backtest.run
# 3) 日次バッチ (API から差分を取得。JQUANTS_API_KEY が必要)
python -m system.daily --mail
python -m system.daily --no-fetch --asof 2024-08-30 --start 2024-03-01   # 過去の日で試す
python -m pytest -q system/tests
```

GitHub Actions (`.github/workflows/system.yml`) は平日18:30 JST に日次バッチを動かす。
Secrets に `JQUANTS_API_KEY` (必須)、メールを送るなら `MAIL_USER`・`MAIL_APP_PASSWORD`・`MAIL_TO` を入れる。
`config.toml` の `[paper] live = false` のあいだは紙上運用 (レポートの注文は実際には出さない)。

## 注意

- `jq_client.ENDPOINTS` の `master`・`calendar` のパスは V2 の仕様書で未確認。
- 業種上限 (G・R は同一業種2銘柄まで) は銘柄マスタ (`data/master.parquet`) を入れるまで効かない。
- 本システムは投資判断の材料で、売買を推奨するものではない。
