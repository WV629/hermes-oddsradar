"""Tool schemas: what the model reads to decide when to call each tool."""

_MARKET = {"type": "string",
           "description": "A polymarket.com event/market URL or a market slug (as returned by odds_search)."}

ODDS_SEARCH = {
    "name": "odds_search",
    "description": ("Search ACTIVE Polymarket prediction markets and return events with each market's current "
                    "odds (prices are probabilities, 0-1). Use when the user asks what the odds or the market's "
                    "probability of something are (elections, rates, crypto prices, sports, launches). Read-only."),
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "Keywords, e.g. 'fed december cut' or 'bitcoin 150k'."},
        "limit": {"type": "integer", "description": "Max events (1-10, default 5)."},
    }, "required": ["query"]},
}

ODDS_MARKET = {
    "name": "odds_market",
    "description": ("Live detail for one Polymarket market: outcome prices, live CLOB midpoint, 24h and 7d move "
                    "in percentage points, volume, liquidity, end date, resolution status and a 7-day sparkline. "
                    "If an event has several markets the error lists their slugs; pick one and call again."),
    "parameters": {"type": "object", "properties": {
        "market": _MARKET,
        "outcome": {"type": "string", "description": "Outcome to price live (default: the first, usually Yes)."},
    }, "required": ["market"]},
}

ODDS_WATCH = {
    "name": "odds_watch",
    "description": ("Manage the user's persistent Polymarket watchlist (kept across sessions). action=add needs "
                    "market and at least one rule: move_pts (alert when odds move that many percentage points "
                    "from the last alert, default 5), above / below (alert once when odds cross that percent, "
                    "re-armed after a 1-pt pullback). action=remove takes id or market. action=list shows all."),
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["add", "remove", "list"]},
        "market": _MARKET,
        "id": {"type": "string", "description": "Watch id for action=remove."},
        "outcome": {"type": "string", "description": "Outcome to watch (default Yes)."},
        "move_pts": {"type": "number", "description": "Alert on a move of this many points (e.g. 5)."},
        "above": {"type": "number", "description": "Alert when odds rise to this percent (e.g. 60)."},
        "below": {"type": "number", "description": "Alert when odds fall to this percent (e.g. 40)."},
    }, "required": ["action"]},
}

ODDS_CHECK = {
    "name": "odds_check",
    "description": ("Evaluate every watched market now and return the alerts that fired (moves, threshold "
                    "crossings, resolutions). Firing an alert resets that watch's baseline; pass peek=true to "
                    "look without consuming alerts. For recurring alerts, prefer a no-agent cron job running "
                    "`hermes oddsradar check --quiet` (zero tokens per tick) over an LLM cron job."),
    "parameters": {"type": "object", "properties": {
        "peek": {"type": "boolean", "description": "Do not reset baselines or consume alerts."},
    }},
}

ODDS_MOVERS = {
    "name": "odds_movers",
    "description": ("The biggest 24h odds moves among the most-traded active Polymarket markets (min $50K 24h "
                    "volume). Use for 'what is moving on Polymarket today' style questions."),
    "parameters": {"type": "object", "properties": {
        "limit": {"type": "integer", "description": "How many (1-20, default 8)."},
    }},
}

ALL = [ODDS_SEARCH, ODDS_MARKET, ODDS_WATCH, ODDS_CHECK, ODDS_MOVERS]
