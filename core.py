"""Odds Radar core: market lookup, the persistent watchlist and the alert engine.

Nothing here imports Hermes, so the whole engine is unit-testable offline.
The watchlist is one JSON file in the plugin's data directory, written
atomically (temp file + rename) under a lock so the CLI, the cron script and a
gateway session can share it safely.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from .api import ApiError, PolymarketClient

REARM_GAP = 0.01  # 1 pt hysteresis before a threshold alert can fire again
_LOCK = threading.Lock()


class OddsError(ValueError):
    """User-facing error with a readable message."""


# ---------------------------------------------------------------- parsing --
def _jlist(value: Any) -> list:
    """Gamma double-encodes arrays as JSON strings ("[\"Yes\", \"No\"]")."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.startswith("["):
        try:
            out = json.loads(value)
            return out if isinstance(out, list) else []
        except ValueError:
            return []
    return []


def _f(value: Any) -> Optional[float]:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Market:
    slug: str
    question: str
    outcomes: list[str]
    prices: list[float]
    token_ids: list[str]
    event_slug: str = ""
    event_title: str = ""
    label: str = ""
    closed: bool = False
    active: bool = True
    end_date: str = ""
    volume_24h: float = 0.0
    volume: float = 0.0
    liquidity: float = 0.0
    change_1d: Optional[float] = None
    change_1w: Optional[float] = None
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None

    @classmethod
    def from_gamma(cls, m: dict, event: Optional[dict] = None) -> "Market":
        ev = event or ((m.get("events") or [None])[0]) or {}
        return cls(
            slug=m.get("slug") or "",
            question=m.get("question") or m.get("title") or "",
            outcomes=[str(o) for o in _jlist(m.get("outcomes"))] or ["Yes", "No"],
            prices=[p for p in (_f(x) for x in _jlist(m.get("outcomePrices"))) if p is not None],
            token_ids=[str(t) for t in _jlist(m.get("clobTokenIds"))],
            event_slug=ev.get("slug") or "",
            event_title=ev.get("title") or "",
            label=m.get("groupItemTitle") or "",
            closed=bool(m.get("closed")),
            active=bool(m.get("active", True)),
            end_date=(m.get("endDate") or "")[:10],
            volume_24h=_f(m.get("volume24hr")) or 0.0,
            volume=_f(m.get("volumeNum") or m.get("volume")) or 0.0,
            liquidity=_f(m.get("liquidityNum") or m.get("liquidity")) or 0.0,
            change_1d=_f(m.get("oneDayPriceChange")),
            change_1w=_f(m.get("oneWeekPriceChange")),
            best_bid=_f(m.get("bestBid")),
            best_ask=_f(m.get("bestAsk")),
        )

    def outcome_index(self, outcome: str) -> int:
        for i, name in enumerate(self.outcomes):
            if name.lower() == outcome.lower():
                return i
        raise OddsError(f"'{outcome}' is not an outcome of this market (outcomes: {', '.join(self.outcomes)})")

    def price(self, outcome: str = "Yes") -> Optional[float]:
        i = self.outcome_index(outcome)
        return self.prices[i] if i < len(self.prices) else None

    @property
    def url(self) -> str:
        if self.event_slug and self.event_slug != self.slug:
            return f"https://polymarket.com/event/{self.event_slug}/{self.slug}"
        return f"https://polymarket.com/event/{self.slug}"

    @property
    def resolved_outcome(self) -> Optional[str]:
        """On a closed market the winning outcome trades at (or very near) 1."""
        if not self.closed or not self.prices:
            return None
        best = max(range(len(self.prices)), key=lambda i: self.prices[i])
        return self.outcomes[best] if self.prices[best] >= 0.98 else None


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,200}$")


