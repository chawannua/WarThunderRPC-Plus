"""Tests for wtrpc.flight — rolling-window flight regime analysis."""

from wtrpc.models import AirState, Flight
from wtrpc.flight import FlightAnalyzer, label


def make_analyzer(window_seconds=20.0, min_samples=5):
    return FlightAnalyzer(window_seconds=window_seconds, min_samples=min_samples)


class TestInsufficientData:
    def test_no_samples_is_unknown(self):
        analyzer = make_analyzer()
        assert analyzer.state() == AirState.UNKNOWN

    def test_fewer_than_min_samples_is_unknown(self):
        analyzer = make_analyzer(min_samples=5)
        for t in range(3):
            analyzer.add(Flight(mach=0.5), timestamp=float(t))
        assert analyzer.state() == AirState.UNKNOWN

    def test_all_none_fields_is_unknown_and_does_not_raise(self):
        analyzer = make_analyzer(min_samples=3)
        for t in range(5):
            analyzer.add(Flight(), timestamp=float(t))
        # Must not raise, and with nothing to go on the state is UNKNOWN.
        assert analyzer.state() == AirState.UNKNOWN


class TestWindowEviction:
    def test_old_samples_are_evicted_outside_window(self):
        analyzer = make_analyzer(window_seconds=10.0, min_samples=1)
        analyzer.add(Flight(mach=0.5), timestamp=0.0)
        analyzer.add(Flight(mach=0.9), timestamp=100.0)
        # Only the most recent sample should remain in the window.
        assert len(analyzer._samples) == 1  # noqa: SLF001 (white-box check)


class TestSupersonic:
    def test_latest_mach_at_or_above_one_is_supersonic(self):
        analyzer = make_analyzer(min_samples=1)
        for t in range(5):
            analyzer.add(Flight(mach=0.5, vertical_speed_ms=0.0), timestamp=float(t))
        analyzer.add(Flight(mach=1.05, vertical_speed_ms=0.0), timestamp=5.0)
        assert analyzer.state() == AirState.SUPERSONIC

    def test_mach_exactly_one_is_supersonic(self):
        analyzer = make_analyzer(min_samples=1)
        for t in range(5):
            analyzer.add(Flight(mach=1.0, vertical_speed_ms=0.0), timestamp=float(t))
        assert analyzer.state() == AirState.SUPERSONIC


class TestClimbingAndDiving:
    def test_mean_vertical_speed_above_threshold_is_climbing(self):
        analyzer = make_analyzer(min_samples=1)
        for t in range(5):
            analyzer.add(Flight(vertical_speed_ms=20.0), timestamp=float(t))
        assert analyzer.state() == AirState.CLIMBING

    def test_mean_vertical_speed_below_threshold_is_diving(self):
        analyzer = make_analyzer(min_samples=1)
        for t in range(5):
            analyzer.add(Flight(vertical_speed_ms=-30.0), timestamp=float(t))
        assert analyzer.state() == AirState.DIVING

    def test_mild_vertical_speed_is_cruising(self):
        analyzer = make_analyzer(min_samples=1)
        for t in range(5):
            analyzer.add(Flight(vertical_speed_ms=2.0), timestamp=float(t))
        assert analyzer.state() == AirState.CRUISING

    def test_no_vertical_speed_data_defaults_to_cruising_when_other_data_present(self):
        analyzer = make_analyzer(min_samples=1)
        for t in range(5):
            analyzer.add(Flight(mach=0.3), timestamp=float(t))
        assert analyzer.state() == AirState.CRUISING


class TestDogfightingByLoadFactor:
    def test_sustained_high_g_is_dogfighting(self):
        analyzer = make_analyzer(min_samples=1)
        # 5 samples, at least 35% (>= 2) with |G| >= 3.0
        loads = [3.5, 3.2, 0.5, 1.0, 4.0]
        for t, g in enumerate(loads):
            analyzer.add(Flight(load_factor=g, nearest_hostile_km=1.2), timestamp=float(t))
        assert analyzer.state() == AirState.DOGFIGHTING

    def test_negative_high_g_counts_via_abs(self):
        analyzer = make_analyzer(min_samples=1)
        loads = [-3.5, -3.2, 0.5, 1.0, -4.0]
        for t, g in enumerate(loads):
            analyzer.add(Flight(load_factor=g, nearest_hostile_km=1.2), timestamp=float(t))
        assert analyzer.state() == AirState.DOGFIGHTING

    def test_below_35_percent_high_g_is_not_dogfighting_from_g_alone(self):
        analyzer = make_analyzer(min_samples=1)
        # Only 1/5 = 20% >= 3.0G, and no other dogfight signal.
        loads = [3.5, 0.5, 0.5, 1.0, 0.4]
        for t, g in enumerate(loads):
            analyzer.add(
                Flight(load_factor=g, vertical_speed_ms=0.0, heading_deg=90.0),
                timestamp=float(t),
            )
        assert analyzer.state() != AirState.DOGFIGHTING

    def test_g_fraction_is_of_samples_with_a_reading_not_the_whole_window(self):
        """A sparse ``load_factor`` reading must not be diluted by samples
        that simply never reported one.

        Only 2 of 5 samples in the window carry a load_factor at all; one of
        those two clears the G threshold, which is 50% of the *readings*
        -- comfortably over the 35% bar. Dividing by the whole window (5)
        instead of the 2 actual readings would give 20% and wrongly miss
        this as hard maneuvering.
        """
        analyzer = make_analyzer(min_samples=1)
        analyzer.add(Flight(load_factor=4.0), timestamp=0.0)
        analyzer.add(Flight(load_factor=0.5), timestamp=1.0)
        analyzer.add(Flight(), timestamp=2.0)
        analyzer.add(Flight(), timestamp=3.0)
        analyzer.add(Flight(), timestamp=4.0)
        assert analyzer.state() == AirState.MANEUVERING


