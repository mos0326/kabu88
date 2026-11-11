"""コマンドラインから使う。

  python -m kabu.cli themes                         # AIに中長期テーマを提案させる
  python -m kabu.cli theme "AIデータセンター拡大による液冷需要"
  python -m kabu.cli audit 6857 5803                # 個別銘柄の監査
  python -m kabu.cli quant                          # 銘柄リストの数値表（AIなし）
  python -m kabu.cli check                          # J-Quants の接続テスト
"""

from __future__ import annotations

import argparse
import json

from . import config
from .pipeline import Settings, analyze_theme, audit_stocks, propose_themes


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


def main() -> None:
    ap = argparse.ArgumentParser(description="kabu88: 中長期テーマ分析と数値監査")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("themes", help="AIに中長期テーマを提案させる")
    p.add_argument("--n", type=int, default=5)
    p.add_argument("--focus", default="")
    p = sub.add_parser("theme", help="テーマを分析する")
    p.add_argument("theme")
    p.add_argument("--include", default="", help="必ず含める証券コード（カンマ区切り）")
    p.add_argument("--max", type=int, default=8)
    p = sub.add_parser("audit", help="個別銘柄を監査する")
    p.add_argument("codes", nargs="+")
    sub.add_parser("quant", help="銘柄リストの数値表（AIなし）")
    sub.add_parser("check", help="J-Quants の接続テスト")
    for sp in sub.choices.values():
        sp.add_argument("--no-web", action="store_true", help="Web検索を使わない")
        sp.add_argument("--no-jquants", action="store_true", help="J-Quants を使わない")
        sp.add_argument("--tdnet", action="store_true", help="決算短信PDFを自動取得してAIに読ませる")
        sp.add_argument("--demo", action="store_true", help="合成データで動作確認")
    a = ap.parse_args()

    s = Settings(use_web=not a.no_web, use_jquants=not a.no_jquants, attach_tdnet=a.tdnet,
                 demo=a.demo or config.DEMO_MODE, max_companies=getattr(a, "max", 8))
    if a.cmd == "check":
        from .jquants import JQuants
        if not config.has_jquants():
            print("JQUANTS_API_KEY が .env に設定されていません。")
            return
        for k, v in JQuants().check_access().items():
            print(f"{k}: {v}")
    elif a.cmd == "quant":
        from .quant import universe_table
        df, gaps = universe_table(demo=s.demo, use_jquants=s.use_jquants, on_status=print)
        print(df.to_string(index=False))
        print("\n".join(gaps))
    elif a.cmd == "themes":
        r = propose_themes(a.n, a.focus, s)
        print(json.dumps(r["themes"], ensure_ascii=False, indent=1))
    elif a.cmd == "theme":
        _print_run(analyze_theme(a.theme, a.include, s=s))
    elif a.cmd == "audit":
        _print_run(audit_stocks(a.codes, s=s))


if __name__ == "__main__":
    main()
