"""Ground vehicle state, distilled from ``/indicators``.

The aircraft side of this app reads ``/state``, but that endpoint is aviation
only -- in a tank it answers ``{"valid": false}`` and nothing else. Everything
below therefore comes from ``/indicators``, whose field set changes completely
depending on what the player is driving: 79 fields in an F-16C, 23 in an
M1A2.

What is NOT here, and cannot be: the type of shell loaded. It is absent from
the API, and unlike the aircraft weapon HUD it cannot be read off the screen
either -- the tank ammo selector is drawn as icons with counts
(``[1] 16  [2] 5  [G] 4/26``) and never spells out "APFSDS" or "M829A2"
anywhere in battle. Those names appear only on the loadout screen.

Two captures of the same M1A2, one healthy and one badly shot up, are what
established the semantics here, and they are worth recording because the
field names are actively misleading::

    healthy:  first_stage_ammo=18  crew_current=4  crew_total=4
              gunner_state=0  driver_state=0
              (breach_dead, v_drive_broken and h_drive_dead absent entirely)

    damaged:  first_stage_ammo=11  crew_current=1  crew_total=4
              gunner_state=1  driver_state=1
              breach_dead=1  v_drive_broken=1  h_drive_dead=1

So a damage field is ABSENT while its component is fine and only appears once
it breaks. That pattern held across every capture and is what this module
relies on: a component counts as broken only when its field is present AND
truthy, so misreading the convention can only fail to report damage, never
invent it.

``gunner_state`` and ``driver_state`` are NOT used, and the reason is worth
keeping. Read together, the two M1A2 captures suggested 1 meant "out of
action". An HSTV-L then reported 1 for both while carrying a full crew of
three, which that reading cannot explain. One pair of samples was not enough
to establish the semantics, and "Gunner down" printed about an intact crew is
worse than printing nothing.
"""

from __future__ import annotations

import math

from wtrpc.models import Ground

#: Fields that exist only while the named component is broken.
_DAMAGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("breach_dead", "Breech destroyed"),
    ("v_drive_broken", "Vertical drive out"),
    ("h_drive_dead", "Horizontal drive out"),
)

#: Deliberately empty. ``gunner_state`` and ``driver_state`` were originally
#: read as "1 means this crewman is out", inferred from a single pair of M1A2
#: captures where the damaged one showed 1 alongside 1 of 4 crew remaining.
#: An HSTV-L then reported 1 for both while carrying a FULL crew of 3, which
#: contradicts that reading outright. Whatever the field encodes -- a state
#: enum, a replacement in progress, something vehicle-specific -- it is not
#: simply "dead", and a status line that says "Gunner down" about an intact
#: crew is worse than saying nothing. Restore an entry here only with
#: evidence from several vehicles, not one.
_CREW_FIELDS: tuple[tuple[str, str], ...] = ()


def _number(source: dict | None, key: str) -> float | None:
    if not source:
        return None
    value = source.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    # NaN/inf pass the isinstance check but blow up ``int()`` below (and mean
    # nothing sensible for a rounds count or a crew size anyway), so they are
    # treated the same as any other unreadable value: unknown, not a crash.
    if not math.isfinite(result):
        return None
    return result


def _is_broken(source: dict, key: str) -> bool:
    """True only when the field is present and truthy.

    Absence means healthy, which is how the game reports it, and it also
    means an unknown convention degrades into silence rather than into a
    false alarm on someone's Discord status.
    """
    value = _number(source, key)
    return value is not None and value >= 1.0


def read_ground(indicators: dict | None) -> Ground:
    """Distil a ground vehicle sample. Missing fields stay ``None``/empty."""
    if not indicators:
        return Ground()

    crew_alive = _number(indicators, "crew_current")
    crew_total = _number(indicators, "crew_total")

    damage: list[str] = []
    for key, label in _CREW_FIELDS:
        if _is_broken(indicators, key):
            damage.append(label)
    for key, label in _DAMAGE_FIELDS:
        if _is_broken(indicators, key):
            damage.append(label)

    return Ground(
        ready_ammo=_number(indicators, "first_stage_ammo"),
        crew_alive=int(crew_alive) if crew_alive is not None else None,
        crew_total=int(crew_total) if crew_total is not None else None,
        speed_kph=_number(indicators, "speed"),
        damage=tuple(damage),
    )


def ammo_label(ground: Ground) -> str:
    """Rounds left in the ready rack, or "" when unknown."""
    if ground.ready_ammo is None:
        return ""
    rounds = int(ground.ready_ammo)
    if rounds < 0:
        return ""
    return f"{rounds} round" + ("s" if rounds != 1 else "")


def crew_label(ground: Ground) -> str:
    """Crew still in the fight, shown only once someone is missing.

    A full crew is the normal state and saying so adds nothing; a depleted
    one is the interesting bit.
    """
    if ground.crew_alive is None or ground.crew_total is None:
        return ""
    if ground.crew_total <= 0 or ground.crew_alive >= ground.crew_total:
        return ""
    return f"{ground.crew_alive}/{ground.crew_total} crew"


def damage_label(ground: Ground) -> str:
    """The most serious damage, or "" when nothing is broken.

    Only one item is reported: the status line is already carrying the
    vehicle, the mode and the map, and a tank that has lost three modules has
    usually lost the engagement too.
    """
    return ground.damage[0] if ground.damage else ""
