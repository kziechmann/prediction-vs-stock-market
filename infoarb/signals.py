"""Turn linked markets + stock data into ranked divergence signals.

Two signal families:

A. PRICE  — the market is a bet on the share price itself ("NVDA above $200 on Dec 31",
   "TSLA hits $500 in December"). We price the same contract from the stock's own
   volatility and compare. Edge = model probability minus the price you would pay
   (ask for YES, 1-bid for NO), net of platform fees.

B. EVENT  — the market is about a company event (earnings beat, FDA approval, merger,
   CEO exit...). If the odds of a material event moved a lot over the window but the
   stock's market-adjusted return did not move the way that change implies, the stock
   may not have priced it in.
       expected_move = direction * delta_prob * impact_pct
       gap           = expected_move - abnormal_return
       gap_z         = gap / stock's own window standard deviation   (context: is it beyond noise?)
       score         = |gap| / gap_unit * confidence

Scores are comparable within a family; across families they are a rough common scale
(one unit ~ a 5-point contract edge or a 1% unpriced stock move, at full confidence).
"""

from __future__ import annotations

import math
from datetime import date, datetime

from . import matching, quant
from .util import clamp, parse_time


def confidence(m: dict) -> float:
    """0-1 trust in the market price, from traded volume and resting liquidity."""
    vol = max(m.get("volume_total") or 0.0, 1.0)
    cv = clamp((math.log10(vol) - 3.0) / 3.5, 0.0, 1.0)       # $1k -> 0, ~$3M -> 1
    depth = m.get("liquidity") or m.get("open_interest")
    if not depth:
        return round(cv * 0.85, 3)
    cl = clamp((math.log10(max(depth, 1.0)) - 2.5) / 3.0, 0.0, 1.0)  # $300 -> 0, $300k -> 1
    return round(0.7 * cv + 0.3 * cl, 3)


def kalshi_fee(p: float, rate: float) -> float:
    return rate * p * (1 - p) if rate else 0.0


# --------------------------------------------------------------------------- A: price markets

def price_signal(m: dict, cond: dict, st: dict, hist: dict, notes_t: dict, cfg: dict, now: datetime) -> dict | None:
    pcfg = cfg["price_signal"]
    s0 = st["price"]
    level = cond["level"]
    if not s0 or not level or not (0.2 * s0 <= level <= 5 * s0):
        return None  # threshold is not a plausible share price (e.g. a revenue number)
    end = parse_time(m.get("end_date"))
    if not end:
        return None
    tdays = quant.trading_days_between(now, end)
    if tdays < pcfg["min_trading_days"] or tdays > pcfg["max_trading_days"]:
        return None
    sigma = notes_t.get("implied_vol") or ((st["vol_ann"] or 0) * pcfg["vol_multiplier"])
    if not sigma:
        return None
    t = tdays / quant.TRADING_DAYS
    already = False
    if cond["kind"] == "touch":
        already = _already_touched(m, cond, hist, now)
        model = 1.0 if already else quant.prob_touch(s0, level, sigma, t, daily_close_monitoring=cond["monitoring"] == "close")
    elif cond["kind"] == "range":
        model = quant.prob_between(s0, level, cond["level_hi"], sigma, t)
    else:
        model = quant.prob_terminal(s0, level, sigma, t, above=cond["side"] == "above")

    p = m["prob"]
    lo_p, hi_p = pcfg["skip_market_prob_outside"]
    if not (lo_p <= p <= hi_p) and not already:
        return None
    ask = m.get("ask") if m.get("ask") is not None else p
    bid = m.get("bid") if m.get("bid") is not None else p
    fee_r = m.get("fee_rate") or 0.0
    edge_yes = model - ask - kalshi_fee(ask, fee_r)
    edge_no = (1 - model) - (1 - bid) - kalshi_fee(1 - bid, fee_r)
    side, edge = ("YES", edge_yes) if edge_yes >= edge_no else ("NO", edge_no)
    conf = confidence(m)
    score = clamp(edge / pcfg["edge_unit"], 0, 8) * conf if edge > 0 else 0.0

    pm_view = "more bullish" if _bullish_prob(cond, p) > _bullish_prob(cond, model) else "more bearish"
    level_txt = f"${level:,.2f}" + (f"–${cond['level_hi']:,.2f}" if cond["kind"] == "range" else "")
    entry = ask if side == "YES" else 1 - bid
    action = (f"Buy {side} at {entry:.2f} on {m['platform'].title()} "
              f"(stock-implied {('YES' if side == 'YES' else 'NO')} ≈ {(model if side == 'YES' else 1 - model):.2f})")
    reading = (f"Crowd is {pm_view} than {st['symbol']}'s own volatility implies for {level_txt} "
               f"({cond['kind']}, {tdays:.1f} trading days, σ={sigma:.0%}).")
    return {
        "signal": "price",
        "kind": cond["kind"], "side": cond["side"], "level": level, "level_hi": cond.get("level_hi"),
        "monitoring": cond["monitoring"],
        "model_prob": round(model, 4), "market_prob": p, "bid": m.get("bid"), "ask": m.get("ask"),
        "edge_mid": round(model - p, 4), "edge": round(edge, 4), "trade_side": side, "entry": round(entry, 4),
        "sigma": round(sigma, 4), "sigma_source": "analyst implied vol" if notes_t.get("implied_vol") else "realized",
        "trading_days": round(tdays, 2), "distance_pct": round(level / s0 - 1, 4), "already_touched": already,
        "confidence": conf, "score": round(score, 3),
        "pm_view": pm_view, "action": action, "reading": reading,
        "stock_side": 0,
    }


