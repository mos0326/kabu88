"""API料金の計算と、月の予算上限。"""

from __future__ import annotations

from datetime import date

from . import config

# USD / 100万トークン（入力, 出力）。Haiku 5.5 は 10万トークン以下のプロンプトの料金。
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
WEB_SEARCH_USD = 0.01  # 1回あたり（$10 / 1,000回）。Web取得はトークン料金のみ

MODEL_LABELS = {
    "claude-opus-5-5": "Claude Opus 5.5（最も高品質・$4/$20）",
    "claude-sonnet-5-5": "Claude Sonnet 5.5（約半額・$2/$10）",
    "claude-haiku-5-5": "Claude Haiku 5.5（最安・$0.10/$0.50）",
}


def usage_cost(usage: dict, models: list[str] | None = None) -> float:
    """使用量から料金を計算（複数モデルが混ざる場合は高い方の単価で見積もる）。"""
    rates = [PRICES.get(m, PRICES["claude-opus-5-5"]) for m in (models or [config.MODEL])] or [PRICES["claude-opus-5-5"]]
    pin, pout = max(rates)
    inp = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0) * 1.25
    inp += usage.get("cache_read_input_tokens", 0) * 0.1
    return round((inp * pin + usage.get("output_tokens", 0) * pout) / 1e6
                 + usage.get("web_search_requests", 0) * WEB_SEARCH_USD, 4)


def estimate(model: str, use_web: bool, max_searches: int, max_fetches: int, n_companies: int,
             n_pdfs: int = 0) -> float:
    """1回の分析のおおよその上限見積もり（実際はこれより安いことが多い）。"""
    pin, pout = PRICES.get(model, PRICES["claude-opus-5-5"])
    inp = 6000 + n_companies * 2500  # 監査の入力（数値データ）
    out = 2500 + n_companies * 900
    if use_web:
        inp += 4000 + max_searches * 9000 + max_fetches * 12000
        out += 3000
    inp += n_pdfs * 40000
    return round((inp * pin + out * pout) / 1e6 + (max_searches if use_web else 0) * WEB_SEARCH_USD, 2)


def month_spend(runs: list[dict]) -> float:
    """今月 Claude API で使った金額（保存した分析結果から集計。Claude Code 実行分は0円）。"""
    ym = date.today().strftime("%Y-%m")
    total = 0.0
    for r in runs:
        if r.get("created_at", "").startswith(ym) and r.get("source", "api") == "api":
            total += r.get("cost_usd") if r.get("cost_usd") is not None else usage_cost(r.get("usage", {}), r.get("models"))
    return round(total, 2)
