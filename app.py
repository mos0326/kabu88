"""kabu88 ダッシュボード: streamlit run app.py

AIは「株価を当てる予言者」ではなく「人間の判断を補佐する、データ整理・監査エンジン」として使う。
- 短期（数日〜数週間）の予測・目標株価・売買指示は出さない
- 数値は外部API（Yahoo Finance / J-Quants）から取得し、過熱・需給のフラグはコードで判定する
- AIは中長期テーマの調査と、定性情報 × 数値の突き合わせ（監査）を担当する
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from kabu import config, costs, store
from kabu.ai import AIError, Document
from kabu.charts import candle
from kabu.data import fetch_prices, load_universe
from kabu.pipeline import Settings, analyze_theme, audit_stocks, propose_themes
from kabu.quant import universe_table

st.set_page_config(page_title="kabu88 テーマ分析・監査", page_icon="🧭", layout="wide")

EXAMPLE_THEMES = [
    "AIデータセンター拡大による液冷需要",
    "データセンター向け電力インフラ（変圧器・送配電）",
    "防衛費増額による国内防衛産業の拡大",
    "インフラ老朽化対策（国土強靱化）",
    "半導体サプライチェーンの国内回帰",
]
PRIORITY_BADGE = {"深掘り": ":green-badge[深掘り]", "監視": ":orange-badge[監視]", "優先度低": ":gray-badge[優先度低]"}
LEVEL_ICON = {"警告": "🔴", "注意": "🟡", "情報": "🔵"}


@st.cache_data(ttl=1800, show_spinner=False)
def prices_for(ticker: str, demo: bool) -> pd.DataFrame | None:
    return fetch_prices([ticker], demo=demo).get(ticker)


@st.cache_data(ttl=900, show_spinner=False)
def cached_universe_table(demo: bool, use_jq: bool):
    return universe_table(demo=demo, use_jquants=use_jq)


@st.cache_data(ttl=600, show_spinner=False)
def jquants_access() -> dict:
    from kabu.jquants import JQuants
    return JQuants().check_access()


def fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, dict):
        return " / ".join(f"{k}: {fmt(x)}" for k, x in v.items())
    if isinstance(v, float):
        return f"{v:,.2f}".rstrip("0").rstrip(".") if abs(v) < 1e6 else f"{v:,.0f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def bullets(items) -> str:
    return "\n".join(f"- {x}" for x in items) if items else "—"


# ---------------------------------------------------------------- サイドバー
with st.sidebar:
    st.title("🧭 kabu88")
    st.caption("中長期テーマの調査と、数値にもとづく監査")

    st.subheader("接続状況")
    demo = st.toggle("デモモード（合成データ）", value=config.DEMO_MODE,
                     help="ネットに繋がずに画面を確認するモード。株価・指標は架空です。")
    st.write("🧪 デモデータ" if demo else "✅ Yahoo Finance（株価・PER/PBR）無料")
    if not demo and config.USE_JPX_FREE:
        st.write("✅ JPX公表データ（信用残・空売り残高）無料")
    st.write(("✅" if config.has_api_key() else "➖") + " Claude API（従量課金・任意）")
    if config.has_jquants():
        st.write(f"✅ J-Quants（{config.JQUANTS_PLAN}プラン）")
        if st.button("J-Quants 接続テスト", width="stretch"):
            with st.spinner("確認しています…"):
                st.session_state["jq_access"] = jquants_access()
        for k, v in st.session_state.get("jq_access", {}).items():
            st.caption(f"{'✅' if v.startswith('利用可') else '⚠️'} {k}: {v}")
    else:
        st.write("➖ J-Quants 未設定（会社予想・進捗率なし）")

    st.subheader("💴 料金")
    spent = costs.month_spend(store.load_runs())
    budget = config.MONTHLY_BUDGET_USD
    st.markdown(f"今月の Claude API 料金（目安）: **${spent:.2f}**"
                + (f" ／ 上限 ${budget:.2f}" if budget > 0 else " ／ 上限なし"))
    if budget > 0:
        st.progress(min(1.0, spent / budget))
    st.caption("Claude Code で `/kabu-theme` を使えば API料金は0円です（サブスクの範囲内）。")

    st.subheader("API実行の設定")
    models = list(costs.MODEL_LABELS)
    model = st.selectbox("モデル", models, index=models.index(config.MODEL) if config.MODEL in models else 0,
                         format_func=lambda m: costs.MODEL_LABELS[m])
    max_companies = st.slider("テーマ分析で調べる企業数（最大）", 2, 10, 5)
    use_web = st.toggle("Web検索・Web取得で一次情報を調べる", value=True)
    max_searches = st.slider("Web検索の最大回数（1回 約1.5円）", 1, 10, 3, disabled=not use_web)
    max_fetches = st.slider("Webページ・PDF取得の最大回数", 0, 5, 2, disabled=not use_web)
    use_jq = st.toggle("J-Quants の数値を使う", value=config.has_jquants(), disabled=not config.has_jquants())
    attach_tdnet = st.toggle("決算短信PDFを自動取得してAIに読ませる", value=False, disabled=not config.has_jquants(),
                             help="J-Quants の TDnet/適時開示情報アドオンが必要です。PDFを読むぶんトークン料金が増えます。")
    tdnet_max = st.slider("決算短信を読ませる企業数", 1, 5, 2, disabled=not attach_tdnet)
    extra = st.text_area("AIに追加で見てほしい観点（任意）", placeholder="例: 為替感応度を重視 / 配当方針も確認して")
    est = costs.estimate(model, use_web, max_searches, max_fetches, max_companies, tdnet_max if attach_tdnet else 0)
    st.caption(f"1回あたりの料金の目安: 最大 約${est:.2f}（約{est * 155:.0f}円）")

settings = Settings(max_companies=max_companies, use_web=use_web, max_searches=max_searches, max_fetches=max_fetches,
                    use_jquants=use_jq, attach_tdnet=attach_tdnet, tdnet_max=tdnet_max, extra=extra, demo=demo,
                    model=model)
over_budget = budget > 0 and spent >= budget

st.info("このツールのAIは**株価を当てません**。短期の値動き・目標株価・売買の指示は出さず、"
        "中長期テーマの調査と、外部APIの数値（バリュエーション・需給・過熱）による監査に徹します。判断するのはあなたです。",
        icon="🧭")
if demo:
    st.warning("デモモードです。株価・指標は架空の合成データです。", icon="🧪")


# ---------------------------------------------------------------- 結果の表示
def render_company(c: dict, run: dict, key: str) -> None:
    q, a = c["quant"], c.get("audit") or {}
    flags = q.get("flags", [])
    with st.container(border=True):
        badges = " ".join(x for x in (PRIORITY_BADGE.get(a.get("priority"), ""),
                                      f":blue-badge[テーマ関連度 {a.get('exposure', '-')}]",
                                      f":violet-badge[織り込み度 {a.get('priced_in', '-')}]") if x)
        st.markdown(f"#### {c['name']}（{c['code']}）　{badges}")
        if a.get("theme_role"):
            st.caption(a["theme_role"])
        left, right = st.columns([5, 6], gap="large")

        with left:
            st.markdown("**📐 数値（外部APIから取得）**")
            for section in ("株価", "バリュエーション", "業績", "需給", "イベント"):
                vals = {k: v for k, v in q.get(section, {}).items() if v is not None}
                if vals:
                    with st.expander(section, expanded=section in ("バリュエーション", "需給")):
                        st.dataframe(pd.DataFrame({"指標": list(vals), "値": [fmt(v) for v in vals.values()]}),
                                     hide_index=True, width="stretch")
            st.caption("出典: " + " / ".join(f"{k}={v}" for k, v in q.get("出典", {}).items() if not k.startswith("_")))
            st.markdown("**🚩 コードで判定した監査フラグ**")
            st.markdown(bullets([f"{LEVEL_ICON[f['level']]} [{f['category']}] {f['text']}" for f in flags])
                        if flags else "目立った過熱・需給の警告なし")

        with right:
            st.markdown("**🤖 AIの監査（定性情報 × 数値）**")
            if not a:
                st.write("AIの監査結果がありません。")
            else:
                st.markdown(f"**中長期の変化**: {a['fundamental_change']}")
                st.markdown(f"**数値の監査**: {a['quant_audit']}")
                st.markdown(f"**織り込み度（{a['priced_in']}）**: {a['priced_in_reason']}")
                b1, b2 = st.columns(2)
                b1.markdown("**強気材料**\n" + bullets(a["bull_points"]))
                b2.markdown("**弱気材料・リスク**\n" + bullets(a["bear_points"]))
                b1.markdown("**追うべきKPI**\n" + bullets(a["kpis_to_watch"]))
                b2.markdown("**次の確認ポイント**\n" + bullets(a["next_checkpoints"]))
                st.markdown(f"**調査の優先度: {a['priority']}** — {a['priority_reason']}")
                if a.get("evidence"):
                    with st.expander("根拠と出典"):
                        st.markdown(bullets([f"{e['fact']}（{e['source']}）" for e in a["evidence"]]))

        with st.expander("チャート（1年・日足）"):
            df = prices_for(f"{c['code']}.T", run.get("demo", False))
            if df is not None:
                st.plotly_chart(candle(df), width="stretch", key=f"{key}_chart")

        if run.get("_file"):
            dec = store.load_decisions().get(f"{run['_file']}:{c['code']}", {})
            d1, d2, d3 = st.columns([2, 5, 1], vertical_alignment="bottom")
            choice = d1.selectbox("あなたの判断", store.DECISIONS, key=f"{key}_dec",
                                  index=store.DECISIONS.index(dec.get("decision", "未判断")))
            memo = d2.text_input("判断メモ", value=dec.get("memo", ""), key=f"{key}_memo",
                                 placeholder="例: 次の決算で受注残を確認してから判断")
            if d3.button("保存", key=f"{key}_save"):
                store.save_decision(run["_file"], c["code"], choice, memo)
                st.toast("判断メモを保存しました")


def render_run(run: dict, key: str) -> None:
    ov = run.get("theme_overview", {})
    st.subheader(f"{ov.get('title') or run.get('theme') or '個別銘柄の監査'}")
    src = "Claude Code（API料金なし）" if run.get("source") == "claude-code" else f"Claude API（{', '.join(run.get('models', []))}）"
    st.caption(f"{run['created_at'][:16].replace('T', ' ')} ・ 時間軸: {ov.get('horizon', '-')} ・ 実行: {src}"
               + (" ・ 🧪デモ" if run.get("demo") else ""))
    if ov.get("thesis"):
        st.markdown(ov["thesis"])
    c1, c2, c3 = st.columns(3)
    c1.markdown("**追い風**\n" + bullets(ov.get("drivers")))
    c2.markdown("**逆風・不確実性**\n" + bullets(ov.get("headwinds")))
    c3.markdown("**仮説が崩れる条件**\n" + bullets(ov.get("invalidation")))
    if ov.get("value_chain"):
        st.markdown("**バリューチェーン**: " + " → ".join(ov["value_chain"]))

    env = run.get("environment", {})
    cols = st.columns(max(1, len(env)))
    for col, (label, m) in zip(cols, env.items()):
        if "終値" in m:
            yr = f"（1年 {m['1年騰落%']:+.1f}%）" if m.get("1年騰落%") is not None else ""
            mo = f"{m['1ヶ月騰落%']:+.1f}%（1ヶ月）" if m.get("1ヶ月騰落%") is not None else None
            col.metric(label + yr, fmt(m["終値"]), mo)
        else:
            col.metric(label, f"{m['値']}%", f"基準日 {m['基準日']}", delta_color="off")
    if run.get("environment_note"):
        st.caption("相場環境: " + run["environment_note"])

    if run.get("cross_check"):
        st.warning("**定性情報と数値の食い違い**\n" + bullets(run["cross_check"]), icon="⚖️")

    rows = []
    for c in run["companies"]:
        q, a = c["quant"], c.get("audit") or {}
        rows.append({"コード": c["code"], "銘柄名": c["name"], "優先度": a.get("priority", "-"),
                     "関連度": a.get("exposure", "-"), "織り込み度": a.get("priced_in", "-"),
                     "警告": sum(f["level"] == "警告" for f in q["flags"]), "注意": sum(f["level"] == "注意" for f in q["flags"]),
                     "1年騰落%": q["株価"].get("1年騰落%"), "200日線乖離%": q["株価"].get("200日線乖離%"),
                     "予想PER": q["バリュエーション"].get("予想PER"), "PBR": q["バリュエーション"].get("PBR"),
                     "信用倍率": q.get("需給", {}).get("信用倍率"), "予想営業増益率%": q.get("業績", {}).get("予想営業増益率%")})
    table = pd.DataFrame(rows)
    for col in table.columns[5:]:
        table[col] = pd.to_numeric(table[col], errors="coerce")  # データなしは空欄で表示
    st.dataframe(table, hide_index=True, width="stretch")

    for c in run["companies"]:
        render_company(c, run, f"{key}_{c['code']}")

    with st.expander("AIのリサーチメモ（Web検索・開示資料）"):
        st.markdown(run.get("research_notes") or "（なし）")
        if run.get("documents"):
            st.caption("読み込んだ資料: " + " / ".join(run["documents"]))
    if run.get("data_gaps"):
        with st.expander(f"不足していたデータ（{len(run['data_gaps'])}件）"):
            st.markdown(bullets(run["data_gaps"]))
    if run.get("guard_log"):
        with st.expander(f"短期予測ガードで除去した文（{len(run['guard_log'])}件）"):
            st.dataframe(pd.DataFrame(run["guard_log"]), hide_index=True, width="stretch")
    u = run.get("usage", {})
    if u:
        cost = run.get("cost_usd") if run.get("cost_usd") is not None else costs.usage_cost(u, run.get("models"))
        st.caption(f"入力 {u.get('input_tokens', 0):,} / 出力 {u.get('output_tokens', 0):,} トークン ・ "
                   f"Web検索 {u.get('web_search_requests', 0)} 回 ・ Web取得 {u.get('web_fetch_requests', 0)} 回 ・ "
                   f"料金の目安 約${cost:.3f}（約{cost * 155:.0f}円）")
    if run.get("_report"):
        st.caption(f"Markdownレポート: `{run['_report']}`")


def uploaded_docs(files) -> list[Document]:
    return [Document(f.name, f.getvalue()) for f in files or []]


def run_with_status(label: str, fn):
    with st.status(label, expanded=True) as status:
        try:
            result = fn(st.write)
            status.update(label="完了しました", state="complete", expanded=False)
            return result
        except AIError as e:
            status.update(label="AIの実行に失敗しました", state="error")
            st.error(str(e))
        except Exception as e:  # ネットワーク・APIのエラーを画面に出す
            status.update(label="エラーが発生しました", state="error")
            st.exception(e)
    return None


need_key = not config.has_api_key()
blocked = need_key or over_budget
CLAUDE_CODE_HINT = ("💡 **API料金をかけない方法**: Claude Code でこのリポジトリを開き、`/kabu-theme テーマ名`（個別銘柄は "
                    "`/kabu-audit 6857 5803`、テーマ提案は `/kabu-themes`）と入力すると、サブスクリプションの範囲内で同じ分析ができます。"
                    "結果は「🗂 履歴と検証」タブに表示されます。")


def api_notice() -> None:
    if over_budget:
        st.error(f"今月の Claude API 料金が上限（${budget:.2f}）に達したため、ボタンからのAI実行を止めています。"
                 "`.env` の KABU_MONTHLY_BUDGET_USD で変更できます。")
    elif need_key:
        st.caption("Claude API キー（`.env` の `ANTHROPIC_API_KEY`）を設定すると、このボタンから直接実行できます（従量課金）。")


def recent_same_theme(theme: str):
    for r in store.load_runs():
        if r.get("theme") == theme and (pd.Timestamp.now() - pd.Timestamp(r["created_at"])).days < 7:
            return r
    return None
tab_theme, tab_audit, tab_quant, tab_hist, tab_univ = st.tabs(
    ["🧭 テーマ分析", "🔍 個別銘柄の監査", "📊 数値チェック（AIなし）", "🗂 履歴と検証", "⚙️ 銘柄リスト"])

# ---------------------------------------------------------------- テーマ分析
with tab_theme:
    st.header("中長期テーマから関連企業を調べる")
    st.caption("テーマ → 一次情報（決算短信・有報・統計・ニュース）の調査 → 関連企業の抽出 → 外部APIの数値取得 → AIが監査")
    st.success(CLAUDE_CODE_HINT)

    with st.expander("💡 テーマが決まっていない場合: AIにテーマを提案させる"):
        f1, f2 = st.columns([4, 1], vertical_alignment="bottom")
        focus = f1.text_input("関心のある分野（任意）", placeholder="例: エネルギー、内需、素材")
        n_themes = f2.number_input("提案数", 2, 8, 4)
        if st.button("AIにテーマを提案させる（API）", disabled=blocked or not use_web):
            res = run_with_status("テーマを探しています…", lambda cb: propose_themes(int(n_themes), focus, settings, on_status=cb))
            if res:
                st.session_state["proposed"] = res
        for i, t in enumerate(st.session_state.get("proposed", {}).get("themes", [])):
            with st.container(border=True):
                st.markdown(f"**{t['title']}**（{t['horizon']}）")
                st.markdown(t["thesis"])
                st.caption(f"市場での注目度: {t['crowdedness']}")
                g1, g2 = st.columns(2)
                g1.markdown("**追い風**\n" + bullets(t["drivers"]))
                g2.markdown("**逆風**\n" + bullets(t["headwinds"]))
                st.caption("関連しそうな企業: " + "、".join(f"{x['name']}({x['code']})" for x in t["example_companies"]))
                if st.button("このテーマを分析する", key=f"use_theme_{i}"):
                    st.session_state["theme_input"] = t["title"]
                    st.rerun()

    picked = st.pills("例", EXAMPLE_THEMES, selection_mode="single", key="theme_pill")
    if picked and st.session_state.get("_last_pill") != picked:
        st.session_state["theme_input"] = picked
        st.session_state["_last_pill"] = picked
    theme = st.text_input("分析するテーマ", key="theme_input", placeholder="例: AIデータセンター拡大による液冷需要")
    include = st.text_input("必ず含めたい銘柄（任意・証券コード）", placeholder="例: 6367 6501")
    files = st.file_uploader("AIに読ませる資料（任意・PDF）: 決算短信・有価証券報告書・決算説明資料など",
                             type=["pdf"], accept_multiple_files=True, key="theme_files")
    same = recent_same_theme(theme.strip()) if theme.strip() else None
    if same:
        st.info(f"このテーマは {same['created_at'][:10]} に分析済みです（「🗂 履歴と検証」タブで無料で見られます）。")
    api_notice()
    st.caption(f"料金の目安: 最大 約${est:.2f}（約{est * 155:.0f}円）")
    if st.button("🧭 このテーマを分析する（API）", type="primary", disabled=blocked or not theme.strip()):
        res = run_with_status("分析しています…", lambda cb: analyze_theme(theme.strip(), include, uploaded_docs(files), settings, on_status=cb))
        if res:
            st.session_state["theme_run"] = res
    if st.session_state.get("theme_run"):
        st.divider()
        render_run(st.session_state["theme_run"], "theme")

# ---------------------------------------------------------------- 個別銘柄の監査
with tab_audit:
    st.header("気になる銘柄を監査する")
    st.caption("外部APIの数値（バリュエーション・需給・過熱・会社予想）を取得し、開示資料やニュースと突き合わせて、楽観的な見方に反証を当てます。")
    st.success(CLAUDE_CODE_HINT)
    uni = load_universe()
    sel = st.multiselect("銘柄リストから選ぶ", [f"{s.code} {s.name}" for s in uni], max_selections=10)
    free = st.text_input("または証券コードを入力（スペース区切り）", placeholder="例: 6857 5803 285A", key="audit_codes")
    codes = [x.split()[0] for x in sel] + free.split()
    files2 = st.file_uploader("AIに読ませる資料（任意・PDF）", type=["pdf"], accept_multiple_files=True, key="audit_files")
    api_notice()
    if st.button("🔍 監査する（API）", type="primary", disabled=blocked or not codes):
        res = run_with_status("監査しています…", lambda cb: audit_stocks(codes, uploaded_docs(files2), settings, on_status=cb))
        if res:
            st.session_state["audit_run"] = res
    if st.session_state.get("audit_run"):
        st.divider()
        render_run(st.session_state["audit_run"], "audit")

# ---------------------------------------------------------------- 数値チェック
with tab_quant:
    st.header("銘柄リスト全体の数値チェック（AIなし・無料）")
    st.caption("過熱・割高・需給のフラグはしきい値でコード判定しています（AIは使いません）。列見出しをクリックすると並べ替えできます。")
    if st.button("📊 数値を取得する"):
        with st.spinner("取得しています…"):
            try:
                st.session_state["quant_table"] = cached_universe_table(demo, use_jq)
            except Exception as e:
                st.exception(e)
    if "quant_table" in st.session_state:
        df, gaps = st.session_state["quant_table"]
        for g in gaps:
            st.caption("ℹ️ " + g)
        st.dataframe(df, hide_index=True, height=460, width="stretch",
                     column_config={"フラグ": st.column_config.TextColumn(width="large")})
        sel1 = st.selectbox("チャートを見る", [f"{c} {n}" for c, n in zip(df["コード"], df["銘柄名"])])
        if sel1:
            d = prices_for(f"{sel1.split()[0]}.T", demo)
            if d is not None:
                st.plotly_chart(candle(d, title=sel1), width="stretch")
    with st.expander("フラグのしきい値"):
        st.json(config.THRESHOLDS)

# ---------------------------------------------------------------- 履歴と検証
with tab_hist:
    st.header("過去の分析と、事後検証")
    runs = store.load_runs()
    if not runs:
        st.info("まだ分析の履歴がありません。")
    else:
        labels = [f"{r['created_at'][:16].replace('T', ' ')}　{r.get('theme') or '個別監査: ' + ' '.join(c['code'] for c in r['companies'])}"
                  for r in runs]
        idx = st.selectbox("分析を選ぶ", range(len(runs)), format_func=lambda i: labels[i])
        st.markdown("##### AIの優先度づけは役に立っているか？")
        st.caption("分析した時点からの株価の動きを、AIの優先度・あなたの判断ごとに比べます（短期の結果に一喜一憂せず、数ヶ月単位で見てください）。")
        include_demo = st.checkbox("デモモードの結果も含める", value=False)
        if st.button("📈 現在の株価で検証する"):
            with st.spinner("最新の株価を取得しています…"):
                ev = store.evaluate([r for r in runs if include_demo or not r.get("demo")], demo=demo)
            if ev.empty:
                st.info("対象となる分析がありません。")
            else:
                agg = {"騰落率%": "mean", "コード": "count"} | ({"日経平均比%": "mean"} if "日経平均比%" in ev else {})
                summary = ev.groupby("AIの優先度").agg(agg).rename(columns={"コード": "件数"}).round(2)
                st.dataframe(summary, width="stretch")
                st.dataframe(ev, hide_index=True, width="stretch")
        st.divider()
        render_run(runs[idx], f"hist_{runs[idx]['_file']}")

# ---------------------------------------------------------------- 銘柄リスト
with tab_univ:
    st.header("数値チェックの対象銘柄")
    st.caption("行の追加・削除・編集ができます。コードは東証の4桁コード（例: 7203）。テーマ分析ではこのリスト外の企業も対象になります。")
    univ = pd.read_csv(config.UNIVERSE_CSV, dtype=str)
    edited = st.data_editor(univ, num_rows="dynamic", hide_index=True, height=460)
    if st.button("💾 保存"):
        edited = edited.dropna(subset=["code"])
        edited["code"] = edited["code"].str.strip()
        edited.to_csv(config.UNIVERSE_CSV, index=False)
        st.success(f"{len(edited)}銘柄を保存しました。")
