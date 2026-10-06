# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

import asyncio
import random
import time
import typing

from ..core.api_error import ApiError
from ..types.audio_session_closed import AudioSessionClosed
from ..types.error import Error
from ..types.video_session_closed import VideoSessionClosed
from websockets.exceptions import ConnectionClosed, WebSocketException

Endpoint = typing.Literal["audio", "video"]

# Going away, a dropped connection, a server error, a restart, and try again later.
_RECONNECT_CODES = frozenset({1001, 1006, 1011, 1012, 1013})

# The close code for a connection that ended without a close frame from the server.
_ABNORMAL_CLOSURE = 1006

# A request timeout or a rate limit can clear by the next attempt. Other 4xx statuses, such as the
# one for an invalid API key, fail the same way every time.
_RETRYABLE_STATUSES = frozenset({408, 429})

# The Node SDK's defaults, so that both SDKs reconnect on the same schedule.
_MAX_ATTEMPTS = 30
_FIRST_DELAY_RANGE = (1.0, 5.0)
_DELAY_GROWTH = 1.3
_MAX_DELAY = 10.0
# A connection that stays open this long starts the count of attempts again.
_MIN_UPTIME = 5.0


class Session:
    """What the current session reported before it ended."""

    def __init__(self, endpoint: Endpoint) -> None:
        self._endpoint = endpoint
        self.reset()

    def reset(self) -> None:
        self._closed_reason: typing.Any = None
        self._error_code: typing.Any = None

    def observe(self, message: object) -> None:
        if isinstance(message, (AudioSessionClosed, VideoSessionClosed)):
            self._closed_reason = message.reason
        elif isinstance(message, Error):
            self._error_code = message.code

    def should_reconnect(self, closed: ConnectionClosed) -> bool:
        # A server that is shutting down may close with code 1000, which otherwise means the
        # session ended for good.
        if self._closed_reason == "server_shutdown":
            return True
        # A connection the client failed itself, as after an unanswered keepalive ping, also has
        # no close frame from the server.
        code = closed.rcvd.code if closed.rcvd is not None else _ABNORMAL_CLOSURE
        # The API reference says to open a new connection after an audio internal error. The other
        # session-ending errors, such as an invalid config, need a change from the caller first,
        # and a video session ends on internal errors only after five in a row.
        if code == 1008:
            return self._endpoint == "audio" and self._error_code == "internal_error"
        return code in _RECONNECT_CODES


def is_retryable(error: Exception) -> bool:
    """Whether a connection attempt that failed with `error` can succeed if it is made again."""
    if isinstance(error, ApiError) and error.status_code is not None:
        return not 400 <= error.status_code < 500 or error.status_code in _RETRYABLE_STATUSES
    # Before Python 3.11, asyncio's timeout error is not an OSError.
    return isinstance(error, (OSError, asyncio.TimeoutError, WebSocketException))


class Attempts:
    """Spaces out the attempts to reconnect, and ends them once too many have been made in a row."""

    def __init__(self) -> None:
        self._first_delay = random.uniform(*_FIRST_DELAY_RANGE)
        self._made = 0
        self._opened_at: typing.Optional[float] = None

    def opened(self) -> None:
        self._opened_at = time.monotonic()

    def next_delay(self) -> typing.Optional[float]:
        """The seconds to wait before the next attempt, or None when no attempts are left."""
        if self._opened_at is not None and time.monotonic() - self._opened_at >= _MIN_UPTIME:
            self._made = 0
        self._opened_at = None
        if self._made >= _MAX_ATTEMPTS:
            return None
        self._made += 1
        return min(_MAX_DELAY, self._first_delay * _DELAY_GROWTH ** (self._made - 1))
