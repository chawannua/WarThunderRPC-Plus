"""HTTP client for War Thunder's local telemetry server.

War Thunder exposes a small HTTP API on ``127.0.0.1:8111`` while the game is
running (see https://wiki.warthunder.com/index.php?title=Localhost_API).
This client wraps that API with real connect/read timeouts and never raises
-- every method returns ``None`` (or ``False``/``""``) on any failure so that
callers can poll it in a loop without a game running, a game in the hangar,
or a flaky connection ever crashing the process.

This module returns only raw ``dict``/``PIL.Image.Image`` values. Building a
``wtrpc.models.GameState`` out of that raw data is another layer's job.
"""

from __future__ import annotations

import io
import json

import requests
from PIL import Image, UnidentifiedImageError

from wtrpc.phash import average_hash, hamming_distance

try:
    # Optional on purpose: NOTICE tells anyone uneasy about the unspecified
    # upstream licence of this data table that they may delete it, so the app
    # has to actually survive its absence -- it just stops naming maps.
    from wtrpc.maps import maps
except ImportError:  # pragma: no cover - exercised by test_maps_table_is_optional
    maps = {}

#: Hamming distance at or below which an image hash is considered a match
#: for a known map.
_MAP_MATCH_THRESHOLD = 3


class WarThunderClient:
    """Thin, never-raising HTTP client for War Thunder's localhost API."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8111,
        connect_timeout: float = 0.5,
        read_timeout: float = 1.5,
    ) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.base_url = f"http://{host}:{port}"

        self._available = False
        self.consecutive_failures = 0

    @property
    def available(self) -> bool:
        """True if the most recent request to the game completed successfully."""
        return self._available

    def _timeout(self) -> tuple[float, float]:
        return (self.connect_timeout, self.read_timeout)

    def _mark_success(self) -> None:
        self._available = True
        self.consecutive_failures = 0

    def _mark_failure(self) -> None:
        self._available = False
        self.consecutive_failures += 1

    def _request(self, path: str) -> requests.Response | None:
        """Issue a bounded-timeout GET, or None on any connection-level failure."""
        url = f"{self.base_url}{path}"
        try:
            response = requests.get(url, timeout=self._timeout())
        except (requests.RequestException, OSError):
            self._mark_failure()
            return None

        if response.status_code != 200:
            self._mark_failure()
            return None

        return response

    def _get_json(self, path: str) -> dict | None:
        response = self._request(path)
        if response is None:
            return None

        # An empty 200 body is a normal, healthy answer, not a failure: the
        # game serves /mission.json with no content whenever the player is in
        # the hangar. Counting that as a failure made `available` read False
        # and `consecutive_failures` climb while the game was plainly running.
        if not response.content.strip():
            self._mark_success()
            return None

        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError):
            self._mark_failure()
            return None

        if not isinstance(data, dict):
            self._mark_failure()
            return None

        self._mark_success()
        return data

    def indicators(self) -> dict | None:
        """GET /indicators -- army, vehicle type, compass, altitude, etc."""
        return self._get_json("/indicators")

    def state(self) -> dict | None:
        """GET /state -- aircraft-only flight telemetry. None for ground vehicles."""
        return self._get_json("/state")

    def mission(self) -> dict | None:
        """GET /mission.json -- current mission objectives."""
        return self._get_json("/mission.json")

    def map_info(self) -> dict | None:
        """GET /map_info.json -- minimap bounds and grid metadata."""
        return self._get_json("/map_info.json")

    def hudmsg(self, last_event_id: int = 0, last_damage_id: int = 0) -> dict | None:
        """GET /hudmsg -- the kill and damage feed.

        Both query parameters are mandatory: the endpoint answers HTTP 400
        without them. Passing the highest id already seen returns only newer
        entries, which is how the caller avoids counting a kill twice.
        """
        return self._get_json(
            f"/hudmsg?lastEvt={int(last_event_id)}&lastDmg={int(last_damage_id)}"
        )

    def map_obj(self) -> list | None:
        """GET /map_obj.json -- markers on the minimap.

        Unlike the other endpoints this one returns a JSON *array*, so it does
        not go through ``_get_json``.
        """
        response = self._request("/map_obj.json")
        if response is None:
            return None

        if not response.content.strip():
            self._mark_success()
            return None

        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError):
            self._mark_failure()
            return None

        if not isinstance(data, list):
            self._mark_failure()
            return None

        self._mark_success()
        return data

    def map_image(self) -> Image.Image | None:
        """GET /map.img -- the current minimap as a JPEG, decoded in memory."""
        response = self._request("/map.img")
        if response is None:
            return None

        if not response.content:
            self._mark_failure()
            return None

        try:
            image = Image.open(io.BytesIO(response.content))
            image.load()
        except (OSError, UnidentifiedImageError, ValueError):
            self._mark_failure()
            return None

        self._mark_success()
        return image

    def identify_map(self, image: Image.Image | None) -> str:
        """Identify the minimap image against the known map hash table.

        Returns the human readable map name (underscores replaced with
        spaces) when the closest known hash is within the match threshold,
        otherwise returns "". Also returns "" when the two closest matches
        are different maps within one bit of each other (e.g. Sinai vs
        Sands of Sinai): that is a coin flip, not an identification.
        """
        if image is None:
            return ""

        image_hash = average_hash(image)

        # Several hashes can belong to one map, so rank maps, not hashes:
        # otherwise a second variant of the best map takes the runner-up slot
        # and hides a different map just as close.
        closest: dict[str, int] = {}
        for known_hash, meta in maps.items():
            distance = hamming_distance(image_hash, known_hash)
            name = meta["name"]
            if name not in closest or distance < closest[name]:
                closest[name] = distance
        if not closest:
            return ""

        ranked = sorted(closest.items(), key=lambda item: item[1])
        best_name, best_distance = ranked[0]
        if best_distance > _MAP_MATCH_THRESHOLD:
            return ""
        if len(ranked) > 1 and ranked[1][1] - best_distance <= 1:
            return ""

        return best_name.replace("_", " ")
