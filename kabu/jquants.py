"""J-Quants API V2 クライアント（JPX公式の株式データ）。

仕様: https://jpx-jquants.com/ja/spec
- 認証は `x-api-key` ヘッダー（ダッシュボードで発行したAPIキー）
- レスポンスは原則 {"data": [...], "pagination_key": "..."}
- プランごとに使えるAPIが異なる:
    全プラン        : 上場銘柄一覧 / バリュエーション指標 / 財務情報 / 決算発表予定日（Freeは12週間遅延）
    Standard以上    : 信用取引残高 / 業種別空売り比率 / 空売り残高報告
    TDnetアドオン   : 適時開示一覧・ファイル（決算短信PDF）
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import date, timedelta

import requests

from . import config

BASE_URL = "https://api.jquants.com/v2"


class JQuantsError(RuntimeError):
    pass


class PlanError(JQuantsError):
    """APIキーが無効、または契約プラン・アドオンで使えないAPI。"""


def code5(code: str) -> str:
    code = str(code).strip()
    return code + "0" if len(code) == 4 else code


def code4(code: str) -> str:
    code = str(code).strip()
    return code[:4] if len(code) == 5 and code.endswith("0") else code


def num(v) -> float | None:
    """J-Quants の数値（文字列 "" や None を含む）を float に。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class JQuants:
    def __init__(self, api_key: str | None = None, rpm: int | None = None, session: requests.Session | None = None):
        self.api_key = api_key or config.JQUANTS_API_KEY
        if not self.api_key:
            raise JQuantsError("JQUANTS_API_KEY が設定されていません。")
        self.min_interval = 60.0 / max(1, rpm or config.JQUANTS_RPM)
        self.session = session or requests.Session()
        self._last_call = 0.0
        self._lock = threading.Lock()
        self._cache: dict[tuple, object] = {}

    # ------------------------------------------------------------ 低レベル
    def _throttle(self) -> None:
        with self._lock:
            wait = self._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _request(self, path: str, params: dict) -> dict:
        for attempt in range(5):
            self._throttle()
            try:
                r = self.session.get(BASE_URL + path, params=params, headers={"x-api-key": self.api_key}, timeout=30)
            except requests.RequestException as e:
                if attempt == 4:
                    raise JQuantsError(f"J-Quantsに接続できません: {e}") from e
                time.sleep(2**attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(60, 5 * 2**attempt))
                continue
            if r.status_code in (401, 403):
                raise PlanError(f"{path}: {_message(r)}（APIキー、または契約プラン・アドオンを確認してください）")
            if r.status_code >= 400:
                raise JQuantsError(f"{path}: HTTP {r.status_code} {_message(r)}")
            return r.json()
        raise JQuantsError(f"{path}: リクエストが混み合っています（レート制限）。時間をおいて再実行してください。")

    # 決算短信などは頻繁に変わらないので、ディスクにも保存してリクエスト数を節約する
    DISK_TTL_HOURS = {"/fins/summary": 12, "/fins/earnings-date": 12, "/equities/master": 24 * 7}

    def get(self, path: str, **params) -> list[dict]:
        """data 配列をページネーションしながら全件取得する（結果はメモリとディスクにキャッシュ）。"""
        params = {k.rstrip("_"): v for k, v in params.items() if v not in (None, "")}
        key = (path, tuple(sorted(params.items())))
        if key in self._cache:
            return self._cache[key]  # type: ignore[return-value]
        disk = self._disk_path(key) if path in self.DISK_TTL_HOURS else None
        if disk and disk.exists() and time.time() - disk.stat().st_mtime < self.DISK_TTL_HOURS[path] * 3600:
            rows = json.loads(disk.read_text(encoding="utf-8"))
            self._cache[key] = rows
            return rows
        rows: list[dict] = []
        while True:
            body = self._request(path, params)
            rows.extend(body.get("data") or [])
            nxt = body.get("pagination_key")
            if not nxt:
                break
            params = {**params, "pagination_key": nxt}
        self._cache[key] = rows
        if disk:
            disk.parent.mkdir(parents=True, exist_ok=True)
            disk.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return rows

    @staticmethod
    def _disk_path(key: tuple):
        h = hashlib.sha1(repr(key).encode()).hexdigest()[:16]
        return config.DATA_DIR / "cache" / "jquants" / f"{key[0].strip('/').replace('/', '_')}_{h}.json"

    # ------------------------------------------------------------ 各API
    def master(self, code: str | None = None, date_: str | None = None) -> list[dict]:
        return self.get("/equities/master", code=code and code5(code), date=date_)

    def valuation(self, code: str | None = None, date_: str | None = None, from_: str | None = None, to: str | None = None) -> list[dict]:
        return self.get("/equities/valuation", code=code and code5(code), date=date_, **{"from": from_}, to=to)

    def fins_summary(self, code: str) -> list[dict]:
        return self.get("/fins/summary", code=code5(code))

    def earnings_date(self, code: str) -> list[dict]:
        return self.get("/fins/earnings-date", code=code5(code))

    def margin_interest(self, code: str | None = None, date_: str | None = None, from_: str | None = None, to: str | None = None) -> list[dict]:
        return self.get("/markets/margin-interest", code=code and code5(code), date=date_, **{"from": from_}, to=to)

    def short_ratio(self, date_: str | None = None, s33: str | None = None, from_: str | None = None, to: str | None = None) -> list[dict]:
        return self.get("/markets/short-ratio", date=date_, s33=s33, **{"from": from_}, to=to)

    def short_sale_report(self, code: str, disc_date_from: str, disc_date_to: str) -> list[dict]:
        return self.get("/markets/short-sale-report", code=code5(code), disc_date_from=disc_date_from, disc_date_to=disc_date_to)

    def td_list(self, code: str, from_: str, to: str) -> list[dict]:
        return self.get("/td/list", code=code5(code), **{"from": from_}, to=to)

    def td_files(self, disc_no: str, docs: str = "g") -> dict:
        """適時開示ファイルのダウンロードURL（有効期限15分）。"""
        body = self._request("/td/files", {"discNo": disc_no, "docs": docs})
        return body.get("files") or {}

    def download(self, url: str) -> bytes:
        r = self.session.get(url, timeout=60)
        r.raise_for_status()
        return r.content

    # ------------------------------------------------------------ 便利関数
    def latest_earnings_pdf(self, code: str, lookback_days: int = 200) -> tuple[str, str, bytes] | None:
        """直近の決算短信（全文PDF）を取得する。TDnet/適時開示情報アドオンが必要。"""
        today = date.today()
        rows = self.td_list(code, (today - timedelta(days=lookback_days)).isoformat(), today.isoformat())
        cands = [r for r in rows if "決算短信" in (r.get("Title") or "")
                 and r.get("DiscStatus") != "delete" and "g" in (r.get("Docs") or [])]
        if not cands:
            return None
        r = max(cands, key=lambda r: (r.get("DiscDate", ""), r.get("DiscTime", ""), r.get("DiscNo", "")))
        url = self.td_files(r["DiscNo"], docs="g").get("pdf")
        if not url:
            return None
        return r.get("Title", "決算短信"), r.get("DiscDate", ""), self.download(url)

    def latest_date(self, kind: str = "valuation", probe_code: str = "7203", lookback_days: int = 150) -> str | None:
        """そのAPIで取得できる最新の日付を、代表銘柄の直近データから調べる（Freeプランは12週間遅れ）。"""
        today = date.today()
        frm = (today - timedelta(days=lookback_days)).isoformat()
        fetch = {"valuation": self.valuation, "margin": self.margin_interest}[kind]
        rows = fetch(code=probe_code, from_=frm, to=today.isoformat())
        dates = sorted(r["Date"] for r in rows if r.get("Date"))
        return dates[-1] if dates else None

    def check_access(self) -> dict[str, str]:
        """各データの利用可否を確認する（接続テスト用）。"""
        today = date.today()
        frm = (today - timedelta(days=40)).isoformat()
        tests = {
            "バリュエーション指標": lambda: self.valuation(code="7203", from_=frm, to=today.isoformat()),
            "財務情報（決算短信）": lambda: self.fins_summary("7203"),
            "信用取引残高": lambda: self.margin_interest(code="7203", from_=frm, to=today.isoformat()),
            "空売り残高報告": lambda: self.short_sale_report("7203", frm, today.isoformat()),
            "TDnet適時開示": lambda: self.td_list("7203", frm, today.isoformat()),
        }
        out = {}
        for name, fn in tests.items():
            try:
                rows = fn()
                latest = max((r.get("Date") or r.get("DiscDate") or "" for r in rows), default="")
                if name == "バリュエーション指標" and latest and (today - date.fromisoformat(latest)).days > 30:
                    out[name] = f"利用可（最新 {latest}・遅延あり。Freeプランは12週間遅れ）"
                else:
                    out[name] = f"利用可（最新 {latest}）" if latest else "利用可（該当データなし）"
            except PlanError as e:
                out[name] = f"利用不可: {e}"
            except JQuantsError as e:
                out[name] = f"エラー: {e}"
        return out


def _message(r: requests.Response) -> str:
    try:
        return str(r.json().get("message", r.text))[:200]
    except ValueError:
        return r.text[:200]
