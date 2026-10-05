# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

import asyncio
import collections
import contextlib
import dataclasses
import threading
import time
import typing

import websockets
from ..core.api_error import ApiError
from ._policy import Backoff, Ending, delay_for, followup_for

Message = typing.TypeVar("Message")
Update = typing.TypeVar("Update")


@dataclasses.dataclass(frozen=True)
class Reconnecting:
    """A connection ended, and another will be opened after `delay` seconds."""

    # The close code of the connection that ended; 1006 when it dropped without a close frame.
    code: int
    # Counting from 1 since the last session that stayed up.
    attempt: int
    delay: float
    # The HTTP status, when a handshake was rejected.
    http_status: typing.Optional[int] = None


@dataclasses.dataclass(frozen=True)
class Reconnected:
    """A new server session started, under a new session ID (run ID), with the configuration resent."""

    previous_session_id: str
    session_id: str
    # The close code that ended the previous connection.
    code: int
    attempts: int


@dataclasses.dataclass(frozen=True)
class SessionEnded:
    """Why the session ended for good."""

    code: int
    # Whether the reconnect attempts ran out.
    gave_up: bool
    http_status: typing.Optional[int] = None
    # The `reason` of the last `session.closed`, when one arrived before the close.
    session_closed_reason: typing.Optional[str] = None
    error: typing.Optional[str] = None


# A session that stays up this long resets the attempt count, so a session that keeps failing soon
# after it starts still runs out of attempts.
STABLE_SESSION_SECONDS = 10.0

_CLOSED = (websockets.exceptions.ConnectionClosed, OSError)


