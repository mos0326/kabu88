---
name: kabu-audit
description: 指定した日本株（証券コード）を、外部APIの数値（バリュエーション・信用残・空売り・会社予想・過熱）と開示資料・ニュースで監査して保存する。Claude API の従量課金を使わない。「〇〇（コード）を監査して」「この銘柄大丈夫？」と頼まれたときに使う。
argument-hint: "<証券コード...>"
---

# 個別銘柄の監査（Claude Code で実行・API料金なし）

対象: $ARGUMENTS

## 0. 準備
1. `python -c "import yfinance, pypdf, xlrd"` が失敗したら `pip install -q -r requirements.txt` を実行する。
2. `python -m kabu.cli rules` を実行し、表示されたルールに例外なく従う（短期予測・目標株価・売買推奨は書かない。数値は prepare の出力だけを使う）。

## 1. 数値を取得する（無料）
```
python -m kabu.cli prepare <コード...>
```
出力JSONの数値・監査フラグを読み、「保留中の分析ID」と「監査JSONの保存先」（標準エラー）を控える。

## 2. 定性リサーチ（WebSearch / WebFetch・検索は合計5回程度まで）
各社について一次情報を優先して調べる:
1. 直近の決算短信・決算説明資料: 業績の変化の中身、会社予想の修正とその理由
2. 中期経営計画・有価証券報告書: 事業構成、成長投資、中長期の構造変化
3. 業界・マクロの中長期の追い風・逆風
4. 中長期の業績見通しに影響しそうな最近のニュース
出典（資料名・サイト名・日付）つきの箇条書きで `data/runs/pending/notes.md` に書く。

## 3. 監査JSONを書く
`python -m kabu.cli schema` の形式で「監査JSONの保存先」に書く。テーマ指定がないので、theme_overview には対象銘柄に共通する中長期の論点をまとめる。
楽観的な見方には、提供された数値（割高・過熱・需給の重さ・減益予想・下方修正）で反証を当てること。priority は調査の優先度であり売買推奨ではない。

## 4. 保存して報告する
```
python -m kabu.cli finalize <保留中の分析ID> --notes data/runs/pending/notes.md
```
エラーが出たら直して再実行する。成功したらレポートのパスと要点を短く伝える（売買の推奨や株価の予想はしない）。