def parse_ref(ref: str) -> tuple[Optional[str], Optional[str]]:
    """Return (event_slug, market_slug) from a polymarket.com URL or a bare slug."""
    ref = (ref or "").strip()
    if not ref:
        raise OddsError("Give a Polymarket URL or market slug.")
    if "://" in ref or ref.startswith("polymarket.com"):
        parsed = urlparse(ref if "://" in ref else f"https://{ref}")
        if not (parsed.hostname or "").endswith("polymarket.com"):
            raise OddsError("Only polymarket.com links are supported.")
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in ("event", "market"):
            event = parts[1] if parts[0] == "event" else None
            market = parts[2] if parts[0] == "event" and len(parts) >= 3 else (parts[1] if parts[0] == "market" else None)
            return event, market
        raise OddsError("That link does not point to a Polymarket event or market.")
    if not _SLUG_RE.match(ref):
        raise OddsError(f"'{ref}' is neither a Polymarket URL nor a slug.")
    return None, ref


class Radar:
    """Everything the tools, slash command and CLI share."""

    def __init__(self, data_dir: Path, client: Optional[PolymarketClient] = None,
                 clock=time.time):
        self.data_dir = Path(data_dir)
        self.client = client or PolymarketClient()
        self.clock = clock

    # ------------------------------------------------------------ lookup --
    def resolve(self, ref: str) -> Market:
        event_slug, market_slug = parse_ref(ref)
        if market_slug:
            m = self.client.market_by_slug(market_slug)
            if m:
                return Market.from_gamma(m)
            if event_slug is None:
                event_slug = market_slug  # a bare slug may be an event slug
        ev = self.client.event_by_slug(event_slug) if event_slug else None
        if not ev and event_slug and not market_slug:
            m = self.client.market_by_slug(event_slug)  # single-market events often share the slug
            if m:
                return Market.from_gamma(m)
        if not ev:
            raise OddsError(f"No Polymarket market or event found for '{ref}'.")
        markets = [x for x in (ev.get("markets") or []) if not x.get("closed")] or (ev.get("markets") or [])
        if len(markets) == 1:
            return Market.from_gamma(markets[0], ev)
        options = sorted((Market.from_gamma(x, ev) for x in markets),
                         key=lambda mk: -(mk.price("Yes") or 0) if "Yes" in mk.outcomes else 0)
        listing = "\n".join(f"  {mk.slug}  ({mk.label or mk.question}, {_pct(mk.prices[0] if mk.prices else None)})"
                            for mk in options[:12])
        raise OddsError(f"'{ev.get('title')}' has {len(markets)} markets; pick one slug:\n{listing}")

    def live_price(self, market: Market, outcome: Optional[str] = None) -> Optional[float]:
        """CLOB midpoint (live) with the Gamma snapshot as fallback."""
        i = market.outcome_index(outcome) if outcome else 0
        outcome = market.outcomes[i]
        if not market.closed and i < len(market.token_ids):
            try:
                mid = self.client.midpoint(market.token_ids[i])
                if mid is not None and 0.0 <= mid <= 1.0:
                    return mid
            except ApiError:
                pass
        return market.price(outcome)

    def search(self, query: str, limit: int = 5) -> list[dict]:
        if not (query or "").strip():
            raise OddsError("Search needs a query, e.g. 'bitcoin' or 'fed rates'.")
        out = []
        for ev in self.client.search_events(query.strip(), limit=limit)[:limit]:
            markets = [Market.from_gamma(m, ev) for m in (ev.get("markets") or []) if not m.get("closed")]
            markets.sort(key=lambda mk: -(mk.prices[0] if mk.prices else 0))
            out.append({
                "event": ev.get("title") or "", "event_slug": ev.get("slug") or "",
                "volume_24h": _f(ev.get("volume24hr")) or 0.0,
                "markets": [{"slug": mk.slug, "label": mk.label or mk.question,
                             "outcomes": mk.outcomes, "prices": mk.prices} for mk in markets[:6]],
                "more_markets": max(0, len(markets) - 6),
            })
        return out

    def movers(self, limit: int = 8, min_volume: float = 50_000.0) -> list[Market]:
        pool = [Market.from_gamma(m) for m in self.client.top_markets(limit=150)]
        pool = [m for m in pool if m.change_1d is not None and m.volume_24h >= min_volume and not m.closed
                and m.prices and 0.015 < m.prices[0] < 0.985]  # skip markets that are effectively settled
        pool.sort(key=lambda m: abs(m.change_1d or 0), reverse=True)
        return pool[:limit]

    def sparkline(self, market: Market, outcome: Optional[str] = None, interval: str = "1w") -> str:
        i = market.outcome_index(outcome) if outcome else 0
        if i >= len(market.token_ids):
            return ""
        try:
            hist = self.client.price_history(market.token_ids[i], interval=interval)
        except ApiError:
            return ""
        return spark([float(h["p"]) for h in hist if "p" in h])

    # ---------------------------------------------------------- watchlist --
    @property
    def path(self) -> Path:
        return self.data_dir / "watchlist.json"

    def load(self) -> list["Watch"]:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return []
        except ValueError as exc:
            raise OddsError(f"watchlist file is corrupt ({self.path}): {exc}") from exc
        return [Watch.from_dict(w) for w in raw.get("watches", [])]

    def save(self, watches: list["Watch"]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "watches": [asdict(w) for w in watches]}, indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, prefix=".watchlist.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def add_watch(self, ref: str, outcome: Optional[str] = None, move: Optional[float] = 5.0,
                  above: Optional[float] = None, below: Optional[float] = None) -> "Watch":
        rules = Rules.build(move=move, above=above, below=below)
        market = self.resolve(ref)
        # Default to the first outcome: "Yes" on binary markets, "Over"/a team name elsewhere.
        outcome = market.outcomes[market.outcome_index(outcome)] if outcome else market.outcomes[0]
        if market.closed:
            raise OddsError(f"'{market.question}' is already closed.")
        price = self.live_price(market, outcome)
        if price is None:
            raise OddsError("Could not read a price for that market right now.")
        now = self.clock()
        with _LOCK:
            watches = self.load()
            for w in watches:
                if w.market_slug == market.slug and w.outcome == outcome:
                    w.rules = rules
                    self.save(watches)
                    return w
            w = Watch(id=uuid.uuid4().hex[:6], market_slug=market.slug, event_slug=market.event_slug,
                      question=market.question, label=market.label, outcome=outcome, rules=rules,
                      baseline=price, baseline_at=now, last_price=price, last_checked=now,
                      created_at=now, above_armed=rules.above is None or price < rules.above,
                      below_armed=rules.below is None or price > rules.below)
            watches.append(w)
            self.save(watches)
            return w

    def remove_watch(self, key: str) -> Optional["Watch"]:
        key = (key or "").strip()
        with _LOCK:
            watches = self.load()
            for i, w in enumerate(watches):
                if key in (w.id, w.market_slug):
                    gone = watches.pop(i)
                    self.save(watches)
                    return gone
        return None

    def check(self, commit: bool = True) -> tuple[list["Alert"], list[str]]:
        """Evaluate every watch. Returns (alerts, errors). ``commit=False`` peeks."""
        with _LOCK:
            watches = self.load()
            alerts: list[Alert] = []
            errors: list[str] = []
            now = self.clock()
            keep: list[Watch] = []
            for w in watches:
                try:
                    m = self.client.market_by_slug(w.market_slug)
                    if not m:
                        errors.append(f"{w.id}: market '{w.market_slug}' not found")
                        keep.append(w)
                        continue
                    market = Market.from_gamma(m)
                    price = self.live_price(market, w.outcome)
                except (ApiError, OddsError) as exc:
                    errors.append(f"{w.id}: {exc}")
                    keep.append(w)
                    continue
                new_alerts, done = evaluate(w, market, price, now)
                alerts.extend(new_alerts)
                if not done:
                    keep.append(w)
            if commit:
                self.save(keep)
            return alerts, errors


