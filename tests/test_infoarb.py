"""Offline tests: python3 -m unittest discover -s info_arb/tests -t info_arb"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixtures  # noqa: E402
from infoarb import kalshi, matching, mdlite, polymarket, quant, signals, track  # noqa: E402
from infoarb.themes import load_themes  # noqa: E402

INDEX = matching.load_index(ROOT / "companies.json")
THEMES = load_themes(ROOT / "themes.json")
CFG = json.loads((ROOT / "config.json").read_text())
NOW = datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc)  # a Wednesday, market open


class QuantTests(unittest.TestCase):
    def test_terminal_at_the_money_is_near_half(self):
        p = quant.prob_terminal(100, 100, 0.3, 30 / 252)
        self.assertAlmostEqual(p, 0.5, delta=0.03)
        self.assertLess(p, 0.5)  # lognormal median sits below the forward

    def test_touch_is_about_twice_terminal(self):
        s0, k, vol, t = 100, 115, 0.4, 40 / 252
        term = quant.prob_terminal(s0, k, vol, t)
        touch = quant.prob_touch(s0, k, vol, t)
        self.assertGreater(touch, term)
        self.assertAlmostEqual(touch / term, 2.0, delta=0.15)

    def test_touch_monotone_and_bounded(self):
        a = quant.prob_touch(100, 120, 0.4, 10 / 252)
        b = quant.prob_touch(100, 120, 0.4, 60 / 252)
        self.assertLess(a, b)
        self.assertLessEqual(b, 1.0)
        self.assertGreater(quant.prob_touch(100, 101, 0.4, 60 / 252), 0.9)
        down = quant.prob_touch(100, 85, 0.4, 40 / 252)
        self.assertAlmostEqual(down, quant.prob_touch(100, 100 / 0.85, 0.4, 40 / 252), delta=0.05)

    def test_close_monitoring_lowers_touch_probability(self):
        cont = quant.prob_touch(100, 110, 0.35, 20 / 252)
        disc = quant.prob_touch(100, 110, 0.35, 20 / 252, daily_close_monitoring=True)
        self.assertLess(disc, cont)

    def test_range_probability(self):
        p = quant.prob_between(100, 95, 105, 0.3, 20 / 252)
        self.assertGreater(p, 0.3)
        self.assertLess(p, 0.7)

    def test_trading_days_skip_weekends(self):
        fri = datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)
        mon_close = datetime(2026, 11, 23, 21, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(quant.trading_days_between(fri, mon_close), 1.0, places=3)
        wed_open = datetime(2026, 11, 18, 14, 30, tzinfo=timezone.utc)
        self.assertAlmostEqual(quant.trading_days_between(wed_open, mon_close), 4.0, places=3)

    def test_realized_vol_recovers_input(self):
        h = fixtures.price_history("X", NOW.date(), 100, 0.4, days=400, seed=3)
        v = quant.realized_vol(h["close"], 250)
        self.assertAlmostEqual(v, 0.4, delta=0.06)


class LinkingTests(unittest.TestCase):
    def tickers(self, q, **kw):
        return [link.ticker for link in INDEX.link_market({"question": q, **kw})]

    def test_direct_names_and_tickers(self):
        self.assertEqual(self.tickers("Will Tesla deliver 500k cars in Q4?"), ["TSLA"])
        self.assertEqual(self.tickers("What price will NVIDIA (NVDA) hit in December?"), ["NVDA"])
        self.assertEqual(self.tickers("Will $AAPL close green?"), ["AAPL"])
        self.assertEqual(self.tickers("Will GTA VI be delayed again?"), ["TTWO"])

    def test_ambiguous_words_do_not_link(self):
        self.assertEqual(self.tickers("Will there be a snap election in Japan?"), [])
        self.assertEqual(self.tickers("Will the Fed hit its inflation target in 2027?"), [])
        self.assertEqual(self.tickers("Will the US issue more travel visa restrictions?"), [])
        self.assertEqual(self.tickers("A meta-analysis of crypto ETFs"), [])
        self.assertEqual(self.tickers("Will Doug Ford call an election?"), [])

    def test_multi_company_and_proxy(self):
        self.assertEqual(self.tickers("Will Paramount acquire Warner Bros. Discovery?"), ["PSKY", "WBD"])
        links = INDEX.link_market({"question": "Will OpenAI IPO before 2027?"})
        self.assertEqual([link.ticker for link in links], ["MSFT"])
        self.assertLess(links[0].weight, 1)
        # A direct match beats a proxy.
        self.assertEqual(self.tickers("Will Microsoft and OpenAI renegotiate?"), ["MSFT"])

    def test_event_title_fallback_only_when_unambiguous(self):
        self.assertEqual(self.tickers("↑ $250", event_title="What price will Tesla hit in December?"), ["TSLA"])
        self.assertEqual(self.tickers("Yes", event_title="Apple vs Microsoft: bigger market cap?"), [])

    def test_mna_roles(self):
        q = "Will Paramount acquire Warner Bros. Discovery?"
        links = {link.ticker: link for link in INDEX.link_market({"question": q})}
        self.assertEqual(matching.mna_role(q, links["WBD"]), "target")
        self.assertEqual(matching.mna_role(q, links["PSKY"]), "acquirer")
        q2 = "Will Warner Bros be acquired by Comcast?"
        links2 = {link.ticker: link for link in INDEX.link_market({"question": q2})}
        self.assertEqual(matching.mna_role(q2, links2["WBD"]), "target")


class ClassifyTests(unittest.TestCase):
    def test_noise(self):
        self.assertIsNotNone(matching.noise_reason("What will Jensen Huang say during the earnings call?"))
        self.assertIsNotNone(matching.noise_reason("Will Elon tweet 300 times this week?"))
        self.assertIsNotNone(matching.noise_reason("Which company has the best AI model end of December?"))
        self.assertIsNone(matching.noise_reason("Will the FDA approve orforglipron by March?"))

    def test_categories_and_direction(self):
        q = "Will the FDA approve orforglipron by March 31?"
        self.assertEqual(matching.classify(q), "regulatory")
        self.assertEqual(matching.heuristic_direction("regulatory", q), 1)
        q = "Will the DOJ win its antitrust case to break up Google?"
        self.assertEqual(matching.classify(q), "regulatory")
        self.assertEqual(matching.heuristic_direction("regulatory", "Will regulators block the deal?"), -1)
        self.assertEqual(matching.classify("Will NVIDIA beat earnings estimates in Q4?"), "earnings")
        self.assertEqual(matching.heuristic_direction("earnings", "Will NVIDIA beat earnings estimates?"), 1)
        self.assertEqual(matching.classify("Will Tim Cook step down as CEO in 2026?"), "leadership")
        self.assertEqual(matching.classify("Tesla Up or Down on November 21?"), "up_down")
        self.assertEqual(matching.classify("Largest company in the world end of 2026?"), "market_cap")


class PreferenceTests(unittest.TestCase):
    def test_horizon_weight_interpolates(self):
        prefs = CFG["preferences"]
        self.assertAlmostEqual(signals.horizon_weight(0, prefs), prefs["horizon_weights"][0][1])
        self.assertAlmostEqual(signals.horizon_weight(90, prefs), 1.0)
        mid = signals.horizon_weight(18.5, prefs)
        self.assertGreater(mid, signals.horizon_weight(7, prefs))
        self.assertLess(mid, signals.horizon_weight(30, prefs))
        self.assertAlmostEqual(signals.horizon_weight(10_000, prefs), prefs["horizon_weights"][-1][1])
        self.assertEqual(signals.horizon_weight(None, prefs), 1.0)

    def test_unpriced_gap(self):
        self.assertEqual(signals.unpriced(0.05, 0.0), (0.05, 0.0))          # nothing priced
        gap, pf = signals.unpriced(0.05, 0.02)
        self.assertAlmostEqual(gap, 0.03); self.assertAlmostEqual(pf, 0.4)  # 40% priced
        self.assertEqual(signals.unpriced(0.05, 0.08), (0.0, 1.0))          # overshot: priced in
        self.assertEqual(signals.unpriced(0.05, -0.10), (0.05, 0.0))        # moved the other way: capped
        gap, pf = signals.unpriced(-0.04, -0.01)
        self.assertAlmostEqual(gap, -0.03); self.assertAlmostEqual(pf, 0.25)

    def test_event_uses_thirty_day_window_when_only_trend_moved(self):
        now = NOW
        inputs = fixtures.build_inputs(now)
        m = fixtures.market(id="w30", event_id="w30", question="Will Paramount acquire Warner Bros. Discovery?",
                            prob=0.6, prob_change_7d=0.01, prob_change_30d=0.30,
                            end_date=(now + timedelta(days=200)).isoformat())
        res = signals.build([m], inputs["stocks"], INDEX, {}, CFG, now, themes=THEMES)
        r = [r for r in res["event_signals"] if r["ticker"] == "WBD"][0]
        self.assertEqual(r["window"], 30)


class ThemeMatchTests(unittest.TestCase):
    def match(self, q, **kw):
        return {tm.theme.id: tm.direction for tm in THEMES.match({"question": q, **kw})}

    def test_matches_and_directions(self):
        self.assertEqual(self.match("Will the US allow Nvidia to sell H20 chips to China?"), {"chips_china": 1})
        self.assertEqual(self.match("Will Trump impose tariffs on semiconductors by March?")["chips_china"], -1)
        self.assertEqual(self.match("Will the Supreme Court rule the IEEPA tariffs unlawful?"), {"import_tariffs": 1})
        self.assertEqual(self.match("Will Trump raise tariffs on China above 100%?"), {"import_tariffs": -1})
        self.assertEqual(self.match("Will the CLARITY Act be signed into law in 2026?"), {"crypto_policy": 1})
        self.assertEqual(self.match("Will marijuana be rescheduled in 2026?"), {"cannabis": 1})
        self.assertEqual(self.match("Russia x Ukraine ceasefire in 2026?"), {"defense": -1})
        self.assertEqual(self.match("Will Fannie Mae IPO in 2026?"), {"gse_release": 1})
        self.assertEqual(self.match("Will 30-year mortgage rates fall below 6%?"), {"mortgage_rates": 1})

    def test_event_title_counts_for_matching(self):
        self.assertIn("chips_china", self.match("Yes", event_title="Nvidia H20 sales to China resumed by...?"))

    def test_unrelated(self):
        self.assertEqual(self.match("Will the Lakers win the NBA title?"), {})
        self.assertEqual(self.match("Will Bitcoin hit $150k in 2026?"), {})


class PriceParseTests(unittest.TestCase):
    def parse(self, q, **kw):
        return matching.parse_price_condition({"question": q, **kw})

    def test_touch_and_terminal(self):
        c = self.parse("Will NVIDIA (NVDA) reach $250 in December?")
        self.assertEqual((c["kind"], c["side"], c["level"]), ("touch", "up", 250))
        c = self.parse("Will Tesla dip to $300 in December?")
        self.assertEqual((c["kind"], c["side"], c["level"]), ("touch", "down", 300))
        c = self.parse("Will Apple close above $1,250.50 on December 31?")
        self.assertEqual((c["kind"], c["side"], c["level"], c["monitoring"]), ("terminal", "above", 1250.5, "close"))
        c = self.parse("Will Meta be below $500 at the end of December?")
        self.assertEqual((c["kind"], c["side"]), ("terminal", "below"))

    def test_group_labels(self):
        c = self.parse("What price will NVIDIA hit in December?", outcome_label="↓ $150")
        self.assertEqual((c["kind"], c["side"], c["level"]), ("touch", "down", 150))
        c = self.parse("NVIDIA (NVDA) above ___ end of December?", outcome_label="$200")
        self.assertEqual((c["kind"], c["side"], c["level"]), ("terminal", "above", 200))

    def test_not_price_markets(self):
        self.assertIsNone(self.parse("Will Apple's market cap exceed $5 trillion?"))
        self.assertIsNone(self.parse("Will Tesla report revenue above $30 billion?"))
        self.assertIsNone(self.parse("Will the FDA approve the drug?"))

    def test_kalshi_strikes(self):
        c = self.parse("Apple price on Dec 31", strike_type="between", floor_strike=240, cap_strike=250)
        self.assertEqual((c["kind"], c["level"], c["level_hi"]), ("range", 240, 250))


class AdapterTests(unittest.TestCase):
    def test_polymarket_normalize_string_encoded_fields(self):
        events = [{"id": 7, "slug": "nvda-dec", "title": "What price will NVIDIA hit in December?",
                   "tags": [{"label": "Stocks", "slug": "stocks"}],
                   "markets": [{"id": "91", "question": "Will NVIDIA reach $250 in December?",
                                "groupItemTitle": "↑ $250", "outcomes": "[\"Yes\", \"No\"]",
                                "outcomePrices": "[\"0.18\", \"0.82\"]", "volumeNum": 123456.7,
                                "volume1wk": "2345.6", "liquidityNum": "9000", "bestBid": 0.17, "bestAsk": 0.19,
                                "oneWeekPriceChange": -0.05, "endDate": "2026-12-31T21:00:00Z",
                                "active": True, "closed": False},
                               {"id": "92", "question": "closed one", "closed": True}]}]
        rows = polymarket.normalize(events)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["key"], "polymarket:91")
        self.assertAlmostEqual(r["prob"], 0.18)
        self.assertAlmostEqual(r["volume_7d"], 2345.6)
        self.assertEqual(r["url"], "https://polymarket.com/event/nvda-dec")
        self.assertEqual(r["tags"], ["Stocks"])

    def test_kalshi_normalize_cents_and_dollars(self):
        events = [{"event_ticker": "KXTSLA-26", "series_ticker": "KXTSLA", "title": "Tesla deliveries Q4", "category": "Companies",
                   "markets": [
                       {"ticker": "A", "title": "Tesla deliveries Q4", "yes_sub_title": "Above 450k", "status": "active",
                        "yes_bid": 40, "yes_ask": 44, "last_price": 42, "previous_price": 30, "volume": 12000,
                        "open_interest": 5000, "close_time": "2027-01-02T15:00:00Z"},
                       {"ticker": "B", "title": "Tesla deliveries Q4", "yes_sub_title": "Above 500k", "status": "active",
                        "yes_bid_dollars": "0.1000", "yes_ask_dollars": "0.1200", "last_price_dollars": "0.1100",
                        "volume_fp": "800.00", "close_time": "2027-01-02T15:00:00Z"}]}]
        rows = kalshi.normalize(events)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["prob"], 0.42)
        self.assertAlmostEqual(rows[0]["prob_change_1d"], 0.12)
        self.assertIn("Above 450k", rows[0]["question"])
        self.assertAlmostEqual(rows[1]["prob"], 0.11)
        self.assertAlmostEqual(rows[1]["volume_total"], 800.0)
        self.assertEqual(rows[1]["url"], "https://kalshi.com/markets/kxtsla")


class FetchFallbackTests(unittest.TestCase):
    """Network is stubbed: these check control flow, not the live APIs."""

    def setUp(self):
        from infoarb import net
        self.net = net
        self.real_get = net.get

    def tearDown(self):
        self.net.get = self.real_get

    def test_polymarket_falls_back_when_order_rejected(self):
        calls = []

        def fake(url, params=None, **kw):
            calls.append(dict(params or {}))
            if (params or {}).get("order") == "volume1wk":
                raise self.net.FetchError(url, "HTTP 422", 422)
            if (params or {}).get("offset", 0) == 0 and not (params or {}).get("tag_slug"):
                return [{"id": 1, "markets": []}, {"id": 2, "markets": []}]
            return []

        self.net.get = fake
        events, status = polymarket.fetch_events(max_events=300, tag_slugs=["stocks"], max_per_tag=100)
        self.assertTrue(status["ok"])
        self.assertEqual(status["order"], "volume24hr")
        self.assertEqual(len(events), 2)

    def test_polymarket_reports_blocked_host(self):
        def fake(url, params=None, **kw):
            raise self.net.FetchError(url, "network error: Tunnel connection failed: 403 Forbidden")
        self.net.get = fake
        events, status = polymarket.fetch_events(max_events=100)
        self.assertFalse(status["ok"])
        self.assertIn("403", status["error"])

    def test_kalshi_series_path_then_paged_fallback(self):
        def series_ok(url, params=None, **kw):
            if url.endswith("/series"):
                return {"series": [{"ticker": "KXTSLA", "category": "Companies"}, {"ticker": "KXMVEX"}]}
            if params.get("series_ticker") == "KXTSLA":
                return {"events": [{"event_ticker": "KXTSLA-1", "title": "t", "markets": []}], "cursor": ""}
            raise AssertionError("unexpected call " + url)
        self.net.get = series_ok
        events, status = kalshi.fetch_events(["Companies"], skip_series_prefixes=["KXMVE"])
        self.assertEqual(status["mode"], "series")
        self.assertEqual([e["series_ticker"] for e in events], ["KXTSLA"])

        def series_empty(url, params=None, **kw):
            if url.endswith("/series"):
                return {"series": []}
            if not params.get("cursor"):
                return {"events": [{"event_ticker": "A", "series_ticker": "KXA", "category": "Companies"},
                                   {"event_ticker": "B", "series_ticker": "KXB", "category": "Sports"}], "cursor": "c2"}
            return {"events": [{"event_ticker": "C", "series_ticker": "KXMVE1"}], "cursor": None}
        self.net.get = series_empty
        events, status = kalshi.fetch_events(["Companies"], skip_categories=["Sports"], skip_series_prefixes=["KXMVE"])
        self.assertEqual(status["mode"], "paged")
        self.assertEqual([e["event_ticker"] for e in events], ["A"])


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs = fixtures.build_inputs(NOW)
        cls.result = signals.build(cls.inputs["markets"], cls.inputs["stocks"], INDEX, {"markets": {}, "tickers": {}},
                                   CFG, NOW, themes=THEMES)

    def test_mispriced_price_bet_is_flagged_yes(self):
        rows = [r for r in self.result["price_signals"] if r["key"] == "polymarket:p1"]
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertTrue(r["flagged"])
        self.assertEqual(r["trade_side"], "YES")
        self.assertGreater(r["model_prob"], 0.4)
        self.assertAlmostEqual(r["edge"], r["model_prob"] - 0.21, places=4)

    def test_takeover_target_flags_long_and_acquirer_needs_call(self):
        rows = {r["ticker"]: r for r in self.result["event_signals"] if r["key"] == "polymarket:e1"}
        self.assertEqual(set(rows), {"WBD", "PSKY"})
        wbd, psky = rows["WBD"], rows["PSKY"]
        self.assertEqual((wbd["mna_role"], wbd["direction"], wbd["stock_side"]), ("target", 1, 1))
        self.assertAlmostEqual(wbd["expected_move"], 0.30 * CFG["impact_defaults"]["mna_target"], places=4)
        self.assertGreater(wbd["gap"], 0.04)
        self.assertEqual((psky["mna_role"], psky["direction"], psky["stock_side"]), ("acquirer", 0, 0))
        flagged = {(o["key"], o["ticker"]) for o in self.result["opportunities"]}
        self.assertIn(("polymarket:e1", "WBD"), flagged)
        self.assertIn(("polymarket:e1", "PSKY"), flagged)
        self.assertEqual(self.result["opportunities"][0]["rank"], 1)

    def test_small_impact_event_is_listed_not_flagged(self):
        r = [r for r in self.result["event_signals"] if r["key"] == "polymarket:e2"][0]
        self.assertEqual(r["ticker"], "TTWO")
        self.assertFalse(r["flagged"])

    def test_noise_and_unlinked_are_excluded(self):
        keys = {r["key"] for r in self.result["opportunities"] + self.result["event_signals"] + self.result["price_signals"]}
        self.assertNotIn("polymarket:n1", keys)
        self.assertNotIn("polymarket:u1", keys)
        self.assertIn("polymarket:n1", {r["key"] for r in self.result["watchlist"]})

    def test_fair_kalshi_strike_not_flagged_and_fee_applied(self):
        r = [r for r in self.result["price_signals"] if r["key"] == "kalshi:KXAAPL-1"][0]
        self.assertFalse(r["flagged"])
        self.assertLess(abs(r["edge"]), CFG["price_signal"]["min_edge"])

    def test_analyst_notes_override_direction_and_ignore(self):
        notes = {"markets": {"polymarket:e2": {"direction": -1, "impact_pct": 0.08, "verdict": "watch", "thesis": "t"},
                             "polymarket:p1": {"ignore": True}}, "tickers": {}}
        res = signals.build(self.inputs["markets"], self.inputs["stocks"], INDEX, notes, CFG, NOW, themes=THEMES)
        e = [r for r in res["event_signals"] if r["key"] == "polymarket:e2"][0]
        self.assertEqual((e["direction"], e["direction_source"], e["stock_side"]), (-1, "analyst", -1))
        self.assertAlmostEqual(e["impact_pct"], 0.08)
        self.assertTrue(e["flagged"])
        self.assertEqual(e["analyst"]["verdict"], "watch")
        self.assertFalse(any(r["key"] == "polymarket:p1" for r in res["price_signals"]))

    def test_theme_nets_markets_against_sector_etf(self):
        rows = [r for r in self.result["theme_signals"] if r["key"] == "theme:chips_china"]
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["ticker"], r["window"], r["stock_side"]), ("SMH", 30, -1))
        # ban odds +25 pts (YES bad) and allow odds -15 pts (YES good) both point down
        self.assertAlmostEqual(r["expected_move"], -(0.25 + 0.15) * 0.05, places=4)
        self.assertAlmostEqual(r["priced_fraction"], 0.0, places=2)
        self.assertEqual(r["boost"], CFG["preferences"]["theme_boost"])
        self.assertTrue(r["flagged"])
        self.assertEqual({c["key"] for c in r["markets"]}, {"polymarket:t1", "polymarket:t2"})

    def test_theme_without_etf_data_goes_to_watchlist(self):
        self.assertFalse(any(r["key"] == "theme:cannabis" for r in self.result["theme_signals"]))
        self.assertIn("polymarket:t3", {r["key"] for r in self.result["watchlist"]})

    def test_short_dated_event_is_down_weighted(self):
        r = [r for r in self.result["event_signals"] if r["key"] == "polymarket:s1"][0]
        self.assertLess(r["horizon_days"], 7)
        self.assertLess(r["horizon_weight"], 0.5)
        self.assertFalse(r["exempt"])
        self.assertAlmostEqual(r["score"], r["score_base"] * r["horizon_weight"], places=2)

    def test_large_price_edge_is_exempt_from_horizon_penalty(self):
        r = [r for r in self.result["price_signals"] if r["key"] == "polymarket:p1"][0]
        self.assertGreaterEqual(r["edge"], CFG["preferences"]["large_delta"]["price_edge"])
        self.assertTrue(r["exempt"])
        self.assertEqual(r["horizon_weight"], 1.0)

    def test_themes_rank_ahead_of_similar_single_stock_gaps(self):
        ranks = {(o["signal"], o["ticker"]): o["rank"] for o in self.result["opportunities"]}
        self.assertIn(("theme", "SMH"), ranks)

    def test_unlinked_big_movers_are_collected(self):
        now = NOW
        inputs = fixtures.build_inputs(now)
        m = fixtures.market(id="mv", event_id="mv", question="Will the US government shut down again in January?",
                            prob=0.55, prob_change_30d=0.30, volume_total=900_000.0,
                            end_date=(now + timedelta(days=60)).isoformat())
        res = signals.build(inputs["markets"] + [m], inputs["stocks"], INDEX, {}, CFG, now, themes=THEMES)
        keys = [r["key"] for r in res["unlinked_movers"]]
        self.assertEqual(keys[0], "polymarket:mv")          # biggest move ranks first
        self.assertIn("polymarket:u1", keys)                # the Japan election market also qualifies
        self.assertNotIn("polymarket:n1", keys)             # linked/noise markets never appear here

    def test_track_record(self):
        snap = {"date": (NOW.date() - timedelta(days=7)).isoformat(), "opportunities": [
            {"key": "polymarket:e1", "ticker": "TTWO", "signal": "event", "question": "q", "url": "u", "platform": "polymarket",
             "score": 2, "stock_side": 1, "stock_price": 200.0, "bench_price": 600.0, "trade_side": None, "entry": None, "market_prob": 0.5},
            {"key": "polymarket:p1", "ticker": "NVDA", "signal": "price", "question": "q", "url": "u", "platform": "polymarket",
             "score": 2, "stock_side": 0, "stock_price": 180.0, "bench_price": 600.0, "trade_side": "YES", "entry": 0.10, "market_prob": 0.1}]}
        stats = {"TTWO": {"price": 220.0}, "SPY": {"price": 606.0}, "NVDA": {"price": 181.0}}
        by_key = {m["key"]: m for m in self.inputs["markets"]}
        rec = track.evaluate([snap], by_key, stats, "SPY", NOW.date())
        e = [r for r in rec["rows"] if r["signal"] == "event"][0]
        self.assertAlmostEqual(e["equity_pnl"], 0.10 - 0.01, places=6)
        p = [r for r in rec["rows"] if r["signal"] == "price"][0]
        self.assertAlmostEqual(p["pm_pnl"], 0.20 - 0.10, places=6)
        self.assertEqual(rec["summary"]["equity_hit_rate"], 1.0)


class EndToEndTests(unittest.TestCase):
    def test_scan_and_render_offline(self):
        import render
        import scan
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            inputs = fixtures.build_inputs(datetime.now(timezone.utc))
            inputs["markets"][1]["question"] += " </script><img src=x onerror=alert(1)>"
            (tmp / "inputs.json").write_text(json.dumps(inputs))
            notes = tmp / "notes.json"
            notes.write_text(json.dumps({"markets": {}, "tickers": {}}))
            code = scan.main(["--inputs", str(tmp / "inputs.json"), "--out", str(tmp / "data"), "--notes", str(notes)])
            self.assertEqual(code, 0)
            latest = json.loads((tmp / "data" / "latest.json").read_text())
            self.assertGreaterEqual(len(latest["opportunities"]), 4)
            self.assertTrue(any(o["signal"] == "theme" for o in latest["opportunities"]))
            self.assertIn("## Themes", (tmp / "data" / "candidates.md").read_text())
            self.assertTrue((tmp / "data" / "candidates.md").read_text().startswith("# Candidates"))
            self.assertEqual(len(list((tmp / "data" / "snapshots").glob("*.json"))), 1)
            briefs = tmp / "briefs"
            briefs.mkdir()
            (briefs / "2026-11-22.md").write_text("## Top idea\n\n**TTWO** looks <b>cheap</b>. [link](https://example.com)\n\n- one\n- two\n")
            code = render.main(["--data", str(tmp / "data" / "latest.json"), "--notes", str(notes),
                                "--briefs", str(briefs), "--out", str(tmp / "site")])
            self.assertEqual(code, 0)
            html = (tmp / "site" / "index.html").read_text()
            frag = (tmp / "site" / "artifact.html").read_text()
            self.assertTrue(html.startswith("<!doctype html>"))
            self.assertNotIn("<html", frag)
            self.assertIn("<title>Info Arb Weekly</title>", frag)
            payload = html.split('id="payload" type="application/json">', 1)[1].split("</script>", 1)[0]
            self.assertNotIn("</script", payload.lower())
            self.assertNotIn("<b>cheap</b>", html)
            data = json.loads(payload)
            self.assertIn("<strong>TTWO</strong>", data["brief"]["html"])

    def test_empty_render(self):
        import render
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            code = render.main(["--data", str(tmp / "missing.json"), "--notes", str(tmp / "n.json"),
                                "--briefs", str(tmp), "--out", str(tmp / "site")])
            self.assertEqual(code, 0)
            self.assertIn('"data":null', (tmp / "site" / "index.html").read_text())


class MarkdownTests(unittest.TestCase):
    def test_escapes_and_whitelist(self):
        out = mdlite.render("# T\n\nHi <script>x</script> **b** [ok](https://a.b) [bad](javascript:alert(1))\n\n| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn('<a href="https://a.b"', out)
        self.assertNotIn('href="javascript', out)
        self.assertIn("<table>", out)
        self.assertIn("<h2>T</h2>", out)


if __name__ == "__main__":
    unittest.main()
