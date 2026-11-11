"""株価から計算する指標（すべてコードで計算し、AIには計算させない）。"""

from __future__ import annotations

import numpy as np
import pandas as pd


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


def _r(x, nd=2):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def price_metrics(df: pd.DataFrame) -> dict:
    """日足から、中長期の位置と過熱感を表す指標を作る。"""
    c = df["Close"].astype(float)
    v = df["Volume"].astype(float)
    last = float(c.iloc[-1])

    def ret(n: int):
        return (last / float(c.iloc[-n - 1]) - 1) * 100 if len(c) > n else None

    def dev(n: int):
        return (last / float(c.tail(n).mean()) - 1) * 100 if len(c) >= n else None

    year = df.tail(250)
    daily = c.pct_change()
    vol25 = float(v.tail(25).mean()) if len(v) else None
    return {
        "終値": _r(last, 1),
        "株価日付": df.index[-1].strftime("%Y-%m-%d"),
        "1ヶ月騰落%": _r(ret(20)),
        "3ヶ月騰落%": _r(ret(60)),
        "6ヶ月騰落%": _r(ret(120)),
        "1年騰落%": _r(ret(245)),
        "52週高値から%": _r((last / float(year["High"].max()) - 1) * 100),
        "52週安値から%": _r((last / float(year["Low"].min()) - 1) * 100),
        "25日線乖離%": _r(dev(25)),
        "200日線乖離%": _r(dev(200)),
        "RSI14": _r(rsi(c).iloc[-1], 1),
        "年率ボラ%": _r(daily.tail(60).std() * np.sqrt(252) * 100, 1),
        "25日平均出来高": int(vol25) if vol25 else None,
        "売買代金20日平均(億円)": _r(float((c * v).tail(20).mean()) / 1e8, 1),
    }
