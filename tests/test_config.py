"""Tests for wtrpc.config: JSON-backed app configuration.

Every test passes an explicit ``path`` so nothing here touches the real
%APPDATA%/WarThunderRPC-Plus/config.json on the machine running the suite.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from wtrpc.config import Config, config_path, load, save


# ---------------------------------------------------------------------------
# Defaults & clamping
# ---------------------------------------------------------------------------


def test_defaults_match_spec():
    cfg = Config()
    assert cfg.client_id == "1550761049056223352"
    assert cfg.poll_interval == 3.0
    assert cfg.min_update_interval == 15.0
    assert cfg.show_map is True
    assert cfg.show_vehicle_image is True
    assert cfg.show_flight_data is True
    assert cfg.dogfight_detection is True
    assert cfg.connect_timeout == 0.5
    assert cfg.read_timeout == 1.5


def test_min_update_interval_clamped_to_floor_of_15():
    cfg = Config(min_update_interval=5.0)
    assert cfg.min_update_interval == 15.0

    cfg2 = Config(min_update_interval=0.0)
    assert cfg2.min_update_interval == 15.0

    cfg3 = Config(min_update_interval=30.0)
    assert cfg3.min_update_interval == 30.0


def test_poll_interval_clamped_to_floor_of_1():
    cfg = Config(poll_interval=0.1)
    assert cfg.poll_interval == 1.0

    cfg2 = Config(poll_interval=-5.0)
    assert cfg2.poll_interval == 1.0

    cfg3 = Config(poll_interval=5.0)
    assert cfg3.poll_interval == 5.0


# ---------------------------------------------------------------------------
# config_path()
# ---------------------------------------------------------------------------


def test_config_path_uses_appdata_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    p = config_path()
    assert p == tmp_path / "WarThunderRPC-Plus" / "config.json"


def test_config_path_falls_back_when_appdata_unset(monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)
    p = config_path()
    assert p == pathlib.Path.home() / ".config" / "WarThunderRPC-Plus" / "config.json"


# ---------------------------------------------------------------------------
# load() / save() round trip
# ---------------------------------------------------------------------------


def test_load_creates_file_with_defaults_when_missing(tmp_path):
    path = tmp_path / "config.json"
    assert not path.exists()

    cfg = load(path)

    assert path.exists()
    assert cfg == Config()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["client_id"] == "1550761049056223352"


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "config.json"
    cfg = Config(client_id="123", poll_interval=5.0, show_map=False)
    save(cfg, path)

    loaded = load(path)
    assert loaded == cfg


def test_load_corrupt_json_falls_back_to_defaults_without_raising(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not valid json!!", encoding="utf-8")

    cfg = load(path)

    assert cfg == Config()


def test_load_empty_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("", encoding="utf-8")

    cfg = load(path)

    assert cfg == Config()


def test_load_ignores_unknown_keys(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"client_id": "999", "totally_unknown_field": "surprise"}),
        encoding="utf-8",
    )

    cfg = load(path)

    assert cfg.client_id == "999"
    assert not hasattr(cfg, "totally_unknown_field")


def test_load_wrong_typed_value_falls_back_for_that_field_only(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "client_id": "999",
                "poll_interval": "fast",  # wrong type
                "show_map": "yes",  # wrong type
                "show_vehicle_image": False,  # valid, non-default
            }
        ),
        encoding="utf-8",
    )

    cfg = load(path)

    assert cfg.client_id == "999"  # good field kept
    assert cfg.poll_interval == Config().poll_interval  # bad field -> default
    assert cfg.show_map == Config().show_map  # bad field -> default
    assert cfg.show_vehicle_image is False  # good field kept


def test_load_clamps_values_loaded_from_disk(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"poll_interval": 0.2, "min_update_interval": 2.0}),
        encoding="utf-8",
    )

    cfg = load(path)

    assert cfg.poll_interval == 1.0
    assert cfg.min_update_interval == 15.0


def test_load_accepts_int_for_float_field(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"poll_interval": 5}), encoding="utf-8")

    cfg = load(path)

    assert cfg.poll_interval == 5.0


def test_load_never_raises_when_path_is_a_directory(tmp_path):
    # Pathological input: "path" points at a directory, not a file.
    bogus = tmp_path / "config.json"
    bogus.mkdir()

    cfg = load(bogus)

    assert cfg == Config()


def test_load_with_default_path_uses_config_path(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    cfg = load()
    assert cfg == Config()
    assert (tmp_path / "WarThunderRPC-Plus" / "config.json").exists()


def test_default_application_is_not_the_upstream_one():
    """Discord prints the APPLICATION'S name after "Playing", and no payload
    field overrides it. Shipping the upstream project's id made the presence
    announce that project's name instead of War Thunder, so the default must
    stay pointed at this project's own application."""
    assert Config().client_id != "1211769535468937237"
    assert Config().large_image == "logo"
