"""テーマ分析・個別銘柄監査の全体の流れ。

  定性（AI + Web検索/開示PDF）と 定量（外部API + コード判定）を別々に集め、最後にAIが突き合わせて監査する。

AIの部分は2通りの実行方法がある:
  - Claude API（従量課金）… ダッシュボードのボタン / CLI の theme・audit コマンド
  - Claude Code（サブスクリプションの範囲内・API料金なし）… .claude/skills の手順で
      prepare（数値の取得）→ Claude Code が調査して監査JSONを書く → finalize（検証・ガード・保存）
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import config, costs, report, store
from .ai import AUDIT_SCHEMA, AIError, Analyst, Document
from .guard import scrub
from .jquants import JQuants, JQuantsError
from .quant import build_snapshots
from .schema_check import validate

CODE_RE = re.compile(r"^[0-9][0-9A-Z]{3}$")


def pending_dir() -> Path:
    return config.RUNS_DIR / "pending"


@dataclass
class Settings:
    # 料金を抑える既定値（必要に応じてダッシュボードで増やす）
    max_companies: int = 5
    use_web: bool = True
    max_searches: int = 3
    max_fetches: int = 2
    use_jquants: bool = True
    attach_tdnet: bool = False  # 決算短信PDFを自動取得してAIに読ませる（J-Quants TDnetアドオンが必要・トークン料金増）
    tdnet_max: int = 2
    extra: str = ""
    model: str = config.MODEL
    demo: bool = config.DEMO_MODE


class BudgetError(AIError):
    pass


def check_budget() -> float:
    """今月の Claude API 料金が上限に達していれば止める。戻り値は今月の使用額（USD）。"""
    spent = costs.month_spend(store.load_runs())
    if config.MONTHLY_BUDGET_USD > 0 and spent >= config.MONTHLY_BUDGET_USD:
        raise BudgetError(f"今月の Claude API 料金が上限（${config.MONTHLY_BUDGET_USD:.2f}）に達しました（使用 ${spent:.2f}）。"
                          "Claude Code からの実行（API料金なし）を使うか、.env の KABU_MONTHLY_BUDGET_USD を見直してください。")
    return spent


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
                   use_web=s.use_web, client=client, model=s.model)


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


def assemble_run(*, mode: str, theme: str, source: str, audit: dict, notes: str, meta: dict, snaps: list[dict],
                 env: dict, gaps: list[str], benchmark: float | None, settings: dict, models: list[str],
                 documents: list[str], guard_log: list[dict], usage: dict, created_at: str | None = None) -> dict:
    """APIでも Claude Code でも同じ形式で、数値（quant）と監査（audit）を銘柄ごとに結合して保存する。"""
    by_code = {c["code"]: c for c in audit.get("companies", [])}
    companies = [{
        "code": snap["code"], "name": snap["name"], "quant": snap, "audit": by_code.get(snap["code"]),
        "extraction": meta.get(snap["code"]), "price_at_analysis": snap["株価"]["終値"],
    } for snap in snaps]
    order = {"深掘り": 0, "監視": 1, "優先度低": 2}
    companies.sort(key=lambda c: order.get((c["audit"] or {}).get("priority"), 3))
    run = {
        "created_at": created_at or datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "source": source,
        "theme": theme,
        "demo": settings.get("demo", False),
        "settings": settings,
        "models": models,
        "theme_overview": audit.get("theme_overview", {}),
        "environment": env,
        "environment_note": audit.get("environment_note", ""),
        "cross_check": audit.get("cross_check", []),
        "data_gaps": list(dict.fromkeys(gaps + audit.get("data_gaps", []))),
        "companies": companies,
        "research_notes": notes,
        "documents": documents,
        "guard_log": guard_log,
        "benchmark_at_analysis": benchmark,
        "usage": usage,
        "cost_usd": costs.usage_cost(usage, models) if source == "api" else 0.0,
    }
    path = store.save_run(run)
    run["_file"] = path.name
    rpath = report.save_markdown(run)
    run["_report"] = str(rpath.relative_to(config.ROOT)) if rpath.is_relative_to(config.ROOT) else str(rpath)
    return run


def _bench(prices: dict) -> float | None:
    b = prices.get(config.BENCHMARK)
    return float(b["Close"].iloc[-1]) if b is not None else None


def _finish(mode, theme, analyst: Analyst, notes, meta, snaps, env, gaps, prices, s: Settings,
            docs: list[Document], tdnet: list[Document], on_status) -> dict:
    audit = analyst.audit(theme, notes, snaps, env, gaps, extra=s.extra, docs=tdnet)
    run = assemble_run(mode=mode, theme=theme, source="api", audit=audit, notes=notes, meta=meta, snaps=snaps,
                       env=env, gaps=gaps, benchmark=_bench(prices), settings=asdict(s), models=sorted(analyst.models),
                       documents=[d.title for d in docs + tdnet], guard_log=analyst.guard_log, usage=analyst.usage)
    on_status(f"保存しました: data/runs/{run['_file']}（レポート: {run['_report']}）・料金の目安 ${run['cost_usd']:.3f}")
    return run


# ================================================================ Claude API で実行（従量課金）

def analyze_theme(theme: str, include: list[str] | str = (), docs: list[Document] | None = None,
                  s: Settings | None = None, on_status: Callable[[str], None] = print, client=None) -> dict:
    """テーマ → 一次情報の調査 → 関連企業の抽出 → 数値の取得 → 監査。"""
    s, docs, include = s or Settings(), docs or [], normalize_codes(include)
    check_budget()
    analyst = _analyst(s, on_status, client)
    notes = analyst.research_theme(theme, include, docs, max_companies=s.max_companies)
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
    check_budget()
    analyst = _analyst(s, on_status, client)
    snaps, env, gaps, prices = build_snapshots(codes, demo=s.demo, use_jquants=s.use_jquants, on_status=on_status)
    if not snaps:
        raise RuntimeError("指定した銘柄の株価データを取得できませんでした。")
    notes = analyst.research_companies([(x["code"], x["name"]) for x in snaps], docs)
    tdnet = _tdnet_docs([x["code"] for x in snaps], s, gaps, on_status)
    return _finish("audit", "", analyst, notes, {}, snaps, env, gaps, prices, s, docs, tdnet, on_status)


def propose_themes(n: int = 4, focus: str = "", s: Settings | None = None,
                   on_status: Callable[[str], None] = print, client=None) -> dict:
    s = s or Settings()
    check_budget()
    analyst = _analyst(s, on_status, client)
    themes, notes = analyst.propose_themes(n, focus)
    return {"created_at": datetime.now().isoformat(timespec="seconds"), "themes": themes, "notes": notes,
            "usage": analyst.usage, "guard_log": analyst.guard_log,
            "cost_usd": costs.usage_cost(analyst.usage, sorted(analyst.models))}


# ================================================================ Claude Code で実行（API料金なし）

def prepare(codes: list[str] | str, theme: str = "", demo: bool | None = None, use_jquants: bool = True,
            on_status: Callable[[str], None] = lambda s: None) -> dict:
    """数値だけを取得して「保留中の分析」を作る。Claude Code はこの数値を読んで監査JSONを書く。"""
    codes = normalize_codes(codes)
    if not codes:
        raise RuntimeError("証券コードを指定してください（例: 6857 5803）。")
    demo = config.DEMO_MODE if demo is None else demo
    snaps, env, gaps, prices = build_snapshots(codes, demo=demo, use_jquants=use_jquants, on_status=on_status)
    if not snaps:
        raise RuntimeError("指定した銘柄の株価データを取得できませんでした。")
    pid = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    pending = {"id": pid, "created_at": datetime.now().isoformat(timespec="seconds"), "theme": theme,
               "mode": "theme" if theme else "audit", "demo": demo, "snapshots": snaps, "environment": env,
               "gaps": gaps, "benchmark": _bench(prices)}
    pending_dir().mkdir(parents=True, exist_ok=True)
    (pending_dir() / f"{pid}.json").write_text(json.dumps(pending, ensure_ascii=False, indent=1, default=str),
                                             encoding="utf-8")
    return pending


def finalize(pid: str, audit_path: str | Path | None = None, notes_path: str | Path | None = None) -> dict:
    """Claude Code が書いた監査JSONを検証し、短期予測ガードを通して保存する。"""
    pfile = pending_dir() / f"{pid}.json"
    if not pfile.exists():
        raise RuntimeError(f"保留中の分析 {pid} が見つかりません（data/runs/pending/ を確認してください）。")
    pending = json.loads(pfile.read_text(encoding="utf-8"))
    apath = Path(audit_path) if audit_path else pending_dir() / f"{pid}.audit.json"
    try:
        audit = json.loads(apath.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise RuntimeError(f"監査JSON {apath} がありません。") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"監査JSONの書式が正しくありません: {e}") from e
    errors = validate(audit, AUDIT_SCHEMA)
    if errors:
        raise RuntimeError("監査JSONがスキーマに合いません:\n" + "\n".join(errors[:30]))
    valid = {s["code"] for s in pending["snapshots"]}
    missing = valid - {c["code"] for c in audit["companies"]}
    audit["companies"] = [c for c in audit["companies"] if c["code"] in valid]
    audit, log = scrub(audit, "audit")
    notes = Path(notes_path).read_text(encoding="utf-8") if notes_path and Path(notes_path).exists() else ""
    notes, log = scrub(notes, "research_notes", log)
    gaps = pending["gaps"] + ([f"監査されなかった銘柄: {', '.join(sorted(missing))}"] if missing else [])
    run = assemble_run(mode=pending["mode"], theme=pending["theme"], source="claude-code", audit=audit, notes=notes,
                       meta={}, snaps=pending["snapshots"], env=pending["environment"], gaps=gaps,
                       benchmark=pending.get("benchmark"), settings={"demo": pending.get("demo", False)},
                       models=["Claude Code"], documents=[], guard_log=log, usage={},
                       created_at=pending["created_at"])
    pfile.unlink()
    apath.unlink(missing_ok=True)
    return run