# ------------------------------------------------------------ the engine --
@dataclass
class Rules:
    move: Optional[float] = 5.0    # alert when price moves this many points from the baseline
    above: Optional[float] = None  # alert when price crosses up through this (0-1)
    below: Optional[float] = None  # alert when price crosses down through this (0-1)

    @classmethod
    def build(cls, move=None, above=None, below=None) -> "Rules":
        def frac(v, name):
            if v is None:
                return None
            v = float(v)
            if v > 1:
                v = v / 100.0  # accept 60 or 0.6
            if not 0 < v < 1:
                raise OddsError(f"{name} must be between 0 and 100 (percent)")
            return round(v, 4)
        mv = None if move is None else float(move)
        if mv is not None and not 0 < mv < 100:
            raise OddsError("move must be between 0 and 100 points")
        r = cls(move=mv, above=frac(above, "above"), below=frac(below, "below"))
        if r.move is None and r.above is None and r.below is None:
            raise OddsError("A watch needs at least one rule: move, above or below.")
        if r.above is not None and r.below is not None and r.below >= r.above:
            raise OddsError("below must be lower than above")
        return r

    def describe(self) -> str:
        parts = []
        if self.move is not None:
            parts.append(f"±{self.move:g} pts")
        if self.above is not None:
            parts.append(f"above {_pct(self.above)}")
        if self.below is not None:
            parts.append(f"below {_pct(self.below)}")
        return ", ".join(parts)


