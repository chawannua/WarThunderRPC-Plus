"""Read the ammunition a vehicle is carrying, from War Thunder's save file.

The localhost API does not say what shells a tank has, let alone which one is
loaded. But the game stores the loadout the player chose in the hangar as
plain text, per vehicle, in its profile save::

    us_m1a2_sep2_abrams{
      USEROPT_WEAPONS:t="us_m1a2_sep2_abrams_default"
      USEROPT_BULLETS0:t="120mm_M829A_APDS_FS"
      USEROPT_BULLET_COUNT0:i=23
      USEROPT_BULLETS3:t="120mm_DM_HEAT_FS"
      USEROPT_BULLET_COUNT3:i=5
    }

That is the belt the player packed, not the round currently up the spout.
Switching shells mid-battle writes nothing: a byte-for-byte diff across a
session of deliberate shell switching came back with zero changed lines, and
the file had not been touched for over two minutes. So this answers "what is
this tank carrying", and only the gunner sight answers "what is loaded right
now" -- see ``wtrpc.weapon_ocr.read_loaded_shell``.

Reading is best-effort and never raises: a missing file, a different game
install, or an unparseable block all come back empty.
"""

from __future__ import annotations

import logging
import pathlib
import re
import sys

log = logging.getLogger(__name__)

#: Where the profile save lives under the user's Documents folder.
_SAVE_GLOBS = (
    "My Games/WarThunder/Saves/last/production/global.blk",
    "My Games/WarThunder/Saves/*/production/global.blk",
)


def _known_folder_documents() -> pathlib.Path | None:
    """Ask Windows for the real Documents folder via the shell API.

    OneDrive can silently redirect "Documents" outside ``~/Documents``;
    querying the shell directly (``FOLDERID_Documents``) is the only
    reliable way to find where War Thunder actually writes its saves.
    Returns ``None`` off Windows or when the lookup fails for any reason.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        folderid_documents = _GUID(
            0xFDD39AD0,
            0x238F,
            0x46AF,
            (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7),
        )

        buf = ctypes.c_wchar_p()
        result = ctypes.windll.shell32.SHGetKnownFolderPath(  # type: ignore[attr-defined]
            ctypes.byref(folderid_documents), 0, None, ctypes.byref(buf)
        )
        if result != 0 or not buf.value:
            return None
        path = pathlib.Path(buf.value)
        ctypes.windll.ole32.CoTaskMemFree(buf)  # type: ignore[attr-defined]
        return path
    except (OSError, ValueError, AttributeError):
        return None


def _documents_dirs() -> list[pathlib.Path]:
    """Return candidate Documents folders to search, most authoritative first.

    Injectable/monkeypatchable so callers (and tests) can control where
    ``save_file`` looks without touching the real filesystem.
    """
    home = pathlib.Path.home()
    candidates = [
        _known_folder_documents(),
        home / "Documents",
        home / "OneDrive" / "Documents",
    ]

    deduped: list[pathlib.Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _safe_mtime(path: pathlib.Path) -> float:
    """``path.stat().st_mtime``, or 0.0 when the stat call itself fails."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0

_BULLET_RE = re.compile(
    r'USEROPT_BULLETS(\d+)\s*:\s*t\s*=\s*"([^"]*)"', re.ASCII
)
_COUNT_RE = re.compile(r"USEROPT_BULLET_COUNT(\d+)\s*:\s*i\s*=\s*(-?\d+)", re.ASCII)

#: Internal belt names carry their shell type in the middle. Longest patterns
#: first so "APDS_FS" is not shortened to "APDS", and "HEAT_FS" not to "HEAT".
_SHELL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("apds_fs", "APFSDS"),
    ("apfsds", "APFSDS"),
    ("heat_fs", "HEATFS"),
    ("heatfs", "HEATFS"),
    ("hesh", "HESH"),
    ("heat", "HEAT"),
    ("apcbc", "APCBC"),
    ("apcr", "APCR"),
    ("aphe", "APHE"),
    ("apds", "APDS"),
    ("smoke", "SMOKE"),
    ("atgm", "ATGM"),
    ("shrapnel", "SHRAPNEL"),
    ("apc", "APC"),
    ("_he", "HE"),
    ("_ap", "AP"),
)


def save_file() -> pathlib.Path | None:
    """Locate the profile save, or ``None`` when it cannot be found.

    Never raises: an unreadable Documents folder or a save whose ``stat()``
    fails (permissions, a vanished file) is skipped rather than propagated.
    """
    try:
        for base in _documents_dirs():
            for pattern in _SAVE_GLOBS:
                try:
                    if "*" in pattern:
                        matches = sorted(base.glob(pattern), key=_safe_mtime, reverse=True)
                        if matches:
                            return matches[0]
                    else:
                        candidate = base / pattern
                        if candidate.exists():
                            return candidate
                except OSError as exc:
                    log.debug("Could not search %s: %s", base, exc)
    except OSError as exc:
        log.debug("Could not resolve Documents folders: %s", exc)
    return None


def classify_shell(internal_name: str) -> str:
    """Map an internal belt name to a readable shell type.

    ``120mm_M829A_APDS_FS`` becomes ``APFSDS``. Returns "" when nothing in
    the name is recognisable rather than guessing.
    """
    lowered = internal_name.lower()
    for needle, label in _SHELL_PATTERNS:
        if needle in lowered:
            return label
    return ""


def _vehicle_block(text: str, vehicle_id: str) -> str:
    """Extract the braced block for one vehicle, or "" when absent.

    The same vehicle id appears several times in the file under different
    parents -- sights, camouflage, loadout -- so the block containing the
    ammunition keys is the one wanted, not merely the first match.
    """
    for match in re.finditer(
        rf"^\s*{re.escape(vehicle_id)}\s*\{{", text, re.MULTILINE
    ):
        start = match.end()
        depth = 1
        i = start
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        block = text[start : i - 1]
        if "USEROPT_BULLETS" in block:
            return block
    return ""


def read_loadout(vehicle_id: str, path: pathlib.Path | None = None) -> list[tuple[str, int]]:
    """Return ``[(shell type, rounds), ...]`` for a vehicle, biggest belt first.

    Empty when the save cannot be read, the vehicle is not in it, or it
    carries no recognisable ammunition. Never raises.
    """
    if not vehicle_id:
        return []

    try:
        resolved = path or save_file()
    except OSError as exc:
        log.debug("Could not locate War Thunder profile save: %s", exc)
        return []
    if resolved is None:
        log.debug("War Thunder profile save not found")
        return []

    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.debug("Could not read %s: %s", resolved, exc)
        return []

    block = _vehicle_block(text, vehicle_id)
    if not block:
        return []

    names = {int(i): n for i, n in _BULLET_RE.findall(block)}
    counts = {int(i): int(c) for i, c in _COUNT_RE.findall(block)}

    shells: dict[str, int] = {}
    for slot, internal in names.items():
        if not internal:
            continue
        label = classify_shell(internal)
        if not label:
            continue
        rounds = counts.get(slot, 0)
        if rounds <= 0:
            continue
        shells[label] = shells.get(label, 0) + rounds

    return sorted(shells.items(), key=lambda kv: (-kv[1], kv[0]))


def loadout_label(shells: list[tuple[str, int]], limit: int = 2) -> str:
    """Render a loadout as ``"APFSDS + HEAT"``, most-carried first."""
    if not shells:
        return ""
    return " + ".join(name for name, _ in shells[:limit])
