"""Assembles the final Discord Rich Presence payload from a ``GameState``.

Pure — no clock reads, no I/O. The caller (owned by another agent) is
responsible for reading ``/indicators``, ``/state``, ``/mission.json`` and
wiring the four display flags from ``Config``; this module only turns an
already-assembled ``GameState`` snapshot into text.
"""

from __future__ import annotations

from wtrpc.flight import label as flight_regime_label
from wtrpc.ground import ammo_label, crew_label, damage_label
from wtrpc.models import Activity, AirState, Army, GameState, PresencePayload
from wtrpc.naming import encyclopedia_image_url, is_placeholder

MAX_FIELD_LEN = 128
_SEPARATOR = " · "  # middle dot
_ELLIPSIS = "…"

_VERBS: dict[Army, str] = {
    Army.AIR: "Piloting a",
    Army.TANK: "Driving a",
    Army.SHIP: "Commanding a",
}

_DEFAULT_DETAILS = "War Thunder"
_DEFAULT_STATE = "..."


def _truncate(text: str, max_len: int = MAX_FIELD_LEN) -> str:
    """Truncate to ``max_len`` characters, adding an ellipsis when cut."""
    if len(text) <= max_len:
        return text
    if max_len <= 1:
        return text[:max_len]
    return text[: max_len - 1].rstrip() + _ELLIPSIS


def _vehicle_or_fallback(name: str, fallback: str = "Unknown vehicle") -> str:
    return name if name else fallback


def _build_hangar() -> tuple[str, str]:
    return "In the hangar", "Browsing vehicles..."


def _build_loading(state: GameState) -> tuple[str, str]:
    details = state.mode or "Loading"
    return details, "Loading into a match..."


def _build_test_drive(
    state: GameState, *, show_flight_data: bool = True, dogfight_detection: bool = True
) -> tuple[str, str]:
    """Test Flight carries the same telemetry as a match.

    /state serves full aircraft telemetry in a test flight, and this is how
    most people first try the app, so hiding Mach and airspeed here meant the
    headline feature looked broken on first contact.
    """
    verb = _VERBS.get(state.army, "Testing a")
    vehicle = _vehicle_or_fallback(state.vehicle_name, "a vehicle")
    body = _build_vehicle_state(state, show_flight_data)

    if body and body != _vehicle_or_fallback(state.vehicle_name):
        # _build_vehicle_state already appended the telemetry; prefix the verb.
        state_text = f"{verb} {body}"
    else:
        state_text = f"{verb} {vehicle}"

    details = "Test Drive"
    regime = _regime_label(state, dogfight_detection)
    if regime:
        details = f"{details}{_SEPARATOR}{regime}"
    return details, state_text


# Mach only says something once an aircraft is genuinely fast. Parked on the
# runway the game reports M=0.00, and a helicopter cruises around M=0.05 --
# measured live in an AH-64A, right on the old threshold, so the figure
# flickered in and out of the presence while saying nothing. Airspeed is the
# meaningful number down there and is shown regardless.
_MIN_MACH = 0.30
_MIN_IAS_KPH = 20.0


def _kill_suffix(state: GameState, show_kills: bool) -> str:
    if not show_kills or state.kills <= 0:
        return ""
    return f"{state.kills} kill" + ("s" if state.kills != 1 else "")


