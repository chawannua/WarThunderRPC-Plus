"""Language-resilient match-mode classification.

``/mission.json`` reports objective text in the GAME CLIENT'S LANGUAGE, so
English string matching alone is unreliable — a known bug in the app this
one replaces. Strategy: try to match a known English objective prefix first
(covers the common case, the client's language is usually English or the
text still appears in English regardless of client language for many
missions). When nothing matches — because the client is in Thai, Russian,
German, ... — fall back to a safe generic derived from ``army`` alone rather
than silently producing a wrong label.
"""

from __future__ import annotations

from wtrpc.models import Army

# (english prefix, label, whether the label still needs an "Air "/"Ground "/
#  "Naval " prefix derived from `army`)
_PREFIX_TABLE: list[tuple[str, str, bool]] = [
    ("capture and maintain superiority over the airfields", "Air Domination", False),
    ("capture and hold airfields.", "Air Domination", False),
    ("capture and hold the airfield", "Air Domination", False),
    ("capture and maintain superiority over the air zone", "Air Domination", False),
    ("capture and maintain superiority over the points", "Domination", True),
    ("capture the enemy point", "Battle", True),
    ("prevent capture of allied point", "Battle", True),
    ("prevent the capture of the allied point", "Battle", True),
    ("capture and keep hold of the point", "Conquest", True),
    ("destroy the enemy ground vehicles", "Air Ground Strike", False),
    ("destroy the highlighted targets", "Air Frontline", False),
]

_ARMY_WORD: dict[Army, str] = {
    Army.AIR: "Air",
    Army.TANK: "Ground",
    Army.SHIP: "Naval",
    Army.UNKNOWN: "",
}

_GENERIC_FALLBACK: dict[Army, str] = {
    Army.AIR: "Air Battle",
    Army.TANK: "Ground Battle",
    Army.SHIP: "Naval Battle",
    Army.UNKNOWN: "",
}


def classify_mode(objective_text: str, army: Army) -> str:
    """Return a short match-mode label, e.g. "Air Domination"."""
    text = (objective_text or "").strip().lower()

    for prefix, label, needs_army_prefix in _PREFIX_TABLE:
        if text.startswith(prefix):
            if not needs_army_prefix:
                return label
            word = _ARMY_WORD.get(army, "")
            return f"{word} {label}".strip() if word else label

    return _GENERIC_FALLBACK.get(army, "")
