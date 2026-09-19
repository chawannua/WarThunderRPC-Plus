"""Tests for wtrpc.presence — Discord connection management and throttling.

Discord itself is never contacted: ``pypresence.Presence`` is patched at its
lookup point (``wtrpc.presence.connect`` does ``from pypresence import
Presence`` lazily, so patching ``pypresence.Presence`` on the module is what
actually takes effect). Time is controlled explicitly via a fake
``time.monotonic`` so rate-limit and backoff behaviour is deterministic.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pypresence.exceptions import ServerError

from wtrpc.models import PresencePayload
from wtrpc.presence import RECONNECT_BACKOFF_S, PresenceManager


class FakeClock:
    """A controllable stand-in for ``time.monotonic``."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def mock_presence_cls():
    """Patch ``pypresence.Presence`` where ``PresenceManager.connect`` looks it up."""
    with patch("pypresence.Presence") as mock_cls:
        yield mock_cls


def _payload(details: str = "details", state: str = "state") -> PresencePayload:
    return PresencePayload(details=details, state=state)


# ---------------------------------------------------------------------------
# 1. First update after connect / 2. unchanged payload does not resend
# ---------------------------------------------------------------------------


class TestUpdateSending:
    def test_first_update_after_connect_sends(self, mock_presence_cls):
        instance = mock_presence_cls.return_value
        manager = PresenceManager("client-id")
        payload = _payload("Flying", "Cruising")

        sent = manager.update(payload)

        assert sent is True
        assert manager.connected is True
        instance.connect.assert_called_once()
        instance.update.assert_called_once_with(**payload.as_kwargs())

    def test_unchanged_payload_does_not_resend(self, mock_presence_cls):
        instance = mock_presence_cls.return_value
        manager = PresenceManager("client-id")
        payload = _payload("A", "1")

        manager.update(payload)
        sent_again = manager.update(payload)

        assert sent_again is False
        assert instance.update.call_count == 1


# ---------------------------------------------------------------------------
# 3. Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    def test_changed_payload_holds_then_sends_once_the_window_elapses(
        self, mock_presence_cls
    ):
        instance = mock_presence_cls.return_value
        clock = FakeClock(0.0)
        with patch("wtrpc.presence.time.monotonic", new=clock):
            manager = PresenceManager("client-id", min_update_interval=15.0)
            a = _payload("A", "1")
            b = _payload("B", "2")

            assert manager.update(a) is True  # first send ever, always goes
            assert instance.update.call_count == 1

            clock.now = 5.0
            assert manager.update(b) is False  # changed, but inside the window
            assert instance.update.call_count == 1

            clock.now = 14.9
            assert manager.update(b) is False  # still (just) inside the window
            assert instance.update.call_count == 1

            clock.now = 16.0
            assert manager.update(b) is True  # window elapsed -- now it sends
            assert instance.update.call_count == 2
            instance.update.assert_called_with(**b.as_kwargs())


# ---------------------------------------------------------------------------
# 4. The stale pending bug
# ---------------------------------------------------------------------------


class TestStalePendingBug:
    def test_reverting_to_the_last_sent_value_cancels_a_queued_change(
        self, mock_presence_cls
    ):
        """A -> B -> A inside the rate-limit window must never publish B.

        Before the fix, the queued payload was only overwritten when the
        newly offered value differed from what was last sent, so reverting
        to the old value early-exited without clearing the queue and a
        stale ``B`` survived to be flushed once the window opened again.
        """
        instance = mock_presence_cls.return_value
        clock = FakeClock(0.0)
        with patch("wtrpc.presence.time.monotonic", new=clock):
            manager = PresenceManager("client-id", min_update_interval=15.0)
            a = _payload("A", "1")
            b = _payload("B", "2")

            assert manager.update(a) is True  # t=0: A goes out immediately
            clock.now = 5.0
            assert manager.update(b) is False  # t=5: B queued, inside window
            clock.now = 10.0
            assert manager.update(a) is False  # t=10: reverted to A, cancels B
            clock.now = 20.0
            assert manager.update(a) is False  # t=20: still A, unchanged -> no-op

            # The only thing that ever reached Discord is the original A.
            assert instance.update.call_count == 1
            instance.update.assert_called_once_with(**a.as_kwargs())


# ---------------------------------------------------------------------------
# 5. The wedge bug: payload rejection vs. transport failure
# ---------------------------------------------------------------------------


