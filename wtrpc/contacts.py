"""Work out how close the nearest hostile aircraft is, from the minimap feed.

This exists to separate two situations the instruments cannot tell apart. A
9G break turn reads exactly like a turning fight, but if the closest bandit is
six kilometres away and opening, it is not a fight. ``/map_obj.json`` carries
the positions, so the distance can settle it.

Everything here is pure: it takes the two decoded JSON documents and returns a
number. No network, no clock.

Marker shapes, confirmed against a live match:

    {"type": "aircraft", "icon": "Player",  "color[]": [250, 200,  30],
     "x": 0.346, "y": 0.683, "dx": -0.98, "dy": -0.18}
    {"type": "aircraft", "icon": "Fighter", "color[]": [240,  12,   0],
     "x": 0.346, "y": 0.673, ...}

The player is the yellow marker; hostiles are red; friendlies are blue. The
coordinates are fractions of the map, so they need the map extent from
``/map_info.json`` to become distances.
"""

from __future__ import annotations

from math import hypot

# A marker counts as hostile when red clearly dominates. War Thunder uses
# #f00C00 / #fa0C00 for enemies, #174DFF for friendlies and #faC81E for you.
_HOSTILE_MIN_RED = 180
_HOSTILE_MAX_GREEN = 120
_HOSTILE_MAX_BLUE = 120

_DEFAULT_MAP_SPAN_M = 65536.0 * 2


def _rgb(marker: dict) -> tuple[int, int, int] | None:
    channels = marker.get("color[]")
    if not isinstance(channels, (list, tuple)) or len(channels) < 3:
        return None
    try:
        return int(channels[0]), int(channels[1]), int(channels[2])
    except (TypeError, ValueError):
        return None


def is_hostile(marker: dict) -> bool:
    """True when the marker is drawn in the enemy colour."""
    rgb = _rgb(marker)
    if rgb is None:
        return False
    red, green, blue = rgb
    return (
        red >= _HOSTILE_MIN_RED
        and green <= _HOSTILE_MAX_GREEN
        and blue <= _HOSTILE_MAX_BLUE
    )


def is_aircraft(marker: dict) -> bool:
    return marker.get("type") == "aircraft"


def find_player(markers: list) -> dict | None:
    for marker in markers:
        if isinstance(marker, dict) and marker.get("icon") == "Player":
            return marker
    return None


def map_span_m(map_info: dict | None) -> tuple[float, float]:
    """Width and height of the map in metres, from ``/map_info.json``.

    Falls back to the game's usual +/-65536 coordinate space when the document
    is missing or malformed, which keeps distances in the right ballpark
    rather than producing nonsense.
    """
    if not isinstance(map_info, dict):
        return _DEFAULT_MAP_SPAN_M, _DEFAULT_MAP_SPAN_M

    lo = map_info.get("map_min")
    hi = map_info.get("map_max")
    if (
        isinstance(lo, (list, tuple))
        and isinstance(hi, (list, tuple))
        and len(lo) >= 2
        and len(hi) >= 2
    ):
        try:
            span_x = abs(float(hi[0]) - float(lo[0]))
            span_y = abs(float(hi[1]) - float(lo[1]))
        except (TypeError, ValueError):
            return _DEFAULT_MAP_SPAN_M, _DEFAULT_MAP_SPAN_M
        if span_x > 0 and span_y > 0:
            return span_x, span_y

    return _DEFAULT_MAP_SPAN_M, _DEFAULT_MAP_SPAN_M


def nearest_hostile_km(markers: list | None, map_info: dict | None) -> float | None:
    """Horizontal distance in km to the closest hostile aircraft.

    Returns ``None`` when it cannot be worked out — no minimap data, no player
    marker, or no hostile aircraft shown. ``None`` means "unknown", never
    "clear skies": outside Arcade the minimap only shows spotted enemies. It
    is also a purely horizontal distance, because the minimap has no altitude.
    """
    if not markers:
        return None

    player = find_player(markers)
    if player is None:
        return None

    try:
        px = float(player["x"])
        py = float(player["y"])
    except (KeyError, TypeError, ValueError):
        return None

    span_x, span_y = map_span_m(map_info)

    best: float | None = None
    for marker in markers:
        if not isinstance(marker, dict):
            continue
        if marker is player or not is_aircraft(marker) or not is_hostile(marker):
            continue
        try:
            dx = (float(marker["x"]) - px) * span_x
            dy = (float(marker["y"]) - py) * span_y
        except (KeyError, TypeError, ValueError):
            continue
        distance_km = hypot(dx, dy) / 1000.0
        if best is None or distance_km < best:
            best = distance_km

    return best
