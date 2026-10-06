import asyncio
import collections
import contextlib
import json
import time
import typing

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed, ConnectionClosedError, InvalidStatus
from websockets.http11 import Request, Response

from hume_expression_measurement import (
    AsyncExpressionMeasurementClient,
    AudioSessionClosed,
    AudioSessionCreated,
    AudioSessionUpdate,
    ExpressionMeasurementClient,
    ExpressionMeasurementClientEnvironment,
    FaceConfigUpdate,
    SessionClose,
    VideoSessionCreated,
    VideoSessionUpdate,
)
from hume_expression_measurement.core import ApiError, RequestOptions
from hume_expression_measurement.reconnect import (
    ReconnectingAudioSocket,
    ReconnectingVideoSocket,
    _policy,
    reconnecting,
)

# Every wait in these tests is bounded, so that a regression fails rather than hangs CI.
_TIMEOUT = 10

_Endpoint = typing.Literal["audio", "video"]
_Kind = typing.Literal["async", "sync"]
_Message = typing.Dict[str, typing.Any]

_UPDATE = AudioSessionUpdate(type="session.update", measurement_timer_ms=500)
_UPDATE_ON_WIRE: _Message = {"type": "session.update", "measurement_timer_ms": 500}
_CLOSE = SessionClose(type="session.close")
_FRAME = b"\x01\x00"


# Runs each test against the async client and the sync one.
@pytest.fixture(params=["async", "sync"])
def kind(request: pytest.FixtureRequest) -> _Kind:
    return typing.cast(_Kind, request.param)


@pytest.fixture(autouse=True)
def _short_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real delays are at least a second. Tests of the delays set their own.
    monkeypatch.setattr(_policy, "_FIRST_DELAY_RANGE", (0.01, 0.01))
    monkeypatch.setattr(_policy, "_MAX_DELAY", 0.01)


def _session_created(endpoint: _Endpoint, session_id: str) -> _Message:
    if endpoint == "audio":
        audio = {"type": "audio/pcm", "encoding": "s16le", "sample_rate": 16000, "channels": 1}
        return {"type": "session.created", "session_id": session_id, "audio": audio, "measurement_timer_ms": 3000}
    face = {"threshold": 0.5, "min_size": 32}
    return {"type": "session.created", "session_id": session_id, "image": {"type": "image/jpeg"}, "face": face}


def _session_closed(endpoint: _Endpoint, session_id: str, reason: str) -> _Message:
    audio = endpoint == "audio"
    return {
        "type": "session.closed",
        "session_id": session_id,
        "reason": reason,
        "received": {"audio_duration_ms": 0, "frames": 0} if audio else {"frames": 0},
        "produced": {"utterances": 0, "measurements": 0} if audio else {"measurements": 0},
    }


# The server's side of one session. Records what the client sends: text parsed as JSON, and binary
# messages as bytes.
class _FakeSession:
    def __init__(self, connection: ServerConnection, endpoint: _Endpoint, session_id: str) -> None:
        self.connection = connection
        self.received: typing.List[typing.Union[_Message, bytes]] = []
        self._endpoint = endpoint
        self._session_id = session_id

    async def close(self, code: int) -> None:
        await self.connection.close(code)

    def drop(self) -> None:
        self.connection.transport.abort()

    async def end_with_reason(self, reason: str, code: int = 1000) -> None:
        await self.connection.send(json.dumps(_session_closed(self._endpoint, self._session_id, reason)))
        await self.connection.close(code)

    # Ends the session the way the server does after a session-ending error.
    async def end_with_error(self, code: str) -> None:
        error = {"type": "error", "code": code, "message": f"{code} message", "retryable": False}
        await self.connection.send(json.dumps(error))
        await self.end_with_reason("error", 1008)

    # Text that is not valid UTF-8, which makes the client fail the connection.
    async def send_invalid_text(self) -> None:
        await self.connection.send(b"\xff", text=True)


