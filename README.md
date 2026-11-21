# Info Arb: prediction markets vs. stocks

A weekly scanner that looks for "information arbitrage" between prediction markets and
the stock market: places where the crowd on Polymarket or Kalshi has moved on something
about a listed company, and the company's stock hasn't.

Each week it:

1. pulls the most-traded open markets from **Polymarket** and **Kalshi** (volume and
   liquidity are the confidence measure),
2. links each market to a listed company via `companies.json` (names, products, CEOs,
   tickers; a few private companies map to listed shareholders at reduced weight),
3. pulls six months of daily prices for every linked stock plus SPY,
4. scores two kinds of divergence (below), and
5. writes a ranked list, a dashboard, and a snapshot for tracking how the calls do.

A scheduled Claude agent then reviews the top candidates (news, resolution rules,
direction and size of impact), writes a short brief, and publishes the dashboard. Its
instructions are in [`AGENT.md`](AGENT.md).

## The two signals

**Price bets.** Markets on the share price itself: "NVDA above $200 on Dec 31",
"TSLA hits $500 in December", Kalshi price ranges. The same contract is priced from the
stock's own volatility (lognormal, blend of 20- and 60-day realized vol, time in trading
days; the barrier-touch formula for "hits" markets, with a daily-close correction when
the rules use closing prices). Edge = stock-implied probability minus what you'd pay
(ask for YES, 1 − bid for NO), net of Kalshi's fee. The analyst can replace realized vol
with options-implied vol per ticker, which matters around earnings.

**Event odds.** Earnings, approvals, deals, lawsuits, leadership, product launches, index
inclusion. Over the last 7 days:

```
expected move = direction × Δ probability × impact      (impact = stock move if YES fully resolves)
gap           = expected move − stock's beta-adjusted return vs SPY
score         = |gap| / 1% × confidence
```

`gap / weekly σ` is shown alongside so you can tell a real gap from ordinary noise.
Direction and impact start as keyword heuristics plus the defaults in `config.json`. The
weekly analyst overrides them in `analysis/notes.json`, and those overrides persist.

Confidence (0–1) comes from traded volume (log scale, ~$1k → 0, ~$3M → 1) and resting
liquidity or open interest. Mention/tweet-count markets, popularity rankings, AI
leaderboards, "up or down today" and market-cap races are excluded or kept on the
watchlist only, since they're noise or derived from the price itself.

## Run it

Python 3.10+ and the standard library only; there's nothing to install.

```bash
python3 info_arb/scan.py            # live scan → info_arb/data/
python3 info_arb/render.py          # → info_arb/site/index.html (open it in a browser)
python3 info_arb/scan.py --rescore  # re-score after editing analysis/notes.json, no network
python3 info_arb/tests/test_infoarb.py   # offline tests
```

| Output | What it is |
|---|---|
| `data/latest.json` | Everything the dashboard shows |
| `data/candidates.md` | Compact review list for the analyst |
| `data/snapshots/DATE.json` | Flagged calls, used for the track record |
| `analysis/notes.json` | Analyst overrides and verdicts (persist week to week) |
| `analysis/briefs/DATE.md` | The weekly brief |
| `site/index.html` | Self-contained dashboard (any static host) |
| `site/artifact.html` | Same page without the document shell, for a claude.ai Artifact |

Tune thresholds in `config.json`; add companies or aliases in `companies.json` (avoid
aliases that are common words: Target, Block, Snap, Visa, Ford).

## Network access

The scanner needs these hosts (all public, no API keys):

- `gamma-api.polymarket.com` (Polymarket markets)
- `api.elections.kalshi.com` (Kalshi markets and price history)
- `query1.finance.yahoo.com`, `query2.finance.yahoo.com` (stock prices)
- `stooq.com` (fallback stock prices)

In a Claude Code cloud session, add them under the environment's network settings
(Allowed domains). If they are blocked, the scanner exits with code 2 or 3 and the weekly
agent reports the blocked hosts instead of publishing.

## Going public

The dashboard is a single static file with its data embedded, so any static host works.
For GitHub Pages: merge to `main`, set **Settings → Pages → Source** to "GitHub Actions",
and add the repository variable `INFO_ARB_PUBLIC = true`. The workflow in
`.github/workflows/info-arb-pages.yml` then deploys `info_arb/site` whenever it changes.
Before publishing, keep the "not investment advice" footer, and consider leaving the
analyst's position-level calls out of the public brief.

## Limits worth knowing

- Realized vol is a yardstick, not an options price. Expect apparent edges on price
  bets right before earnings; check implied vol before trading them.
- Polymarket's 7-day change is its own field; Kalshi's comes from daily candles for
  linked markets only.
- Holidays aren't in the trading-day count.
- Kalshi volume is in contracts ($1 max payout each), Polymarket's in dollars.
- Linking is regex-based. The analyst's `mapping-error` verdicts are how it improves;
  move recurring fixes into `companies.json`.
- Check that you can legally trade on each platform from where you live.
