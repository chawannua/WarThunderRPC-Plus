"""Tests for wtrpc.contacts and the dogfight/manoeuvre distinction.

The marker shapes below were copied from a live War Thunder match, not
invented: an F-4S in an air battle against MiG-15s.
"""

from __future__ import annotations

import pytest

from wtrpc.contacts import (
    find_player,
    looks_like_a_match,
    is_hostile,
    map_span_m,
    nearest_hostile_km,
)
from wtrpc.flight import FlightAnalyzer
from wtrpc.models import AirState, Flight

# Real /map_info.json from a live air battle.
MAP_INFO = {
    "grid_size": [81636.13, 31033.50],
    "grid_steps": [8100.0, 8100.0],
    "grid_zero": [-53513.37, 6001.77],
    "map_max": [65536.0, 65536.0],
    "map_min": [-65536.0, -65536.0],
    "valid": True,
}

PLAYER = {
    "type": "aircraft",
    "color": "#faC81E",
    "color[]": [250, 200, 30],
    "blink": 0,
    "icon": "Player",
    "icon_bg": "none",
    "x": 0.346029,
    "y": 0.682834,
    "dx": -0.983172,
    "dy": -0.18268,
}


def hostile(x: float, y: float) -> dict:
    return {
        "type": "aircraft",
        "color": "#f00C00",
        "color[]": [240, 12, 0],
        "blink": 1,
        "icon": "Fighter",
        "icon_bg": "none",
        "x": x,
        "y": y,
    }


def friendly(x: float, y: float) -> dict:
    return {
        "type": "aircraft",
        "color": "#174DFF",
        "color[]": [23, 77, 255],
        "icon": "Fighter",
        "x": x,
        "y": y,
    }


GROUND_ENEMY = {
    "type": "ground_model",
    "color": "#fa0C00",
    "color[]": [250, 12, 0],
    "icon": "MediumTank",
    "x": 0.3461,
    "y": 0.6829,
}


class TestMarkerClassification:
    def test_enemy_aircraft_is_hostile(self):
        assert is_hostile(hostile(0.1, 0.1)) is True

    def test_friendly_is_not_hostile(self):
        assert is_hostile(friendly(0.1, 0.1)) is False

    def test_player_marker_is_not_hostile(self):
        assert is_hostile(PLAYER) is False

    def test_marker_without_colour_channels_is_not_hostile(self):
        assert is_hostile({"type": "aircraft", "icon": "Fighter"}) is False

    def test_find_player_returns_the_player_marker(self):
        assert find_player([hostile(0.1, 0.1), PLAYER]) is PLAYER

    def test_find_player_returns_none_when_absent(self):
        assert find_player([hostile(0.1, 0.1)]) is None


class TestMapSpan:
    def test_span_comes_from_map_min_and_max(self):
        assert map_span_m(MAP_INFO) == (131072.0, 131072.0)

    @pytest.mark.parametrize("bad", [None, {}, {"map_min": "x"}, {"map_max": [1]}])
    def test_falls_back_rather_than_raising(self, bad):
        span_x, span_y = map_span_m(bad)
        assert span_x > 0 and span_y > 0


