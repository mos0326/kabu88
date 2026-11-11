from datetime import date, timedelta

from kabu import quant


def _fin(**kw):
    base = {k: "" for k in ("Sales", "OP", "NP", "FSales", "FOP", "FNP", "FDivAnn", "NxFSales", "NxFOP", "NxFNp",
                            "NxFDivAnn", "NxtFYEn")}
    return base | kw


FINS = [
    # 前期（2026年3月期）の本決算: 実績と、今期（2027年3月期）の予想
    _fin(DiscDate="2026-05-10", CurPerType="FY", CurFYEn="2026-03-31", Sales="100000000000", OP="10000000000",
         NP="7000000000", NxtFYEn="2027-03-31", NxFSales="110000000000", NxFOP="12000000000", NxFNp="8000000000",
         NxFDivAnn="50"),
    # 1Q: 予想据え置き
    _fin(DiscDate="2026-08-05", CurPerType="1Q", CurFYEn="2027-03-31", Sales="26000000000", OP="3000000000",
         FSales="110000000000", FOP="12000000000", FDivAnn="50"),
    # 2Q: 上方修正
    _fin(DiscDate="2026-11-05", CurPerType="2Q", CurFYEn="2027-03-31", Sales="55000000000", OP="7000000000",
         FSales="115000000000", FOP="13200000000", FDivAnn="60"),
]


def test_fin_metrics_forecast_progress_and_revision():
    m = quant.fin_metrics(FINS, price=3000)
    assert m["予想対象期"] == "2027-03"
    assert m["会社予想営業利益(億円)"] == 132.0
    assert m["予想営業増益率%"] == 32.0  # 132億 / 100億
    assert m["予想増収率%"] == 15.0
    assert m["営業利益進捗率%"] == 53.0  # 70億 / 132億
    assert m["進捗の基準"] == "2Q累計"
    assert m["業績予想の修正"].startswith("上方修正")
    assert m["予想配当利回り%"] == 2.0
    assert m["営業利益推移(億円)"] == {"2026-03": 100.0}


def test_fin_metrics_downward_revision_flag():
    fins = FINS[:2] + [_fin(DiscDate="2026-11-05", CurPerType="2Q", CurFYEn="2027-03-31", OP="4000000000",
                            FSales="100000000000", FOP="9000000000")]
    m = quant.fin_metrics(fins)
    assert m["業績予想の修正"].startswith("下方修正")
    assert m["予想営業増益率%"] == -10.0
    flags = quant.flags_for({"業績": m})
    assert {f["category"] for f in flags} == {"業績"}
    assert any(f["level"] == "警告" for f in flags)


def test_margin_and_short_metrics_and_flags():
    mm = quant.margin_metrics({"LongVol": 6_000_000, "ShrtVol": 1_000_000, "Date": "2026-11-10"},
                              {"LongVol": 4_000_000}, avg_volume=500_000)
    assert mm["信用倍率"] == 6.0 and mm["買残/出来高(日)"] == 12.0 and mm["買残4週変化%"] == 50.0
    ss = quant.short_sale_metrics([
        {"SSName": "A", "DICName": "", "FundName": "", "CalcDate": "2026-10-01", "ShrtPosToSO": 0.006},
        {"SSName": "A", "DICName": "", "FundName": "", "CalcDate": "2026-10-20", "ShrtPosToSO": 0.012},
        {"SSName": "B", "DICName": "", "FundName": "", "CalcDate": "2026-10-20", "ShrtPosToSO": 0.004},  # 0.5%未満は除外
        {"SSName": "C", "DICName": "", "FundName": "", "CalcDate": "2026-10-21", "ShrtPosToSO": 0.02},
    ])
    assert ss == {"大口空売り残高%": 3.2, "大口空売り報告数": 2}
    flags = quant.flags_for({"需給": mm | ss})
    texts = " ".join(f["text"] for f in flags)
    assert "信用倍率6.0倍" in texts and "12.0日分" in texts and "4週間で+50.0%" in texts and "3.2%" in texts


def test_price_overheat_flags():
    s = {"株価": {"RSI14": 80, "25日線乖離%": 20.0, "200日線乖離%": 45.0, "3ヶ月騰落%": 50.0, "1年騰落%": 120.0},
         "バリュエーション": {"予想PER": 55, "PBR": 9}}
    cats = [f["category"] for f in quant.flags_for(s)]
    assert cats.count("過熱") == 2 and cats.count("織り込み") == 3 and cats.count("割高") == 2


def test_next_earnings_and_event_flag():
    soon = (date.today() + timedelta(days=5)).isoformat()
    later = (date.today() + timedelta(days=60)).isoformat()
    ev = quant.next_earnings([{"SchDate": later}, {"SchDate": soon}, {"SchDate": "2020-01-01"}, {"SchDate": ""}])
    assert ev == {"次回決算予定日": soon, "次回決算まで(日)": 5}
    assert quant.flags_for({"イベント": ev})[0]["category"] == "イベント"


def test_build_snapshots_demo():
    snaps, env, gaps, prices = quant.build_snapshots(["6857", "7203"], demo=True)
    assert [s["code"] for s in snaps] == ["6857", "7203"]
    assert snaps[0]["name"] == "アドバンテスト"
    assert set(env) >= {"日経平均", "ドル円"}
    assert any("J-Quants未接続" in g for g in gaps)
    for s in snaps:
        assert s["株価"]["終値"] > 0 and isinstance(s["flags"], list)
