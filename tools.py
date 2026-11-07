"""Agent tools. Every handler returns a JSON string and never raises."""

from __future__ import annotations

import json
import time

from .api import ApiError
from .commands import radar
from .core import OddsError, Watch


def _err(exc: Exception) -> str:
    return json.dumps({"error": str(exc)})


def _watch_json(w: Watch) -> dict:
    return {"id": w.id, "market_slug": w.market_slug, "question": w.question, "outcome": w.outcome,
            "price": w.last_price, "baseline": w.baseline, "rules": w.rules.describe()}


def odds_search(args: dict, **kwargs) -> str:
    try:
        limit = max(1, min(int(args.get("limit") or 5), 10))
        return json.dumps({"results": radar().search(str(args.get("query") or ""), limit=limit)})
    except (OddsError, ApiError, ValueError) as exc:
        return _err(exc)


def odds_market(args: dict, **kwargs) -> str:
    try:
        r = radar()
        m = r.resolve(str(args.get("market") or ""))
        outcome = str(args.get("outcome") or m.outcomes[0])
        live = r.live_price(m, outcome)
        return json.dumps({
            "question": m.question, "label": m.label, "url": m.url, "slug": m.slug,
            "outcomes": dict(zip(m.outcomes, m.prices)), "live_price": {outcome: live},
            "change_24h_pts": None if m.change_1d is None else round(m.change_1d * 100, 2),
            "change_7d_pts": None if m.change_1w is None else round(m.change_1w * 100, 2),
            "volume_24h_usd": round(m.volume_24h, 2), "volume_usd": round(m.volume, 2),
            "liquidity_usd": round(m.liquidity, 2), "end_date": m.end_date,
            "closed": m.closed, "resolved_outcome": m.resolved_outcome,
            "sparkline_7d": r.sparkline(m, outcome),
        })
    except (OddsError, ApiError) as exc:
        return _err(exc)


def odds_watch(args: dict, **kwargs) -> str:
    try:
        r = radar()
        action = str(args.get("action") or "list").lower()
        if action == "list":
            return json.dumps({"watches": [_watch_json(w) for w in r.load()]})
        if action == "remove":
            gone = r.remove_watch(str(args.get("id") or args.get("market") or ""))
            return json.dumps({"removed": _watch_json(gone) if gone else None})
        if action == "add":
            has_threshold = args.get("above") is not None or args.get("below") is not None
            move = args.get("move_pts")
            if move is None and not has_threshold:
                move = 5.0
            w = r.add_watch(str(args.get("market") or ""), outcome=args.get("outcome") or None,
                            move=move, above=args.get("above"), below=args.get("below"))
            return json.dumps({"added": _watch_json(w),
                               "hint": "Alerts are produced by odds_check, or for free by a no-agent cron "
                                       "job running `hermes oddsradar check --quiet` "
                                       "(set up with `hermes oddsradar cron-script`)."})
        return _err(OddsError("action must be add, remove or list"))
    except (OddsError, ApiError, ValueError, TypeError) as exc:
        return _err(exc)


def odds_check(args: dict, **kwargs) -> str:
    try:
        commit = not bool(args.get("peek"))
        alerts, errors = radar().check(commit=commit)
        now = time.time()
        return json.dumps({"alerts": [{"kind": a.kind, "question": a.question, "outcome": a.outcome,
                                       "from": a.old, "to": a.new, "url": a.url, "text": a.text(now)}
                                      for a in alerts],
                           "errors": errors, "committed": commit})
    except (OddsError, ApiError) as exc:
        return _err(exc)


def odds_movers(args: dict, **kwargs) -> str:
    try:
        limit = max(1, min(int(args.get("limit") or 8), 20))
        return json.dumps({"movers": [{
            "question": m.question, "label": m.label, "slug": m.slug, "price": m.prices[0] if m.prices else None,
            "change_24h_pts": round((m.change_1d or 0) * 100, 2), "volume_24h_usd": round(m.volume_24h, 2),
            "url": m.url} for m in radar().movers(limit=limit)]})
    except (OddsError, ApiError, ValueError) as exc:
        return _err(exc)