@dataclass
class Watch:
    id: str
    market_slug: str
    question: str
    outcome: str
    rules: Rules
    baseline: float
    baseline_at: float
    last_price: float
    last_checked: float
    created_at: float
    event_slug: str = ""
    label: str = ""
    above_armed: bool = True
    below_armed: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "Watch":
        d = dict(d)
        d["rules"] = Rules(**(d.get("rules") or {}))
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def title(self) -> str:
        q = self.question
        return q if self.outcome == "Yes" else f"{q} [{self.outcome}]"


@dataclass
class Alert:
    watch_id: str
    kind: str          # move | above | below | resolved
    question: str
    outcome: str
    old: Optional[float]
    new: Optional[float]
    url: str
    since: Optional[float] = None
    detail: str = ""

    def text(self, now: Optional[float] = None) -> str:
        if self.kind == "resolved":
            return f"✅ RESOLVED · {self.question} → {self.detail}\n   {self.url}"
        delta = (self.new - self.old) * 100 if self.new is not None and self.old is not None else 0.0
        arrow = "▲" if delta >= 0 else "▼"
        head = {"move": f"{arrow} {delta:+.1f} pts",
                "above": f"⬆ crossed above {self.detail}",
                "below": f"⬇ crossed below {self.detail}"}[self.kind]
        ago = f" in {_ago((now or time.time()) - self.since)}" if self.since is not None else ""
        who = "" if self.outcome == "Yes" else f" [{self.outcome}]"
        return (f"{head} · {self.question}{who}\n"
                f"   {_pct(self.old)} → {_pct(self.new)}{ago} · {self.url}")


def evaluate(w: Watch, market: Market, price: Optional[float], now: float) -> tuple[list[Alert], bool]:
    """Pure function: apply a watch's rules to a fresh price. Mutates ``w``.

    Returns (alerts, finished). A resolved market fires once and is dropped.
    """
    alerts: list[Alert] = []
    if market.closed:
        winner = market.resolved_outcome
        detail = f"{winner} won" if winner else "market closed (awaiting resolution)"
        alerts.append(Alert(w.id, "resolved", w.question, w.outcome, w.last_price, price, market.url,
                            detail=detail))
        return alerts, True
    if price is None:
        return alerts, False
    r = w.rules
    if r.move is not None and abs(price - w.baseline) * 100 >= r.move - 1e-9:
        alerts.append(Alert(w.id, "move", w.question, w.outcome, w.baseline, price, market.url, since=w.baseline_at))
        w.baseline, w.baseline_at = price, now
    if r.above is not None:
        if w.above_armed and price >= r.above:
            alerts.append(Alert(w.id, "above", w.question, w.outcome, w.last_price, price, market.url,
                                detail=_pct(r.above)))
            w.above_armed = False
        elif not w.above_armed and price < r.above - REARM_GAP:
            w.above_armed = True
    if r.below is not None:
        if w.below_armed and price <= r.below:
            alerts.append(Alert(w.id, "below", w.question, w.outcome, w.last_price, price, market.url,
                                detail=_pct(r.below)))
            w.below_armed = False
        elif not w.below_armed and price > r.below + REARM_GAP:
            w.below_armed = True
    w.last_price, w.last_checked = price, now
    return alerts, False


