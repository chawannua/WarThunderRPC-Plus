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


# ---------------------------------------------------------------------------
# main() -- a failure below the poll layer must cost one iteration, not the
# whole process
# ---------------------------------------------------------------------------


class TestMainSurvivesBuildOrUpdateFailure:
    def test_build_presence_raising_does_not_kill_the_process(self, tmp_path, monkeypatch):
        from wtrpc import __main__ as main_module

        calls = {"poll": 0, "sleep": 0}

        class FakePoller:
            def __init__(self, cfg):
                pass

            def poll(self):
                calls["poll"] += 1
                return object()  # anything that is not None

        class FakePresenceManager:
            def __init__(self, *a, **kw):
                pass

            def update(self, payload):
                pass

            def clear(self):
                pass

            def close(self):
                pass

        def fake_build_presence(state, **kwargs):
            if calls["poll"] == 1:
                raise RuntimeError("boom")
            return object()

        def fake_sleep(seconds):
            calls["sleep"] += 1
            if calls["sleep"] >= 2:
                raise KeyboardInterrupt

        monkeypatch.setattr(main_module, "Poller", FakePoller)
        monkeypatch.setattr(main_module, "PresenceManager", FakePresenceManager)
        monkeypatch.setattr(main_module, "build_presence", fake_build_presence)
        monkeypatch.setattr(main_module.time, "sleep", fake_sleep)

        result = main_module.main(["-c", str(tmp_path / "config.json")])

        assert result == 0
        # The loop must have kept going past the failing build_presence() call
        # rather than letting the exception escape and kill the process.
        assert calls["poll"] >= 2

    def test_presence_update_raising_does_not_kill_the_process(self, tmp_path, monkeypatch):
        from wtrpc import __main__ as main_module

        calls = {"poll": 0, "sleep": 0}

        class FakePoller:
            def __init__(self, cfg):
                pass

            def poll(self):
                calls["poll"] += 1
                return object()

        class FakePresenceManager:
            def __init__(self, *a, **kw):
                pass

            def update(self, payload):
                raise RuntimeError("discord exploded")

            def clear(self):
                pass

            def close(self):
                pass

        def fake_sleep(seconds):
            calls["sleep"] += 1
            if calls["sleep"] >= 2:
                raise KeyboardInterrupt

        monkeypatch.setattr(main_module, "Poller", FakePoller)
        monkeypatch.setattr(main_module, "PresenceManager", FakePresenceManager)
        monkeypatch.setattr(main_module, "build_presence", lambda state, **kw: object())
        monkeypatch.setattr(main_module.time, "sleep", fake_sleep)

        result = main_module.main(["-c", str(tmp_path / "config.json")])

        assert result == 0
        assert calls["poll"] >= 2



# ---------------------------------------------------------------------------
# Respawns and match boundaries
# ---------------------------------------------------------------------------


def _match_client(**overrides) -> MagicMock:
    kwargs = dict(
        in_map=True,
        vehicle_valid=True,
        vehicle_type="us_m1_abrams",
        army="tank",
        primary_text="Capture the point",
    )
    kwargs.update(overrides)
    return _client_mock(**kwargs)


def _respawn(poller: Poller, client: MagicMock) -> None:
    """Die, sit on the respawn screen long enough to commit LOADING, spawn again."""
    client.indicators.return_value = {**client.indicators.return_value, "valid": False}
    _drive(poller)
    assert poller.activity is Activity.LOADING
    client.indicators.return_value = {**client.indicators.return_value, "valid": True}
    _drive(poller)
    assert poller.activity is Activity.IN_MATCH


