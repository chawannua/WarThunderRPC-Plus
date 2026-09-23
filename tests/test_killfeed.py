"""Tests for wtrpc.killfeed -- kill counting from War Thunder's HUD event feed.

These never require a running instance of War Thunder: ``KillFeed`` takes an
injected ``fetch`` callable instead of talking HTTP directly, so every test
here supplies canned payloads (mirroring the wtclient tests' mock style).
"""

from __future__ import annotations

import json
import pathlib

from wtrpc.killfeed import KillEvent, KillFeed, parse_event, strip_tags

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES / name, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# strip_tags
# ---------------------------------------------------------------------------


def test_strip_tags_removes_squadron_tag():
    assert strip_tags("^LVRTS^ Chawannua (F-4S Phantom II) destroyed [ai] MiG-15bis") == (
        "Chawannua (F-4S Phantom II) destroyed [ai] MiG-15bis"
    )


def test_strip_tags_no_tag_is_unchanged():
    assert strip_tags("Chawannua (F-4S Phantom II) destroyed [ai] MiG-15bis") == (
        "Chawannua (F-4S Phantom II) destroyed [ai] MiG-15bis"
    )


def test_strip_tags_collapses_whitespace():
    assert strip_tags("^TAG^   Bob   (P-51D)  destroyed   Enemy") == "Bob (P-51D) destroyed Enemy"


# ---------------------------------------------------------------------------
# parse_event -- happy paths
# ---------------------------------------------------------------------------


def test_parse_event_live_captured_message():
    msg = "^LVRTS^ Chawannua (F-4S Phantom II) destroyed [ai] MiG-15bis"
    event = parse_event(msg)
    assert event == KillEvent(
        killer="Chawannua",
        killer_vehicle="F-4S Phantom II",
        victim="MiG-15bis",
        victim_is_ai=True,
        is_kill=True,
    )


def test_parse_event_killer_with_no_squadron_tag():
    msg = "Wingman42 (Bf 109 G-6) destroyed [ai] Yak-9"
    event = parse_event(msg)
    assert event.killer == "Wingman42"
    assert event.killer_vehicle == "Bf 109 G-6"
    assert event.is_kill is True


def test_parse_event_human_victim_has_no_ai_marker():
    msg = "Chawannua (F-4S Phantom II) shot down PilotName"
    event = parse_event(msg)
    assert event.victim == "PilotName"
    assert event.victim_is_ai is False
    assert event.is_kill is True


def test_parse_event_shot_down_is_a_kill():
    event = parse_event("Chawannua (F-4S Phantom II) shot down [ai] MiG-15bis")
    assert event.is_kill is True


def test_parse_event_damage_only_lines_are_not_kills():
    for verb in ("damaged", "critically damaged", "set afire"):
        msg = f"Chawannua (F-4S Phantom II) {verb} [ai] MiG-15bis"
        event = parse_event(msg)
        assert event is not None, msg
        assert event.is_kill is False, msg


def test_parse_event_tricky_player_name_containing_destroyed():
    msg = "xX_destroyed_Xx (F-4S Phantom II) destroyed [ai] MiG-15bis"
    event = parse_event(msg)
    assert event.killer == "xX_destroyed_Xx"
    assert event.killer_vehicle == "F-4S Phantom II"
    assert event.victim == "MiG-15bis"
    assert event.is_kill is True


def test_parse_event_not_a_kill_or_damage_line_returns_none():
    assert parse_event("^LVRTS^ Chawannua has earned 'Ace Pilot'") is None
    assert parse_event("Player123 has joined the game") is None
    assert parse_event("Mission objective A captured") is None


# ---------------------------------------------------------------------------
# KillFeed -- polling and id cursors
# ---------------------------------------------------------------------------


def test_killfeed_poll_counts_kills_after_player_identified():
    payload = _load_fixture("killfeed_poll1.json")
    feed = KillFeed(fetch=lambda last_evt, last_dmg: payload)
    feed.identify_player("f-4s")
    feed.poll()

    assert feed.player_name == "Chawannua"
    # one "set afire" (not a kill) + one "destroyed" (a kill) for the same
    # victim must count as exactly one kill; Wingman42's kill is not ours.
    assert feed.kills == 1


