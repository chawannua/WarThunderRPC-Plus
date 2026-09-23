"""Tests for wtrpc.__main__ — the polling loop and state machine.

Every network call goes through a mocked ``WarThunderClient`` (never a real
``requests`` call), so these tests pass whether or not War Thunder happens to
be running.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from wtrpc import config as config_module
from wtrpc.__main__ import (
    TRANSITION_CONFIRMATIONS,
    Poller,
    _parse_army,
    _primary_objective,
    _read_flight,
)
from wtrpc.models import Activity, Army, Flight
from wtrpc.wtclient import WarThunderClient


def make_poller(**config_overrides) -> Poller:
    cfg = config_module.Config(**config_overrides)
    return Poller(cfg)


def _client_mock(
    *,
    in_map: bool,
    vehicle_valid: bool,
    vehicle_type: str = "fw190a5",
    army: str = "air",
    primary_text: str = "",
    state_data: dict | None = None,
    available: bool = True,
) -> MagicMock:
    """Build a mocked WarThunderClient that drives the state machine deterministically."""
    client = MagicMock(spec=WarThunderClient)
    client.available = available
    client.indicators.return_value = {
        "valid": vehicle_valid,
        "type": vehicle_type,
        "army": army,
        "compass": 10.0,
        "aviahorizon_roll": 0.0,
    }
    client.map_info.return_value = {"valid": in_map}
    if primary_text:
        client.mission.return_value = {
            "objectives": [{"text": primary_text, "primary": True}]
        }
    else:
        client.mission.return_value = {"objectives": []}
    client.state.return_value = state_data if state_data is not None else {}
    client.map_image.return_value = None
    client.identify_map.return_value = ""
    return client


def _drive(poller: Poller, n: int = TRANSITION_CONFIRMATIONS):
    """Poll ``n`` times (default: enough to commit a transition) and return the last state."""
    state = None
    for _ in range(n):
        state = poller.poll()
    return state


# ---------------------------------------------------------------------------
# 9. _parse_army
# ---------------------------------------------------------------------------


class TestParseArmy:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("air", Army.AIR),
            ("tank", Army.TANK),
            ("ship", Army.SHIP),
            (None, Army.UNKNOWN),
            ("", Army.UNKNOWN),
            ("AIR ", Army.AIR),  # whitespace/case tolerant
            ("helicopter", Army.UNKNOWN),
        ],
    )
    def test_parse_army_maps_values(self, raw, expected):
        assert _parse_army(raw) is expected


# ---------------------------------------------------------------------------
# 10. _read_flight
# ---------------------------------------------------------------------------


class TestReadFlight:
    def test_reads_the_exact_live_key_strings(self):
        state = {
            "M": 0.85,
            "IAS, km/h": 450.0,
            "TAS, km/h": 500.0,
            "H, m": 3000.0,
            "Ny": 2.5,
            "Vy, m/s": -5.0,
        }
        indicators = {"compass": 134.2, "aviahorizon_roll": -12.5}

        flight = _read_flight(state, indicators)

        assert flight.mach == 0.85
        assert flight.ias_kph == 450.0
        assert flight.tas_kph == 500.0
        assert flight.altitude_m == 3000.0
        assert flight.load_factor == 2.5
        assert flight.vertical_speed_ms == -5.0
        assert flight.heading_deg == 134.2
        assert flight.roll_deg == -12.5

    def test_tolerates_every_key_being_absent(self):
        assert _read_flight({}, {}) == Flight()

    def test_tolerates_none_state_and_indicators(self):
        assert _read_flight(None, None) == Flight()

    def test_does_not_treat_a_bool_as_a_number(self):
        state = {"M": True, "IAS, km/h": False}
        indicators = {"compass": True}

        flight = _read_flight(state, indicators)

        assert flight.mach is None
        assert flight.ias_kph is None
        assert flight.heading_deg is None


# ---------------------------------------------------------------------------
# 11. _primary_objective
# ---------------------------------------------------------------------------


class TestPrimaryObjective:
    def test_none_mission_returns_empty(self):
        assert _primary_objective(None) == ""

    def test_objectives_null_returns_empty(self):
        # The live game really sends this exact shape while a mission loads.
        mission = {"objectives": None, "status": "running"}
        assert _primary_objective(mission) == ""

    def test_empty_objectives_list_returns_empty(self):
        assert _primary_objective({"objectives": []}) == ""

    def test_no_entry_marked_primary_returns_empty(self):
        mission = {"objectives": [{"text": "Secondary", "primary": False}]}
        assert _primary_objective(mission) == ""

    def test_primary_entry_returns_its_text(self):
        mission = {
            "objectives": [
                {"text": "Secondary", "primary": False},
                {"text": "Destroy the enemy's ground forces", "primary": True},
            ]
        }
        assert _primary_objective(mission) == "Destroy the enemy's ground forces"


# ---------------------------------------------------------------------------
# 12. State machine: reachability, mutual exclusivity, confirmation delay
# ---------------------------------------------------------------------------


class TestStateMachine:
    def test_all_four_activities_are_reachable_and_mutually_exclusive(self):
        scenarios = [
            (dict(in_map=False, vehicle_valid=False), Activity.HANGAR),
            (
                dict(in_map=True, vehicle_valid=True, vehicle_type="dummy_plane"),
                Activity.LOADING,
            ),
            (
                dict(in_map=True, vehicle_valid=False, vehicle_type="fw190a5"),
                Activity.LOADING,
            ),
            (
                dict(
                    in_map=True,
                    vehicle_valid=True,
                    vehicle_type="fw190a5",
                    primary_text="",
                ),
                Activity.TEST_DRIVE,
            ),
            (
                dict(
                    in_map=True,
                    vehicle_valid=True,
                    vehicle_type="fw190a5",
                    primary_text="Destroy the enemy's forces",
                ),
                Activity.IN_MATCH,
            ),
        ]

        for kwargs, expected in scenarios:
            poller = make_poller()
            poller.client = _client_mock(**kwargs)
            state = _drive(poller)
            assert state.activity == expected, kwargs
            assert poller.activity == expected, kwargs

    def test_invalid_vehicle_wins_over_an_in_progress_match(self):
        """An invalid/placeholder vehicle means LOADING even if the mission
        already reports a primary objective -- the branches are evaluated
        top to bottom and are mutually exclusive."""
        poller = make_poller()
        poller.client = _client_mock(
            in_map=True,
            vehicle_valid=False,
            vehicle_type="fw190a5",
            primary_text="Some objective",
        )

        state = _drive(poller)

        assert state.activity == Activity.LOADING

    def test_a_single_poll_does_not_commit_a_transition(self):
        poller = make_poller()
        poller.client = _client_mock(in_map=False, vehicle_valid=False)

        poller.poll()

        # TRANSITION_CONFIRMATIONS (2) agreeing polls are required to commit.
        assert poller.activity == Activity.UNKNOWN
        assert poller.activity_since is None

    def test_unconfirmed_transition_does_not_leak_into_the_reported_state(self):
        """The debounce in ``_set_activity`` used to protect only the timer:
        the raw, unconfirmed per-poll activity still drove ``GameState``,
        ``_match_mode``, and kill counting, so a single stray poll (e.g. one
        dropped /map_info request making the map briefly look empty) could
        report a match as ended and stop counting kills for a poll even
        though the committed activity had not actually changed.
        """
        client = _client_mock(
            in_map=True,
            vehicle_valid=True,
            vehicle_type="fw190a5",
            primary_text="Destroy the enemy's forces",
        )
        poller = make_poller(show_kills=True)
        poller.client = client
        poller.killfeed.identify_player = lambda vid: None
        poller.killfeed.poll = lambda: None

        state = _drive(poller)
        assert state.activity == Activity.IN_MATCH
        assert poller.activity == Activity.IN_MATCH

        # Set after the match-entry transition commits, since committing into
        # IN_MATCH itself resets the killfeed (new match, ids restart).
        poller.killfeed._kills = 5

        # One stray poll reporting HANGAR (e.g. in_map flickered false for a
        # single request). This must not be believed yet -- committed
        # activity requires TRANSITION_CONFIRMATIONS agreeing polls.
        client.map_info.return_value = {"valid": False}

        next_state = poller.poll()

        assert poller.activity == Activity.IN_MATCH  # unconfirmed, unchanged
        assert next_state.activity == Activity.IN_MATCH  # reported state agrees
        assert next_state.kills == 5  # kill counting still gated on committed
        assert next_state.mode  # match mode still latched


# ---------------------------------------------------------------------------
# 13. The timer reset bug
# ---------------------------------------------------------------------------


class TestTimerResetBug:
    def test_a_single_dropped_map_info_does_not_reset_the_match_timer(self):
        client = _client_mock(
            in_map=True,
            vehicle_valid=True,
            vehicle_type="fw190a5",
            army="air",
            primary_text="Destroy the enemy's forces",
            state_data={"M": 0.6, "IAS, km/h": 400.0},
        )
        poller = make_poller()
        poller.client = client

        # Commit into IN_MATCH (needs two consecutive agreeing polls).
        state = _drive(poller)
        assert state.activity == Activity.IN_MATCH
        started_at = poller.activity_since
        assert started_at is not None

        # A few more stable polls.
        for _ in range(3):
            state = poller.poll()
            assert state.activity == Activity.IN_MATCH
            assert state.match_started_at == started_at

        samples_before = len(poller.analyzer._samples)  # noqa: SLF001 (white-box check)

        # Exactly one poll where /map_info.json drops out, but the game is
        # still reachable (client.available stays True). This must not be
        # mistaken for leaving the map.
        client.map_info.return_value = None
        state = poller.poll()

        assert state.activity == Activity.IN_MATCH
        assert state.match_started_at == started_at
        assert poller.activity_since == started_at
        # The rolling flight window kept growing rather than being reset.
        assert len(poller.analyzer._samples) == samples_before + 1  # noqa: SLF001

        # Recovery: map_info comes back, everything stays stable.
        client.map_info.return_value = {"valid": True}
        for _ in range(3):
            state = poller.poll()
            assert state.activity == Activity.IN_MATCH
            assert state.match_started_at == started_at


# ---------------------------------------------------------------------------
# 14. Game closed vs. empty response
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_two_consecutive_failed_indicators_requests_means_the_game_is_gone(self):
        poller = make_poller()
        client = MagicMock(spec=WarThunderClient)
        client.available = False
        client.indicators.return_value = None
        poller.client = client

        poller.poll()
        assert poller.poll() is None
        assert poller.activity == Activity.UNKNOWN

    def test_a_single_failed_indicators_request_does_not_clear_presence(self):
        # A stalled/garbled /indicators response used to be treated the same
        # as "the game closed", blanking the presence for one bad poll in the
        # middle of a match.
        client = _client_mock(
            in_map=True,
            vehicle_valid=True,
            vehicle_type="fw190a5",
            primary_text="Destroy the enemy's forces",
        )
        poller = make_poller()
        poller.client = client

        state = _drive(poller)
        assert state.activity == Activity.IN_MATCH

        client.indicators.return_value = None
        client.available = False

        result = poller.poll()

        assert result is state  # kept the last good snapshot
        assert poller.activity == Activity.IN_MATCH  # not reset after one stall

    def test_a_failed_request_that_recovers_does_not_carry_the_failure_forward(self):
        client = _client_mock(
            in_map=True,
            vehicle_valid=True,
            vehicle_type="fw190a5",
            primary_text="Destroy the enemy's forces",
        )
        poller = make_poller()
        poller.client = client
        state = _drive(poller)

        client.indicators.return_value = None
        client.available = False
        poller.poll()  # one failure

        client.indicators.return_value = {
            "valid": True,
            "type": "fw190a5",
            "army": "air",
            "compass": 10.0,
            "aviahorizon_roll": 0.0,
        }
        client.available = True
        recovered = poller.poll()
        assert recovered is not None
        assert poller.activity == Activity.IN_MATCH

        # A single new failure after the recovery must not immediately go
        # offline either -- the counter must have reset on the good poll.
        client.indicators.return_value = None
        client.available = False
        result = poller.poll()
        assert result is state or result is recovered
        assert poller.activity == Activity.IN_MATCH

    def test_indicators_none_but_available_is_not_treated_as_offline(self):
        client = _client_mock(
            in_map=True,
            vehicle_valid=True,
            vehicle_type="fw190a5",
            primary_text="Destroy the enemy's forces",
        )
        poller = make_poller()
        poller.client = client

        state = _drive(poller)
        assert state.activity == Activity.IN_MATCH

        client.indicators.return_value = None
        client.available = True

        result = poller.poll()

        assert result is not None
        assert result is state  # the last good snapshot, not a fresh/blank one
        assert poller.activity == Activity.IN_MATCH  # unchanged


# ---------------------------------------------------------------------------
# 15. Aircraft telemetry gating
# ---------------------------------------------------------------------------


class TestAircraftTelemetryGating:
    def test_telemetry_is_read_during_test_drive(self):
        client = _client_mock(
            in_map=True,
            vehicle_valid=True,
            vehicle_type="fw190a5",
            army="air",
            primary_text="",
            state_data={"M": 0.5, "IAS, km/h": 300.0},
        )
        poller = make_poller()
        poller.client = client

        state = _drive(poller)

        assert state.activity == Activity.TEST_DRIVE
        assert state.flight.mach == 0.5
        client.state.assert_called()

    def test_telemetry_is_read_during_in_match(self):
        client = _client_mock(
            in_map=True,
            vehicle_valid=True,
            vehicle_type="fw190a5",
            army="air",
            primary_text="Destroy the enemy's forces",
            state_data={"M": 0.9, "IAS, km/h": 600.0},
        )
        poller = make_poller()
        poller.client = client

        state = _drive(poller)

        assert state.activity == Activity.IN_MATCH
        assert state.flight.mach == 0.9

    def test_telemetry_is_never_read_for_a_tank(self):
        client = _client_mock(
            in_map=True,
            vehicle_valid=True,
            vehicle_type="us_m1_abrams",
            army="tank",
            primary_text="Destroy the enemy's forces",
            state_data={"M": 0.9},  # would leak through if wrongly read
        )
        poller = make_poller()
        poller.client = client

        state = _drive(poller)

        assert state.army == Army.TANK
        assert state.flight == Flight()
        client.state.assert_not_called()

    def test_telemetry_is_never_read_for_a_ship(self):
        client = _client_mock(
            in_map=True,
            vehicle_valid=True,
            vehicle_type="us_destroyer",
            army="ship",
            primary_text="Destroy the enemy's forces",
            state_data={"M": 0.9},
        )
        poller = make_poller()
        poller.client = client

        state = _drive(poller)

        assert state.army == Army.SHIP
        assert state.flight == Flight()
        client.state.assert_not_called()


# ---------------------------------------------------------------------------
# 16. _resolve_map
# ---------------------------------------------------------------------------


class TestResolveMap:
    def test_fetches_the_map_image_on_entering_a_map(self):
        poller = make_poller()
        client = MagicMock(spec=WarThunderClient)
        client.map_image.return_value = "FAKE_IMAGE"
        client.identify_map.return_value = "Abandoned Factory"
        poller.client = client

        name = poller._resolve_map(True)  # noqa: SLF001 (white-box check)

        assert name == "Abandoned Factory"
        client.map_image.assert_called_once()
        client.identify_map.assert_called_once_with("FAKE_IMAGE")

    def test_does_not_refetch_while_still_on_the_same_map(self):
        poller = make_poller()
        client = MagicMock(spec=WarThunderClient)
        client.map_image.return_value = "FAKE_IMAGE"
        client.identify_map.return_value = "Abandoned Factory"
        poller.client = client

        poller._resolve_map(True)  # noqa: SLF001
        poller._resolve_map(True)  # noqa: SLF001
        poller._resolve_map(True)  # noqa: SLF001

        client.map_image.assert_called_once()  # only the initial fetch

    def test_clears_the_name_on_leaving_the_map(self):
        poller = make_poller()
        client = MagicMock(spec=WarThunderClient)
        client.map_image.return_value = "FAKE_IMAGE"
        client.identify_map.return_value = "Abandoned Factory"
        poller.client = client

        poller._resolve_map(True)  # noqa: SLF001
        name = poller._resolve_map(False)  # noqa: SLF001

        assert name == ""
        assert poller.map_name == ""

    def test_show_map_false_never_fetches(self):
        poller = make_poller(show_map=False)
        client = MagicMock(spec=WarThunderClient)
        poller.client = client

        name = poller._resolve_map(True)  # noqa: SLF001

        assert name == ""
        client.map_image.assert_not_called()


class TestSpawnIntoMatchIsNotMistakenForTestFlight:
    """Spawning into a match briefly looks identical to a test flight.

    For a few seconds after the load screen the player is on a map in a valid
    vehicle while /mission.json has not published its objectives, which reads
    as TEST_DRIVE. Measured live, that window was about six seconds. The rate
    limit happened to swallow it, but correctness must not depend on that.
    """

    @staticmethod
    def _poller(monkeypatch):
        from wtrpc.config import Config
        from wtrpc.__main__ import Poller

        poller = Poller(Config())
        poller.client = MagicMock(spec=WarThunderClient)
        poller.client.available = True
        poller.client.map_image.return_value = None
        poller.client.identify_map.return_value = ""
        poller.client.map_obj.return_value = []
        poller.client.state.return_value = {"valid": True, "M": 0.8}
        return poller

    def _feed(self, poller, *, objectives, polls):
        poller.client.indicators.return_value = {
            "valid": True,
            "army": "air",
            "type": "f-16c",
        }
        poller.client.map_info.return_value = {"valid": True}
        poller.client.mission.return_value = {"objectives": objectives}
        for _ in range(polls):
            poller.poll()

    def test_objectiveless_spawn_does_not_flip_to_test_drive(self, monkeypatch):
        poller = self._poller(monkeypatch)

        # Loading screen: no vehicle yet.
        poller.client.indicators.return_value = {
            "valid": False,
            "army": "air",
            "type": "dummy_plane",
        }
        poller.client.map_info.return_value = {"valid": True}
        poller.client.mission.return_value = None
        for _ in range(3):
            poller.poll()
        assert poller.activity is Activity.LOADING

        # Spawned, but objectives have not arrived yet -- the ambiguous window.
        self._feed(poller, objectives=None, polls=3)
        assert poller.activity is Activity.LOADING, (
            "a match that is loading must not be reported as a test flight "
            "just because the objectives are a few seconds late"
        )

        # Objectives arrive: it resolves to a real match.
        self._feed(
            poller,
            objectives=[{"primary": True, "text": "Assist the ground forces"}],
            polls=2,
        )
        assert poller.activity is Activity.IN_MATCH

    def test_a_genuine_test_flight_still_resolves_eventually(self, monkeypatch):
        poller = self._poller(monkeypatch)
        poller.client.indicators.return_value = {
            "valid": False,
            "army": "air",
            "type": "dummy_plane",
        }
        poller.client.map_info.return_value = {"valid": True}
        poller.client.mission.return_value = None
        for _ in range(3):
            poller.poll()
        assert poller.activity is Activity.LOADING

        # No objectives ever arrive, because this really is a test flight.
        self._feed(poller, objectives=None, polls=10)
        assert poller.activity is Activity.TEST_DRIVE


class TestMatchWithoutObjectives:
    """A match can run for minutes before /mission.json publishes anything.

    Measured live on Golan Heights: 62 consecutive polls in a real battle with
    no objective at all, reported as a test flight the whole time. The minimap
    settles it, because the hash table holds battle maps only -- across 340
    polls of a genuine test flight the map was never identified once, and
    across those 62 match polls it was identified every time.
    """

    @staticmethod
    def _poller(map_name: str):
        from wtrpc.config import Config
        from wtrpc.__main__ import Poller

        poller = Poller(Config())
        poller.client = MagicMock(spec=WarThunderClient)
        poller.client.available = True
        poller.client.map_obj.return_value = []
        poller.client.state.return_value = {"valid": True, "M": 0.9}
        poller.client.map_image.return_value = object()
        poller.client.identify_map.return_value = map_name
        poller.client.indicators.return_value = {
            "valid": True,
            "army": "air",
            "type": "f-16c",
        }
        poller.client.map_info.return_value = {"valid": True}
        poller.client.mission.return_value = {"objectives": None}
        return poller

    def test_recognised_battle_map_without_objectives_is_a_match(self):
        poller = self._poller("Golan Heights")
        for _ in range(4):
            state = poller.poll()
        assert poller.activity is Activity.IN_MATCH
        assert state.map_name == "Golan Heights"

    def test_unrecognised_map_without_objectives_is_still_a_test_flight(self):
        poller = self._poller("")
        for _ in range(10):
            state = poller.poll()
        assert poller.activity is Activity.TEST_DRIVE
        assert state.map_name == ""


class TestMatchModeBelongsToTheMatch:
    """A Ground RB battle stays a Ground RB battle when you spawn a helicopter.

    Observed live on Mozdok: the same objective text throughout, but the label
    followed the vehicle -- "Ground Domination" in the tank, "Air Domination"
    the moment an AH-64A spawned. Only one of those describes the match.
    """

    OBJECTIVE = "Capture and maintain superiority over the points"

    @staticmethod
    def _poller():
        from wtrpc.config import Config
        from wtrpc.__main__ import Poller

        poller = Poller(Config())
        poller.client = MagicMock(spec=WarThunderClient)
        poller.client.available = True
        poller.client.map_image.return_value = None
        poller.client.identify_map.return_value = "Mozdok"
        poller.client.map_obj.return_value = [{"type": "respawn_base_tank"}]
        poller.client.map_info.return_value = {"valid": True}
        poller.client.state.return_value = {"valid": False}
        poller.client.hudmsg.return_value = {"events": [], "damage": []}
        return poller

    def _spawn(self, poller, army: str, vehicle: str, polls: int = 3):
        poller.client.indicators.return_value = {
            "valid": True,
            "army": army,
            "type": vehicle,
        }
        poller.client.mission.return_value = {
            "objectives": [{"primary": True, "text": self.OBJECTIVE}]
        }
        state = None
        for _ in range(polls):
            state = poller.poll()
        return state

    def test_helicopter_does_not_relabel_a_ground_battle(self):
        poller = self._poller()
        in_tank = self._spawn(poller, "tank", "tankModels/us_m1a2_sep2_abrams")
        assert in_tank.mode == "Ground Domination"

        in_heli = self._spawn(poller, "air", "ah_64a_peten")
        assert in_heli.mode == "Ground Domination", (
            "the battle type belongs to the match, not to the vehicle"
        )

    def test_a_jet_does_not_relabel_it_either(self):
        poller = self._poller()
        self._spawn(poller, "tank", "tankModels/us_m1a2_sep2_abrams")
        in_jet = self._spawn(poller, "air", "f_16c_block_50")
        assert in_jet.mode == "Ground Domination"

    def test_returning_to_the_hangar_clears_the_latch(self):
        poller = self._poller()
        self._spawn(poller, "tank", "tankModels/us_m1a2_sep2_abrams")

        poller.client.map_info.return_value = {"valid": False}
        poller.client.map_obj.return_value = []
        poller.client.identify_map.return_value = ""
        for _ in range(3):
            poller.poll()
        assert poller.match_mode == "", "a new match must be free to relabel"

    def test_an_air_battle_still_labels_itself_air(self):
        """An air battle has no tank respawns at all -- that is the tell."""
        poller = self._poller()
        poller.client.map_obj.return_value = [
            {"type": "respawn_base_fighter"},
            {"type": "respawn_base_bomber"},
        ]
        in_jet = self._spawn(poller, "air", "f_16c_block_50")
        assert in_jet.mode == "Air Domination"

    def test_the_battle_type_comes_from_the_map_not_the_hangar_pick(self):
        """Selecting a jet before a Ground RB match must not relabel it.

        During the load screen the reported army is whatever was selected in
        the hangar, which is how a Ground RB battle on Mozdok came out as
        "Air Battle" for its entire duration.
        """
        poller = self._poller()
        poller.client.map_info.return_value = {"valid": False}
        poller.client.map_obj.return_value = []
        poller.client.indicators.return_value = {
            "valid": False,
            "army": "air",
            "type": "dummy_plane",
        }
        for _ in range(3):
            poller.poll()

        poller.client.map_info.return_value = {"valid": True}
        poller.client.map_obj.return_value = [{"type": "respawn_base_tank"}]
        spawned = self._spawn(poller, "tank", "tankModels/us_hstv_l")
        assert spawned.mode == "Ground Domination"


class TestStaleSpawnFrame:
    """The first poll after a respawn carries the previous vehicle's state.

    Captured live: spawning a pristine ADATS Bradley reported a destroyed
    breech and both drives out -- precisely the wreck of the M1A2 that had
    just died -- for exactly one frame, then read clean for the rest of the
    life. The rate limit happened to swallow it, but correctness must not
    depend on that.
    """

    WRECK = {
        "valid": True,
        "army": "tank",
        "type": "tankModels/us_adats_bradley",
        "first_stage_ammo": 0.0,
        "crew_current": 3.0,
        "crew_total": 3.0,
        "breach_dead": 1.0,
        "v_drive_broken": 1.0,
        "h_drive_dead": 1.0,
    }
    SETTLED = {
        "valid": True,
        "army": "tank",
        "type": "tankModels/us_adats_bradley",
        "first_stage_ammo": -1.0,
        "crew_current": 3.0,
        "crew_total": 3.0,
    }

    @staticmethod
    def _poller():
        from wtrpc.config import Config
        from wtrpc.__main__ import Poller

        poller = Poller(Config())
        poller.client = MagicMock(spec=WarThunderClient)
        poller.client.available = True
        poller.client.map_image.return_value = None
        poller.client.identify_map.return_value = "Mozdok"
        poller.client.map_obj.return_value = [{"type": "respawn_base_tank"}]
        poller.client.map_info.return_value = {"valid": True}
        poller.client.state.return_value = {"valid": False}
        poller.client.hudmsg.return_value = {"events": [], "damage": []}
        poller.client.mission.return_value = {
            "objectives": [{"primary": True, "text": "Capture and hold the point"}]
        }
        return poller

    def test_first_frame_on_a_new_vehicle_reports_no_damage(self):
        poller = self._poller()
        poller.client.indicators.return_value = {
            "valid": True,
            "army": "tank",
            "type": "tankModels/us_m1a2_sep2_abrams",
            "first_stage_ammo": 18.0,
        }
        for _ in range(3):
            poller.poll()

        poller.client.indicators.return_value = self.WRECK
        first = poller.poll()
        assert first.ground.damage == (), (
            "a freshly spawned vehicle must not inherit the last one's wreck"
        )

    def test_the_next_frame_is_trusted(self):
        poller = self._poller()
        poller.client.indicators.return_value = self.WRECK
        poller.poll()
        second = poller.poll()
        assert second.ground.damage == (
            "Breech destroyed",
            "Vertical drive out",
            "Horizontal drive out",
        ), "once the vehicle is stable its real damage must show"

    def test_a_missile_carrier_reports_no_gun_ammo(self):
        """The ADATS has no gun ready rack; the game says -1, not 0."""
        from wtrpc.ground import ammo_label, read_ground

        assert ammo_label(read_ground(self.SETTLED)) == ""