def _bullish_prob(cond: dict, p: float) -> float:
    """Map a contract probability onto 'how bullish is this' for comparison."""
    if cond["kind"] == "touch":
        return p if cond["side"] == "up" else 1 - p
    if cond["kind"] == "terminal":
        return p if cond["side"] == "above" else 1 - p
    return p


def _already_touched(m: dict, cond: dict, hist: dict, now: datetime) -> bool:
    month = matching.window_start_month(m.get("question", ""), now.date())
    if month and month == now.month:
        start = date(now.year, month, 1).isoformat()
    else:
        sd = parse_time(m.get("start_date"))
        start = sd.date().isoformat() if sd else now.date().isoformat()
    series = hist["close"] if cond["monitoring"] == "close" else (hist["high"] if cond["side"] == "up" else hist["low"])
    for d, v in zip(hist["dates"], series):
        if d < start or v is None:
            continue
        if (cond["side"] == "up" and v >= cond["level"]) or (cond["side"] == "down" and v <= cond["level"]):
            return True
    return False


# --------------------------------------------------------------------------- B: event markets

def event_signal(m: dict, link, category: str, st: dict, note: dict, cfg: dict) -> dict | None:
    ecfg = cfg["event_signal"]
    dp = m.get("prob_change_7d")
    if dp is None or abs(dp) < ecfg["min_prob_change"]:
        return None
    text = f"{m.get('question', '')} {m.get('outcome_label') or ''}"
    role = matching.mna_role(text, link) if category == "mna" else None
    if "direction" in note:
        direction, dsrc = int(note["direction"]), "analyst"
    else:
        direction, dsrc = matching.heuristic_direction(category, text, link), "heuristic"
    impact_key = "mna_target" if (category == "mna" and role == "target") else category
    impact = note.get("impact_pct") or cfg["impact_defaults"].get(impact_key, cfg["impact_defaults"]["other"])
    impact *= link.weight

    abn, sd = st.get("abn_ret_window"), st.get("window_sd")
    if abn is None or not sd:
        return None
    conf = confidence(m)
    if direction:
        expected = direction * dp * impact
        gap = expected - abn
        gap_z = gap / sd
        # Rank by the size of the unpriced move (what a trade could earn); sigma is shown as context.
        score = min(abs(gap) / ecfg["gap_unit"], 8.0) * conf
        stock_side = 1 if gap > 0 else -1
        lag = "lags" if (expected > 0 and abn < expected) or (expected < 0 and abn > expected) else "overshoots"
        reading = (f"Odds {'rose' if dp > 0 else 'fell'} {abs(dp) * 100:.0f} pts in a week; at ~{impact:.0%} impact that "
                   f"implies {expected:+.1%} for {link.ticker}, but it moved {abn:+.1%} vs the market "
                   f"(gap {gap_z:+.1f}σ, stock {lag}).")
        action = f"{'Long' if stock_side > 0 else 'Short'} bias on {link.ticker} until the gap closes or the odds reverse"
    else:
        expected = gap = gap_z = None
        score = ecfg["unknown_direction_weight"] * min(abs(dp) / 0.10, 4.0) * conf
        stock_side = 0
        reading = (f"Odds moved {dp * 100:+.0f} pts in a week while {link.ticker} moved {abn:+.1%} vs the market. "
                   f"Whether YES is good or bad for the stock needs an analyst call.")
        action = "Needs direction call before trading"
    return {
        "signal": "event",
        "category": category, "mna_role": role,
        "direction": direction, "direction_source": dsrc, "impact_pct": round(impact, 4),
        "prob_change": round(dp, 4), "market_prob": m["prob"],
        "expected_move": None if expected is None else round(expected, 4),
        "abn_return": round(abn, 4), "stock_return": st.get("ret_window"), "bench_return": st.get("bench_ret_window"),
        "gap": None if gap is None else round(gap, 4), "gap_z": None if gap_z is None else round(gap_z, 2),
        "confidence": conf, "score": round(score, 3), "stock_side": stock_side,
        "reading": reading, "action": action,
    }


# --------------------------------------------------------------------------- assembly

