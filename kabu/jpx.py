"""JPX（日本取引所グループ）の無料公開データ。J-Quants の有料プランがなくても需給を見るために使う。

- 銘柄別信用取引残高: 日次のPDF（全銘柄）。https://www.jpx.co.jp/markets/statistics-equities/margin/01.html
- 空売り残高（0.5%以上の報告）: 日次のExcel。https://www.jpx.co.jp/markets/public/short-selling/index.html

サイトに掲載されるのは直近数日分だけなので、取得したファイルは data/cache/jpx に保存し、
使うたびに履歴がたまるようにしている（1日1回程度の取得。個人利用の範囲で使うこと）。
PDFのテキスト抽出は数字が崩れることがあるため、「売残 = 一般 + 制度」「買残 = 一般 + 制度」の
検算が合った行だけを採用する。
"""

from __future__ import annotations

import itertools
import json
import re
from datetime import date, timedelta
from io import BytesIO

import requests

from . import config

BASE = "https://www.jpx.co.jp"
MARGIN_PAGE = BASE + "/markets/statistics-equities/margin/01.html"
SHORT_PAGE = BASE + "/markets/public/short-selling/index.html"
HEADERS = {"User-Agent": "kabu88 (personal research tool)"}


def _cache_dir():
    d = config.DATA_DIR / "cache" / "jpx"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get(url: str) -> bytes:
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.content


def _links(page: str, pattern: str) -> list[tuple[str, str]]:
    """ページ内のファイルへのリンクを (YYYYMMDD, 絶対URL) の新しい順で返す。"""
    html = _get(page).decode("utf-8", errors="ignore")
    found = {(m.group(2), BASE + m.group(1)) for m in re.finditer(pattern, html)}
    return sorted(found, reverse=True)


# ================================================================ 銘柄別信用取引残高（PDF）

_ROW = re.compile(r"\b([0-9][0-9A-Z]{3}0) ([A-Z]{2}[0-9A-Z]{9}[0-9]) 株数 Shs\. (.*)$")
_FULL = re.compile(r"^\d{1,3}(?:,\d{3})*$")
_NUMISH = re.compile(r"^[\d,]+$")


def _fields(rest: str) -> list[str]:
    """崩れた数字の断片のうち、確実なもの（"23," + "731"、"41,80" + "0" など）だけ結合する。"""
    out: list[str] = []
    for t in rest.split():
        prev = out[-1] if out else ""
        if prev and _NUMISH.match(prev) and _NUMISH.match(t) and (
                prev.endswith(",") or t.startswith(",") or re.search(r",\d{1,2}$", prev)):
            out[-1] = prev + t
        elif prev and re.match(r"^\d+\.\d*$", prev) and re.match(r"^\d*%$", t):
            out[-1] = prev + t
        else:
            out.append(t)
    return out


def _values(toks: list[str]) -> list:
    vals, neg = [], False
    for t in toks:
        if t == "▲":
            neg = True
            continue
        if t.startswith("▲"):
            neg, t = True, t[1:]
        if _FULL.match(t):
            n = int(t.replace(",", ""))
            vals.append(-n if neg else n)
        elif t in ("*", "-") or re.match(r"^\d+(\.\d+)?%$", t):
            vals.append(t)
        else:
            vals.append(None)
        neg = False
    return vals


_CHG = (1, 4, 7, 9, 11, 13)  # 前日比の列（新規上場などで "-" になる）


def _check(v: list) -> list | None:
    if len(v) != 14 or any(x is None for x in v):
        return None
    v = [0 if (i in _CHG and x == "-") else x for i, x in enumerate(v)]
    s, sc, _, b, bc, _, sn, snc, ss, ssc, bn, bnc, bs, bsc = v
    if not all(isinstance(x, int) for x in (s, sc, b, bc, sn, snc, ss, ssc, bn, bnc, bs, bsc)):
        return None
    ok = s == sn + ss and b == bn + bs and sc == snc + ssc and bc == bnc + bsc
    return v if ok else None


