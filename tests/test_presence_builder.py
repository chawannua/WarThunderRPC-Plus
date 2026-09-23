"""Tests for wtrpc.presence_builder — assembling the final Discord payload."""

import pytest

from wtrpc.models import Activity, AirState, Army, Flight, GameState, Ground, PresencePayload
from wtrpc.presence_builder import build_presence

MAX_LEN = 128


class TestHangar:
    def test_hangar_details_and_state(self):
        state = GameState(activity=Activity.HANGAR)
        payload = build_presence(state)
        assert payload.details == "In the hangar"
        assert payload.state == "Browsing vehicles..."


class TestLoading:
    def test_loading_uses_mode_as_details(self):
        state = GameState(activity=Activity.LOADING, army=Army.AIR, mode="Air Battle")
        payload = build_presence(state)
        assert payload.details == "Air Battle"
        assert payload.state == "Loading into a match..."

    def test_loading_falls_back_when_mode_empty(self):
        state = GameState(activity=Activity.LOADING, army=Army.AIR, mode="")
        payload = build_presence(state)
        assert payload.details  # never empty
        assert payload.state == "Loading into a match..."


class TestTestDrive:
    def test_test_drive_air_uses_piloting_verb(self):
        state = GameState(
            activity=Activity.TEST_DRIVE,
            army=Army.AIR,
            vehicle_name="F-4E PHANTOM",
        )
        payload = build_presence(state)
        assert payload.details == "Test Drive"
        assert payload.state == "Piloting a F-4E PHANTOM"

    def test_test_drive_tank_uses_driving_verb(self):
        state = GameState(
            activity=Activity.TEST_DRIVE, army=Army.TANK, vehicle_name="T-34"
        )
        payload = build_presence(state)
        assert payload.state == "Driving a T-34"

    def test_test_drive_ship_uses_commanding_verb(self):
        state = GameState(
            activity=Activity.TEST_DRIVE, army=Army.SHIP, vehicle_name="US DESTROYER"
        )
        payload = build_presence(state)
        assert payload.state == "Commanding a US DESTROYER"

    def test_test_drive_missing_vehicle_name_never_empty_state(self):
        state = GameState(activity=Activity.TEST_DRIVE, army=Army.AIR, vehicle_name="")
        payload = build_presence(state)
        assert payload.state != ""


class TestInMatchDetails:
    def test_combines_regime_mode_and_map(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.AIR,
            vehicle_name="F-4E PHANTOM",
            map_name="Kursk",
            mode="Air Domination",
            air_state=AirState.DOGFIGHTING,
        )
        payload = build_presence(state)
        assert payload.details == "Dogfighting · Air Domination on Kursk"

    def test_omits_map_cleanly_when_missing(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.AIR,
            vehicle_name="F-4E PHANTOM",
            map_name="",
            mode="Air Domination",
            air_state=AirState.CRUISING,
        )
        payload = build_presence(state)
        assert " on " not in payload.details
        assert payload.details == "Cruising · Air Domination"

    def test_omits_regime_cleanly_when_unknown(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.TANK,
            vehicle_name="US M1 ABRAMS",
            map_name="El Alamein",
            mode="Ground Battle",
            air_state=AirState.UNKNOWN,
        )
        payload = build_presence(state)
        assert payload.details == "Ground Battle on El Alamein"
        assert not payload.details.startswith("·")

    def test_omits_mode_cleanly_when_missing(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.TANK,
            vehicle_name="US M1 ABRAMS",
            map_name="El Alamein",
            mode="",
            air_state=AirState.UNKNOWN,
        )
        payload = build_presence(state)
        assert payload.details == "El Alamein"

    def test_everything_missing_never_yields_empty_details(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.UNKNOWN,
            vehicle_name="",
            map_name="",
            mode="",
            air_state=AirState.UNKNOWN,
        )
        payload = build_presence(state)
        assert payload.details != ""
        assert not payload.details.strip().endswith(" on")
        assert "  " not in payload.details


