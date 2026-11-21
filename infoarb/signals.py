"""Turn linked markets + stock data into ranked divergence signals.

Three signal families:

THEME  — markets about an industry-wide or macro development (chip export controls, drug
   pricing, crypto legislation, tariffs...). Every market in a theme is netted into one
   expected move for a sector ETF, then compared with how that ETF actually moved.

EVENT  — markets about one company's event (earnings, approval, merger, CEO exit...).

PRICE  — markets on a share price itself ("NVDA above $200 on Dec 31"). The same contract
   is priced from the stock's own volatility; edge = model probability minus what you
   would pay (ask for YES, 1-bid for NO), net of fees.

For THEME and EVENT, over a 7-day and a 30-day window (whichever scores higher):
    expected move = direction * delta_prob * impact          (impact: move if YES fully resolves)
    unpriced gap  = the part of the expected move the stock has not made yet
                    (0 if it already moved that far; capped at the expected move if it
                    moved the other way, since that move was probably about something else)
    priced in     = 1 - |gap| / |expected move|
    base score    = |gap| / gap_unit * confidence

Ranking preferences (config "preferences"):
    final score = base score * horizon weight * theme boost
  where the horizon weight favors markets that resolve further out, unless the delta is
  very large (price edge >= large_delta.price_edge, or unpriced gap >= large_delta.unpriced_move).
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

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


# --------------------------------------------------------------------------- preferences

def horizon_days(end_iso: str | None, now: datetime) -> float | None:
    end = parse_time(end_iso)
    return None if end is None else max(0.0, (end - now).total_seconds() / 86400)


def horizon_weight(days: float | None, prefs: dict) -> float:
    pts = sorted(prefs["horizon_weights"])
    if days is None:
        return 1.0
    if days <= pts[0][0]:
        return pts[0][1]
    for (d0, w0), (d1, w1) in zip(pts, pts[1:]):
        if days <= d1:
            return w0 + (w1 - w0) * (days - d0) / (d1 - d0)
    return pts[-1][1]


def apply_preferences(row: dict, cfg: dict, now: datetime, *, large: bool, boost: float = 1.0,
                      days: float | None = None) -> dict:
    prefs = cfg["preferences"]
    if days is None:
        days = horizon_days(row.get("end_date"), now)
    hw = horizon_weight(days, prefs)
    exempt = large and hw < 1.0
    if exempt:
        hw = 1.0
    base = row["score"]
    row.update(score_base=round(base, 3), horizon_days=None if days is None else round(days, 1),
               horizon_weight=round(hw, 3), boost=boost, large_delta=bool(large), exempt=exempt,
               score=round(base * hw * boost, 3))
    return row


def unpriced(expected: float, abn: float) -> tuple[float, float]:
    """(gap, priced_fraction). gap has the sign of `expected` and |gap| <= |expected|."""
    if not expected:
        return 0.0, 1.0
    s = 1.0 if expected > 0 else -1.0
    moved = s * abn
    remaining = clamp(abs(expected) - moved, 0.0, abs(expected))
    return s * remaining, clamp(moved / abs(expected), 0.0, 1.0)


# --------------------------------------------------------------------------- PRICE

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
              f"(stock-implied {side} ≈ {(model if side == 'YES' else 1 - model):.2f})")
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
    if cond["kind"] == "touch":
        return p if cond["side"] == "up" else 1 - p
    if cond["kind"] == "terminal":
        return p if cond["side"] == "above" else 1 - p
    return p


def _already_touched(m: dict, cond: dict, hist: dict, now: datetime) -> bool:
    # Touches only count inside the market's window: from the later of the named month's start
    # and the market's creation (Polymarket: "at any point after market creation and during ...").
    month = matching.window_start_month(m.get("question", ""), now.date())
    sd = parse_time(m.get("start_date"))
    starts = [sd.date().isoformat()] if sd else []
    if month and month == now.month:
        starts.append(date(now.year, month, 1).isoformat())
    start = max(starts) if starts else now.date().isoformat()
    if sd and sd.date().isoformat() == start:
        start = (sd.date() + timedelta(days=1)).isoformat()  # creation-day bar may predate the market
    series = hist["close"] if cond["monitoring"] == "close" else (hist["high"] if cond["side"] == "up" else hist["low"])
    for d, v in zip(hist["dates"], series):
        if d < start or v is None:
            continue
        if (cond["side"] == "up" and v >= cond["level"]) or (cond["side"] == "down" and v <= cond["level"]):
            return True
    return False


# --------------------------------------------------------------------------- EVENT

def _windows(cfg: dict) -> list[int]:
    return [int(w) for w in cfg["event_signal"]["windows"]]


def event_signal(m: dict, link, category: str, st: dict, note: dict, cfg: dict) -> dict | None:
    ecfg = cfg["event_signal"]
    text = f"{m.get('question', '')} {m.get('outcome_label') or ''}"
    role = matching.mna_role(text, link) if category == "mna" else None
    if "direction" in note:
        direction, dsrc = int(note["direction"]), "analyst"
    else:
        direction, dsrc = matching.heuristic_direction(category, text, link), "heuristic"
    impact_key = "mna_target" if (category == "mna" and role == "target") else category
    impact = note.get("impact_pct") or cfg["impact_defaults"].get(impact_key, cfg["impact_defaults"]["other"])
    impact *= link.weight
    conf = confidence(m)

    best = None
    for w in _windows(cfg):
        dp = m.get(f"prob_change_{w}d")
        if dp is None or abs(dp) < ecfg["min_prob_change"][str(w)]:
            continue
        win = (st.get("windows") or {}).get(str(w)) or {}
        abn, sd = win.get("abn"), win.get("sd")
        if abn is None or not sd:
            continue
        if direction:
            expected = clamp(direction * dp * impact, -ecfg["max_expected_move"], ecfg["max_expected_move"])
            gap, priced = unpriced(expected, abn)
            score = min(abs(gap) / ecfg["gap_unit"], 8.0) * conf
        else:
            expected = gap = priced = None
            score = ecfg["unknown_direction_weight"] * min(abs(dp) / 0.10, 4.0) * conf
        cand = {"window": w, "dp": dp, "abn": abn, "sd": sd, "expected": expected, "gap": gap,
                "priced": priced, "score": score}
        if best is None or cand["score"] > best["score"]:
            best = cand
    if best is None:
        return None

    w, dp, abn = best["window"], best["dp"], best["abn"]
    span = "a week" if w == 7 else f"{w} days"
    if direction:
        gap, expected = best["gap"], best["expected"]
        stock_side = 0 if not gap else (1 if gap > 0 else -1)
        reading = (f"Odds {'rose' if dp > 0 else 'fell'} {abs(dp) * 100:.0f} pts over {span}; at ~{impact:.0%} impact "
                   f"that implies {expected:+.1%} for {link.ticker}. It moved {abn:+.1%} vs the market, "
                   f"so about {best['priced']:.0%} looks priced in.")
        action = ("Looks priced in" if not stock_side else
                  f"{'Long' if stock_side > 0 else 'Short'} bias on {link.ticker} for the unpriced {abs(gap):.1%}")
    else:
        stock_side = 0
        reading = (f"Odds moved {dp * 100:+.0f} pts over {span} while {link.ticker} moved {abn:+.1%} vs the market. "
                   f"Whether YES is good or bad for the stock needs an analyst call.")
        action = "Needs direction call before trading"
    gz = (best["gap"] / best["sd"]) if (best["gap"] is not None and best["sd"]) else None
    return {
        "signal": "event", "category": category, "mna_role": role,
        "direction": direction, "direction_source": dsrc, "impact_pct": round(impact, 4),
        "window": w, "prob_change": round(dp, 4), "market_prob": m["prob"],
        "expected_move": None if best["expected"] is None else round(best["expected"], 4),
        "abn_return": round(abn, 4),
        "gap": None if best["gap"] is None else round(best["gap"], 4),
        "priced_fraction": None if best["priced"] is None else round(best["priced"], 3),
        "gap_z": None if gz is None else round(gz, 2),
        "confidence": conf, "score": round(best["score"], 3), "stock_side": stock_side,
        "reading": reading, "action": action,
    }


# --------------------------------------------------------------------------- THEME

def theme_signal(theme, items: list, stats: dict, notes: dict, cfg: dict, now: datetime) -> dict | None:
    """Net every market in a theme into one expected move for the theme's ETF."""
    ecfg = cfg["event_signal"]
    tnote = (notes.get("themes") or {}).get(theme.id, {})
    st = stats.get(theme.vehicle)
    if st is None or tnote.get("ignore"):
        return None
    # One representative market per event (the one with the biggest confidence-weighted move).
    reps: dict[tuple, tuple] = {}
    for m, match in items:
        k = (m["platform"], m.get("event_id"))
        mv = max(abs(m.get(f"prob_change_{w}d") or 0) for w in _windows(cfg)) * confidence(m)
        if k not in reps or mv > reps[k][2]:
            reps[k] = (m, match, mv)

    best = None
    for w in _windows(cfg):
        win = (st.get("windows") or {}).get(str(w)) or {}
        if win.get("abn") is None or not win.get("sd"):
            continue
        contribs, undecided = [], 0
        for m, match, _ in reps.values():
            dp = m.get(f"prob_change_{w}d")
            if dp is None or abs(dp) < ecfg["min_prob_change"][str(w)]:
                continue
            mnote = (notes.get("markets") or {}).get(m["key"], {})
            d = int(mnote.get("direction", match.direction))
            if not d:
                undecided += 1
                continue
            imp = mnote.get("impact_pct") or tnote.get("impact_pct") or theme.impact_pct
            contribs.append({"key": m["key"], "question": m["question"], "url": m["url"], "platform": m["platform"],
                             "prob": m["prob"], "dp": round(dp, 4), "direction": d, "impact": imp,
                             "confidence": confidence(m), "contrib": d * dp * imp, "end_date": m.get("end_date"),
                             "volume_total": m.get("volume_total"), "volume_unit": m.get("volume_unit")})
        if not contribs:
            continue
        expected = clamp(sum(c["contrib"] for c in contribs), -ecfg["max_expected_move"], ecfg["max_expected_move"])
        weight = sum(abs(c["contrib"]) for c in contribs) or 1.0
        conf = sum(c["confidence"] * abs(c["contrib"]) for c in contribs) / weight
        gap, priced = unpriced(expected, win["abn"])
        score = min(abs(gap) / ecfg["gap_unit"], 8.0) * conf
        days = [horizon_days(c["end_date"], now) for c in contribs]
        hz = sum((d or 0) * abs(c["contrib"]) for d, c in zip(days, contribs)) / weight
        cand = {"window": w, "contribs": contribs, "undecided": undecided, "expected": expected, "gap": gap,
                "priced": priced, "conf": conf, "score": score, "days": hz, "abn": win["abn"], "sd": win["sd"]}
        if best is None or cand["score"] > best["score"]:
            best = cand
    if best is None:
        return None

    contribs = sorted(best["contribs"], key=lambda c: abs(c["contrib"]), reverse=True)
    top = contribs[0]
    gap, expected, w = best["gap"], best["expected"], best["window"]
    stock_side = 0 if not gap else (1 if gap > 0 else -1)
    span = "a week" if w == 7 else f"{w} days"
    n = len(contribs)
    reading = (f"{n} market{'s' if n != 1 else ''} on {theme.name.lower()} moved over {span}; netted together they imply "
               f"{expected:+.1%} for {theme.vehicle} ({theme.sector}). {theme.vehicle} moved {best['abn']:+.1%} vs the market, "
               f"so about {best['priced']:.0%} looks priced in.")
    if best["undecided"]:
        reading += f" {best['undecided']} more need a direction call."
    action = ("Looks priced in" if not stock_side else
              f"{'Long' if stock_side > 0 else 'Short'} {theme.vehicle} or the {theme.sector.lower()} basket "
              f"for the unpriced {abs(gap):.1%}")
    row = {
        "signal": "theme", "key": f"theme:{theme.id}", "theme_id": theme.id, "category": "theme",
        "ticker": theme.vehicle, "company": theme.name, "sector": theme.sector, "basket": theme.members,
        "platform": top["platform"], "url": top["url"], "question": theme.name, "event_title": theme.name,
        "outcome_label": None, "link_via": "theme", "link_weight": 1.0, "proxy_note": None, "noise": None,
        "prob": top["prob"], "market_prob": top["prob"], "prob_change": top["dp"], "window": w,
        "prob_change_7d": None, "prob_change_30d": None, "markets": contribs[:8], "n_markets": len(items),
        "n_events": len(reps), "expected_move": round(expected, 4), "abn_return": round(best["abn"], 4),
        "gap": round(gap, 4), "priced_fraction": round(best["priced"], 3),
        "gap_z": round(gap / best["sd"], 2) if best["sd"] else None,
        "confidence": round(best["conf"], 3), "score": round(best["score"], 3), "stock_side": stock_side,
        "reading": reading, "action": action, "end_date": top["end_date"],
        "volume_total": sum(c["volume_total"] or 0 for c in contribs), "volume_unit": top["volume_unit"],
        "stock_price": st["price"], "stock_ret_7d": st.get("ret_window"), "stock_abn_7d": st.get("abn_ret_window"),
        "stock_abn_30d": st.get("abn_30"),
        "analyst": {k: tnote[k] for k in ("verdict", "thesis", "updated") if k in tnote} or None,
    }
    large = abs(gap) >= cfg["preferences"]["large_delta"]["unpriced_move"]
    return apply_preferences(row, cfg, now, large=large, boost=cfg["preferences"]["theme_boost"], days=best["days"])