def parse_margin_row(rest: str) -> list | None:
    """「株数」行の14列を返す。検算が合わなければ None。"""
    toks = _fields(rest)
    v = _check(_values(toks))
    if v:
        return v
    # 空白で割れた数字（"2 4,000" → 24,000 など）の結合候補を試し、検算が合うものだけ採用
    pairs = [i for i in range(len(toks) - 1)
             if _NUMISH.match(toks[i]) and _NUMISH.match(toks[i + 1]) and _FULL.match(toks[i] + toks[i + 1])]
    for k in range(1, 4):
        for combo in itertools.combinations(pairs, k):
            if any(b - a < 2 for a, b in zip(combo, combo[1:])):
                continue
            t2, skip = [], set()
            for i, t in enumerate(toks):
                if i in skip:
                    continue
                if i in combo:
                    t2.append(t + toks[i + 1])
                    skip.add(i + 1)
                else:
                    t2.append(t)
            v = _check(_values(t2))
            if v:
                return v
    return None


def parse_margin_pdf(pdf: bytes) -> dict[str, dict]:
    from pypdf import PdfReader

    out: dict[str, dict] = {}
    for page in PdfReader(BytesIO(pdf)).pages:
        for line in (page.extract_text() or "").splitlines():
            m = _ROW.search(line)
            if not m:
                continue
            v = parse_margin_row(m.group(3))
            if v:
                code = m.group(1)[:4]
                out[code] = {"ShrtVol": v[0], "LongVol": v[3], "LongChg": v[4],
                             "LongToListed": v[5] if isinstance(v[5], str) and v[5].endswith("%") else None}
    return out


def margin_balances(on_status=lambda s: None) -> tuple[str | None, dict[str, dict], dict[str, dict]]:
    """最新の銘柄別信用残と、約4週間前（キャッシュにあれば）を返す: (基準日, 最新, 4週前)。"""
    cache = _cache_dir()
    links = _links(MARGIN_PAGE, r'href="([^"]+/(\d{8})_mtall\.pdf)"')
    if not links:
        return None, {}, {}
    ymd, url = links[0]
    path = cache / f"margin_{ymd}.json"
    if path.exists():
        latest = json.loads(path.read_text(encoding="utf-8"))
    else:
        on_status("JPXの銘柄別信用残（PDF）を解析しています（1日1回・約15秒）…")
        latest = parse_margin_pdf(_get(url))
        if latest:
            path.write_text(json.dumps(latest, ensure_ascii=False), encoding="utf-8")
    d = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:]))
    target = (d - timedelta(days=28)).strftime("%Y%m%d")
    older = sorted(p for p in cache.glob("margin_*.json") if p.stem.split("_")[1] <= target)
    prev = json.loads(older[-1].read_text(encoding="utf-8")) if older else {}
    return d.isoformat(), latest, prev


# ================================================================ 空売り残高（Excel）

def _parse_short_xls(data: bytes) -> list[dict]:
    import pandas as pd

    df = pd.read_excel(BytesIO(data), engine="xlrd", header=None)
    rows = []
    for r in df.itertuples(index=False):
        code, ratio = str(r[2]).strip(), r[10]
        if not re.match(r"^[0-9][0-9A-Z]{3}$", code) or not isinstance(ratio, (int, float)):
            continue
        rows.append({"Code": code, "CalcDate": str(r[1])[:10], "SSName": str(r[5]), "DICName": str(r[7]),
                     "FundName": str(r[9]), "ShrtPosToSO": float(ratio)})
    return rows


def short_positions(on_status=lambda s: None) -> tuple[str | None, dict[str, list[dict]]]:
    """取得できた期間の空売り残高報告を銘柄ごとにまとめる: (期間の説明, {code: [報告...]})。"""
    cache = _cache_dir()
    try:
        links = _links(SHORT_PAGE, r'href="([^"]+/(\d{8})_Short_Positions\.xls)"')
    except requests.RequestException:
        links = []
    for ymd, url in links:
        path = cache / f"short_{ymd}.xls"
        if not path.exists():
            on_status(f"JPXの空売り残高報告（{ymd}）を取得しています…")
            path.write_bytes(_get(url))
    files = sorted(cache.glob("short_*.xls"))[-60:]  # 直近約3ヶ月分まで
    by_code: dict[str, list[dict]] = {}
    for p in files:
        try:
            for row in _parse_short_xls(p.read_bytes()):
                by_code.setdefault(row["Code"], []).append(row)
        except Exception:
            continue
    if not files:
        return None, {}
    span = f"{files[0].stem[6:]}〜{files[-1].stem[6:]}公表分"
    return span, by_code
