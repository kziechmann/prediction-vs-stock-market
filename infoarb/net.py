"""Tiny HTTP helper on the standard library: retries, backoff, JSON decoding.

Honors HTTPS_PROXY / SSL_CERT_FILE from the environment (urllib does this
natively), so it works behind corporate or sandbox proxies without extra code.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 info-arb-scanner/0.1"
)


class FetchError(RuntimeError):
    """Raised when a URL cannot be fetched after retries."""

    def __init__(self, url: str, reason: str, status: int | None = None):
        super().__init__(f"{reason} ({url})")
        self.url = url
        self.reason = reason
        self.status = status

    @property
    def host(self) -> str:
        return urllib.parse.urlparse(self.url).netloc


def build_url(url: str, params: dict | None = None) -> str:
    if not params:
        return url
    clean = {k: v for k, v in params.items() if v is not None}
    sep = "&" if "?" in url else "?"
    return url + sep + urllib.parse.urlencode(clean, doseq=True)


# Per-host "don't call before" times, driven by rate-limit headers (Kalshi sends
# x-ratelimit-remaining / -cost / -reset; others may send Retry-After).
_pause_until: dict[str, float] = {}
MAX_RATE_WAIT = 20.0


def _num_header(headers, name: str) -> float | None:
    try:
        v = headers.get(name) if headers is not None else None
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _note_rate_limit(host: str, headers) -> None:
    remaining = _num_header(headers, "x-ratelimit-remaining")
    cost = _num_header(headers, "x-ratelimit-cost") or 1.0
    reset = _num_header(headers, "x-ratelimit-reset")
    if remaining is not None and reset is not None and remaining < cost:
        _pause_until[host] = time.time() + min(reset, MAX_RATE_WAIT)


def _wait_for(host: str) -> None:
    delay = _pause_until.get(host, 0) - time.time()
    if delay > 0:
        time.sleep(delay)


def get(url: str, params: dict | None = None, *, as_json: bool = True,
        retries: int = 3, timeout: float = 30.0, backoff: float = 1.5,
        headers: dict | None = None, max_rate_waits: int = 8):
    """GET a URL and return parsed JSON (or text). Raises FetchError.

    429 responses wait for the server's reset/Retry-After time and do not use up `retries`.
    """
    full = build_url(url, params)
    host = urllib.parse.urlparse(full).netloc
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json, text/csv, */*"}
    if headers:
        hdrs.update(headers)
    last: FetchError | None = None
    attempt = rate_waits = 0
    while attempt < retries:
        _wait_for(host)
        req = urllib.request.Request(full, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                _note_rate_limit(host, resp.headers)
            return json.loads(body) if as_json else body
        except urllib.error.HTTPError as e:
            last = FetchError(full, f"HTTP {e.code}", e.code)
            if e.code == 429 and rate_waits < max_rate_waits:
                rate_waits += 1
                wait = _num_header(e.headers, "retry-after") or _num_header(e.headers, "x-ratelimit-reset") or backoff ** rate_waits
                _pause_until[host] = time.time() + min(max(wait, 0.5), MAX_RATE_WAIT)
                continue
            # 4xx other than rate limiting will not get better on retry.
            if 400 <= e.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            reason = str(getattr(e, "reason", e))
            if "Tunnel connection failed" in reason and ("403" in reason or "407" in reason):
                # An egress proxy refused this host: a policy decision, so retrying won't help.
                raise FetchError(full, f"blocked by network proxy ({host})", 403)
            last = FetchError(full, f"network error: {reason}")
        except json.JSONDecodeError:
            last = FetchError(full, "response was not valid JSON")
            break
        attempt += 1
        if attempt < retries:
            time.sleep(backoff ** attempt)
    assert last is not None
    raise last
