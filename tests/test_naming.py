"""Tests for wtrpc.naming — vehicle name/id formatting and encyclopedia URLs."""

from wtrpc.naming import (
    PLACEHOLDER_IDS,
    is_placeholder,
    format_vehicle,
    encyclopedia_url,
    encyclopedia_image_url,
)


class TestPlaceholderIds:
    def test_dummy_plane_lowercase_is_placeholder(self):
        assert is_placeholder("dummy_plane") is True

    def test_dummy_plane_uppercase_is_placeholder(self):
        assert is_placeholder("DUMMY_PLANE") is True

    def test_dummy_plane_mixed_case_is_placeholder(self):
        assert is_placeholder("Dummy_Plane") is True

    def test_real_vehicle_is_not_placeholder(self):
        assert is_placeholder("f_4e") is False

    def test_empty_string_is_placeholder(self):
        assert is_placeholder("") is True

    def test_placeholder_ids_contains_dummy_plane(self):
        assert "dummy_plane" in PLACEHOLDER_IDS


class TestFormatVehicle:
    def test_simple_aircraft_name(self):
        vehicle_id, display_name = format_vehicle("f_4e")
        assert vehicle_id == "f_4e"
        assert display_name == "F 4E"

    def test_strips_tank_models_prefix(self):
        vehicle_id, display_name = format_vehicle("tankModels/us_m1_abrams")
        assert vehicle_id == "us_m1_abrams"
        assert display_name == "US M1 ABRAMS"

    def test_strips_arbitrary_prefix(self):
        vehicle_id, display_name = format_vehicle("shipModels/us_destroyer")
        assert vehicle_id == "us_destroyer"
        assert display_name == "US DESTROYER"

    def test_strips_multiple_prefixes(self):
        vehicle_id, display_name = format_vehicle("a/b/us_tank")
        assert vehicle_id == "us_tank"
        assert display_name == "US TANK"

    def test_empty_input_yields_empty_tuple(self):
        assert format_vehicle("") == ("", "")

    def test_placeholder_input_yields_empty_tuple(self):
        assert format_vehicle("dummy_plane") == ("", "")
        assert format_vehicle("DUMMY_PLANE") == ("", "")

    def test_underscores_replaced_with_spaces(self):
        _, display_name = format_vehicle("us_p_51d_25nt")
        assert "_" not in display_name
        assert display_name == "US P 51D 25NT"


class TestEncyclopediaUrls:
    def test_encyclopedia_url_points_at_the_wiki_unit_page(self):
        assert encyclopedia_url("us_m1_abrams") == (
            "https://wiki.warthunder.com/unit/us_m1_abrams"
        )

    def test_encyclopedia_image_url_builds_expected_path(self):
        # The bare encyclopedia.warthunder.com host the original project used
        # no longer resolves; images live on the static CDN now.
        assert encyclopedia_image_url("us_m1_abrams") == (
            "https://static.encyclopedia.warthunder.com/images/us_m1_abrams.png"
        )

    def test_image_url_does_not_use_the_retired_host(self):
        url = encyclopedia_image_url("b-17e")
        assert "//encyclopedia.warthunder.com" not in url

    def test_encyclopedia_url_empty_for_empty_id(self):
        assert encyclopedia_url("") == ""

    def test_encyclopedia_image_url_empty_for_empty_id(self):
        assert encyclopedia_image_url("") == ""

    def test_encyclopedia_url_empty_for_placeholder(self):
        assert encyclopedia_url("dummy_plane") == ""

    def test_encyclopedia_image_url_empty_for_placeholder(self):
        assert encyclopedia_image_url("DUMMY_PLANE") == ""
