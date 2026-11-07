"""Read-only client for Polymarket's public APIs (no keys, no accounts).

Only two hosts are ever contacted:

* ``gamma-api.polymarket.com`` for search, market and event metadata
* ``clob.polymarket.com`` for live midpoints and price history

Every request has a hard timeout and a small bounded retry on 429/5xx, so a
slow or unreachable API fails cleanly under cron and the gateway instead of
hanging the agent.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
USER_AGENT = "hermes-oddsradar/0.1 (+https://github.com/WV629/hermes-oddsradar)"
ALLOWED_HOSTS = {"gamma-api.polymarket.com", "clob.polymarket.com"}

Transport = Callable[[str, float], Any]


class ApiError(RuntimeError):
    """Raised when the API cannot be reached or answers with an error."""


def _urllib_transport(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - host allowlisted below
        return json.loads(resp.read().decode("utf-8"))


class PolymarketClient:
    """Tiny JSON client. ``transport`` is injectable so tests run offline."""

    def __init__(self, transport: Optional[Transport] = None, timeout: float = 10.0,
                 retries: int = 2, backoff: float = 0.6):
        self._transport = transport or _urllib_transport
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff

    # -- plumbing ---------------------------------------------------------
    def get(self, base: str, path: str, params: Optional[dict] = None) -> Any:
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"{base}{path}" + (f"?{query}" if query else "")
        host = urllib.parse.urlparse(url).hostname
        if host not in ALLOWED_HOSTS:  # defence in depth: never talk to anything else
            raise ApiError(f"refusing to contact {host!r}")
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return self._transport(url, self.timeout)
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code not in (429, 500, 502, 503, 504):
                    raise ApiError(f"HTTP {exc.code} from {host}") from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last = exc
            if attempt < self.retries:
                time.sleep(self.backoff * (2 ** attempt))
        raise ApiError(f"{host} unreachable: {last}")

    # -- endpoints --------------------------------------------------------
    def search_events(self, query: str, limit: int = 5) -> list[dict]:
        data = self.get(GAMMA, "/public-search", {
            "q": query, "limit_per_type": limit, "events_status": "active",
            "keep_closed_markets": 0,
        })
        return list((data or {}).get("events") or [])

    def market_by_slug(self, slug: str) -> Optional[dict]:
        data = self.get(GAMMA, "/markets", {"slug": slug})
        return data[0] if isinstance(data, list) and data else None

    def event_by_slug(self, slug: str) -> Optional[dict]:
        data = self.get(GAMMA, "/events", {"slug": slug})
        return data[0] if isinstance(data, list) and data else None

    def top_markets(self, limit: int = 100) -> list[dict]:
        data = self.get(GAMMA, "/markets", {
            "active": "true", "closed": "false", "order": "volume24hr",
            "ascending": "false", "limit": limit,
        })
        return list(data or [])

    def midpoint(self, token_id: str) -> Optional[float]:
        data = self.get(CLOB, "/midpoint", {"token_id": token_id})
        try:
            return float((data or {}).get("mid"))
        except (TypeError, ValueError):
            return None

    def price_history(self, token_id: str, interval: str = "1w", fidelity: int = 180) -> list[dict]:
        data = self.get(CLOB, "/prices-history", {
            "market": token_id, "interval": interval, "fidelity": fidelity,
        })
        return list((data or {}).get("history") or [])
