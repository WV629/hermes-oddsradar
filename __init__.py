"""Odds Radar: Polymarket odds, a persistent watchlist and zero-token alerts for Hermes."""

from __future__ import annotations

from . import commands, schemas, tools

TOOLSET = "oddsradar"

_HANDLERS = {
    "odds_search": tools.odds_search,
    "odds_market": tools.odds_market,
    "odds_watch": tools.odds_watch,
    "odds_check": tools.odds_check,
    "odds_movers": tools.odds_movers,
}


def register(ctx) -> None:
    for schema in schemas.ALL:
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema,
                          handler=_HANDLERS[schema["name"]])
    ctx.register_command("odds", handler=commands.handle_slash,
                         description="Polymarket odds, watchlist and alerts (/odds help)",
                         args_hint="[query | show | watch | list | check | movers]")
    ctx.register_cli_command(name="oddsradar", help="Polymarket odds, watchlist and zero-token alerts",
                             setup_fn=commands.setup_cli, handler_fn=commands.handle_cli)
