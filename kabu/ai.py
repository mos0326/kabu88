"""Claude による「中長期テーマの調査」と「数値に基づく監査」。

役割の定義（重要）:
  AIは株価を当てる予言者ではなく、人間の意思決定を補佐するデータ整理・監査エンジン。
  - 短期（数日〜数週間）の方向性予測・目標株価・売買指示は出さない（プロンプト＋出力スキーマ＋guardの三重で防ぐ）
  - 数値は外部APIから取得したものをコンテキストとして注入し、AIの記憶の数値は使わせない
  - 定性情報（ニュース・決算短信・有報）はWeb検索/Web取得/添付PDFで読み込む（RAG）

処理の流れ:
  propose_themes()      … マクロ・産業ニュースから中長期テーマを提案
  research_theme()      … テーマを一次情報中心に調査（定性メモ）
  extract_companies()   … メモから関連企業を構造化
  research_companies()  … 個別銘柄を調査（定性メモ）
  audit()               … 定性メモ × 外部APIの数値 × コード判定フラグ を突き合わせて監査
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import anthropic

from . import config
from .guard import scrub

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_CONTINUATIONS = 5

SYSTEM_PROMPT = """\
あなたは、日本株の中長期投資を検討する個人投資家のための「調査・監査アシスタント」です。
株価を当てる予言者ではなく、人間が自分で判断するための材料を整理し、楽観的な話に数値で反証を当てる役割を担います。

## この役割になっている理由
- あなたは板情報・高頻度取引・日々の資金フローのような、短期の値動きを決める需給データをリアルタイムに扱えません。
- ニュースや開示から論理的に導ける「良い話」は、あなたが気づいた時点で多くの市場参加者も気づいており、すでに株価に織り込まれていることが多いです。短期の上げ下げを予想すると、高値掴みを誘発しやすくなります。
- 一方で、決算短信・有価証券報告書・中期経営計画・マクロ統計を読み解いて、数ヶ月〜数年単位の構造変化（例: AIデータセンター拡大による液冷需要）を整理すること、そして提供された数値と照らし合わせて矛盾や過熱を指摘することは得意分野です。