# Plays the server's side of realtime sessions, one per connection, each opening with
# session.created. Handshakes are refused with the statuses passed to `refuse_next`, in order.
class _FakeServer:
    def __init__(self, endpoint: _Endpoint) -> None:
        self.requests: typing.List[Request] = []
        self.request_times: typing.List[float] = []
        self.sessions: typing.List[_FakeSession] = []
        self._endpoint = endpoint
        self._refusals: typing.Deque[int] = collections.deque()
        self._release: typing.Optional[asyncio.Event] = None

    def refuse_next(self, *statuses: int) -> None:
        self._refusals.extend(statuses)

    # Holds every handshake from now on until the returned event is set.
    def hold_handshakes(self) -> asyncio.Event:
        self._release = asyncio.Event()
        return self._release

    async def process_request(self, connection: ServerConnection, request: Request) -> typing.Optional[Response]:
        self.requests.append(request)
        self.request_times.append(time.monotonic())
        if self._release is not None:
            await self._release.wait()
        if self._refusals:
            return connection.respond(self._refusals.popleft(), "Refused by the test.\n")
        return None

    async def handle(self, connection: ServerConnection) -> None:
        session_id = f"session-{len(self.sessions) + 1}"
        session = _FakeSession(connection, self._endpoint, session_id)
        self.sessions.append(session)
        with contextlib.suppress(ConnectionClosed):
            await connection.send(json.dumps(_session_created(self._endpoint, session_id)))
            async for data in connection:
                session.received.append(data if isinstance(data, bytes) else json.loads(data))


@contextlib.asynccontextmanager
async def _serving(server: _FakeServer, port: int = 0) -> typing.AsyncIterator[int]:
    async with serve(server.handle, "127.0.0.1", port, process_request=server.process_request) as listener:
        yield listener.sockets[0].getsockname()[1]


def _client(kind: _Kind, port: int) -> typing.Union[AsyncExpressionMeasurementClient, ExpressionMeasurementClient]:
    environment = ExpressionMeasurementClientEnvironment(base="http://127.0.0.1", production=f"ws://127.0.0.1:{port}")
    if kind == "async":
        return AsyncExpressionMeasurementClient(api_key="test-key", environment=environment)
    return ExpressionMeasurementClient(api_key="test-key", environment=environment)


# The interface of the async sockets, which the sync ones are given below.
class _AudioSocket(typing.Protocol):
    async def send_audio_session_update(self, message: AudioSessionUpdate) -> None: ...

    async def send_audio_session_close(self, message: SessionClose) -> None: ...

    async def send_audio_frame(self, message: bytes) -> None: ...

    async def recv(self) -> object: ...

    def __aiter__(self) -> typing.AsyncIterator[object]: ...


class _VideoSocket(typing.Protocol):
    async def send_video_session_update(self, message: VideoSessionUpdate) -> None: ...

    def __aiter__(self) -> typing.AsyncIterator[object]: ...


_SyncSocketT = typing.TypeVar("_SyncSocketT", bound=typing.Union[ReconnectingAudioSocket, ReconnectingVideoSocket])


# Gives a sync socket the async socket's interface by making each blocking call in a thread, so
# that the fake server on the test's event loop keeps serving. Iterating it receives on one thread
# while sends run on others, as a sync app does.
class _InThreads(typing.Generic[_SyncSocketT]):
    def __init__(self, socket: _SyncSocketT) -> None:
        self._socket = socket

    async def recv(self) -> object:
        return await asyncio.to_thread(self._socket.recv)

    async def __aiter__(self) -> typing.AsyncIterator[object]:
        messages = iter(self._socket)
        done = object()
        while True:
            message = await asyncio.to_thread(next, messages, done)
            if message is done:
                return
            yield message


class _AudioInThreads(_InThreads[ReconnectingAudioSocket]):
    async def send_audio_session_update(self, message: AudioSessionUpdate) -> None:
        await asyncio.to_thread(self._socket.send_audio_session_update, message)

    async def send_audio_session_close(self, message: SessionClose) -> None:
        await asyncio.to_thread(self._socket.send_audio_session_close, message)

    async def send_audio_frame(self, message: bytes) -> None:
        await asyncio.to_thread(self._socket.send_audio_frame, message)


