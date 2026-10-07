"""Loads and validates the YAML files in `config/`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config.schema import (
    AgentsConfig,
    AtlasConfig,
    RiskConfig,
    ScannerConfig,
    StrategiesConfig,
)
from app.config.settings import CONFIG_DIR


class ConfigError(RuntimeError):
    """Raised when a config file is missing or invalid.

    Deliberately fatal: ATLAS refuses to start on a bad risk config rather
        than falling back to defaults the operator did not choose.
    """


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(
            f"Missing config file: {path}\n"
            f"Expected it in {path.parent}. Did you copy the repository completely?"
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from exc

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name} must contain a mapping at the top level.")
    return raw


def load_config(config_dir: Path | None = None) -> AtlasConfig:
    """Read, validate and return every YAML config.

    Args:
        config_dir: override for tests. Defaults to the project `config/`.

    Raises:
        ConfigError: on a missing file, bad YAML, or a value that fails
            validation (including unknown keys, which are treated as typos).
    """
    directory = config_dir or CONFIG_DIR

    files = {
        "risk": (directory / "risk.yaml", RiskConfig),
        "scanner": (directory / "scanner.yaml", ScannerConfig),
        "strategies": (directory / "strategies.yaml", StrategiesConfig),
        "agents": (directory / "agents.yaml", AgentsConfig),
    }

    parsed: dict[str, Any] = {}
    for key, (path, model) in files.items():
        raw = _read_yaml(path)
        try:
            parsed[key] = model.model_validate(raw)
        except Exception as exc:  # pydantic ValidationError and friends
            raise ConfigError(f"{path.name} failed validation:\n{exc}") from exc

    return AtlasConfig(**parsed)


@lru_cache(maxsize=1)
def get_config() -> AtlasConfig:
    """Process-wide config singleton. Tests use `get_config.cache_clear()`."""
    return load_config()
