"""Optional OCR reader for War Thunder's on-screen weapon-selection HUD.

War Thunder's local telemetry server on ``127.0.0.1:8111`` does not expose
which weapon or gun group is currently selected. All 8 endpoints, 79
``/indicators`` fields and 32 ``/state`` fields were checked across 468
samples taken over three minutes while actively cycling weapons; the only
armament-related fields are ``weapon2`` and ``weapon3``, both undocumented
and both ``0.0`` for the entire session. There is no API route to this
information, so reading the screen is the only remaining option.

The HUD block this module targets is rendered in a single flat green, on top
of the arbitrary game image, in the top-left of the screen. Transcribed from
a real screenshot of an F-4S::

    THR      110 % WEP
    IAS      678 km/h
    SPD      741 km/h
    ALT      1733 m
    RALT     1709 m

    >  AAM      8/4(L)      AIM-7F
    -  FLR      40
    -  CHFF     20
    >  AG AUTO  750         20 mm Mk 11 mod 5
       FUEL     26:15

A leading ``>`` marks a selected weapon group, ``-`` an unselected one. THR,
IAS, SPD, ALT, RALT and FUEL are plain telemetry rows -- not weapons -- and
carry no marker at all.

This entire feature is OPTIONAL and must be safe to ship disabled by
default: every third-party dependency (``pytesseract``, the Tesseract
binary itself, ``mss``) is imported lazily inside try/except, ``available()``
honestly reports whether extraction can work right now, and every
extraction function returns ``None`` (or an empty list), logging at debug
level, instead of raising. Nothing in this module may raise into a caller.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from PIL import Image

log = logging.getLogger(__name__)

try:  # pragma: no cover - exercised indirectly, depends on the environment
    import pytesseract

    _HAVE_PYTESSERACT = True
except ImportError:  # pragma: no cover - depends on the environment
    pytesseract = None  # type: ignore[assignment]
    _HAVE_PYTESSERACT = False


def available() -> bool:
    """Return whether weapon-name extraction can actually work right now.

    ``True`` only when ``pytesseract`` is importable AND it can find a
    working Tesseract binary. Never raises.
    """
    if not _HAVE_PYTESSERACT:
        log.debug("pytesseract is not installed; weapon OCR unavailable")
        return False
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:  # noqa: BLE001 - genuinely must never raise
        log.debug("Tesseract binary not usable: %s", exc)
        return False
    return True


@dataclass(frozen=True)
class WeaponLine:
    """One parsed row of the weapon-selection HUD block.

    ``count`` and ``name`` are the raw OCR strings for those columns --
    verbatim, not corrected -- and are ``""`` when that column was absent
    (e.g. a gun group with no separate weapon name).
    """

    selected: bool
    group: str
    count: str
    name: str


# Tokens recognised as the leading selection marker, including the OCR
# misreads of ``>`` and ``-`` this module was written to tolerate.
_SELECTED_MARKERS = {">", "s", "S", "»"}  # '>' itself, plus 's'/'S'/'»'
_UNSELECTED_MARKERS = {"-", "_", "‐", "—"}  # '-', '_', '‐', '—'


def parse_weapon_line(text: str) -> WeaponLine | None:
    """Parse one line of OCR text from the weapon HUD block.

    Pure string parsing -- no image or OCR calls here. Returns ``None`` for
    blank lines, telemetry rows (THR/IAS/SPD/ALT/RALT/FUEL, which carry no
    leading marker), and anything else that does not look like a weapon
    group line.

    Columns are expected to be separated by runs of two or more spaces,
    which is how the HUD's fixed-width layout renders and how ``pytesseract``
    reproduces it in practice; group and weapon names may themselves contain
    single interior spaces (``"AG AUTO"``, ``"20 mm Mk 11 mod 5"``).
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None

    tokens = [t for t in re.split(r"\s{2,}", stripped) if t.strip()]
    if not tokens:
        return None

    marker = tokens[0]
    if marker in _SELECTED_MARKERS:
        selected = True
    elif marker in _UNSELECTED_MARKERS:
        selected = False
    else:
        return None

    rest = tokens[1:]
    if not rest:
        return None

    group = rest[0]
    count = rest[1] if len(rest) > 1 else ""
    name = " ".join(rest[2:]) if len(rest) > 2 else ""
    return WeaponLine(selected=selected, group=group, count=count, name=name)


def selected_weapons(lines: list[str]) -> list[WeaponLine]:
    """Parse ``lines`` and return only the selected weapon groups, in order."""
    result = []
    for line in lines:
        parsed = parse_weapon_line(line)
        if parsed is not None and parsed.selected:
            result.append(parsed)
    return result


