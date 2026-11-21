"""Industry / macro themes: markets that move a whole sector, measured against a sector ETF."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Theme:
    id: str
    name: str
    sector: str
    vehicle: str
    members: list[str]
    direction: int
    impact_pct: float
    patterns: list[re.Pattern]
    positive: list[re.Pattern] = field(default_factory=list)
    negative: list[re.Pattern] = field(default_factory=list)
    exclude: list[re.Pattern] = field(default_factory=list)


@dataclass
class ThemeMatch:
    theme: Theme
    direction: int      # what YES means for the vehicle: +1, -1, or 0 (needs a call)
    via: str            # the text that matched


def _rx(items) -> list[re.Pattern]:
    return [re.compile(p, re.I) for p in items or []]


class ThemeIndex:
    def __init__(self, data: dict):
        self.themes = [
            Theme(t["id"], t["name"], t.get("sector", ""), t["vehicle"], t.get("members", []),
                  int(t.get("direction", 0)), float(t.get("impact_pct", 0.03)), _rx(t["patterns"]),
                  _rx(t.get("positive")), _rx(t.get("negative")), _rx(t.get("exclude")))
            for t in data.get("themes", [])
        ]
        self.by_id = {t.id: t for t in self.themes}

    def match(self, market: dict) -> list[ThemeMatch]:
        specific = " ".join(x for x in (market.get("question"), market.get("outcome_label")) if x)
        full = f"{specific} {market.get('event_title') or ''}"
        out = []
        for t in self.themes:
            hit = next((m for rx in t.patterns if (m := rx.search(full))), None)
            if not hit or any(rx.search(full) for rx in t.exclude):
                continue
            pos = any(rx.search(specific) for rx in t.positive)
            neg = any(rx.search(specific) for rx in t.negative)
            direction = 1 if (pos and not neg) else -1 if (neg and not pos) else (0 if (pos and neg) else t.direction)
            out.append(ThemeMatch(t, direction, hit.group(0)))
        return out

    def vehicles(self) -> set[str]:
        return {t.vehicle for t in self.themes}


def load_themes(path: Path) -> ThemeIndex:
    p = Path(path)
    return ThemeIndex(json.loads(p.read_text())) if p.exists() else ThemeIndex({})