class TestNearestHostile:
    def test_matches_the_distance_measured_in_a_live_match(self):
        """Live sample: player and bandit 0.010182 apart on a 131.072 km map."""
        markers = [PLAYER, hostile(0.345646, 0.672652)]
        distance = nearest_hostile_km(markers, MAP_INFO)
        assert distance == pytest.approx(1.336, abs=0.01)

    def test_picks_the_closest_of_several(self):
        markers = [
            PLAYER,
            hostile(0.40, 0.42),
            hostile(0.345646, 0.672652),
            hostile(0.20, 0.30),
        ]
        assert nearest_hostile_km(markers, MAP_INFO) == pytest.approx(1.336, abs=0.01)

    def test_friendlies_are_ignored(self):
        markers = [PLAYER, friendly(0.3461, 0.6829), hostile(0.40, 0.42)]
        # The friendly is metres away; the answer must be the distant hostile.
        assert nearest_hostile_km(markers, MAP_INFO) > 20.0

    def test_ground_targets_are_ignored(self):
        """A tank directly underneath is not an air contact."""
        markers = [PLAYER, GROUND_ENEMY]
        assert nearest_hostile_km(markers, MAP_INFO) is None

    def test_none_when_no_hostiles_shown(self):
        assert nearest_hostile_km([PLAYER, friendly(0.1, 0.1)], MAP_INFO) is None

    def test_none_when_player_marker_missing(self):
        assert nearest_hostile_km([hostile(0.1, 0.1)], MAP_INFO) is None

    @pytest.mark.parametrize("markers", [None, []])
    def test_none_for_empty_input(self, markers):
        assert nearest_hostile_km(markers, MAP_INFO) is None

    def test_malformed_markers_are_skipped_not_fatal(self):
        markers = [
            PLAYER,
            {"type": "aircraft", "color[]": [240, 12, 0], "icon": "Fighter"},  # no x/y
            {"type": "aircraft", "color[]": [240, 12, 0], "x": "nope", "y": 1},
            hostile(0.345646, 0.672652),
        ]
        assert nearest_hostile_km(markers, MAP_INFO) == pytest.approx(1.336, abs=0.01)


class TestDogfightVersusManeuvering:
    """The whole point: identical instruments, different situations."""

    @staticmethod
    def _hard_turn(analyzer: FlightAnalyzer, hostile_km: float | None) -> AirState:
        for t, g in enumerate([5.2, 4.8, 6.1, 5.5, 4.9, 5.8]):
            analyzer.add(
                Flight(mach=0.85, load_factor=g, nearest_hostile_km=hostile_km),
                timestamp=float(t),
            )
        return analyzer.state()

    def test_high_g_with_a_bandit_in_range_is_a_dogfight(self):
        assert self._hard_turn(FlightAnalyzer(), 1.2) == AirState.DOGFIGHTING

    def test_high_g_with_the_nearest_bandit_far_away_is_only_manoeuvring(self):
        """Measured live: a 9.4G break with the closest enemy 5.8 km away and
        opening. That is a disengagement, not a fight."""
        assert self._hard_turn(FlightAnalyzer(), 5.8) == AirState.MANEUVERING

    def test_high_g_with_no_contact_information_is_only_manoeuvring(self):
        """Outside Arcade the minimap may show nothing. Unknown must not be
        promoted to "dogfighting" on an assumption."""
        assert self._hard_turn(FlightAnalyzer(), None) == AirState.MANEUVERING

    def test_a_brief_merge_still_counts_as_a_dogfight(self):
        """Fighters pass inside a kilometre then separate fast; the closest
        approach in the window is what matters, not the latest reading."""
        analyzer = FlightAnalyzer()
        for t, km in enumerate([4.0, 2.2, 0.8, 2.6, 4.1, 5.5]):
            analyzer.add(
                Flight(mach=0.85, load_factor=5.0, nearest_hostile_km=km),
                timestamp=float(t),
            )
        assert analyzer.state() == AirState.DOGFIGHTING

    def test_contact_without_manoeuvring_is_engaged_not_a_dogfight(self):
        """Close to someone but flying steadily: a firing pass, not a turn fight.

        Measured live: 25 seconds between 0.5 and 2.6 km of an enemy fighter
        at Mach 1.4 while pulling barely over 1G. Calling that a dogfight
        overstates it; calling it "Supersonic" says nothing about the enemy
        being right there.
        """
        analyzer = FlightAnalyzer()
        for t in range(6):
            analyzer.add(
                Flight(
                    mach=0.6,
                    load_factor=1.0,
                    roll_deg=0.0,
                    vertical_speed_ms=0.0,
                    nearest_hostile_km=0.5,
                ),
                timestamp=float(t),
            )
        assert analyzer.state() == AirState.ENGAGED

    def test_engaged_outranks_supersonic(self):
        """A bandit at knife range matters more than the airspeed indicator."""
        analyzer = FlightAnalyzer()
        for t in range(6):
            analyzer.add(
                Flight(mach=1.4, load_factor=1.1, nearest_hostile_km=0.48),
                timestamp=float(t),
            )
        assert analyzer.state() == AirState.ENGAGED

    def test_steady_flight_with_nobody_near_is_not_engaged(self):
        analyzer = FlightAnalyzer()
        for t in range(6):
            analyzer.add(
                Flight(
                    mach=0.6,
                    load_factor=1.0,
                    roll_deg=0.0,
                    vertical_speed_ms=0.0,
                    nearest_hostile_km=18.0,
                ),
                timestamp=float(t),
            )
        assert analyzer.state() == AirState.CRUISING

    def test_all_three_combat_states_from_the_same_g_trace(self):
        """The three-way split in one table: identical manoeuvring, different
        company. This is the whole point of tracking contact range."""
        def run(gs: list[float], hostile_km: float | None) -> AirState:
            analyzer = FlightAnalyzer()
            for t, g in enumerate(gs):
                analyzer.add(
                    Flight(mach=0.9, load_factor=g, nearest_hostile_km=hostile_km),
                    timestamp=float(t),
                )
            return analyzer.state()

        hard = [5.2, 4.8, 6.1, 5.5, 4.9, 5.8]
        gentle = [1.0, 1.1, 0.9, 1.2, 1.0, 1.1]

        assert run(hard, 1.2) == AirState.DOGFIGHTING
        assert run(hard, 30.0) == AirState.MANEUVERING
        assert run(gentle, 1.2) == AirState.ENGAGED
        assert run(gentle, 30.0) == AirState.CRUISING