class TestDogfightingByHeadingAndRoll:
    def test_fast_heading_change_with_steep_roll_is_dogfighting(self):
        analyzer = make_analyzer(min_samples=1)
        headings = [0, 10, 20, 30, 40, 50]
        rolls = [50, 50, 50, 10, 10, 10]  # 3/6 = 50% >= 45 deg
        for t, (h, r) in enumerate(zip(headings, rolls)):
            analyzer.add(
                Flight(heading_deg=float(h), roll_deg=float(r), load_factor=0.5,
                       nearest_hostile_km=1.2),
                timestamp=float(t),
            )
        # Mean |delta heading| = 10 deg/s over 1s steps >= 8 deg/s threshold
        assert analyzer.state() == AirState.DOGFIGHTING

    def test_heading_wraparound_359_to_1_is_handled_correctly(self):
        analyzer = make_analyzer(min_samples=1)
        # Heading crosses 359 -> 1, a true delta of 2 degrees, not 358.
        headings = [358.0, 359.0, 1.0, 2.0, 3.0, 4.0]
        rolls = [50.0, 46.0, 50.0, 48.0, 47.0, 45.0]  # all >= 45 -> 100% >= 30%
        for t, (h, r) in enumerate(zip(headings, rolls)):
            analyzer.add(
                Flight(heading_deg=h, roll_deg=r, load_factor=0.5), timestamp=float(t)
            )
        # If wraparound were handled naively, mean delta would be huge
        # (~60 deg/s) and falsely trigger dogfighting purely off a bogus
        # spike; here we assert it is NOT falsely inflated by measuring a
        # scenario where correct unwrap gives < 8 deg/s and roll alone
        # should not be enough (needs BOTH heading rate AND roll %).
        # Correct per-step deltas: 1,2,1,1,1 -> mean = 1.2 deg/s < 8, so this
        # should NOT be dogfighting via the heading+roll rule despite high
        # roll percentage.
        assert analyzer.state() != AirState.DOGFIGHTING

    def test_heading_wraparound_with_genuinely_fast_turn_is_dogfighting(self):
        analyzer = make_analyzer(min_samples=1)
        # Genuine fast turn crossing the 360/0 boundary: 350 -> 358 -> 6 -> 14 -> 22 -> 30
        # per-step deltas of 8 deg/s each, correctly unwrapped.
        headings = [350.0, 358.0, 6.0, 14.0, 22.0, 30.0]
        rolls = [50.0, 50.0, 50.0, 50.0, 50.0, 50.0]
        for t, (h, r) in enumerate(zip(headings, rolls)):
            analyzer.add(
                Flight(
                    heading_deg=h,
                    roll_deg=r,
                    load_factor=0.5,
                    nearest_hostile_km=1.2,
                ),
                timestamp=float(t),
            )
        assert analyzer.state() == AirState.DOGFIGHTING

    def test_roll_fraction_is_of_samples_with_a_reading_not_the_whole_window(self):
        """Same denominator bug, for roll_deg: only 2 of 10 samples in the
        window report a roll at all, and both clear the threshold -- 100%
        of the readings, not the 20% dividing by the whole window would
        give (which sits under the 30% bar)."""
        analyzer = make_analyzer(min_samples=1)
        headings = [float(10 * i) for i in range(10)]  # 10 deg/s each step
        for t, h in enumerate(headings):
            roll = 50.0 if t < 2 else None
            analyzer.add(
                Flight(heading_deg=h, roll_deg=roll, load_factor=0.5),
                timestamp=float(t),
            )
        assert analyzer.state() == AirState.MANEUVERING


class TestReset:
    def test_reset_clears_samples(self):
        analyzer = make_analyzer(min_samples=1)
        analyzer.add(Flight(mach=0.5), timestamp=0.0)
        analyzer.reset()
        assert analyzer.state() == AirState.UNKNOWN


class TestLabel:
    def test_label_mapping(self):
        assert label(AirState.DOGFIGHTING) == "Dogfighting"
        assert label(AirState.SUPERSONIC) == "Supersonic"
        assert label(AirState.CLIMBING) == "Climbing"
        assert label(AirState.DIVING) == "Diving"
        assert label(AirState.CRUISING) == "Cruising"
        assert label(AirState.UNKNOWN) == ""


class TestPriorityOrder:
    def test_dogfighting_beats_supersonic(self):
        analyzer = make_analyzer(min_samples=1)
        loads = [3.5, 3.2, 0.5, 1.0, 4.0]
        for t, g in enumerate(loads):
            analyzer.add(Flight(load_factor=g, mach=1.2, nearest_hostile_km=1.2), timestamp=float(t))
        assert analyzer.state() == AirState.DOGFIGHTING

    def test_supersonic_beats_climbing(self):
        analyzer = make_analyzer(min_samples=1)
        for t in range(5):
            analyzer.add(
                Flight(mach=1.2, vertical_speed_ms=20.0), timestamp=float(t)
            )
        assert analyzer.state() == AirState.SUPERSONIC

    def test_climbing_beats_diving_not_simultaneously_possible_but_beats_cruising(self):
        analyzer = make_analyzer(min_samples=1)
        for t in range(5):
            analyzer.add(Flight(vertical_speed_ms=16.0), timestamp=float(t))
        assert analyzer.state() == AirState.CLIMBING
