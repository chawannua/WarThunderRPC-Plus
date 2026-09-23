"""Tests for wtrpc.wtclient.WarThunderClient.

These never require a running instance of War Thunder: every network call is
mocked via ``unittest.mock.patch`` over ``requests.get``.
"""

from __future__ import annotations

import io
import json
import pathlib

import pytest
import requests
from PIL import Image
from unittest.mock import patch, MagicMock

from wtrpc.wtclient import WarThunderClient

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES / name, "r", encoding="utf-8") as f:
        return json.load(f)


def _mock_response(json_data=None, status_code=200, content=None, json_error=None):
    """Build a response mock that a real HTTP server could actually produce.

    The body defaults to the serialised ``json_data`` rather than to b"",
    because a 200 carrying JSON always has a non-empty body. Mocks that pair
    decodable JSON with an empty body describe a state the game never emits,
    and the client legitimately treats an empty body as "no data".
    """
    resp = MagicMock()
    resp.status_code = status_code
    if content is None:
        content = json.dumps(json_data).encode() if json_data is not None else b""
    resp.content = content
    if json_error is not None:
        resp.json.side_effect = json_error
    else:
        resp.json.return_value = json_data
    return resp


def _make_png_bytes(size=(32, 32), color=(10, 20, 30)):
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# indicators() / state() / mission() / map_info() happy paths
# ---------------------------------------------------------------------------


@patch("wtrpc.wtclient.requests.get")
def test_indicators_happy_path(mock_get):
    data = _load_fixture("client_indicators_air.json")
    mock_get.return_value = _mock_response(json_data=data)

    client = WarThunderClient()
    result = client.indicators()

    assert result == data
    assert client.available is True
    assert client.consecutive_failures == 0
    mock_get.assert_called_once()
    _, kwargs = mock_get.call_args
    assert kwargs["timeout"] == (0.5, 1.5)


@patch("wtrpc.wtclient.requests.get")
def test_state_happy_path(mock_get):
    data = _load_fixture("client_state_aircraft.json")
    mock_get.return_value = _mock_response(json_data=data)

    client = WarThunderClient()
    result = client.state()

    assert result == data
    assert client.available is True


@patch("wtrpc.wtclient.requests.get")
def test_mission_happy_path(mock_get):
    data = _load_fixture("client_mission.json")
    mock_get.return_value = _mock_response(json_data=data)

    client = WarThunderClient()
    result = client.mission()

    assert result == data
    assert result["objectives"][0]["primary"] is True


@patch("wtrpc.wtclient.requests.get")
def test_map_info_happy_path(mock_get):
    data = _load_fixture("client_map_info.json")
    mock_get.return_value = _mock_response(json_data=data)

    client = WarThunderClient()
    result = client.map_info()

    assert result == data


@patch("wtrpc.wtclient.requests.get")
def test_indicators_hangar_valid_false(mock_get):
    data = _load_fixture("client_indicators_hangar.json")
    mock_get.return_value = _mock_response(json_data=data)

    client = WarThunderClient()
    result = client.indicators()

    assert result == {"valid": False}


# ---------------------------------------------------------------------------
# Failure modes: connection refused, timeout, HTTP error, malformed/empty body
# ---------------------------------------------------------------------------


@patch("wtrpc.wtclient.requests.get")
def test_connection_refused_returns_none_and_never_raises(mock_get):
    mock_get.side_effect = requests.exceptions.ConnectionError("refused")

    client = WarThunderClient()
    result = client.indicators()

    assert result is None
    assert client.available is False
    assert client.consecutive_failures == 1


@patch("wtrpc.wtclient.requests.get")
def test_read_timeout_returns_none(mock_get):
    mock_get.side_effect = requests.exceptions.ReadTimeout("timed out")

    client = WarThunderClient()
    result = client.state()

    assert result is None
    assert client.available is False
    assert client.consecutive_failures == 1


@patch("wtrpc.wtclient.requests.get")
def test_connect_timeout_returns_none(mock_get):
    mock_get.side_effect = requests.exceptions.ConnectTimeout("timed out")

    client = WarThunderClient()
    result = client.indicators()

    assert result is None
    assert client.available is False


@patch("wtrpc.wtclient.requests.get")
def test_http_500_returns_none(mock_get):
    mock_get.return_value = _mock_response(status_code=500, content=b"error")

    client = WarThunderClient()
    result = client.mission()

    assert result is None
    assert client.available is False
    assert client.consecutive_failures == 1


