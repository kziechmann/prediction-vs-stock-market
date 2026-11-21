"""Small parsing helpers shared by the source adapters."""

from __future__ import annotations

import json
from datetime import datetime, timezone


def fnum(value, default: float | None = None) -> float | None:
    """Coerce API numbers that may arrive as str, int, float or None."""
    if value is None or value == "":
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:  # NaN
        return default
    return out


def json_list(value) -> list:
    """Polymarket encodes some arrays as JSON strings ('["Yes","No"]')."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def parse_time(value) -> datetime | None:
    """Parse ISO-8601 strings (with or without Z) or unix seconds into aware UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
