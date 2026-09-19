"""JSON-backed application configuration.

Stored at ``%APPDATA%/WarThunderRPC-Plus/config.json`` on a normal Windows
install, falling back to ``~/.config/WarThunderRPC-Plus/config.json`` when
``APPDATA`` is unset (e.g. in a test environment or on another OS).

``load`` never raises: a missing file is created with defaults, a corrupt
file falls back to defaults wholesale, and an individual field with the
wrong type falls back to that field's default while every other valid field
is kept.
"""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import asdict, dataclass, fields

_APP_DIR_NAME = "WarThunderRPC-Plus"
_CONFIG_FILE_NAME = "config.json"

_POLL_INTERVAL_FLOOR = 1.0
# The flight regime needs 5 samples inside a 20s window, so polling slower than
# once every 5s silently switches the headline feature off. Cap it instead.
_POLL_INTERVAL_CEILING = 5.0
_MIN_UPDATE_INTERVAL_FLOOR = 15.0
# requests raises ValueError, not a RequestException, for a non-positive
# timeout -- which no caller catches, so an unlucky config file would kill the
# process on its first poll.
_TIMEOUT_FLOOR = 0.1
_TIMEOUT_CEILING = 30.0


@dataclass(frozen=True)
class Config:
    """User-configurable settings for WarThunderRPC-Plus."""

    client_id: str = "1550761049056223352"
    """Discord application ID, registered as "War Thunder".

    THE APPLICATION'S NAME is what Discord prints after "Playing" -- no field
    in the presence payload can override it. The upstream project's ID renders
    as that application's own name instead, so this project uses its own."""
    large_image: str = "logo"
    """Either an asset key uploaded to your Discord application, or a plain
    https URL. A key only resolves against the application that owns it, so a
    new client_id needs either its own uploaded `logo` asset or a URL here."""
    player_name: str = ""
    """Your exact in-game name, used to count your kills from the HUD feed.

    /hudmsg carries no identity at all, so without this the player can only be
    guessed from the vehicle model -- which picks a stranger the moment anyone
    else drives the same one. Left empty, the kill counter stays off rather
    than risk showing someone else's score as yours."""
    show_kills: bool = True
    show_weapon: bool = False
    """Read the selected weapon off the screen with OCR.

    Off by default: it needs Tesseract installed, it only works while the
    weapon HUD block is actually rendered, and it reads pixels rather than
    data. Everything else in this app comes from the game's own API."""
    weapon_region: str = "0,0,900,600"
    """Screen box "left,top,right,bottom" holding the weapon HUD block.

    The block sits top-left, but its exact position moves with resolution and
    UI scale, so it is adjustable."""
    poll_interval: float = 3.0
    min_update_interval: float = 15.0
    show_map: bool = True
    show_vehicle_image: bool = True
    show_flight_data: bool = True
    dogfight_detection: bool = True
    connect_timeout: float = 0.5
    read_timeout: float = 1.5

    def __post_init__(self) -> None:
        # Discord rate-limits presence updates to roughly one per 15s, and
        # polling faster than once a second is pointless churn, so both
        # intervals are clamped no matter how the Config was constructed.
        object.__setattr__(
            self,
            "poll_interval",
            min(
                _POLL_INTERVAL_CEILING,
                max(_POLL_INTERVAL_FLOOR, float(self.poll_interval)),
            ),
        )
        object.__setattr__(
            self,
            "min_update_interval",
            max(_MIN_UPDATE_INTERVAL_FLOOR, float(self.min_update_interval)),
        )
        for field_name in ("connect_timeout", "read_timeout"):
            object.__setattr__(
                self,
                field_name,
                min(
                    _TIMEOUT_CEILING,
                    max(_TIMEOUT_FLOOR, float(getattr(self, field_name))),
                ),
            )


def config_path() -> pathlib.Path:
    """Resolve the on-disk location of the config file."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        base = pathlib.Path(appdata)
    else:
        base = pathlib.Path.home() / ".config"
    return base / _APP_DIR_NAME / _CONFIG_FILE_NAME


def _coerce_field(name: str, raw_value: object, default_value: object) -> object:
    """Return ``raw_value`` if it matches the field's expected type, else the default."""
    expected_type = type(default_value)

    if expected_type is bool:
        return raw_value if isinstance(raw_value, bool) else default_value

    if expected_type is float:
        if isinstance(raw_value, bool):
            return default_value
        if isinstance(raw_value, (int, float)):
            return float(raw_value)
        return default_value

    if expected_type is str:
        return raw_value if isinstance(raw_value, str) else default_value

    return raw_value if isinstance(raw_value, expected_type) else default_value


def load(path: pathlib.Path | str | None = None) -> Config:
    """Load config from disk, tolerating a missing, corrupt, or partially bad file."""
    resolved = pathlib.Path(path) if path is not None else config_path()
    defaults = Config()

    if not resolved.exists():
        save(defaults, resolved)
        return defaults

    try:
        raw_text = resolved.read_text(encoding="utf-8")
    except OSError:
        return defaults

    try:
        raw = json.loads(raw_text) if raw_text.strip() else None
    except (ValueError, json.JSONDecodeError):
        return defaults

    if not isinstance(raw, dict):
        return defaults

    kwargs = {}
    for f in fields(Config):
        default_value = getattr(defaults, f.name)
        if f.name in raw:
            kwargs[f.name] = _coerce_field(f.name, raw[f.name], default_value)
        else:
            kwargs[f.name] = default_value

    return Config(**kwargs)


def save(cfg: Config, path: pathlib.Path | str | None = None) -> None:
    """Write ``cfg`` to disk as JSON, creating parent directories as needed."""
    resolved = pathlib.Path(path) if path is not None else config_path()
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
    except OSError:
        pass
