"""API料金をかけない経路（Claude Code の prepare/finalize）と、料金を抑える仕組みのテスト。"""

import json
from datetime import date, timedelta

import pytest

from kabu import ai, config, costs, jpx, pipeline, quant, store
from kabu.pipeline import Settings

from .fakes import FakeClient


def _audit_for(codes, extra_text=""):
    return {
        "theme_overview": {"title": "液冷", "thesis": "構造変化。", "horizon": "1〜3年", "drivers": ["a"], "headwinds": ["b"],
                           "invalidation": ["c"], "value_chain": ["d"]},
        "environment_note": "調整局面。",
        "companies": [{
            "code": c, "name": "社名", "theme_role": "役割", "exposure": "高", "fundamental_change": "受注拡大。" + extra_text,
            "evidence": [{"fact": "受注増", "source": "決算短信 2026-10"}], "quant_audit": "PBRが高い。",
            "priced_in": "高い", "priced_in_reason": "1年で上昇", "bull_points": ["需要"], "bear_points": ["割高"],
            "kpis_to_watch": ["受注残"], "next_checkpoints": ["次回決算"], "priority": "監視", "priority_reason": "割高"}
            for c in codes],
        "cross_check": ["ニュースは良いが需給は過熱"],
        "data_gaps": [],
    }


def test_prepare_and_finalize_claude_code_route():
    pending = pipeline.prepare("6857 7203", theme="液冷", demo=True)
    pdir = pipeline.pending_dir()
    assert (pdir / f"{pending['id']}.json").exists()
    audit_path = pdir / f"{pending['id']}.audit.json"
    audit_path.write_text(json.dumps(_audit_for(["6857", "7203", "9999"], "来週は上昇しそう。"), ensure_ascii=False),
                          encoding="utf-8")
    notes = pdir / "notes.md"
    notes.write_text("液冷の需要が拡大（出典: 各社資料）。目標株価は5,000円。", encoding="utf-8")
    run = pipeline.finalize(pending["id"], notes_path=notes)
    assert run["source"] == "claude-code" and run["cost_usd"] == 0.0
    assert [c["code"] for c in run["companies"]] == ["6857", "7203"]  # 9999 は数値がないので除外
    assert "来週は上昇しそう" not in json.dumps(run["companies"], ensure_ascii=False)
    assert "目標株価" not in run["research_notes"] and len(run["guard_log"]) == 3  # 2社分 + メモ
    assert (config.REPORTS_DIR).exists() and list(config.REPORTS_DIR.glob("*.md"))
    assert not (pdir / f"{pending['id']}.json").exists()  # 保留ファイルは片付く
    assert store.load_runs()[0]["source"] == "claude-code"


def test_finalize_rejects_invalid_audit():
    pending = pipeline.prepare("6857", demo=True)
    bad = _audit_for(["6857"])
    bad["companies"][0]["priority"] = "買い"  # 売買推奨の値は enum で弾く
    del bad["cross_check"]
    (pipeline.pending_dir() / f"{pending['id']}.audit.json").write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(RuntimeError) as e:
        pipeline.finalize(pending["id"])
    assert "priority" in str(e.value) and "cross_check" in str(e.value)


def test_regex_company_extraction_skips_api_call():
    notes = "メモ本文。\n【関連企業】\n- 6367 ダイキン工業 | 冷却装置 | 高 | データセンター向け空調\n- 6501 日立製作所 | 電力 | 中 | 変電設備\n- 不正な行"
    parsed = ai.parse_companies(notes)
    assert [c["code"] for c in parsed] == ["6367", "6501"] and parsed[0]["exposure"] == "高"

    class NotesClient(FakeClient):
        def _stream(self, **kw):
            if "tools" in kw:
                self.calls.append(kw)
                from .fakes import _Stream, _msg, text
                return _Stream(_msg([text(notes)]))
            return super()._stream(**kw)

    client = NotesClient(companies=[("6367", "ダイキン工業"), ("6501", "日立製作所")])
    run = pipeline.analyze_theme("液冷", s=Settings(demo=True), on_status=lambda s: None, client=client)
    schemas = [c.get("output_config", {}).get("format", {}).get("schema") for c in client.calls]
    assert ai.COMPANIES_SCHEMA not in schemas  # 抽出のAI呼び出しが省略された
    assert {c["code"] for c in run["companies"]} == {"6367", "6501"}
    assert run["cost_usd"] > 0


