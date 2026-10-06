# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

import abc
import asyncio
import contextlib
import json
import threading
import typing

from ..audio.client import AsyncAudioClient, AudioClient
from ..audio.socket_client import AsyncAudioSocketClient, AudioSocketClient, AudioSocketClientResponse
from ..core.request_options import RequestOptions
from ..types.audio_session_update import AudioSessionUpdate
from ..types.session_close import SessionClose
from ..types.video_session_update import VideoSessionUpdate
from ..video.client import AsyncVideoClient, VideoClient
from ..video.socket_client import AsyncVideoSocketClient, VideoSocketClient, VideoSocketClientResponse
from ._policy import Attempts, Endpoint, Session, is_retryable
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, ConnectionClosedOK

_SocketT = typing.TypeVar("_SocketT")
_ResponseT = typing.TypeVar("_ResponseT")
_UpdateT = typing.TypeVar("_UpdateT")


class _AsyncReconnectingSocket(abc.ABC, typing.Generic[_SocketT, _ResponseT, _UpdateT]):
    _ENDPOINT: typing.ClassVar[Endpoint]
    # The socket from `connect()` for the current session. While a reconnect is under way it is
    # the closed one, so that sending raises as it does on any closed socket.
    _socket: _SocketT

    def __init__(self, connect: typing.Callable[[], typing.AsyncContextManager[_SocketT]]) -> None:
        self._connect = connect
        self._connection = contextlib.AsyncExitStack()
        self._session = Session(self._ENDPOINT)
        self._attempts = Attempts()
        self._update: typing.Optional[_UpdateT] = None
        # Set once no reconnect may follow: the session ended for good, the caller sent
        # session.close, or the context exited. Setting it wakes a reconnect that is waiting.
        self._ended = asyncio.Event()
        # The reconnect under way, in a task of its own so that it outlives a cancelled receive.
        self._reconnecting: typing.Optional[asyncio.Task[bool]] = None

    async def recv(self) -> _ResponseT:
        """
        Receive the next message. When the session has ended in a way that a new session can
        recover from, this reconnects first, and the message comes from the new session.
        Cancelling it, as a timeout does, leaves a reconnect under way running for the next call.
        """
        return await self._receive(iterating=False)

    async def __aiter__(self) -> typing.AsyncIterator[_ResponseT]:
        while True:
            try:
                message = await self._receive(iterating=True)
            except ConnectionClosedOK:
                return
            yield message

    async def _receive(self, *, iterating: bool) -> _ResponseT:
        while True:
            try:
                message = await self._recv_from(self._socket)
            except ConnectionClosed as error:
                closed = error
            except json.JSONDecodeError:
                # Matches the socket from `connect()`, whose iteration skips text that is not JSON
                # and whose recv raises.
                if iterating:
                    continue
                raise
            else:
                # Its iteration also skips messages of unknown types, which its recv returns as dicts.
                if iterating and isinstance(message, dict):
                    continue
                self._session.observe(message)
                return message
            reconnecting = self._reconnecting
            if reconnecting is None:
                reconnecting = self._reconnecting = asyncio.create_task(self._reconnect(closed))
            # Outside the except clause, so that an error from reconnecting keeps its own context.
            # Shielded, so that cancelling this receive leaves the reconnect running for the next
            # receive to wait on, rather than starting another attempt.
            try:
                reconnected = await asyncio.shield(reconnecting)
            except Exception:
                self._reconnecting = None
                self._ended.set()
                raise
            self._reconnecting = None
            if not reconnected:
                self._ended.set()
                raise closed

    async def _reconnect(self, closed: ConnectionClosed) -> bool:
        """
        Opens a new session if one can follow the session that ended, and returns whether it did.
        Raises when no attempts are left: the last attempt's error, or, if that attempt connected,
        a `ConnectionClosedError` for the close that ended its session.
        """
        if self._ended.is_set() or not self._session.should_reconnect(closed):
            return False
        await self._connection.aclose()
        failure: typing.Optional[Exception] = None
        while True:
            delay = self._attempts.next_delay()
            if delay is None:
                if failure is not None:
                    raise failure
                # The last attempt connected, and its session ended before the count started
                # again. An error even after a normal close, so that iteration does not end as
                # though the session had finished.
                raise ConnectionClosedError(closed.rcvd, closed.sent, closed.rcvd_then_sent)
            if await self._ends_within(delay):
                return False
            try:
                return await self._open()
            except Exception as error:
                if not is_retryable(error):
                    raise
                failure = error

    async def _open(self) -> bool:
        """
        Opens a connection and sends it the last session.update. Returns False, having closed the
        connection, if the socket ended meanwhile.
        """
        async with contextlib.AsyncExitStack() as connection:
            socket = await connection.enter_async_context(self._connect())
            if self._update is not None:
                await self._send_update_to(socket, self._update)
            if self._ended.is_set():
                return False
            self._connection = connection.pop_all()
            self._socket = socket
        self._session.reset()
        self._attempts.opened()
        return True

    async def _ends_within(self, delay: float) -> bool:
        try:
            await asyncio.wait_for(self._ended.wait(), delay)
        except asyncio.TimeoutError:
            return False
        return True

    def _end(self) -> _SocketT:
        """Ends the socket, so that no reconnect follows, and returns the current session's socket."""
        self._ended.set()
        return self._socket

    async def _close(self) -> None:
        self._ended.set()
        await self._connection.aclose()

    # Supplied by each endpoint, since the sockets from `connect()` differ in method names and
    # message types.
    @abc.abstractmethod
    async def _recv_from(self, socket: _SocketT) -> _ResponseT: ...

    @abc.abstractmethod
    async def _send_update_to(self, socket: _SocketT, update: _UpdateT) -> None: ...


