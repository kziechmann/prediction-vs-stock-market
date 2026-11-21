#!/usr/bin/env python3
"""Weekly scan: prediction markets about listed companies vs. the stocks themselves.

Usage (from the repo root):
  python3 info_arb/scan.py               # live fetch + score + write data/
  python3 info_arb/scan.py --rescore     # re-score cached inputs after editing analysis/notes.json
  python3 info_arb/scan.py --inputs F    # score a saved inputs file (tests, offline runs)

Outputs (info_arb/data/):
  latest.json            everything the dashboard needs
  candidates.md          compact list for the weekly analyst (Claude) to review
  snapshots/DATE.json    flagged signals, used later for the track record
  cache/inputs.json      raw normalized inputs, so --rescore needs no network (git-ignored)
Exit codes: 0 ok, 2 no market source reachable, 3 no stock data reachable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from infoarb import kalshi, polymarket, signals, stocks, track
from infoarb.matching import load_index
from infoarb.util import iso, utcnow

ROOT = Path(__file__).resolve().parent


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def rounded(obj, nd=5):
    if isinstance(obj, float):
        return round(obj, nd)
    if isinstance(obj, dict):
        return {k: rounded(v, nd) for k, v in obj.items()}
    if isinstance(obj, list):
        return [rounded(v, nd) for v in obj]
    return obj


def fetch_inputs(cfg: dict, index, snapshots: list, now) -> dict:
    src_cfg = cfg["sources"]
    markets, sources = [], {}
    if src_cfg["polymarket"]["enabled"]:
        pc = src_cfg["polymarket"]
        events, status = polymarket.fetch_events(pc["max_events"], pc["tag_slugs"], pc["max_events_per_tag"])
        pm = polymarket.normalize(events)
        status["markets"] = len(pm)
        sources["polymarket"] = status
        markets += pm
        print(f"polymarket: {status}", file=sys.stderr)
    if src_cfg["kalshi"]["enabled"]:
        kc = src_cfg["kalshi"]
        events, status = kalshi.fetch_events(kc["series_categories"], kc["max_series"], kc["max_pages"],
                                             kc["skip_categories"], kc["skip_series_prefixes"])
        km = kalshi.normalize(events, kc["taker_fee_rate"])
        linked_k = [m for m in km if index.link_market(m)]
        status.update(markets=len(km), **kalshi.enrich_history(linked_k, kc["max_history_calls"],
                                                                cfg["event_signal"]["window_days"]))
        sources["kalshi"] = status
        markets += km
        print(f"kalshi: {status}", file=sys.stderr)

    tickers = {cfg["sources"]["stocks"]["benchmark"]}
    for m in markets:
        tickers.update(link.ticker for link in index.link_market(m))
    tickers |= track.tickers_in(snapshots, 70, now.date())
    sc = cfg["sources"]["stocks"]
    hist, errors = stocks.fetch_many(tickers, sc["history_range"], sc["request_pause_seconds"])
    sources["stocks"] = {"ok": bool(hist), "tickers": len(hist), "failed": len(errors),
                         "errors": dict(list(errors.items())[:10])}
    print(f"stocks: {len(hist)} ok, {len(errors)} failed", file=sys.stderr)
    for m in markets:
        m["description"] = (m.get("description") or "")[:300]
    return {"fetched_at": iso(now), "markets": markets, "stocks": hist, "sources": sources}


def candidates_md(result: dict, generated: str) -> str:
    lines = [f"# Candidates for analyst review — {generated}", "",
             "Each row's `key` is what analysis/notes.json uses. Prob/edge in probability points.", ""]
    lines += ["## Flagged opportunities", "",
              "| # | key | ticker | signal | question | prob | Δ7d | stock abn 7d | model / edge | score |",
              "|---|---|---|---|---|---|---|---|---|---|"]

    def pct(x, signed=False):
        return "—" if x is None else (f"{x:+.1%}" if signed else f"{x:.0%}")

    for r in result["opportunities"]:
        model = f"{r['model_prob']:.0%} / {r['edge']:+.2f}" if r["signal"] == "price" else f"gap {r['gap_z']:+.1f}σ" if r.get("gap_z") is not None else "dir?"
        q = r["question"].replace("|", "/")[:110]
        lines.append(f"| {r['rank']} | `{r['key']}` | {r['ticker']} | {r['signal']}/{r['category']} | [{q}]({r['url']}) | "
                     f"{pct(r['prob'])} | {pct(r.get('prob_change_7d'), True)} | {pct(r.get('stock_abn_7d'), True)} | {model} | {r['score']:.2f} |")
    lines += ["", "Readings:", ""]
    for r in result["opportunities"]:
        lines.append(f"- **#{r['rank']} {r['ticker']}** — {r['reading']} → {r['action']}")
    need = [r for r in result["event_signals"] if not r.get("direction")][:15]
    if need:
        lines += ["", "## Big odds moves that need a direction call", "",
                  "| key | ticker | category | question | prob | Δ7d | stock abn 7d |", "|---|---|---|---|---|---|---|"]
        for r in need:
            lines.append(f"| `{r['key']}` | {r['ticker']} | {r['category']} | [{r['question'][:110]}]({r['url']}) | "
                         f"{pct(r['prob'])} | {pct(r.get('prob_change_7d'), True)} | {pct(r.get('stock_abn_7d'), True)} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rescore", action="store_true", help="re-score data/cache/inputs.json without fetching")
    ap.add_argument("--inputs", type=Path, help="score this inputs file instead of fetching")
    ap.add_argument("--out", type=Path, default=ROOT / "data", help="output folder (default info_arb/data)")
    ap.add_argument("--notes", type=Path, default=ROOT / "analysis" / "notes.json")
    ap.add_argument("--no-snapshot", action="store_true", help="don't write a dated snapshot")
    args = ap.parse_args(argv)

    cfg = load_json(ROOT / "config.json", None)
    index = load_index(ROOT / "companies.json")
    notes = load_json(args.notes, {"markets": {}, "tickers": {}})
    out = args.out
    (out / "cache").mkdir(parents=True, exist_ok=True)
    (out / "snapshots").mkdir(parents=True, exist_ok=True)
    snaps = track.load_snapshots(out / "snapshots")
    now = utcnow()

    if args.inputs or args.rescore:
        path = args.inputs or (out / "cache" / "inputs.json")
        inputs = load_json(path, None)
        if not inputs:
            print(f"no inputs at {path}; run a live scan first", file=sys.stderr)
            return 2
    else:
        inputs = fetch_inputs(cfg, index, snaps, now)

    if not any(s.get("ok") for k, s in inputs["sources"].items() if k != "stocks"):
        print("no prediction-market source was reachable:", json.dumps(inputs["sources"]), file=sys.stderr)
        return 2
    if not inputs["stocks"]:
        print("no stock prices were reachable:", json.dumps(inputs["sources"].get("stocks")), file=sys.stderr)
        return 3
    if not (args.inputs or args.rescore):
        (out / "cache" / "inputs.json").write_text(json.dumps(inputs))

    result = signals.build(inputs["markets"], inputs["stocks"], index, notes, cfg, now)
    bench = cfg["sources"]["stocks"]["benchmark"]
    today = now.date()
    prior = [s for s in snaps if s.get("date") != today.isoformat()]
    by_key = {m["key"]: m for m in inputs["markets"]}
    record = track.evaluate(prior, by_key, result["stocks"], bench, today)

    used = {r["ticker"] for k in ("opportunities", "price_signals", "event_signals", "watchlist") for r in result[k]}
    used |= {r["ticker"] for r in record["rows"]} | {bench}
    latest = {
        "generated_at": iso(now),
        "inputs_fetched_at": inputs.get("fetched_at"),
        "benchmark": bench,
        "sources": inputs["sources"],
        "counts": result["counts"],
        "config": {k: cfg[k] for k in ("filters", "price_signal", "event_signal", "ranking")},
        "opportunities": result["opportunities"],
        "price_signals": result["price_signals"][:200],
        "event_signals": result["event_signals"][:200],
        "watchlist": result["watchlist"],
        "track_record": record,
        "stocks": {k: v for k, v in result["stocks"].items() if k in used},
    }
    (out / "latest.json").write_text(json.dumps(rounded(latest), indent=1))
    (out / "candidates.md").write_text(candidates_md(result, latest["generated_at"]))
    if not args.no_snapshot:
        snap = {"date": today.isoformat(), "generated_at": iso(now),
                "opportunities": track.snapshot_rows(result["opportunities"], result["stocks"], bench)}
        (out / "snapshots" / f"{today.isoformat()}.json").write_text(json.dumps(rounded(snap), indent=1))

    c = result["counts"]
    print(f"scanned {c['markets']} markets, linked {c['linked']}, "
          f"{len(result['opportunities'])} opportunities "
          f"({sum(1 for r in result['opportunities'] if r['signal'] == 'price')} price, "
          f"{sum(1 for r in result['opportunities'] if r['signal'] == 'event')} event)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
