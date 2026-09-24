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
import time
from dataclasses import dataclass

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

try:  # pragma: no cover - exercised indirectly, depends on the environment
    import pytesseract

    _HAVE_PYTESSERACT = True
except ImportError:  # pragma: no cover - depends on the environment
    pytesseract = None  # type: ignore[assignment]
    _HAVE_PYTESSERACT = False


#: Tesseract reads a whole block better than one line at a time here: the
#: HUD is already a tidy table, and --psm 6 ("a uniform block of text") both
#: reads it correctly and costs one call instead of one per row.
_OCR_CONFIG = "--psm 6"

#: Where Windows installers put the binary. The winget package does not add
#: itself to PATH for already-open shells, and a PyInstaller build has no
#: shell at all, so falling back to the standard locations is what makes this
#: work in practice rather than only after a reboot.
_WINDOWS_TESSERACT_PATHS = (
    "C:/Program Files/Tesseract-OCR/tesseract.exe",
    "C:/Program Files (x86)/Tesseract-OCR/tesseract.exe",
)


def _locate_tesseract() -> bool:
    """Point pytesseract at a Tesseract binary, returning whether one works."""
    if not _HAVE_PYTESSERACT:
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:  # noqa: BLE001 - probing, must not raise
        pass

    import os

    # A test double stands in for the module in places, so reach for the
    # nested attribute defensively rather than assuming the real package.
    inner = getattr(pytesseract, "pytesseract", None)
    if inner is None:
        return False

    original_cmd = getattr(inner, "tesseract_cmd", None)
    for candidate in _WINDOWS_TESSERACT_PATHS:
        if not os.path.exists(candidate):
            continue
        try:
            inner.tesseract_cmd = candidate
            pytesseract.get_tesseract_version()
        except Exception:  # noqa: BLE001
            continue
        log.debug("Found Tesseract at %s", candidate)
        return True

    # None of the guessed paths worked -- leave tesseract_cmd as it was
    # found rather than stuck pointing at the last (broken) candidate tried.
    if original_cmd is not None:
        inner.tesseract_cmd = original_cmd
    return False


#: Cached result of the last successful/unsuccessful probe, so `available()`
#: does not spawn `tesseract --version` on every call. ``None`` means "not
#: probed yet".
_tesseract_located: bool | None = None
_tesseract_probed_at = 0.0

#: How long a failed probe is trusted. The managed app runs for the whole game
#: session, so an install made after launch has to be noticed eventually; a
#: success never needs rechecking.
_TESSERACT_RETRY_S = 300.0


def reset_tesseract_cache() -> None:
    """Forget the memoised Tesseract probe result.

    For tests, and for the rare case a Tesseract install appears or
    disappears while the process is running.
    """
    global _tesseract_located
    _tesseract_located = None


def _prepare_for_ocr(binary: Image.Image, scale: int = 3) -> Image.Image:
    """Turn the isolation mask into something Tesseract reads well.

    Two steps matter. The mask is white text on black, and Tesseract expects
    dark text on light, so it is inverted. And HUD glyphs are small -- around
    ten pixels tall at 1080p -- which Tesseract reads poorly, so the image is
    upscaled. Without both, the same frame that reads perfectly comes back as
    noise.
    """
    grey = binary.convert("L")
    inverted = ImageOps.invert(grey)
    if scale > 1:
        inverted = inverted.resize(
            (inverted.width * scale, inverted.height * scale), Image.LANCZOS
        )
    return inverted


def available() -> bool:
    """Return whether weapon-name extraction can actually work right now.

    ``True`` only when ``pytesseract`` is importable AND it can find a
    working Tesseract binary. Never raises.

    The underlying probe spawns ``tesseract --version`` as a subprocess, so
    the result is memoised at module level after the first call instead of
    being re-run on every check; call ``reset_tesseract_cache()`` to force a
    fresh probe. A failure is retried after ``_TESSERACT_RETRY_S``.
    """
    global _tesseract_located, _tesseract_probed_at
    if _tesseract_located or (
        _tesseract_located is False
        and time.monotonic() - _tesseract_probed_at < _TESSERACT_RETRY_S
    ):
        return _tesseract_located

    _tesseract_probed_at = time.monotonic()
    if not _HAVE_PYTESSERACT:
        log.debug("pytesseract is not installed; weapon OCR unavailable")
        _tesseract_located = False
        return False
    if not _locate_tesseract():
        log.debug("no usable Tesseract binary found")
        _tesseract_located = False
        return False
    _tesseract_located = True
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


