"""Shared data contracts between the telemetry layer and the presence layer.

Everything here is a plain dataclass with no I/O so that the presence logic can
be exercised in tests without War Thunder or Discord running.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Army(str, Enum):
    """Vehicle class reported by ``/indicators`` under the ``army`` key."""

    AIR = "air"
    TANK = "tank"
    SHIP = "ship"
    UNKNOWN = "unknown"


class Activity(str, Enum):
    """Coarse description of what the player is doing right now."""

    HANGAR = "hangar"
    LOADING = "loading"
    TEST_DRIVE = "test_drive"
    IN_MATCH = "in_match"
    UNKNOWN = "unknown"


class AirState(str, Enum):
    """Flight regime derived from a rolling window of aircraft telemetry.

    Three of these describe combat and are told apart by two independent
    questions -- is the aircraft being thrown around, and is anyone close?

    ===============  ================  ==================
    state            hard manoeuvring  hostile within 2.5 km
    ===============  ================  ==================
    DOGFIGHTING      yes               yes
    MANEUVERING      yes               no
    ENGAGED          no                yes
    ===============  ================  ==================

    All three were observed in one play session. Pulling 11.8G with the
    nearest enemy 30 km away is a break turn, not a dogfight. Holding station
    480 m from an enemy fighter for 25 seconds at Mach 1.4 without turning
    hard is a firing pass, not aerobatics. Reporting either as "Dogfighting"
    is simply wrong, and the instruments alone cannot tell them apart -- the
    position of the nearest hostile is what separates them.
    """

    DOGFIGHTING = "dogfighting"
    ENGAGED = "engaged"
    MANEUVERING = "maneuvering"
    SUPERSONIC = "supersonic"
    CLIMBING = "climbing"
    DIVING = "diving"
    CRUISING = "cruising"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Flight:
    """Aircraft telemetry sample distilled from ``/state`` and ``/indicators``.

    ``mach`` and ``ias_kph`` are ``None`` when the game did not report them,
    which happens for ground vehicles and briefly during respawns.
    """

    mach: float | None = None
    ias_kph: float | None = None
    tas_kph: float | None = None
    altitude_m: float | None = None
    load_factor: float | None = None
    vertical_speed_ms: float | None = None
    heading_deg: float | None = None
    roll_deg: float | None = None
    throttle_pct: float | None = None
    nearest_hostile_km: float | None = None
    """Horizontal distance to the closest hostile aircraft on the minimap.

    ``None`` means "not known", which is not the same as "nobody is there":
    outside Arcade the minimap only shows enemies your team has spotted, and
    the minimap carries no altitude, so this is a horizontal distance between
    two aircraft that may be far apart vertically. Treat it as evidence that
    someone IS close, never as proof that nobody is.
    """

    @property
    def valid(self) -> bool:
        return self.mach is not None or self.ias_kph is not None


@dataclass(frozen=True)
class GameState:
    """Everything the presence builder needs, in one immutable snapshot."""

    activity: Activity = Activity.UNKNOWN
    army: Army = Army.UNKNOWN
    vehicle_id: str = ""
    """Raw internal name, e.g. ``us_m1_abrams``. Empty when unknown."""
    vehicle_name: str = ""
    """Display name, e.g. ``US M1 ABRAMS``. Empty when unknown."""
    map_name: str = ""
    """Human readable map name, or empty when it could not be identified."""
    mode: str = ""
    """Match mode label, e.g. ``Air Domination``. Empty when unknown."""
    flight: Flight = field(default_factory=Flight)
    air_state: AirState = AirState.UNKNOWN
    weapon: str = ""
    """Selected weapon name read from the HUD, empty when unread."""
    kills: int = 0
    """Confirmed kills by the player in this match, 0 when not counted."""
    match_started_at: int | None = None
    """Unix timestamp the current activity began, for the Discord elapsed timer."""


@dataclass(frozen=True)
class PresencePayload:
    """A Discord rich presence update, ready to hand to pypresence.

    Discord truncates ``details``, ``state``, ``large_text`` and ``small_text``
    at 128 characters, so the builder is responsible for staying under that.
    """

    details: str
    state: str
    start: int | None = None
    large_image: str = "logo"
    large_text: str = "War Thunder"
    small_image: str | None = None
    small_text: str | None = None

    def as_kwargs(self) -> dict:
        """Render to the keyword arguments ``pypresence.Presence.update`` takes."""
        payload = {
            "details": self.details,
            "state": self.state,
            "large_image": self.large_image,
            "large_text": self.large_text,
        }
        if self.start is not None:
            payload["start"] = self.start
        if self.small_image:
            payload["small_image"] = self.small_image
        if self.small_text:
            payload["small_text"] = self.small_text
        return payload
