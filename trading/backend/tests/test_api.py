"""API tests via FastAPI's TestClient.

The app is mounted against a runtime built on the simulated broker, so these
exercise the real routers and the real serialisation without a network call.

One rule gets special attention: **there must be no HTTP path to live
trading.** Several tests below assert that explicitly.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import api_router, ws_router
from app.runtime import AtlasRuntime


@pytest.fixture
def client(runtime: AtlasRuntime) -> Iterator[TestClient]:
    """A test client wired to the already-started simulated runtime."""
    app = FastAPI(title="ATLAS test")
    app.include_router(api_router)
    app.include_router(ws_router)
    app.state.runtime = runtime

    with TestClient(app) as test_client:
        yield test_client


def _orders(client: TestClient) -> list[dict]:
    """All orders currently known to the broker."""
    return client.get("/api/orders?status=all").json()["orders"]


# --------------------------------------------------------------------------- #
# system
# --------------------------------------------------------------------------- #


def test_status_endpoint(client: TestClient) -> None:
    response = client.get("/api/system/status")
    assert response.status_code == 200

    body = response.json()
    assert body["mode"]["effective"] == "paper"
    assert body["mode"]["is_live"] is False
    assert body["broker"]["simulated"] is True
    assert "kill_switch" in body
    assert "event_bus" in body
    assert "agents" in body


def test_health_endpoint_reports_components(client: TestClient) -> None:
    response = client.get("/api/system/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] in {"ok", "degraded"}
    assert body["components"]["broker"] is True
    assert body["components"]["database"] is True
    assert body["components"]["event_bus"] is True


def test_health_returns_200_even_when_degraded(client: TestClient, runtime: AtlasRuntime) -> None:
    """The dashboard must be able to *display* a problem.

    A health endpoint that 500s when something is wrong is useless for that.
    """

    async def broken_ping() -> bool:
        return False

    runtime.broker.ping = broken_ping  # type: ignore[method-assign]

    response = client.get("/api/system/health")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


def test_mode_endpoint_explains_the_gates(client: TestClient) -> None:
    body = client.get("/api/system/mode").json()

    assert body["effective"] == "paper"
    assert set(body["live_gates"]) == {
        "mode_is_live",
        "live_trading_enabled",
        "manual_confirmation",
    }
    assert "no way to switch to live mode from this API" in body["explanation"]


def test_capabilities_endpoint_lists_unsupported(client: TestClient) -> None:
    body = client.get("/api/system/capabilities").json()

    assert body["capabilities"]["broker_name"] == "simulated"
    assert "Crypto trading" in body["unsupported"]


def test_logs_endpoint(client: TestClient) -> None:
    body = client.get("/api/system/logs?limit=10").json()
    assert isinstance(body["logs"], list)


def test_events_endpoint_supports_a_pattern(client: TestClient) -> None:
    body = client.get("/api/system/events?pattern=system.*&limit=10").json()

    assert isinstance(body["events"], list)
    assert all(e["topic"].startswith("system.") for e in body["events"])


# --------------------------------------------------------------------------- #
# there is no HTTP path to live trading
# --------------------------------------------------------------------------- #


def test_no_endpoint_can_switch_to_live(client: TestClient) -> None:
    """Live mode must require editing .env and restarting. Nothing else."""
    for path in (
        "/api/system/mode",
        "/api/system/mode/live",
        "/api/system/trading-mode",
        "/api/system/live",
    ):
        response = client.post(path, json={"mode": "live"})
        assert response.status_code in (404, 405), (
            f"{path} accepted a POST; there must be no API route to live trading"
        )


def test_openapi_exposes_no_live_switch(client: TestClient) -> None:
    spec = client.get("/openapi.json")
    # The test app does not serve /openapi.json unless FastAPI adds it; when
    # it does, assert no path suggests enabling live trading.
    if spec.status_code != 200:
        pytest.skip("openapi not served by the test app")

    for path, methods in spec.json()["paths"].items():
        for method in methods:
            if method.lower() in {"post", "put", "patch"}:
                assert "live" not in path.lower(), (
                    f"{method.upper()} {path} looks like a live switch"
                )


# --------------------------------------------------------------------------- #
# kill switch
# --------------------------------------------------------------------------- #


def test_kill_switch_engage_and_release(client: TestClient, runtime: AtlasRuntime) -> None:
    assert client.get("/api/system/kill-switch").json()["engaged"] is False

    engaged = client.post("/api/system/kill-switch/engage", json={"reason": "api test"}).json()
    assert engaged["engaged"] is True
    assert runtime.kill_switch.is_engaged is True

    released = client.post("/api/system/kill-switch/release").json()
    assert released["engaged"] is False
    assert runtime.kill_switch.is_engaged is False


def test_kill_switch_engage_is_idempotent_over_http(client: TestClient) -> None:
    client.post("/api/system/kill-switch/engage", json={"reason": "first"})
    second = client.post("/api/system/kill-switch/engage", json={"reason": "second"})

    assert second.status_code == 200
    assert second.json()["engaged"] is True


# --------------------------------------------------------------------------- #
# account and portfolio
# --------------------------------------------------------------------------- #


def test_account_endpoint(client: TestClient) -> None:
    body = client.get("/api/account").json()

    assert body["account"]["equity"] == 100_000.0
    assert body["simulated"] is True
    assert body["mode"] == "paper"


def test_account_response_contains_no_credentials(client: TestClient) -> None:
    """Brokerage secrets must never reach frontend JavaScript."""
    for path in ("/api/account", "/api/system/status", "/api/system/capabilities"):
        text = client.get(path).text.lower()
        for forbidden in ("alpaca_api_key", "alpaca_secret_key", "secret_key"):
            assert forbidden not in text, f"{path} leaked {forbidden}"


def test_positions_endpoint(client: TestClient) -> None:
    body = client.get("/api/positions").json()

    assert body["count"] == 0
    assert body["positions"] == []


def test_orders_endpoint(client: TestClient) -> None:
    body = client.get("/api/orders?status=all").json()

    assert isinstance(body["orders"], list)
    assert "tracker" in body


def test_orders_endpoint_validates_status(client: TestClient) -> None:
    assert client.get("/api/orders?status=banana").status_code == 422


def test_portfolio_endpoint(client: TestClient) -> None:
    body = client.get("/api/portfolio").json()

    assert body["portfolio"]["equity"] == 100_000.0
    assert body["source"] in {"portfolio_agent", "direct_broker_read"}


def test_equity_curve_endpoint(client: TestClient) -> None:
    body = client.get("/api/portfolio/equity-curve").json()
    assert isinstance(body["points"], list)


# --------------------------------------------------------------------------- #
# market data
# --------------------------------------------------------------------------- #


def test_market_clock_endpoint(client: TestClient) -> None:
    body = client.get("/api/market/clock").json()

    assert body["clock"]["is_open"] is True
    assert body["status"] == "open"


def test_bars_endpoint(client: TestClient) -> None:
    body = client.get("/api/market/bars/spy?timeframe=5Min&limit=20").json()

    assert body["symbol"] == "SPY", "the symbol should be normalised to upper case"
    assert body["count"] > 0
    assert len(body["bars"]) == body["count"]


def test_bars_endpoint_rejects_a_bad_timeframe(client: TestClient) -> None:
    assert client.get("/api/market/bars/SPY?timeframe=banana").status_code == 400


def test_quote_endpoint_reports_staleness(client: TestClient) -> None:
    body = client.get("/api/market/quote/SPY").json()

    assert body["quote"]["symbol"] == "SPY"
    assert body["stale"] is False
    assert body["age_seconds"] < 10


def test_indicators_endpoint(client: TestClient) -> None:
    body = client.get("/api/market/indicators/SPY?timeframe=5Min").json()

    assert body["indicators"]["symbol"] == "SPY"
    assert body["indicators"]["bar_count"] > 0


def test_snapshots_endpoint(client: TestClient) -> None:
    body = client.get("/api/market/snapshots?symbols=SPY,QQQ").json()
    assert set(body["snapshots"]) == {"SPY", "QQQ"}


def test_snapshots_endpoint_requires_symbols(client: TestClient) -> None:
    assert client.get("/api/market/snapshots?symbols=").status_code in (400, 422)


def test_watchlist_endpoint(client: TestClient) -> None:
    body = client.get("/api/market/watchlist").json()

    assert body["universe"] == "core_watchlist"
    assert len(body["rows"]) == len(body["symbols"])


# --------------------------------------------------------------------------- #
# agents
# --------------------------------------------------------------------------- #


def test_agents_list(client: TestClient) -> None:
    body = client.get("/api/agents").json()

    ids = {a["id"] for a in body["agents"]}
    for expected in ("orchestrator", "market_data", "portfolio", "risk", "execution"):
        assert expected in ids
    assert "health" in body


def test_agent_detail_includes_domain_information(client: TestClient) -> None:
    body = client.get("/api/agents/risk").json()

    assert body["agent"]["id"] == "risk"
    assert body["agent"]["role"]
    assert "detail" in body
    assert "kill_switch" in body["detail"]


def test_unknown_agent_is_404(client: TestClient) -> None:
    assert client.get("/api/agents/nope").status_code == 404


def test_agent_graph(client: TestClient) -> None:
    body = client.get("/api/agents/graph").json()

    node_ids = {n["id"] for n in body["nodes"]}
    assert "broker" in node_ids
    assert any(e["source"] == "risk" and e["target"] == "execution" for e in body["edges"])


def test_agent_start_and_stop(client: TestClient) -> None:
    assert client.post("/api/agents/scanner/start").status_code == 200
    assert client.post("/api/agents/scanner/stop").status_code == 200


def test_protected_agents_cannot_be_stopped_over_http(client: TestClient) -> None:
    """Stopping risk would leave execution with no veto authority."""
    for agent_id in ("risk", "execution"):
        response = client.post(f"/api/agents/{agent_id}/stop")
        assert response.status_code == 403, f"{agent_id} must be protected"
        assert "protected" in response.json()["detail"]


def test_briefing_endpoint(client: TestClient) -> None:
    body = client.get("/api/agents/briefing/summary").json()

    assert body["mode"] == "paper"
    assert "agents" in body
    assert "kill_switch" in body


# --------------------------------------------------------------------------- #
# scanner
# --------------------------------------------------------------------------- #


def test_scanner_run_and_results(client: TestClient) -> None:
    client.post("/api/agents/scanner/start")

    run = client.post("/api/scanner/run").json()
    assert run["ranked"] > 0

    results = client.get("/api/scanner/results").json()
    assert results["results"]
    assert results["results"][0]["rank"] == 1
    # The disclaimer matters: the score is relative, not an absolute quality.
    assert "relative" in results["disclaimer"].lower()


def test_scanner_universes_endpoint(client: TestClient) -> None:
    body = client.get("/api/scanner/universes").json()

    assert body["active"] == "core_watchlist"
    assert "core_watchlist" in body["universes"]
    assert body["streaming_limit"] == 30, "free plan websocket budget"


# --------------------------------------------------------------------------- #
# strategies
# --------------------------------------------------------------------------- #


def test_strategies_list_warns(client: TestClient) -> None:
    body = client.get("/api/strategies").json()

    assert body["globally_enabled"] is False
    assert body["loaded"] >= 1
    assert "backtested" in body["warning"]


def test_strategy_enable_is_session_only(client: TestClient) -> None:
    """Enabling from the UI must not be written back to strategies.yaml."""
    body = client.post("/api/strategies/ema_vwap_momentum_v1/enable").json()

    assert body["strategy"]["enabled"] is True
    assert body["persisted"] is False
    # And it still cannot run, because the global brake is file-only.
    assert any("globally_enabled" in note for note in body["notes"])


def test_strategy_disable(client: TestClient) -> None:
    client.post("/api/strategies/ema_vwap_momentum_v1/enable")
    body = client.post("/api/strategies/ema_vwap_momentum_v1/disable").json()

    assert body["strategy"]["enabled"] is False


def test_unknown_strategy_is_404(client: TestClient) -> None:
    assert client.post("/api/strategies/nope/enable").status_code == 404


# --------------------------------------------------------------------------- #
# risk
# --------------------------------------------------------------------------- #


def test_risk_status_exposes_limits_and_ceilings(client: TestClient) -> None:
    body = client.get("/api/risk/status").json()

    assert body["config"]["max_risk_per_trade_pct"] > 0
    # The compiled-in ceilings config cannot exceed.
    assert body["hard_ceilings"]["max_risk_per_trade_pct"] == 5.0
    assert body["hard_ceilings"]["max_leverage"] == 2.0


def test_risk_check_is_read_only(client: TestClient, runtime: AtlasRuntime) -> None:
    """The learning endpoint must never place an order."""
    before = len(_orders(client))

    response = client.post(
        "/api/risk/check",
        json={
            "symbol": "SPY",
            "side": "buy",
            "quantity": 1,
            "entry_price": 585.0,
            "stop_price": 580.0,
            "target_price": 600.0,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert "decision" in body
    assert "No order was created or submitted" in body["note"]
    assert len(_orders(client)) == before, "the check must not place an order"
    assert runtime.order_tracker.orders_today == 0


def test_risk_check_rejects_an_incoherent_proposal(client: TestClient) -> None:
    """A stop above entry on a long is refused before any rule runs."""
    response = client.post(
        "/api/risk/check",
        json={
            "symbol": "SPY",
            "side": "buy",
            "quantity": 1,
            "entry_price": 580.0,
            "stop_price": 585.0,
        },
    )

    assert response.status_code == 422
    assert "not internally coherent" in response.json()["detail"]


def test_risk_check_explains_a_rejection(client: TestClient) -> None:
    """An oversized trade must come back with the rule that blocked it."""
    body = client.post(
        "/api/risk/check",
        json={
            "symbol": "SPY",
            "side": "buy",
            "quantity": 100000,
            "entry_price": 585.0,
            "stop_price": 580.0,
            "target_price": 600.0,
        },
    ).json()

    decision = body["decision"]
    assert decision["decision"] == "rejected"
    assert decision["reasons"]
    assert any("max_risk_per_trade" in rule for rule in decision["failed_rules"])


def test_risk_decisions_and_events(client: TestClient) -> None:
    assert isinstance(client.get("/api/risk/decisions").json()["decisions"], list)
    assert isinstance(client.get("/api/risk/events").json()["events"], list)


# --------------------------------------------------------------------------- #
# journal
# --------------------------------------------------------------------------- #


def test_journal_endpoint(client: TestClient) -> None:
    body = client.get("/api/journal?limit=10").json()
    assert isinstance(body["entries"], list)


def test_journal_search(client: TestClient) -> None:
    assert client.get("/api/journal?search=risk&symbol=SPY").status_code == 200


def test_mentor_explanations_endpoint(client: TestClient) -> None:
    assert isinstance(client.get("/api/mentor/explanations").json()["explanations"], list)


def test_proposals_endpoint(client: TestClient) -> None:
    assert isinstance(client.get("/api/proposals").json()["proposals"], list)


def test_unknown_trace_is_404(client: TestClient) -> None:
    response = client.get("/api/trace/does-not-exist")

    assert response.status_code == 404
    assert "no records for trace" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# websocket
# --------------------------------------------------------------------------- #


def test_websocket_sends_a_snapshot_first(client: TestClient) -> None:
    """The UI must paint immediately, not wait for the first event."""
    with client.websocket_connect("/ws/events") as websocket:
        message = websocket.receive_json()

        assert message["type"] == "snapshot"
        assert message["data"]["mode"]["effective"] == "paper"


def test_websocket_streams_events(client: TestClient) -> None:
    """An event published inside the app must reach the socket.

    TestClient runs the app on its own event loop in a worker thread, so the
    event is triggered through an HTTP call rather than by publishing on the
    bus from this thread.
    """
    with client.websocket_connect("/ws/events") as websocket:
        assert websocket.receive_json()["type"] == "snapshot"

        client.post("/api/system/kill-switch/engage", json={"reason": "ws test"})

        message = websocket.receive_json()
        assert message["type"] == "event"
        assert message["topic"].startswith(("system.", "risk."))