class AsyncReconnectingAudioSocket(
    _AsyncReconnectingSocket[AsyncAudioSocketClient, AudioSocketClientResponse, AudioSessionUpdate]
):
    """
    A realtime audio socket that reconnects when the server restarts or the connection drops, from
    `reconnecting(client.audio)`. While it reconnects, sending raises
    `websockets.exceptions.ConnectionClosed`. Audio and `session.update` sent then have no effect,
    and `session.close` stops the reconnect.
    """

    _ENDPOINT = "audio"

    async def send_audio_session_update(self, message: AudioSessionUpdate) -> None:
        """
        Send the configuration, which each new session after a reconnect also receives before
        anything else. Send it before the first audio frame: a `session.update` after audio ends
        the session with `config_invalid`.
        """
        await self._socket.send_audio_session_update(message)
        self._update = message

    async def send_audio_session_close(self, message: SessionClose) -> None:
        """
        Ask the server to end the session once it has measured the audio already sent. No
        reconnect follows once you call this, even if the server is shutting down or the send
        raises.
        """
        await self._end().send_audio_session_close(message)

    async def send_audio_frame(self, message: bytes) -> None:
        """Send audio: signed 16-bit little-endian PCM, mono, 16 kHz, with no header."""
        await self._socket.send_audio_frame(message)

    async def _recv_from(self, socket: AsyncAudioSocketClient) -> AudioSocketClientResponse:
        return await socket.recv()

    async def _send_update_to(self, socket: AsyncAudioSocketClient, update: AudioSessionUpdate) -> None:
        await socket.send_audio_session_update(update)


