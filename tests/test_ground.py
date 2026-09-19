"""Tests for wtrpc.ground.

The two indicator dictionaries below are verbatim captures of the same M1A2
SEP V2 taken minutes apart, one intact and one badly shot up. They are what
established the field semantics, which are not guessable from the names.
"""

from __future__ import annotations

import pytest

from wtrpc.ground import (
    ammo_label,
    crew_label,
    damage_label,
    read_ground,
)
from wtrpc.models import Activity, Army, GameState, Ground
from wtrpc.presence_builder import build_presence

# Captured live. Note what is NOT here: no breach_dead, no v_drive_broken,
# no h_drive_dead. The game omits a damage field entirely while the component
# is fine rather than reporting it as zero.
HEALTHY = {
    "army": "tank",
    "type": "tankModels/us_m1a2_sep2_abrams",
    "valid": True,
    "first_stage_ammo": 18.0,
    "crew_current": 4.0,
    "crew_total": 4.0,
    "gunner_state": 0.0,
    "driver_state": 0.0,
    "stabilizer": 1.0,
    "speed": 45.0,
    "rpm": 2703.0,
    "gear": 10.0,
}

# The same tank after taking hits.
DAMAGED = {
    "army": "tank",
    "type": "tankModels/us_m1a2_sep2_abrams",
    "valid": True,
    "first_stage_ammo": 11.0,
    "crew_current": 1.0,
    "crew_total": 4.0,
    "gunner_state": 1.0,
    "driver_state": 1.0,
    "breach_dead": 1.0,
    "v_drive_broken": 1.0,
    "h_drive_dead": 1.0,
    "stabilizer": 1.0,
    "speed": 0.0,
}


class TestReadGround:
    def test_healthy_capture(self):
        g = read_ground(HEALTHY)
        assert g.ready_ammo == 18.0
        assert (g.crew_alive, g.crew_total) == (4, 4)
        assert g.damage == ()

    def test_damaged_capture(self):
        g = read_ground(DAMAGED)
        assert g.ready_ammo == 11.0
        assert (g.crew_alive, g.crew_total) == (1, 4)
        assert "Breech destroyed" in g.damage
        assert "Vertical drive out" in g.damage

    def test_absent_damage_field_means_healthy(self):
        """The game omits the field entirely rather than sending a zero."""
        assert read_ground(HEALTHY).damage == ()

    def test_crew_state_fields_are_not_interpreted(self):
        """gunner_state/driver_state are deliberately ignored.

        Two M1A2 captures suggested 1 meant "out of action". An HSTV-L then
        reported 1 for both while carrying a full crew of three, which that
        reading cannot explain -- so the field is not used at all rather than
        printing "Gunner down" about an intact crew.
        """
        assert read_ground({"gunner_state": 1.0}).damage == ()
        assert read_ground({"driver_state": 1.0}).damage == ()
        assert read_ground(
            {"gunner_state": 1.0, "driver_state": 1.0, "crew_current": 3.0,
             "crew_total": 3.0}
        ).damage == ()

    @pytest.mark.parametrize("indicators", [None, {}, {"army": "tank"}])
    def test_missing_data_is_not_fatal(self, indicators):
        g = read_ground(indicators)
        assert g.ready_ammo is None and g.damage == ()

    def test_junk_values_are_ignored(self):
        g = read_ground({"first_stage_ammo": "lots", "crew_current": None})
        assert g.ready_ammo is None and g.crew_alive is None

    def test_a_bool_is_not_a_number(self):
        assert read_ground({"first_stage_ammo": True}).ready_ammo is None


class TestLabels:
    def test_ammo_is_singular_at_one(self):
        assert ammo_label(Ground(ready_ammo=1.0)) == "1 round"
        assert ammo_label(Ground(ready_ammo=18.0)) == "18 rounds"
        assert ammo_label(Ground(ready_ammo=0.0)) == "0 rounds"

    def test_ammo_unknown_is_blank(self):
        assert ammo_label(Ground()) == ""

    def test_full_crew_is_not_worth_saying(self):
        assert crew_label(Ground(crew_alive=4, crew_total=4)) == ""

    def test_depleted_crew_is(self):
        assert crew_label(Ground(crew_alive=1, crew_total=4)) == "1/4 crew"

    def test_only_the_worst_damage_is_reported(self):
        g = Ground(damage=("Gunner down", "Breech destroyed", "Driver down"))
        assert damage_label(g) == "Gunner down"

    def test_no_damage_is_blank(self):
        assert damage_label(Ground()) == ""


class TestGroundPresence:
    """A tank used to show nothing but its own name."""

    @staticmethod
    def _state(indicators: dict) -> GameState:
        return GameState(
            activity=Activity.IN_MATCH,
            army=Army.TANK,
            vehicle_id="us_m1a2_sep2_abrams",
            vehicle_name="US M1A2 SEP2 ABRAMS",
            map_name="Fulda",
            mode="Ground Domination",
            ground=read_ground(indicators),
            match_started_at=1,
        )

    def test_healthy_tank_shows_its_ready_rack(self):
        payload = build_presence(self._state(HEALTHY))
        assert payload.state == "US M1A2 SEP2 ABRAMS · 18 rounds"
        assert payload.details == "Ground Domination on Fulda"

    def test_damaged_tank_shows_crew_and_damage(self):
        payload = build_presence(self._state(DAMAGED))
        assert "1/4 crew" in payload.state
        assert "11 rounds" in payload.state
        assert payload.details.startswith("Breech destroyed")

    def test_no_mach_or_airspeed_for_a_tank(self):
        """/state is aviation only; a tank must never claim a Mach number."""
        payload = build_presence(self._state(HEALTHY))
        assert "Mach" not in payload.state
        assert "IAS" not in payload.state

    def test_tank_without_data_still_names_the_vehicle(self):
        payload = build_presence(self._state({}))
        assert payload.state == "US M1A2 SEP2 ABRAMS"

    def test_fields_stay_within_the_discord_limit(self):
        payload = build_presence(self._state(DAMAGED))
        for value in (payload.details, payload.state):
            assert 0 < len(value) <= 128