class TestFlightDataInState:
    def test_air_vehicle_shows_mach_and_ias(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.AIR,
            vehicle_name="F-4E PHANTOM",
            flight=Flight(mach=0.94, ias_kph=1120.0),
            air_state=AirState.CRUISING,
        )
        payload = build_presence(state)
        assert payload.state == "F-4E PHANTOM · Mach 0.94 · 1120 km/h IAS"

    def test_mach_only_when_ias_missing(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.AIR,
            vehicle_name="F-4E PHANTOM",
            flight=Flight(mach=0.94, ias_kph=None),
        )
        payload = build_presence(state)
        assert payload.state == "F-4E PHANTOM · Mach 0.94"

    def test_ias_only_when_mach_missing(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.AIR,
            vehicle_name="F-4E PHANTOM",
            flight=Flight(mach=None, ias_kph=560.0),
        )
        payload = build_presence(state)
        assert payload.state == "F-4E PHANTOM · 560 km/h IAS"

    def test_falls_back_to_vehicle_name_when_both_missing(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.AIR,
            vehicle_name="F-4E PHANTOM",
            flight=Flight(mach=None, ias_kph=None),
        )
        payload = build_presence(state)
        assert payload.state == "F-4E PHANTOM"

    def test_ground_vehicle_never_shows_mach_or_ias(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.TANK,
            vehicle_name="US M1 ABRAMS",
            flight=Flight(mach=0.5, ias_kph=100.0),
        )
        payload = build_presence(state)
        assert "Mach" not in payload.state
        assert "IAS" not in payload.state
        assert payload.state == "US M1 ABRAMS"

    def test_show_flight_data_false_hides_mach_and_ias(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.AIR,
            vehicle_name="F-4E PHANTOM",
            flight=Flight(mach=0.94, ias_kph=1120.0),
        )
        payload = build_presence(state, show_flight_data=False)
        assert payload.state == "F-4E PHANTOM"

    def test_missing_vehicle_name_never_yields_empty_state(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.AIR,
            vehicle_name="",
            flight=Flight(mach=None, ias_kph=None),
        )
        payload = build_presence(state)
        assert payload.state != ""


class TestDogfightDetectionFlag:
    def test_dogfighting_hidden_when_flag_false(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.AIR,
            vehicle_name="F-4E PHANTOM",
            mode="Air Domination",
            map_name="Kursk",
            air_state=AirState.DOGFIGHTING,
        )
        payload = build_presence(state, dogfight_detection=False)
        assert "Dogfighting" not in payload.details

    def test_other_regimes_still_work_when_dogfight_detection_false(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.AIR,
            vehicle_name="F-4E PHANTOM",
            mode="Air Domination",
            map_name="Kursk",
            air_state=AirState.CLIMBING,
        )
        payload = build_presence(state, dogfight_detection=False)
        assert "Climbing" in payload.details


class TestUnknownActivity:
    def test_unknown_activity_never_crashes_and_fields_are_sane(self):
        state = GameState(activity=Activity.UNKNOWN)
        payload = build_presence(state)
        assert payload.details != ""
        assert payload.state != ""


class TestSmallImage:
    def test_small_image_and_text_set_for_real_vehicle(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.AIR,
            vehicle_id="f_4e",
            vehicle_name="F-4E PHANTOM",
        )
        payload = build_presence(state)
        assert payload.small_image == (
            "https://static.encyclopedia.warthunder.com/images/f_4e.png"
        )
        assert payload.small_text == "F-4E PHANTOM"

    def test_small_image_none_when_show_vehicle_image_false(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.AIR,
            vehicle_id="f_4e",
            vehicle_name="F-4E PHANTOM",
        )
        payload = build_presence(state, show_vehicle_image=False)
        assert payload.small_image is None
        assert payload.small_text is None

    def test_small_image_none_when_vehicle_id_empty(self):
        state = GameState(activity=Activity.IN_MATCH, army=Army.AIR, vehicle_id="")
        payload = build_presence(state)
        assert payload.small_image is None

    def test_small_image_none_when_vehicle_id_placeholder(self):
        state = GameState(
            activity=Activity.IN_MATCH, army=Army.AIR, vehicle_id="dummy_plane"
        )
        payload = build_presence(state)
        assert payload.small_image is None


