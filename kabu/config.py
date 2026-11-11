"""設定値とパス。環境変数（.env）から読み込む。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
RUNS_DIR = DATA_DIR / "runs"
DECISIONS_FILE = DATA_DIR / "decisions.json"
UNIVERSE_CSV = ROOT / "config" / "universe.csv"

# ---------------------------------------------------------------- 接続先
# Claude（必須）: ANTHROPIC_API_KEY。モデルの既定は Claude Opus 5.5。
MODEL = os.getenv("KABU_MODEL", "claude-opus-5-5")

# J-Quants API V2（JPX公式・任意）: ダッシュボードで発行したAPIキー
JQUANTS_API_KEY = os.getenv("JQUANTS_API_KEY", "").strip()
# 契約プランの1分あたりリクエスト上限（Free 5 / Light 60 / Standard 120 / Premium 500）
JQUANTS_RPM = int(os.getenv("JQUANTS_RPM", "60"))

# 1 にするとネット接続なしで合成データを使うデモモード（動作確認用）
DEMO_MODE = os.getenv("KABU_DEMO", "0") == "1"

# 市場全体の把握に使う指標（Yahoo Finance のティッカー）
BENCHMARK = "^N225"  # 日経平均
TOPIX_ETF = "1306.T"  # TOPIX連動ETF（TOPIXの代わり）
USDJPY = "JPY=X"  # ドル円

# ---------------------------------------------------------------- 監査フラグのしきい値
# 数値はすべて外部APIの値からコードで判定する（AIには判定させない）。
THRESHOLDS = {
    "RSI過熱": 75,  # RSI14 がこれ以上
    "25日線乖離過熱%": 15,
    "200日線乖離過熱%": 40,  # 長期平均から大きく上振れ = 期待の織り込みが進んでいる可能性
    "3ヶ月騰落過熱%": 40,
    "1年騰落過熱%": 100,
    "予想PER割高": 40,
    "PBR割高": 8,
    "信用倍率高": 5,  # 買い残 / 売り残
    "買残出来高日数高": 10,  # 買い残 / 25日平均出来高
    "買残急増%": 30,  # 4週間前比
    "大口空売り残高%": 3,  # 空売り残高報告（0.5%以上の報告分）の合計
    "業種空売り比率高%": 50,
    "減益予想%": -10,  # 会社予想の営業増益率がこれ以下
    "決算接近日数": 14,
    "データ鮮度日数": 30,  # バリュエーションの基準日がこれより古ければ注意
}


def has_api_key() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


def has_jquants() -> bool:
    return bool(JQUANTS_API_KEY)
