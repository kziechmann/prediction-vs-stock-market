"""Polymarket adapter (public Gamma API, no key needed).

Docs: https://docs.polymarket.com  — /events returns events with nested markets.
Several array fields (outcomes, outcomePrices, clobTokenIds) arrive as JSON-encoded
strings; numbers sometimes arrive as strings. Everything is parsed defensively.
"""

from __future__ import annotations

from . import net
from .util import fnum, iso, json_list, parse_time

GAMMA = "https://gamma-api.polymarket.com"
PAGE = 100
# Tried in order; the first one the API accepts is used for the whole run.
ORDER_FIELDS = ("volume1wk", "volume24hr", "volume")


def _page(params: dict, order_state: dict) -> list:
    """Fetch one page of events, falling back through ORDER_FIELDS if one is rejected."""
    orders = [order_state["order"]] if order_state.get("order") else list(ORDER_FIELDS) + [None]
    last_err = None
    for order in orders:
        p = dict(params)
        if order:
            p.update(order=order, ascending="false")
        try:
            data = net.get(f"{GAMMA}/events", p)
            order_state["order"] = order
            if isinstance(data, dict):  # some deployments wrap results
                data = data.get("data") or data.get("events") or []
            return data if isinstance(data, list) else []
        except net.FetchError as e:
            last_err = e
            if e.status is None or e.status >= 500 or e.status == 429:
                raise
    raise last_err  # every ordering was rejected


def fetch_events(max_events: int = 1500, tag_slugs=(), max_per_tag: int = 300) -> tuple[list, dict]:
    """Return (events, status). Pulls the top events by volume plus tag-specific lists."""
    base = {"active": "true", "closed": "false", "archived": "false", "limit": PAGE}
    seen: dict[str, dict] = {}
    order_state: dict = {}
    errors: list[str] = []

    def pull(extra: dict, cap: int):
        for offset in range(0, cap, PAGE):
            try:
                batch = _page({**base, **extra, "offset": offset}, order_state)
            except net.FetchError as e:
                errors.append(f"{extra or 'top'} @{offset}: {e.reason}")
                return
            for ev in batch:
                if isinstance(ev, dict) and ev.get("id") is not None:
                    seen.setdefault(str(ev["id"]), ev)
            if len(batch) < PAGE:
                return

    pull({}, max_events)
    if not seen and errors:
        return [], {"ok": False, "error": errors[0], "events": 0}
    for slug in tag_slugs:
        pull({"tag_slug": slug}, max_per_tag)
    status = {"ok": True, "events": len(seen), "order": order_state.get("order")}
    if errors:
        status["warnings"] = errors[:5]
    return list(seen.values()), status


def normalize(events: list) -> list[dict]:
    """Flatten events into one record per open Yes/No market."""
    out = []
    for ev in events:
        tags = [t.get("label") or t.get("slug") for t in (ev.get("tags") or []) if isinstance(t, dict)]
        ev_slug = ev.get("slug") or ""
        for m in ev.get("markets") or []:
            if not isinstance(m, dict) or m.get("closed") is True or m.get("active") is False:
                continue
            outcomes = json_list(m.get("outcomes"))
            prices = [fnum(p) for p in json_list(m.get("outcomePrices"))]
            prob = prices[0] if prices else fnum(m.get("lastTradePrice"))
            if prob is None:
                continue
            bid, ask = fnum(m.get("bestBid")), fnum(m.get("bestAsk"))
            if bid is not None and ask is not None and (ask <= 0 or bid > ask):
                bid = ask = None
            volume = fnum(m.get("volumeNum"), None)
            if volume is None:
                volume = fnum(m.get("volume"), 0.0)
            liquidity = fnum(m.get("liquidityNum"), None)
            if liquidity is None:
                liquidity = fnum(m.get("liquidity"))
            start = parse_time(m.get("startDate") or ev.get("startDate") or m.get("createdAt"))
            end = parse_time(m.get("endDate") or ev.get("endDate"))
            out.append({
                "platform": "polymarket",
                "key": f"polymarket:{m.get('id')}",
                "id": str(m.get("id")),
                "event_id": str(ev.get("id")),
                "event_title": ev.get("title") or "",
                "question": m.get("question") or ev.get("title") or "",
                "outcome_label": m.get("groupItemTitle") or None,
                "yes_label": str(outcomes[0]) if outcomes else "Yes",
                "url": f"https://polymarket.com/event/{ev_slug}" if ev_slug else "https://polymarket.com",
                "prob": prob,
                "bid": bid,
                "ask": ask,
                "prob_change_1d": fnum(m.get("oneDayPriceChange")),
                "prob_change_7d": fnum(m.get("oneWeekPriceChange")),
                "volume_total": volume or 0.0,
                "volume_24h": fnum(m.get("volume24hr")),
                "volume_7d": fnum(m.get("volume1wk")),
                "liquidity": liquidity,
                "open_interest": None,
                "volume_unit": "USD",
                "start_date": iso(start),
                "end_date": iso(end),
                "tags": [t for t in tags if t],
                "description": (m.get("description") or ev.get("description") or "")[:600],
                "fee_rate": 0.0,
            })
    return out
