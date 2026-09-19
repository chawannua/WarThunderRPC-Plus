"""Entry point: poll War Thunder, assemble a game state, mirror it to Discord.

The state machine below is deliberately written as a flat chain of mutually
exclusive branches. The project this replaces expressed the same logic as
``A and B and C or in_match is False``, and because ``and`` binds tighter than
``or`` in Python that collapsed to ``(...) or (not in_match)`` -- which made
the first branch fire for every out-of-match state and left the test drive
branches permanently unreachable. Keeping the conditions short and disjoint is
the fix.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import logging.handlers
import signal
import sys
import time
from pathlib import Path

from . import config as config_module
from .contacts import looks_like_a_match, nearest_hostile_km
from .flight import FlightAnalyzer
from .ground import read_ground
from .loadout import loadout_label, read_loadout
from .killfeed import KillFeed
from .models import Activity, AirState, Army, Flight, GameState, Ground
from .modes import classify_mode
from .naming import format_vehicle, is_placeholder
from .presence import PresenceManager
from .presence_builder import build_presence
from .weapon_ocr import read_loaded_shell, read_selected_weapon
from .wtclient import WarThunderClient

log = logging.getLogger("wtrpc")

# How long to wait before re-hashing the minimap when identification failed.
MAP_RETRY_INTERVAL_S = 30.0
# Back off polling this far when War Thunder is not answering at all.
OFFLINE_POLL_INTERVAL_S = 5.0
# Consecutive polls that must agree before an activity change is believed.
TRANSITION_CONFIRMATIONS = 2
# Screen-reading the weapon costs far more than an HTTP GET, so it runs on
# its own slower cadence.
WEAPON_READ_INTERVAL_S = 10.0
# Spawning into a match briefly looks exactly like a test flight: the player is
# on a map in a valid vehicle, but /mission.json has not published its
# objectives yet. Measured live, that window lasted about six seconds before
# the objectives arrived and the state corrected itself to IN_MATCH. Rather
# than trust the rate limit to hide the wrong state, demand real evidence
# before believing that a match that was loading is only a test flight.
SLOW_TRANSITIONS: dict[tuple[Activity, Activity], int] = {
    (Activity.LOADING, Activity.TEST_DRIVE): 6,
}
# Activities during which the player is actually flying, so aircraft telemetry
# is meaningful and the rolling flight window should survive the transition.
_FLYING_ACTIVITIES = (Activity.IN_MATCH, Activity.TEST_DRIVE)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)

    # Presence strings contain a middle dot separator, and a Windows console
    # on a non-Latin codepage (cp874 for Thai, cp932 for Japanese, ...) raises
    # UnicodeEncodeError trying to print it. Discord itself is UTF-8 and never
    # had a problem; only our own logging did.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
    console.setLevel(level)
    root.addHandler(console)

    # A rolling file log so users can attach something useful to bug reports.
    try:
        log_dir = config_module.config_path().parent
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "warthunderrpc-plus.log",
            maxBytes=512_000,
            backupCount=2,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        file_handler.setLevel(logging.DEBUG)
        root.addHandler(file_handler)
    except OSError as exc:
        log.debug("File logging unavailable: %s", exc)


def _parse_army(raw: object) -> Army:
    value = str(raw or "").strip().lower()
    for army in (Army.AIR, Army.TANK, Army.SHIP):
        if value == army.value:
            return army
    return Army.UNKNOWN


def _read_flight(
    state: dict | None,
    indicators: dict | None,
    nearest_hostile_km: float | None = None,
) -> Flight:
    """Distil an aircraft telemetry sample. Missing keys stay ``None``."""

    def number(source: dict | None, key: str) -> float | None:
        if not source:
            return None
        value = source.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    return Flight(
        mach=number(state, "M"),
        ias_kph=number(state, "IAS, km/h"),
        tas_kph=number(state, "TAS, km/h"),
        altitude_m=number(state, "H, m"),
        load_factor=number(state, "Ny"),
        vertical_speed_ms=number(state, "Vy, m/s"),
        heading_deg=number(indicators, "compass"),
        roll_deg=number(indicators, "aviahorizon_roll"),
        throttle_pct=number(state, "throttle 1, %"),
        nearest_hostile_km=nearest_hostile_km,
    )


def _primary_objective(mission: dict | None) -> str:
    if not mission:
        return ""
    objectives = mission.get("objectives")
    if not isinstance(objectives, list):
        return ""
    for objective in objectives:
        if isinstance(objective, dict) and objective.get("primary"):
            text = objective.get("text")
            if isinstance(text, str) and text.strip():
                return text
    return ""


class Poller:
    """Holds the bits of state that have to survive between polls."""

    def __init__(self, cfg: config_module.Config) -> None:
        self.cfg = cfg
        self.client = WarThunderClient(
            connect_timeout=cfg.connect_timeout, read_timeout=cfg.read_timeout
        )
        self.analyzer = FlightAnalyzer()
        self.killfeed = KillFeed(
            lambda e, d: self.client.hudmsg(e, d), player_name=cfg.player_name
        )
        self.activity = Activity.UNKNOWN
        self.activity_since: int | None = None
        self.map_name = ""
        self.map_checked_at = 0.0
        self.was_in_map = False
        # Last known good readings, carried forward across a dropped request.
        self.last_in_map = False
        self.last_in_match = False
        # A transition must be seen on this many consecutive polls before it is
        # believed, so a single stalled response cannot bounce the state.
        self._candidate: Activity | None = None
        self._candidate_count = 0
        self.last_state: GameState | None = None
        # The battle type belongs to the MATCH, not to whatever the player is
        # sitting in. Spawning a helicopter in a Ground RB match must not
        # relabel it "Air Domination" -- observed live on Mozdok, where the
        # objective text never changed but the label followed the vehicle.
        self.match_mode = ""
        self.weapon = ""
        self.shell = ""
        self.loadout = ""
        self._loadout_vehicle = ""
        self._weapon_checked_at = 0.0
        self._shell_checked_at = 0.0

    def _resolve_map(self, in_map: bool) -> str:
        """Identify the map, but only when it can actually have changed.

        Hashing the minimap on every poll -- as the original did, downloading
        the image every three seconds even while sitting in the hangar -- is
        pure waste. The map cannot change mid-match.
        """
        if not in_map or not self.cfg.show_map:
            self.map_name = ""
            self.was_in_map = in_map
            return ""

        entered_map = not self.was_in_map
        self.was_in_map = True
        now = time.monotonic()

        needs_lookup = entered_map or (
            not self.map_name and (now - self.map_checked_at) >= MAP_RETRY_INTERVAL_S
        )
        if not needs_lookup:
            return self.map_name

        self.map_checked_at = now
        image = self.client.map_image()
        self.map_name = self.client.identify_map(image) if image is not None else ""
        if self.map_name:
            log.debug("Map identified as %s", self.map_name)
        return self.map_name

    def _read_weapon(self) -> str:
        """Read the selected weapon off the screen, on a slow cadence.

        A screen grab plus OCR costs far more than an HTTP GET, and nobody
        changes weapon every three seconds, so this runs at its own interval
        and keeps the previous answer in between. A frame where the HUD block
        is not drawn -- it comes and goes with the camera view -- keeps the
        last good reading rather than blanking the presence.
        """
        now = time.monotonic()
        if (now - self._weapon_checked_at) < WEAPON_READ_INTERVAL_S:
            return self.weapon
        self._weapon_checked_at = now

        try:
            box = tuple(int(v) for v in self.cfg.weapon_region.split(","))
            if len(box) != 4:
                raise ValueError(self.cfg.weapon_region)
        except (ValueError, AttributeError):
            log.debug("Bad weapon_region %r; using the default box",
                      getattr(self.cfg, "weapon_region", None))
            box = (0, 0, 900, 600)

        name = read_selected_weapon(box)
        if name:
            log.debug("Weapon read from the HUD: %s", name)
            return name
        return self.weapon

    def _match_mode(self, objective: str, army: Army, activity: Activity) -> str:
        """Label the match, and keep that label for as long as it lasts.

        The mode is latched on the first poll of a match that can name it,
        because the battle type cannot change mid-match while the player's
        vehicle very much can. Re-deriving it every poll made a Ground RB
        battle read as "Air Domination" the moment a helicopter spawned.
        """
        if activity in (Activity.HANGAR, Activity.UNKNOWN):
            self.match_mode = ""
            return ""

        if not self.match_mode:
            candidate = classify_mode(objective, army)
            # An unknown army yields a bare label with no Air/Ground prefix,
            # which is worth waiting a poll or two for rather than latching.
            if candidate and army is not Army.UNKNOWN:
                self.match_mode = candidate
            return candidate

        return self.match_mode

    def _read_shell(self) -> str:
        """Read the loaded shell off the gunner sight, on the slow cadence.

        The name is only drawn in the gunner view, so a frame taken while
        driving reads nothing; the previous answer is kept rather than
        blanking the presence.
        """
        now = time.monotonic()
        if (now - self._shell_checked_at) < WEAPON_READ_INTERVAL_S:
            return self.shell
        self._shell_checked_at = now

        # The shell name is drawn in the gunner sight, not in the corner
        # block the aircraft HUD uses, and the sight fills the screen. Scan
        # the whole frame rather than guess a box: the green isolation leaves
        # very little behind, and read_shell_name only accepts known shell
        # names, so a wider search costs little and misses nothing.
        name = read_loaded_shell(None)
        if name:
            log.debug("Shell read from the sight: %s", name)
            return name
        return self.shell

    def _weapon_box(self) -> tuple[int, int, int, int]:
        try:
            box = tuple(int(v) for v in self.cfg.weapon_region.split(","))
            if len(box) == 4:
                return box  # type: ignore[return-value]
        except (ValueError, AttributeError):
            pass
        log.debug("Bad weapon_region %r; using the default box",
                  getattr(self.cfg, "weapon_region", None))
        return (0, 0, 900, 600)

    def _set_activity(self, activity: Activity) -> None:
        """Commit an activity change, but only once it has been seen twice.

        The original set its timer once at process start, so the counter showed
        how long the helper had been open rather than how long the match had
        been running. Resetting on every observed change fixed that but created
        a worse failure: War Thunder's HTTP server stalls under load, one
        dropped /map_info.json reads as "left the map", and the Discord timer
        would snap back to 00:00 in the middle of a match. Requiring agreement
        across consecutive polls costs one poll of latency on a real transition
        and absorbs the spurious ones entirely.
        """
        if activity is self.activity:
            self._candidate = None
            self._candidate_count = 0
            return

        if activity is self._candidate:
            self._candidate_count += 1
        else:
            self._candidate = activity
            self._candidate_count = 1

        needed = SLOW_TRANSITIONS.get(
            (self.activity, activity), TRANSITION_CONFIRMATIONS
        )
        if self._candidate_count < needed:
            log.debug(
                "Ignoring unconfirmed %s -> %s (%d/%d)",
                self.activity.value,
                activity.value,
                self._candidate_count,
                needed,
            )
            return

        log.info("State: %s -> %s", self.activity.value, activity.value)
        previous = self.activity
        self.activity = activity
        self._candidate = None
        self._candidate_count = 0
        self.activity_since = int(time.time())

        # Keep the telemetry window across match <-> test drive, which are both
        # flying; only a return to the hangar or a load screen really ends it.
        if activity not in _FLYING_ACTIVITIES or previous not in _FLYING_ACTIVITIES:
            self.analyzer.reset()
        if activity is Activity.IN_MATCH:
            # HUD message ids restart with each match, so a stale cursor would
            # skip the whole new feed and report zero kills all match.
            self.killfeed.reset()

    def poll(self) -> GameState | None:
        """Build one snapshot, or ``None`` when War Thunder is unreachable.

        A ``None`` from the client means either "the game is gone" or "the game
        answered with an empty body", and those must not be confused: the
        second happens transiently during scene changes, and treating it as the
        first would clear the presence and drop to the offline poll interval
        while the player is mid-match. ``client.available`` tells them apart.
        """
        indicators = self.client.indicators()
        if indicators is None:
            if not self.client.available:
                self.was_in_map = False
                self.map_name = ""
                self.last_in_map = False
                self.last_in_match = False
                self._set_activity(Activity.UNKNOWN)
                return None
            # Reachable but momentarily empty: keep the last state rather than
            # announcing that War Thunder closed.
            log.debug("Empty /indicators while the game is still reachable")
            return self.last_state

        map_info = self.client.map_info()
        # A failed request is not evidence that the player left the map.
        in_map = (
            bool(map_info.get("valid")) if map_info is not None else self.last_in_map
        )
        self.last_in_map = in_map
        vehicle_valid = bool(indicators.get("valid"))

        raw_type = str(indicators.get("type") or "")
        army = _parse_army(indicators.get("army"))
        vehicle_id, vehicle_name = format_vehicle(raw_type)
        placeholder = is_placeholder(raw_type)

        mission = self.client.mission()
        if mission is None and not self.client.available:
            in_match = self.last_in_match
        else:
            in_match = bool(_primary_objective(mission))
        objective = _primary_objective(mission)
        self.last_in_match = in_match

        # Resolve the map before deciding, because whether the minimap matches
        # a known battle map is itself evidence about what the player is doing.
        map_name = self._resolve_map(in_map)

        # /mission.json is authoritative about being in a match but not prompt:
        # measured live, a battle ran three full minutes before publishing any
        # objective, and the player was reported as being in a test flight the
        # whole time. Two independent corroborations close that gap.
        #
        # A recognised map name is one, but it leans on a hash table of maps
        # known in 2024, so anything newer still falls through -- which is
        # exactly what happened on ordinary maps after the first attempt.
        #
        # Minimap markers are the one that does not age: respawn bases,
        # capture zones and defending points exist in a battle and nowhere
        # else, whatever the map is called.
        markers = self.client.map_obj() if in_map else None
        battle_markers = looks_like_a_match(markers)

        # Mutually exclusive, evaluated top to bottom. No boolean gymnastics.
        if not in_map:
            activity = Activity.HANGAR
        elif not vehicle_valid or placeholder:
            activity = Activity.LOADING
        elif in_match or map_name or battle_markers:
            activity = Activity.IN_MATCH
        else:
            activity = Activity.TEST_DRIVE

        self._set_activity(activity)

        # Test Flight is how most people first try this app, and /state serves
        # full telemetry there, so gating the headline feature on IN_MATCH hid
        # it exactly when a new user was looking for it.
        flight = Flight()
        ground = Ground()
        air_state = AirState.UNKNOWN
        if army in (Army.TANK, Army.SHIP) and activity in _FLYING_ACTIVITIES:
            ground = read_ground(indicators)
            # The save file only changes between sorties, so read it once per
            # vehicle rather than on every poll.
            if vehicle_id != self._loadout_vehicle:
                self._loadout_vehicle = vehicle_id
                self.loadout = loadout_label(read_loadout(vehicle_id))
                if self.loadout:
                    log.debug("Loadout for %s: %s", vehicle_id, self.loadout)
            ground = dataclasses.replace(ground, loadout=self.loadout)
            if self.cfg.show_weapon:
                self.shell = self._read_shell()
                ground = dataclasses.replace(ground, shell=self.shell)
        elif activity not in _FLYING_ACTIVITIES:
            self.shell = ""
        if army is Army.AIR and activity in _FLYING_ACTIVITIES:
            # The minimap tells us whether anyone is actually out there, which
            # is the only way to tell a turning fight from hard aerobatics.
            hostile_km = nearest_hostile_km(markers, map_info)
            flight = _read_flight(self.client.state(), indicators, hostile_km)
            self.analyzer.add(flight, time.monotonic())
            air_state = self.analyzer.state()

        if self.cfg.show_weapon and army is Army.AIR and activity in _FLYING_ACTIVITIES:
            self.weapon = self._read_weapon()
        elif activity not in _FLYING_ACTIVITIES:
            self.weapon = ""

        kills = 0
        if activity is Activity.IN_MATCH and self.cfg.show_kills:
            self.killfeed.identify_player(vehicle_id)
            self.killfeed.poll()
            kills = self.killfeed.kills

        state = GameState(
            activity=activity,
            army=army,
            vehicle_id=vehicle_id,
            vehicle_name=vehicle_name,
            map_name=map_name,
            mode=self._match_mode(objective, army, activity),
            flight=flight,
            ground=ground,
            air_state=air_state,
            weapon=self.weapon,
            kills=kills,
            match_started_at=self.activity_since,
        )
        self.last_state = state
        return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="warthunderrpc-plus",
        description="Discord Rich Presence for War Thunder, with flight telemetry.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-c", "--config", type=Path, default=None, help="config file path")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    cfg = config_module.load(args.config)
    log.info("WarThunderRPC-Plus starting (poll %.1fs)", cfg.poll_interval)

    poller = Poller(cfg)
    presence = PresenceManager(cfg.client_id, cfg.min_update_interval)

    running = True

    def stop(_signum, _frame) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    try:
        signal.signal(signal.SIGTERM, stop)
    except (AttributeError, ValueError):
        pass  # SIGTERM is not always available on Windows

    warned_offline = False
    try:
        while running:
            # This runs unattended for hours next to a game. An unforeseen
            # exception from any layer should cost one poll, not the session.
            try:
                state = poller.poll()
            except Exception:
                log.exception("Poll failed; continuing")
                time.sleep(cfg.poll_interval)
                continue

            if state is None:
                if not warned_offline:
                    log.info("Waiting for War Thunder... (is the game running?)")
                    warned_offline = True
                presence.clear()
                time.sleep(OFFLINE_POLL_INTERVAL_S)
                continue

            if warned_offline:
                log.info("War Thunder detected")
                warned_offline = False

            presence.update(
                build_presence(
                    state,
                    show_map=cfg.show_map,
                    show_vehicle_image=cfg.show_vehicle_image,
                    show_flight_data=cfg.show_flight_data,
                    dogfight_detection=cfg.dogfight_detection,
                    show_kills=cfg.show_kills,
                    large_image=cfg.large_image,
                )
            )
            time.sleep(cfg.poll_interval)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Shutting down")
        presence.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