# --------------------------------------------------------------------------- assembly

def build(markets: list[dict], stocks: dict, index: matching.CompanyIndex, notes: dict, cfg: dict,
          now: datetime, themes=None) -> dict:
    """Link, classify and score. Returns opportunities, per-family lists and a watchlist."""
    fcfg, prefs = cfg["filters"], cfg["preferences"]
    bench = stocks.get(cfg["sources"]["stocks"]["benchmark"])
    stats = {sym: quant.stock_stats(h, bench, now.date(), _windows(cfg), cfg["price_signal"]["vol_blend_short_weight"])
             for sym, h in stocks.items()}

    mnotes, tnotes = notes.get("markets", {}), notes.get("tickers", {})
    price_rows, event_rows, watch = [], [], []
    theme_items: dict[str, list] = {}
    theme_only: list[tuple[dict, list]] = []
    movers: list[dict] = []
    counts = {"markets": len(markets), "linked": 0, "noise": 0, "price_markets": 0, "event_markets": 0,
              "theme_markets": 0, "no_stock_data": 0, "ignored": 0}

    for m in markets:
        note = mnotes.get(m["key"], {})
        if note.get("ignore"):
            counts["ignored"] += 1
            continue
        if (m.get("volume_total") or 0) < fcfg["min_volume_any"]:
            continue
        text = f"{m.get('question', '')} {m.get('outcome_label') or ''}"
        noise = matching.noise_reason(text)
        conf = confidence(m)

        tmatches = themes.match(m) if themes else []
        if tmatches and not noise and m["volume_total"] >= fcfg["min_volume_event"] and conf >= fcfg["min_confidence"]:
            counts["theme_markets"] += 1
            for tm in tmatches:
                theme_items.setdefault(tm.theme.id, []).append((m, tm))

        links = [matching.Link(note["ticker"], index.names.get(note["ticker"], note["ticker"]), 1.0, "analyst", 0)] \
            if note.get("ticker") else index.link_market(m)
        if not links:
            if tmatches:
                theme_only.append((m, tmatches))
            elif not noise and m["volume_total"] >= fcfg["min_volume_unlinked_mover"]:
                move = max(abs(m.get("prob_change_7d") or 0), abs(m.get("prob_change_30d") or 0))
                if move >= fcfg["min_move_unlinked_mover"]:
                    movers.append({k: m.get(k) for k in ("key", "platform", "url", "question", "event_title", "prob",
                                                         "prob_change_7d", "prob_change_30d", "volume_total",
                                                         "volume_unit", "end_date", "tags")} | {"move": move})
            continue
        counts["linked"] += 1
        category = note.get("category") or matching.classify(text)
        cond = matching.parse_price_condition(m) if category not in ("up_down", "market_cap") else None
        if cond:
            category = "price"
        if noise and not note.get("category"):
            counts["noise"] += 1
        for link in links:
            st, hist = stats.get(link.ticker), stocks.get(link.ticker)
            if st is None and link.dynamic:
                continue  # a "(XYZ)" in the text that didn't check out as a listed stock
            base = _base_row(m, link, category, st, note, noise)
            if st is None:
                counts["no_stock_data"] += 1
                watch.append(base)
                continue
            sig = None
            if noise and not note.get("category"):
                pass
            elif cond and m["volume_total"] >= fcfg["min_volume_price"] and conf >= fcfg["min_confidence"]:
                counts["price_markets"] += 1
                sig = price_signal(m, cond, st, hist, tnotes.get(link.ticker, {}), cfg, now)
                if sig:
                    row = apply_preferences({**base, **sig}, cfg, now,
                                            large=sig["edge"] >= prefs["large_delta"]["price_edge"])
                    row["flagged"] = sig["edge"] >= cfg["price_signal"]["min_edge"] and row["score"] >= cfg["ranking"]["min_score"]
                    price_rows.append(row)
            elif category not in ("price", "up_down", "market_cap") and m["volume_total"] >= fcfg["min_volume_event"] and conf >= fcfg["min_confidence"]:
                counts["event_markets"] += 1
                sig = event_signal(m, link, category, st, note, cfg)
                if sig:
                    large = sig["gap"] is not None and abs(sig["gap"]) >= prefs["large_delta"]["unpriced_move"]
                    row = apply_preferences({**base, **sig}, cfg, now, large=large)
                    row["flagged"] = row["score"] >= cfg["ranking"]["min_score"]
                    event_rows.append(row)
            if not sig:
                watch.append(base)

    theme_rows = []
    if themes:
        for tid, items in theme_items.items():
            row = theme_signal(themes.by_id[tid], items, stats, notes, cfg, now)
            if row:
                row["flagged"] = row["score"] >= cfg["ranking"]["min_score"]
                theme_rows.append(row)
        used = {c["key"] for r in theme_rows for c in r["markets"]}
        for m, tms in theme_only:
            if m["key"] in used:
                continue
            t = tms[0].theme
            st = stats.get(t.vehicle)
            link = matching.Link(t.vehicle, t.name, 1.0, "theme", 0)
            watch.append(_base_row(m, link, f"theme: {t.name}", st, mnotes.get(m["key"], {}),
                                   matching.noise_reason(m.get("question", ""))))

    rcfg = cfg["ranking"]
    flagged = [r for r in theme_rows + price_rows + event_rows if r.get("flagged")]
    flagged.sort(key=lambda r: r["score"], reverse=True)
    opportunities = _dedupe(flagged)[: rcfg["max_opportunities"]]
    for i, r in enumerate(opportunities, 1):
        r["rank"] = i
    theme_rows.sort(key=lambda r: r["score"], reverse=True)
    price_rows.sort(key=lambda r: r["score"], reverse=True)
    event_rows.sort(key=lambda r: r["score"], reverse=True)
    watch.sort(key=lambda r: r.get("volume_total") or 0, reverse=True)
    counts["themes"] = len(theme_rows)
    movers.sort(key=lambda r: r["move"] * math.log10(max(r["volume_total"] or 1, 10)), reverse=True)
    return {
        "opportunities": opportunities,
        "theme_signals": theme_rows,
        "price_signals": price_rows,
        "event_signals": event_rows,
        "watchlist": watch[: rcfg["max_watchlist"]],
        "unlinked_movers": movers[:25],
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
        "prob_change_30d": m.get("prob_change_30d"),
        "volume_total": m.get("volume_total"), "volume_7d": m.get("volume_7d"), "volume_unit": m.get("volume_unit"),
        "liquidity": m.get("liquidity"), "open_interest": m.get("open_interest"),
        "end_date": m.get("end_date"),
        "stock_price": st["price"] if st else None, "stock_ret_7d": st.get("ret_window") if st else None,
        "stock_abn_7d": st.get("abn_ret_window") if st else None,
        "stock_abn_30d": st.get("abn_30") if st else None,
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
