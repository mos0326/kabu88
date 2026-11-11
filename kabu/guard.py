"""短期予測ガード: AIの出力から「短期の値動き予測・目標株価・売買指示」に当たる文を取り除く。

プロンプトで禁止したうえでの最後の安全網。取り除いた文は記録して画面に表示する（透明性のため）。
「上値が重い」「買われすぎ」のような“現状の需給・過熱の指摘”は対象外。
"""

from __future__ import annotations

import re

_SHORT = r"(?:明日|翌日|今日|本日|今週|来週|週明け|週内|数日|数週間|今月中|月内|年内|近日|近いうち|目先|短期(?:的)?(?:に|には|では|で)?)"
_MOVE = r"(?:上昇|下落|反発|反落|急騰|急落|続伸|続落|値上がり|値下がり|上がる|下がる|上げる|下げる|戻る|戻す)"
_PREDICT = r"(?:する|しそう|しやすい|が見込まれ|が期待され|を予想|と予想|と見込|に転じ|へ向か|局面入り|余地が大きい|可能性が高い)"

PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"目標株価|株価目標|上値目[標処途]|下値目[処途]|ターゲットプライス|理論株価は"), "目標株価"),
    (re.compile(r"\d[\d,，.]*\s*円(?:台)?(?:を|まで|へ|に|付近まで)\s*(?:目指|上昇|下落|到達|回復|戻)"), "価格水準の予測"),
    (re.compile(_SHORT + r"[^。！!？?\n]{0,40}?" + _MOVE + r"[^。！!？?\n]{0,6}?" + _PREDICT), "短期の方向性予測"),
    (re.compile(r"買い時|売り時|仕込み時|買い場|売り場|エントリー(?:ポイント|タイミング)|今が(?:買|売)"), "売買タイミング"),
    (re.compile(r"買い推奨|売り推奨|(?:買う|売る)べき|(?:買い|売り)を推奨|損切り(?:ライン|水準)(?:は|を)\s*\d"), "売買指示"),
]

# 銘柄コード・出典URLなど、検査しないキー
SKIP_KEYS = {"code", "name", "source", "url", "exposure", "priced_in", "priority"}

_SPLIT = re.compile(r"(?<=[。！？!?\n])")


def violations(text: str) -> list[str]:
    return [label for pat, label in PATTERNS if pat.search(text)]


def _scrub_text(text: str, path: str, log: list[dict]) -> str:
    kept = []
    for sentence in _SPLIT.split(text):
        hit = violations(sentence)
        if hit:
            log.append({"field": path, "reason": "・".join(hit), "removed": sentence.strip()})
        else:
            kept.append(sentence)
    return "".join(kept).strip()


def scrub(obj, path: str = "", log: list[dict] | None = None):
    """dict/list/str を再帰的に検査し、違反した文を削除した複製と削除ログを返す。"""
    if log is None:
        log = []
    if isinstance(obj, str):
        cleaned = _scrub_text(obj, path, log)
        if obj.strip() and not cleaned:
            cleaned = "（短期予測・売買指示に当たる表現のため非表示）"
        return cleaned, log
    if isinstance(obj, list):
        out = []
        for i, x in enumerate(obj):
            y, _ = scrub(x, f"{path}[{i}]", log)
            if y != "（短期予測・売買指示に当たる表現のため非表示）":
                out.append(y)
        return out, log
    if isinstance(obj, dict):
        return {k: (v if k in SKIP_KEYS else scrub(v, f"{path}.{k}" if path else k, log)[0]) for k, v in obj.items()}, log
    return obj, log
