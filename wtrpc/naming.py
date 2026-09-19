"""Vehicle name formatting and encyclopedia URL helpers.

Pure string logic only — no network, no filesystem. ``/indicators`` reports a
raw internal name such as ``us_m1_abrams`` or, for ground vehicles, a prefixed
form such as ``tankModels/us_m1_abrams``. During respawn and at match start
the game reports the placeholder ``dummy_plane`` (sometimes ``DUMMY_PLANE``),
a camera name rather than a real vehicle.
"""

from __future__ import annotations

PLACEHOLDER_IDS: set[str] = {"dummy_plane"}
"""Internal names that mean 'no real vehicle yet', lower-cased for comparison."""

# Gaijin retired the bare ``encyclopedia.warthunder.com`` host -- it no longer
# resolves in DNS at all -- so every thumbnail the original project emitted has
# been dead for some time. Images now live on the static CDN, and the vehicle
# page moved to the new wiki. Both take the same internal id that /indicators
# reports under "type", e.g. "b-17e" or "us_m1_abrams".
_ENCYCLOPEDIA_BASE = "https://static.encyclopedia.warthunder.com/images/"
_ENCYCLOPEDIA_SUFFIX = ".png"
_WIKI_UNIT_BASE = "https://wiki.warthunder.com/unit/"


def is_placeholder(raw: str) -> bool:
    """True when ``raw`` is empty or a known placeholder camera name."""
    if not raw:
        return True
    return raw.strip().lower() in PLACEHOLDER_IDS


def format_vehicle(raw: str) -> tuple[str, str]:
    """Split a raw internal name into ``(vehicle_id, display_name)``.

    Any ``*/`` prefixes (e.g. ``tankModels/``) are stripped to get the id.
    The display name replaces underscores with spaces and upper-cases.
    Empty or placeholder input yields ``("", "")``.
    """
    if is_placeholder(raw):
        return "", ""

    vehicle_id = raw.strip()
    if "/" in vehicle_id:
        vehicle_id = vehicle_id.rsplit("/", 1)[-1]

    if is_placeholder(vehicle_id):
        return "", ""

    display_name = vehicle_id.replace("_", " ").upper()
    return vehicle_id, display_name


def _usable(vehicle_id: str) -> bool:
    return bool(vehicle_id) and not is_placeholder(vehicle_id)


def encyclopedia_url(vehicle_id: str) -> str:
    """Build the wiki page URL for a vehicle, or '' when unusable.

    Suitable for a Discord rich presence button.
    """
    if not _usable(vehicle_id):
        return ""
    return _WIKI_UNIT_BASE + vehicle_id


def encyclopedia_image_url(vehicle_id: str) -> str:
    """Build the vehicle thumbnail URL, or '' when unusable.

    Returning '' rather than a guaranteed-404 link matters because the caller
    omits ``small_image`` entirely when this is empty.
    """
    if not _usable(vehicle_id):
        return ""
    return _ENCYCLOPEDIA_BASE + vehicle_id + _ENCYCLOPEDIA_SUFFIX
