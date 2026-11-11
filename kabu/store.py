"""分析結果・判断メモの保存と、過去の分析の事後検証。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import config
from .data import fetch_prices


def save_run(run: dict) -> Path:
    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.fromisoformat(run["created_at"]).strftime("%Y-%m-%d_%H%M%S")
    path = config.RUNS_DIR / f"{ts}_{run['mode']}.json"
    n = 2
    while path.exists():  # 同じ秒に複数回実行した場合
        path = config.RUNS_DIR / f"{ts}_{run['mode']}_{n}.json"
        n += 1
    path.write_text(json.dumps(run, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def load_runs() -> list[dict]:
    runs = []
    for p in sorted(config.RUNS_DIR.glob("*.json"), reverse=True):
        try:
            run = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        run["_file"] = p.name
        runs.append(run)
    return runs


# ---------------------------------------------------------------- 判断メモ（判断するのは人間）

DECISIONS = ["未判断", "監視リストに入れる", "さらに調べる", "購入を検討", "見送り"]


def load_decisions() -> dict:
    try:
        return json.loads(config.DECISIONS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_decision(run_file: str, code: str, decision: str, memo: str) -> None:
    d = load_decisions()
    d[f"{run_file}:{code}"] = {"decision": decision, "memo": memo, "updated_at": datetime.now().isoformat(timespec="seconds")}
    config.DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.DECISIONS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 事後検証

def evaluate(runs: list[dict], demo: bool | None = None) -> pd.DataFrame:
    """過去に分析した銘柄の、分析時点からの騰落率を日経平均と比べる（AIの優先度づけが役に立ったかの検証用）。"""
    decisions = load_decisions()
    rows = []
    for run in runs:
        for c in run.get("companies", []):
            if not c.get("price_at_analysis"):
                continue
            a = c.get("audit") or {}
            dec = decisions.get(f"{run.get('_file')}:{c['code']}", {})
            rows.append({
                "分析日": run["created_at"][:10],
                "テーマ": run.get("theme") or "（個別監査）",
                "コード": c["code"],
                "銘柄名": c.get("name", ""),
                "AIの優先度": a.get("priority", "-"),
                "織り込み度": a.get("priced_in", "-"),
                "あなたの判断": dec.get("decision", "未判断"),
                "分析時株価": c["price_at_analysis"],
                "_bench": run.get("benchmark_at_analysis"),
                "_date": run["created_at"],
            })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    prices = fetch_prices(sorted({f"{c}.T" for c in df["コード"]}) + [config.BENCHMARK], period="5d", demo=demo)
    latest = {t: float(p["Close"].iloc[-1]) for t, p in prices.items()}
    bench_now = latest.get(config.BENCHMARK)
    df["現在株価"] = df["コード"].map(lambda c: latest.get(f"{c}.T"))
    df["経過日数"] = (pd.Timestamp.now() - pd.to_datetime(df["_date"])).dt.days
    df["騰落率%"] = ((df["現在株価"] / df["分析時株価"] - 1) * 100).round(2)
    if bench_now:
        df["日経平均比%"] = (df["騰落率%"] - (bench_now / df["_bench"] - 1) * 100).round(2)
    return df.drop(columns=["_bench", "_date"])