class AsyncReconnectingVideoSocket(
    _AsyncReconnectingSocket[AsyncVideoSocketClient, VideoSocketClientResponse, VideoSessionUpdate]
):
    """
    A realtime video socket that reconnects when the server restarts or the connection drops, from
    `reconnecting(client.video)`. While it reconnects, sending raises
    `websockets.exceptions.ConnectionClosed`. Frames and `session.update` sent then have no effect,
    and `session.close` stops the reconnect.
    """

    _ENDPOINT = "video"

    async def send_video_session_update(self, message: VideoSessionUpdate) -> None:
        """
        Send the configuration, which each new session after a reconnect also receives before
        anything else. Send it before the first frame, since the configuration locks at the first
        frame.
        """
        await self._socket.send_video_session_update(message)
        self._update = message

    async def send_video_session_close(self, message: SessionClose) -> None:
        """
        Ask the server to end the session once it has answered the frames already sent. No
        reconnect follows once you call this, even if the server is shutting down or the send
        raises.
        """
        await self._end().send_video_session_close(message)

    async def send_image_frame(self, message: bytes) -> None:
        """Send one complete JPEG image."""
        await self._socket.send_image_frame(message)

    async def _recv_from(self, socket: AsyncVideoSocketClient) -> VideoSocketClientResponse:
        return await socket.recv()

    async def _send_update_to(self, socket: AsyncVideoSocketClient, update: VideoSessionUpdate) -> None:
        await socket.send_video_session_update(update)


class _ReconnectingSocket(abc.ABC, typing.Generic[_SocketT, _ResponseT, _UpdateT]):
    _ENDPOINT: typing.ClassVar[Endpoint]
    # The socket from `connect()` for the current session. While a reconnect is under way it is
    # the closed one, so that sending raises as it does on any closed socket.
    _socket: _SocketT

    def __init__(self, connect: typing.Callable[[], typing.ContextManager[_SocketT]]) -> None:
        self._connect = connect
        self._connection = contextlib.ExitStack()
        self._session = Session(self._ENDPOINT)
        self._attempts = Attempts()
        self._update: typing.Optional[_UpdateT] = None
        # Set once no reconnect may follow: the session ended for good, the caller sent
        # session.close, or the context exited. Setting it wakes a reconnect that is waiting.
        self._ended = threading.Event()
        # Held while the connection is replaced or detached. Another thread can end the socket
        # while the receiving thread opens a connection, and then either the opened connection is
        # the one that thread sees, or it is closed without being used.
        self._lock = threading.Lock()

    def recv(self) -> _ResponseT:
        """
        Receive the next message. When the session has ended in a way that a new session can
        recover from, this reconnects first, and the message comes from the new session.
        """
        return self._receive(iterating=False)

    def __iter__(self) -> typing.Iterator[_ResponseT]:
        while True:
            try:
                message = self._receive(iterating=True)
            except ConnectionClosedOK:
                return
            yield message

    def _receive(self, *, iterating: bool) -> _ResponseT:
        while True:
            try:
                message = self._recv_from(self._socket)
            except ConnectionClosed as error:
                closed = error
            except json.JSONDecodeError:
                # Matches the socket from `connect()`, whose iteration skips text that is not JSON
                # and whose recv raises.
                if iterating:
                    continue
                raise
            else:
                # Its iteration also skips messages of unknown types, which its recv returns as dicts.
                if iterating and isinstance(message, dict):
                    continue
                self._session.observe(message)
                return message
            # Outside the except clause, so that an error from reconnecting keeps its own context.
            try:
                reconnected = self._reconnect(closed)
            except Exception:
                self._ended.set()
                raise
            if not reconnected:
                self._ended.set()
                raise closed

    def _reconnect(self, closed: ConnectionClosed) -> bool:
        """
        Opens a new session if one can follow the session that ended, and returns whether it did.
        Raises when no attempts are left: the last attempt's error, or, if that attempt connected,
        a `ConnectionClosedError` for the close that ended its session.
        """
        if self._ended.is_set() or not self._session.should_reconnect(closed):
            return False
        self._detach_connection().close()
        failure: typing.Optional[Exception] = None
        while True:
            delay = self._attempts.next_delay()
            if delay is None:
                if failure is not None:
                    raise failure
                # The last attempt connected, and its session ended before the count started
                # again. An error even after a normal close, so that iteration does not end as
                # though the session had finished.
                raise ConnectionClosedError(closed.rcvd, closed.sent, closed.rcvd_then_sent)
            if self._ended.wait(delay):
                return False
            try:
                return self._open()
            except Exception as error:
                if not is_retryable(error):
                    raise
                failure = error

    def _open(self) -> bool:
        """
        Opens a connection and sends it the last session.update. Returns False, having closed the
        connection, if the socket ended meanwhile.
        """
        with contextlib.ExitStack() as connection:
            socket = connection.enter_context(self._connect())
            if self._update is not None:
                self._send_update_to(socket, self._update)
            with self._lock:
                if self._ended.is_set():
                    return False
                self._connection = connection.pop_all()
                self._socket = socket
        self._session.reset()
        self._attempts.opened()
        return True

    def _end(self) -> _SocketT:
        """Ends the socket, so that no reconnect follows, and returns the current session's socket."""
        self._ended.set()
        with self._lock:
            return self._socket

    def _detach_connection(self) -> contextlib.ExitStack:
        # Swapped under the lock, so that each connection is closed by one thread only.
        with self._lock:
            connection, self._connection = self._connection, contextlib.ExitStack()
        return connection

    def _close(self) -> None:
        self._ended.set()
        self._detach_connection().close()

    # Supplied by each endpoint, since the sockets from `connect()` differ in method names and
    # message types.
    @abc.abstractmethod
    def _recv_from(self, socket: _SocketT) -> _ResponseT: ...

    @abc.abstractmethod
    def _send_update_to(self, socket: _SocketT, update: _UpdateT) -> None: ...


