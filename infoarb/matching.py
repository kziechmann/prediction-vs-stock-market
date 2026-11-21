"""Link prediction markets to listed companies, classify the event, parse price thresholds."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- linking


@dataclass
class Link:
    ticker: str
    name: str
    weight: float          # 1.0 direct, <1 for private-company proxies
    via: str               # the alias that matched
    pos: int               # character offset of the match in the question text
    proxy_note: str | None = None
    dynamic: bool = False  # ticker read from "(TICKER)"/"$TICKER" text, not from companies.json


# Tickers written in market text, e.g. "Opendoor Technologies Inc. (OPEN)" or "$OPEN".
DYN_PAREN = re.compile(r"([A-Z][\w.&'’-]*(?:\s+[A-Z&][\w.&'’-]*){0,5})\s*\(([A-Z]{1,5}(?:\.[A-Z])?)\)")
DYN_DOLLAR = re.compile(r"\$([A-Z]{2,5})\b")
CRYPTO_CONTEXT = re.compile(r"crypto|token|\bcoin\b|memecoin|bitcoin|ethereum|solana|blockchain|airdrop|\bfdv\b", re.I)
NOT_TICKERS = set("""US USA UK EU UN AI AGI GDP CPI PCE PPI FED FOMC ECB BOE BOJ NFP NFL NBA WNBA MLB NHL MLS UFC WWE PGA
ETF IPO CEO CFO CTO SEC FDA DOJ FTC FCC CDC WHO NATO OPEC IMF WTI BTC ETH SOL XRP DOGE ADA BNB USDT USDC TRX LTC HYPE SUI
PEPE LINK AVAX DOT SHIB TON NEAR APT ARB OP ENA PUMP WLFI TRUMP NYC LA DC GOP DNC RNC MAGA TV PM AM ET PT UTC EST PST EDT PDT
YES NO ATH EV EVS OK II III IV VI VII AFC NFC ACC EPL UCL MVP COVID ICE IRS EPA DHS DOD NASA SCOTUS APEC BRICS ESPN HBO
CNN BBC NPR CBS ABC NBC TBD TBA ID IQ OTC NYSE CEST CET GMT HIGH LOW FC SC AC CF DAX DJIA SPX NDX VIX
FTSE NIKKEI KOSPI""".split())
FINANCE_TAGS = {"stocks", "business", "finance", "tech", "earnings", "ipos", "ipo", "economy", "companies",
                "financials", "big tech", "ai", "science and technology", "equities"}
STOCK_WORDS = re.compile(r"\bstocks?\b|\bshares?\b|share price|stock price|market cap|\bipo\b|\bearnings\b|"
                         r"\bclos(e|es|ed|ing)\b|\b(hit|reach|dip)\b", re.I)


def finance_context(market: dict) -> bool:
    tags = {str(t).lower() for t in market.get("tags") or []}
    text = " ".join(x for x in (market.get("question"), market.get("event_title")) if x)
    return bool(tags & FINANCE_TAGS) or bool(STOCK_WORDS.search(text))


class CompanyIndex:
    def __init__(self, data: dict):
        self.companies = []
        for c in data.get("companies", []):
            self.companies.append(self._compile(c["ticker"], c["name"], c.get("aliases", []), c.get("cs", []), 1.0, None))
        for p in data.get("proxies", []):
            for t in p["tickers"]:
                self.companies.append(self._compile(t, p["name"], p.get("aliases", []), [], p.get("weight", 0.3), p.get("note"), proxy=True))
        self.names = {c["ticker"]: c["name"] for c in data.get("companies", [])}

    @staticmethod
    def _compile(ticker, name, aliases, cs, weight, note, proxy=False):
        def alt(words):
            return "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))
        ci = re.compile(rf"(?<![\w$]){'(?:' + alt(aliases) + ')'}(?!\w)", re.I) if aliases else None
        sym = [ticker, ticker.replace("-", ".")]
        cs_words = list(cs)
        tick = re.compile(rf"\$(?:{alt(sym)})\b|\((?:{alt(sym)})\)") if not proxy else None
        csr = re.compile(rf"(?<![\w$]){'(?:' + alt(cs_words) + ')'}(?!\w)") if cs_words else None
        return {"ticker": ticker, "name": name, "ci": ci, "cs": csr, "tick": tick,
                "weight": weight, "note": note, "proxy": proxy}

    def find(self, text: str, allow_detected: bool = False) -> list[Link]:
        found: dict[str, Link] = {}
        for c in self.companies:
            best = None
            for rx in (c["tick"], c["cs"], c["ci"]):
                if rx is None:
                    continue
                m = rx.search(text)
                if m and (best is None or m.start() < best.start()):
                    best = m
            if best is None:
                continue
            link = Link(c["ticker"], c["name"], c["weight"], best.group(0), best.start(),
                        c["note"] if c["proxy"] else None)
            prev = found.get(c["ticker"])
            if prev is None or link.weight > prev.weight:
                found[c["ticker"]] = link
        if allow_detected and not any(link.weight >= 1 for link in found.values()) and not CRYPTO_CONTEXT.search(text):
            for m in DYN_PAREN.finditer(text):
                t = m.group(2)
                if len(t) >= 2 and t not in NOT_TICKERS and t not in found:
                    found[t] = Link(t, m.group(1).strip(), 1.0, f"({t})", m.start(2), None, dynamic=True)
            for m in DYN_DOLLAR.finditer(text):
                t = m.group(1)
                if t not in NOT_TICKERS and t not in found:
                    found[t] = Link(t, t, 1.0, f"${t}", m.start(1), None, dynamic=True)
        links = list(found.values())
        if any(link.weight >= 1 for link in links):
            links = [link for link in links if link.weight >= 1]
        return sorted(links, key=lambda link: link.pos)

    def link_market(self, market: dict) -> list[Link]:
        specific = " ".join(x for x in (market.get("question"), market.get("outcome_label")) if x)
        links = self.find(specific, allow_detected=finance_context(market))
        if not links:
            ev = self.find(market.get("event_title") or "")
            if len(ev) == 1:
                links = ev
        return links


NAME_STOP = {"inc", "corp", "corporation", "company", "co", "ltd", "plc", "holdings", "holding", "group",
             "technologies", "technology", "the", "class", "common", "stock", "shares", "and", "will", "what"}


def names_agree(a: str | None, b: str | None) -> bool:
    """Do two company names share a meaningful word? Used to vet tickers read from market text."""
    def words(x):
        return {w for w in re.findall(r"[a-z0-9]+", (x or "").lower()) if len(w) >= 3 and w not in NAME_STOP}
    return bool(words(a) & words(b))


def load_index(path: Path) -> CompanyIndex:
    return CompanyIndex(json.loads(Path(path).read_text()))


# --------------------------------------------------------------------------- classification

NOISE = [
    (re.compile(r"\b(say|says|said|mention|mentions|mentioned|tweet|tweets|tweeted|posts?|posted|word)\b"), "mention/post count"),
    (re.compile(r"year in search|most searched|\bsearched\b|#1 (free )?app|top free app|app store|google play|most streamed|wrapped|top (song|artist|album)|box office|opening weekend|rotten tomatoes|youtube views|subscribers on youtube"), "popularity ranking"),
    (re.compile(r"best ai model|top ai model|ai model (is|be) (best|#1|top)|lmarena|chatbot arena|text arena|\barena\b|"
                r"humanity.s last exam|frontiermath|arc-agi|swe-bench|benchmark score"), "AI leaderboard"),
]

CATEGORY_RULES = [
    ("up_down", re.compile(r"\bup or down\b")),
    ("market_cap", re.compile(r"market cap|largest company|most valuable|biggest company|\$\s?\d+(?:\.\d+)?\s?(?:t|tn|trillion)\b|\btrillion\b")),
    ("bankruptcy", re.compile(r"bankrupt|chapter 11|chapter 7|insolven|delist")),
    ("index_inclusion", re.compile(r"(added to|join|joins|inclusion in|included in|enter)\s+(the\s+)?(s&p ?500|nasdaq[- ]100|dow jones|dow\b)")),
    ("mna", re.compile(r"acquir|acquisition|\bmerge|merger|takeover|buyout|\bbuy\b|\bbuys\b|purchase|bid for|take private|go private|tender offer|\bdeal\b")),
    ("earnings", re.compile(r"earnings|\beps\b|revenue|quarterly|guidance|deliver(y|ies|ed)|\bq[1-4]\b|net income|profit|beat (estimates|expectations)")),
    ("regulatory", re.compile(r"\bfda\b|approv|antitrust|\bdoj\b|\bftc\b|\bsec\b|regulator|\bban\b|banned|tariff|sanction|export control|investigation|\bprobe\b|break ?up|divest|cfius|european commission|eu commission")),
    ("legal", re.compile(r"lawsuit|\bsue[sd]?\b|court|verdict|ruling|\btrial\b|settle|judge|guilty|appeal")),
    ("leadership", re.compile(r"\bceo\b|step down|steps down|resign|fired|ousted|successor|chairman|out as")),
    ("corporate_action", re.compile(r"stock split|\bsplit\b|dividend|buyback|repurchase|spin ?off|spinoff|\bipo\b")),
    ("product", re.compile(r"launch|release|unveil|announce|\bship|debut|available|robotaxi|delay|postpone|before 20\d\d|by (the )?(end of )?20\d\d")),
]

POSITIVE = re.compile(r"\b(beat|beats|exceed|exceeds|above|more than|over|at least|top|approv\w*|clear(ed|s)?|allow\w*|lift\w*|win|wins|won|grant\w*|dismiss\w*|overturn\w*|launch\w*|release\w*|split|dividend|buyback|added|join|joins)\b")
NEGATIVE = re.compile(r"\b(miss|misses|below|less than|under|fewer than|ban|bans|banned|block\w*|reject\w*|den(y|ies|ied)|tariffs?|sanction\w*|fine[sd]?|probe|investigat\w*|break ?up|divest\w*|restrict\w*|lose|loses|lost|guilty|liable|delay\w*|postpone\w*|cancel\w*|recall\w*|bankrupt\w*|delist\w*)\b")
ACQUIRE_ACTIVE = re.compile(r"\b(acquire[sd]?|buy|buys|purchase[sd]?|takeover of|bid for|merge with|take over)\b")
ACQUIRE_PASSIVE = re.compile(r"\b(be |been |is |gets? )?(acquired|bought|purchased|taken private|taken over)\b")


def noise_reason(text: str) -> str | None:
    t = text.lower()
    for rx, reason in NOISE:
        if rx.search(t):
            return reason
    return None


def classify(text: str) -> str:
    t = text.lower()
    for name, rx in CATEGORY_RULES:
        if rx.search(t):
            return name
    return "other"


def heuristic_direction(category: str, text: str, link: Link | None = None) -> int:
    """+1 if YES is good for the stock, -1 if bad, 0 if unclear. Coarse on purpose."""
    t = text.lower()
    if category in ("bankruptcy",):
        return -1
    if category in ("index_inclusion",):
        return 1
    if category == "mna":
        return 1 if (link and mna_role(text, link) == "target") else 0
    if category == "leadership":
        return 0
    if category in ("up_down", "market_cap"):
        return 0
    pos, neg = bool(POSITIVE.search(t)), bool(NEGATIVE.search(t))
    if category == "corporate_action" and re.search(r"\bipo\b|spin", t):
        return 0
    if pos and not neg:
        return 1
    if neg and not pos:
        return -1
    return 0


def mna_role(text: str, link: Link) -> str:
    """Is the linked company the target or the acquirer in an M&A question?"""
    t = text.lower()
    active = ACQUIRE_ACTIVE.search(t)
    passive = ACQUIRE_PASSIVE.search(t)
    if passive and link.pos < passive.start():
        return "target"
    if active:
        return "target" if link.pos > active.start() else "acquirer"
    return "unknown"


# --------------------------------------------------------------------------- price thresholds

AMT = r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]

HIT_HL = re.compile(r"\b(hit|hits|reach|reaches)\s*\((high|low)\)\s*" + AMT)
METRIC_WORDS = re.compile(r"download|launch|\bsales\b|growth|point of sale|\bfares?\b|deliver|revenue|subscriber|"
                          r"\busers\b|\bunits\b|ticket|gas price|price of any|lowest price|highest price of|average price|"
                          r"\bmsrp\b|retail price|\bcost\b|salary|\brent\b|home price|metascore|\bscore\b|rating|"
                          r"followers|\bviews\b|streams|box office|\beps\b|guidance|market cap|valuation|"
                          r"\bgdp\b|inflation|\bcpi\b|unemployment|interest rate|bitcoin|ethereum|\bgold\b|\boil\b")
PRICE_CONTEXT = re.compile(r"\bstocks?\b|\bshares?\b|share price|stock price|closing price|"
                           r"\bclos(e|es|ed|ing)\b\s*(above|below|at|between|under|over|higher|lower)|"
                           r"\(([A-Z]{2,5}(?:\.[A-Z])?)\)|\$[A-Z]{2,5}\b|\b(finish|end|trade)s?\s*(above|below)|"
                           r"\bwhat price\b|\bprice will\b|"
                           r"\b(hit|hits|reach|reaches|dip|dips|fall|falls|drop|drops)\s*(to\s*)?\$|"
                           r"\b(be|is|close|trade|finish|end)\s+(above|below|over|under)\s*\$", re.I)
TOUCH_UP = re.compile(r"\b(reach|reaches|hit|hits|rise to|rises to|climb to|surpass|surpasses|exceed|exceeds|break above|trade above|go above)\b\s*(?:a high of\s*)?" + AMT)
TOUCH_DOWN = re.compile(r"\b(dip to|dips to|fall to|falls to|drop to|drops to|sink to|sinks to|fall below|falls below|drop below|drops below|go below|trade below|hit a low of)\b\s*" + AMT)
RANGE = re.compile(r"(?:between\s*)?" + AMT + r"\s*(?:-|–|to|and)\s*" + AMT)
CLOSE_REL = re.compile(r"\b(?:close|closes|closed|trade|trades)\s+(above|below)\s+(?:its\s+|the\s+)?[a-z ]{0,24}?\(?" + AMT)
ABOVE = re.compile(r"(close above|closes above|close at or above|at or above|above|over|higher than|greater than|≥|>)\s*" + AMT)
BELOW = re.compile(r"(close below|closes below|close at or below|at or below|below|under|lower than|less than|≤|<)\s*" + AMT)
LABEL = re.compile(r"^\s*(↑|↓|>|<|≥|≤|above|below)?\s*\$?\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(\+|or more|or higher|or less|or lower)?\s*$", re.I)


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_price_condition(market: dict) -> dict | None:
    """Turn a share-price question into {kind, level(s), side, monitoring}.

    kind: 'touch' (hit at any time before expiry), 'terminal' (above/below at expiry),
    'range' (between two levels at expiry). Returns None for anything else.
    """
    q = (market.get("question") or "")
    label = market.get("outcome_label") or ""
    text = f"{q} {label}".lower()
    if re.search(r"market cap|market value|trillion|billion|revenue|earnings|eps\b|deliver", text):
        return None
    raw = f"{q} {label} {market.get('event_title') or ''}"
    if METRIC_WORDS.search(raw.lower()):
        return None  # a business metric or another asset, not this company's share price
    if not (PRICE_CONTEXT.search(raw) or PRICE_CONTEXT.search((market.get("description") or "")[:400])):
        return None  # no sign the threshold is a share price
    monitoring = "close" if re.search(r"\bclos(e|es|ing)\b", text + " " + (market.get("description") or "").lower()[:300]) else "intraday"

    st = (market.get("strike_type") or "").lower()
    lo, hi = market.get("floor_strike"), market.get("cap_strike")
    if st and (lo or hi):
        if st.startswith("greater") and lo:
            return {"kind": "terminal", "side": "above", "level": lo, "monitoring": "close"}
        if st.startswith("less") and hi:
            return {"kind": "terminal", "side": "below", "level": hi, "monitoring": "close"}
        if st == "between" and lo and hi:
            return {"kind": "range", "side": "between", "level": lo, "level_hi": hi, "monitoring": "close"}

    m = HIT_HL.search(text)
    if m:
        return {"kind": "touch", "side": "up" if m.group(2) == "high" else "down", "level": _num(m.group(3)),
                "monitoring": monitoring}
    m = TOUCH_UP.search(text)
    if m:
        return {"kind": "touch", "side": "up", "level": _num(m.group(2)), "monitoring": monitoring}
    m = TOUCH_DOWN.search(text)
    if m:
        return {"kind": "touch", "side": "down", "level": _num(m.group(2)), "monitoring": monitoring}

    lm = LABEL.match(label)
    if lm and re.search(r"\b(hit|reach|price will)\b", q.lower()):
        arrow, val, suffix = lm.group(1) or "", _num(lm.group(2)), (lm.group(3) or "").lower()
        if arrow in ("↓",) or "less" in suffix or "lower" in suffix:
            return {"kind": "touch", "side": "down", "level": val, "monitoring": monitoring}
        return {"kind": "touch", "side": "up", "level": val, "monitoring": monitoring}
    if lm and re.search(r"\b(above|below|close|end of|on)\b", q.lower()):
        arrow, val, suffix = (lm.group(1) or "").lower(), _num(lm.group(2)), (lm.group(3) or "").lower()
        side = "below" if arrow in ("<", "≤", "below", "↓") or "less" in suffix or "lower" in suffix else "above"
        if "below" in q.lower() and not arrow:
            side = "below"
        return {"kind": "terminal", "side": side, "level": val, "monitoring": "close"}

    m = RANGE.search(text)
    if m and re.search(r"between|close at|close between|range|\bend\b|finish", text):
        a, b = sorted((_num(m.group(1)), _num(m.group(2))))
        if b > a:
            return {"kind": "range", "side": "between", "level": a, "level_hi": b, "monitoring": "close"}
    m = CLOSE_REL.search(text)
    if m:
        return {"kind": "terminal", "side": m.group(1), "level": _num(m.group(2)), "monitoring": "close"}
    m = ABOVE.search(text)
    if m:
        return {"kind": "terminal", "side": "above", "level": _num(m.group(2)), "monitoring": "close"}
    m = BELOW.search(text)
    if m:
        return {"kind": "terminal", "side": "below", "level": _num(m.group(2)), "monitoring": "close"}
    return None


def window_start_month(text: str, today) -> int | None:
    """If the question names a month (e.g. 'in November'), return that month number."""
    t = text.lower()
    for i, name in enumerate(MONTHS, start=1):
        if re.search(rf"\b(in|during|of|end of)\s+{name}\b", t):
            return i
    return None