def isolate_hud(
    image: Image.Image, *, min_green: int = 90, dominance: int = 40
) -> Image.Image:
    """Isolate HUD-green pixels into a binary (mode "L") image.

    A pixel is considered HUD green when ``g >= min_green`` and
    ``g - r >= dominance`` and ``g - b >= dominance``. Matching pixels are
    white (255), everything else is black (0).

    The defaults were tuned against synthetic fixtures rendering the HUD's
    saturated green (RGB roughly ``(40, 220, 60)``) over photographic-style
    noisy backgrounds: ``min_green=90`` rejects dim backgrounds while easily
    passing the HUD's green channel (typically 200+), and ``dominance=40``
    rejects desaturated/grey or blue-ish background pixels that happen to
    have a moderately high green channel, while still tolerating anti-
    aliased glyph edges that are partially blended with the background.
    """
    rgb = image.convert("RGB")
    r_data, g_data, b_data = rgb.split()
    r_px = r_data.tobytes()
    g_px = g_data.tobytes()
    b_px = b_data.tobytes()

    out_px = bytes(
        255 if (g >= min_green and g - r >= dominance and g - b >= dominance) else 0
        for r, g, b in zip(r_px, g_px, b_px)
    )
    out = Image.frombytes("L", rgb.size, out_px)
    return out


def find_text_rows(
    binary: Image.Image, *, gap_tolerance: int = 2, min_pixels: int = 1
) -> list[tuple[int, int]]:
    """Return (top, bottom) pixel spans of rows containing text.

    Rows are found by horizontal projection: a row "has text" when it
    contains at least ``min_pixels`` non-zero pixels. Runs of text rows
    separated by no more than ``gap_tolerance`` consecutive blank rows are
    merged into a single span, so that the ascenders/descenders and small
    inter-line gaps of real glyph rendering do not fragment one line of text
    into several spans.
    """
    gray = binary.convert("L")
    width, height = gray.size
    data = gray.tobytes()

    row_has_text = []
    for y in range(height):
        row = data[y * width : (y + 1) * width]
        count = sum(1 for v in row if v)
        row_has_text.append(count >= min_pixels)

    rows: list[tuple[int, int]] = []
    start = None
    end = None
    gap = 0
    for y, has in enumerate(row_has_text):
        if has:
            if start is None:
                start = y
            end = y
            gap = 0
        elif start is not None:
            gap += 1
            if gap > gap_tolerance:
                rows.append((start, end))
                start = None
                end = None
                gap = 0
    if start is not None:
        rows.append((start, end))
    return rows


def capture_region(
    box: tuple[int, int, int, int] | None = None,
) -> Image.Image | None:
    """Grab part of the primary screen as an RGB image, or ``None``.

    ``box`` is ``(left, top, right, bottom)`` in screen pixels, or ``None``
    for the whole primary monitor. Prefers ``mss`` (faster, no extra
    permissions dialog on some platforms); falls back to
    ``PIL.ImageGrab``; returns ``None`` if neither is usable. Never raises.
    """
    try:
        import mss  # type: ignore
    except ImportError:
        mss = None  # type: ignore[assignment]

    if mss is not None:
        try:
            with mss.mss() as sct:
                if box is not None:
                    left, top, right, bottom = box
                    monitor = {
                        "left": left,
                        "top": top,
                        "width": right - left,
                        "height": bottom - top,
                    }
                else:
                    monitor = sct.monitors[1]
                shot = sct.grab(monitor)
                return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        except Exception as exc:  # noqa: BLE001 - must never raise
            log.debug("mss screen capture failed: %s", exc)

    try:
        from PIL import ImageGrab

        return ImageGrab.grab(bbox=box)
    except Exception as exc:  # noqa: BLE001 - must never raise
        log.debug("ImageGrab screen capture failed: %s", exc)
        return None


def extract(image: Image.Image) -> list[WeaponLine]:
    """Run the full pipeline over a supplied image and return weapon lines.

    Works on any image the caller provides -- a live screenshot, or a
    fixture in a test -- so this function itself never touches the screen.
    Returns an empty list (logging at debug level) if OCR is unavailable or
    anything in the pipeline fails; never raises.
    """
    if not available():
        log.debug("weapon OCR unavailable; extract() returning no results")
        return []

    try:
        binary = isolate_hud(image)
        rows = find_text_rows(binary)
        results: list[WeaponLine] = []
        for top, bottom in rows:
            crop = binary.crop((0, top, binary.width, bottom + 1))
            text = pytesseract.image_to_string(crop, config="--psm 7")
            parsed = parse_weapon_line(text)
            if parsed is not None:
                results.append(parsed)
        return results
    except Exception as exc:  # noqa: BLE001 - must never raise
        log.debug("weapon extraction failed: %s", exc)
        return []


def read_selected_weapon(
    box: tuple[int, int, int, int] | None = None,
) -> str | None:
    """One-call convenience: capture the screen and return the selected
    weapon's name (e.g. ``"AIM-7F"``), or ``None`` if it cannot be read.

    Never raises. When several weapon groups are selected at once (which
    the HUD does allow, e.g. a gun and a missile group together), returns
    the name of the first selected group found, top to bottom.
    """
    if not available():
        log.debug("weapon OCR unavailable; read_selected_weapon() returning None")
        return None

    try:
        image = capture_region(box)
        if image is None:
            log.debug("screen capture unavailable; read_selected_weapon() returning None")
            return None

        for weapon in extract(image):
            if weapon.selected and weapon.name:
                return weapon.name
        return None
    except Exception as exc:  # noqa: BLE001 - must never raise
        log.debug("read_selected_weapon failed: %s", exc)
        return None