class TestShowMapFlag:
    def test_map_hidden_when_show_map_false(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.TANK,
            vehicle_name="US M1 ABRAMS",
            map_name="El Alamein",
            mode="Ground Battle",
        )
        payload = build_presence(state, show_map=False)
        assert "El Alamein" not in payload.details
        assert " on " not in payload.details


class TestStartTimestamp:
    def test_match_started_at_is_propagated(self):
        state = GameState(activity=Activity.IN_MATCH, match_started_at=1234567890)
        payload = build_presence(state)
        assert payload.start == 1234567890

    def test_none_when_not_provided(self):
        state = GameState(activity=Activity.HANGAR, match_started_at=None)
        payload = build_presence(state)
        assert payload.start is None


class TestLengthLimits:
    @pytest.mark.parametrize("activity", list(Activity))
    def test_all_fields_within_discord_limit_for_absurdly_long_names(self, activity):
        long_name = "X" * 500
        state = GameState(
            activity=activity,
            army=Army.AIR,
            vehicle_id="x" * 500,
            vehicle_name=long_name,
            map_name=long_name,
            mode=long_name,
            flight=Flight(mach=0.94, ias_kph=1120.0),
            air_state=AirState.DOGFIGHTING,
        )
        payload = build_presence(state)
        assert len(payload.details) <= MAX_LEN
        assert len(payload.state) <= MAX_LEN
        assert len(payload.large_text) <= MAX_LEN
        if payload.small_text is not None:
            assert len(payload.small_text) <= MAX_LEN

    def test_length_limit_for_all_army_types(self):
        for army in Army:
            long_name = "Y" * 500
            state = GameState(
                activity=Activity.TEST_DRIVE,
                army=army,
                vehicle_name=long_name,
            )
            payload = build_presence(state)
            assert len(payload.state) <= MAX_LEN
            assert len(payload.details) <= MAX_LEN


class TestReturnsPresencePayload:
    def test_return_type(self):
        state = GameState(activity=Activity.HANGAR)
        payload = build_presence(state)
        assert isinstance(payload, PresencePayload)


class TestGroundKillSuffix:
    """The TANK/SHIP branch used to drop the kill count entirely."""

    def test_tank_with_ready_rack_appends_kills(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.TANK,
            vehicle_name="US M1A2 SEP2 ABRAMS",
            ground=Ground(ready_ammo=18.0),
            kills=3,
        )
        payload = build_presence(state, show_kills=True)
        assert payload.state == "US M1A2 SEP2 ABRAMS · 18 rounds · 3 kills"

    def test_tank_with_no_other_bits_still_appends_kills(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.TANK,
            vehicle_name="US M1A2 SEP2 ABRAMS",
            kills=1,
        )
        payload = build_presence(state, show_kills=True)
        assert payload.state == "US M1A2 SEP2 ABRAMS · 1 kill"

    def test_ship_with_no_kills_omits_suffix(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.SHIP,
            vehicle_name="US DESTROYER",
            kills=0,
        )
        payload = build_presence(state, show_kills=True)
        assert "kill" not in payload.state

    def test_tank_kills_hidden_when_show_kills_false(self):
        state = GameState(
            activity=Activity.IN_MATCH,
            army=Army.TANK,
            vehicle_name="US M1A2 SEP2 ABRAMS",
            kills=3,
        )
        payload = build_presence(state, show_kills=False)
        assert "kill" not in payload.state