class ReconnectingAudioSocket(_ReconnectingSocket[AudioSocketClient, AudioSocketClientResponse, AudioSessionUpdate]):
    """
    A realtime audio socket that reconnects when the server restarts or the connection drops, from
    `reconnecting(client.audio)`. While it reconnects, sending raises
    `websockets.exceptions.ConnectionClosed`. Audio and `session.update` sent then have no effect,
    and `session.close` stops the reconnect. You can send from other threads while one thread
    receives.
    """

    _ENDPOINT = "audio"

    def send_audio_session_update(self, message: AudioSessionUpdate) -> None:
        """
        Send the configuration, which each new session after a reconnect also receives before
        anything else. Send it before the first audio frame: a `session.update` after audio ends
        the session with `config_invalid`.
        """
        self._socket.send_audio_session_update(message)
        self._update = message

    def send_audio_session_close(self, message: SessionClose) -> None:
        """
        Ask the server to end the session once it has measured the audio already sent. No
        reconnect follows once you call this, even if the server is shutting down or the send
        raises.
        """
        self._end().send_audio_session_close(message)

    def send_audio_frame(self, message: bytes) -> None:
        """Send audio: signed 16-bit little-endian PCM, mono, 16 kHz, with no header."""
        self._socket.send_audio_frame(message)

    def _recv_from(self, socket: AudioSocketClient) -> AudioSocketClientResponse:
        return socket.recv()

    def _send_update_to(self, socket: AudioSocketClient, update: AudioSessionUpdate) -> None:
        socket.send_audio_session_update(update)