class _VideoInThreads(_InThreads[ReconnectingVideoSocket]):
    async def send_video_session_update(self, message: VideoSessionUpdate) -> None:
        await asyncio.to_thread(self._socket.send_video_session_update, message)


_T = typing.TypeVar("_T")


@contextlib.asynccontextmanager
async def _entered_in_thread(manager: typing.ContextManager[_T]) -> typing.AsyncIterator[_T]:
    entered = await asyncio.to_thread(manager.__enter__)
    try:
        yield entered
    finally:
        await asyncio.to_thread(manager.__exit__, None, None, None)


@contextlib.asynccontextmanager
async def _connect_audio(
    kind: _Kind, port: int, request_options: typing.Optional[RequestOptions] = None
) -> typing.AsyncIterator[_AudioSocket]:
    manager = reconnecting(_client(kind, port).audio, request_options=request_options)
    if kind == "async":
        async with manager as socket:
            yield socket
    else:
        async with _entered_in_thread(manager) as socket:
            yield _AudioInThreads(socket)


@contextlib.asynccontextmanager
async def _connect_video(kind: _Kind, port: int) -> typing.AsyncIterator[_VideoSocket]:
    manager = reconnecting(_client(kind, port).video)
    if kind == "async":
        async with manager as socket:
            yield socket
    else:
        async with _entered_in_thread(manager) as socket:
            yield _VideoInThreads(socket)


# The async client raises an ApiError for a refused handshake, and the sync client the websockets
# error, and a reconnecting socket raises what its client raises.
def _refused_status(kind: _Kind, error: typing.Optional[BaseException]) -> int:
    if kind == "async":
        assert isinstance(error, ApiError), error
        assert error.status_code is not None
        return error.status_code
    assert isinstance(error, InvalidStatus), error
    return error.response.status_code


# Iterates a socket in the background, as an app's receive loop does, since receiving is what
# notices a close and reconnects.
class _Reader:
    def __init__(self, socket: typing.AsyncIterable[object]) -> None:
        self.messages: typing.List[object] = []
        self._task = asyncio.ensure_future(self._read(socket))

    async def _read(self, socket: typing.AsyncIterable[object]) -> None:
        async for message in socket:
            self.messages.append(message)

    @property
    def done(self) -> bool:
        return self._task.done()

    @property
    def error(self) -> typing.Optional[BaseException]:
        return self._task.exception()

    def session_ids(self) -> typing.List[str]:
        return [
            message.session_id
            for message in self.messages
            if isinstance(message, (AudioSessionCreated, VideoSessionCreated))
        ]


async def _until(condition: typing.Callable[[], bool]) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), _TIMEOUT)


# Waits until iteration ends or the client connects once more than `requests`, so that a reconnect
# that should not happen fails the assertion rather than the wait.
async def _stopped(reader: _Reader, server: _FakeServer, requests: int) -> typing.Optional[BaseException]:
    await _until(lambda: reader.done or len(server.requests) > requests)
    assert len(server.requests) == requests
    return reader.error


def _close_code(error: typing.Optional[BaseException]) -> typing.Optional[int]:
    if error is None:
        return None
    assert isinstance(error, ConnectionClosedError), error
    assert error.rcvd is not None
    return error.rcvd.code


async def _open_with_update(socket: _AudioSocket, server: _FakeServer) -> _Reader:
    reader = _Reader(socket)
    await socket.send_audio_session_update(_UPDATE)
    await _until(lambda: len(server.sessions[0].received) == 1)
    return reader


_End = typing.Callable[[_FakeSession], typing.Awaitable[None]]


async def _drop(session: _FakeSession) -> None:
    session.drop()