@patch("wtrpc.wtclient.requests.get")
def test_malformed_json_body_returns_none(mock_get):
    mock_get.return_value = _mock_response(
        content=b"not json",
        json_error=json.JSONDecodeError("bad json", "not json", 0),
    )

    client = WarThunderClient()
    result = client.map_info()

    assert result is None
    assert client.available is False


@patch("wtrpc.wtclient.requests.get")
def test_empty_body_returns_none_but_keeps_the_game_marked_available(mock_get):
    """An empty 200 means "no data right now", not "the game went away".

    War Thunder serves /mission.json with an empty body for the whole time the
    player sits in the hangar. Treating that as a transport failure made
    `available` report False while the game was demonstrably running, and made
    `consecutive_failures` climb forever during a normal hangar session.
    """
    mock_get.return_value = _mock_response(
        content=b"", json_error=json.JSONDecodeError("Expecting value", "", 0)
    )

    client = WarThunderClient()
    result = client.mission()

    assert result is None
    assert client.available is True
    assert client.consecutive_failures == 0


@patch("wtrpc.wtclient.requests.get")
def test_whitespace_only_body_is_also_treated_as_no_data(mock_get):
    mock_get.return_value = _mock_response(
        content=b"  \n ", json_error=json.JSONDecodeError("Expecting value", "", 0)
    )

    client = WarThunderClient()

    assert client.mission() is None
    assert client.available is True


@patch("wtrpc.wtclient.requests.get")
def test_non_empty_but_malformed_body_is_still_a_failure(mock_get):
    """A body with bytes in it that will not parse is a genuine fault."""
    mock_get.return_value = _mock_response(
        content=b"<html>500 oops</html>",
        json_error=json.JSONDecodeError("Expecting value", "", 0),
    )

    client = WarThunderClient()

    assert client.indicators() is None
    assert client.available is False
    assert client.consecutive_failures == 1


@patch("wtrpc.wtclient.requests.get")
def test_consecutive_failures_increments_then_resets_on_success(mock_get):
    client = WarThunderClient()

    mock_get.side_effect = requests.exceptions.ConnectionError("refused")
    client.indicators()
    client.indicators()
    assert client.consecutive_failures == 2

    mock_get.side_effect = None
    mock_get.return_value = _mock_response(json_data={"valid": True})
    client.indicators()
    assert client.consecutive_failures == 0
    assert client.available is True


def test_never_raises_on_os_error():
    with patch("wtrpc.wtclient.requests.get", side_effect=OSError("network down")):
        client = WarThunderClient()
        result = client.state()
        assert result is None
        assert client.available is False


# ---------------------------------------------------------------------------
# map_image()
# ---------------------------------------------------------------------------


@patch("wtrpc.wtclient.requests.get")
def test_map_image_happy_path_returns_pil_image(mock_get):
    png_bytes = _make_png_bytes(size=(64, 48))
    mock_get.return_value = _mock_response(status_code=200, content=png_bytes)

    client = WarThunderClient()
    img = client.map_image()

    assert img is not None
    assert img.size == (64, 48)
    assert client.available is True


@patch("wtrpc.wtclient.requests.get")
def test_map_image_connection_refused_returns_none(mock_get):
    mock_get.side_effect = requests.exceptions.ConnectionError("refused")

    client = WarThunderClient()
    img = client.map_image()

    assert img is None
    assert client.available is False


@patch("wtrpc.wtclient.requests.get")
def test_map_image_bad_bytes_returns_none(mock_get):
    mock_get.return_value = _mock_response(status_code=200, content=b"not an image")

    client = WarThunderClient()
    img = client.map_image()

    assert img is None
    assert client.available is False


@patch("wtrpc.wtclient.requests.get")
def test_map_image_http_error_returns_none(mock_get):
    mock_get.return_value = _mock_response(status_code=404, content=b"")

    client = WarThunderClient()
    img = client.map_image()

    assert img is None


@patch("wtrpc.wtclient.requests.get")
def test_map_image_empty_body_returns_none(mock_get):
    mock_get.return_value = _mock_response(status_code=200, content=b"")

    client = WarThunderClient()
    img = client.map_image()

    assert img is None


def test_map_image_never_writes_to_current_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    png_bytes = _make_png_bytes()
    with patch("wtrpc.wtclient.requests.get") as mock_get:
        mock_get.return_value = _mock_response(status_code=200, content=png_bytes)
        client = WarThunderClient()
        img = client.map_image()
        assert img is not None

    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# identify_map()
# ---------------------------------------------------------------------------