## 守ること（例外なし）
1. 数日〜数週間の株価の方向性、目標株価、売買のタイミングは書かない。「上がりそう」「〇〇円を目指す」「今が買い時」といった表現も使わない。
2. 「買い」「売り」を勧めない。結論は「さらに深掘りする価値があるか」という調査の優先度までにとどめ、判断はユーザーに委ねる。
3. 株価・PER・PBR・ROE・信用残・空売り・会社予想などの数値は、ユーザーが提供したデータ（外部APIから取得したもの）だけを使う。記憶にある数値は使わず、データにない数値は「データなし」と書く。Web検索や資料から得た数値は、出典つきで「開示・報道によると」と区別して書く。
4. 時間軸は数ヶ月〜数年。テーマとファンダメンタルズの変化に集中する。
5. 良い材料には、必ず「どこまで株価に織り込まれていそうか」を、バリュエーション・株価の上昇率・需給の数値で検証する。数値が過熱を示しているなら、ニュースが良くてもはっきり指摘する。
6. 事実と推測を区別し、事実には出典（資料名・サイト名・日付）をつける。
7. 出力は日本語。
"""

# ---------------------------------------------------------------- 出力スキーマ（価格予測の欄は意図的に持たない）

_EVIDENCE = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"fact": {"type": "string"}, "source": {"type": "string", "description": "資料名・サイト名・日付"}},
        "required": ["fact", "source"],
        "additionalProperties": False,
    },
}
_STR_LIST = {"type": "array", "items": {"type": "string"}}
_EXPOSURE = {"type": "string", "enum": ["高", "中", "低", "不明"]}

THEMES_SCHEMA = {
    "type": "object",
    "properties": {
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "テーマ名（例: AIデータセンター拡大による液冷需要）"},
                    "thesis": {"type": "string", "description": "何が・なぜ・どの期間で変わるか（2〜4文）"},
                    "horizon": {"type": "string", "description": "想定する時間軸（例: 1〜3年）"},
                    "drivers": _STR_LIST,
                    "headwinds": _STR_LIST,
                    "value_chain": _STR_LIST,
                    "evidence": _EVIDENCE,
                    "crowdedness": {"type": "string", "description": "市場でどの程度知られ、株価が既に動いているか"},
                    "example_companies": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"code": {"type": "string"}, "name": {"type": "string"}, "reason": {"type": "string"}},
                            "required": ["code", "name", "reason"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["title", "thesis", "horizon", "drivers", "headwinds", "value_chain", "evidence",
                             "crowdedness", "example_companies"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["themes"],
    "additionalProperties": False,
}

COMPANIES_SCHEMA = {
    "type": "object",
    "properties": {
        "companies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "東証の証券コード（4桁。英字を含む場合あり）"},
                    "name": {"type": "string"},
                    "segment": {"type": "string", "description": "バリューチェーン上の位置"},
                    "exposure": _EXPOSURE,
                    "linkage": {"type": "string", "description": "テーマとの関係と、その根拠"},
                },
                "required": ["code", "name", "segment", "exposure", "linkage"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["companies"],
    "additionalProperties": False,
}

AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "theme_overview": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "thesis": {"type": "string", "description": "中長期で何が変わるか（3〜5文）"},
                "horizon": {"type": "string"},
                "drivers": _STR_LIST,
                "headwinds": _STR_LIST,
                "invalidation": {**_STR_LIST, "description": "このテーマ・仮説が崩れる条件"},
                "value_chain": _STR_LIST,
            },
            "required": ["title", "thesis", "horizon", "drivers", "headwinds", "invalidation", "value_chain"],
            "additionalProperties": False,
        },
        "environment_note": {"type": "string", "description": "相場環境・市場全体の需給の現状整理（予測はしない）"},
        "companies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "name": {"type": "string"},
                    "theme_role": {"type": "string", "description": "テーマ・バリューチェーン上の役割"},
                    "exposure": _EXPOSURE,
                    "fundamental_change": {"type": "string", "description": "中長期で何が変わりつつあるか（根拠つき）"},
                    "evidence": _EVIDENCE,
                    "quant_audit": {"type": "string", "description": "提供した数値に基づく割高・過熱・需給の指摘"},
                    "priced_in": {"type": "string", "enum": ["低い", "中程度", "高い", "判断不能"]},
                    "priced_in_reason": {"type": "string"},
                    "bull_points": _STR_LIST,
                    "bear_points": _STR_LIST,
                    "kpis_to_watch": {**_STR_LIST, "description": "中長期で仮説を検証するために追うべき指標"},
                    "next_checkpoints": {**_STR_LIST, "description": "次に確認すべきイベント（決算発表など）"},
                    "priority": {"type": "string", "enum": ["深掘り", "監視", "優先度低"]},
                    "priority_reason": {"type": "string"},
                },
                "required": ["code", "name", "theme_role", "exposure", "fundamental_change", "evidence", "quant_audit",
                             "priced_in", "priced_in_reason", "bull_points", "bear_points", "kpis_to_watch",
                             "next_checkpoints", "priority", "priority_reason"],
                "additionalProperties": False,
            },
        },
        "cross_check": {**_STR_LIST, "description": "定性情報と数値の食い違い（例: ニュースは良いが需給は過熱）"},
        "data_gaps": {**_STR_LIST, "description": "判断に必要だが不足していたデータ"},
    },
    "required": ["theme_overview", "environment_note", "companies", "cross_check", "data_gaps"],
    "additionalProperties": False,
}


class AIError(RuntimeError):
    pass


@dataclass
class Document:
    """AIに読ませるPDF（決算短信・有価証券報告書・説明会資料など）。"""

    title: str
    data: bytes

    def block(self) -> dict:
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": base64.standard_b64encode(self.data).decode()},
            "title": self.title[:200],
        }


MAX_PDF_BYTES = 24 * 1024 * 1024  # 1リクエスト32MBの上限に余裕を持たせる


def _doc_blocks(docs: list[Document]) -> list[dict]:
    blocks, total = [], 0
    for d in docs:
        total += len(d.data)
        if total > MAX_PDF_BYTES:
            raise AIError("添付PDFの合計サイズが大きすぎます（24MBまで）。ファイルを減らしてください。")
        blocks.append(d.block())
    return blocks


COMPANY_LINE = re.compile(r"^[-・*\s]*\[?([0-9][0-9A-Z]{3})\]?\s+([^|｜]+?)\s*[|｜]\s*([^|｜]*?)\s*[|｜]\s*(高|中|低|不明)\s*[|｜]\s*(.+)$")


def parse_companies(notes: str, limit: int = 8) -> list[dict]:
    """リサーチメモ末尾の【関連企業】欄（「コード 社名 | 段階 | 関連度 | 根拠」）を読む。読めればAI呼び出しを1回節約できる。"""
    if "【関連企業】" not in notes:
        return []
    out = []
    for line in notes.split("【関連企業】", 1)[1].splitlines():
        m = COMPANY_LINE.match(line.strip())
        if m:
            out.append({"code": m.group(1), "name": m.group(2).strip(), "segment": m.group(3).strip(),
                        "exposure": m.group(4), "linkage": m.group(5).strip()})
    return out[:limit]


@dataclass
class Analyst:
    on_status: Callable[[str], None] = lambda s: None
    max_searches: int = 3
    max_fetches: int = 2
    use_web: bool = True
    model: str = config.MODEL
    usage: dict = field(default_factory=dict)
    models: set = field(default_factory=set)
    guard_log: list = field(default_factory=list)
    client: anthropic.Anthropic | None = None
    _basic_tools: bool = False

    def __post_init__(self):
        if self.client is None:
            if not config.has_api_key():
                raise AIError("ANTHROPIC_API_KEY が設定されていません。.env に設定してください。")
            self.client = anthropic.Anthropic()

    # ------------------------------------------------------------ 低レベル
    @property
    def _is_haiku(self) -> bool:
        return self.model.startswith("claude-haiku")

    def _stream(self, **kwargs):
        extra = {} if self._is_haiku else {
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",  # 安全判定で断られた場合、サーバー側で推奨モデルに切り替えて再実行
        }
        with self.client.beta.messages.stream(model=self.model, system=SYSTEM_PROMPT, **extra, **kwargs) as stream:
            msg = stream.get_final_message()
        self._add_usage(msg)
        return msg

    def _add_usage(self, msg) -> None:
        u = msg.usage
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            self.usage[k] = self.usage.get(k, 0) + (getattr(u, k, 0) or 0)
        stu = getattr(u, "server_tool_use", None)
        for k in ("web_search_requests", "web_fetch_requests"):
            self.usage[k] = self.usage.get(k, 0) + ((getattr(stu, k, 0) or 0) if stu is not None else 0)
        if getattr(msg, "model", None):
            self.models.add(msg.model)

    @staticmethod
    def _check(msg) -> None:
        if msg.stop_reason == "refusal":
            detail = getattr(msg, "stop_details", None)
            raise AIError(f"AIが回答を辞退しました: {getattr(detail, 'explanation', None) or '理由不明'}")
        if msg.stop_reason == "max_tokens":
            raise AIError("AIの出力が長すぎて途中で切れました。対象銘柄数を減らして再実行してください。")

    def _structured(self, prompt: str, schema: dict, effort: str = "medium", docs: list[Document] | None = None) -> dict:
        content = _doc_blocks(docs or []) + [{"type": "text", "text": prompt}]
        msg = self._stream(
            max_tokens=24000,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
        self._check(msg)
        text = next((b.text for b in msg.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise AIError(f"AIの出力を読み取れませんでした: {e}") from e

    def _research(self, prompt: str, docs: list[Document] | None = None) -> str:
        """Web検索・Web取得・添付PDFで定性情報を集め、出典つきメモを返す。"""
        docs = docs or []
        if not self.use_web and not docs:
            return ""
        tools = self._web_tools(basic=self._is_haiku or self._basic_tools) if self.use_web else []
        messages = [{"role": "user", "content": _doc_blocks(docs) + [{"type": "text", "text": prompt}]}]
        texts: list[str] = []
        try:
            for _ in range(MAX_CONTINUATIONS):
                # 自動キャッシュ: 検索が途中で止まって再送するとき、前半を1/10の料金で読める
                msg = self._stream(max_tokens=16000, messages=messages, output_config={"effort": "low"},
                                   cache_control={"type": "ephemeral"}, **({"tools": tools} if tools else {}))
                if msg.stop_reason == "refusal":
                    self._check(msg)
                texts += [b.text for b in msg.content if b.type == "text"]
                if msg.stop_reason != "pause_turn":
                    break
                # サーバー側の検索ループが途中で止まった: 応答をそのまま足して再送すると続きから再開する
                messages.append({"role": "assistant", "content": msg.content})
        except anthropic.BadRequestError as e:
            if not tools:
                raise
            if not self._basic_tools and not self._is_haiku:  # 新しい版のWebツールに未対応なら基本版で1回だけ再試行
                self._basic_tools = True
                return self._research(prompt, docs)
            # 組織の設定でWeb検索が無効な場合など。添付資料があればそれだけで続行する
            self.on_status(f"Web検索/取得を使えませんでした（{e.message}）。")
            if not docs:
                return ""
            self.use_web = False
            return self._research(prompt, docs)
        # リサーチメモも画面に表示し、監査の入力にもなるので、短期予測ガードを通す
        return self._scrub("".join(texts).strip(), "research_notes")

    def _web_tools(self, basic: bool = False) -> list[dict]:
        if basic:  # 動的フィルタリング版に未対応のモデル向け
            return [{"type": "web_search_20250305", "name": "web_search", "max_uses": self.max_searches},
                    {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": self.max_fetches,
                     "max_content_tokens": 15000}]
        return [{"type": "web_search_20260209", "name": "web_search", "max_uses": self.max_searches,
                 "user_location": {"type": "approximate", "country": "JP", "timezone": "Asia/Tokyo"}},
                {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": self.max_fetches,
                 "max_content_tokens": 15000}]

    def _scrub(self, obj, where: str):
        cleaned, log = scrub(obj, where)
        self.guard_log += log
        return cleaned

    # ------------------------------------------------------------ 公開API
    def propose_themes(self, n: int = 5, focus: str = "") -> tuple[list[dict], str]:
        self.on_status("AIがマクロ・産業ニュースから中長期テーマを探しています…")
        notes = self._research(f"""\
