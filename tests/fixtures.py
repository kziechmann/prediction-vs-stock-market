"""Synthetic inputs for offline tests. Prices and markets are made up; they only exercise the math."""

from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta, timezone


def price_history(symbol: str, end: date, start_price: float, vol: float, days: int = 130,
                  seed: int = 1, last_week_return: float | None = None, forced_days: int = 7) -> dict:
    rng = random.Random(seed)
    dates, closes = [], []
    d = end - timedelta(days=int(days * 7 / 5) + 10)
    px = start_price
    while d <= end:
        if d.weekday() < 5:
            px *= math.exp(rng.gauss(0, vol / math.sqrt(252)) - 0.5 * vol ** 2 / 252)
            dates.append(d.isoformat())
            closes.append(px)
        d += timedelta(days=1)
    if last_week_return is not None:
        # Force the final `forced_days` move onto a straight path from the close before it.
        base_idx = max(i for i, s in enumerate(dates) if s <= (end - timedelta(days=forced_days)).isoformat())
        base = closes[base_idx]
        n = len(closes) - 1 - base_idx
        for k in range(1, n + 1):
            closes[base_idx + k] = base * (1 + last_week_return * k / n)
    return {"symbol": symbol, "name": symbol, "source": "fixture", "currency": "USD",
            "price": closes[-1], "last_close": closes[-1], "as_of": dates[-1], "dates": dates,
            "close": closes, "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes]}


def market(**kw) -> dict:
    base = {
        "platform": "polymarket", "event_id": "e1", "event_title": "", "outcome_label": None, "yes_label": "Yes",
        "url": "https://polymarket.com/event/test", "bid": None, "ask": None, "prob_change_1d": None,
        "prob_change_7d": None, "volume_total": 500_000.0, "volume_24h": None, "volume_7d": 50_000.0,
        "liquidity": 40_000.0, "open_interest": None, "volume_unit": "USD", "start_date": None,
        "end_date": None, "tags": [], "description": "", "fee_rate": 0.0,
    }
    base.update(kw)
    base.setdefault("key", f"{base['platform']}:{base['id']}")
    return base


def build_inputs(now: datetime) -> dict:
    today = now.date()
    end_30 = (now + timedelta(days=30)).replace(hour=21, minute=0, second=0, microsecond=0)
    stocks = {
        "SPY": price_history("SPY", today, 600, 0.15, seed=11, last_week_return=0.0, forced_days=31),
        "SMH": price_history("SMH", today, 300, 0.30, seed=17, last_week_return=0.0, forced_days=31),
        "NVDA": price_history("NVDA", today, 180, 0.45, seed=12),
        "TTWO": price_history("TTWO", today, 240, 0.30, seed=13, last_week_return=0.0),
        "AAPL": price_history("AAPL", today, 230, 0.25, seed=14),
        "PFE": price_history("PFE", today, 25, 0.25, seed=18, last_week_return=0.0),
        "WBD": price_history("WBD", today, 20, 0.40, seed=15, last_week_return=0.0),
        "PSKY": price_history("PSKY", today, 15, 0.45, seed=16, last_week_return=0.0),
    }
    nvda = stocks["NVDA"]["price"]
    markets = [
        # Share-price bet priced far too low by the crowd: an at-the-money level 30 days out.
        market(id="p1", question=f"Will NVIDIA (NVDA) close above ${nvda:.0f} on {end_30:%B %d}?",
               prob=0.20, bid=0.19, ask=0.21, end_date=end_30.isoformat()),
        # Takeover odds jumped 30 pts; both stocks flat -> flag long the target (WBD),
        # and flag the acquirer (PSKY) as needing a direction call.
        market(id="e1", question="Will Paramount acquire Warner Bros. Discovery before 2027?", prob=0.75,
               bid=0.74, ask=0.76, prob_change_7d=0.30, end_date=(now + timedelta(days=200)).isoformat()),
        # Product-launch odds up 30 pts; at the default 2.5% impact this is within noise (not flagged).
        market(id="e2", question="Will GTA 6 release before June 2027?", prob=0.75, bid=0.74, ask=0.76,
               prob_change_7d=0.30, end_date=(now + timedelta(days=200)).isoformat()),
        # Mention market: noise, must never be flagged.
        market(id="n1", question="Will Tim Cook say 'AI' 10+ times during the Apple earnings call?",
               prob=0.6, prob_change_7d=0.25, end_date=(now + timedelta(days=20)).isoformat()),
        # Unrelated market: must not link.
        market(id="u1", question="Will there be a snap election in Japan before 2027?", prob=0.3,
               prob_change_7d=0.2, end_date=(now + timedelta(days=60)).isoformat()),
        # Theme: chip export controls tightening over 30 days (two separate events), SMH flat -> short SMH.
        market(id="t1", event_id="t1", question="Will the US ban Nvidia H20 chip sales to China in 2026?", prob=0.6,
               prob_change_7d=0.03, prob_change_30d=0.25, end_date=(now + timedelta(days=120)).isoformat()),
        market(id="t2", event_id="t2", question="Will the US allow Nvidia to sell H200 chips to China?", prob=0.2,
               prob_change_7d=-0.01, prob_change_30d=-0.15, end_date=(now + timedelta(days=120)).isoformat()),
        # Theme-only market whose ETF has no price data -> watchlist, no signal.
        market(id="t3", event_id="t3", question="Will marijuana be rescheduled to Schedule III in 2026?", prob=0.4,
               prob_change_30d=0.10, end_date=(now + timedelta(days=150)).isoformat()),
        # Short-dated event with a modest gap: down-weighted by the horizon preference.
        market(id="s1", event_id="s1", question="Will the FDA approve Pfizer's new obesity drug by December 5?",
               prob=0.7, prob_change_7d=0.20, end_date=(now + timedelta(days=5)).isoformat()),
        # Kalshi structured strike, fairly priced -> evaluated, not flagged.
        market(platform="kalshi", id="KXAAPL-1", question="Apple stock price on Dec 31",
               series_ticker="KXAAPL", ticker="KXAAPL-1", prob=0.5, bid=0.49, ask=0.51,
               strike_type="greater", floor_strike=round(stocks["AAPL"]["price"], 2), cap_strike=None,
               volume_total=80_000.0, open_interest=20_000.0, liquidity=None, volume_unit="contracts",
               fee_rate=0.07, end_date=end_30.isoformat()),
    ]
    return {"fetched_at": now.isoformat(), "markets": markets, "stocks": stocks,
            "sources": {"polymarket": {"ok": True, "markets": 4}, "kalshi": {"ok": True, "markets": 1},
                        "stocks": {"ok": True, "tickers": 4, "failed": 0}}}