@pytest.mark.parametrize(
    "end",
    [
        pytest.param(lambda session: session.close(1001), id="close code 1001"),
        pytest.param(lambda session: session.close(1011), id="close code 1011"),
        pytest.param(lambda session: session.close(1012), id="close code 1012"),
        pytest.param(lambda session: session.close(1013), id="close code 1013"),
        pytest.param(_drop, id="a dropped connection"),
        pytest.param(lambda session: session.send_invalid_text(), id="a connection the client fails"),
        pytest.param(
            lambda session: session.end_with_reason("server_shutdown"),
            id="session.closed with reason server_shutdown and close code 1000",
        ),
        pytest.param(
            lambda session: session.end_with_reason("server_shutdown", 1008),
            id="session.closed with reason server_shutdown and close code 1008",
        ),
        pytest.param(lambda session: session.end_with_error("internal_error"), id="an internal error"),
    ],
)
async def test_reconnects_and_sends_the_session_update_again_after(kind: _Kind, end: _End) -> None:
    server = _FakeServer("audio")
    async with _serving(server) as port, _connect_audio(kind, port) as socket:
        reader = await _open_with_update(socket, server)

        await end(server.sessions[0])

        await _until(lambda: len(server.sessions) == 2 and len(server.sessions[1].received) == 1)
        assert server.sessions[1].received == [_UPDATE_ON_WIRE]
        await _until(lambda: reader.session_ids() == ["session-1", "session-2"])


@pytest.mark.parametrize(
    ("end", "code"),
    [
        pytest.param(lambda session: session.close(1000), None, id="close code 1000"),
        pytest.param(
            lambda session: session.end_with_reason("idle_timeout"), None, id="session.closed with reason idle_timeout"
        ),
        pytest.param(
            lambda session: session.end_with_error("config_invalid"),
            1008,
            id="a session-ending error other than an internal error",
        ),
        pytest.param(lambda session: session.close(1008), 1008, id="close code 1008 with no internal error"),
        pytest.param(lambda session: session.close(4000), 4000, id="close code 4000"),
    ],
)
async def test_does_not_reconnect_and_ends_as_a_socket_from_connect_does_after(
    kind: _Kind, end: _End, code: typing.Optional[int]
) -> None:
    server = _FakeServer("audio")
    async with _serving(server) as port, _connect_audio(kind, port) as socket:
        reader = await _open_with_update(socket, server)

        await end(server.sessions[0])

        assert _close_code(await _stopped(reader, server, requests=1)) == code


async def test_passes_on_the_previous_sessions_session_closed_and_then_the_new_sessions_session_created(
    kind: _Kind,
) -> None:
    server = _FakeServer("audio")
    async with _serving(server) as port, _connect_audio(kind, port) as socket:
        reader = _Reader(socket)
        await _until(lambda: reader.session_ids() == ["session-1"])

        await server.sessions[0].end_with_reason("server_shutdown")

        await _until(lambda: reader.session_ids() == ["session-1", "session-2"])
        closed = reader.messages[1]
        assert isinstance(closed, AudioSessionClosed)
        assert closed.reason == "server_shutdown"
        assert len(reader.messages) == 3


async def test_recv_reconnects_too(kind: _Kind) -> None:
    server = _FakeServer("audio")
    async with _serving(server) as port, _connect_audio(kind, port) as socket:
        first = await asyncio.wait_for(socket.recv(), _TIMEOUT)

        await server.sessions[0].close(1012)

        second = await asyncio.wait_for(socket.recv(), _TIMEOUT)
        assert isinstance(first, AudioSessionCreated)
        assert isinstance(second, AudioSessionCreated)
        assert [first.session_id, second.session_id] == ["session-1", "session-2"]


# A sync receive cannot be cancelled, so this covers the async client only.
async def test_a_receive_that_times_out_leaves_the_reconnect_running_for_the_next_receive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Longer than each receive waits, so that receives time out while the reconnect waits.
    monkeypatch.setattr(_policy, "_FIRST_DELAY_RANGE", (0.1, 0.1))
    monkeypatch.setattr(_policy, "_MAX_DELAY", 0.1)
    server = _FakeServer("audio")
    async with _serving(server) as port, _connect_audio("async", port) as socket:
        assert isinstance(await asyncio.wait_for(socket.recv(), _TIMEOUT), AudioSessionCreated)
        release = server.hold_handshakes()
        await server.sessions[0].close(1012)

        async def receive_with_timeouts() -> object:
            while True:
                with contextlib.suppress(asyncio.TimeoutError):
                    return await asyncio.wait_for(socket.recv(), 0.02)

        receiving = asyncio.ensure_future(receive_with_timeouts())
        await _until(lambda: receiving.done() or len(server.requests) == 2)
        assert len(server.requests) == 2
        # Receives also time out while the handshake is held.
        await asyncio.sleep(0.1)
        release.set()

        message = await asyncio.wait_for(receiving, _TIMEOUT)
        assert isinstance(message, AudioSessionCreated)
        assert message.session_id == "session-2"
        assert len(server.requests) == 2


