"""Daily stock history: Yahoo Finance chart API, with Stooq CSV as a fallback."""

from __future__ import annotations

import csv
import io
import time
from datetime import datetime, timezone

from . import net
from .util import fnum


def _yahoo(symbol: str, range_: str) -> dict:
    last = None
    for host in ("query1", "query2"):
        try:
            data = net.get(
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}",
                {"range": range_, "interval": "1d", "includePrePost": "false", "events": "div,splits"},
                retries=2,
            )
        except net.FetchError as e:
            last = e
            continue
        result = ((data.get("chart") or {}).get("result") or [None])[0]
        if not result:
            raise net.FetchError(f"yahoo:{symbol}", "no chart result")
        ts = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        rows = []
        for i, t in enumerate(ts):
            c = fnum((quote.get("close") or [None] * len(ts))[i])
            if c is None:
                continue
            h = fnum((quote.get("high") or [None] * len(ts))[i], c)
            lo = fnum((quote.get("low") or [None] * len(ts))[i], c)
            rows.append((datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat(), c, h, lo))
        meta = result.get("meta") or {}
        return _pack(symbol, rows, "yahoo", fnum(meta.get("regularMarketPrice")),
                     meta.get("currency"), meta.get("longName") or meta.get("shortName"))
    raise last or net.FetchError(f"yahoo:{symbol}", "unreachable")


def _stooq(symbol: str) -> dict:
    sym = symbol.lower().replace(".", "-")
    if "." not in sym and not sym.endswith(".us"):
        sym = f"{sym}.us"
    text = net.get("https://stooq.com/q/d/l/", {"s": sym, "i": "d"}, as_json=False, retries=2)
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for r in reader:
        c = fnum(r.get("Close"))
        if c is None or not r.get("Date"):
            continue
        rows.append((r["Date"], c, fnum(r.get("High"), c), fnum(r.get("Low"), c)))
    if not rows:
        raise net.FetchError(f"stooq:{symbol}", "empty CSV (symbol unknown or download blocked)")
    return _pack(symbol, rows[-130:], "stooq", None, "USD", None)


def _pack(symbol, rows, source, live_price, currency, name) -> dict:
    if not rows:
        raise net.FetchError(f"{source}:{symbol}", "no price rows")
    dates = [r[0] for r in rows]
    close = [r[1] for r in rows]
    return {
        "symbol": symbol,
        "name": name,
        "source": source,
        "currency": currency or "USD",
        "price": live_price or close[-1],
        "last_close": close[-1],
        "as_of": dates[-1],
        "dates": dates,
        "close": close,
        "high": [r[2] for r in rows],
        "low": [r[3] for r in rows],
    }


def fetch_history(symbol: str, range_: str = "6mo") -> dict:
    try:
        return _yahoo(symbol, range_)
    except net.FetchError as first:
        try:
            return _stooq(symbol)
        except net.FetchError as second:
            raise net.FetchError(symbol, f"yahoo: {first.reason}; stooq: {second.reason}")


def fetch_many(symbols, range_: str = "6mo", pause: float = 0.35) -> tuple[dict, dict]:
    out, errors = {}, {}
    for s in sorted(set(symbols)):
        try:
            out[s] = fetch_history(s, range_)
        except net.FetchError as e:
            errors[s] = e.reason
        time.sleep(pause)
    return out, errors