class _AttemptFailed(Exception):
    def __init__(self, ending: Ending, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.ending = ending
        self.cause = cause
        self.retry_after = _retry_after(cause)


def _failed(cause: BaseException) -> _AttemptFailed:
    status = cause.status_code if isinstance(cause, ApiError) else None
    return _AttemptFailed(Ending(code=1006, opened=False, client_closing=False, http_status=status), cause)


def _retry_after(cause: BaseException) -> typing.Optional[float]:
    # The generated client raises ApiError while handling the websockets exception that holds the response.
    handshake = cause.__context__
    headers = getattr(handshake, "headers", None) or getattr(getattr(handshake, "response", None), "headers", None)
    value = headers.get("Retry-After") if headers is not None else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _field(message: typing.Any, name: str) -> typing.Any:
    return message.get(name) if isinstance(message, dict) else getattr(message, name, None)


def _close_code(socket: typing.Any) -> int:
    # The generated socket client keeps the websockets connection in `_websocket`.
    code = getattr(getattr(socket, "_websocket", None), "close_code", None)
    return code if isinstance(code, int) else 1006


class _Driver(typing.Generic[Update]):
    def __init__(
        self,
        *,
        open: typing.Callable[[], typing.Any],
        send_update: typing.Callable[[typing.Any, Update], typing.Any],
        send_close: typing.Callable[[typing.Any], typing.Any],
        send_media: typing.Callable[[typing.Any, bytes], typing.Any],
        media_cost: typing.Callable[[bytes], int],
        max_queued: int,
    ) -> None:
        self.open = open
        self.send_update = send_update
        self.send_close = send_close
        self.send_media = send_media
        self.media_cost = media_cost
        self.max_queued = max_queued


class _State(typing.Generic[Update]):
    def __init__(self, driver: _Driver[Update], session_update: typing.Optional[Update], backoff: Backoff) -> None:
        self._driver = driver
        self._update = session_update
        self._backoff = backoff
        self._socket: typing.Any = None
        self._ready = False
        self._queue: typing.Deque[bytes] = collections.deque()
        self._queued = 0
        self._dropped = 0
        self._client_closing = False
        self._ended: typing.Optional[SessionEnded] = None
        self._session_id: typing.Optional[str] = None
        self._established_at: typing.Optional[float] = None
        self._attempts = 0
        self._last_code = 1006

    @property
    def session_id(self) -> typing.Optional[str]:
        """The ID of the current server session, which is also its run ID. Changes on each reconnect."""
        return self._session_id

    @property
    def reconnecting(self) -> bool:
        """Whether media sent now is queued rather than sent, because no server session is ready."""
        return not self._ready and self._ended is None

    @property
    def dropped_media(self) -> int:
        """Media dropped from the queue while reconnecting, because the queue was full."""
        return self._dropped

    @property
    def ended(self) -> typing.Optional[SessionEnded]:
        """Why the session ended, once it has."""
        return self._ended

    def _check_sendable(self) -> None:
        if self._ended is not None:
            raise RuntimeError("The session has ended.")
        if self._client_closing:
            raise RuntimeError("The session is closing.")

    def _enqueue(self, media: bytes) -> None:
        self._queue.append(media)
        self._queued += self._driver.media_cost(media)
        while self._queued > self._driver.max_queued and self._queue:
            self._queued -= self._driver.media_cost(self._queue.popleft())
            self._dropped += 1

    def _take_queued(self) -> typing.Optional[bytes]:
        if not self._queue:
            return None
        media = self._queue.popleft()
        self._queued -= self._driver.media_cost(media)
        return media

    def _start_session(self, created: typing.Any) -> typing.Optional[Reconnected]:
        previous = self._session_id
        self._session_id = str(_field(created, "session_id"))
        self._established_at = time.monotonic()
        if previous is None:
            return None
        return Reconnected(
            previous_session_id=previous, session_id=self._session_id, code=self._last_code, attempts=self._attempts
        )

    def _plan(self, ending: Ending, retry_after: typing.Optional[float]) -> typing.Union[Reconnecting, SessionEnded]:
        self._ready = False
        followup = followup_for(ending)
        if followup == "stop":
            return self._end(ending, gave_up=False)
        if self._established_at is not None and time.monotonic() - self._established_at >= STABLE_SESSION_SECONDS:
            self._attempts = 0
        self._established_at = None
        self._attempts += 1
        if self._attempts > self._backoff.max_attempts:
            return self._end(ending, gave_up=True)
        self._last_code = ending.code
        delay = delay_for(self._attempts, followup, self._backoff)
        if retry_after is not None:
            delay = max(delay, retry_after)
        return Reconnecting(code=ending.code, attempt=self._attempts, delay=delay, http_status=ending.http_status)

    def _end(self, ending: Ending, *, gave_up: bool, error: typing.Optional[str] = None) -> SessionEnded:
        if self._ended is None:
            self._ended = SessionEnded(
                code=ending.code,
                gave_up=gave_up,
                http_status=ending.http_status,
                session_closed_reason=ending.session_closed_reason,
                error=error,
            )
        self._ready = False
        self._queue.clear()
        self._queued = 0
        return self._ended

    def _ending(self, socket: typing.Any, session_closed_reason: typing.Optional[str]) -> Ending:
        return Ending(
            code=_close_code(socket),
            opened=True,
            client_closing=self._client_closing,
            session_closed_reason=session_closed_reason,
        )


class AsyncReconnectingSession(_State[Update], typing.Generic[Message, Update]):
    """
    A realtime session that reconnects when the server restarts or the connection drops. Iterate it
    to receive messages: iteration is what notices a dropped connection and reconnects, and it
    ends when the session ends for good, with the reason in `ended`.
    """

    def __init__(self, driver: _Driver[Update], session_update: typing.Optional[Update], backoff: Backoff) -> None:
        super().__init__(driver, session_update, backoff)
        self._stack: typing.Optional[contextlib.AsyncExitStack] = None
        self._pending: typing.Deque[typing.Any] = collections.deque()

    async def __aenter__(self) -> "AsyncReconnectingSession[Message, Update]":
        while True:
            try:
                created, _ = await self._attempt()
            except _AttemptFailed as failed:
                plan = self._plan(failed.ending, failed.retry_after)
                if isinstance(plan, SessionEnded):
                    raise failed.cause
                await asyncio.sleep(plan.delay)
                continue
            self._pending.append(created)
            return self

    async def __aexit__(self, *exc_info: typing.Any) -> None:
        await self.close()

    async def __aiter__(self) -> typing.AsyncIterator[typing.Union[Message, Reconnecting, Reconnected]]:
        while self._pending:
            yield self._pending.popleft()
        while self._ended is None and self._socket is not None:
            socket = self._socket
            reason: typing.Optional[str] = None
            try:
                async for message in socket:
                    if _field(message, "type") == "session.closed":
                        reason = _field(message, "reason")
                    yield message
            except _CLOSED:
                pass
            if self._ended is not None:
                return
            ending = self._ending(socket, reason)
            await self._discard()
            plan = self._plan(ending, None)
            while isinstance(plan, Reconnecting):
                yield plan
                await asyncio.sleep(plan.delay)
                if self._ended is not None:
                    return
                try:
                    created, reconnected = await self._attempt()
                except _AttemptFailed as failed:
                    plan = self._plan(failed.ending, failed.retry_after)
                    continue
                if reconnected is not None:
                    yield reconnected
                yield created
                break

    async def send_session_update(self, update: Update) -> None:
        """
        Sends the configuration and keeps it, so that every new server session after a reconnect
        gets it too. Send it before the first media, as the configuration locks at the first frame.
        """
        self._check_sendable()
        self._update = update
        if self._ready:
            with contextlib.suppress(*_CLOSED):
                await self._driver.send_update(self._socket, update)

    async def send_session_close(self) -> None:
        """Asks the server to end the session after the media already sent. No reconnect follows."""
        self._check_sendable()
        self._client_closing = True
        if self._ready:
            with contextlib.suppress(*_CLOSED):
                await self._driver.send_close(self._socket)
        else:
            await self.close()

    async def close(self) -> None:
        """Closes the connection at once, discarding anything the server had not yet sent."""
        if self._ended is None:
            self._client_closing = True
            self._end(Ending(code=1000, opened=True, client_closing=True), gave_up=False)
        await self._discard()

    async def _send_media(self, media: bytes) -> None:
        self._check_sendable()
        if self._ready:
            try:
                await self._driver.send_media(self._socket, media)
                return
            except _CLOSED:
                self._ready = False
        self._enqueue(media)

    async def _attempt(self) -> typing.Tuple[typing.Any, typing.Optional[Reconnected]]:
        stack = contextlib.AsyncExitStack()
        try:
            socket = await stack.enter_async_context(self._driver.open())
            created = await socket.recv()
        except Exception as error:
            with contextlib.suppress(Exception):
                await stack.aclose()
            raise _failed(error) from error
        if _field(created, "type") != "session.created":
            await stack.aclose()
            raise _failed(RuntimeError(f"The server's first message was {_field(created, 'type')!r}."))
        self._stack, self._socket = stack, socket
        reconnected = self._start_session(created)
        try:
            if self._update is not None:
                await self._driver.send_update(socket, self._update)
            # Media sent while this flush waits is queued behind it.
            media = self._take_queued()
            while media is not None:
                await self._driver.send_media(socket, media)
                media = self._take_queued()
            self._ready = True
        except _CLOSED:
            pass
        return created, reconnected

    async def _discard(self) -> None:
        stack, self._stack, self._ready = self._stack, None, False
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()


class ReconnectingSession(_State[Update], typing.Generic[Message, Update]):
    """
    A realtime session that reconnects when the server restarts or the connection drops. Iterate it
    to receive messages: iteration is what notices a dropped connection and reconnects, and it
    ends when the session ends for good, with the reason in `ended`. Media can be sent from another
    thread while one thread iterates.
    """

    def __init__(self, driver: _Driver[Update], session_update: typing.Optional[Update], backoff: Backoff) -> None:
        super().__init__(driver, session_update, backoff)
        self._stack: typing.Optional[contextlib.ExitStack] = None
        self._pending: typing.Deque[typing.Any] = collections.deque()
        # Held while sending, so that queued media is flushed before media sent after it.
        self._lock = threading.RLock()

    def __enter__(self) -> "ReconnectingSession[Message, Update]":
        while True:
            try:
                created, _ = self._attempt()
            except _AttemptFailed as failed:
                plan = self._plan(failed.ending, failed.retry_after)
                if isinstance(plan, SessionEnded):
                    raise failed.cause
                time.sleep(plan.delay)
                continue
            self._pending.append(created)
            return self

    def __exit__(self, *exc_info: typing.Any) -> None:
        self.close()

    def __iter__(self) -> typing.Iterator[typing.Union[Message, Reconnecting, Reconnected]]:
        while self._pending:
            yield self._pending.popleft()
        while self._ended is None and self._socket is not None:
            socket = self._socket
            reason: typing.Optional[str] = None
            try:
                for message in socket:
                    if _field(message, "type") == "session.closed":
                        reason = _field(message, "reason")
                    yield message
            except _CLOSED:
                pass
            if self._ended is not None:
                return
            ending = self._ending(socket, reason)
            self._discard()
            with self._lock:
                plan = self._plan(ending, None)
            while isinstance(plan, Reconnecting):
                yield plan
                time.sleep(plan.delay)
                if self._ended is not None:
                    return
                try:
                    created, reconnected = self._attempt()
                except _AttemptFailed as failed:
                    with self._lock:
                        plan = self._plan(failed.ending, failed.retry_after)
                    continue
                if reconnected is not None:
                    yield reconnected
                yield created
                break

    def send_session_update(self, update: Update) -> None:
        """
        Sends the configuration and keeps it, so that every new server session after a reconnect
        gets it too. Send it before the first media, as the configuration locks at the first frame.
        """
        with self._lock:
            self._check_sendable()
            self._update = update
            if self._ready:
                with contextlib.suppress(*_CLOSED):
                    self._driver.send_update(self._socket, update)

    def send_session_close(self) -> None:
        """Asks the server to end the session after the media already sent. No reconnect follows."""
        with self._lock:
            self._check_sendable()
            self._client_closing = True
            ready = self._ready
            if ready:
                with contextlib.suppress(*_CLOSED):
                    self._driver.send_close(self._socket)
        if not ready:
            self.close()

    def close(self) -> None:
        """Closes the connection at once, discarding anything the server had not yet sent."""
        with self._lock:
            if self._ended is None:
                self._client_closing = True
                self._end(Ending(code=1000, opened=True, client_closing=True), gave_up=False)
        self._discard()

    def _send_media(self, media: bytes) -> None:
        with self._lock:
            self._check_sendable()
            if self._ready:
                try:
                    self._driver.send_media(self._socket, media)
                    return
                except _CLOSED:
                    self._ready = False
            self._enqueue(media)

    def _attempt(self) -> typing.Tuple[typing.Any, typing.Optional[Reconnected]]:
        stack = contextlib.ExitStack()
        try:
            socket = stack.enter_context(self._driver.open())
            created = socket.recv()
        except Exception as error:
            with contextlib.suppress(Exception):
                stack.close()
            raise _failed(error) from error
        if _field(created, "type") != "session.created":
            stack.close()
            raise _failed(RuntimeError(f"The server's first message was {_field(created, 'type')!r}."))
        with self._lock:
            self._stack, self._socket = stack, socket
            reconnected = self._start_session(created)
            try:
                if self._update is not None:
                    self._driver.send_update(socket, self._update)
                media = self._take_queued()
                while media is not None:
                    self._driver.send_media(socket, media)
                    media = self._take_queued()
                self._ready = True
            except _CLOSED:
                pass
        return created, reconnected

    def _discard(self) -> None:
        with self._lock:
            stack, self._stack, self._ready = self._stack, None, False
        if stack is not None:
            with contextlib.suppress(Exception):
                stack.close()