async def test_iteration_skips_messages_it_cannot_parse_as_the_socket_from_connect_does(kind: _Kind) -> None:
    server = _FakeServer("audio")
    async with _serving(server) as port, _connect_audio(kind, port) as socket:
        reader = _Reader(socket)
        await _until(lambda: reader.session_ids() == ["session-1"])

        await server.sessions[0].connection.send("not JSON")
        await server.sessions[0].connection.send(json.dumps({"type": "a.future.message"}))
        await server.sessions[0].end_with_reason("idle_timeout")

        assert await _stopped(reader, server, requests=1) is None
        assert [type(message) for message in reader.messages] == [AudioSessionCreated, AudioSessionClosed]


async def test_a_new_session_receives_the_last_session_update_before_anything_sent_after_the_reconnect(
    kind: _Kind,
) -> None:
    server = _FakeServer("audio")
    async with _serving(server) as port, _connect_audio(kind, port) as socket:
        reader = _Reader(socket)
        await socket.send_audio_session_update(AudioSessionUpdate(type="session.update", measurement_timer_ms=250))
        await socket.send_audio_session_update(_UPDATE)
        await _until(lambda: len(server.sessions[0].received) == 2)

        await server.sessions[0].close(1012)
        await _until(lambda: "session-2" in reader.session_ids())
        await socket.send_audio_frame(_FRAME)

        await _until(lambda: len(server.sessions[1].received) == 2)
        assert server.sessions[1].received == [_UPDATE_ON_WIRE, _FRAME]