class ReconnectingVideoSocket(_ReconnectingSocket[VideoSocketClient, VideoSocketClientResponse, VideoSessionUpdate]):
    """
    A realtime video socket that reconnects when the server restarts or the connection drops, from
    `reconnecting(client.video)`. While it reconnects, sending raises
    `websockets.exceptions.ConnectionClosed`. Frames and `session.update` sent then have no effect,
    and `session.close` stops the reconnect. You can send from other threads while one thread
    receives.
    """

    _ENDPOINT = "video"

    def send_video_session_update(self, message: VideoSessionUpdate) -> None:
        """
        Send the configuration, which each new session after a reconnect also receives before
        anything else. Send it before the first frame, since the configuration locks at the first
        frame.
        """
        self._socket.send_video_session_update(message)
        self._update = message

    def send_video_session_close(self, message: SessionClose) -> None:
        """
        Ask the server to end the session once it has answered the frames already sent. No
        reconnect follows once you call this, even if the server is shutting down or the send
        raises.
        """
        self._end().send_video_session_close(message)

    def send_image_frame(self, message: bytes) -> None:
        """Send one complete JPEG image."""
        self._socket.send_image_frame(message)

    def _recv_from(self, socket: VideoSocketClient) -> VideoSocketClientResponse:
        return socket.recv()

    def _send_update_to(self, socket: VideoSocketClient, update: VideoSessionUpdate) -> None:
        socket.send_video_session_update(update)


_AsyncReconnectingT = typing.TypeVar(
    "_AsyncReconnectingT", bound=_AsyncReconnectingSocket[typing.Any, typing.Any, typing.Any]
)
_ReconnectingT = typing.TypeVar("_ReconnectingT", bound=_ReconnectingSocket[typing.Any, typing.Any, typing.Any])


@contextlib.asynccontextmanager
async def _async_connected(socket: _AsyncReconnectingT) -> typing.AsyncIterator[_AsyncReconnectingT]:
    await socket._open()
    try:
        yield socket
    finally:
        await socket._close()


@contextlib.contextmanager
def _connected(socket: _ReconnectingT) -> typing.Iterator[_ReconnectingT]:
    socket._open()
    try:
        yield socket
    finally:
        socket._close()


@typing.overload
def reconnecting(
    resource: AudioClient, *, request_options: typing.Optional[RequestOptions] = None
) -> typing.ContextManager[ReconnectingAudioSocket]: ...


@typing.overload
def reconnecting(
    resource: VideoClient, *, request_options: typing.Optional[RequestOptions] = None
) -> typing.ContextManager[ReconnectingVideoSocket]: ...


@typing.overload
def reconnecting(
    resource: AsyncAudioClient, *, request_options: typing.Optional[RequestOptions] = None
) -> typing.AsyncContextManager[AsyncReconnectingAudioSocket]: ...


@typing.overload
def reconnecting(
    resource: AsyncVideoClient, *, request_options: typing.Optional[RequestOptions] = None
) -> typing.AsyncContextManager[AsyncReconnectingVideoSocket]: ...


