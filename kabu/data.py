"""Yahoo Finance（yfinance）からの株価・基本指標の取得。

KABU_DEMO=1（またはdemo=True）のときはネットに接続せず、合成データを返す。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import config

OHLCV = ["Open", "High", "Low", "Close", "Volume"]


@dataclass
class Stock:
    code: str
    name: str
    sector: str

    @property
    def ticker(self) -> str:
        return f"{self.code}.T"


def load_universe(path=config.UNIVERSE_CSV) -> list[Stock]:
    df = pd.read_csv(path, dtype=str).fillna("")
    return [Stock(r.code.strip(), r.name.strip(), r.sector.strip()) for r in df.itertuples() if r.code.strip()]


def universe_names() -> dict[str, tuple[str, str]]:
    return {s.code: (s.name, s.sector) for s in load_universe()}


# ---------------------------------------------------------------- 株価

def fetch_prices(tickers: list[str], period: str = "2y", demo: bool | None = None) -> dict[str, pd.DataFrame]:
    """日足 OHLCV を取得し {ticker: DataFrame} で返す。取得できなかった銘柄は含まれない。"""
    demo = config.DEMO_MODE if demo is None else demo
    tickers = list(dict.fromkeys(tickers))
    if not tickers:
        return {}
    if demo:
        return {t: _synthetic_prices(t) for t in tickers}

    import yfinance as yf

    raw = yf.download(tickers, period=period, interval="1d", group_by="ticker",
                      auto_adjust=True, progress=False, threads=True)
    out: dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
        except KeyError:
            continue
        df = df[[c for c in OHLCV if c in df.columns]].dropna(subset=["Close"])
        if len(df) >= 30:
            out[t] = df
    return out


def _seed(text: str) -> int:
    return int(hashlib.md5(text.encode()).hexdigest()[:8], 16)


def _synthetic_prices(ticker: str, days: int = 500) -> pd.DataFrame:
    """銘柄ごとに決まった乱数でランダムウォークを作る（デモ用）。"""
    rng = np.random.default_rng(_seed(ticker))
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=days)
    base = {"^N225": 40000.0, "JPY=X": 150.0, "1306.T": 400.0}.get(ticker, float(rng.uniform(500, 15000)))
    drift = rng.normal(0.0004, 0.0008)
    vol = 0.006 if ticker == "JPY=X" else rng.uniform(0.012, 0.03)
    close = base * np.exp(np.cumsum(rng.normal(drift, vol, days)))
    open_ = close * (1 + rng.normal(0, vol / 3, days))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, vol / 2, days)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, vol / 2, days)))
    volume = rng.lognormal(mean=np.log(3e6), sigma=0.4, size=days)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}, index=idx)


# ---------------------------------------------------------------- 基本指標（J-Quantsがない場合の代替）

def yahoo_fundamentals(ticker: str, demo: bool | None = None) -> dict:
    """PER・PBRなどを Yahoo Finance から取得。取れない項目は None。"""
    demo = config.DEMO_MODE if demo is None else demo
    if demo:
        rng = np.random.default_rng(_seed(ticker) + 1)
        return {
            "PER": round(float(rng.uniform(6, 45)), 1),
            "予想PER": round(float(rng.uniform(6, 40)), 1),
            "PBR": round(float(rng.uniform(0.5, 6)), 2),
            "ROE%": round(float(rng.uniform(2, 25)), 1),
            "配当利回り%": round(float(rng.uniform(0, 5)), 2),
            "時価総額(億円)": int(rng.uniform(3000, 400000)),
            "売上成長率%(前年同期比)": round(float(rng.normal(5, 8)), 1),
            "利益成長率%(前年同期比)": round(float(rng.normal(8, 20)), 1),
        }

    import yfinance as yf

    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:
        info = {}

    def g(key, scale=1.0, nd=2):
        v = info.get(key)
        return None if v is None else round(float(v) * scale, nd)

    mcap = info.get("marketCap")
    return {
        "PER": g("trailingPE"),
        "予想PER": g("forwardPE"),
        "PBR": g("priceToBook"),
        "ROE%": g("returnOnEquity", 100, 1),
        # yfinance 1.x の dividendYield はパーセント値（例: 0.33 = 0.33%）
        "配当利回り%": g("dividendYield"),
        "時価総額(億円)": int(mcap / 1e8) if mcap else None,
        "売上成長率%(前年同期比)": g("revenueGrowth", 100, 1),
        "利益成長率%(前年同期比)": g("earningsGrowth", 100, 1),
    }
