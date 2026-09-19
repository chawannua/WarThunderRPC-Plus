"""Tests for wtrpc.modes — language-resilient match-mode classification."""

from wtrpc.models import Army
from wtrpc.modes import classify_mode


class TestEnglishPrefixMatching:
    def test_capture_and_maintain_superiority_over_airfields_is_air_domination(self):
        assert (
            classify_mode(
                "Capture and maintain superiority over the airfields", Army.AIR
            )
            == "Air Domination"
        )

    def test_capture_and_hold_airfields_variant(self):
        assert (
            classify_mode("Capture and hold airfields.", Army.AIR)
            == "Air Domination"
        )

    def test_capture_and_hold_the_airfield_variant(self):
        assert (
            classify_mode("Capture and hold the airfield", Army.AIR)
            == "Air Domination"
        )

    def test_capture_and_maintain_superiority_over_air_zone_variant(self):
        assert (
            classify_mode(
                "Capture and maintain superiority over the air zone", Army.AIR
            )
            == "Air Domination"
        )

    def test_capture_and_maintain_superiority_over_points_is_domination(self):
        assert (
            classify_mode(
                "Capture and maintain superiority over the points", Army.TANK
            )
            == "Ground Domination"
        )

    def test_capture_the_enemy_point_is_battle(self):
        assert classify_mode("Capture the enemy point", Army.TANK) == "Ground Battle"

    def test_prevent_capture_of_allied_point_is_battle(self):
        assert (
            classify_mode("Prevent capture of allied point", Army.TANK)
            == "Ground Battle"
        )

    def test_prevent_the_capture_of_the_allied_point_is_battle(self):
        assert (
            classify_mode("Prevent the capture of the allied point", Army.SHIP)
            == "Naval Battle"
        )

    def test_capture_and_keep_hold_of_point_is_conquest(self):
        assert (
            classify_mode("Capture and keep hold of the point", Army.TANK)
            == "Ground Conquest"
        )

    def test_destroy_enemy_ground_vehicles_is_air_ground_strike(self):
        assert (
            classify_mode("Destroy the enemy ground vehicles", Army.AIR)
            == "Air Ground Strike"
        )

    def test_destroy_highlighted_targets_is_air_frontline(self):
        assert (
            classify_mode("Destroy the highlighted targets", Army.AIR)
            == "Air Frontline"
        )

    def test_case_insensitive_matching(self):
        assert (
            classify_mode("CAPTURE AND HOLD THE AIRFIELD", Army.AIR)
            == "Air Domination"
        )

    def test_tolerates_leading_trailing_whitespace(self):
        assert (
            classify_mode("   Capture and hold the airfield   ", Army.AIR)
            == "Air Domination"
        )


class TestArmyPrefixing:
    def test_air_army_prefixes_domination_with_air(self):
        result = classify_mode("Capture and hold the airfield", Army.AIR)
        assert result.startswith("Air ")

    def test_tank_army_prefixes_domination_with_ground(self):
        result = classify_mode(
            "Capture and maintain superiority over the points", Army.TANK
        )
        assert result.startswith("Ground ")


class TestUnrecognizedLanguageFallback:
    """The fix for the language bug: no English match must never fall
    through to a wrong label. Fall back to a generic derived from army."""

    def test_thai_objective_text_air_army_falls_back_to_generic(self):
        thai_text = "ยึดครองและรักษาสนามบิน"
        assert classify_mode(thai_text, Army.AIR) == "Air Battle"

    def test_thai_objective_text_tank_army_falls_back_to_generic(self):
        thai_text = "ทำลายยานพาหนะภาคพื้นดินของศัตรู"
        assert classify_mode(thai_text, Army.TANK) == "Ground Battle"

    def test_russian_objective_text_air_army_falls_back_to_generic(self):
        russian_text = "Захватить и удерживать превосходство над аэродромами"
        assert classify_mode(russian_text, Army.AIR) == "Air Battle"

    def test_russian_objective_text_ship_army_falls_back_to_generic(self):
        russian_text = "Уничтожить вражеские корабли"
        assert classify_mode(russian_text, Army.SHIP) == "Naval Battle"

    def test_unknown_army_with_unrecognized_text_returns_empty(self):
        assert classify_mode("some unrecognized text", Army.UNKNOWN) == ""

    def test_empty_objective_text_air_army_falls_back_to_generic(self):
        assert classify_mode("", Army.AIR) == "Air Battle"


class TestGenericArmyFallbacks:
    def test_air_generic(self):
        assert classify_mode("gibberish", Army.AIR) == "Air Battle"

    def test_tank_generic(self):
        assert classify_mode("gibberish", Army.TANK) == "Ground Battle"

    def test_ship_generic(self):
        assert classify_mode("gibberish", Army.SHIP) == "Naval Battle"

    def test_unknown_generic(self):
        assert classify_mode("gibberish", Army.UNKNOWN) == ""
