"""テーマ分析・個別銘柄監査の全体の流れ。

  定性（AI + Web検索/開示PDF）と 定量（外部API + コード判定）を別々に集め、最後にAIが突き合わせて監査する。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable

from . import config, store
from .ai import Analyst, Document
from .jquants import JQuants, JQuantsError
from .quant import build_snapshots

CODE_RE = re.compile(r"^[0-9][0-9A-Z]{3}$")


@dataclass
class Settings:
    max_companies: int = 8
    use_web: bool = True
    max_searches: int = 8
    max_fetches: int = 5
    use_jquants: bool = True
    attach_tdnet: bool = False  # 決算短信PDFを自動取得してAIに読ませる（J-Quants TDnetアドオンが必要）
    tdnet_max: int = 3
    extra: str = ""
    demo: bool = config.DEMO_MODE


def normalize_codes(raw) -> list[str]:
    if isinstance(raw, str):
        raw = re.split(r"[\s,、]+", raw)
    out = []
    for c in raw:
        c = str(c).strip().upper()
        if CODE_RE.match(c) and c not in out:
            out.append(c)
    return out


def _analyst(s: Settings, on_status, client=None) -> Analyst:
    return Analyst(on_status=on_status, max_searches=s.max_searches, max_fetches=s.max_fetches,
                   use_web=s.use_web, client=client)


def _tdnet_docs(codes: list[str], s: Settings, gaps: list[str], on_status) -> list[Document]:
    if not (s.attach_tdnet and config.has_jquants() and not s.demo):
        return []
    jq, docs = JQuants(), []
    for code in codes[: s.tdnet_max]:
        on_status(f"{code} の最新の決算短信PDFを取得しています（TDnet）…")
        try:
            got = jq.latest_earnings_pdf(code)
        except JQuantsError as e:
            gaps.append(f"決算短信PDFを取得できませんでした（{e}）")
            break
        if got:
            title, d, pdf = got
            docs.append(Document(f"{code} {title}（{d}開示）", pdf))
    return docs


def _finish(mode: str, theme: str, analyst: Analyst, notes: str, meta: dict, snaps: list[dict], env: dict,
            gaps: list[str], prices: dict, s: Settings, docs: list[Document], tdnet: list[Document], on_status) -> dict:
    audit = analyst.audit(theme, notes, snaps, env, gaps, extra=s.extra, docs=tdnet)
    by_code = {c["code"]: c for c in audit.get("companies", [])}
    companies = []
    for snap in snaps:
        companies.append({
            "code": snap["code"],
            "name": snap["name"],
            "quant": snap,
            "audit": by_code.get(snap["code"]),
            "extraction": meta.get(snap["code"]),
            "price_at_analysis": snap["株価"]["終値"],
        })
    order = {"深掘り": 0, "監視": 1, "優先度低": 2}
    companies.sort(key=lambda c: order.get((c["audit"] or {}).get("priority"), 3))
    bench = prices.get(config.BENCHMARK)
    run = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "theme": theme,
        "demo": s.demo,
        "settings": asdict(s),
        "models": sorted(analyst.models),
        "theme_overview": audit.get("theme_overview", {}),
        "environment": env,
        "environment_note": audit.get("environment_note", ""),
        "cross_check": audit.get("cross_check", []),
        "data_gaps": list(dict.fromkeys(gaps + audit.get("data_gaps", []))),
        "companies": companies,
        "research_notes": notes,
        "documents": [d.title for d in docs + tdnet],
        "guard_log": analyst.guard_log,
        "benchmark_at_analysis": float(bench["Close"].iloc[-1]) if bench is not None else None,
        "usage": analyst.usage,
    }
    path = store.save_run(run)
    on_status(f"保存しました: data/runs/{path.name}")
    run["_file"] = path.name
    return run


def analyze_theme(theme: str, include: list[str] | str = (), docs: list[Document] | None = None,
                  s: Settings | None = None, on_status: Callable[[str], None] = print, client=None) -> dict:
    """テーマ → 一次情報の調査 → 関連企業の抽出 → 数値の取得 → 監査。"""
    s, docs, include = s or Settings(), docs or [], normalize_codes(include)
    analyst = _analyst(s, on_status, client)
    notes = analyst.research_theme(theme, include, docs)
    extracted = analyst.extract_companies(theme, notes, s.max_companies, include)
    meta = {}
    for c in extracted:
        code = normalize_codes([c.get("code", "")])
        if code:
            meta.setdefault(code[0], c)
    codes = normalize_codes(include + list(meta))[: max(s.max_companies, len(include))]
    if not codes:
        raise RuntimeError("テーマに関連する銘柄を特定できませんでした。テーマを具体的にして再実行してください。")
    snaps, env, gaps, prices = build_snapshots(codes, {c: m.get("name", "") for c, m in meta.items()},
                                               demo=s.demo, use_jquants=s.use_jquants, on_status=on_status)
    tdnet = _tdnet_docs([x["code"] for x in snaps], s, gaps, on_status)
    return _finish("theme", theme, analyst, notes, meta, snaps, env, gaps, prices, s, docs, tdnet, on_status)


def audit_stocks(codes: list[str] | str, docs: list[Document] | None = None, s: Settings | None = None,
                 on_status: Callable[[str], None] = print, client=None) -> dict:
    """指定した銘柄の 数値の取得 → 開示資料・ニュースの調査 → 監査。"""
    s, docs, codes = s or Settings(), docs or [], normalize_codes(codes)
    if not codes:
        raise RuntimeError("証券コードを入力してください（例: 6857 5803）。")
    analyst = _analyst(s, on_status, client)
    snaps, env, gaps, prices = build_snapshots(codes, demo=s.demo, use_jquants=s.use_jquants, on_status=on_status)
    if not snaps:
        raise RuntimeError("指定した銘柄の株価データを取得できませんでした。")
    notes = analyst.research_companies([(x["code"], x["name"]) for x in snaps], docs)
    tdnet = _tdnet_docs([x["code"] for x in snaps], s, gaps, on_status)
    return _finish("audit", "", analyst, notes, {}, snaps, env, gaps, prices, s, docs, tdnet, on_status)


def propose_themes(n: int = 5, focus: str = "", s: Settings | None = None,
                   on_status: Callable[[str], None] = print, client=None) -> dict:
    s = s or Settings()
    analyst = _analyst(s, on_status, client)
    themes, notes = analyst.propose_themes(n, focus)
    return {"created_at": datetime.now().isoformat(timespec="seconds"), "themes": themes, "notes": notes,
            "usage": analyst.usage, "guard_log": analyst.guard_log}