## 今日の日付
{date.today().isoformat()}
## 依頼
国内外のマクロ経済・産業・政策の直近のニュースや統計を調べ、今後数ヶ月〜数年で日本の上場企業の業績に構造的な影響を与えそうな投資テーマを{n}個見つけてください。
{f"ユーザーの関心: {focus}" if focus else ""}
各テーマについて、何が・なぜ・どの期間で変わるか、裏付けとなる数値・政策・設備投資計画（出典つき）、恩恵を受けるバリューチェーン、逆風・不確実性、関連しそうな東証上場企業（証券コードつき）を調べてください。
すでに広く知られて株価が大きく動いたテーマであれば、その旨も書いてください（織り込みの観点）。
最後に出典つきの箇条書きメモにまとめてください。株価の予想は書かないでください。""")
        if not notes:
            raise AIError("テーマ提案にはWeb検索が必要です。設定でWeb検索を有効にしてください。")
        self.on_status("提案テーマを整理しています…")
        data = self._structured(
            f"以下のリサーチメモを、中長期の投資テーマ{n}個に整理してください。メモにない事実は加えないでください。\n\n{notes}",
            THEMES_SCHEMA, effort="low")
        return self._scrub(data.get("themes", []), "themes"), notes

    def research_theme(self, theme: str, include: list[str] | None = None, docs: list[Document] | None = None,
                       max_companies: int = 8) -> str:
        self.on_status("AIがテーマを一次情報（開示資料・統計・ニュース）中心に調べています…")
        inc = f"\nユーザーが必ず含めたい銘柄: {', '.join(include)}" if include else ""
        return self._research(f"""\