class TestPayloadRejectionVsTransportFailure:
    def test_server_error_keeps_the_connection_and_drops_the_payload(
        self, mock_presence_cls
    ):
        instance = mock_presence_cls.return_value
        clock = FakeClock(0.0)
        with patch("wtrpc.presence.time.monotonic", new=clock):
            manager = PresenceManager("client-id", min_update_interval=15.0)
            manager.update(_payload("A", "1"))  # t=0: connects and sends fine
            assert manager.connected is True

            clock.now = 20.0
            bad = _payload("Bad", "Payload")
            instance.update.side_effect = ServerError("discord said no")

            result = manager.update(bad)

            assert result is False
            assert manager.connected is True  # the pipe itself is fine

            # The rejected payload must not be retried forever: offering it
            # again must not hit Discord a second time even though it would
            # now succeed.
            instance.update.side_effect = None
            again = manager.update(bad)

            assert again is False
            assert instance.update.call_count == 2  # the original send + the one rejection

    def test_transport_error_drops_the_connection(self, mock_presence_cls):
        instance = mock_presence_cls.return_value
        clock = FakeClock(0.0)
        with patch("wtrpc.presence.time.monotonic", new=clock):
            manager = PresenceManager("client-id", min_update_interval=15.0)
            manager.update(_payload("A", "1"))
            assert manager.connected is True

            clock.now = 20.0
            instance.update.side_effect = ConnectionError("pipe closed")

            result = manager.update(_payload("B", "2"))

            assert result is False
            assert manager.connected is False


# ---------------------------------------------------------------------------
# 6. Backoff persistence
# ---------------------------------------------------------------------------


class TestBackoffPersistence:
    def test_backoff_only_resets_on_a_successful_update(self, mock_presence_cls):
        """Repeated connect failures escalate; a bare successful connect(),
        or recovering the pipe after an update() transport failure, must
        never reset the failure counter -- only a successful update() does.
        """
        instance = mock_presence_cls.return_value
        clock = FakeClock(0.0)
        with patch("wtrpc.presence.time.monotonic", new=clock):
            manager = PresenceManager("client-id")

            # Two consecutive connect() failures escalate the backoff.
            mock_presence_cls.side_effect = RuntimeError("no discord")
            manager.connect()
            delay_1 = manager._next_connect_at - clock.now  # noqa: SLF001 (white-box check)
            assert delay_1 == RECONNECT_BACKOFF_S[0]

            clock.now += delay_1
            manager.connect()
            delay_2 = manager._next_connect_at - clock.now  # noqa: SLF001
            assert delay_2 == RECONNECT_BACKOFF_S[1]
            assert delay_2 > delay_1

            # Reconnecting succeeds, but the update itself keeps failing with
            # a transport error -- a real presence update never gets through.
            clock.now += delay_2
            mock_presence_cls.side_effect = None
            assert manager.connect() is True
            instance.update.side_effect = ConnectionError("pipe closed")
            clock.now += 20  # clear of the rate-limit window
            manager.update(_payload("x", "y"))
            assert manager.connected is False  # transport error drops the pipe

            # A drop triggered from update() must escalate the delay too, not
            # pin it at the shortest value. Connecting to Discord almost always
            # succeeds; it is publishing that fails, so if only connect()'s own
            # failures counted, this loop would retry every 2s indefinitely.
            delay_after_drop = manager._next_connect_at - clock.now  # noqa: SLF001
            assert delay_after_drop == RECONNECT_BACKOFF_S[2]
            assert delay_after_drop > delay_2

            # The next connect failure continues escalating from there rather
            # than restarting at the shortest delay.
            clock.now = manager._next_connect_at  # noqa: SLF001
            mock_presence_cls.side_effect = RuntimeError("no discord again")
            manager.connect()
            delay_3 = manager._next_connect_at - clock.now  # noqa: SLF001
            assert delay_3 == RECONNECT_BACKOFF_S[3]
            assert delay_3 >= delay_after_drop


# ---------------------------------------------------------------------------
# 7. clear() idempotency
# ---------------------------------------------------------------------------


class TestClear:
    def test_clear_is_idempotent(self, mock_presence_cls):
        instance = mock_presence_cls.return_value
        manager = PresenceManager("client-id")
        manager.update(_payload("A", "1"))  # establishes the connection

        for _ in range(5):
            manager.clear()

        assert instance.clear.call_count == 1


# ---------------------------------------------------------------------------
# 8. connect() failure handling
# ---------------------------------------------------------------------------


class TestConnectFailures:
    def test_returns_false_without_raising_when_presence_construction_fails(
        self, mock_presence_cls
    ):
        mock_presence_cls.side_effect = FileNotFoundError("no discord ipc pipe")
        manager = PresenceManager("client-id")

        result = manager.connect()

        assert result is False
        assert manager.connected is False

    def test_returns_false_without_raising_when_connect_call_fails(
        self, mock_presence_cls
    ):
        instance = mock_presence_cls.return_value
        instance.connect.side_effect = ConnectionRefusedError("refused")
        manager = PresenceManager("client-id")

        result = manager.connect()

        assert result is False
        assert manager.connected is False

    def test_does_not_retry_before_its_backoff_deadline(self, mock_presence_cls):
        clock = FakeClock(0.0)
        with patch("wtrpc.presence.time.monotonic", new=clock):
            mock_presence_cls.side_effect = RuntimeError("no discord")
            manager = PresenceManager("client-id")

            assert manager.connect() is False
            mock_presence_cls.assert_called_once()

            mock_presence_cls.side_effect = None  # would succeed if retried
            clock.now = 1.0  # still within the shortest (2.0s) backoff window
            assert manager.connect() is False
            mock_presence_cls.assert_called_once()  # never attempted again
