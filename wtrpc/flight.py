"""Rolling-window flight regime analysis, built entirely from ``Flight``
telemetry samples. No network, no filesystem, no clock reads — the caller
supplies the timestamp for each sample so this stays fully testable offline.
"""

from __future__ import annotations

from wtrpc.models import AirState, Flight

_DOGFIGHT_G_THRESHOLD = 3.0
_DOGFIGHT_G_SAMPLE_FRACTION = 0.35
_DOGFIGHT_HEADING_RATE_DEG_PER_S = 8.0
_DOGFIGHT_ROLL_THRESHOLD_DEG = 45.0
_DOGFIGHT_ROLL_SAMPLE_FRACTION = 0.30
# Inside this range an enemy fighter is close enough that hard manoeuvring is
# a fight rather than aerobatics. Guns reach ~1 km and a merge happens well
# inside 3 km, so 2.5 km is generous without being meaningless. Measured live:
# a 9.4G break with the nearest bandit 5.8 km away and opening is NOT a fight.
_CONTACT_RANGE_KM = 2.5
_SUPERSONIC_MACH = 1.0
_CLIMBING_VS_MS = 15.0
_DIVING_VS_MS = -25.0

_ALL_FIELDS = (
    "mach",
    "ias_kph",
    "tas_kph",
    "altitude_m",
    "load_factor",
    "vertical_speed_ms",
    "heading_deg",
    "roll_deg",
    "throttle_pct",
)

_LABELS: dict[AirState, str] = {
    AirState.DOGFIGHTING: "Dogfighting",
    AirState.MANEUVERING: "Maneuvering",
    AirState.SUPERSONIC: "Supersonic",
    AirState.CLIMBING: "Climbing",
    AirState.DIVING: "Diving",
    AirState.CRUISING: "Cruising",
    AirState.UNKNOWN: "",
}


def _angular_delta(a: float, b: float) -> float:
    """Shortest signed delta from angle ``a`` to angle ``b``, handling the
    359 -> 1 degree wraparound correctly (result stays within [-180, 180])."""
    return ((b - a + 180.0) % 360.0) - 180.0


def _all_fields_none(sample: Flight) -> bool:
    return all(getattr(sample, field) is None for field in _ALL_FIELDS)


class FlightAnalyzer:
    """Classifies the current flight regime from a rolling telemetry window."""

    def __init__(self, window_seconds: float = 20.0, min_samples: int = 5) -> None:
        self.window_seconds = window_seconds
        self.min_samples = min_samples
        self._samples: list[tuple[float, Flight]] = []

    def add(self, sample: Flight, timestamp: float) -> None:
        """Append a sample and evict anything older than ``window_seconds``."""
        self._samples.append((timestamp, sample))
        cutoff = timestamp - self.window_seconds
        self._samples = [(t, s) for t, s in self._samples if t >= cutoff]

    def reset(self) -> None:
        """Clear the window, e.g. when a new match starts."""
        self._samples = []

    def state(self) -> AirState:
        if len(self._samples) < self.min_samples:
            return AirState.UNKNOWN

        samples = [s for _, s in self._samples]
        if all(_all_fields_none(s) for s in samples):
            return AirState.UNKNOWN

        if self._is_maneuvering_hard(samples):
            # Same instruments, two very different situations. Only call it a
            # dogfight when a hostile is actually close enough to be fighting.
            return (
                AirState.DOGFIGHTING
                if self._hostile_in_contact(samples)
                else AirState.MANEUVERING
            )

        latest_mach = samples[-1].mach
        if latest_mach is not None and latest_mach >= _SUPERSONIC_MACH:
            return AirState.SUPERSONIC

        vs_values = [
            s.vertical_speed_ms for s in samples if s.vertical_speed_ms is not None
        ]
        if vs_values:
            mean_vs = sum(vs_values) / len(vs_values)
            if mean_vs >= _CLIMBING_VS_MS:
                return AirState.CLIMBING
            if mean_vs <= _DIVING_VS_MS:
                return AirState.DIVING

        return AirState.CRUISING

    def _hostile_in_contact(self, samples: list[Flight]) -> bool:
        """True when a hostile aircraft was within fighting range recently.

        Uses the closest approach seen anywhere in the window rather than the
        latest reading, because a merge is brief: two fighters pass inside a
        kilometre and are three kilometres apart two seconds later, and that
        whole exchange is still one dogfight.
        """
        distances = [
            s.nearest_hostile_km
            for s in samples
            if s.nearest_hostile_km is not None
        ]
        if not distances:
            # Nothing known. Do not promote to DOGFIGHTING on an assumption.
            return False
        return min(distances) <= _CONTACT_RANGE_KM

    def _is_maneuvering_hard(self, samples: list[Flight]) -> bool:
        total = len(samples)

        high_g_count = sum(
            1
            for s in samples
            if s.load_factor is not None and abs(s.load_factor) >= _DOGFIGHT_G_THRESHOLD
        )
        if total and (high_g_count / total) >= _DOGFIGHT_G_SAMPLE_FRACTION:
            return True

        heading_entries = [
            (t, s.heading_deg) for t, s in self._samples if s.heading_deg is not None
        ]
        rates = []
        for (t0, h0), (t1, h1) in zip(heading_entries, heading_entries[1:]):
            dt = t1 - t0
            if dt <= 0:
                continue
            rates.append(abs(_angular_delta(h0, h1)) / dt)
        mean_rate = sum(rates) / len(rates) if rates else 0.0

        roll_count = sum(
            1
            for s in samples
            if s.roll_deg is not None and abs(s.roll_deg) >= _DOGFIGHT_ROLL_THRESHOLD_DEG
        )
        roll_fraction = (roll_count / total) if total else 0.0

        if mean_rate >= _DOGFIGHT_HEADING_RATE_DEG_PER_S and (
            roll_fraction >= _DOGFIGHT_ROLL_SAMPLE_FRACTION
        ):
            return True

        return False


def label(state: AirState) -> str:
    """Map an ``AirState`` to display text, "" for unknown."""
    return _LABELS.get(state, "")