class TestRespawnKeepsTheMatch:
    def test_respawn_keeps_the_match_timer(self, monkeypatch):
        monkeypatch.setattr("wtrpc.__main__.read_loadout", lambda _v: [])
        client = _match_client()
        poller = make_poller()
        poller.client = client
        _drive(poller)
        started_at = poller.activity_since

        monkeypatch.setattr("wtrpc.__main__.time.time", lambda: started_at + 600)
        _respawn(poller, client)

        assert poller.activity_since == started_at
        assert poller.poll().match_started_at == started_at

    def test_respawn_does_not_reset_the_kill_feed(self, monkeypatch):
        monkeypatch.setattr("wtrpc.__main__.read_loadout", lambda _v: [])
        client = _match_client()
        poller = make_poller()
        poller.client = client
        _drive(poller)
        poller.killfeed = MagicMock()
        poller.killfeed.kills = 2

        _respawn(poller, client)

        poller.killfeed.reset.assert_not_called()

    def test_a_new_match_after_the_hangar_resets_the_kill_feed(self, monkeypatch):
        monkeypatch.setattr("wtrpc.__main__.read_loadout", lambda _v: [])
        client = _match_client()
        poller = make_poller()
        poller.client = client
        _drive(poller)
        poller.killfeed = MagicMock()
        poller.killfeed.kills = 0

        client.map_info.return_value = {"valid": False}
        _drive(poller)
        assert poller.activity is Activity.HANGAR
        client.map_info.return_value = {"valid": True}
        _drive(poller)

        poller.killfeed.reset.assert_called_once()


class TestMatchFinished:
    def test_returning_to_the_hangar_after_a_match_counts_one_finished_match(self, monkeypatch):
        monkeypatch.setattr("wtrpc.__main__.read_loadout", lambda _v: [])
        client = _match_client()
        poller = make_poller()
        poller.client = client
        _drive(poller)
        assert poller.matches_finished == 0

        _respawn(poller, client)
        assert poller.matches_finished == 0

        client.map_info.return_value = {"valid": False}
        _drive(poller)
        assert poller.matches_finished == 1

    def test_leaving_a_test_drive_is_not_a_finished_match(self):
        client = _client_mock(in_map=True, vehicle_valid=True)
        poller = make_poller()
        poller.client = client
        _drive(poller)
        assert poller.activity is Activity.TEST_DRIVE

        client.map_info.return_value = {"valid": False}
        _drive(poller)
        assert poller.matches_finished == 0


class TestPerVehicleStateIsNotStale:
    def test_loadout_is_reread_after_the_hangar(self, monkeypatch):
        reads = []
        monkeypatch.setattr(
            "wtrpc.__main__.read_loadout", lambda v: reads.append(v) or ["x"]
        )
        monkeypatch.setattr("wtrpc.__main__.loadout_label", lambda _l: "APFSDS")
        client = _match_client()
        poller = make_poller()
        poller.client = client
        _drive(poller)
        client.map_info.return_value = {"valid": False}
        _drive(poller)
        client.map_info.return_value = {"valid": True}
        _drive(poller)

        assert len(reads) == 2

    def test_an_empty_loadout_read_is_retried(self, monkeypatch):
        results = [[], ["x"]]
        monkeypatch.setattr("wtrpc.__main__.read_loadout", lambda _v: results.pop(0))
        monkeypatch.setattr(
            "wtrpc.__main__.loadout_label", lambda l: "APFSDS" if l else ""
        )
        clock = [1000.0]
        monkeypatch.setattr("wtrpc.__main__.time.monotonic", lambda: clock[0])
        client = _match_client()
        poller = make_poller()
        poller.client = client
        _drive(poller)
        assert poller.loadout == ""

        clock[0] += 60
        poller.poll()
        assert poller.loadout == "APFSDS"

    def test_same_vehicle_respawn_ignores_the_wreck_frame(self, monkeypatch):
        monkeypatch.setattr("wtrpc.__main__.read_loadout", lambda _v: [])
        from wtrpc.models import Ground

        wreck = Ground(crew_alive=0, crew_total=4)
        monkeypatch.setattr("wtrpc.__main__.read_ground", lambda _i: wreck)
        client = _match_client()
        poller = make_poller()
        poller.client = client
        _drive(poller, 4)

        client.indicators.return_value = {**client.indicators.return_value, "valid": False}
        poller.poll()
        client.indicators.return_value = {**client.indicators.return_value, "valid": True}
        state = poller.poll()

        assert state.ground.crew_alive is None

    def test_shell_is_cleared_when_the_vehicle_changes(self, monkeypatch):
        monkeypatch.setattr("wtrpc.__main__.read_loadout", lambda _v: [])
        client = _match_client()
        poller = make_poller()
        poller.client = client
        _drive(poller)
        poller.shell = "APFSDS"

        client.indicators.return_value = {
            **client.indicators.return_value,
            "type": "germ_leopard_2a6",
        }
        poller.poll()

        assert poller.shell == ""


