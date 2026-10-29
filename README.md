# 日次株ランキング

東証の銘柄を毎営業日スクリーニングし、総合SCOREと条件別ランキングを表示する静的WebUIです。
参考: https://j-kabu.jp/rankings

## 仕組み

1. `screener/build_data.py` が JPX の上場銘柄一覧と Yahoo Finance (yfinance) からデータを集め、
   9種類のスクリーニングと総合SCOREを計算して `docs/data/latest.json` に書き出します。
   前営業日のスナップショット (`docs/data/history/`) と比べて順位の変動も記録します。
2. `screener/build_page.py` が `docs/index.html` を生成します。
3. GitHub Actions (`.github/workflows/daily.yml`) が平日16:30 JSTに 1→2 を実行してコミットし、
   GitHub Pages が `docs/` を公開します。

## スクリーニング

| タブ | 条件 | 並び順 |
|---|---|---|
| 総合SCORE | 11項目中8項目以上のデータあり | SCORE |
| 高配当 | 配当利回り3.5%以上, ROE5%以上, 時価総額100億円以上 | 配当利回り |
| 高配当×低PBR | 配当利回り3%以上, PBR1.0倍以下 | 配当とPBRの順位合計 |
| 低PER | PER0〜12倍, ROE8%以上 | PERが低い順 |
| 低PBR | PBR0.8倍以下, ROE0%超 | PBRが低い順 |
| 高ROE | ROE15%以上, 負債資本倍率100%未満 | ROE |
| 成長株 | 売上・利益成長率10%以上, 時価総額300億円以上 | 成長率の順位合計 |
| 小型成長 | 時価総額300億円未満, 売上成長率15%以上, 営業黒字 | 売上成長と利益率 |
| モメンタム | 52週レンジ位置80%以上, 売買代金1億円以上 | 3ヶ月騰落率 |

条件は `build_data.py` の `SCREENS` で変更できます。

## 保有銘柄ページ (`#holdings`)

リポジトリ直下の `holdings.csv` (SBI証券のポートフォリオ画面からダウンロードしたCSVをそのまま置く) を読み、
保有している個別株ごとに次の項目を表示します。投資信託は名前だけ一覧にします。

| 項目 | データ元 |
|---|---|
| その日の評価 | 総合SCOREとカテゴリ別スコア、前日比、ランクインしているスクリーニング |
| 決算日 | JPXの決算発表予定日一覧 (未掲載ならYahoo Financeの予定日) |
| セクター | JPXの33業種区分 |
| ステージ | 150日(約30週)移動平均の傾きと株価の位置によるワインスタインのステージ (1底固め/2上昇/3天井圏/4下降) |
| 信用残 | JPXの銘柄別信用取引残高 (日々・申込日基準) の売残・買残・信用倍率 |

`holdings.csv` を更新してpushすると、ワークフローが自動で走って反映されます。

## マーケット (`#markets`) と経済スケジュール (`#calendar`)

- マーケット: 日経平均、日経平均先物 (CME円建て)、NYダウ、S&P500、ナスダック、SOX、VIX、米10年債利回り、
  ドル円、ユーロ円、WTI原油、金の日足ローソク足 (Yahoo Finance、1年分)。指標は `build_markets.py` の `INDICES` で増減できます。
- 経済スケジュール: Forex Factory の公開週間カレンダー (日本時間に変換、主なイベント名は日本語訳付き) を
  毎日 `cache/calendar_store.json` に蓄積し、過去3日〜10日先を表示します。公開フィードは今週分だけなので、
  来週以降の予定は週が替わってから入ります。
- 平日7:00 JST にもマーケットとスケジュールだけ更新します。
- チャートは TradingView Lightweight Charts (Apache-2.0) を `docs/vendor/` に同梱しています。

## 総合SCORE

PER・PBR・配当利回り・ROE・営業利益率・売上成長率・利益成長率・負債資本倍率・3ヶ月騰落率・
52週レンジ位置・売買代金の11項目を、対象銘柄内のパーセンタイル (0〜100) に換算して平均します。
PER・PBR・負債資本倍率は低いほど高評価、赤字(負のPER)は最下位扱いです。

## セットアップ (GitHub)

1. このフォルダの中身をリポジトリに push する
2. Settings → Pages で「Deploy from a branch」、ブランチ `main` / フォルダ `/docs` を選ぶ
3. Actions タブで `daily-ranking` を一度手動実行する (初回は30〜60分ほどかかります)

財務指標は Yahoo のレート制限を避けるため1回あたり最大4000銘柄 (実質全銘柄) を取り直し、
7日より古いものから順に更新します (`FUND_REFRESH_PER_RUN`, `FUND_MAX_AGE_DAYS` で調整)。
初回から数日は財務指標のない銘柄があり、SCOREの対象数が少なめになります。

## ローカルで試す

```bash
pip install -r requirements.txt
python screener/build_data.py --demo   # ネット不要のデモデータ
python screener/build_page.py
python -m http.server -d docs 8000     # http://localhost:8000
```

## 注意

yfinance は Yahoo Finance の非公式APIです。個人の調査用途で使い、データの正確性は保証されません。
本ランキングは投資判断の材料であり、売買を推奨するものではありません。
