"""テスト用の偽 Anthropic クライアント（APIを呼ばずに応答を返す）。"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

from kabu import ai


def _msg(blocks, stop="end_turn", searches=0):
    usage = NS(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0, cache_creation_input_tokens=0,
               server_tool_use=NS(web_search_requests=searches, web_fetch_requests=0))
    return NS(content=blocks, stop_reason=stop, usage=usage, model="claude-opus-5-5", stop_details=None)


def text(t):
    return NS(type="text", text=t)


class _Stream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeClient:
    """リクエストの種類（スキーマ・ツールの有無）を見て、それらしい応答を返す。"""

    def __init__(self, companies=None, pause_first=True, audit_extra=""):
        self.calls: list[dict] = []
        self.companies = companies or [("6857", "アドバンテスト"), ("5803", "フジクラ")]
        self.pause_first = pause_first
        self.audit_extra = audit_extra
        self.beta = NS(messages=NS(stream=self._stream))

    def _stream(self, **kw):
        self.calls.append(kw)
        fmt = (kw.get("output_config") or {}).get("format")
        if fmt is None:  # リサーチ（Web検索あり/なし）
            if self.pause_first and len([c for c in self.calls if "tools" in c]) == 1 and "tools" in kw:
                return _Stream(_msg([text("途中経過。")], stop="pause_turn", searches=3))
            return _Stream(_msg([text("液冷の需要が拡大（出典: 各社決算説明資料 2026-10）。来週は上昇しそう。")], searches=2))
        schema = fmt["schema"]
        if schema is ai.THEMES_SCHEMA:
            out = {"themes": [{
                "title": "AIデータセンター拡大による液冷需要", "thesis": "発熱密度の上昇で液冷が標準に。",
                "horizon": "1〜3年", "drivers": ["GPU電力増"], "headwinds": ["投資の反動"], "value_chain": ["冷却装置"],
                "evidence": [{"fact": "設備投資増", "source": "各社資料"}], "crowdedness": "広く知られている",
                "example_companies": [{"code": "6367", "name": "ダイキン工業", "reason": "空調"}]}]}
        elif schema is ai.COMPANIES_SCHEMA:
            out = {"companies": [{"code": c, "name": n, "segment": "部材", "exposure": "高", "linkage": "根拠"}
                                 for c, n in self.companies] + [{"code": "XXXX1", "name": "不正", "segment": "", "exposure": "不明", "linkage": ""}]}
        elif schema is ai.AUDIT_SCHEMA:
            out = {
                "theme_overview": {"title": "液冷", "thesis": "構造変化。", "horizon": "1〜3年", "drivers": ["a"],
                                   "headwinds": ["b"], "invalidation": ["c"], "value_chain": ["d"]},
                "environment_note": "市場全体は調整局面。",
                "companies": [{
                    "code": c, "name": n, "theme_role": "役割", "exposure": "高",
                    "fundamental_change": "受注が拡大。" + self.audit_extra, "evidence": [{"fact": "受注増", "source": "決算短信"}],
                    "quant_audit": "PBRが高く割高感。目標株価は5,000円。", "priced_in": "高い", "priced_in_reason": "1年で大きく上昇",
                    "bull_points": ["需要拡大"], "bear_points": ["今が買い時という声もあるが過熱"], "kpis_to_watch": ["受注残"],
                    "next_checkpoints": ["次回決算"], "priority": "監視", "priority_reason": "割高"}
                    for c, n in self.companies] + [{
                    "code": "0000", "name": "存在しない", "theme_role": "", "exposure": "不明", "fundamental_change": "",
                    "evidence": [], "quant_audit": "", "priced_in": "判断不能", "priced_in_reason": "", "bull_points": [],
                    "bear_points": [], "kpis_to_watch": [], "next_checkpoints": [], "priority": "優先度低", "priority_reason": ""}],
                "cross_check": ["ニュースは良いが需給は過熱。"],
                "data_gaps": ["信用残なし"],
            }
        else:
            raise AssertionError("unknown schema")
        return _Stream(_msg([NS(type="thinking", thinking=""), text(json.dumps(out, ensure_ascii=False))]))
