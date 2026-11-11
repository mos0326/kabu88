"""分析結果を Markdown レポートにする（Claude アプリや GitHub でそのまま読める形）。"""

from __future__ import annotations

import re
from pathlib import Path

from . import config

KEY_METRICS = [
    ("株価", "終値"), ("株価", "1年騰落%"), ("株価", "3ヶ月騰落%"), ("株価", "200日線乖離%"), ("株価", "RSI14"),
    ("バリュエーション", "予想PER"), ("バリュエーション", "PBR"), ("バリュエーション", "ROE%"),
    ("業績", "予想営業増益率%"), ("業績", "営業利益進捗率%"), ("業績", "業績予想の修正"),
    ("需給", "信用倍率"), ("需給", "買残/出来高(日)"), ("需給", "買残4週変化%"), ("需給", "大口空売り残高%"),
    ("イベント", "次回決算予定日"),
]
ICON = {"警告": "🔴", "注意": "🟡", "情報": "🔵"}


def _v(x) -> str:
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:,.2f}".rstrip("0").rstrip(".")
    return str(x)


def _list(items) -> str:
    return "\n".join(f"- {x}" for x in items) if items else "- —"


def to_markdown(run: dict) -> str:
    ov = run.get("theme_overview", {})
    src = "Claude Code（API料金なし）" if run.get("source") == "claude-code" else f"Claude API（{', '.join(run.get('models', []))}）"
    lines = [
        f"# {ov.get('title') or run.get('theme') or '個別銘柄の監査'}",
        "",
        f"- 分析日時: {run['created_at'][:16].replace('T', ' ')}　/　時間軸: {ov.get('horizon', '-')}　/　実行: {src}",
        "- ⚠️ 短期の値動き・目標株価・売買の推奨は含みません。判断はご自身で行ってください。",
        "",
        ov.get("thesis", ""),
        "",
        "## 追い風", "", _list(ov.get("drivers")), "",
        "## 逆風・不確実性", "", _list(ov.get("headwinds")), "",
        "## 仮説が崩れる条件", "", _list(ov.get("invalidation")),
    ]
    if ov.get("value_chain"):
        lines += ["", "**バリューチェーン**: " + " → ".join(ov["value_chain"])]
    env = run.get("environment", {})
    if env:
        lines += ["", "## 相場環境（外部APIの数値）"]
        for k, m in env.items():
            if "終値" in m:
                lines.append(f"- {k}: {_v(m['終値'])}（1ヶ月 {_v(m.get('1ヶ月騰落%'))}% / 1年 {_v(m.get('1年騰落%'))}%）")
            else:
                lines.append(f"- {k}: {m.get('値')}%（{m.get('基準日')}）")
        if run.get("environment_note"):
            lines.append(f"- AIの整理: {run['environment_note']}")
    if run.get("cross_check"):
        lines += ["", "## ⚖️ 定性情報と数値の食い違い", _list(run["cross_check"])]

    lines += ["", "## 一覧", "", "| コード | 銘柄 | 調査の優先度 | 関連度 | 織り込み度 | 警告 | 注意 |", "|---|---|---|---|---|---|---|"]
    for c in run["companies"]:
        a, fl = c.get("audit") or {}, c["quant"]["flags"]
        lines.append(f"| {c['code']} | {c['name']} | {a.get('priority', '-')} | {a.get('exposure', '-')} | "
                     f"{a.get('priced_in', '-')} | {sum(f['level'] == '警告' for f in fl)} | {sum(f['level'] == '注意' for f in fl)} |")

    for c in run["companies"]:
        q, a = c["quant"], c.get("audit") or {}
        lines += ["", f"## {c['name']}（{c['code']}）— {a.get('priority', '-')}", ""]
        if a.get("theme_role"):
            lines.append(f"_{a['theme_role']}_\n")
        lines += ["**数値（外部API）**", "", "| 指標 | 値 |", "|---|---|"]
        for sec, key in KEY_METRICS:
            val = q.get(sec, {}).get(key)
            if val is not None:
                lines.append(f"| {key} | {_v(val)} |")
        lines.append("")
        lines.append("出典: " + " / ".join(f"{k}={v}" for k, v in q.get("出典", {}).items() if not k.startswith("_")))
        lines += ["", "**監査フラグ（コード判定）**", _list([f"{ICON[f['level']]} [{f['category']}] {f['text']}" for f in q["flags"]])]
        if a:
            lines += [
                "", f"**中長期の変化**: {a.get('fundamental_change', '')}",
                "", f"**数値の監査**: {a.get('quant_audit', '')}",
                "", f"**織り込み度（{a.get('priced_in', '-')}）**: {a.get('priced_in_reason', '')}",
                "", "**強気材料**", _list(a.get("bull_points")),
                "", "**弱気材料・リスク**", _list(a.get("bear_points")),
                "", "**追うべきKPI**", _list(a.get("kpis_to_watch")),
                "", "**次の確認ポイント**", _list(a.get("next_checkpoints")),
                "", f"**調査の優先度: {a.get('priority', '-')}** — {a.get('priority_reason', '')}",
            ]
            if a.get("evidence"):
                lines += ["", "**根拠と出典**", _list([f"{e['fact']}（{e['source']}）" for e in a["evidence"]])]
    if run.get("data_gaps"):
        lines += ["", "## 不足していたデータ", _list(run["data_gaps"])]
    if run.get("guard_log"):
        lines += ["", f"## 短期予測ガードで除去した文（{len(run['guard_log'])}件）",
                  _list([f"{g['removed']}（{g['reason']}）" for g in run["guard_log"]])]
    if run.get("research_notes"):
        lines += ["", "## リサーチメモ（出典つき）", "", run["research_notes"]]
    return "\n".join(lines).strip() + "\n"


def save_markdown(run: dict) -> Path:
    d = config.REPORTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w\-ぁ-んァ-ヶ一-龠ー]+", "_", run.get("theme") or "監査_" + "_".join(c["code"] for c in run["companies"]))[:40]
    path = d / f"{run['created_at'][:10]}_{slug}.md"
    path.write_text(to_markdown(run), encoding="utf-8")
    return path
