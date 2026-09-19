"""Discord connection management and update throttling.

Discord rate limits rich presence updates to roughly one every 15 seconds and
silently drops the rest, so the original approach of firing an update on every
poll was mostly wasted work. This module sends an update only when the payload
actually changed, and holds a changed payload back until the rate limit window
has elapsed rather than dropping it.

It also survives Discord restarting underneath it, which the original did not.
"""

from __future__ import annotations

import logging
import time

from .models import PresencePayload

log = logging.getLogger(__name__)

RECONNECT_BACKOFF_S = (2.0, 5.0, 10.0, 30.0)


def _payload_rejection_types() -> tuple[type[BaseException], ...]:
    """Exceptions that mean "Discord refused this payload", not "pipe is dead".

    pypresence raises ``ServerError`` when Discord answers with an error event,
    which is an application-level no, not a transport failure. Treating the two
    identically is what lets one bad payload wedge the presence permanently.
    """
    try:
        from pypresence import exceptions as _exc
    except ImportError:
        return ()

    types = [
        getattr(_exc, name)
        for name in ("ServerError", "ArgumentError", "DiscordError")
        if isinstance(getattr(_exc, name, None), type)
        and issubclass(getattr(_exc, name), BaseException)
    ]
    return tuple(types)


_PAYLOAD_REJECTED = _payload_rejection_types() or (RuntimeError,)


class PresenceManager:
    """Owns the pypresence client and decides when an update is worth sending."""

    def __init__(self, client_id: str, min_update_interval: float = 15.0) -> None:
        self._client_id = client_id
        self._min_interval = max(15.0, float(min_update_interval))
        self._rpc = None
        self._connected = False
        self._last_sent: PresencePayload | None = None
        self._last_sent_at = 0.0
        self._pending: PresencePayload | None = None
        self._failures = 0
        self._next_connect_at = 0.0
        self._connected_at = 0.0
        self._cleared = False

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        """Try to reach Discord. Returns success; never raises, never blocks long.

        Respects a backoff schedule so a closed Discord does not mean hammering
        the IPC pipe several times a second.
        """
        if self._connected:
            return True

        now = time.monotonic()
        if now < self._next_connect_at:
            return False

        try:
            from pypresence import Presence

            self._rpc = Presence(self._client_id)
            self._rpc.connect()
        except Exception as exc:  # pypresence raises a wide variety of types
            self._rpc = None
            self._connected = False
            delay = RECONNECT_BACKOFF_S[min(self._failures, len(RECONNECT_BACKOFF_S) - 1)]
            self._failures += 1
            self._next_connect_at = now + delay
            log.debug("Discord connect failed (%s); retrying in %.0fs", exc, delay)
            return False

        self._connected = True
        # NOTE: _failures is deliberately NOT reset here. Connecting is easy;
        # it is publishing that can fail. If a bad payload keeps tearing the
        # connection down, resetting the counter on every successful reconnect
        # pins the backoff at its shortest value and the app hot-loops forever.
        # Only a successful update() clears it.
        self._connected_at = now
        # Force the next update through even if the payload looks unchanged,
        # because a fresh connection has no presence set on it.
        self._last_sent = None
        self._cleared = False
        log.info("Connected to Discord")
        return True

    def _drop_connection(self, exc: BaseException) -> None:
        """Tear down after a transport failure and lengthen the retry delay.

        The delay has to escalate here, not only in ``connect()``. Connecting
        to Discord almost always succeeds; it is publishing that fails. If a
        drop triggered from ``update()`` left the counter alone, the app would
        reconnect every two seconds forever against a Discord that cannot take
        our updates, which is a hot loop wearing the IPC pipe for nothing.
        """
        delay = RECONNECT_BACKOFF_S[min(self._failures, len(RECONNECT_BACKOFF_S) - 1)]
        self._failures += 1
        log.warning("Lost the Discord connection (%s); retrying in %.0fs", exc, delay)
        self._connected = False
        self._rpc = None
        self._last_sent = None
        self._cleared = False
        self._next_connect_at = time.monotonic() + delay

    def update(self, payload: PresencePayload) -> bool:
        """Publish ``payload``, subject to change detection and rate limiting.

        Returns True when something was actually sent to Discord.
        """
        # Always remember the newest payload, even when it equals what was last
        # sent. Only overwriting on a difference lets a stale value survive:
        # change A->B, then revert B->A inside the rate limit window, and the
        # queued B would still be published afterwards, leaving Discord showing
        # a state the player left a quarter of a minute ago.
        self._pending = payload

        if payload == self._last_sent:
            self._pending = None
            return False

        if not self.connect():
            return False

        now = time.monotonic()
        if self._last_sent is not None and (now - self._last_sent_at) < self._min_interval:
            return False

        try:
            self._rpc.update(**self._pending.as_kwargs())
        except _PAYLOAD_REJECTED as exc:
            # Discord understood us and said no. The pipe is fine; the payload
            # is not. Tearing down a healthy connection here would reconnect,
            # re-queue the same bad payload and fail again forever, so drop the
            # payload instead and keep the connection.
            log.warning("Discord rejected the presence payload, skipping it: %s", exc)
            self._pending = None
            self._last_sent = payload
            self._last_sent_at = now
            return False
        except Exception as exc:
            self._drop_connection(exc)
            return False

        self._last_sent = self._pending
        self._last_sent_at = now
        self._pending = None
        self._failures = 0
        self._cleared = False
        log.debug("Presence updated: %s | %s", payload.details, payload.state)
        return True

    def clear(self) -> None:
        """Remove the presence, e.g. once War Thunder has closed.

        Idempotent on purpose: the main loop calls this on every offline poll,
        and without the guard a closed game meant an IPC round-trip every few
        seconds forever. Discord throttles those, and a throttled reply used to
        be misread as a dead connection.
        """
        self._pending = None
        if self._cleared:
            return
        if not self._connected or self._rpc is None:
            return
        try:
            self._rpc.clear()
        except Exception as exc:
            self._drop_connection(exc)
            return
        self._cleared = True
        self._last_sent = None

    def close(self) -> None:
        """Tear the connection down on shutdown, quietly."""
        if self._rpc is None:
            return
        try:
            self._rpc.clear()
            self._rpc.close()
        except Exception:
            pass
        finally:
            self._rpc = None
            self._connected = False