def test_killfeed_poll_does_not_double_count_on_overlapping_second_poll():
    calls = []

    def fetch(last_evt, last_dmg):
        calls.append((last_evt, last_dmg))
        if len(calls) == 1:
            return _load_fixture("killfeed_poll1.json")
        return _load_fixture("killfeed_poll2.json")

    feed = KillFeed(fetch=fetch)
    feed.identify_player("f-4s")

    feed.poll()
    assert feed.kills == 1

    # second payload re-sends ids 2 and 3 (already seen) plus new id 4.
    feed.poll()
    assert feed.kills == 2

    # the fetch callable must have been called with cursors that advance.
    assert calls[0] == (0, 0)
    assert calls[1][1] == 3  # lastDmg from poll1's highest id


def test_killfeed_fetch_is_called_with_last_ids():
    seen = []
    payload = {"events": [], "damage": []}

    def fetch(last_evt, last_dmg):
        seen.append((last_evt, last_dmg))
        return payload

    feed = KillFeed(fetch=fetch)
    feed.poll()
    feed.poll()
    assert seen == [(0, 0), (0, 0)]


def test_killfeed_another_players_kill_does_not_count():
    payload = {
        "events": [],
        "damage": [
            {"id": 1, "msg": "Wingman42 (Bf 109 G-6) destroyed [ai] Yak-9"},
        ],
    }
    feed = KillFeed(fetch=lambda e, d: payload)
    feed.identify_player("f-4s")
    feed.poll()
    assert feed.kills == 0


# ---------------------------------------------------------------------------
# KillFeed -- player identification heuristic
# ---------------------------------------------------------------------------


def test_identify_player_normalises_internal_name_vs_display_name():
    payload = {
        "events": [],
        "damage": [
            {"id": 1, "msg": "Chawannua (F-4S Phantom II) destroyed [ai] MiG-15bis"},
        ],
    }
    feed = KillFeed(fetch=lambda e, d: payload)
    feed.identify_player("f-4s")
    feed.poll()
    assert feed.player_name == "Chawannua"
    assert feed.kills == 1


def test_identify_player_before_any_events_reports_unknown():
    feed = KillFeed(fetch=lambda e, d: {"events": [], "damage": []})
    feed.identify_player("f-4s")
    assert feed.player_name == ""
    assert feed.kills == 0


def test_identify_player_ambiguous_when_two_pilots_share_vehicle():
    payload = {
        "events": [],
        "damage": [
            {"id": 1, "msg": "Chawannua (F-4S Phantom II) destroyed [ai] MiG-15bis"},
            {"id": 2, "msg": "OtherPilot (F-4S Phantom II) destroyed [ai] MiG-17"},
        ],
    }
    feed = KillFeed(fetch=lambda e, d: payload)
    feed.identify_player("f-4s")
    feed.poll()
    assert feed.player_name == ""
    assert feed.kills == 0


def test_identify_player_empty_vehicle_id_reports_unknown():
    feed = KillFeed(fetch=lambda e, d: {"events": [], "damage": []})
    feed.identify_player("")
    assert feed.player_name == ""
    assert feed.kills == 0


# ---------------------------------------------------------------------------
# KillFeed -- reset
# ---------------------------------------------------------------------------


def test_reset_clears_kills_and_id_cursors():
    calls = []

    def fetch(last_evt, last_dmg):
        calls.append((last_evt, last_dmg))
        return _load_fixture("killfeed_poll1.json")

    feed = KillFeed(fetch=fetch)
    feed.identify_player("f-4s")
    feed.poll()
    assert feed.kills == 1

    feed.reset()
    assert feed.kills == 0

    feed.poll()
    # cursors were reset to 0, so the same ids from poll1 are re-processed
    # (a new match legitimately restarts id numbering from 1).
    assert calls[-1] == (0, 0)
    assert feed.kills == 1


# ---------------------------------------------------------------------------
# KillFeed -- malformed payloads must never raise
# ---------------------------------------------------------------------------


def test_poll_survives_fetch_returning_none():
    feed = KillFeed(fetch=lambda e, d: None)
    feed.poll()
    assert feed.kills == 0


def test_poll_survives_fetch_returning_empty_dict():
    feed = KillFeed(fetch=lambda e, d: {})
    feed.poll()
    assert feed.kills == 0