#: A quantity column: "40", "512", "4/4", "4[L]", "4/4[L]", "8/4(L)".
#:
#: Brackets and parentheses are both real -- an F-16C renders "4/4[L]" and an
#: F-4S "8/4(L)". Letters that OCR habitually substitutes for digits (O for 0,
#: l/I for 1) are accepted, which is the entire point of reading pixels, but
#: at least one genuine digit must be present so a word like "BOMB" can never
#: be mistaken for a count.
_COUNT_RE = re.compile(
    r"^(?=[^\d]*\d)[\dOolI]+(?:/[\dOolI]+)?(?:[\[(][A-Za-z][\])])?$", re.ASCII
)

#: Rows that are instrument readings, not weapons. Trailing digits are
#: stripped before comparison so OIL1/ENGN2 match OIL/ENGN.
_TELEMETRY_LABELS = frozenset(
    {"THR", "IAS", "SPD", "TAS", "ALT", "RALT", "FUEL", "OIL", "ENGN",
     "RPM", "TEMP", "WEP", "M", "G"}
)

#: Groups deliberately left out of the presence. The cannon is selected
#: essentially all the time, so reporting it says nothing about what the
#: pilot has actually chosen; countermeasures are not weapons at all.
_UNINTERESTING_GROUPS = frozenset(
    {"CNN", "CNN AUTO", "CANNON", "GUN", "GUNS", "MG", "MGUN",
     "FLR", "FLARE", "FLARES", "CHFF", "CHAFF"}
)


#: The HUD also draws rows in red to mean "not ready" -- a reloading weapon
#: ("AAM 0:29[27]"), or WEP engaged. Measured rgb(199, 36, 50) to
#: rgb(210, 47, 59).
#:
#: Off by default, deliberately. Red is far less distinctive against a game
#: image than pure green is -- dark reds occur naturally in terrain, fire and
#: tracers, and switching it on produced false positives on the background
#: fixture where green-only produces exactly none. Nothing is lost for the
#: purpose this module exists for, because a SELECTED weapon is always green:
#: a group that starts reloading loses its ">" marker and turns red at the
#: same moment. Turn it on only to read the not-ready rows themselves.
_WARNING_MAX_RATIO = 0.45


def _is_telemetry_label(group: str) -> bool:
    head = group.strip().upper().rstrip("0123456789")
    return head in _TELEMETRY_LABELS


#: A real group is an uppercase abbreviation: AAM, AGM, CNN AUTO, AG AUTO.
#: OCR occasionally hallucinates a "row" out of stray lit pixels -- ('mN', '')
#: turned up once in five live frames -- and this rejects those without
#: needing a list of every group the game has.
_GROUP_RE = re.compile(r"^[A-Z][A-Z0-9]*(?: [A-Z][A-Z0-9]*)*$", re.ASCII)


