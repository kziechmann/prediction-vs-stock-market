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


def get(url: str, params: dict | None = None, *, as_json: bool = True,
        retries: int = 3, timeout: float = 30.0, backoff: float = 1.5,
        headers: dict | None = None):
    """GET a URL and return parsed JSON (or text). Raises FetchError."""
    full = build_url(url, params)
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json, text/csv, */*"}
    if headers:
        hdrs.update(headers)
    last: FetchError | None = None
    for attempt in range(retries):
        req = urllib.request.Request(full, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if as_json else body
        except urllib.error.HTTPError as e:
            last = FetchError(full, f"HTTP {e.code}", e.code)
            # 4xx other than rate limiting will not get better on retry.
            if 400 <= e.code < 500 and e.code != 429:
                break
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            reason = str(getattr(e, "reason", e))
            if "Tunnel connection failed" in reason and ("403" in reason or "407" in reason):
                # An egress proxy refused this host: a policy decision, so retrying won't help.
                raise FetchError(full, f"blocked by network proxy ({urllib.parse.urlparse(full).netloc})", 403)
            last = FetchError(full, f"network error: {reason}")
        except json.JSONDecodeError:
            last = FetchError(full, "response was not valid JSON")
            break
        time.sleep(backoff ** attempt)
    assert last is not None
    raise last
