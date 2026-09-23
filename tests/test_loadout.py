"""Tests for wtrpc.loadout.

The block below is copied verbatim from a real War Thunder profile save.
"""

from __future__ import annotations

import pathlib

import pytest

import wtrpc.loadout as loadout
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


class TestDocumentsDirs:
    def test_dedupes_case_insensitively(self, monkeypatch):
        monkeypatch.setattr(loadout, "_known_folder_documents", lambda: pathlib.Path("C:/Users/x/Documents"))
        monkeypatch.setattr(pathlib.Path, "home", lambda: pathlib.Path("C:/Users/x"))
        dirs = loadout._documents_dirs()
        lowered = [str(d).lower() for d in dirs]
        assert len(lowered) == len(set(lowered)), "duplicate Documents dirs must be deduped"

    def test_falls_back_when_shell_lookup_unavailable(self, monkeypatch):
        monkeypatch.setattr(loadout, "_known_folder_documents", lambda: None)
        monkeypatch.setattr(pathlib.Path, "home", lambda: pathlib.Path("C:/Users/x"))
        dirs = loadout._documents_dirs()
        assert pathlib.Path("C:/Users/x/Documents") in dirs
        assert pathlib.Path("C:/Users/x/OneDrive/Documents") in dirs

    def test_known_folder_result_comes_first(self, monkeypatch):
        shell_dir = pathlib.Path("D:/Redirected/Documents")
        monkeypatch.setattr(loadout, "_known_folder_documents", lambda: shell_dir)
        monkeypatch.setattr(pathlib.Path, "home", lambda: pathlib.Path("C:/Users/x"))
        dirs = loadout._documents_dirs()
        assert dirs[0] == shell_dir


class TestSaveFileResolution:
    def test_uses_injectable_base_dirs(self, monkeypatch, tmp_path):
        """save_file() must search whatever _documents_dirs() returns."""
        save_dir = tmp_path / "My Games" / "WarThunder" / "Saves" / "last" / "production"
        save_dir.mkdir(parents=True)
        (save_dir / "global.blk").write_text("x", encoding="utf-8")
        monkeypatch.setattr(loadout, "_documents_dirs", lambda: [tmp_path])
        assert save_file() == save_dir / "global.blk"

    def test_never_raises_when_a_candidate_mtime_is_unreadable(self, monkeypatch, tmp_path):
        """A permission error on one save's stat() must not escape save_file()."""
        base = tmp_path / "My Games" / "WarThunder" / "Saves"
        for name in ("alpha", "beta"):
            d = base / name / "production"
            d.mkdir(parents=True)
            (d / "global.blk").write_text("x", encoding="utf-8")
        monkeypatch.setattr(loadout, "_documents_dirs", lambda: [tmp_path])

        real_stat = pathlib.Path.stat

        def flaky_stat(self, *a, **kw):
            if self.parent.parent.name == "alpha":
                raise OSError("permission denied")
            return real_stat(self, *a, **kw)

        monkeypatch.setattr(pathlib.Path, "stat", flaky_stat)
        result = save_file()
        assert result is not None and result.name == "global.blk"

    def test_never_raises_when_no_documents_dirs_are_readable(self, monkeypatch):
        monkeypatch.setattr(loadout, "_documents_dirs", lambda: [pathlib.Path("Z:/does/not/exist")])
        assert save_file() is None


def test_read_loadout_never_raises_even_if_save_file_blows_up(monkeypatch):
    def boom():
        raise OSError("disk error")

    monkeypatch.setattr(loadout, "save_file", boom)
    assert read_loadout("us_m1a2_sep2_abrams") == []