def is_interesting(line: "WeaponLine") -> bool:
    """False for groups not worth putting in a Discord status.

    Rejects the cannon and countermeasures, and anything that does not look
    like a group abbreviation at all.
    """
    group = line.group.strip().upper()
    if group in _UNINTERESTING_GROUPS:
        return False
    if not _GROUP_RE.match(line.group.strip()):
        return False
    return True


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

    tokens = [t for t in stripped.split() if t]
    if not tokens:
        return None

    # The marker is optional. A real capture shows three states, not two:
    # ">" selected, "-" unselected, and no marker at all for a weapon that is
    # carried but not currently cycled to (e.g. "   AGM  4[L]  AGM-88C").
    # Dropping unmarked lines threw those away entirely.
    selected = False
    had_marker = False
    if tokens[0] in _SELECTED_MARKERS:
        selected = True
        had_marker = True
        tokens = tokens[1:]
    elif tokens[0] in _UNSELECTED_MARKERS:
        had_marker = True
        tokens = tokens[1:]

    if not tokens:
        return None

    # Anchor on the count rather than on column spacing. Group names can be
    # two words ("CNN AUTO", "AG AUTO") separated by a single space, so
    # splitting on runs of spaces put the group and the count in one field
    # and shifted every column after it.
    count_index = next(
        (i for i, tok in enumerate(tokens) if _COUNT_RE.match(tok)), None
    )

    if count_index == 0:
        # A bare quantity with no group in front of it is not a weapon row.
        return None

    if count_index is None:
        # Some groups carry no quantity at all ("> GUN"). Accept those only
        # when a marker vouched for the row, otherwise any stray line of text
        # would parse as a weapon.
        if not had_marker:
            return None
        group = " ".join(tokens)
        if _is_telemetry_label(group):
            return None
        if not _GROUP_RE.match(group):
            # A marker alone does not vouch for text that is not a group
            # abbreviation -- OCR occasionally hallucinates a "row" out of
            # stray lit pixels ('mN' has turned up in a live frame).
            return None
        return WeaponLine(selected=selected, group=group, count="", name="")

    group = " ".join(tokens[:count_index])
    if _is_telemetry_label(group):
        return None

    count = tokens[count_index]
    name = " ".join(tokens[count_index + 1 :])
    return WeaponLine(selected=selected, group=group, count=count, name=name)


#: Shell types War Thunder names in the gunner sight. The sight prints the
#: loaded shell as bare green text with no marker and no count -- "APFSDS",
#: "HEAT MP" -- which is a different shape from the aircraft weapon block and
#: needs its own reader. Longer names come first so "HEAT MP" is not
#: shortened to "HEAT".
_SHELL_NAMES = (
    "APFSDS", "APDS", "APCBC", "APHE", "APCR", "APHEBC", "APBC", "AP",
    "HEAT MP", "HEATFS", "HEAT", "HESH", "HE VT", "HE",
    "ATGM", "SMOKE", "SHRAPNEL", "PROX", "AAM",
)


def read_shell_name(lines: list[str]) -> str:
    """Find the loaded shell type in OCR output from the gunner sight.

    The sight draws the shell name on its own, in green, with no selection
    marker and no quantity -- so `parse_weapon_line` cannot see it, since
    that function anchors on a count. Matching against known shell names is
    what distinguishes "APFSDS" from the range numbers and other stray text
    sharing the sight.
    """
    for raw in lines:
        text = raw.strip().upper()
        if not text:
            continue
        # OCR picks the sight's green dot up as a stray glyph on the front.
        # Punctuation is replaced with a space rather than deleted, so a
        # hyphenated read like "HE-VT" still splits into two tokens instead
        # of gluing into "HEVT", which matches nothing.
        cleaned = "".join(
            ch if ch.isalnum() or ch.isspace() else " " for ch in text
        ).strip()
        # Match whole tokens, not substrings: "NOTASHELL" contains "HE", and
        # a loose search would happily report a shell type that is not there.
        tokens = cleaned.split()
        for shell in _SHELL_NAMES:
            parts = shell.split()
            for i in range(len(tokens) - len(parts) + 1):
                if tokens[i : i + len(parts)] == parts:
                    return shell
    return ""


def selected_weapons(
    lines: list[str], *, include_uninteresting: bool = False
) -> list[WeaponLine]:
    """Parse ``lines`` and return the selected weapon groups, in order.

    The cannon and countermeasures are filtered out by default: the gun is
    selected almost permanently, so naming it conveys nothing about what the
    pilot chose. Pass ``include_uninteresting=True`` to get everything.
    """
    result = []
    for line in lines:
        parsed = parse_weapon_line(line)
        if parsed is None or not parsed.selected:
            continue
        if not include_uninteresting and not is_interesting(parsed):
            continue
        result.append(parsed)
    return result


