from kabu import pipeline, store
from kabu.pipeline import Settings

from .fakes import FakeClient


def test_analyze_theme_end_to_end_demo():
    client = FakeClient()
    run = pipeline.analyze_theme("AIデータセンター拡大による液冷需要", include="7203", s=Settings(demo=True),
                                 on_status=lambda s: None, client=client)
    codes = [c["code"] for c in run["companies"]]
    assert codes[0] == "7203" or "7203" in codes  # 必ず含める銘柄
    assert set(codes) == {"7203", "6857", "5803"}  # 不正なコード(XXXX1)は除外
    # 監査は定量データにある銘柄だけ（存在しない 0000 は除外）
    assert all(c["audit"] is None or c["audit"]["code"] == c["code"] for c in run["companies"])
    # 短期予測ガード: 目標株価・売買タイミング・短期予測の文が消えている
    texts = str(run["companies"]) + run["research_notes"]
    assert "目標株価" not in str([c["audit"] for c in run["companies"]])
    assert "今が買い時" not in texts
    assert "来週は上昇しそう" not in run["research_notes"]
    assert "液冷の需要が拡大" in run["research_notes"]
    assert run["guard_log"], "ガードの削除ログが残る"
    # pause_turn から再開して、リサーチメモがつながっている
    assert "途中経過。" in run["research_notes"]
    research_calls = [c for c in client.calls if "tools" in c]
    assert len(research_calls) == 2 and research_calls[1]["messages"][-1]["role"] == "assistant"
    assert {t["name"] for t in research_calls[0]["tools"]} == {"web_search", "web_fetch"}
    # すべてのリクエストでサーバー側フォールバックを有効にしている
    assert all(c["fallbacks"] == "default" for c in client.calls)
    # 保存されている
    saved = store.load_runs()
    assert saved and saved[0]["theme"] == "AIデータセンター拡大による液冷需要"


def test_audit_stocks_without_web():
    client = FakeClient(companies=[("6857", "アドバンテスト")])
    run = pipeline.audit_stocks("6857", s=Settings(demo=True, use_web=False), on_status=lambda s: None, client=client)
    assert [c["code"] for c in run["companies"]] == ["6857"]
    assert run["research_notes"] == ""  # Web検索なし・添付なしなら調査は省略
    assert all("tools" not in c for c in client.calls)


def test_propose_themes_and_evaluate():
    r = pipeline.propose_themes(3, s=Settings(demo=True), on_status=lambda s: None, client=FakeClient())
    assert r["themes"][0]["title"].startswith("AIデータセンター")
    pipeline.audit_stocks("6857", s=Settings(demo=True, use_web=False), on_status=lambda s: None,
                          client=FakeClient(companies=[("6857", "アドバンテスト")]))
    runs = store.load_runs()
    store.save_decision(runs[0]["_file"], "6857", "監視リストに入れる", "決算を確認")
    ev = store.evaluate(runs, demo=True)
    assert list(ev["コード"]) == ["6857"]
    assert ev.loc[0, "あなたの判断"] == "監視リストに入れる"
    assert "日経平均比%" in ev.columns


def test_normalize_codes():
    assert pipeline.normalize_codes("6857, 5803 285a 12 6857") == ["6857", "5803", "285A"]