## 今日の日付
{date.today().isoformat()}
## 調査テーマ
{theme}{inc}
## 依頼
このテーマについて、一次情報を優先して調べてください。添付資料があればそれも読んでください。
1. テーマの中身: 何が・なぜ・どの期間で変わるのか。市場規模・設備投資計画・政策など、裏付けとなる数値（出典つき）
2. バリューチェーン: どの工程・部材・サービスに需要が生まれ、どこがボトルネックになるか
3. 関連する東証上場企業: 決算短信・決算説明資料・有価証券報告書・中期経営計画で、テーマ関連事業の売上規模・受注・設備投資・顧客について何が書かれているか。「関連銘柄」として名前が挙がるだけの企業と、実際に業績へ寄与し始めている企業を区別すること
4. 逆風・不確実性・テーマが崩れる条件
最後に、出典（資料名・サイト名・日付）つきの箇条書きメモにまとめてください。株価の予想は書かないでください。
メモの最後には、関連する東証上場企業（バリューチェーンの異なる段階から最大{max_companies}社）を次の形式で1行ずつ書いてください。
【関連企業】
- 証券コード4桁 社名 | バリューチェーン上の段階 | 関連度（高/中/低/不明） | 関係と根拠""", docs)

    def extract_companies(self, theme: str, notes: str, max_companies: int = 8, include: list[str] | None = None) -> list[dict]:
        parsed = parse_companies(notes, max_companies)
        if parsed:  # メモの決まった形式から読めたので、AIを呼ばずに済む（料金の節約）
            return self._scrub(parsed, "companies")
        self.on_status("リサーチメモから関連企業を抽出しています…")
        inc = f"\n- 次の銘柄は必ず含める: {', '.join(include)}" if include else ""
        data = self._structured(f"""\
