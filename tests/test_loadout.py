"""Tests for wtrpc.loadout.

The block below is copied verbatim from a real War Thunder profile save.
"""

from __future__ import annotations

import pytest

from wtrpc.loadout import (
    classify_shell,
    loadout_label,
    read_loadout,
    save_file,
)

REAL_SAVE = """
version:i=28497

content{
  profile{
    aircrafts{
      us_m1a2_sep2_abrams{
        crosshair:t="us_modern_sight_without_range"
      }
    }
    unit_option{
      us_m1a2_sep2_abrams{
        USEROPT_WEAPONS:t="us_m1a2_sep2_abrams_default"
        USEROPT_BULLETS0:t="120mm_M829A_APDS_FS"
        USEROPT_BULLET_COUNT0:i=23
        USEROPT_BULLET_COUNT1:i=0
        USEROPT_BULLETS2:t="NATO_APDS_FS"
        USEROPT_BULLET_COUNT2:i=0
        USEROPT_BULLETS3:t="120mm_DM_HEAT_FS"
        USEROPT_BULLET_COUNT3:i=5
        USEROPT_BULLETS1:t=""
        USEROPT_BULLETS4:t=""
        USEROPT_BULLETS5:t=""
      }
      us_m18_hellcat{
        USEROPT_BULLETS0:t="76mm_usa_M62_APCBC"
        USEROPT_BULLET_COUNT0:i=30
        USEROPT_BULLETS1:t="76mm_usa_M42A1_HE"
        USEROPT_BULLET_COUNT1:i=15
      }
    }
  }
}
"""


@pytest.fixture
def save(tmp_path):
    path = tmp_path / "global.blk"
    path.write_text(REAL_SAVE, encoding="utf-8")
    return path


class TestClassifyShell:
    @pytest.mark.parametrize(
        "internal,expected",
        [
            ("120mm_M829A_APDS_FS", "APFSDS"),
            ("NATO_APDS_FS", "APFSDS"),
            ("120mm_DM_HEAT_FS", "HEATFS"),
            ("76mm_usa_M62_APCBC", "APCBC"),
            ("76mm_usa_M42A1_HE", "HE"),
            ("37mm_usa_m3_APC", "APC"),
        ],
    )
    def test_real_internal_names(self, internal, expected):
        assert classify_shell(internal) == expected

    def test_apds_fs_is_not_shortened_to_apds(self):
        """Longest patterns must win, or every APFSDS reads as APDS."""
        assert classify_shell("120mm_M829A_APDS_FS") == "APFSDS"

    def test_unrecognisable_name_is_blank_not_a_guess(self):
        assert classify_shell("37mm_default") == ""
        assert classify_shell("") == ""


class TestReadLoadout:
    def test_reads_the_real_block(self, save):
        assert read_loadout("us_m1a2_sep2_abrams", save) == [
            ("APFSDS", 23),
            ("HEATFS", 5),
        ]

    def test_zero_count_belts_are_dropped(self, save):
        """NATO_APDS_FS is listed with a count of 0 -- it is not carried."""
        shells = dict(read_loadout("us_m1a2_sep2_abrams", save))
        assert shells["APFSDS"] == 23, "only the belt with rounds should count"

    def test_sorted_by_rounds_carried(self, save):
        assert [n for n, _ in read_loadout("us_m18_hellcat", save)] == ["APCBC", "HE"]

    def test_picks_the_block_with_ammunition_not_the_first_match(self, save):
        """The same vehicle id also appears under the sights section."""
        assert read_loadout("us_m1a2_sep2_abrams", save)

    def test_unknown_vehicle_is_empty(self, save):
        assert read_loadout("not_a_vehicle", save) == []

    def test_empty_vehicle_id_is_empty(self, save):
        assert read_loadout("", save) == []

    def test_missing_file_is_empty_not_an_error(self, tmp_path):
        assert read_loadout("us_m1a2_sep2_abrams", tmp_path / "nope.blk") == []

    def test_garbage_file_is_empty_not_an_error(self, tmp_path):
        path = tmp_path / "junk.blk"
        path.write_bytes(b"\x00\xff\xfe not blk at all")
        assert read_loadout("us_m1a2_sep2_abrams", path) == []


class TestLoadoutLabel:
    def test_joins_the_biggest_belts(self):
        assert loadout_label([("APFSDS", 23), ("HEATFS", 5)]) == "APFSDS + HEATFS"

    def test_limits_how_many_are_named(self):
        shells = [("APFSDS", 23), ("HEATFS", 5), ("SMOKE", 4)]
        assert loadout_label(shells, limit=2) == "APFSDS + HEATFS"

    def test_single_belt(self):
        assert loadout_label([("APFSDS", 20)]) == "APFSDS"

    def test_empty_is_blank(self):
        assert loadout_label([]) == ""


def test_save_file_lookup_does_not_raise():
    """It may or may not find one depending on the machine; it must not blow up."""
    result = save_file()
    assert result is None or result.name == "global.blk"