# ------------------------------------------------------------- formatting --
_BARS = "▁▂▃▄▅▆▇█"


def spark(values: list[float], width: int = 28) -> str:
    if len(values) < 2:
        return ""
    if len(values) > width:  # downsample evenly
        step = (len(values) - 1) / (width - 1)
        values = [values[round(i * step)] for i in range(width)]
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return _BARS[3] * len(values)
    return "".join(_BARS[min(7, int((v - lo) / (hi - lo) * 7.999))] for v in values)


def _pct(p: Optional[float]) -> str:
    if p is None:
        return "n/a"
    v = p * 100
    return f"{v:.1f}%" if v >= 1 or v == 0 else f"{v:.2f}%"


def _money(v: float) -> str:
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"${v / div:.1f}{unit}"
    return f"${v:.0f}"


def _ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def fmt_market(market: Market, live: Optional[float], sparkline: str = "") -> str:
    lines = [market.question]
    if market.label and market.label not in market.question:
        lines[0] += f" — {market.label}"
    odds = []
    for i, name in enumerate(market.outcomes):
        p = live if (i == 0 and live is not None) else (market.prices[i] if i < len(market.prices) else None)
        odds.append(f"{name} {_pct(p)}")
    lines.append("  " + " · ".join(odds) + ("  (live mid)" if live is not None else ""))
    moves = []
    if market.change_1d is not None:
        moves.append(f"24h {market.change_1d * 100:+.1f} pts")
    if market.change_1w is not None:
        moves.append(f"7d {market.change_1w * 100:+.1f} pts")
    if market.best_bid is not None and market.best_ask is not None:
        moves.append(f"bid/ask {_pct(market.best_bid)}/{_pct(market.best_ask)}")
    if moves:
        lines.append("  " + " · ".join(moves))
    lines.append(f"  vol 24h {_money(market.volume_24h)} · total {_money(market.volume)} · "
                 f"liquidity {_money(market.liquidity)}" + (f" · ends {market.end_date}" if market.end_date else ""))
    if sparkline:
        lines.append(f"  7d {sparkline}")
    lines.append(f"  {market.url}")
    if market.closed:
        lines.append(f"  CLOSED{' · ' + market.resolved_outcome + ' won' if market.resolved_outcome else ''}")
    return "\n".join(lines)


def fmt_search(results: list[dict]) -> str:
    if not results:
        return "No active Polymarket events match that."
    out = []
    for r in results:
        out.append(f"{r['event']}  (24h vol {_money(r['volume_24h'])})")
        for m in r["markets"]:
            p = m["prices"][0] if m["prices"] else None
            name = m["outcomes"][0] if m["outcomes"] else "Yes"
            tag = "" if name == "Yes" else f"{name}: "
            out.append(f"  {_pct(p):>6}  {tag}{m['label']}")
            out.append(f"         slug: {m['slug']}")
        if r["more_markets"]:
            out.append(f"  … {r['more_markets']} more markets in this event")
    return "\n".join(out)


def fmt_watches(watches: list[Watch], now: Optional[float] = None) -> str:
    if not watches:
        return "Watchlist is empty. Add one with: watch <polymarket url or slug> [move=5] [above=60] [below=40]"
    now = now or time.time()
    out = []
    for w in watches:
        out.append(f"[{w.id}] {w.title}")
        out.append(f"   now {_pct(w.last_price)} · baseline {_pct(w.baseline)} ({_ago(now - w.baseline_at)} ago) · "
                   f"rules {w.rules.describe()}")
    return "\n".join(out)


def fmt_movers(markets: list[Market]) -> str:
    if not markets:
        return "No big movers right now."
    out = ["Biggest 24h moves among the most-traded Polymarket markets:"]
    for m in markets:
        name = m.question + (f" — {m.label}" if m.label and m.label not in m.question else "")
        out.append(f"  {(m.change_1d or 0) * 100:+5.1f} pts  {_pct(m.prices[0] if m.prices else None):>6}  {name}")
        out.append(f"           {m.slug} · 24h vol {_money(m.volume_24h)}")
    return "\n".join(out)
