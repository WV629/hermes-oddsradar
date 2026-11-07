"""Text commands shared by the ``/odds`` slash command and ``hermes oddsradar``.

Both surfaces call :func:`run` with a token list and print/return its text, so
chat and terminal behave identically. No LLM is involved anywhere here.
"""

from __future__ import annotations

import os
import shlex
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from .api import ApiError
from .core import (OddsError, Radar, _pct, fmt_market, fmt_movers, fmt_search, fmt_watches)

PLUGIN_NAME = "oddsradar"

HELP = """Odds Radar: Polymarket odds in your chat, with zero-token alerts.

  /odds <query>                      search active markets (e.g. /odds fed rates)
  /odds show <url|slug>              live odds, 24h/7d move, volume, 7-day sparkline
  /odds watch <url|slug> [move=5] [above=60] [below=40] [outcome=Yes]
                                     add a market to your watchlist
  /odds list                         show the watchlist
  /odds unwatch <id|slug>            remove a watch
  /odds check                        evaluate every watch now and print alerts
  /odds movers                       biggest 24h moves among the most-traded markets

Read-only: never trades, never needs a key or a wallet."""


def data_dir() -> Path:
    """Per-profile plugin data dir; falls back to an env var outside Hermes (tests)."""
    override = os.environ.get("ODDSRADAR_DATA_DIR")
    if override:
        return Path(override)
    try:
        from plugins.plugin_storage import plugin_data_dir
        return Path(plugin_data_dir(PLUGIN_NAME))
    except ImportError:  # running outside Hermes
        return Path.home() / ".oddsradar"


_radar_factory: Callable[[], Radar] = lambda: Radar(data_dir())


def set_radar_factory(factory: Callable[[], Radar]) -> None:
    """Tests swap in a Radar with an offline client."""
    global _radar_factory
    _radar_factory = factory


def radar() -> Radar:
    return _radar_factory()


def _opts(tokens: list[str]) -> tuple[list[str], dict[str, str]]:
    pos, kw = [], {}
    for t in tokens:
        if "=" in t and not t.startswith("http"):
            k, v = t.split("=", 1)
            kw[k.strip().lower().lstrip("-")] = v.strip()
        else:
            pos.append(t)
    return pos, kw


def _num(kw: dict, key: str, default: Optional[float]) -> Optional[float]:
    if key not in kw:
        return default
    v = kw[key].rstrip("%").lower()
    if v in ("", "off", "none", "no"):
        return None
    try:
        return float(v)
    except ValueError as exc:
        raise OddsError(f"{key}= needs a number, got '{kw[key]}'") from exc


def run(tokens: list[str]) -> str:
    """Execute one command and return the text to show."""
    if not tokens or tokens[0] in ("help", "-h", "--help"):
        return HELP
    cmd, rest = tokens[0].lower(), tokens[1:]
    r = radar()
    try:
        if cmd == "show":
            if not rest:
                return "Usage: show <polymarket url or slug>"
            m = r.resolve(rest[0])
            return fmt_market(m, r.live_price(m), r.sparkline(m))
        if cmd == "watch":
            pos, kw = _opts(rest)
            if not pos:
                return "Usage: watch <polymarket url or slug> [move=5] [above=60] [below=40] [outcome=Yes]"
            has_threshold = "above" in kw or "below" in kw
            w = r.add_watch(pos[0], outcome=kw.get("outcome"),
                            move=_num(kw, "move", None if has_threshold else 5.0),
                            above=_num(kw, "above", None), below=_num(kw, "below", None))
            return (f"Watching [{w.id}] {w.title}\n   now {_pct(w.baseline)} · alert on {w.rules.describe()}\n"
                    "   Run `check` (or schedule `hermes oddsradar cron-script`) to get alerts.")
        if cmd in ("list", "ls", "watches"):
            return fmt_watches(r.load())
        if cmd in ("unwatch", "rm", "remove"):
            if not rest:
                return "Usage: unwatch <watch id or slug>"
            gone = r.remove_watch(rest[0])
            return f"Removed [{gone.id}] {gone.title}" if gone else f"No watch matches '{rest[0]}'."
        if cmd == "check":
            alerts, errors = r.check(commit=True)
            now = time.time()
            lines = [a.text(now) for a in alerts]
            lines += [f"(skipped {e})" for e in errors]
            return "\n".join(lines) if lines else "No alerts: every watched market is inside its rules."
        if cmd == "movers":
            return fmt_movers(r.movers())
        if cmd == "search":
            rest = rest or []
            return fmt_search(r.search(" ".join(rest)))
        return fmt_search(r.search(" ".join(tokens)))  # bare text = search
    except (OddsError, ApiError) as exc:
        return f"Odds Radar: {exc}"


