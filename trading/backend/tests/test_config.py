"""Configuration and trading-mode tests.

The mode tests matter most: they are what guarantee ATLAS cannot reach a live
account by accident.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config.loader import ConfigError, load_config
from app.config.settings import (
    LIVE_CONFIRMATION_PHRASE,
    Settings,
    StockDataFeed,
    TradingMode,
)


def _settings(**env: str) -> Settings:
    """Build Settings from explicit values, ignoring any .env on disk."""
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# trading mode: the safety gate
# --------------------------------------------------------------------------- #


def test_default_mode_is_paper() -> None:
    assert _settings().effective_mode is TradingMode.PAPER


def test_unknown_mode_falls_back_to_paper() -> None:
    """A typo must never be interpreted generously."""
    for value in ("LIVE_TRADING", "prod", "real", "", "liv", "live ", "LIVE!"):
        settings = _settings(ATLAS_TRADING_MODE=value)
        assert settings.effective_mode is TradingMode.PAPER, f"{value!r} must not go live"


def test_live_requires_all_three_gates() -> None:
    """Each gate alone, and each pair, must be insufficient."""
    base = {
        "ATLAS_TRADING_MODE": "live",
        "ATLAS_LIVE_TRADING_ENABLED": "true",
        "ATLAS_MANUAL_LIVE_CONFIRMATION": LIVE_CONFIRMATION_PHRASE,
    }

    # All three: live is granted.
    assert _settings(**base).effective_mode is TradingMode.LIVE

    # Remove any one: blocked.
    for key in base:
        partial = dict(base)
        partial[key] = "no" if key != "ATLAS_TRADING_MODE" else "paper"
        settings = _settings(**partial)
        assert settings.effective_mode is TradingMode.PAPER, f"removing {key} must block live"


def test_live_confirmation_phrase_must_match_exactly() -> None:
    for phrase in ("yes", "true", "I understand the risk", "i_understand_the_risk", "YES"):
        settings = _settings(
            ATLAS_TRADING_MODE="live",
            ATLAS_LIVE_TRADING_ENABLED="true",
            ATLAS_MANUAL_LIVE_CONFIRMATION=phrase,
        )
        assert settings.effective_mode is TradingMode.PAPER, f"{phrase!r} must not unlock live"


def test_blocked_live_is_explained() -> None:
    """The operator must be told which gate is closed, not just refused."""
    settings = _settings(ATLAS_TRADING_MODE="live", ATLAS_LIVE_TRADING_ENABLED="false")
    warnings = settings.mode_warnings()
    assert any("LIVE trading was requested but is BLOCKED" in w for w in warnings)
    assert settings.live_gates["mode_is_live"] is True
    assert settings.live_gates["live_trading_enabled"] is False


def test_backtest_and_replay_modes_pass_through() -> None:
    for value, expected in (
        ("backtest", TradingMode.BACKTEST),
        ("replay", TradingMode.REPLAY),
    ):
        assert _settings(ATLAS_TRADING_MODE=value).effective_mode is expected


def test_is_paper_true_for_every_non_live_mode() -> None:
    for value in ("paper", "backtest", "replay", "nonsense"):
        assert _settings(ATLAS_TRADING_MODE=value).is_paper is True


def test_mode_properties() -> None:
    assert TradingMode.LIVE.uses_real_money is True
    assert TradingMode.PAPER.uses_real_money is False
    assert TradingMode.PAPER.touches_broker is True
    assert TradingMode.BACKTEST.touches_broker is False


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #


def test_missing_credentials_detected() -> None:
    settings = _settings()
    assert settings.has_alpaca_credentials is False
    assert any("No Alpaca credentials" in w for w in settings.mode_warnings())


def test_whitespace_only_credentials_are_not_credentials() -> None:
    settings = _settings(ALPACA_API_KEY="   ", ALPACA_SECRET_KEY="\t")
    assert settings.has_alpaca_credentials is False


def test_credentials_detected_when_present() -> None:
    settings = _settings(ALPACA_API_KEY="PKTEST", ALPACA_SECRET_KEY="secret")
    assert settings.has_alpaca_credentials is True


# --------------------------------------------------------------------------- #
# market data plan limits
# --------------------------------------------------------------------------- #


def test_sip_downgrades_to_iex_without_paid_plan() -> None:
    """Requesting SIP on the free plan would error on recent data."""
    settings = _settings(ATLAS_STOCK_DATA_FEED="sip", ATLAS_HAS_PAID_DATA_PLAN="false")
    assert settings.effective_data_feed is StockDataFeed.IEX
    assert any("Downgraded to IEX" in w for w in settings.mode_warnings())


def test_sip_allowed_with_paid_plan() -> None:
    settings = _settings(ATLAS_STOCK_DATA_FEED="sip", ATLAS_HAS_PAID_DATA_PLAN="true")
    assert settings.effective_data_feed is StockDataFeed.SIP


def test_delayed_sip_allowed_on_free_plan() -> None:
    """delayed_sip is full-market data on a 15-minute delay, and is free."""
    settings = _settings(ATLAS_STOCK_DATA_FEED="delayed_sip")
    assert settings.effective_data_feed is StockDataFeed.DELAYED_SIP


def test_unknown_feed_falls_back_to_iex() -> None:
    assert _settings(ATLAS_STOCK_DATA_FEED="nonsense").effective_data_feed is StockDataFeed.IEX


def test_stream_symbols_capped_on_free_plan() -> None:
    """The free Alpaca plan allows 30 websocket symbols."""
    settings = _settings(ATLAS_MAX_STREAM_SYMBOLS="500", ATLAS_HAS_PAID_DATA_PLAN="false")
    assert settings.effective_max_stream_symbols == 30
    assert any("30 streaming symbols" in w for w in settings.mode_warnings())


def test_stream_symbols_uncapped_with_paid_plan() -> None:
    settings = _settings(ATLAS_MAX_STREAM_SYMBOLS="500", ATLAS_HAS_PAID_DATA_PLAN="true")
    assert settings.effective_max_stream_symbols == 500


def test_cors_origins_parsed() -> None:
    settings = _settings(ATLAS_CORS_ORIGINS="http://a.test, http://b.test ,")
    assert settings.cors_origin_list == ["http://a.test", "http://b.test"]


# --------------------------------------------------------------------------- #
# YAML config
# --------------------------------------------------------------------------- #


def test_real_config_loads(config: object) -> None:
    """The shipped YAML must validate. Guards against a bad edit."""
    from app.config.schema import AtlasConfig

    assert isinstance(config, AtlasConfig)


def test_risk_defaults_are_conservative(config: object) -> None:
    """A regression guard: these are the numbers that protect the account."""
    risk = config.risk  # type: ignore[attr-defined]
    assert risk.max_risk_per_trade_pct <= 1.0, "per-trade risk should stay well under 1%"
    assert risk.max_daily_loss_pct <= 5.0
    assert risk.max_leverage <= 1.0, "default must be cash-account behaviour"
    assert risk.require_market_open is True
    assert risk.kill_switch.flatten_positions is False, (
        "flattening must be opt-in: market-selling the book during an outage "
        "turns a paper problem into a realised loss"
    )


def test_strategies_ship_disabled(config: object) -> None:
    """Nothing may trade until the operator explicitly turns it on."""
    strategies = config.strategies  # type: ignore[attr-defined]
    assert strategies.strategies_globally_enabled is False
    assert all(not s.enabled for s in strategies.strategies)


def test_active_universe_is_within_the_free_stream_budget(config: object) -> None:
    """The default universe must fit on one free-plan websocket."""
    scanner = config.scanner  # type: ignore[attr-defined]
    assert len(scanner.active_symbols) <= 30, (
        "the default universe should fit the free plan's 30-symbol websocket limit"
    )


def test_correlation_groups_lookup(config: object) -> None:
    risk = config.risk  # type: ignore[attr-defined]
    assert risk.correlation_group_for("SPY") == "us_broad_index"
    assert risk.correlation_group_for("NVDA") == "semiconductors"
    assert risk.correlation_group_for("ZZZZ_NOT_A_SYMBOL") is None


def test_missing_config_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Missing config file"):
        load_config(tmp_path)


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    (tmp_path / "risk.yaml").write_text("this: [is: not: valid", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    """A typo in a risk limit must fail loudly, not be silently ignored.

    `max_risk_per_trade_pc` (missing the t) would otherwise mean trading at the
    default limit while believing it was configured.
    """
    from pydantic import ValidationError

    from app.config.schema import RiskConfig

    with pytest.raises(ValidationError):
        RiskConfig.model_validate({"max_risk_per_trade_pc": 0.1})


def test_out_of_range_value_is_rejected() -> None:
    from pydantic import ValidationError

    from app.config.schema import RiskConfig

    with pytest.raises(ValidationError):
        RiskConfig.model_validate({"max_risk_per_trade_pct": -5})


def test_scanner_rejects_unknown_active_universe() -> None:
    from app.config.schema import ScannerConfig

    with pytest.raises(Exception, match="not defined"):
        ScannerConfig.model_validate({"active": "does_not_exist", "universes": {"real": ["SPY"]}})


def test_duplicate_agent_ids_rejected() -> None:
    from app.config.schema import AgentsConfig

    with pytest.raises(Exception, match="duplicate agent ids"):
        AgentsConfig.model_validate(
            {
                "agents": [
                    {"id": "dup", "name": "A", "role": "r", "type": "scanner"},
                    {"id": "dup", "name": "B", "role": "r", "type": "risk"},
                ]
            }
        )