def test_identify_map_returns_empty_string_when_no_close_match():
    client = WarThunderClient()
    fake_maps = {
        "0000000000000000": {
            "name": "Some_Fake_Map",
            "ULHC_lat": 0.0,
            "ULHC_lon": 0.0,
            "size_km": 10,
        }
    }
    with patch("wtrpc.wtclient.maps", fake_maps):
        # all-white image hashes to all-zero bits (ffffffffffffffff distance
        # from an all-zero hash is 64, way beyond the threshold of 3)
        noisy = Image.new("RGB", (64, 64))
        pixels = []
        for y in range(64):
            for x in range(64):
                pixels.append(((x * 37 + y * 91) % 256, (x * 13) % 256, (y * 71) % 256))
        noisy.putdata(pixels)
        result = client.identify_map(noisy)
        assert result == ""


def test_identify_map_returns_name_with_spaces_on_close_match():
    client = WarThunderClient()

    from wtrpc.phash import average_hash

    probe = Image.new("RGB", (64, 64), (5, 5, 5))
    exact_hash = average_hash(probe)

    fake_maps = {
        exact_hash: {
            "name": "Second_Battle_of_El_Alamein",
            "ULHC_lat": 30.4,
            "ULHC_lon": 27.3,
            "size_km": 65,
        }
    }
    with patch("wtrpc.wtclient.maps", fake_maps):
        result = client.identify_map(probe)
        assert result == "Second Battle of El Alamein"


def test_identify_map_none_image_returns_empty_string():
    client = WarThunderClient()
    assert client.identify_map(None) == ""


def test_identify_map_returns_empty_string_when_ambiguous():
    """Sinai vs Sands of Sinai: two different maps one bit apart is a coin
    flip, not an identification -- prefer no answer to a wrong one."""
    client = WarThunderClient()

    from wtrpc.phash import average_hash

    probe = Image.new("RGB", (64, 64), (5, 5, 5))
    exact_hash = average_hash(probe)
    neighbour_hash = format(int(exact_hash, 16) ^ 0b1, "016x")

    fake_maps = {
        exact_hash: {
            "name": "Sinai",
            "ULHC_lat": 0.0,
            "ULHC_lon": 0.0,
            "size_km": 65,
        },
        neighbour_hash: {
            "name": "Sands_of_Sinai",
            "ULHC_lat": 0.0,
            "ULHC_lon": 0.0,
            "size_km": 65,
        },
    }
    with patch("wtrpc.wtclient.maps", fake_maps):
        result = client.identify_map(probe)
        assert result == ""


def test_identify_map_close_second_place_same_name_is_not_ambiguous():
    """Two hash variants of the *same* map should not trigger the
    different-name ambiguity guard."""
    client = WarThunderClient()

    from wtrpc.phash import average_hash

    probe = Image.new("RGB", (64, 64), (5, 5, 5))
    exact_hash = average_hash(probe)
    neighbour_hash = format(int(exact_hash, 16) ^ 0b1, "016x")

    fake_maps = {
        exact_hash: {
            "name": "Second_Battle_of_El_Alamein",
            "ULHC_lat": 0.0,
            "ULHC_lon": 0.0,
            "size_km": 65,
        },
        neighbour_hash: {
            "name": "Second_Battle_of_El_Alamein",
            "ULHC_lat": 0.0,
            "ULHC_lon": 0.0,
            "size_km": 65,
        },
    }
    with patch("wtrpc.wtclient.maps", fake_maps):
        result = client.identify_map(probe)
        assert result == "Second Battle of El Alamein"


def test_identify_map_a_second_variant_of_the_best_map_does_not_hide_a_rival():
    """A at 0, A again at 1, B at 1: the runner-up that matters is B."""
    client = WarThunderClient()

    from wtrpc.phash import average_hash

    probe = Image.new("RGB", (64, 64), (5, 5, 5))
    exact_hash = average_hash(probe)
    same_map_variant = format(int(exact_hash, 16) ^ 0b01, "016x")
    rival = format(int(exact_hash, 16) ^ 0b10, "016x")

    meta = {"ULHC_lat": 0.0, "ULHC_lon": 0.0, "size_km": 65}
    fake_maps = {
        exact_hash: {"name": "Sinai", **meta},
        same_map_variant: {"name": "Sinai", **meta},
        rival: {"name": "Sands_of_Sinai", **meta},
    }
    with patch("wtrpc.wtclient.maps", fake_maps):
        assert client.identify_map(probe) == ""