def reconnecting(
    resource: typing.Union[AudioClient, VideoClient, AsyncAudioClient, AsyncVideoClient],
    *,
    request_options: typing.Optional[RequestOptions] = None,
) -> typing.Union[
    typing.ContextManager[ReconnectingAudioSocket],
    typing.ContextManager[ReconnectingVideoSocket],
    typing.AsyncContextManager[AsyncReconnectingAudioSocket],
    typing.AsyncContextManager[AsyncReconnectingVideoSocket],
]:
    """
    Connects to a realtime endpoint with a socket that reconnects when the server restarts or the
    connection drops, and sends each new session the last `session.update`. Use it in place of
    `connect()`, with `with` for an `ExpressionMeasurementClient` and `async with` for an
    `AsyncExpressionMeasurementClient`. The socket has the same send methods, `recv()` and
    iteration as the socket that `connect()` yields.

    It reconnects after a `session.closed` with reason `server_shutdown`, which the server sends
    when it shuts down, as during a deployment; after the connection closes with code 1001, 1011,
    1012 or 1013, or ends without a close frame; and after an audio `internal_error`. Any other
    close ends the socket, and so does any close after you send `session.close`. Receiving is what
    notices a close and reconnects, so keep iterating or calling `recv()` while the session runs.

    A new session does not continue the previous one. Every message is passed on, so you receive
    the previous session's `session.closed`, if it sent one, then `session.created` again, with a
    new session ID, which is also a new run ID. IDs and times in the new session's results start
    again from 0. Results for media the previous session had received but not yet measured never
    arrive. The new session receives your last `session.update` before anything else.

    The first attempt waits 1 to 5 seconds, and each further attempt 1.3 times as long as the one
    before, up to 10 seconds. An attempt that fails with a network error, a timeout, or a 408, 429
    or 5xx status is retried, up to 30 attempts in a row. The count starts again once a connection
    has stayed open for 5 seconds. The first connection is not retried, since a wrong API key or
    URL would fail the same way again.

    While the socket reconnects, sending raises `websockets.exceptions.ConnectionClosed`. Media and
    `session.update` sent then have no effect, and `session.close` stops the reconnect. A live
    source can skip frames until sending succeeds again. A recording loses whatever the previous
    session had not measured, so rather than skip frames, let the error end the session, then send
    the recording again on a new connection or upload it. Leaving the `with` or `async with` block
    also stops a reconnect that is under way. With the sync client, send from other threads while
    one thread receives.

    Parameters
    ----------
    resource : typing.Union[AudioClient, VideoClient, AsyncAudioClient, AsyncVideoClient]
        `client.audio` or `client.video` of an `ExpressionMeasurementClient` or an
        `AsyncExpressionMeasurementClient`.

    request_options : typing.Optional[RequestOptions]
        Request-specific configuration, used for every connection.

    Returns
    -------
    typing.Union[typing.ContextManager, typing.AsyncContextManager]
        Connects on entry and yields a `ReconnectingAudioSocket` or a `ReconnectingVideoSocket`
        for the sync client, and an `AsyncReconnectingAudioSocket` or an
        `AsyncReconnectingVideoSocket` for the async client.

    Raises
    ------
    TypeError
        resource is neither `client.audio` nor `client.video`.

    Entering raises what `connect()` raises when the first connection fails. Once the session has
    ended for good, iteration ends after a normal close and otherwise raises
    `websockets.exceptions.ConnectionClosed`, as the socket from `connect()` does, and `recv()`
    raises it. When the server refuses a reconnect with a 4xx status other than 408 or 429, they
    raise the error that `connect()` raised for it at once. When the 30 attempts run out, they
    raise the last attempt's error, or, if that attempt connected but closed again within 5
    seconds, a `websockets.exceptions.ConnectionClosedError` for that close, even a normal one.

    Examples
    --------
    import asyncio

    from hume_expression_measurement import AsyncExpressionMeasurementClient, AudioSessionCreated
    from hume_expression_measurement.reconnect import reconnecting
    from websockets.exceptions import ConnectionClosed

    client = AsyncExpressionMeasurementClient(api_key="YOUR_API_KEY")
    async with reconnecting(client.audio) as socket:

        async def send(frames):
            async for frame in frames:
                try:
                    await socket.send_audio_frame(frame)
                except ConnectionClosed:
                    # Skipped while the socket reconnects.
                    continue

        sender = asyncio.create_task(send(microphone))
        async for message in socket:
            if isinstance(message, AudioSessionCreated):
                print("session", message.session_id)
    """
    if isinstance(resource, AudioClient):
        audio = resource
        return _connected(ReconnectingAudioSocket(lambda: audio.connect(request_options=request_options)))
    if isinstance(resource, VideoClient):
        video = resource
        return _connected(ReconnectingVideoSocket(lambda: video.connect(request_options=request_options)))
    if isinstance(resource, AsyncAudioClient):
        async_audio = resource
        return _async_connected(
            AsyncReconnectingAudioSocket(lambda: async_audio.connect(request_options=request_options))
        )
    if isinstance(resource, AsyncVideoClient):
        async_video = resource
        return _async_connected(
            AsyncReconnectingVideoSocket(lambda: async_video.connect(request_options=request_options))
        )
    raise TypeError(
        "reconnecting takes client.audio or client.video of an ExpressionMeasurementClient or an "
        "AsyncExpressionMeasurementClient."
    )