def build(markets: list[dict], stocks: dict, index: matching.CompanyIndex, notes: dict, cfg: dict,
          now: datetime) -> dict:
    """Link, classify and score. Returns opportunities, per-family lists and a watchlist."""
    fcfg = cfg["filters"]
    bench_sym = cfg["sources"]["stocks"]["benchmark"]
    bench = stocks.get(bench_sym)
    window = cfg["event_signal"]["window_days"]
    stats = {sym: quant.stock_stats(h, bench, now.date(), window, cfg["price_signal"]["vol_blend_short_weight"])
             for sym, h in stocks.items()}

    mnotes, tnotes = notes.get("markets", {}), notes.get("tickers", {})
    price_rows, event_rows, watch = [], [], []
    counts = {"markets": len(markets), "linked": 0, "noise": 0, "price_markets": 0, "event_markets": 0,
              "no_stock_data": 0, "ignored": 0}

    for m in markets:
        note = mnotes.get(m["key"], {})
        if note.get("ignore"):
            counts["ignored"] += 1
            continue
        if (m.get("volume_total") or 0) < fcfg["min_volume_any"]:
            continue
        links = [matching.Link(note["ticker"], index.names.get(note["ticker"], note["ticker"]), 1.0, "analyst", 0)] \
            if note.get("ticker") else index.link_market(m)
        if not links:
            continue
        counts["linked"] += 1
        text = f"{m.get('question', '')} {m.get('outcome_label') or ''}"
        noise = matching.noise_reason(text)
        category = note.get("category") or matching.classify(text)
        cond = matching.parse_price_condition(m) if category not in ("up_down", "market_cap") else None
        if cond:
            category = "price"
        if noise and not note.get("category"):
            counts["noise"] += 1
        for link in links:
            st, hist = stats.get(link.ticker), stocks.get(link.ticker)
            base = _base_row(m, link, category, st, note, noise)
            if st is None:
                counts["no_stock_data"] += 1
                watch.append(base)
                continue
            sig = None
            conf = confidence(m)
            if noise and not note.get("category"):
                pass
            elif cond and m["volume_total"] >= fcfg["min_volume_price"] and conf >= fcfg["min_confidence"]:
                counts["price_markets"] += 1
                sig = price_signal(m, cond, st, hist, tnotes.get(link.ticker, {}), cfg, now)
                if sig and sig["edge"] < cfg["price_signal"]["min_edge"]:
                    price_rows.append({**base, **sig, "flagged": False})
                    sig = None
                elif sig:
                    price_rows.append({**base, **sig, "flagged": True})
            elif category not in ("price", "up_down", "market_cap") and m["volume_total"] >= fcfg["min_volume_event"] and conf >= fcfg["min_confidence"]:
                counts["event_markets"] += 1
                sig = event_signal(m, link, category, st, note, cfg)
                if sig:
                    event_rows.append({**base, **sig, "flagged": sig["score"] >= cfg["ranking"]["min_score"]})
            if not sig:
                watch.append(base)

    rcfg = cfg["ranking"]
    flagged = [r for r in price_rows + event_rows if r.get("flagged") and r["score"] >= rcfg["min_score"]]
    flagged.sort(key=lambda r: r["score"], reverse=True)
    opportunities = _dedupe(flagged)[: rcfg["max_opportunities"]]
    for i, r in enumerate(opportunities, 1):
        r["rank"] = i
    price_rows.sort(key=lambda r: r.get("edge", 0), reverse=True)
    event_rows.sort(key=lambda r: r["score"], reverse=True)
    watch.sort(key=lambda r: r.get("volume_total") or 0, reverse=True)
    return {
        "opportunities": opportunities,
        "price_signals": price_rows,
        "event_signals": event_rows,
        "watchlist": watch[: rcfg["max_watchlist"]],
        "stocks": stats,
        "counts": counts,
    }


def _base_row(m: dict, link, category: str, st: dict | None, note: dict, noise: str | None) -> dict:
    return {
        "key": m["key"], "platform": m["platform"], "url": m["url"],
        "question": m["question"], "event_title": m.get("event_title"), "outcome_label": m.get("outcome_label"),
        "ticker": link.ticker, "company": link.name, "link_via": link.via, "link_weight": link.weight,
        "proxy_note": link.proxy_note,
        "category": category, "noise": noise,
        "prob": m["prob"], "prob_change_1d": m.get("prob_change_1d"), "prob_change_7d": m.get("prob_change_7d"),
        "volume_total": m.get("volume_total"), "volume_7d": m.get("volume_7d"), "volume_unit": m.get("volume_unit"),
        "liquidity": m.get("liquidity"), "open_interest": m.get("open_interest"),
        "end_date": m.get("end_date"),
        "stock_price": st["price"] if st else None, "stock_ret_7d": st.get("ret_window") if st else None,
        "stock_abn_7d": st.get("abn_ret_window") if st else None,
        "analyst": {k: note[k] for k in ("verdict", "thesis", "updated") if k in note} or None,
    }


def _dedupe(rows: list[dict]) -> list[dict]:
    """Keep the best row per (event, ticker) so one ladder of strikes doesn't flood the list."""
    seen, out = set(), []
    for r in rows:
        k = (r["platform"], r.get("event_title"), r["ticker"], r["signal"])
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out
