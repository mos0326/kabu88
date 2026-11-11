"""コマンドラインから使う。

■ API料金なし（Claude Code から使う。.claude/skills の手順が自動で呼ぶ）
  python -m kabu.cli rules                          # AIが守るルールを表示
  python -m kabu.cli schema                         # 監査JSONの形式を表示
  python -m kabu.cli prepare 6367 6501 --theme "AIデータセンター拡大による液冷需要"   # 数値だけ取得
  python -m kabu.cli finalize <id> --notes data/runs/pending/notes.md                 # 検証・ガード・保存
  python -m kabu.cli quant                          # 銘柄リストの数値表（AIなし）

■ Claude API（従量課金）
  python -m kabu.cli themes                         # AIに中長期テーマを提案させる
  python -m kabu.cli theme "AIデータセンター拡大による液冷需要"
  python -m kabu.cli audit 6857 5803
  python -m kabu.cli budget                         # 今月の API 料金

■ その他
  python -m kabu.cli check                          # J-Quants の接続テスト
"""

from __future__ import annotations

import argparse
import json
import sys

from . import config


def _print_run(r: dict) -> None:
    ov = r.get("theme_overview", {})
    print(f"\n=== {ov.get('title', '')}（{ov.get('horizon', '')}） ===\n{ov.get('thesis', '')}")
    for c in r["companies"]:
        a = c.get("audit") or {}
        print(f"\n[{a.get('priority', '-')}] {c['code']} {c['name']}  関連度:{a.get('exposure', '-')}  織り込み:{a.get('priced_in', '-')}")
        for f in c["quant"]["flags"]:
            print(f"   {f['level']}: {f['text']}")
        print(f"   変化: {a.get('fundamental_change', '')}")
        print(f"   数値監査: {a.get('quant_audit', '')}")
    if r.get("cross_check"):
        print("\n=== 定性と数値の食い違い ===\n" + "\n".join(f"- {x}" for x in r["cross_check"]))
    if r.get("guard_log"):
        print(f"\n（短期予測ガードで {len(r['guard_log'])} 文を除去しました）")
    print(f"\n保存: data/runs/{r.get('_file')}　レポート: {r.get('_report')}")
    if r.get("source") == "api":
        print(f"料金の目安: ${r.get('cost_usd', 0):.3f}")


def _compact(pending: dict) -> dict:
    """Claude Code に読ませる数値（出典の内部項目を除く）。"""
    return {
        "id": pending["id"],
        "theme": pending["theme"],
        "environment": pending["environment"],
        "data_gaps": pending["gaps"],
        "companies": [{k: v for k, v in s.items() if k != "出典"}
                      | {"出典": {k: v for k, v in s["出典"].items() if not k.startswith("_")}}
                      for s in pending["snapshots"]],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="kabu88: 中長期テーマ分析と数値監査")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rules", help="AIが守るルール（短期予測の禁止など）を表示")
    sub.add_parser("schema", help="監査JSONの形式（JSON Schema）を表示")
    p = sub.add_parser("prepare", help="[API料金なし] 数値だけ取得して保留中の分析を作る")
    p.add_argument("codes", nargs="+")
    p.add_argument("--theme", default="")
    p = sub.add_parser("finalize", help="[API料金なし] 監査JSONを検証・ガードして保存")
    p.add_argument("id")
    p.add_argument("--audit", default=None, help="監査JSONのパス（既定: data/runs/pending/<id>.audit.json）")
    p.add_argument("--notes", default=None, help="リサーチメモ（Markdown）のパス")
    sub.add_parser("quant", help="銘柄リストの数値表（AIなし）")
    sub.add_parser("check", help="J-Quants の接続テスト")
    sub.add_parser("budget", help="今月の Claude API 料金")
    p = sub.add_parser("themes", help="[API] AIに中長期テーマを提案させる")
    p.add_argument("--n", type=int, default=4)
    p.add_argument("--focus", default="")
    p = sub.add_parser("theme", help="[API] テーマを分析する")
    p.add_argument("theme")
    p.add_argument("--include", default="", help="必ず含める証券コード（カンマ区切り）")
    p.add_argument("--max", type=int, default=5)
    p = sub.add_parser("audit", help="[API] 個別銘柄を監査する")
    p.add_argument("codes", nargs="+")
    for sp in sub.choices.values():
        sp.add_argument("--no-web", action="store_true", help="Web検索を使わない")
        sp.add_argument("--no-jquants", action="store_true", help="J-Quants を使わない")
        sp.add_argument("--tdnet", action="store_true", help="決算短信PDFを自動取得してAIに読ませる")
        sp.add_argument("--model", default=config.MODEL, help="Claude のモデル（API実行時）")
        sp.add_argument("--demo", action="store_true", help="合成データで動作確認")
    a = ap.parse_args()
    demo = a.demo or config.DEMO_MODE

    if a.cmd == "rules":
        from .ai import SYSTEM_PROMPT
        print(SYSTEM_PROMPT)
        return
    if a.cmd == "schema":
        from .ai import AUDIT_SCHEMA
        print(json.dumps(AUDIT_SCHEMA, ensure_ascii=False, indent=1))
        return
    if a.cmd == "prepare":
        from .pipeline import pending_dir, prepare
        pending = prepare(a.codes, a.theme, demo=demo, use_jquants=not a.no_jquants,
                          on_status=lambda s: print(s, file=sys.stderr))
        print(json.dumps(_compact(pending), ensure_ascii=False, default=str))
        print(f"\n保留中の分析ID: {pending['id']}\n監査JSONの保存先: {pending_dir() / (pending['id'] + '.audit.json')}",
              file=sys.stderr)
        return
    if a.cmd == "finalize":
        from .pipeline import finalize
        try:
            _print_run(finalize(a.id, a.audit, a.notes))
        except RuntimeError as e:
            print(f"エラー: {e}", file=sys.stderr)
            sys.exit(1)
        return
    if a.cmd == "check":
        from .jquants import JQuants
        if not config.has_jquants():
            print("JQUANTS_API_KEY が .env に設定されていません。")
            return
        for k, v in JQuants().check_access().items():
            print(f"{k}: {v}")
        return
    if a.cmd == "quant":
        from .quant import universe_table
        df, gaps = universe_table(demo=demo, use_jquants=not a.no_jquants, on_status=print)
        print(df.to_string(index=False))
        print("\n".join(gaps))
        return
    if a.cmd == "budget":
        from . import costs, store
        spent = costs.month_spend(store.load_runs())
        limit = f"${config.MONTHLY_BUDGET_USD:.2f}" if config.MONTHLY_BUDGET_USD > 0 else "なし"
        print(f"今月の Claude API 料金（目安）: ${spent:.2f} / 上限 {limit}")
        return

    from .pipeline import Settings, analyze_theme, audit_stocks, propose_themes
    s = Settings(use_web=not a.no_web, use_jquants=not a.no_jquants, attach_tdnet=a.tdnet, demo=demo,
                 max_companies=getattr(a, "max", 5), model=a.model)
    if a.cmd == "themes":
        r = propose_themes(a.n, a.focus, s)
        print(json.dumps(r["themes"], ensure_ascii=False, indent=1))
        print(f"料金の目安: ${r['cost_usd']:.3f}")
    elif a.cmd == "theme":
        _print_run(analyze_theme(a.theme, a.include, s=s))
    elif a.cmd == "audit":
        _print_run(audit_stocks(a.codes, s=s))


if __name__ == "__main__":
    main()