def isolate_hud(
    image: Image.Image,
    *,
    min_green: int = 80,
    dominance: int = 40,
    max_ratio: float = 0.45,
    include_warning: bool = False,
) -> Image.Image:
    """Isolate HUD-green pixels into a binary (mode "L") image.

    A pixel is HUD green when ``g >= min_green``, both other channels are at
    most ``max_ratio`` of the green channel, and both clear ``dominance`` in
    absolute terms. Matching pixels are white (255), everything else black.

    Calibrated against a real 1920x1080 capture of the game rather than a
    guess. The HUD renders in essentially pure green -- measured
    ``rgb(31, 255, 0)`` in the weapon block and ``rgb(0, 255, 0)`` in the
    radar scope, with anti-aliased edges running down through
    ``rgb(11, 113, 13)`` and ``rgb(5, 195, 6)``. Across that frame the mean
    green-minus-red was 170 and green-minus-blue 169.

    The ratio test is what makes this robust, and it is why it was added: an
    absolute ``g - r`` margin alone also passes foliage and terrain, which are
    green but carry a substantial red channel. Requiring red and blue to stay
    BELOW a fraction of green demands the near-pure green only the HUD
    produces. Verified on the real capture: it lifted the weapon block, the
    radar scope, the compass ribbon and enemy nameplates cleanly out of the
    game image with the glyphs fully legible.
    """
    rgb = image.convert("RGB")
    r_data, g_data, b_data = rgb.split()
    r_px = r_data.tobytes()
    g_px = g_data.tobytes()
    b_px = b_data.tobytes()

    def _is_hud(r: int, g: int, b: int) -> bool:
        if (
            g >= min_green
            and g - r >= dominance
            and g - b >= dominance
            and r <= g * max_ratio
            and b <= g * max_ratio
        ):
            return True
        if include_warning and (
            r >= min_green
            and r - g >= dominance
            and r - b >= dominance
            and g <= r * _WARNING_MAX_RATIO
            and b <= r * _WARNING_MAX_RATIO
        ):
            return True
        return False

    out_px = bytes(
        255 if _is_hud(r, g, b) else 0 for r, g, b in zip(r_px, g_px, b_px)
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
        if not find_text_rows(binary):
            return []

        text = pytesseract.image_to_string(
            _prepare_for_ocr(binary), config=_OCR_CONFIG
        )
        results: list[WeaponLine] = []
        for line in text.splitlines():
            parsed = parse_weapon_line(line)
            if parsed is not None:
                results.append(parsed)
        return results
    except Exception as exc:  # noqa: BLE001 - must never raise
        log.debug("weapon extraction failed: %s", exc)
        return []


def read_loaded_shell(
    box: tuple[int, int, int, int] | None = None,
) -> str | None:
    """Read the loaded shell type from the gunner sight, e.g. ``"APFSDS"``.

    The sight prints the shell name in the same green as the aircraft HUD, so
    the isolation stage is shared; only the parsing differs, since the name
    stands alone with no marker and no count. Returns ``None`` when it cannot
    be read -- which includes every moment the player is not in the gunner
    view, because the name is only drawn there.
    """
    if not available():
        return None
    try:
        image = capture_region(box)
        if image is None:
            return None
        binary = isolate_hud(image)
        if not find_text_rows(binary):
            return None
        text = pytesseract.image_to_string(
            _prepare_for_ocr(binary), config=_OCR_CONFIG
        )
        return read_shell_name(
            [line for line in text.splitlines() if line.strip()]
        ) or None
    except Exception as exc:  # noqa: BLE001 - must never raise
        log.debug("shell read failed: %s", exc)
        return None


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
            if weapon.selected and weapon.name and is_interesting(weapon):
                return weapon.name
        return None
    except Exception as exc:  # noqa: BLE001 - must never raise
        log.debug("read_selected_weapon failed: %s", exc)
        return None
