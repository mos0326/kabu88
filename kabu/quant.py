"""定量レイヤー: 外部APIの数値だけで、銘柄ごとのデータ表と監査フラグを作る。

ここではAIを一切使わない。AIは、ここで作った数値を「与えられたデータ」として読むだけにする
（数値の捏造や、学習データ内の古い数値の混入を防ぐため）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

import pandas as pd

from . import config
from .data import fetch_prices, universe_names, yahoo_fundamentals
from .indicators import price_metrics
from .jquants import JQuants, JQuantsError, PlanError, code4, num

SRC_YAHOO = "Yahoo Finance"
SRC_JQ = "J-Quants（JPX公式）"


def _r(x, nd=2):
    return None if x is None else round(float(x), nd)


def _oku(yen):
    return None if yen is None else round(yen / 1e8, 1)


# ================================================================ J-Quants から取る部分

@dataclass
class JQBulk:
    """全銘柄分をまとめて取得したデータ（API呼び出し回数を抑えるため）。"""

    valuation: dict[str, dict] = field(default_factory=dict)
    valuation_date: str | None = None
    margin: dict[str, dict] = field(default_factory=dict)
    margin_prev: dict[str, dict] = field(default_factory=dict)
    margin_date: str | None = None
    master: dict[str, dict] = field(default_factory=dict)
    short_ratio: dict[str, float] = field(default_factory=dict)  # S33 -> %
    short_ratio_market: float | None = None
    short_ratio_date: str | None = None
    unavailable: dict[str, str] = field(default_factory=dict)  # データ名 -> 理由


def _guard(bulk: JQBulk, name: str, fn: Callable):
    """プラン外のAPIは一度失敗したら記録して以後呼ばない。"""
    if name in bulk.unavailable:
        return None
    try:
        return fn()
    except PlanError as e:
        bulk.unavailable[name] = f"契約プラン外または権限なし（{e}）"
    except JQuantsError as e:
        bulk.unavailable[name] = f"取得エラー（{e}）"
    return None


def load_bulk(jq: JQuants, with_margin: bool = True) -> JQBulk:
    b = JQBulk()
    today = date.today()

    rows = _guard(b, "上場銘柄一覧", lambda: jq.master())
    for r in rows or []:
        b.master[code4(r["Code"])] = r

    latest = _guard(b, "バリュエーション指標", lambda: jq.latest_date("valuation"))
    if latest:
        b.valuation_date = latest
        for r in _guard(b, "バリュエーション指標", lambda: jq.valuation(date_=latest)) or []:
            b.valuation[code4(r["Code"])] = r

    if with_margin:
        probe = _guard(b, "信用取引残高", lambda: jq.margin_interest(
            code="7203", from_=(today - timedelta(days=150)).isoformat(), to=today.isoformat()))
        dates = sorted({r["Date"] for r in probe or [] if r.get("Date")})
        if dates:
            b.margin_date = dates[-1]
            target = (date.fromisoformat(dates[-1]) - timedelta(days=28)).isoformat()
            prev = max((d for d in dates if d <= target), default=None)
            for r in _guard(b, "信用取引残高", lambda: jq.margin_interest(date_=b.margin_date)) or []:
                b.margin[code4(r["Code"])] = r
            if prev:
                for r in _guard(b, "信用取引残高", lambda: jq.margin_interest(date_=prev)) or []:
                    b.margin_prev[code4(r["Code"])] = r

        sr = _guard(b, "業種別空売り比率", lambda: jq.short_ratio(
            from_=(today - timedelta(days=14)).isoformat(), to=today.isoformat()))
        if sr:
            d = max(r["Date"] for r in sr)
            day = [r for r in sr if r["Date"] == d]
            b.short_ratio_date = d
            tot_short = tot_all = 0.0
            for r in day:
                short = (num(r.get("ShrtWithResVa")) or 0) + (num(r.get("ShrtNoResVa")) or 0)
                total = short + (num(r.get("SellExShortVa")) or 0)
                tot_short += short
                tot_all += total
                if total > 0:
                    b.short_ratio[str(r["S33"])] = round(short / total * 100, 1)
            b.short_ratio_market = round(tot_short / tot_all * 100, 1) if tot_all else None
    return b


def fin_metrics(rows: list[dict], price: float | None = None) -> dict:
    """財務情報（決算短信）から、会社予想・進捗・修正履歴をまとめる。金額は億円。"""
    recs = sorted(rows, key=lambda r: (r.get("DiscDate", ""), r.get("DiscTime", ""), r.get("DiscNo", "")))
    actual: dict[str, dict] = {}
    forecasts: dict[str, list[dict]] = {}
    quarters: dict[str, list[dict]] = {}
    for r in recs:
        fy, per = r.get("CurFYEn") or "", r.get("CurPerType") or ""
        sales, op, np_ = num(r.get("Sales")), num(r.get("OP")), num(r.get("NP"))
        if per == "FY" and sales is not None and fy:
            actual[fy] = {"sales": sales, "op": op, "np": np_}
        elif per in ("1Q", "2Q", "3Q") and op is not None and fy:
            quarters.setdefault(fy, []).append({"per": per, "op": op, "sales": sales, "date": r.get("DiscDate")})
        if fy and (num(r.get("FOP")) is not None or num(r.get("FSales")) is not None):
            forecasts.setdefault(fy, []).append({
                "date": r.get("DiscDate"), "sales": num(r.get("FSales")), "op": num(r.get("FOP")),
                "np": num(r.get("FNP")), "div": num(r.get("FDivAnn"))})
        nfy = r.get("NxtFYEn") or ""
        if nfy and (num(r.get("NxFOP")) is not None or num(r.get("NxFSales")) is not None):
            forecasts.setdefault(nfy, []).append({
                "date": r.get("DiscDate"), "sales": num(r.get("NxFSales")), "op": num(r.get("NxFOP")),
                "np": num(r.get("NxFNp")), "div": num(r.get("NxFDivAnn"))})

    out: dict = {}
    if recs:
        out["直近開示日"] = recs[-1].get("DiscDate")
        out["直近開示書類"] = recs[-1].get("DocType")
    last_actual = max(actual) if actual else ""
    hist = sorted(actual)[-3:]
    if hist:
        out["売上高推移(億円)"] = {fy[:7]: _oku(actual[fy]["sales"]) for fy in hist}
        out["営業利益推移(億円)"] = {fy[:7]: _oku(actual[fy]["op"]) for fy in hist}

    future = [fy for fy in forecasts if fy > last_actual]
    if not future:
        return out
    target = max(future)
    fc = forecasts[target]
    cur, first = fc[-1], fc[0]
    base = actual.get(max((fy for fy in actual if fy < target), default=""), {})
    out["予想対象期"] = target[:7]
    out["会社予想売上高(億円)"] = _oku(cur["sales"])
    out["会社予想営業利益(億円)"] = _oku(cur["op"])
    if cur["sales"] is not None and base.get("sales"):
        out["予想増収率%"] = _r((cur["sales"] / base["sales"] - 1) * 100, 1)
    if cur["op"] is not None and base.get("op") and base["op"] > 0:
        out["予想営業増益率%"] = _r((cur["op"] / base["op"] - 1) * 100, 1)
    if first["op"] and cur["op"] is not None and len(fc) > 1:
        chg = (cur["op"] / first["op"] - 1) * 100
        if abs(chg) >= 0.5:
            out["業績予想の修正"] = f"{'上方修正' if chg > 0 else '下方修正'}（期初予想比 営業利益 {chg:+.1f}%）"
        else:
            out["業績予想の修正"] = "期初予想から変更なし"
    q = quarters.get(target)
    if q and cur["op"] and cur["op"] > 0:
        out["営業利益進捗率%"] = _r(q[-1]["op"] / cur["op"] * 100, 1)
        out["進捗の基準"] = f"{q[-1]['per']}累計"
    if cur.get("div") is not None:
        out["予想年間配当(円)"] = cur["div"]
        if price:
            out["予想配当利回り%"] = _r(cur["div"] / price * 100)
    return out


def next_earnings(rows: list[dict]) -> dict:
    today = date.today().isoformat()
    upcoming = sorted(r["SchDate"] for r in rows if r.get("SchDate") and r["SchDate"] >= today)
    if not upcoming:
        return {}
    d = upcoming[0]
    return {"次回決算予定日": d, "次回決算まで(日)": (date.fromisoformat(d) - date.today()).days}


def short_sale_metrics(rows: list[dict]) -> dict:
    """空売り残高報告（発行済株式の0.5%以上の空売りの報告）の、報告者ごとの最新値を合計する。"""
    latest: dict[tuple, dict] = {}
    for r in rows:
        k = (r.get("SSName"), r.get("DICName"), r.get("FundName"))
        if k not in latest or r.get("CalcDate", "") > latest[k].get("CalcDate", ""):
            latest[k] = r
    active = [r for r in latest.values() if (num(r.get("ShrtPosToSO")) or 0) >= 0.005]
    return {
        "大口空売り残高%": _r(sum(num(r["ShrtPosToSO"]) for r in active) * 100),
        "大口空売り報告数": len(active),
    }


def margin_metrics(cur: dict | None, prev: dict | None, avg_volume: float | None) -> dict:
    if not cur:
        return {}
    long_, short = num(cur.get("LongVol")), num(cur.get("ShrtVol"))
    out = {"信用買残(株)": long_, "信用売残(株)": short, "信用残基準日": cur.get("Date")}
    if long_ is not None and short:
        out["信用倍率"] = _r(long_ / short)
    if long_ is not None and avg_volume:
        out["買残/出来高(日)"] = _r(long_ / avg_volume, 1)
    if prev and long_ is not None and num(prev.get("LongVol")):
        out["買残4週変化%"] = _r((long_ / num(prev["LongVol"]) - 1) * 100, 1)
    return out


# ================================================================ 監査フラグ（コードで判定）

def flags_for(s: dict) -> list[dict]:
    t = config.THRESHOLDS
    p, v, f, d, e = s.get("株価", {}), s.get("バリュエーション", {}), s.get("業績", {}), s.get("需給", {}), s.get("イベント", {})
    out: list[dict] = []

    def add(level, cat, text):
        out.append({"level": level, "category": cat, "text": text})

    def ge(x, th):
        return x is not None and x >= th

    if ge(p.get("RSI14"), t["RSI過熱"]):
        add("注意", "過熱", f"RSI14が{p['RSI14']}（{t['RSI過熱']}以上は買われすぎの目安）")
    if ge(p.get("25日線乖離%"), t["25日線乖離過熱%"]):
        add("注意", "過熱", f"25日線から{p['25日線乖離%']:+.1f}%乖離")
    if ge(p.get("200日線乖離%"), t["200日線乖離過熱%"]):
        add("警告", "織り込み", f"200日線から{p['200日線乖離%']:+.1f}%上振れ（期待が既に株価に織り込まれている可能性）")
    if ge(p.get("3ヶ月騰落%"), t["3ヶ月騰落過熱%"]):
        add("注意", "織り込み", f"3ヶ月で{p['3ヶ月騰落%']:+.1f}%上昇")
    if ge(p.get("1年騰落%"), t["1年騰落過熱%"]):
        add("警告", "織り込み", f"1年で{p['1年騰落%']:+.1f}%上昇")
    if ge(v.get("予想PER"), t["予想PER割高"]):
        add("注意", "割高", f"予想PER {v['予想PER']}倍")
    if ge(v.get("PBR"), t["PBR割高"]):
        add("注意", "割高", f"PBR {v['PBR']}倍")
    if ge(d.get("信用倍率"), t["信用倍率高"]):
        add("注意", "需給", f"信用倍率{d['信用倍率']}倍（買い残が売り残の{d['信用倍率']}倍＝将来の売り圧力）")
    if ge(d.get("買残/出来高(日)"), t["買残出来高日数高"]):
        add("警告", "需給", f"信用買い残が平均出来高の{d['買残/出来高(日)']}日分（戻り売りで上値が重くなりやすい）")
    if ge(d.get("買残4週変化%"), t["買残急増%"]):
        add("注意", "需給", f"信用買い残が4週間で{d['買残4週変化%']:+.1f}%増加")
    if ge(d.get("大口空売り残高%"), t["大口空売り残高%"]):
        add("注意", "需給", f"大口の空売り残高が発行済株式の{d['大口空売り残高%']}%（{d.get('大口空売り報告数')}件）")
    if ge(d.get("業種空売り比率%"), t["業種空売り比率高%"]):
        add("注意", "需給", f"業種の空売り比率が{d['業種空売り比率%']}%")
    if f.get("予想営業増益率%") is not None and f["予想営業増益率%"] <= t["減益予想%"]:
        add("注意", "業績", f"会社予想は営業{f['予想営業増益率%']:+.1f}%（減益予想）")
    rev = f.get("業績予想の修正", "")
    if rev.startswith("下方修正"):
        add("警告", "業績", rev)
    elif rev.startswith("上方修正"):
        add("情報", "業績", rev)
    days = e.get("次回決算まで(日)")
    if days is not None and days <= t["決算接近日数"]:
        add("注意", "イベント", f"決算発表まで{days}日（{e['次回決算予定日']}）")
    vd = s.get("出典", {}).get("_valuation_date")
    if vd and (date.today() - date.fromisoformat(vd)).days > t["データ鮮度日数"]:
        add("注意", "データ", f"バリュエーションの基準日が{vd}と古い（J-Quants Freeプランは12週間遅延）")
    return out


# ================================================================ スナップショット作成

def environment(prices: dict[str, pd.DataFrame], bulk: JQBulk | None) -> dict:
    env = {}
    for label, t in (("日経平均", config.BENCHMARK), ("TOPIX(1306)", config.TOPIX_ETF), ("ドル円", config.USDJPY)):
        df = prices.get(t)
        if df is not None:
            m = price_metrics(df)
            env[label] = {k: m[k] for k in ("終値", "株価日付", "1ヶ月騰落%", "3ヶ月騰落%", "1年騰落%", "200日線乖離%", "RSI14")}
    if bulk and bulk.short_ratio_market is not None:
        env["市場全体の空売り比率%"] = {"値": bulk.short_ratio_market, "基準日": bulk.short_ratio_date}
    return env


def build_snapshots(
    codes: list[str],
    names: dict[str, str] | None = None,
    demo: bool | None = None,
    use_jquants: bool = True,
    on_status: Callable[[str], None] = lambda s: None,
) -> tuple[list[dict], dict, list[str], dict[str, pd.DataFrame]]:
    """銘柄ごとの数値スナップショット・相場環境・データ不足の一覧・株価データを返す。"""
    demo = config.DEMO_MODE if demo is None else demo
    names = names or {}
    uni = universe_names()
    gaps: list[str] = []

    on_status(f"{len(codes)}銘柄の株価を Yahoo Finance から取得しています…")
    prices = fetch_prices([f"{c}.T" for c in codes] + [config.BENCHMARK, config.TOPIX_ETF, config.USDJPY], demo=demo)

    jq = bulk = None
    if use_jquants and config.has_jquants() and not demo:
        on_status("J-Quants（JPX公式）からバリュエーション・信用残・空売りデータを取得しています…")
        jq = JQuants()
        bulk = load_bulk(jq)
    else:
        gaps.append("J-Quants未接続のため、信用残・空売り・決算短信の数値（会社予想・進捗率・修正履歴）はありません。"
                    "PER/PBR等は Yahoo Finance の値です。")

    env = environment(prices, bulk)
    bench = prices.get(config.BENCHMARK)
    bench_3m = price_metrics(bench)["3ヶ月騰落%"] if bench is not None else None

    snaps: list[dict] = []
    for code in codes:
        df = prices.get(f"{code}.T")
        if df is None:
            gaps.append(f"{code}: 株価データを取得できなかったため除外しました（コード誤り・上場廃止の可能性）。")
            continue
        on_status(f"{code} の数値を集計しています…")
        pm = price_metrics(df)
        if pm["3ヶ月騰落%"] is not None and bench_3m is not None:
            pm["日経平均比3ヶ月%"] = _r(pm["3ヶ月騰落%"] - bench_3m)
        m = bulk.master.get(code, {}) if bulk else {}
        snap = {
            "code": code,
            "name": m.get("CoName") or uni.get(code, ("", ""))[0] or names.get(code) or code,
            "sector": m.get("S33Nm") or uni.get(code, ("", ""))[1],
            "株価": pm,
            "出典": {"株価": f"{SRC_YAHOO}（{pm['株価日付']}）"},
        }

        # バリュエーション
        vrow = bulk.valuation.get(code) if bulk else None
        if vrow:
            snap["バリュエーション"] = {
                "PER": num(vrow.get("PER")), "予想PER": num(vrow.get("FwdPER")), "PBR": num(vrow.get("PBR")),
                "ROE%": _r(num(vrow["ROE"]) * 100, 1) if num(vrow.get("ROE")) is not None else None,
                "予想ROE%": _r(num(vrow["FwdROE"]) * 100, 1) if num(vrow.get("FwdROE")) is not None else None,
                "時価総額(億円)": _r(num(vrow["MktCap"]) / 100, 0) if num(vrow.get("MktCap")) is not None else None,
            }
            snap["出典"]["バリュエーション"] = f"{SRC_JQ}（{bulk.valuation_date}）"
            snap["出典"]["_valuation_date"] = bulk.valuation_date
        else:
            snap["バリュエーション"] = yahoo_fundamentals(f"{code}.T", demo=demo)
            snap["出典"]["バリュエーション"] = SRC_YAHOO + ("（デモデータ）" if demo else "")

        # 業績（決算短信）・イベント・需給
        snap["業績"], snap["イベント"], snap["需給"] = {}, {}, {}
        if jq and bulk:
            fins = _guard(bulk, "財務情報", lambda: jq.fins_summary(code))
            if fins:
                snap["業績"] = fin_metrics(fins, price=pm["終値"])
                snap["出典"]["業績"] = f"{SRC_JQ} 決算短信（{snap['業績'].get('直近開示日')}開示）"
                if "予想配当利回り%" in snap["業績"]:
                    snap["バリュエーション"]["配当利回り%(会社予想)"] = snap["業績"]["予想配当利回り%"]
            ed = _guard(bulk, "決算発表予定日", lambda: jq.earnings_date(code))
            if ed:
                snap["イベント"] = next_earnings(ed)
            mm = margin_metrics(bulk.margin.get(code), bulk.margin_prev.get(code), pm["25日平均出来高"])
            if mm:
                snap["需給"].update(mm)
                snap["出典"]["信用残"] = f"{SRC_JQ}（{mm['信用残基準日']}）"
            today = date.today()
            ss = _guard(bulk, "空売り残高報告", lambda: jq.short_sale_report(
                code, (today - timedelta(days=120)).isoformat(), today.isoformat()))
            if ss is not None:
                snap["需給"].update(short_sale_metrics(ss))
            s33 = str(m.get("S33", ""))
            if s33 in bulk.short_ratio:
                snap["需給"]["業種空売り比率%"] = bulk.short_ratio[s33]
                snap["出典"]["空売り比率"] = f"{SRC_JQ}（{bulk.short_ratio_date}）"

        snap["flags"] = flags_for(snap)
        snaps.append(snap)

    if bulk:
        for name, why in bulk.unavailable.items():
            gaps.append(f"J-Quants「{name}」: {why}")
    return snaps, env, gaps, prices


def universe_table(demo: bool | None = None, use_jquants: bool = True,
                   on_status: Callable[[str], None] = lambda s: None) -> tuple[pd.DataFrame, list[str]]:
    """銘柄リスト全体の数値表（AIなし）。J-Quantsがあれば全銘柄一括のAPIだけを使う。"""
    from .data import load_universe

    demo = config.DEMO_MODE if demo is None else demo
    stocks = load_universe()
    on_status(f"{len(stocks)}銘柄の株価を取得しています…")
    prices = fetch_prices([s.ticker for s in stocks], demo=demo)
    gaps: list[str] = []
    bulk = None
    if use_jquants and config.has_jquants() and not demo:
        on_status("J-Quants から全銘柄のバリュエーション・信用残を取得しています…")
        bulk = load_bulk(JQuants())
        gaps += [f"J-Quants「{k}」: {v}" for k, v in bulk.unavailable.items()]
    rows = []
    for s in stocks:
        df = prices.get(s.ticker)
        if df is None:
            continue
        pm = price_metrics(df)
        snap = {"株価": pm, "バリュエーション": {}, "需給": {}, "出典": {}}
        row = {"コード": s.code, "銘柄名": s.name, "業種": s.sector,
               **{k: pm[k] for k in ("終値", "3ヶ月騰落%", "1年騰落%", "200日線乖離%", "25日線乖離%", "RSI14", "売買代金20日平均(億円)")}}
        if bulk:
            v = bulk.valuation.get(s.code)
            if v:
                snap["バリュエーション"] = {"予想PER": num(v.get("FwdPER")), "PBR": num(v.get("PBR"))}
                row.update({"PER": num(v.get("PER")), "予想PER": num(v.get("FwdPER")), "PBR": num(v.get("PBR")),
                            "ROE%": _r(num(v["ROE"]) * 100, 1) if num(v.get("ROE")) is not None else None})
            mm = margin_metrics(bulk.margin.get(s.code), bulk.margin_prev.get(s.code), pm["25日平均出来高"])
            snap["需給"] = mm
            row.update({k: mm.get(k) for k in ("信用倍率", "買残/出来高(日)", "買残4週変化%")})
        fl = flags_for(snap)
        row["警告"] = sum(1 for x in fl if x["level"] == "警告")
        row["注意"] = sum(1 for x in fl if x["level"] == "注意")
        row["フラグ"] = " / ".join(x["text"] for x in fl if x["level"] != "情報")
        rows.append(row)
    if not bulk:
        gaps.append("J-Quants未接続のため、PER/PBR・信用残の列はありません（個別の分析では Yahoo Finance の値を使います）。")
    return pd.DataFrame(rows), gaps