def handle_slash(raw_args: str) -> str:
    try:
        tokens = shlex.split(raw_args or "")
    except ValueError:
        tokens = (raw_args or "").split()
    return run(tokens)


# ------------------------------------------------------------------ CLI ----
CRON_SCRIPT = '''#!/usr/bin/env python3
"""Odds Radar cron check (written by `hermes oddsradar cron-script`).

Runs `hermes oddsradar check --quiet` in this profile. It prints only when a
watched market crossed one of its rules, so a no-agent cron job delivers a
message only then. No LLM call, no tokens.
"""
import os, shutil, subprocess, sys

hermes = shutil.which("hermes")
cmd = [hermes] if hermes else [sys.executable, "-m", "hermes_cli.main"]
out = subprocess.run(cmd + ["oddsradar", "check", "--quiet"], capture_output=True,
                     text=True, timeout=120, stdin=subprocess.DEVNULL)
sys.stdout.write(out.stdout)
'''


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:  # pragma: no cover - outside Hermes
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def setup_cli(parser) -> None:
    subs = parser.add_subparsers(dest="oddsradar_cmd")
    p = subs.add_parser("search", help="Search active Polymarket markets")
    p.add_argument("query", nargs="+")
    p = subs.add_parser("show", help="Live odds, moves, volume and a 7-day sparkline")
    p.add_argument("ref", help="polymarket.com URL or market slug")
    p = subs.add_parser("watch", help="Add a market to the watchlist")
    p.add_argument("ref")
    p.add_argument("--move", type=float, default=None, help="alert on a move of N points (default 5)")
    p.add_argument("--above", type=float, default=None, help="alert when odds cross above N%%")
    p.add_argument("--below", type=float, default=None, help="alert when odds cross below N%%")
    p.add_argument("--outcome", default=None, help="outcome to watch (default: the first, usually Yes)")
    subs.add_parser("list", help="Show the watchlist")
    p = subs.add_parser("unwatch", help="Remove a watch")
    p.add_argument("key", help="watch id or market slug")
    p = subs.add_parser("check", help="Evaluate every watch and print alerts")
    p.add_argument("--quiet", action="store_true", help="print nothing when there are no alerts (for cron)")
    subs.add_parser("movers", help="Biggest 24h moves among the most-traded markets")
    p = subs.add_parser("cron-script", help="Write a no-agent cron script for zero-token alerts")
    p.add_argument("--force", action="store_true", help="overwrite an existing script")
    parser.set_defaults(func=handle_cli)


def handle_cli(args) -> int:
    cmd = getattr(args, "oddsradar_cmd", None)
    if not cmd:
        print(HELP.replace("/odds", "hermes oddsradar"))
        return 0
    if cmd == "cron-script":
        path = _hermes_home() / "scripts" / "oddsradar-check.py"
        if path.exists() and not args.force:
            print(f"{path} already exists (use --force to overwrite).")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(CRON_SCRIPT, encoding="utf-8")
            print(f"Wrote {path}")
        print("Schedule it (no LLM, zero tokens per tick), for example:\n"
              "  hermes cron create \"every 15m\" --no-agent --script oddsradar-check.py "
              "--deliver telegram --name oddsradar")
        return 0
    if cmd == "check" and args.quiet:
        r = radar()
        try:
            alerts, errors = r.check(commit=True)
        except (OddsError, ApiError) as exc:
            print(f"Odds Radar: {exc}", file=sys.stderr)
            return 1
        now = time.time()
        if alerts:
            print("\n".join(a.text(now) for a in alerts))
        for e in errors:
            print(f"oddsradar: skipped {e}", file=sys.stderr)
        return 0
    tokens: list[str] = [cmd]
    if cmd == "search":
        tokens += args.query
    elif cmd == "show":
        tokens.append(args.ref)
    elif cmd == "watch":
        tokens.append(args.ref)
        for k in ("move", "above", "below"):
            v = getattr(args, k)
            if v is not None:
                tokens.append(f"{k}={v:g}")
        if args.outcome:
            tokens.append(f"outcome={args.outcome}")
    elif cmd == "unwatch":
        tokens.append(args.key)
    out = run(tokens)
    try:
        print(out)
    except BrokenPipeError:  # e.g. piped into `head`
        pass
    return 1 if out.startswith("Odds Radar:") else 0
