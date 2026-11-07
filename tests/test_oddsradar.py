"""Offline tests: a fake transport stands in for Polymarket, so no network is used."""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_pkg():
    """Import the plugin directory as a package named ``oddsradar`` (no Hermes needed)."""
    if "oddsradar" in sys.modules:
        return sys.modules["oddsradar"]
    spec = importlib.util.spec_from_file_location("oddsradar", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["oddsradar"] = mod
    spec.loader.exec_module(mod)
    return mod


pkg = _load_pkg()
from oddsradar import api, commands, core, schemas, tools  # noqa: E402


def market(slug="fed-hike", q="Another Fed rate hike in 2026?", yes=0.60, closed=False, label="",
           event=None, tokens=("111", "222"), d1=0.02):
    return {"slug": slug, "question": q, "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps([str(yes), str(round(1 - yes, 4))]),
            "clobTokenIds": json.dumps(list(tokens)), "closed": closed, "active": not closed,
            "groupItemTitle": label, "endDate": "2026-12-10T00:00:00Z", "volume24hr": 120000,
            "volumeNum": 1600000, "liquidityNum": 98000, "oneDayPriceChange": d1, "oneWeekPriceChange": 0.05,
            "bestBid": yes - 0.005, "bestAsk": yes + 0.005, "events": [event] if event else []}


class FakeAPI:
    """Routes URLs to in-memory fixtures and records every call."""

    def __init__(self):
        self.markets: dict[str, dict] = {}
        self.events: dict[str, dict] = {}
        self.mids: dict[str, float] = {}
        self.history = [{"t": i, "p": 0.5 + i / 100} for i in range(40)]
        self.calls: list[str] = []
        self.fail_next = 0

    def __call__(self, url: str, timeout: float):
        self.calls.append(url)
        if self.fail_next:
            self.fail_next -= 1
            raise urllib.error.URLError("boom")
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/markets" and "slug" in q:
            m = self.markets.get(q["slug"])
            return [m] if m else []
        if u.path == "/markets":
            return list(self.markets.values())
        if u.path == "/events":
            e = self.events.get(q["slug"])
            return [e] if e else []
        if u.path == "/public-search":
            return {"events": list(self.events.values())}
        if u.path == "/midpoint":
            mid = self.mids.get(q["token_id"])
            return {"mid": str(mid)} if mid is not None else {"error": "no orderbook"}
        if u.path == "/prices-history":
            return {"history": self.history}
        raise AssertionError(f"unexpected url {url}")


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture()
def env(tmp_path):
    fake, clock = FakeAPI(), Clock()
    client = api.PolymarketClient(transport=fake, backoff=0)
    radar = core.Radar(tmp_path, client=client, clock=clock)
    commands.set_radar_factory(lambda: radar)
    fake.markets["fed-hike"] = market()
    fake.mids["111"] = 0.60
    yield fake, clock, radar


# ------------------------------------------------------------------ parsing
def test_parse_ref_urls_and_slugs():
    assert core.parse_ref("https://polymarket.com/event/fed/fed-hike") == ("fed", "fed-hike")
    assert core.parse_ref("polymarket.com/event/fed") == ("fed", None)
    assert core.parse_ref("https://polymarket.com/market/fed-hike") == (None, "fed-hike")
    assert core.parse_ref("fed-hike") == (None, "fed-hike")
    for bad in ("", "https://evil.com/event/x", "DROP TABLE", "https://polymarket.com/profile/x"):
        with pytest.raises(core.OddsError):
            core.parse_ref(bad)


def test_market_parses_double_encoded_fields():
    m = core.Market.from_gamma(market(yes=0.25))
    assert m.outcomes == ["Yes", "No"] and m.prices == [0.25, 0.75] and m.token_ids == ["111", "222"]
    assert m.price("no") == 0.75
    with pytest.raises(core.OddsError):
        m.price("Maybe")


def test_resolved_outcome_only_when_closed_and_decisive():
    assert core.Market.from_gamma(market(yes=1.0, closed=True)).resolved_outcome == "Yes"
    assert core.Market.from_gamma(market(yes=0.0, closed=True)).resolved_outcome == "No"
    assert core.Market.from_gamma(market(yes=0.5, closed=True)).resolved_outcome is None
    assert core.Market.from_gamma(market(yes=1.0)).resolved_outcome is None


# ------------------------------------------------------------------- engine
def _watch(**rules):
    r = core.Rules.build(**rules)
    return core.Watch(id="w1", market_slug="fed-hike", question="Q?", outcome="Yes", rules=r, baseline=0.60,
                      baseline_at=0, last_price=0.60, last_checked=0, created_at=0,
                      above_armed=r.above is None or 0.60 < r.above,
                      below_armed=r.below is None or 0.60 > r.below)


def test_move_rule_fires_at_threshold_and_rebaselines():
    w, m = _watch(move=5), core.Market.from_gamma(market())
    assert core.evaluate(w, m, 0.64, 10) == ([], False)
    alerts, _ = core.evaluate(w, m, 0.65, 20)
    assert [a.kind for a in alerts] == ["move"] and w.baseline == 0.65 and w.baseline_at == 20
    assert core.evaluate(w, m, 0.66, 30)[0] == []          # measured from the new baseline
    assert [a.kind for a in core.evaluate(w, m, 0.59, 40)[0]] == ["move"]


def test_threshold_is_edge_triggered_with_hysteresis():
    w, m = _watch(move=None, above=70), core.Market.from_gamma(market())
    assert [a.kind for a in core.evaluate(w, m, 0.71, 1)[0]] == ["above"]
    assert core.evaluate(w, m, 0.75, 2)[0] == []           # stays above: no repeat
    assert core.evaluate(w, m, 0.695, 3)[0] == []          # within 1 pt: not re-armed
    assert core.evaluate(w, m, 0.70, 4)[0] == []
    core.evaluate(w, m, 0.68, 5)                           # pulled back > 1 pt: re-armed
    assert [a.kind for a in core.evaluate(w, m, 0.70, 6)[0]] == ["above"]


def test_below_rule():
    w, m = _watch(move=None, below=40), core.Market.from_gamma(market())
    assert [a.kind for a in core.evaluate(w, m, 0.40, 1)[0]] == ["below"]
    assert core.evaluate(w, m, 0.30, 2)[0] == []


def test_resolution_fires_once_and_finishes():
    w = _watch(move=5)
    alerts, done = core.evaluate(w, core.Market.from_gamma(market(yes=1.0, closed=True)), 1.0, 1)
    assert done and alerts[0].kind == "resolved" and "Yes won" in alerts[0].text()


def test_rules_validation():
    assert core.Rules.build(move=None, above=60).above == 0.6
    assert core.Rules.build(move=None, above=0.6).above == 0.6
    for bad in ({"move": None}, {"move": 0}, {"move": None, "above": 150}, {"move": None, "above": 40, "below": 60}):
        with pytest.raises(core.OddsError):
            core.Rules.build(**bad)


def test_alert_text():
    a = core.Alert("w1", "move", "Q?", "Yes", 0.60, 0.672, "https://polymarket.com/event/q", since=0)
    t = a.text(now=3600 * 3)
    assert "▲ +7.2 pts" in t and "60.0% → 67.2%" in t and "in 3h" in t
    b = core.Alert("w1", "move", "Q?", "No", 0.60, 0.50, "u", since=None)
    assert "▼ -10.0 pts" in b.text() and "[No]" in b.text()


def test_spark():
    assert core.spark([1.0]) == ""
    assert core.spark([0.1, 0.1]) == "▄▄"
    s = core.spark([i / 10 for i in range(100)], width=20)
    assert len(s) == 20 and s[0] == "▁" and s[-1] == "█"


# ---------------------------------------------------------------- watchlist
def test_watch_check_cycle_persists(env, tmp_path):
    fake, clock, radar = env
    w = radar.add_watch("fed-hike", move=5)
    assert w.baseline == 0.60
    saved = json.loads((tmp_path / "watchlist.json").read_text())
    assert saved["watches"][0]["market_slug"] == "fed-hike"
    fake.mids["111"] = 0.62
    assert radar.check() == ([], [])
    clock.t += 7200
    fake.mids["111"] = 0.671
    alerts, errors = radar.check()
    assert errors == [] and len(alerts) == 1 and alerts[0].new == 0.671
    assert radar.load()[0].baseline == 0.671             # committed


def test_peek_does_not_consume(env):
    fake, clock, radar = env
    radar.add_watch("fed-hike", move=5)
    fake.mids["111"] = 0.70
    assert len(radar.check(commit=False)[0]) == 1
    assert len(radar.check(commit=False)[0]) == 1
    assert len(radar.check(commit=True)[0]) == 1
    assert radar.check()[0] == []


def test_duplicate_watch_updates_rules(env):
    _, _, radar = env
    a = radar.add_watch("fed-hike", move=5)
    b = radar.add_watch("fed-hike", move=2)
    assert a.id == b.id and len(radar.load()) == 1 and radar.load()[0].rules.move == 2


def test_resolved_watch_is_dropped(env):
    fake, _, radar = env
    radar.add_watch("fed-hike", move=5)
    fake.markets["fed-hike"] = market(yes=1.0, closed=True)
    alerts, _ = radar.check()
    assert alerts[0].kind == "resolved" and radar.load() == []


def test_api_outage_keeps_watch_and_reports(env):
    fake, _, radar = env
    radar.add_watch("fed-hike", move=5)
    fake.fail_next = 99
    alerts, errors = radar.check()
    assert alerts == [] and errors and "unreachable" in errors[0]
    assert len(radar.load()) == 1


def test_midpoint_falls_back_to_gamma_price(env):
    fake, _, radar = env
    del fake.mids["111"]
    m = radar.resolve("fed-hike")
    assert radar.live_price(m) == 0.60


def test_event_with_many_markets_lists_choices(env):
    fake, _, radar = env
    ev = {"slug": "dem-2028", "title": "Democratic Nominee 2028",
          "markets": [market(slug=f"cand-{i}", label=f"Cand {i}", yes=0.1 * i) for i in range(1, 4)]}
    fake.events["dem-2028"] = ev
    with pytest.raises(core.OddsError) as exc:
        radar.resolve("https://polymarket.com/event/dem-2028")
    assert "3 markets" in str(exc.value) and "cand-3" in str(exc.value)
    fake.events["solo"] = {"slug": "solo", "title": "Solo", "markets": [market(slug="solo-m")]}
    assert radar.resolve("solo").slug == "solo-m"


def test_corrupt_watchlist_is_reported_not_overwritten(env, tmp_path):
    _, _, radar = env
    (tmp_path / "watchlist.json").write_text("{not json")
    with pytest.raises(core.OddsError):
        radar.load()
    assert (tmp_path / "watchlist.json").read_text() == "{not json"


# --------------------------------------------------------------- client/api
def test_client_refuses_other_hosts():
    c = api.PolymarketClient(transport=lambda u, t: {}, backoff=0)
    with pytest.raises(api.ApiError):
        c.get("https://evil.example", "/x")


def test_client_retries_then_succeeds():
    seen = []

    def flaky(url, timeout):
        seen.append(url)
        if len(seen) < 3:
            raise urllib.error.HTTPError(url, 503, "busy", None, None)
        return [1]

    assert api.PolymarketClient(transport=flaky, backoff=0).get(api.GAMMA, "/markets") == [1]
    assert len(seen) == 3


def test_client_does_not_retry_4xx():
    seen = []

    def bad(url, timeout):
        seen.append(url)
        raise urllib.error.HTTPError(url, 404, "nope", None, None)

    with pytest.raises(api.ApiError):
        api.PolymarketClient(transport=bad, backoff=0).get(api.GAMMA, "/markets")
    assert len(seen) == 1


# ------------------------------------------------------------ surfaces
def test_slash_command_flow(env):
    fake, _, _ = env
    assert "Odds Radar" in commands.handle_slash("help")
    out = commands.handle_slash("watch https://polymarket.com/event/fed-hike above=70")
    assert "Watching" in out and "above 70.0%" in out and "±" not in out
    assert "fed rate hike" in commands.handle_slash("list").lower()
    fake.mids["111"] = 0.72
    assert "crossed above 70.0%" in commands.handle_slash("check")
    assert "No alerts" in commands.handle_slash("check")
    wid = json.loads(tools.odds_watch({"action": "list"}))["watches"][0]["id"]
    assert "Removed" in commands.handle_slash(f"unwatch {wid}")
    assert "Odds Radar:" in commands.handle_slash("show not a slug!")


def test_tools_return_json_and_never_raise(env):
    fake, _, _ = env
    fake.events["fed"] = {"slug": "fed", "title": "Fed", "volume24hr": 5, "markets": [market()]}
    assert json.loads(tools.odds_search({"query": "fed"}))["results"][0]["event"] == "Fed"
    detail = json.loads(tools.odds_market({"market": "fed-hike"}))
    assert detail["live_price"] == {"Yes": 0.6} and detail["sparkline_7d"]
    assert "error" in json.loads(tools.odds_market({"market": "https://evil.com/x"}))
    added = json.loads(tools.odds_watch({"action": "add", "market": "fed-hike"}))
    assert added["added"]["rules"] == "±5 pts"
    assert json.loads(tools.odds_check({"peek": True}))["committed"] is False
    assert "error" in json.loads(tools.odds_watch({"action": "explode"}))
    assert isinstance(json.loads(tools.odds_movers({}))["movers"], list)


def test_schemas_match_registration():
    class Ctx:
        def __init__(self):
            self.tools, self.commands, self.cli = [], [], []

        def register_tool(self, name, toolset, schema, handler):
            assert schema["name"] == name and callable(handler)
            self.tools.append(name)

        def register_command(self, name, handler, description="", args_hint=""):
            self.commands.append(name)

        def register_cli_command(self, name, help, setup_fn, handler_fn):
            self.cli.append(name)

    ctx = Ctx()
    pkg.register(ctx)
    manifest = (ROOT / "plugin.yaml").read_text()
    for name in ctx.tools:
        assert f"- {name}" in manifest
    assert len(ctx.tools) == len(schemas.ALL) == 5
    assert ctx.commands == ["odds"] and ctx.cli == ["oddsradar"]


def test_non_binary_outcomes_default_to_first(env):
    fake, _, radar = env
    m = market(slug="lee-tot-ou")
    m["outcomes"] = json.dumps(["Over", "Under"])
    fake.markets["lee-tot-ou"] = m
    w = radar.add_watch("lee-tot-ou", move=2)
    assert w.outcome == "Over" and w.title.endswith("[Over]")
    w2 = radar.add_watch("lee-tot-ou", outcome="under", move=2)
    assert w2.outcome == "Under" and w2.baseline == pytest.approx(0.4)
    with pytest.raises(core.OddsError):
        radar.add_watch("lee-tot-ou", outcome="Yes", move=2)