async def test_sending_while_the_socket_reconnects_raises_and_has_no_effect(
    kind: _Kind, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Long enough for the test to send before the reconnect.
    monkeypatch.setattr(_policy, "_FIRST_DELAY_RANGE", (0.5, 0.5))
    monkeypatch.setattr(_policy, "_MAX_DELAY", 0.5)
    server = _FakeServer("audio")
    async with _serving(server) as port, _connect_audio(kind, port) as socket:
        reader = await _open_with_update(socket, server)

        await server.sessions[0].close(1012)
        with pytest.raises(ConnectionClosed):
            await socket.send_audio_frame(_FRAME)
        with pytest.raises(ConnectionClosed):
            await socket.send_audio_session_update(AudioSessionUpdate(type="session.update", measurement_timer_ms=250))
        assert len(server.requests) == 1

        await _until(lambda: "session-2" in reader.session_ids())
        await socket.send_audio_frame(_FRAME)
        await _until(lambda: len(server.sessions[1].received) == 2)
        assert server.sessions[1].received == [_UPDATE_ON_WIRE, _FRAME]


async def test_reconnects_a_video_session_and_sends_its_session_update_again(kind: _Kind) -> None:
    server = _FakeServer("video")
    async with _serving(server) as port, _connect_video(kind, port) as socket:
        reader = _Reader(socket)
        await socket.send_video_session_update(
            VideoSessionUpdate(type="session.update", face=FaceConfigUpdate(threshold=0.5))
        )
        await _until(lambda: len(server.sessions[0].received) == 1)

        await server.sessions[0].close(1012)

        await _until(lambda: len(server.sessions) == 2 and len(server.sessions[1].received) == 1)
        assert server.sessions[1].received == [{"type": "session.update", "face": {"threshold": 0.5}}]
        await _until(lambda: reader.session_ids() == ["session-1", "session-2"])


async def test_does_not_reconnect_a_video_session_that_ends_after_internal_errors(kind: _Kind) -> None:
    server = _FakeServer("video")
    async with _serving(server) as port, _connect_video(kind, port) as socket:
        reader = _Reader(socket)
        await _until(lambda: reader.session_ids() == ["session-1"])

        await server.sessions[0].end_with_error("internal_error")

        assert _close_code(await _stopped(reader, server, requests=1)) == 1008


@pytest.mark.parametrize(
    ("end", "next_code"),
    [
        pytest.param(lambda session: session.end_with_reason("server_shutdown"), 1000, id="reason server_shutdown"),
        pytest.param(lambda session: session.end_with_error("internal_error"), 1008, id="internal error"),
    ],
)
async def test_does_not_carry_a_sessions_ending_over_to_the_next_session(
    kind: _Kind, end: _End, next_code: int
) -> None:
    server = _FakeServer("audio")
    async with _serving(server) as port, _connect_audio(kind, port) as socket:
        reader = _Reader(socket)
        await _until(lambda: reader.session_ids() == ["session-1"])
        await end(server.sessions[0])
        await _until(lambda: reader.session_ids() == ["session-1", "session-2"])

        await server.sessions[1].close(next_code)

        await _stopped(reader, server, requests=2)


@pytest.mark.parametrize(
    "end",
    [
        pytest.param(lambda session: session.end_with_reason("server_shutdown"), id="a server shutdown"),
        pytest.param(lambda session: session.close(1012), id="a restart"),
    ],
)
async def test_does_not_reconnect_after_the_caller_sends_session_close_and_then(kind: _Kind, end: _End) -> None:
    server = _FakeServer("audio")
    async with _serving(server) as port, _connect_audio(kind, port) as socket:
        reader = _Reader(socket)
        await socket.send_audio_session_close(_CLOSE)
        await _until(lambda: len(server.sessions[0].received) == 1)

        await end(server.sessions[0])

        await _stopped(reader, server, requests=1)


async def test_reconnects_after_the_client_fails_the_connection_unless_the_caller_sent_session_close(
    kind: _Kind,
) -> None:
    server = _FakeServer("audio")
    async with _serving(server) as port, _connect_audio(kind, port) as socket:
        reader = _Reader(socket)
        await _until(lambda: reader.session_ids() == ["session-1"])
        await server.sessions[0].send_invalid_text()
        await _until(lambda: reader.session_ids() == ["session-1", "session-2"])

        await socket.send_audio_session_close(_CLOSE)
        await _until(lambda: len(server.sessions[1].received) == 1)
        await server.sessions[1].send_invalid_text()

        await _stopped(reader, server, requests=2)


async def test_does_not_reconnect_after_the_caller_leaves_the_context(kind: _Kind) -> None:
    server = _FakeServer("audio")
    async with _serving(server) as port:
        async with _connect_audio(kind, port) as socket:
            reader = _Reader(socket)
            await _until(lambda: reader.session_ids() == ["session-1"])

        assert await _stopped(reader, server, requests=1) is None


async def test_leaving_the_context_stops_a_reconnect_that_is_waiting(
    kind: _Kind, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A wait longer than the test would sit out.
    monkeypatch.setattr(_policy, "_FIRST_DELAY_RANGE", (_TIMEOUT * 2, _TIMEOUT * 2))
    monkeypatch.setattr(_policy, "_MAX_DELAY", _TIMEOUT * 2)
    server = _FakeServer("audio")
    async with _serving(server) as port:
        async with _connect_audio(kind, port) as socket:
            reader = _Reader(socket)
            await _until(lambda: reader.session_ids() == ["session-1"])
            await server.sessions[0].close(1012)
            # Lets the reader notice the close and start waiting.
            await asyncio.sleep(0.1)

        assert _close_code(await _stopped(reader, server, requests=1)) == 1012


async def test_a_connection_that_opens_after_the_caller_leaves_the_context_is_closed(kind: _Kind) -> None:
    server = _FakeServer("audio")
    async with _serving(server) as port:
        async with _connect_audio(kind, port) as socket:
            reader = _Reader(socket)
            await _until(lambda: reader.session_ids() == ["session-1"])
            release = server.hold_handshakes()
            await server.sessions[0].close(1012)
            await _until(lambda: len(server.requests) == 2)
        release.set()

        await _until(lambda: len(server.sessions) == 2)
        await asyncio.wait_for(server.sessions[1].connection.wait_closed(), _TIMEOUT)
        assert _close_code(await _stopped(reader, server, requests=2)) == 1012


async def test_sending_session_close_stops_a_reconnect_that_is_waiting(
    kind: _Kind, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A wait longer than the test would sit out.
    monkeypatch.setattr(_policy, "_FIRST_DELAY_RANGE", (_TIMEOUT * 2, _TIMEOUT * 2))
    monkeypatch.setattr(_policy, "_MAX_DELAY", _TIMEOUT * 2)
    server = _FakeServer("audio")
    async with _serving(server) as port, _connect_audio(kind, port) as socket:
        reader = _Reader(socket)
        await _until(lambda: reader.session_ids() == ["session-1"])
        await server.sessions[0].close(1012)
        # Lets the reader notice the close and start waiting.
        await asyncio.sleep(0.1)

        with pytest.raises(ConnectionClosed):
            await socket.send_audio_session_close(_CLOSE)

        assert _close_code(await _stopped(reader, server, requests=1)) == 1012


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_stops_when_the_server_refuses_a_reconnect_with(kind: _Kind, status: int) -> None:
    server = _FakeServer("audio")
    async with _serving(server) as port, _connect_audio(kind, port) as socket:
        reader = _Reader(socket)
        await _until(lambda: reader.session_ids() == ["session-1"])
        server.refuse_next(status)

        await server.sessions[0].close(1012)

        assert _refused_status(kind, await _stopped(reader, server, requests=2)) == status


@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_retries_a_reconnect_the_server_refuses_with(kind: _Kind, status: int) -> None:
    server = _FakeServer("audio")
    async with _serving(server) as port, _connect_audio(kind, port) as socket:
        await _open_with_update(socket, server)
        server.refuse_next(status)

        await server.sessions[0].close(1012)

        await _until(lambda: len(server.sessions) == 2 and len(server.sessions[1].received) == 1)
        assert server.sessions[1].received == [_UPDATE_ON_WIRE]
        assert len(server.requests) == 3


async def test_keeps_reconnecting_while_the_server_restarts(kind: _Kind) -> None:
    server = _FakeServer("audio")
    async with contextlib.AsyncExitStack() as first_server:
        port = await first_server.enter_async_context(_serving(server))
        async with _connect_audio(kind, port) as socket:
            await _open_with_update(socket, server)

            await first_server.aclose()
            # Attempts made while nothing listens on the port fail to connect.
            await asyncio.sleep(0.05)
            async with _serving(server, port):
                await _until(lambda: len(server.sessions) == 2 and len(server.sessions[1].received) == 1)
                assert server.sessions[1].received == [_UPDATE_ON_WIRE]


@pytest.mark.parametrize("status", [401, 503])
async def test_does_not_retry_a_first_connection_the_server_refuses_with(kind: _Kind, status: int) -> None:
    server = _FakeServer("audio")
    server.refuse_next(status)
    async with _serving(server) as port:
        with pytest.raises((ApiError, InvalidStatus)) as raised:
            async with _connect_audio(kind, port):
                pass

    assert _refused_status(kind, raised.value) == status
    assert len(server.requests) == 1


async def test_does_not_retry_a_first_connection_that_cannot_reach_the_server(kind: _Kind) -> None:
    async with _serving(_FakeServer("audio")) as port:
        pass

    with pytest.raises(OSError):
        async with _connect_audio(kind, port):
            pass


async def test_gives_up_after_30_attempts_in_a_row_and_raises_the_last_error(kind: _Kind) -> None:
    server = _FakeServer("audio")
    async with _serving(server) as port, _connect_audio(kind, port) as socket:
        reader = _Reader(socket)
        await _until(lambda: reader.session_ids() == ["session-1"])
        # A 31st attempt would succeed.
        server.refuse_next(*[503] * 30)

        await server.sessions[0].close(1012)

        assert _refused_status(kind, await _stopped(reader, server, requests=31)) == 503


@pytest.mark.parametrize(
    ("uptime", "reconnects"),
    [pytest.param(0.3, True, id="stayed open"), pytest.param(0, False, id="closed at once")],
)
async def test_the_attempt_count_starts_again_only_once_a_connection_has_stayed_open(
    kind: _Kind, monkeypatch: pytest.MonkeyPatch, uptime: float, reconnects: bool
) -> None:
    monkeypatch.setattr(_policy, "_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(_policy, "_MIN_UPTIME", 0.2)
    server = _FakeServer("audio")
    async with _serving(server) as port, _connect_audio(kind, port) as socket:
        reader = _Reader(socket)
        await _until(lambda: reader.session_ids() == ["session-1"])
        await server.sessions[0].close(1012)
        await _until(lambda: reader.session_ids() == ["session-1", "session-2"])
        await asyncio.sleep(uptime)

        await server.sessions[1].close(1012)

        if reconnects:
            await _until(lambda: reader.session_ids() == ["session-1", "session-2", "session-3"])
        else:
            assert _close_code(await _stopped(reader, server, requests=2)) == 1012


@pytest.mark.parametrize(
    ("end", "code"),
    [
        pytest.param(
            lambda session: session.end_with_reason("server_shutdown"),
            1000,
            id="session.closed with reason server_shutdown and close code 1000",
        ),
        pytest.param(lambda session: session.close(1001), 1001, id="close code 1001"),
    ],
)
async def test_raises_when_the_attempts_run_out_even_if_the_last_close_was_normal(
    kind: _Kind, monkeypatch: pytest.MonkeyPatch, end: _End, code: int
) -> None:
    monkeypatch.setattr(_policy, "_MAX_ATTEMPTS", 1)
    server = _FakeServer("audio")
    async with _serving(server) as port, _connect_audio(kind, port) as socket:
        reader = _Reader(socket)
        await _until(lambda: reader.session_ids() == ["session-1"])
        await end(server.sessions[0])
        await _until(lambda: reader.session_ids() == ["session-1", "session-2"])

        await end(server.sessions[1])

        assert _close_code(await _stopped(reader, server, requests=2)) == code


async def test_each_attempt_waits_1_3_times_as_long_as_the_one_before_up_to_the_maximum(
    kind: _Kind,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_policy, "_FIRST_DELAY_RANGE", (0.2, 0.2))
    monkeypatch.setattr(_policy, "_MAX_DELAY", 0.3)
    server = _FakeServer("audio")
    async with _serving(server) as port, _connect_audio(kind, port) as socket:
        reader = _Reader(socket)
        await _until(lambda: reader.session_ids() == ["session-1"])
        server.refuse_next(503, 503, 503)

        await server.sessions[0].close(1012)
        closed_at = time.monotonic()

        await _until(lambda: len(server.sessions) == 2)
        attempt_times = server.request_times[1:]
        waits = [later - earlier for earlier, later in zip([closed_at, *attempt_times], attempt_times)]
        for wait, expected in zip(waits, [0.2, 0.26, 0.3, 0.3]):
            assert expected <= wait < expected + 0.15


async def test_every_connection_carries_the_request_options(kind: _Kind) -> None:
    server = _FakeServer("audio")
    request_options: RequestOptions = {"additional_headers": {"X-Test": "reconnect"}}
    async with _serving(server) as port, _connect_audio(kind, port, request_options) as socket:
        reader = _Reader(socket)
        await _until(lambda: reader.session_ids() == ["session-1"])

        await server.sessions[0].close(1012)

        await _until(lambda: reader.session_ids() == ["session-1", "session-2"])
        assert [request.headers["X-Test"] for request in server.requests] == ["reconnect", "reconnect"]


def test_a_resource_other_than_audio_or_video_raises_type_error(kind: _Kind) -> None:
    with pytest.raises(TypeError):
        reconnecting(_client(kind, 1).runs)
