"""Plotly チャート（中長期の位置を見るため、1年の日足に25/75/200日線を重ねる）。"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


def candle(df: pd.DataFrame, title: str = "", days: int = 250) -> go.Figure:
    d = df.copy()
    for n in (25, 75, 200):
        d[f"SMA{n}"] = d["Close"].rolling(n).mean()
    d = d.tail(days)

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.78, 0.22], vertical_spacing=0.03)
    # 日本式: 陽線=赤, 陰線=青緑
    fig.add_trace(go.Candlestick(x=d.index, open=d["Open"], high=d["High"], low=d["Low"], close=d["Close"],
                                 increasing_line_color="#e0464e", decreasing_line_color="#1f9e89",
                                 name="株価", showlegend=False), row=1, col=1)
    for n, color in ((25, "#f2a541"), (75, "#6c8ebf"), (200, "#9b59b6")):
        fig.add_trace(go.Scatter(x=d.index, y=d[f"SMA{n}"], name=f"{n}日線", line=dict(width=1.4, color=color)), row=1, col=1)
    colors = ["#e0464e" if c >= o else "#1f9e89" for o, c in zip(d["Open"], d["Close"])]
    fig.add_trace(go.Bar(x=d.index, y=d["Volume"], marker_color=colors, name="出来高", showlegend=False), row=2, col=1)
    fig.update_layout(title=title, height=430, margin=dict(l=10, r=10, t=40 if title else 10, b=10),
                      xaxis_rangeslider_visible=False, legend=dict(orientation="h", y=1.02, x=1, xanchor="right"))
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    return fig
