"""TOML configuration for the ``ptouch`` CLI.

A config file supplies *defaults* for the printer target, tape width, font,
font size, orientation, auto-cut, and margin. Precedence is:

    CLI flag  >  config file  >  built-in default

The file is flat TOML, e.g.::

    printer     = "usb://Brother/PT-P710BT?serial=000M5G671606"
    tape        = 24
    auto_cut    = true
    margin_dots = 14
    font        = "/System/Library/Fonts/Supplemental/Arial.ttf"
    font_size   = 40
    orientation = "vertical"

Discovery order when ``--config`` is not passed:
  1. ``$PTOUCH_CONFIG``
  2. ``./ptouch.toml``
  3. ``$XDG_CONFIG_HOME/ptouch/config.toml`` (``~/.config/ptouch/config.toml``)
  4. ``~/.ptouch.toml``
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.10 in CI
    import tomli as tomllib  # type: ignore[no-redef]

__all__ = ["Config", "ConfigError", "load_config", "resolve_config", "find_config_path"]

#: The recognized top-level keys. Anything else is a typo -> hard error.
_KNOWN_KEYS = frozenset(
    {"printer", "tape", "auto_cut", "margin_dots", "font", "font_size", "orientation"}
)


class ConfigError(ValueError):
    """Raised on a missing/unreadable config file or an invalid key/value."""


@dataclass
class Config:
    """Resolved config defaults. ``None`` means "not set in the file"."""

    printer: str | None = None
    tape: float | None = None
    auto_cut: bool | None = None
    margin_dots: int | None = None
    font: str | None = None
    font_size: int | None = None
    orientation: str | None = None
    #: Path the config was loaded from (for diagnostics); None if defaults.
    source: str | None = None


def _xdg_config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else Path.home() / ".config"


def find_config_path() -> str | None:
    """Return the first config path that applies, or None.

    ``$PTOUCH_CONFIG`` wins if set (even if the file is missing -- that's an
    explicit choice and :func:`load_config` will report it clearly).
    """
    env = os.environ.get("PTOUCH_CONFIG")
    if env:
        return env
    for candidate in (
        Path.cwd() / "ptouch.toml",
        _xdg_config_home() / "ptouch" / "config.toml",
        Path.home() / ".ptouch.toml",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def load_config(path: str) -> Config:
    """Load and validate a TOML config file.

    Raises:
        ConfigError: if the file is missing, not valid TOML, has an unknown
            key, or a value has the wrong type / is out of range.
    """
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as err:
        raise ConfigError(f"invalid TOML in {path}: {err}") from err

    unknown = set(data) - _KNOWN_KEYS
    if unknown:
        raise ConfigError(
            f"{path}: unknown config key(s): {', '.join(sorted(unknown))}. "
            f"Known keys: {', '.join(sorted(_KNOWN_KEYS))}"
        )

    cfg = Config(source=str(p))

    if "printer" in data:
        cfg.printer = _as_str(data, "printer", path)
    if "tape" in data:
        cfg.tape = _as_positive_number(data, "tape", path)
    if "auto_cut" in data:
        v = data["auto_cut"]
        if not isinstance(v, bool):
            raise ConfigError(f"{path}: 'auto_cut' must be true or false")
        cfg.auto_cut = v
    if "margin_dots" in data:
        v = data["margin_dots"]
        if isinstance(v, bool) or not isinstance(v, int):
            raise ConfigError(f"{path}: 'margin_dots' must be an integer")
        if not (0 <= v <= 0xFFFF):
            raise ConfigError(f"{path}: 'margin_dots' must be in [0, 65535]")
        cfg.margin_dots = v
    if "font" in data:
        cfg.font = _as_str(data, "font", path)
    if "font_size" in data:
        v = data["font_size"]
        if isinstance(v, bool) or not isinstance(v, int):
            raise ConfigError(f"{path}: 'font_size' must be an integer")
        if v <= 0:
            raise ConfigError(f"{path}: 'font_size' must be positive")
        cfg.font_size = v
    if "orientation" in data:
        v = data["orientation"]
        if v not in ("horizontal", "vertical"):
            raise ConfigError(f"{path}: 'orientation' must be 'horizontal' or 'vertical'")
        cfg.orientation = v

    return cfg


def resolve_config(explicit_path: str | None) -> Config:
    """Load the config for a CLI invocation.

    With ``explicit_path`` set, that file must exist. Otherwise the standard
    locations are searched; if none is found, an empty :class:`Config` (all
    built-in defaults) is returned.
    """
    if explicit_path:
        return load_config(explicit_path)
    found = find_config_path()
    return load_config(found) if found else Config()


def _as_str(data: dict, key: str, path: str) -> str:
    v = data[key]
    if not isinstance(v, str):
        raise ConfigError(f"{path}: '{key}' must be a string")
    return v


def _as_positive_number(data: dict, key: str, path: str) -> float:
    v = data[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ConfigError(f"{path}: '{key}' must be a number")
    if v <= 0:
        raise ConfigError(f"{path}: '{key}' must be positive")
    return float(v)