class TestManagedMode:
    def _main_with(self, monkeypatch, states):
        from wtrpc import __main__ as m

        polls = iter(states)
        self.polls_made = 0

        class FakePoller:
            def __init__(self, _cfg):
                self.matches_finished = 0

            def poll(inner):
                self.polls_made += 1
                item = next(polls, KeyboardInterrupt())
                if isinstance(item, BaseException):
                    raise item
                if item == "finish":
                    inner.matches_finished += 1
                    return MagicMock()
                return item

        presence = MagicMock()
        monkeypatch.setattr(m, "Poller", FakePoller)
        monkeypatch.setattr(m, "PresenceManager", lambda *_a: presence)
        monkeypatch.setattr(m, "build_presence", lambda *_a, **_k: MagicMock())
        monkeypatch.setattr(m, "_setup_logging", lambda _v: None)
        clock = [0.0]
        monkeypatch.setattr(m.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
        monkeypatch.setattr(m.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(m.config_module, "load", lambda _p: config_module.Config())
        return m, presence

    def test_managed_exits_after_a_finished_match(self, monkeypatch):
        m, presence = self._main_with(monkeypatch, [MagicMock(), "finish"] + [MagicMock()] * 5)
        assert m.main(["--managed"]) == 0
        assert self.polls_made == 2
        presence.close.assert_called_once()

    def test_managed_exits_once_the_game_has_been_gone_a_while(self, monkeypatch):
        # OFFLINE_POLL_INTERVAL_S is 5 s, so the 10 s grace is spent within 3 polls.
        m, _ = self._main_with(monkeypatch, [MagicMock()] + [None] * 20)
        assert m.main(["--managed"]) == 0
        assert self.polls_made <= 5

    def test_managed_waits_for_a_game_it_has_never_seen(self, monkeypatch):
        # Still booting: the watcher only launches us once aces.exe exists, and
        # the HTTP server comes up later. Nothing but the interrupt may end it.
        m, _ = self._main_with(monkeypatch, [None] * 30)
        assert m.main(["--managed"]) == 0
        assert self.polls_made == 31

    def test_unmanaged_keeps_running_after_a_match(self, monkeypatch):
        m, _ = self._main_with(monkeypatch, [MagicMock(), "finish", MagicMock()])
        assert m.main([]) == 0
        assert self.polls_made == 4


class TestLogging:
    def test_library_debug_chatter_stays_out_of_the_log(self, monkeypatch, tmp_path):
        import logging

        from wtrpc import __main__ as m

        monkeypatch.setattr(m.config_module, "config_path", lambda: tmp_path / "config.json")
        root = logging.getLogger()
        before = list(root.handlers)
        try:
            m._setup_logging(False)
            for name in ("urllib3", "asyncio", "PIL"):
                assert not logging.getLogger(name).isEnabledFor(logging.DEBUG), name
            assert logging.getLogger("wtrpc").isEnabledFor(logging.DEBUG)
        finally:
            for handler in root.handlers[:]:
                if handler not in before:
                    root.removeHandler(handler)
                    handler.close()
