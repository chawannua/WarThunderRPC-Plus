"""Kill counting from War Thunder's HUD event feed.

War Thunder serves ``GET /hudmsg?lastEvt=<n>&lastDmg=<n>`` while a match is
running. The response is a JSON object with two arrays, ``events`` and
``damage``. This module never touches HTTP itself -- ``KillFeed`` takes an
injected ``fetch`` callable so it can be exercised without a game running
(see ``wtrpc.wtclient`` for the actual HTTP client the caller wires in).

Everything here follows ``wtclient``'s never-raise philosophy: a malformed
payload, a ``None`` fetch result, or a fetch callable that raises must all be
absorbed silently rather than crashing a polling loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

#: Strips ``^SQUAD_TAG^``-style squadron tags wrapped in carets.
_TAG_RE = re.compile(r"\^[^^]*\^")

#: Collapses any run of whitespace to a single space.
_WHITESPACE_RE = re.compile(r"\s+")

#: Kill-feed verbs, ordered so a longer phrase never gets shadowed by a
#: shorter one that happens to be a substring of it (e.g. "critically
#: damaged" before "damaged"). In practice this never matters because each
#: alternative only matches when it appears literally at the current
#: position, but the explicit order keeps the intent obvious.
_VERBS = (
    "critically damaged",
    "shot down",
    "set afire",
    "destroyed",
    "damaged",
    "wrecked",
)

#: Verbs that mean the target was actually removed from the match.
#: ``wrecked`` was not present in any captured live data -- it is War
#: Thunder's verb for a ship sinking, guessed by analogy with "destroyed"
#: since a wrecked ship is gone from the match just like a destroyed plane.
_KILL_VERBS = frozenset({"destroyed", "shot down", "wrecked"})

#: A HUD damage/kill line, anchored on the vehicle-in-parentheses structure
#: and the verb that follows it rather than on any particular word appearing
#: in the killer's name (which can itself contain "destroyed", parentheses,
#: etc.). Killer name is matched non-greedily up to the first " (" so the
#: vehicle group always lands on the actual vehicle parenthetical.
_EVENT_RE = re.compile(
    r"^(?P<killer>.+?)\s+\((?P<vehicle>[^()]+)\)\s+"
    r"(?P<verb>" + "|".join(re.escape(v) for v in _VERBS) + r")\s+"
    r"(?P<ai>\[ai\]\s+)?(?P<victim>.+)$"
)


def strip_tags(msg: str) -> str:
    """Remove ``^...^`` squadron tags and collapse whitespace."""
    without_tags = _TAG_RE.sub("", msg)
    return _WHITESPACE_RE.sub(" ", without_tags).strip()


@dataclass(frozen=True)
class KillEvent:
    """One parsed line from the ``damage`` array of ``/hudmsg``."""

    killer: str
    killer_vehicle: str
    victim: str
    victim_is_ai: bool
    is_kill: bool


def parse_event(msg: str) -> KillEvent | None:
    """Parse one HUD message into a :class:`KillEvent`.

    Returns ``None`` when the line is not a kill/damage line at all
    (achievements, mission text, join/leave notices, ...).
    """
    cleaned = strip_tags(msg)
    match = _EVENT_RE.match(cleaned)
    if match is None:
        return None

    verb = match.group("verb")

    return KillEvent(
        killer=match.group("killer").strip(),
        killer_vehicle=match.group("vehicle").strip(),
        victim=match.group("victim").strip(),
        victim_is_ai=match.group("ai") is not None,
        is_kill=verb in _KILL_VERBS,
    )


def _normalize(text: str) -> str:
    """Fold a vehicle identifier down to bare alphanumerics for comparison.

    Internal names (``f-4s``) and HUD display names (``F-4S Phantom II``)
    differ in case, punctuation and spacing but share the same letters, so
    lowercasing and stripping everything else lets a substring check line
    them up.
    """
    return re.sub(r"[^a-z0-9]", "", text.lower())


class KillFeed:
    """Tracks confirmed kills by the player across the current match.

    Player identification heuristic
    --------------------------------
    ``/hudmsg`` never names the player directly -- only vehicle types. So the
    player is identified by matching the *vehicle* they are currently flying
    (from ``/indicators``, via :meth:`identify_player`) against the vehicle
    string attached to each killer name seen in the feed so far, using a
    normalised (lowercase, alphanumeric-only) substring match in either
    direction. If exactly one distinct killer name has driven a
    vehicle-string match, that name is taken as the player.

    Honest failure modes:
    - Before any matching event has been seen, no name can be resolved yet,
      so ``player_name`` is ``""`` and ``kills`` is 0.
    - If two different players in the same match both drove vehicles that
      normalise to the same (or a containing) string -- most plainly, two
      teammates flying the exact same aircraft type -- the heuristic cannot
      tell them apart from HUD text alone and deliberately refuses to guess:
      ``player_name`` stays ``""`` and ``kills`` stays 0 until the ambiguity
      resolves itself (e.g. the other pilot switches vehicles and stops
      being a candidate).
    """

    def __init__(
        self, fetch: Callable[[int, int], dict | None], player_name: str = ""
    ) -> None:
        """``player_name`` is the player's exact in-game name.

        Supplying it is strongly preferred, because /hudmsg carries no
        identity whatsoever -- `sender`, `enemy` and `mode` were empty or
        constant across all 144 entries of a live match -- so without it the
        only way to find the player is to guess from the vehicle, and that
        guess is wrong as soon as anyone else is driving the same model. In a
        live test it locked onto a stranger in another M1A2 and reported their
        kills as the player's, which is worse than reporting nothing.
        """
        self._fetch = fetch
        self._last_event_id = 0
        self._last_damage_id = 0
        self._player_vehicle_id = ""
        self._configured_name = (player_name or "").strip()
        self._player_name = self._configured_name
        self._kills = 0
        self._events: list[KillEvent] = []

    @property
    def kills(self) -> int:
        """Number of confirmed kills by the player in the current match."""
        return self._kills

    @property
    def player_name(self) -> str:
        """The identified player's in-game name, or "" if unresolved."""
        return self._player_name

    def identify_player(self, vehicle_id: str) -> None:
        """Record the player's current vehicle and re-resolve their name."""
        self._player_vehicle_id = vehicle_id or ""
        self._resolve_player()

    def poll(self) -> None:
        """Fetch new HUD entries and fold them into the running kill count."""
        try:
            payload = self._fetch(self._last_event_id, self._last_damage_id)
        except Exception:
            return

        if not isinstance(payload, dict):
            return

        events = payload.get("events")
        if not isinstance(events, list):
            events = []

        damage = payload.get("damage")
        if not isinstance(damage, list):
            damage = []

        max_event_id = self._advance_cursor(events, self._last_event_id)

        max_damage_id = self._last_damage_id
        for entry in damage:
            if not isinstance(entry, dict):
                continue

            entry_id = entry.get("id")
            if isinstance(entry_id, int):
                if entry_id <= self._last_damage_id:
                    continue
                if entry_id > max_damage_id:
                    max_damage_id = entry_id

            msg = entry.get("msg")
            if not isinstance(msg, str):
                continue

            parsed = parse_event(msg)
            if parsed is not None:
                self._events.append(parsed)

        self._last_event_id = max_event_id
        self._last_damage_id = max_damage_id
        self._resolve_player()

    @staticmethod
    def _advance_cursor(entries: list, current: int) -> int:
        highest = current
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("id")
            if isinstance(entry_id, int) and entry_id > highest:
                highest = entry_id
        return highest

    def _resolve_player(self) -> None:
        # A configured name is exact and always wins. Vehicle matching is only
        # a fallback, and a poor one: it identifies "whoever is driving the
        # same model as you", which in a live match was a stranger in another
        # M1A2 whose kills were then reported as the player's.
        if self._configured_name:
            self._player_name = self._configured_name
            self._recount()
            return

        target = _normalize(self._player_vehicle_id)
        if not target:
            self._player_name = ""
            self._kills = 0
            return

        candidates = set()
        for event in self._events:
            vehicle = _normalize(event.killer_vehicle)
            if vehicle and (target in vehicle or vehicle in target):
                candidates.add(event.killer)

        self._player_name = next(iter(candidates)) if len(candidates) == 1 else ""

        self._recount()

    def _recount(self) -> None:
        if not self._player_name:
            self._kills = 0
            return
        self._kills = sum(
            1
            for event in self._events
            if event.is_kill and event.killer == self._player_name
        )

    def reset(self) -> None:
        """Clear state for a new match. Ids restart per match, so cursors do too."""
        self._last_event_id = 0
        self._last_damage_id = 0
        self._events = []
        self._kills = 0
        # A configured name survives a new match; a guessed one does not.
        self._player_name = self._configured_name