class TestMatchMarkers:
    """Marker sets captured live from both sides of the ambiguity."""

    # A real battle, sampled while /mission.json still had objectives pending.
    BATTLE = [
        {"type": "airfield", "icon": "none"},
        {"type": "defending_point", "icon": "defending_point"},
        {"type": "respawn_base_fighter", "icon": "respawn_base_fighter"},
        {"type": "respawn_base_bomber", "icon": "respawn_base_bomber"},
    ]

    # A genuine test flight: ground targets and airfields, nowhere to respawn
    # and nothing to capture.
    TEST_FLIGHT = [
        {"type": "ground_model", "icon": "IFV"},
        {"type": "ground_model", "icon": "MediumTank"},
        {"type": "aircraft", "icon": "Fighter"},
        {"type": "bombing_point", "icon": "bombing_point"},
        {"type": "airfield", "icon": "none"},
        {"type": "point_of_interest", "icon": "point_of_interest"},
        {"type": "aircraft", "icon": "Player"},
    ]

    def test_battle_markers_are_recognised(self):
        assert looks_like_a_match(self.BATTLE) is True

    def test_test_flight_markers_are_not_a_match(self):
        assert looks_like_a_match(self.TEST_FLIGHT) is False

    def test_bombing_points_and_airfields_alone_are_not_enough(self):
        """Both exist in a test flight, so neither may imply a battle."""
        assert looks_like_a_match(
            [{"type": "bombing_point"}, {"type": "airfield"}]
        ) is False

    @pytest.mark.parametrize(
        "marker",
        [
            {"type": "capture_zone"},
            {"icon": "capture_zone"},
            {"type": "respawn_base_tank"},
            {"icon": "DEFENDING_POINT"},
            {"type": " respawn_base_fighter "},
            {"type": "respawn_base_ucav"},
        ],
    )
    def test_single_battle_marker_is_enough(self, marker):
        """The game reports these under `type` on some markers and `icon` on
        others, so both fields are checked, case and whitespace insensitively."""
        assert looks_like_a_match([marker]) is True

    @pytest.mark.parametrize("markers", [None, [], ["not a dict"], [{}]])
    def test_junk_input_is_not_a_match(self, markers):
        assert looks_like_a_match(markers) is False