def test_haiku_uses_basic_tools_and_no_fallbacks():
    client = FakeClient(companies=[("6857", "アドバンテスト")], pause_first=False)
    pipeline.audit_stocks("6857", s=Settings(demo=True, model="claude-haiku-5-5"), on_status=lambda s: None, client=client)
    assert all("fallbacks" not in c and "betas" not in c for c in client.calls)
    tools = [c for c in client.calls if "tools" in c][0]["tools"]
    assert {t["type"] for t in tools} == {"web_search_20250305", "web_fetch_20250910"}
    assert all(c["model"] == "claude-haiku-5-5" for c in client.calls)


def test_cheap_defaults_and_caching():
    client = FakeClient(companies=[("6857", "アドバンテスト")], pause_first=False)
    pipeline.audit_stocks("6857", s=Settings(demo=True), on_status=lambda s: None, client=client)
    research = [c for c in client.calls if "tools" in c][0]
    assert research["cache_control"] == {"type": "ephemeral"}
    assert research["output_config"]["effort"] == "low"
    assert {t["name"]: t["max_uses"] for t in research["tools"]} == {"web_search": 3, "web_fetch": 2}


def test_budget_blocks_api_runs(monkeypatch):
    monkeypatch.setattr(config, "MONTHLY_BUDGET_USD", 0.001)
    pipeline.audit_stocks("6857", s=Settings(demo=True, use_web=False), on_status=lambda s: None,
                          client=FakeClient(companies=[("6857", "x")]))
    assert costs.month_spend(store.load_runs()) > 0.001
    with pytest.raises(pipeline.BudgetError):
        pipeline.audit_stocks("6857", s=Settings(demo=True), on_status=lambda s: None, client=FakeClient())


def test_usage_cost():
    u = {"input_tokens": 1_000_000, "output_tokens": 100_000, "web_search_requests": 10}
    assert costs.usage_cost(u, ["claude-opus-5-5"]) == pytest.approx(4 + 2 + 0.1)
    assert costs.usage_cost(u, ["claude-haiku-5-5"]) == pytest.approx(0.1 + 0.05 + 0.1)


# ---------------------------------------------------------------- JPX 無料データの解析

def test_jpx_margin_row_with_broken_numbers():
    # 実際のPDFで見られた崩れ方: "▲ 2 4,000"（= ▲24,000）, "23, 731"
    toyota = "2,439,000 ▲ 323,400 0.0% 8,064,300 51,100 0.1% 231,000 ▲ 2 4,000 2,208,000 ▲ 299,400 4,395,600 147,200 3,668,700 ▲ 9 6,100"
    v = jpx.parse_margin_row(toyota)
    assert v[0] == 2_439_000 and v[3] == 8_064_300 and v[4] == 51_100 and v[5] == "0.1%"
    etf = "4,506 1,558 * 45,008 ▲ 2,900 * 0 0 4,506 1,558 21,277 ▲ 729 23, 731 ▲ 2,171"
    v = jpx.parse_margin_row(etf)
    assert v[3] == 45_008 and v[12] == 23_731
    assert jpx.parse_margin_row("1,000 0 0.0% 2,000 0 0.1% 1 2 3 4 5 6 7 8") is None  # 検算が合わなければ捨てる
    new_listing = "0 - 0.0% 326,300 - 8.1% 0 - 0 - 326,300 - 0 -"
    assert jpx.parse_margin_row(new_listing)[3] == 326_300


def test_fin_metrics_eps_bps_and_estimated_next_disclosure():
    end = date.today() - timedelta(days=50)
    end = end.replace(day=1) - timedelta(days=1)  # 先月以前の月末
    rows = [{"DiscDate": (end + timedelta(days=40)).isoformat(), "CurPerType": "2Q", "CurPerEn": end.isoformat(),
             "CurFYEn": "2099-03-31", "Sales": "1000", "OP": "100", "FOP": "200", "FEPS": "150", "BPS": "1200"}]
    m = quant.fin_metrics(rows, price=3000)
    assert m["会社予想EPS(円)"] == 150 and m["BPS(円)"] == 1200
    ev = quant.estimate_next_disclosure(rows)
    assert ev["次回決算予定日"].endswith("頃（推定）") and ev["次回決算まで(日)"] >= 0
