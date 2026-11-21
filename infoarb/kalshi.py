"""Kalshi adapter (public trade API v2; market data needs no key).

Docs: https://docs.kalshi.com — /events?with_nested_markets=true is paginated by cursor.
Kalshi has been moving prices from integer cents (yes_bid) to dollar strings
(yes_bid_dollars); both are handled.
"""

from __future__ import annotations

import time

from . import net
from .util import fnum, iso, parse_time, utcnow

BASE = "https://api.elections.kalshi.com/trade-api/v2"


def _price(m: dict, key: str) -> float | None:
    """Return a 0-1 price from either '<key>_dollars' or integer-cents '<key>'."""
    dollars = fnum(m.get(f"{key}_dollars"))
    if dollars is not None:
        return dollars
    cents = fnum(m.get(key))
    return None if cents is None else cents / 100.0


def _qty(m: dict, key: str) -> float | None:
    for k in (f"{key}_fp", key):
        v = fnum(m.get(k))
        if v is not None:
            return v
    return None


def fetch_events(series_categories=(), max_series: int = 400, max_pages: int = 60,
                 skip_categories=(), skip_series_prefixes=()) -> tuple[list, dict]:
    """Return (events with nested markets, status).

    Preferred path: list series in the relevant categories, then fetch each series' open
    events. This avoids paging through thousands of sports events. Falls back to paging
    all open events if the series listing is unavailable or empty.
    """
    warnings: list[str] = []
    series: list[tuple[str, str]] = []
    for cat in series_categories:
        try:
            data = net.get(f"{BASE}/series", {"category": cat})
        except net.FetchError as e:
            warnings.append(f"series {cat}: {e.reason}")
            if e.status in (401, 403, 407) or e.status is None:
                break  # blocked or unreachable: the fallback will report it
            continue
        for s in data.get("series") or []:
            t = s.get("ticker")
            if t and not any(t.startswith(p) for p in skip_series_prefixes):
                series.append((t, s.get("category") or cat))
    if series:
        events: list[dict] = []
        for ticker, cat in series[:max_series]:
            cursor = None
            for _ in range(5):
                params = {"series_ticker": ticker, "status": "open", "with_nested_markets": "true", "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                try:
                    data = net.get(f"{BASE}/events", params, retries=2)
                except net.FetchError as e:
                    warnings.append(f"{ticker}: {e.reason}")
                    break
                for ev in data.get("events") or []:
                    ev.setdefault("category", cat)
                    ev.setdefault("series_ticker", ticker)
                    events.append(ev)
                cursor = data.get("cursor")
                if not cursor:
                    break
            time.sleep(0.05)
        if events:
            status = {"ok": True, "mode": "series", "series": min(len(series), max_series), "events": len(events)}
            if warnings:
                status["warnings"] = warnings[:5]
            return events, status

    events, cursor, pages, skipped = [], None, 0, 0
    while pages < max_pages:
        params = {"status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            data = net.get(f"{BASE}/events", params)
        except net.FetchError as e:
            if not events:
                return [], {"ok": False, "error": e.reason, "events": 0}
            warnings.append(f"stopped at page {pages}: {e.reason}")
            break
        pages += 1
        for ev in data.get("events") or []:
            ser = ev.get("series_ticker") or ""
            if ev.get("category") in skip_categories or any(ser.startswith(p) for p in skip_series_prefixes):
                skipped += 1
                continue
            events.append(ev)
        cursor = data.get("cursor")
        if not cursor:
            break
    status = {"ok": True, "mode": "paged", "events": len(events), "skipped": skipped, "pages": pages}
    if warnings:
        status["warnings"] = warnings[:5]
    return events, status


def normalize(events: list, fee_rate: float = 0.07) -> list[dict]:
    out = []
    for ev in events:
        series = ev.get("series_ticker") or ""
        markets = ev.get("markets") or []
        multi = len(markets) > 1
        for m in markets:
            status = (m.get("status") or "").lower()
            if status and status not in ("active", "open", "initialized"):
                continue
            bid, ask = _price(m, "yes_bid"), _price(m, "yes_ask")
            last, prev = _price(m, "last_price"), _price(m, "previous_price")
            if bid is not None and ask is not None and 0 < bid <= ask < 1 and ask - bid <= 0.15:
                prob = (bid + ask) / 2
            else:
                prob = last
            if prob is None:
                continue
            if bid is not None and bid <= 0:
                bid = None
            if ask is not None and (ask <= 0 or ask >= 1):
                ask = None
            title = m.get("title") or ev.get("title") or ""
            sub = m.get("yes_sub_title") or m.get("subtitle") or ""
            question = f"{title} — {sub}" if multi and sub and sub not in title else title
            out.append({
                "platform": "kalshi",
                "key": f"kalshi:{m.get('ticker')}",
                "id": m.get("ticker"),
                "ticker": m.get("ticker"),
                "series_ticker": series,
                "event_id": ev.get("event_ticker"),
                "event_title": ev.get("title") or "",
                "question": question,
                "outcome_label": sub or None,
                "yes_label": "Yes",
                "url": f"https://kalshi.com/markets/{series.lower()}" if series else "https://kalshi.com",
                "prob": prob,
                "bid": bid,
                "ask": ask,
                "prob_change_1d": (last - prev) if (last is not None and prev) else None,
                "prob_change_7d": None,  # filled by enrich_history for linked markets
                "prob_change_30d": None,
                "volume_total": _qty(m, "volume") or 0.0,
                "volume_24h": _qty(m, "volume_24h"),
                "volume_7d": None,
                "liquidity": _price(m, "liquidity") or None,  # dollars; deprecated by Kalshi, often 0
                "open_interest": _qty(m, "open_interest"),
                "volume_unit": "contracts",
                "start_date": iso(parse_time(m.get("open_time"))),
                "end_date": iso(parse_time(m.get("close_time") or m.get("expiration_time"))),
                "tags": [ev.get("category")] if ev.get("category") else [],
                "description": (m.get("rules_primary") or "")[:600],
                "strike_type": m.get("strike_type"),
                "floor_strike": fnum(m.get("floor_strike")),
                "cap_strike": fnum(m.get("cap_strike")),
                "fee_rate": fee_rate,
            })
    return out


def enrich_history(markets: list[dict], max_calls: int = 150, windows=(7, 30)) -> dict:
    """Fill prob_change_<w>d from daily candlesticks for the given (already linked) markets.

    If a market is younger than a window, the change is measured from its first candle.
    """
    now = int(utcnow().timestamp())
    start = now - (max(windows) + 2) * 86400
    calls = ok = 0
    for m in markets:
        if m.get("platform") != "kalshi" or calls >= max_calls:
            continue
        calls += 1
        url = f"{BASE}/series/{m['series_ticker']}/markets/{m['ticker']}/candlesticks"
        try:
            data = net.get(url, {"start_ts": start, "end_ts": now, "period_interval": 1440}, retries=2)
        except net.FetchError:
            continue
        points = [(fnum(c.get("end_period_ts"), 0), _candle_price(c)) for c in data.get("candlesticks") or []]
        points = sorted((t, p) for t, p in points if p is not None)
        if not points:
            continue
        for w in windows:
            target = now - w * 86400
            before = [p for t, p in points if t <= target]
            base = before[-1] if before else points[0][1]
            m[f"prob_change_{w}d"] = m["prob"] - base
        ok += 1
        time.sleep(0.1)
    return {"history_calls": calls, "history_ok": ok}


def _candle_price(c: dict) -> float | None:
    price = c.get("price") or {}
    close = _price(price, "close")
    if close:
        return close
    yb, ya = _price(c.get("yes_bid") or {}, "close"), _price(c.get("yes_ask") or {}, "close")
    if yb is not None and ya is not None and ya > 0:
        return (yb + ya) / 2
    return None