def test_poll_survives_junk_payload():
    feed = KillFeed(fetch=lambda e, d: {"damage": "not-a-list", "events": 42})
    feed.poll()
    assert feed.kills == 0


def test_poll_survives_junk_damage_entries():
    payload = {"events": [], "damage": [None, 1, "oops", {"id": "not-an-int", "msg": 5}, {}]}
    feed = KillFeed(fetch=lambda e, d: payload)
    feed.poll()
    assert feed.kills == 0


def test_poll_survives_fetch_raising():
    def fetch(e, d):
        raise RuntimeError("boom")

    feed = KillFeed(fetch=fetch)
    feed.poll()
    assert feed.kills == 0


def test_poll_survives_missing_msg_key():
    payload = {"events": [], "damage": [{"id": 1}]}
    feed = KillFeed(fetch=lambda e, d: payload)
    feed.poll()
    assert feed.kills == 0


# ---------------------------------------------------------------------------
# parse_event -- nested parentheses in the vehicle name
# ---------------------------------------------------------------------------


def test_parse_event_vehicle_name_with_nested_parens():
    msg = "Pilot (T-34 (1941)) destroyed Enemy"
    event = parse_event(msg)
    assert event is not None
    assert event.killer == "Pilot"
    assert event.killer_vehicle == "T-34 (1941)"
    assert event.victim == "Enemy"


# ---------------------------------------------------------------------------
# KillFeed -- fallback identification must not match on loose substrings
# ---------------------------------------------------------------------------


def test_fallback_identification_does_not_match_unrelated_short_vehicle_name():
    # The enemy's bare "T-34" happens to appear as a substring in the middle
    # of the player's long internal vehicle id ("ussr_t_34_85_zis_53"
    # normalises to "ussrt3485zis53", which contains "t34"). A loose
    # substring-in-either-direction match wrongly attributes the enemy's
    # kill to the player.
    payload = {
        "events": [],
        "damage": [
            {"id": 1, "msg": "EnemyPilot (T-34) destroyed [ai] SomeTarget"},
        ],
    }
    feed = KillFeed(fetch=lambda e, d: payload)
    feed.identify_player("ussr_t_34_85_zis_53")
    feed.poll()
    assert feed.player_name == ""
    assert feed.kills == 0


def test_fallback_identification_still_matches_prefix_of_display_name():
    # The existing, legitimate case: the internal id is a genuine prefix of
    # the HUD display name for the same vehicle.
    payload = {
        "events": [],
        "damage": [
            {"id": 1, "msg": "Chawannua (F-4S Phantom II) destroyed [ai] MiG-15bis"},
        ],
    }
    feed = KillFeed(fetch=lambda e, d: payload)
    feed.identify_player("f-4s")
    feed.poll()
    assert feed.player_name == "Chawannua"
    assert feed.kills == 1


# ---------------------------------------------------------------------------
# KillFeed -- non-int / bool damage ids must not bypass dedup
# ---------------------------------------------------------------------------


def test_poll_skips_entries_with_non_int_id_every_time():
    calls = {"n": 0}

    def fetch(last_evt, last_dmg):
        calls["n"] += 1
        return {
            "events": [],
            "damage": [
                {"id": "not-an-int", "msg": "Bob (P-51D) destroyed [ai] Enemy"},
            ],
        }

    feed = KillFeed(fetch=fetch, player_name="Bob")
    feed.poll()
    feed.poll()
    feed.poll()
    assert feed.kills == 0


def test_poll_skips_entries_with_bool_id():
    payload = {
        "events": [],
        "damage": [
            {"id": True, "msg": "Bob (P-51D) destroyed [ai] Enemy"},
        ],
    }
    feed = KillFeed(fetch=lambda e, d: payload, player_name="Bob")
    feed.poll()
    feed.poll()
    assert feed.kills == 0


# ---------------------------------------------------------------------------
# KillFeed -- configured player_name matching ignores case and tags
# ---------------------------------------------------------------------------


def test_configured_player_name_matches_case_insensitively():
    payload = {
        "events": [],
        "damage": [
            {"id": 1, "msg": "^TAG^ Chawannua (F-4S Phantom II) destroyed [ai] MiG-15bis"},
        ],
    }
    feed = KillFeed(fetch=lambda e, d: payload, player_name="chawannua")
    feed.poll()
    assert feed.kills == 1