def _build_vehicle_state(
    state: GameState, show_flight_data: bool, show_kills: bool = True
) -> str:
    vehicle = _vehicle_or_fallback(state.vehicle_name)
    kills = _kill_suffix(state, show_kills)

    if state.army in (Army.TANK, Army.SHIP) and show_flight_data:
        # /state is aviation only, so a ground vehicle has no Mach or
        # airspeed to show. Its ready rack, its crew and its broken modules
        # are the equivalent, and until now they went unused entirely.
        bits = [
            b
            for b in (
                # The loaded round when it can be read; otherwise what the
                # vehicle is carrying, which is always knowable.
                state.ground.shell or state.ground.loadout,
                ammo_label(state.ground),
                crew_label(state.ground),
            )
            if b
        ]
        if kills:
            bits.append(kills)
        if bits:
            return _SEPARATOR.join([vehicle, *bits])
        return vehicle

    if state.army == Army.AIR and show_flight_data and state.flight is not None:
        bits: list[str] = []
        # Parked on the runway the game reports M=0.00 and IAS=0, which are not
        # None and would render as "Mach 0.00 - 0 km/h IAS". Nobody wants that
        # on their profile, so anything below taxi speed is simply omitted.
        if state.flight.mach is not None and state.flight.mach >= _MIN_MACH:
            bits.append(f"Mach {state.flight.mach:.2f}")
        if state.flight.ias_kph is not None and state.flight.ias_kph >= _MIN_IAS_KPH:
            bits.append(f"{round(state.flight.ias_kph)} km/h IAS")
        if state.weapon:
            bits.append(state.weapon)
        if bits:
            return _SEPARATOR.join([vehicle, *bits, *( [kills] if kills else [] )])

    if kills:
        return _SEPARATOR.join([vehicle, kills])
    return vehicle


def _regime_label(state: GameState, dogfight_detection: bool) -> str:
    if state.army in (Army.TANK, Army.SHIP):
        # A knocked-out module is the closest a tank has to a flight regime.
        return damage_label(state.ground)
    if state.air_state == AirState.UNKNOWN:
        return ""
    if state.air_state == AirState.DOGFIGHTING and not dogfight_detection:
        return ""
    return flight_regime_label(state.air_state)


def _build_in_match(
    state: GameState,
    *,
    show_map: bool,
    show_flight_data: bool,
    dogfight_detection: bool,
    show_kills: bool = True,
) -> tuple[str, str]:
    regime = _regime_label(state, dogfight_detection)

    mode = state.mode or ""
    map_name = state.map_name if (show_map and state.map_name) else ""

    left = _SEPARATOR.join(part for part in (regime, mode) if part)

    if left and map_name:
        details = f"{left} on {map_name}"
    elif left:
        details = left
    elif map_name:
        details = map_name
    else:
        details = ""

    state_text = _build_vehicle_state(state, show_flight_data, show_kills)
    return details, state_text


def _build_unknown(state: GameState) -> tuple[str, str]:
    vehicle = _vehicle_or_fallback(state.vehicle_name, "")
    state_text = vehicle if vehicle else "Status unknown"
    return "War Thunder", state_text


def build_presence(
    state: GameState,
    *,
    show_map: bool = True,
    show_vehicle_image: bool = True,
    show_flight_data: bool = True,
    dogfight_detection: bool = True,
    show_kills: bool = True,
    large_image: str = "logo",
) -> PresencePayload:
    """Build a ``PresencePayload`` from a ``GameState`` snapshot. Pure function."""
    if state.activity == Activity.HANGAR:
        details, state_text = _build_hangar()
    elif state.activity == Activity.LOADING:
        details, state_text = _build_loading(state)
    elif state.activity == Activity.TEST_DRIVE:
        details, state_text = _build_test_drive(
            state,
            show_flight_data=show_flight_data,
            dogfight_detection=dogfight_detection,
        )
    elif state.activity == Activity.IN_MATCH:
        details, state_text = _build_in_match(
            state,
            show_map=show_map,
            show_flight_data=show_flight_data,
            dogfight_detection=dogfight_detection,
            show_kills=show_kills,
        )
    else:
        details, state_text = _build_unknown(state)

    details = _truncate(details) if details else _DEFAULT_DETAILS
    state_text = _truncate(state_text) if state_text else _DEFAULT_STATE

    small_image = None
    small_text = None
    if show_vehicle_image and state.vehicle_id and not is_placeholder(state.vehicle_id):
        url = encyclopedia_image_url(state.vehicle_id)
        if url:
            small_image = url
            if state.vehicle_name:
                small_text = _truncate(state.vehicle_name)

    return PresencePayload(
        details=details,
        state=state_text,
        start=state.match_started_at,
        large_image=large_image or "logo",
        large_text=_truncate("War Thunder"),
        small_image=small_image,
        small_text=small_text,
    )
