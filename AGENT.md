# Weekly analyst runbook

You are the weekly analyst for the Info Arb scanner. The scanner finds candidates
mechanically; your job is the judgment it can't make: is the link right, which way does
the event cut, how big is it, has the stock already moved on it, and is the contract's
fine print a trap. Then write a short brief a trader can act on, and publish the dashboard.

**The owner's priorities.** Favor sector-wide themes and large, slower trends that the
stock market hasn't priced yet over short-term trades. A short-dated idea (resolving within
about a month) belongs in the brief only if its delta is very large: the scanner marks
these `exempt` (contract edge ≥ 20 pts, or unpriced stock move ≥ 5%). The ranking already
applies this (horizon weight × theme boost, see `config.json` → `preferences`); your review
order and the brief should follow it too.

All paths below are relative to the repository root. Work on the branch named in
`info_arb/BRANCH` (create nothing else; push only there).

## 0. Setup

```bash
git fetch origin "$(cat info_arb/BRANCH)" && git checkout "$(cat info_arb/BRANCH)" && git pull --ff-only
python3 info_arb/tests/test_infoarb.py    # offline, a few seconds; stop and report if it fails
```

## 1. Scan

```bash
python3 info_arb/scan.py
```

- Exit 0: continue.
- Exit 2 or 3: a data source was unreachable (stderr says which, and whether the
  sandbox proxy refused the host). **Do not substitute made-up or remembered data.**
  Commit nothing, skip to step 7, and report which hosts were blocked. The hosts this
  needs are listed in `info_arb/README.md` under "Network access".
- Partial results (one platform down, some tickers missing) are fine; say so in the brief.

## 2. Review the candidates

Read `info_arb/data/candidates.md`. Review in this order, and spend most of your time on
the first two:

1. **Themes** (the "Themes" table, keys `theme:<id>`). For each, check every netted market
   (listed in `latest.json` → `theme_signals[].markets`): is it really about this theme, and
   is its direction right (does YES help or hurt the sector ETF)? Fix a market with
   `markets.<key>.direction` / `impact_pct`, or drop it with `ignore`. Then judge the theme
   as a whole: is the ETF the right vehicle, which basket members are most exposed, has the
   sector already moved for this reason before the window? Record `themes.<id>.verdict`,
   `thesis` and, if the default is off, `impact_pct`.
2. **Long-dated company events** (flagged event rows resolving more than ~1 month out),
   plus the "needs a direction call" rows with the biggest odds moves (up to ~8).
3. **Short-dated rows** only if marked exempt (very large delta). Skip the rest.
4. **Big movers with no link** (bottom of candidates.md): skim for trends with a
   stock-market angle the scanner doesn't cover. If one is material and has a clear sector
   ETF, add a theme to `info_arb/themes.json` (same shape as the others; precise regex
   patterns, no common words) and say so in the brief. Otherwise mention it under Watching.

For detail on any row, look it up by `key` in `info_arb/data/latest.json`.

For each candidate:

1. **Link.** Is this market really about that listed company, and does it matter to the
   stock? Proxy links (e.g. OpenAI → MSFT) are weak by design. Wrong link → set
   `"verdict": "mapping-error"` and `"ignore": true`, or fix it with `"ticker"`.
2. **Fine print.** Open the market (URL in the row) and read the resolution rules.
   Watch for: resolution source (closing price vs intraday), odd dates, "announce" vs
   "complete", and multi-outcome ladders where the cheap leg is cheap for a reason.
