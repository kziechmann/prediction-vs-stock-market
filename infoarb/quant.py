"""Stock statistics and stock-implied event probabilities (lognormal / GBM).

The model is deliberately simple and transparent:
  * volatility = blend of 20- and 60-day realized vol (annualized, 252 days)
  * zero drift in price terms (risk-neutral-ish over short horizons)
  * time measured in trading days, so weekends add no variance
It is a yardstick for "what the stock's own price action implies", not a pricer.
Options-implied vol, when the analyst supplies it in notes.json, replaces it.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

TRADING_DAYS = 252
# Broadie-Glasserman-Kou continuity correction for daily-close monitoring.
BGK_BETA = 0.5826


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def log_returns(closes: list[float]) -> list[float]:
    return [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]


def realized_vol(closes: list[float], window: int) -> float | None:
    r = log_returns(closes[-(window + 1):])
    if len(r) < max(5, window // 2):
        return None
    mean = sum(r) / len(r)
    var = sum((x - mean) ** 2 for x in r) / (len(r) - 1)
    return math.sqrt(var * TRADING_DAYS)


def blended_vol(closes: list[float], short_weight: float = 0.5) -> float | None:
    v20, v60 = realized_vol(closes, 20), realized_vol(closes, 60)
    if v20 and v60:
        return short_weight * v20 + (1 - short_weight) * v60
    return v20 or v60


def beta(stock: list[float], bench: list[float], window: int = 120) -> float | None:
    """OLS beta of daily log returns over the overlapping tail of two aligned series."""
    n = min(len(stock), len(bench), window + 1)
    a, b = log_returns(stock[-n:]), log_returns(bench[-n:])
    k = min(len(a), len(b))
    if k < 30:
        return None
    a, b = a[-k:], b[-k:]
    ma, mb = sum(a) / k, sum(b) / k
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    var = sum((y - mb) ** 2 for y in b)
    return cov / var if var > 0 else None


def close_on_or_before(dates: list[str], closes: list[float], day: date) -> float | None:
    target = day.isoformat()
    best = None
    for d, c in zip(dates, closes):
        if d <= target:
            best = c
        else:
            break
    return best


def window_return(dates: list[str], closes: list[float], days: int, today: date) -> float | None:
    """Return from the close on/before (today - days) to the latest close."""
    base = close_on_or_before(dates, closes, today - timedelta(days=days))
    if not base or not closes:
        return None
    return closes[-1] / base - 1.0


# NYSE full-day closures and 1pm ET early closes. Update yearly (nyse.com/markets/hours-calendars).
NYSE_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
    "2027-09-06", "2027-11-25", "2027-12-24",
}
NYSE_HALF_DAYS = {"2026-11-27", "2026-12-24", "2027-11-26"}


def trading_days_between(now: datetime, end: datetime) -> float:
    """Trading sessions (Mon-Fri, 14:30-21:00 UTC) remaining from now until end.

    NYSE holidays count as zero and early-close days as half a session (2026-2027 calendar;
    later years fall back to weekdays only). Partial sessions count fractionally.
    """
    if end <= now:
        return 0.0
    session = 6.5 * 3600

    def session_fraction(day: date, start: datetime, stop: datetime) -> float:
        if day.weekday() >= 5 or day.isoformat() in NYSE_HOLIDAYS:
            return 0.0
        open_ = datetime(day.year, day.month, day.day, 14, 30, tzinfo=timezone.utc)
        close = datetime(day.year, day.month, day.day, 18 if day.isoformat() in NYSE_HALF_DAYS else 21, 0,
                         tzinfo=timezone.utc)
        lo, hi = max(open_, start), min(close, stop)
        return max(0.0, (hi - lo).total_seconds()) / session

    total, day = 0.0, now.date()
    while day <= end.date():
        total += session_fraction(day, now, end)
        day += timedelta(days=1)
        if total > 2000:
            break
    return total


def prob_terminal(s0: float, k: float, sigma: float, t_years: float, above: bool = True, mu: float = 0.0) -> float:
    """P(S_T > k) under GBM (or P(S_T < k) if above=False)."""
    if t_years <= 0 or sigma <= 0:
        p = 1.0 if s0 > k else 0.0
        return p if above else 1.0 - p
    d2 = (math.log(s0 / k) + (mu - 0.5 * sigma ** 2) * t_years) / (sigma * math.sqrt(t_years))
    p = norm_cdf(d2)
    return p if above else 1.0 - p


def prob_between(s0: float, lo: float, hi: float, sigma: float, t_years: float) -> float:
    return max(0.0, prob_terminal(s0, lo, sigma, t_years) - prob_terminal(s0, hi, sigma, t_years))


def prob_touch(s0: float, barrier: float, sigma: float, t_years: float, mu: float = 0.0,
               daily_close_monitoring: bool = False) -> float:
    """P(price touches `barrier` at any time before T) under GBM.

    Uses the running-maximum (or minimum) distribution of Brownian motion with
    drift nu = mu - sigma^2/2 in log space. With daily_close_monitoring the barrier
    is shifted outward (BGK correction) because only closes count.
    """
    if barrier <= 0 or s0 <= 0:
        return 0.0
    if t_years <= 0 or sigma <= 0:
        return 1.0 if (barrier >= s0 and s0 >= barrier) else 0.0
    up = barrier > s0
    if daily_close_monitoring:
        shift = math.exp(BGK_BETA * sigma * math.sqrt(1.0 / TRADING_DAYS))
        barrier = barrier * shift if up else barrier / shift
    nu = mu - 0.5 * sigma ** 2
    if not up:
        nu = -nu  # reflect: a down-crossing of X is an up-crossing of -X
    b = abs(math.log(barrier / s0))
    st = sigma * math.sqrt(t_years)
    p = norm_cdf((-b + nu * t_years) / st) + math.exp(2 * nu * b / sigma ** 2) * norm_cdf((-b - nu * t_years) / st)
    return min(1.0, max(0.0, p))


def stock_stats(hist: dict, bench: dict | None, today: date, windows=(7, 30),
                short_weight: float = 0.5) -> dict:
    """Volatility, beta and per-window raw / beta-adjusted abnormal returns vs the benchmark."""
    closes, dates = hist["close"], hist["dates"]
    vol = blended_vol(closes, short_weight)
    b = beta(closes, bench["close"]) if bench else None
    b_used = min(2.5, max(0.3, b)) if b is not None else 1.0
    daily = vol / math.sqrt(TRADING_DAYS) if vol else None
    out = {
        "symbol": hist["symbol"], "name": hist.get("name"), "price": hist["price"], "as_of": hist["as_of"],
        "source": hist["source"], "vol_ann": vol, "beta": b, "spark": closes[-30:], "windows": {},
    }
    for w in windows:
        ret = window_return(dates, closes, w, today)
        bret = window_return(bench["dates"], bench["close"], w, today) if bench else None
        abn = (ret - b_used * bret) if (ret is not None and bret is not None) else None
        sd = daily * math.sqrt(max(1.0, w * 5 / 7)) if daily else None
        out["windows"][str(w)] = {"ret": ret, "bench_ret": bret, "abn": abn, "sd": sd,
                                  "z": (abn / sd) if (abn is not None and sd) else None}
    w7 = out["windows"].get("7", {})
    out.update(ret_window=w7.get("ret"), bench_ret_window=w7.get("bench_ret"),
               abn_ret_window=w7.get("abn"), window_sd=w7.get("sd"), abn_z=w7.get("z"),
               abn_30=(out["windows"].get("30") or {}).get("abn"))
    return out