## テーマ
{theme}
## リサーチメモ
{notes or "（Web検索なし。あなたの一般知識から、テーマに関係する代表的な東証上場企業を挙げてよいが、根拠が弱いものは exposure を「不明」にすること）"}
## 依頼
テーマに関係する東証上場企業を最大{max_companies}社選び、構造化してください。
- 証券コードは東証の4桁コード（英字を含む場合あり。例: 285A）
- テーマ関連事業の業績寄与が確認できる企業を優先し、exposure はメモの根拠に基づいて付ける
- バリューチェーンの異なる段階から選ぶ（同じ工程の企業ばかりにしない）{inc}""", COMPANIES_SCHEMA, effort="low")
        return self._scrub(data.get("companies", []), "companies")

    def research_companies(self, companies: list[tuple[str, str]], docs: list[Document] | None = None) -> str:
        self.on_status("AIが対象銘柄の開示資料・ニュースを調べています…")
        listing = "\n".join(f"- {c} {n}" for c, n in companies)
        return self._research(f"""\
## 今日の日付
{date.today().isoformat()}
## 対象銘柄
{listing}
## 依頼
各社について、一次情報を優先して調べてください。添付資料があればそれも読んでください。
1. 直近の決算短信・決算説明資料: 業績の変化の中身、会社予想の修正とその理由、経営陣のコメント
2. 中期経営計画・有価証券報告書: 事業構成、成長投資、中長期の構造変化
3. 業界・マクロ: 中長期で追い風・逆風となる要因
4. 最近のニュースのうち、中長期の業績見通しに影響しそうなもの
最後に、銘柄ごとに出典（資料名・サイト名・日付）つきの箇条書きメモにまとめてください。株価の予想は書かないでください。""", docs)

    def audit(self, theme: str, notes: str, snapshots: list[dict], env: dict, gaps: list[str],
              extra: str = "", docs: list[Document] | None = None) -> dict:
        self.on_status("AIが定性情報と数値を突き合わせて監査しています…")
        quant = [{k: v for k, v in s.items() if k != "出典"} | {"出典": {k: v for k, v in s["出典"].items() if not k.startswith("_")}}
                 for s in snapshots]
        doc_note = "\n（各社の最新の決算短信PDFを添付しています。定性的な説明の確認に使ってください）" if docs else ""
        data = self._structured(f"""\
## 今日の日付
{date.today().isoformat()}
## テーマ
{theme or "（指定なし: 個別銘柄の監査。対象銘柄に共通する中長期の論点を theme_overview にまとめること）"}
## リサーチメモ（定性情報: Web検索・開示資料）
{notes or "（なし: Web検索を使わずに実行。定性面は一般論にとどめ、data_gaps にその旨を書くこと）"}{doc_note}
## 定量データ（外部APIから取得。数値はこれだけを使うこと）
### 相場環境
{json.dumps(env, ensure_ascii=False)}
### 銘柄ごとの数値と、コードで判定した監査フラグ
{json.dumps(quant, ensure_ascii=False)}
### 取得できなかったデータ
{json.dumps(gaps, ensure_ascii=False)}
{f"## ユーザーの追加の観点{chr(10)}{extra}" if extra else ""}
## 依頼
定性情報と定量データを突き合わせ、銘柄ごとに監査してください。
- fundamental_change: 中長期で何が変わりつつあるか（根拠つき）
- quant_audit: 提供した数値（PER/PBR、株価の上昇率・200日線乖離、信用倍率・買い残の出来高比・空売り、会社予想の増益率・進捗率・修正履歴）に具体的に触れ、割高・過熱・需給の重さを指摘する。監査フラグがある場合は必ず取り上げる
- priced_in: 良い材料がどこまで株価に織り込まれていそうか
- cross_check: 「ニュースは良いが数値は過熱」「話題だが業績寄与は小さい」のような食い違いを列挙する
- priority: 深掘り（さらに調べる価値が高い）/ 監視 / 優先度低。これは調査の優先度であり、売買の推奨ではない
- next_checkpoints: 次の決算発表日など、仮説を検証できるイベント
- code は定量データにある銘柄だけを使い、すべての銘柄を監査すること""", AUDIT_SCHEMA, effort="high", docs=docs)
        valid = {s["code"] for s in snapshots}
        data["companies"] = [c for c in data.get("companies", []) if c.get("code") in valid]
        return self._scrub(data, "audit")