3. **News.** Search the web for what moved the odds this week and when. Compare the
   timing with the stock's move. A stock that already moved before the 7-day window
   may have priced it in; a weekend move in the odds may simply not have reached the
   stock yet (stocks don't trade weekends).
4. **Direction and size** (event rows). Set `direction` (+1 YES helps the stock, -1 hurts)
   and `impact_pct` = your estimate of the stock move if YES fully resolves, given the
   company's size. Rough anchors: takeover target 20–40% premium; acquirer −2% to −6%;
   large-cap FDA approval 1–5%, small-cap 20–60%; CEO exit ±3–8%; earnings beat 2–5%;
   index inclusion 3–7%; major product delay −3% to −10% when it's the core franchise.
5. **Volatility** (price rows). If earnings or another known catalyst falls before the
   contract resolves, realized vol understates the real range. If you can find the
   options-implied vol for that expiry from a reputable source, record it as
   `tickers.<T>.implied_vol` (annualized decimal), plus `next_earnings`.
6. **Verdict.** `actionable` (you'd put money on it now), `watch` (real gap, wrong
   timing or too thin), `priced-in`, `noise`, or `mapping-error`. Write a `thesis` of
   1–3 sentences: why the gap exists, the catalyst and its date, and what would prove it wrong.

Practical notes from earlier runs:

- One market can link to two companies (e.g. a merger: target and acquirer). Give each its own
  direction and impact with `"by_ticker": {"TSLA": {"direction": 1, "impact_pct": 0.08}, ...}`.
- When news is already public and traded (e.g. review scores that moved the stock the same day),
  set `"impact_pct": 0` with verdict `priced-in`; 0 is respected, not replaced by the default.
- Markets that resolve on price drawdowns (e.g. "AI bubble burst" = 3 of 6 drawdown/bankruptcy
  conditions) are price-derived, not information. Price their legs from each stock's volatility
  (see `infoarb/quant.py`: `prob_touch`, `trading_days_between`) and judge the rules wording;
  don't treat their odds moves as unpriced news.
- Successor-name legs and tabloid follow-ups are noise: `"ignore": true`.
- If nothing clears the bar, say so plainly. That is a valid weekly result.

Record everything in `info_arb/analysis/notes.json` (schema in its `_doc` field), set
`"updated"` to today's date, and keep earlier entries unless they are clearly stale
(market closed, or a later verdict replaces them).

## 3. Re-score with your notes

```bash
python3 info_arb/scan.py --rescore
```

Check that the ranking now reflects your calls (ignored rows gone, directions applied). If you
added a theme in step 2, run the full `python3 info_arb/scan.py` again instead, since the new
ETF needs price data.

## 4. Write the brief

Create `info_arb/analysis/briefs/YYYY-MM-DD.md` (today's UTC date) in this shape:

```markdown
## Bottom line
Two or three sentences: the biggest unpriced trend this week and the single best idea.

## Sector themes and trends
1. **$ETF — theme.** Trade: e.g. "Short SMH, or underweight NVDA/AMD vs SPY". Why: the
   netted odds moves and how much is priced in, in numbers from the scan. Catalyst and
   timing. Risk / what would prove it wrong. Links: [top market](...) · [news](...)
(up to 3)

## Company ideas
Same shape, longer-dated first (up to 3).

## Short-term, very large deltas only
Only rows the scanner marked exempt; leave the section out if there are none.

## Watching
- One line each: real but not yet tradeable, and what would make it tradeable.

## Set aside
- One line each for big moves you judged priced in, noise, or mislinked.

## Track record
One short paragraph on how earlier calls are doing (from the dashboard's track record).
```

Rules: every number must come from `latest.json` or a source you link. Say "not
investment advice" once at the end. Keep it under ~600 words.

## 5. Render

```bash
python3 info_arb/render.py
```

## 6. Commit and push

```bash
git add info_arb/data/latest.json info_arb/data/candidates.md info_arb/data/snapshots \
        info_arb/analysis info_arb/site info_arb/themes.json
git commit -m "Weekly info-arb scan $(date -u +%F)"
git push -u origin "$(cat info_arb/BRANCH)"
```

Retry the push up to 4 times with backoff on network errors. If the push is rejected
because the remote moved, `git pull --no-rebase` and push again.

## 7. Publish the dashboard and report

- The dashboard page is already designed; do not restyle it. Publish
  `info_arb/site/artifact.html` with the Artifact tool. If `info_arb/ARTIFACT_URL`
  exists, read that artifact first, then publish to the same URL; otherwise publish a
  new one (icon "chart") and save its URL to `info_arb/ARTIFACT_URL` (commit and push it).
- Finish with a short message: the top 3 ideas (ticker, trade, one-line why, market
  link), the dashboard link, and anything that was blocked or partial.
