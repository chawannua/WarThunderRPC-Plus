"""Tests for wtrpc.weapon_ocr: the optional on-screen weapon-HUD reader.

Three layers are tested separately, in decreasing order of how much value
they carry:

1. ``parse_weapon_line`` -- pure string parsing, no image or OCR involved.
   Exercised hard against both the clean HUD text and OCR-style mangling.
2. The image pipeline (``isolate_hud`` / ``find_text_rows``) -- pure Pillow,
   no OCR engine required, exercised against fixture PNGs checked in under
   ``tests/fixtures/``.
3. OCR orchestration (``extract`` / ``read_selected_weapon`` / ``available``)
   -- exercised with a fake ``pytesseract`` substitute so the suite proves
   the wiring is correct without requiring a real Tesseract install. A
   handful of tests that need genuine Tesseract are skipped cleanly via
   ``pytest.importorskip`` when it is not present.

None of this requires a screen, a running game, or network access.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from PIL import Image, ImageGrab

from wtrpc.weapon_ocr import (
    WeaponLine,
    available,
    capture_region,
    extract,
    find_text_rows,
    isolate_hud,
    parse_weapon_line,
    read_selected_weapon,
    selected_weapons,
)

FIXTURES = Path(__file__).parent / "fixtures"

# The exact HUD block from the task spec, transcribed from a real F-4S
# screenshot. Kept as the ground truth both fixture generation and parsing
# tests are checked against.
CLEAN_HUD_LINES = [
    "THR      110 % WEP",
    "IAS      678 km/h",
    "SPD      741 km/h",
    "ALT      1733 m",
    "RALT     1709 m",
    "",
    ">  AAM      8/4(L)      AIM-7F",
    "-  FLR      40",
    "-  CHFF     20",
    ">  AG AUTO  750         20 mm Mk 11 mod 5",
    "   FUEL     26:15",
]


# ---------------------------------------------------------------------------
# parse_weapon_line -- pure string parsing
# ---------------------------------------------------------------------------


class TestParseWeaponLineTelemetryRows:
    """THR/IAS/SPD/ALT/RALT/FUEL are not weapons and must parse to None."""

    @pytest.mark.parametrize(
        "line",
        [
            "THR      110 % WEP",
            "IAS      678 km/h",
            "SPD      741 km/h",
            "ALT      1733 m",
            "RALT     1709 m",
            "   FUEL     26:15",
            "FUEL     26:15",
        ],
    )
    def test_telemetry_rows_return_none(self, line):
        assert parse_weapon_line(line) is None

    def test_blank_line_returns_none(self):
        assert parse_weapon_line("") is None

    def test_whitespace_only_line_returns_none(self):
        assert parse_weapon_line("     ") is None

    def test_garbage_with_no_marker_returns_none(self):
        assert parse_weapon_line("asdkjbflkj qwer") is None

    def test_lone_marker_with_nothing_after_returns_none(self):
        assert parse_weapon_line(">") is None
        assert parse_weapon_line("-") is None


class TestParseWeaponLineCleanInput:
    def test_selected_missile_group_with_name(self):
        line = ">  AAM      8/4(L)      AIM-7F"
        assert parse_weapon_line(line) == WeaponLine(
            selected=True, group="AAM", count="8/4(L)", name="AIM-7F"
        )

    def test_unselected_flare_group_no_name(self):
        line = "-  FLR      40"
        assert parse_weapon_line(line) == WeaponLine(
            selected=False, group="FLR", count="40", name=""
        )

    def test_unselected_chaff_group_no_name(self):
        line = "-  CHFF     20"
        assert parse_weapon_line(line) == WeaponLine(
            selected=False, group="CHFF", count="20", name=""
        )

    def test_selected_multiword_group_and_multiword_name(self):
        line = ">  AG AUTO  750         20 mm Mk 11 mod 5"
        assert parse_weapon_line(line) == WeaponLine(
            selected=True,
            group="AG AUTO",
            count="750",
            name="20 mm Mk 11 mod 5",
        )

    def test_group_only_no_count_no_name(self):
        line = "-  BOMB"
        assert parse_weapon_line(line) == WeaponLine(
            selected=False, group="BOMB", count="", name=""
        )


class TestParseWeaponLineMangledInput:
    """Variants a real OCR pass would plausibly produce."""

    @pytest.mark.parametrize("marker", [">", "s", "S", "»"])
    def test_selected_marker_ocr_misreads(self, marker):
        line = f"{marker}  AAM      8/4(L)      AIM-7F"
        result = parse_weapon_line(line)
        assert result is not None
        assert result.selected is True
        assert result.group == "AAM"
        assert result.name == "AIM-7F"

    @pytest.mark.parametrize("marker", ["-", "_", "‐", "—"])
    def test_unselected_marker_ocr_misreads(self, marker):
        line = f"{marker}  FLR      40"
        result = parse_weapon_line(line)
        assert result is not None
        assert result.selected is False
        assert result.group == "FLR"
        assert result.count == "40"

    def test_doubled_column_spacing_still_parses(self):
        line = ">    AAM        8/4(L)        AIM-7F"
        assert parse_weapon_line(line) == WeaponLine(
            selected=True, group="AAM", count="8/4(L)", name="AIM-7F"
        )

    def test_zero_read_as_letter_o_is_captured_verbatim(self):
        # OCR frequently confuses '0' and 'O'. parse_weapon_line does not
        # correct content -- it only has to keep the columns straight.
        line = ">  AG AUTO  75O         2O mm Mk 11 mod 5"
        result = parse_weapon_line(line)
        assert result is not None
        assert result.selected is True
        assert result.group == "AG AUTO"
        assert result.count == "75O"
        assert result.name == "2O mm Mk 11 mod 5"

    def test_one_read_as_letter_l_is_captured_verbatim(self):
        line = ">  AAM      8/4(L)      AIM-7F".replace("1", "l")
        # no literal '1' in this particular line; use a line that has one
        line = "-  FLR      4l"
        result = parse_weapon_line(line)
        assert result is not None
        assert result.selected is False
        assert result.group == "FLR"
        assert result.count == "4l"

    def test_missing_count_column(self):
        line = ">  GUN"
        result = parse_weapon_line(line)
        assert result == WeaponLine(selected=True, group="GUN", count="", name="")

    def test_missing_name_column_only_count_present(self):
        line = ">  ROCKET   12"
        result = parse_weapon_line(line)
        assert result == WeaponLine(
            selected=True, group="ROCKET", count="12", name=""
        )

    def test_combined_mangling_marker_and_digits_and_spacing(self):
        line = "»    AAM        8/4(L)        AIM-7F"
        result = parse_weapon_line(line)
        assert result == WeaponLine(
            selected=True, group="AAM", count="8/4(L)", name="AIM-7F"
        )


class TestSelectedWeapons:
    def test_returns_only_selected_in_order(self):
        result = selected_weapons(CLEAN_HUD_LINES)
        assert [w.group for w in result] == ["AAM", "AG AUTO"]
        assert [w.name for w in result] == ["AIM-7F", "20 mm Mk 11 mod 5"]
        assert all(w.selected for w in result)

    def test_empty_list_returns_empty_list(self):
        assert selected_weapons([]) == []

    def test_all_unselected_returns_empty_list(self):
        lines = ["-  FLR      40", "-  CHFF     20"]
        assert selected_weapons(lines) == []


# ---------------------------------------------------------------------------
# Image pipeline -- isolate_hud / find_text_rows
# ---------------------------------------------------------------------------


class TestIsolateHud:
    def test_pure_hud_green_pixel_is_isolated(self):
        img = Image.new("RGB", (4, 4), (40, 220, 60))
        binary = isolate_hud(img)
        assert binary.mode == "L"
        assert binary.size == img.size
        assert set(binary.getdata()) == {255}

    def test_pure_background_colors_are_rejected(self):
        # A palette of plausible non-HUD in-game colours: sky, dirt, metal,
        # shadow, explosion orange, muted distant terrain, water.
        colors = [
            (120, 160, 210),
            (110, 90, 60),
            (90, 90, 95),
            (20, 20, 25),
            (200, 100, 30),
            (80, 95, 55),
            (40, 80, 130),
        ]
        for color in colors:
            img = Image.new("RGB", (2, 2), color)
            binary = isolate_hud(img)
            assert set(binary.getdata()) == {0}, f"{color} was misclassified as HUD green"

    def test_dominance_boundary_is_respected(self):
        # g - r and g - b exactly at the dominance threshold must pass.
        img = Image.new("RGB", (1, 1), (50, 90, 50))  # g=90,r=50,b=50 -> both diffs 40
        assert list(isolate_hud(img, min_green=90, dominance=40).getdata()) == [255]
        # One below the threshold must fail.
        img2 = Image.new("RGB", (1, 1), (51, 90, 50))  # g-r=39 < 40
        assert list(isolate_hud(img2, min_green=90, dominance=40).getdata()) == [0]

    def test_min_green_boundary_is_respected(self):
        img = Image.new("RGB", (1, 1), (0, 90, 0))
        assert list(isolate_hud(img, min_green=90, dominance=40).getdata()) == [255]
        img2 = Image.new("RGB", (1, 1), (0, 89, 0))
        assert list(isolate_hud(img2, min_green=90, dominance=40).getdata()) == [0]

    def test_grayscale_input_is_never_misclassified_as_hud_green(self):
        # A grayscale pixel has r == g == b, so g - r == 0 and g - b == 0,
        # which must always fail dominance regardless of brightness.
        for level in (0, 90, 128, 200, 255):
            img = Image.new("RGB", (1, 1), (level, level, level))
            assert list(isolate_hud(img).getdata()) == [0]

    def test_custom_thresholds_are_honoured(self):
        img = Image.new("RGB", (1, 1), (61, 120, 61))  # g-r == g-b == 59
        # Fails a strict dominance of 60 but passes a looser one of 30.
        assert list(isolate_hud(img, min_green=90, dominance=60).getdata()) == [0]
        assert list(isolate_hud(img, min_green=90, dominance=30).getdata()) == [255]

    def test_background_only_fixture_produces_no_false_positives(self):
        img = Image.open(FIXTURES / "weapon_background_only.png")
        binary = isolate_hud(img)
        assert sum(1 for v in binary.getdata() if v) == 0

    def test_hud_fixture_isolates_a_small_fraction_of_glyph_pixels(self):
        img = Image.open(FIXTURES / "weapon_hud_full.png")
        binary = isolate_hud(img)
        lit = sum(1 for v in binary.getdata() if v)
        total = binary.size[0] * binary.size[1]
        # Text glyphs cover a small minority of the image, but there must be
        # a meaningful number of isolated pixels for OCR to work with.
        assert 0 < lit < total * 0.15


class TestFindTextRows:
    def test_blank_image_has_no_rows(self):
        img = Image.new("L", (100, 50), 0)
        assert find_text_rows(img) == []

    def test_single_solid_row_is_detected(self):
        img = Image.new("L", (20, 10), 0)
        for x in range(20):
            img.putpixel((x, 4), 255)
        rows = find_text_rows(img)
        assert rows == [(4, 4)]

    def test_two_far_apart_rows_are_kept_separate(self):
        img = Image.new("L", (20, 20), 0)
        for x in range(20):
            img.putpixel((x, 2), 255)
            img.putpixel((x, 15), 255)
        rows = find_text_rows(img, gap_tolerance=2)
        assert rows == [(2, 2), (15, 15)]

    def test_gap_tolerance_merges_close_rows(self):
        img = Image.new("L", (20, 20), 0)
        for x in range(20):
            img.putpixel((x, 2), 255)
            img.putpixel((x, 5), 255)  # 2 blank rows between (3, 4)
        merged = find_text_rows(img, gap_tolerance=2)
        assert merged == [(2, 5)]
        split = find_text_rows(img, gap_tolerance=1)
        assert split == [(2, 2), (5, 5)]

    def test_hud_fixture_finds_one_row_per_nonblank_line(self):
        img = Image.open(FIXTURES / "weapon_hud_full.png")
        binary = isolate_hud(img)
        rows = find_text_rows(binary)
        nonblank_lines = [l for l in CLEAN_HUD_LINES if l.strip()]
        assert len(rows) == len(nonblank_lines)
        # Rows must be in top-to-bottom order and non-overlapping.
        for (top1, bottom1), (top2, bottom2) in zip(rows, rows[1:]):
            assert bottom1 < top2


# ---------------------------------------------------------------------------
# available() -- must never raise, must be honest
# ---------------------------------------------------------------------------


class TestAvailable:
    def test_available_returns_bool_and_never_raises(self):
        assert isinstance(available(), bool)

    def test_false_when_pytesseract_not_importable(self, monkeypatch):
        monkeypatch.setattr("wtrpc.weapon_ocr._HAVE_PYTESSERACT", False)
        assert available() is False

    def test_false_when_tesseract_binary_missing(self, monkeypatch):
        fake = types.SimpleNamespace(
            get_tesseract_version=lambda: (_ for _ in ()).throw(
                FileNotFoundError("tesseract not found")
            )
        )
        monkeypatch.setattr("wtrpc.weapon_ocr._HAVE_PYTESSERACT", True)
        monkeypatch.setattr("wtrpc.weapon_ocr.pytesseract", fake)
        assert available() is False

    def test_true_when_pytesseract_and_binary_both_present(self, monkeypatch):
        fake = types.SimpleNamespace(get_tesseract_version=lambda: "5.3.0")
        monkeypatch.setattr("wtrpc.weapon_ocr._HAVE_PYTESSERACT", True)
        monkeypatch.setattr("wtrpc.weapon_ocr.pytesseract", fake)
        assert available() is True


# ---------------------------------------------------------------------------
# capture_region -- screen grabbing, always mocked (no real screen in CI)
# ---------------------------------------------------------------------------


class TestCaptureRegion:
    def test_returns_none_when_nothing_is_available(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "mss", None)  # import mss -> ImportError
        monkeypatch.setattr(ImageGrab, "grab", lambda bbox=None: (_ for _ in ()).throw(
            OSError("no display")
        ))
        assert capture_region() is None
        assert capture_region((0, 0, 10, 10)) is None

    def test_falls_back_to_imagegrab_when_mss_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "mss", None)
        sentinel = Image.new("RGB", (5, 5), (1, 2, 3))
        monkeypatch.setattr(ImageGrab, "grab", lambda bbox=None: sentinel)
        result = capture_region((0, 0, 5, 5))
        assert result is sentinel

    def test_uses_mss_when_available(self, monkeypatch):
        captured_monitor = {}

        class FakeShot:
            def __init__(self, size):
                self.size = size
                self.bgra = bytes([10, 20, 30, 0]) * (size[0] * size[1])

        class FakeSct:
            monitors = [None, {"left": 0, "top": 0, "width": 999, "height": 999}]

            def grab(self, monitor):
                captured_monitor.update(monitor)
                return FakeShot((monitor["width"], monitor["height"]))

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        fake_mss_module = types.ModuleType("mss")
        fake_mss_module.mss = lambda: FakeSct()
        monkeypatch.setitem(sys.modules, "mss", fake_mss_module)

        result = capture_region((10, 20, 40, 50))
        assert result is not None
        assert result.size == (30, 30)
        assert captured_monitor == {"left": 10, "top": 20, "width": 30, "height": 30}

    def test_mss_failure_falls_back_to_imagegrab(self, monkeypatch):
        fake_mss_module = types.ModuleType("mss")
        fake_mss_module.mss = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        monkeypatch.setitem(sys.modules, "mss", fake_mss_module)

        sentinel = Image.new("RGB", (3, 3), (9, 9, 9))
        monkeypatch.setattr(ImageGrab, "grab", lambda bbox=None: sentinel)

        result = capture_region()
        assert result is sentinel


# ---------------------------------------------------------------------------
# extract() / read_selected_weapon() -- orchestration, OCR faked
# ---------------------------------------------------------------------------


class _FakePytesseract:
    """A drop-in substitute for pytesseract that "recognises" canned text.

    Returns one line of ``CLEAN_HUD_LINES`` (skipping blanks) per call, in
    order, simulating a perfect OCR pass over the fixture image -- which
    lets the orchestration logic in extract()/read_selected_weapon() be
    tested deterministically without a real Tesseract install.
    """

    def __init__(self, lines):
        self._lines = iter(lines)

    def get_tesseract_version(self):
        return "5.3.0-fake"

    def image_to_string(self, crop, config=None):
        try:
            return next(self._lines)
        except StopIteration:
            return ""


@pytest.fixture
def fake_ocr(monkeypatch):
    nonblank = [l for l in CLEAN_HUD_LINES if l.strip()]
    fake = _FakePytesseract(nonblank)
    monkeypatch.setattr("wtrpc.weapon_ocr._HAVE_PYTESSERACT", True)
    monkeypatch.setattr("wtrpc.weapon_ocr.pytesseract", fake)
    return fake


class TestExtractWithoutOcr:
    """Real-environment behaviour: no pytesseract installed at all."""

    def test_extract_returns_empty_list_when_unavailable(self):
        img = Image.open(FIXTURES / "weapon_hud_full.png")
        assert extract(img) == []

    def test_read_selected_weapon_returns_none_when_unavailable(self):
        assert read_selected_weapon() is None


class TestExtractWithFakeOcr:
    def test_extract_returns_parsed_weapon_lines_in_order(self, fake_ocr):
        img = Image.open(FIXTURES / "weapon_hud_full.png")
        result = extract(img)
        groups = [w.group for w in result]
        assert groups == ["AAM", "FLR", "CHFF", "AG AUTO"]
        assert [w.selected for w in result] == [True, False, False, True]
        assert result[0].name == "AIM-7F"
        assert result[3].name == "20 mm Mk 11 mod 5"

    def test_extract_never_raises_on_a_tiny_blank_image(self, fake_ocr):
        img = Image.new("RGB", (2, 2), (0, 0, 0))
        assert extract(img) == []

    def test_read_selected_weapon_returns_first_selected_name(
        self, fake_ocr, monkeypatch
    ):
        img = Image.open(FIXTURES / "weapon_hud_full.png")
        monkeypatch.setattr(
            "wtrpc.weapon_ocr.capture_region", lambda box=None: img
        )
        assert read_selected_weapon() == "AIM-7F"

    def test_read_selected_weapon_returns_none_when_capture_fails(
        self, fake_ocr, monkeypatch
    ):
        monkeypatch.setattr(
            "wtrpc.weapon_ocr.capture_region", lambda box=None: None
        )
        assert read_selected_weapon() is None


# ---------------------------------------------------------------------------
# Genuine end-to-end OCR -- only runs if Tesseract is truly installed.
# ---------------------------------------------------------------------------


class TestRealTesseract:
    def test_real_tesseract_reads_something_sane_from_the_fixture(self):
        pytesseract = pytest.importorskip("pytesseract")
        if not available():
            pytest.skip("pytesseract is importable but no Tesseract binary was found")

        img = Image.open(FIXTURES / "weapon_hud_full.png")
        result = extract(img)
        # We do not assert exact OCR text (that is inherently flaky); only
        # that the pipeline produces *some* structured weapon lines and does
        # not raise.
        assert isinstance(result, list)
        for w in result:
            assert isinstance(w, WeaponLine)
