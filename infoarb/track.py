"""Weekly snapshots of flagged signals, and how those calls have done since."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path


def snapshot_rows(opportunities: list[dict], stats: dict, bench_sym: str) -> list[dict]:
    bench_px = (stats.get(bench_sym) or {}).get("price")
    rows = []
    for r in opportunities:
        rows.append({
            "key": r["key"], "ticker": r["ticker"], "signal": r["signal"], "question": r["question"],
            "url": r["url"], "platform": r["platform"], "score": r["score"],
            "stock_side": r.get("stock_side", 0), "stock_price": r.get("stock_price"), "bench_price": bench_px,
            "trade_side": r.get("trade_side"), "entry": r.get("entry"), "market_prob": r.get("prob"),
        })
    return rows


def load_snapshots(folder: Path) -> list[dict]:
    out = []
    for p in sorted(folder.glob("*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def tickers_in(snapshots: list[dict], max_age_days: int, today: date) -> set[str]:
    keep = set()
    for s in snapshots:
        try:
            age = (today - date.fromisoformat(s["date"])).days
        except (KeyError, ValueError):
            continue
        if age <= max_age_days:
            keep.update(r["ticker"] for r in s.get("opportunities", []))
    return keep


def evaluate(snapshots: list[dict], markets_by_key: dict, stats: dict, bench_sym: str, today: date,
             min_age_days: int = 5, max_age_days: int = 70) -> dict:
    bench_now = (stats.get(bench_sym) or {}).get("price")
    rows = []
    for s in snapshots:
        try:
            flagged_on = date.fromisoformat(s["date"])
        except (KeyError, ValueError):
            continue
        age = (today - flagged_on).days
        if age < min_age_days or age > max_age_days:
            continue
        for o in s.get("opportunities", []):
            st = stats.get(o["ticker"]) or {}
            px_now, px_then, b_then = st.get("price"), o.get("stock_price"), o.get("bench_price")
            stock_ret = (px_now / px_then - 1) if (px_now and px_then) else None
            bench_ret = (bench_now / b_then - 1) if (bench_now and b_then) else None
            abn = (stock_ret - bench_ret) if (stock_ret is not None and bench_ret is not None) else None
            equity = o["stock_side"] * abn if (o.get("stock_side") and abn is not None) else None
            m = markets_by_key.get(o["key"])
            pm = None
            if m and o.get("entry") is not None and o.get("trade_side"):
                now_px = m["prob"] if o["trade_side"] == "YES" else 1 - m["prob"]
                pm = now_px - o["entry"]
            rows.append({**o, "flagged_on": s["date"], "age_days": age, "stock_ret": stock_ret,
                         "abn_ret": abn, "equity_pnl": equity, "pm_pnl": pm,
                         "market_prob_now": m["prob"] if m else None, "market_open": bool(m)})
    eq = [r["equity_pnl"] for r in rows if r["equity_pnl"] is not None]
    pm = [r["pm_pnl"] for r in rows if r["pm_pnl"] is not None]
    summary = {
        "signals": len(rows),
        "equity_calls": len(eq),
        "equity_hit_rate": (sum(1 for x in eq if x > 0) / len(eq)) if eq else None,
        "equity_avg": (sum(eq) / len(eq)) if eq else None,
        "pm_calls": len(pm),
        "pm_hit_rate": (sum(1 for x in pm if x > 0) / len(pm)) if pm else None,
        "pm_avg": (sum(pm) / len(pm)) if pm else None,
    }
    rows.sort(key=lambda r: (r["flagged_on"], r["score"]), reverse=True)
    return {"summary": summary, "rows": rows}
