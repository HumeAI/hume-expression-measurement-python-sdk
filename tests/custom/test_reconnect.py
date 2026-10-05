# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

import asyncio
import json
import socket as _socket
import threading
import time
import typing

import pytest
from websockets.sync.server import ServerConnection, serve

from hume_expression_measurement import (
    AsyncExpressionMeasurementClient,
    AudioSessionUpdate,
    ExpressionMeasurementClient,
    FaceConfigUpdate,
    VideoSessionUpdate,
)
from hume_expression_measurement.core.api_error import ApiError
from hume_expression_measurement.environment import ExpressionMeasurementClientEnvironment
from hume_expression_measurement.reconnect import (
    Reconnected,
    Reconnecting,
    SessionEnded,
    connect_audio,
    connect_video,
)
from hume_expression_measurement.reconnect._policy import Backoff, Ending, delay_for, followup_for

UPDATE = AudioSessionUpdate(type="session.update", measurement_timer_ms=5000)


class Connection:
    def __init__(self, websocket: ServerConnection, session_id: str) -> None:
        self.websocket = websocket
        self.session_id = session_id
        self.received: typing.List[typing.Any] = []

    # Closing waits for the client's close frame, so it runs in a thread rather than blocking the
    # test's event loop, which has to answer it.
    def end(self, reason: str, code: int) -> None:
        self.websocket.send(
            json.dumps(
                {
                    "type": "session.closed",
                    "session_id": self.session_id,
                    "reason": reason,
                    "received": {"audio_duration_ms": 0, "frames": 0},
                    "produced": {"utterances": 0, "measurements": 0},
                }
            )
        )
        self.close(code)

    def close(self, code: int) -> None:
        threading.Thread(target=self.websocket.close, args=(code,), daemon=True).start()

    def drop(self) -> None:
        self.websocket.socket.shutdown(_socket.SHUT_RDWR)


class FakeServer:
    """
    Plays the server's side of realtime sessions in a thread: `session.created` on connect,
    `session.updated` for each `session.update`, and `session.closed` then 1000 for `session.close`
    unless `answer_close` is off. Handshakes are rejected with the statuses in `rejections`, one per
    attempt, or always with `reject_all`.
    """

    def __init__(self) -> None:
        self.connections: typing.List[Connection] = []
        self.handshakes = 0
        self.rejections: typing.List[int] = []
        self.reject_all: typing.Optional[int] = None
        self.retry_after: typing.Optional[str] = None
        self.answer_close = True
        self._server = serve(self._handle, "127.0.0.1", 0, process_request=self._process_request)
        self.url = f"ws://127.0.0.1:{self._server.socket.getsockname()[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def latest(self) -> Connection:
        return self.connections[-1]

    def close(self) -> None:
        self._server.shutdown()

    def _process_request(self, connection: ServerConnection, request: typing.Any) -> typing.Any:
        self.handshakes += 1
        status = (
            self.reject_all if self.reject_all is not None else (self.rejections.pop(0) if self.rejections else None)
        )
        if status is None:
            return None
        response = connection.respond(status, "refused\n")
        if self.retry_after is not None:
            response.headers["Retry-After"] = self.retry_after
        return response

    def _handle(self, websocket: ServerConnection) -> None:
        connection = Connection(websocket, f"session-{len(self.connections) + 1}")
        self.connections.append(connection)
        if "/video/" in (websocket.request.path if websocket.request else ""):
            config: typing.Dict[str, typing.Any] = {
                "image": {"type": "jpeg"},
                "face": {"threshold": 0.5, "min_size": 0},
            }
        else:
            config = {
                "audio": {"type": "raw", "encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                "measurement_timer_ms": 3000,
            }
        websocket.send(json.dumps({"type": "session.created", "session_id": connection.session_id, **config}))
        for data in websocket:
            if isinstance(data, bytes):
                connection.received.append(data[0])
                continue
            message = json.loads(data)
            connection.received.append(message)
            if message["type"] == "session.update":
                websocket.send(json.dumps({"type": "session.updated", "session_id": connection.session_id, **config}))
            elif message["type"] == "session.close" and self.answer_close:
                connection.end("client_request", 1000)


@pytest.fixture
def server() -> typing.Iterator[FakeServer]:
    fake = FakeServer()
    yield fake
    fake.close()


def until(condition: typing.Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError
        time.sleep(0.005)


async def until_async(condition: typing.Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError
        await asyncio.sleep(0.005)


def environment(server: FakeServer) -> ExpressionMeasurementClientEnvironment:
    return ExpressionMeasurementClientEnvironment(base=server.url.replace("ws:", "http:"), production=server.url)


def async_client(server: FakeServer) -> AsyncExpressionMeasurementClient:
    return AsyncExpressionMeasurementClient(api_key="test-key", environment=environment(server))


class Recorder:
    """Iterates a session in the background and records what it yields."""

    def __init__(self, session: typing.Any) -> None:
        self.items: typing.List[typing.Any] = []
        self.task = asyncio.ensure_future(self._run(session))

    async def _run(self, session: typing.Any) -> None:
        async for item in session:
            self.items.append(item)

    def of(self, kind: type) -> typing.List[typing.Any]:
        return [item for item in self.items if isinstance(item, kind)]

    def types(self) -> typing.List[str]:
        return [
            type(item).__name__ if isinstance(item, (Reconnecting, Reconnected)) else item.type for item in self.items
        ]


async def test_1012_opens_a_new_session_and_resends_the_last_session_update(server: FakeServer) -> None:
    async with connect_audio(async_client(server).audio, session_update=UPDATE, initial_delay=0.04) as session:
        recorder = Recorder(session)
        await session.send_audio_frame(bytes([1, 0]))
        await until_async(lambda: len(server.latest.received) == 2)

        server.latest.end("server_shutdown", 1012)
        await until_async(lambda: len(recorder.of(Reconnected)) == 1)
        await session.send_audio_frame(bytes([2, 0]))
        await until_async(lambda: len(server.connections[1].received) == 2)

        assert server.connections[1].received == [{"type": "session.update", "measurement_timer_ms": 5000}, 2]
        [reconnecting] = recorder.of(Reconnecting)
        assert (reconnecting.code, reconnecting.attempt, reconnecting.http_status) == (1012, 1, None)
        assert reconnecting.delay < 0.25
        assert recorder.of(Reconnected) == [
            Reconnected(previous_session_id="session-1", session_id="session-2", code=1012, attempts=1)
        ]
        assert session.session_id == "session-2"
        await until_async(lambda: len(recorder.items) == 7)
        assert recorder.types() == [
            "session.created",
            "session.updated",
            "session.closed",
            "Reconnecting",
            "Reconnected",
            "session.created",
            "session.updated",
        ]
    assert session.ended == SessionEnded(code=1000, gave_up=False)


async def test_reconnects_after_server_shutdown_closed_with_1000(server: FakeServer) -> None:
    async with connect_audio(async_client(server).audio, initial_delay=0.04) as session:
        recorder = Recorder(session)
        server.latest.end("server_shutdown", 1000)
        await until_async(lambda: len(recorder.of(Reconnected)) == 1)
        assert recorder.of(Reconnected)[0].code == 1000


async def test_reconnects_when_the_connection_drops_without_a_close_frame(server: FakeServer) -> None:
    async with connect_audio(async_client(server).audio, session_update=UPDATE, initial_delay=0.04) as session:
        recorder = Recorder(session)
        server.latest.drop()
        await until_async(lambda: len(server.connections) == 2 and len(server.connections[1].received) == 1)
        assert recorder.of(Reconnecting)[0].code == 1006
        assert server.connections[1].received == [{"type": "session.update", "measurement_timer_ms": 5000}]


async def test_backs_off_on_1013_and_on_a_503_handshake(server: FakeServer) -> None:
    async with connect_audio(async_client(server).audio, initial_delay=0.04, max_delay=1.0) as session:
        recorder = Recorder(session)
        server.rejections = [503, 503]
        server.latest.close(1013)
        await until_async(lambda: len(recorder.of(Reconnected)) == 1)

        reconnecting = recorder.of(Reconnecting)
        assert [(r.code, r.http_status, r.attempt) for r in reconnecting] == [
            (1013, None, 1),
            (1006, 503, 2),
            (1006, 503, 3),
        ]
        for index, event in enumerate(reconnecting):
            assert 0.02 * 2**index <= event.delay <= 0.04 * 2**index
        assert recorder.of(Reconnected)[0].attempts == 3


async def test_waits_for_retry_after(server: FakeServer) -> None:
    async with connect_audio(async_client(server).audio, initial_delay=0.04) as session:
        recorder = Recorder(session)
        server.rejections = [503]
        server.retry_after = "1"
        server.latest.close(1013)
        await until_async(lambda: len(recorder.of(Reconnected)) == 1)
        assert recorder.of(Reconnecting)[1].delay >= 1.0


async def test_gives_up_after_max_attempts(server: FakeServer) -> None:
    async with connect_audio(async_client(server).audio, initial_delay=0.01, max_attempts=2) as session:
        recorder = Recorder(session)
        server.reject_all = 503
        server.latest.close(1012)
        await asyncio.wait_for(recorder.task, 5)
        assert server.handshakes == 3
        assert session.ended == SessionEnded(code=1006, gave_up=True, http_status=503)


@pytest.mark.parametrize("reason, code", [("idle_timeout", 1000), ("error", 1008), (None, 4001)])
async def test_does_not_reconnect_after(server: FakeServer, reason: typing.Optional[str], code: int) -> None:
    async with connect_audio(async_client(server).audio, initial_delay=0.01) as session:
        recorder = Recorder(session)
        if reason is None:
            server.latest.close(code)
        else:
            server.latest.end(reason, code)
        await asyncio.wait_for(recorder.task, 5)
        await asyncio.sleep(0.1)
        assert len(server.connections) == 1
        assert recorder.of(Reconnecting) == []
        assert session.ended == SessionEnded(code=code, gave_up=False, session_closed_reason=reason)


async def test_does_not_reconnect_after_session_close(server: FakeServer) -> None:
    async with connect_audio(async_client(server).audio, initial_delay=0.01) as session:
        recorder = Recorder(session)
        server.answer_close = False
        await session.send_session_close()
        await until_async(lambda: len(server.latest.received) == 1)
        server.latest.end("server_shutdown", 1012)
        await asyncio.wait_for(recorder.task, 5)
        assert len(server.connections) == 1
        assert session.ended is not None and session.ended.code == 1012


async def test_raises_without_retrying_when_the_api_key_is_refused(server: FakeServer) -> None:
    server.reject_all = 401
    with pytest.raises(ApiError) as raised:
        async with connect_audio(async_client(server).audio, initial_delay=0.01):
            pass
    assert raised.value.status_code == 401
    assert server.handshakes == 1


async def test_queues_audio_sent_while_reconnecting(server: FakeServer) -> None:
    async with connect_audio(async_client(server).audio, session_update=UPDATE, initial_delay=0.2) as session:
        recorder = Recorder(session)
        server.latest.close(1013)
        await until_async(lambda: len(recorder.of(Reconnecting)) == 1)

        assert session.reconnecting
        await session.send_audio_frame(bytes([7, 0]))
        await session.send_audio_frame(bytes([8, 0]))
        await until_async(lambda: len(server.connections) == 2 and len(server.connections[1].received) == 3)
        assert server.connections[1].received == [{"type": "session.update", "measurement_timer_ms": 5000}, 7, 8]
        assert not session.reconnecting


async def test_video_resends_the_last_session_update(server: FakeServer) -> None:
    async with connect_video(async_client(server).video, initial_delay=0.04) as session:
        recorder = Recorder(session)
        await session.send_session_update(
            VideoSessionUpdate(type="session.update", face=FaceConfigUpdate(threshold=0.8))
        )
        server.latest.end("server_shutdown", 1012)
        await until_async(lambda: len(server.connections) == 2 and len(server.connections[1].received) == 1)
        assert server.connections[1].received == [{"type": "session.update", "face": {"threshold": 0.8}}]
        assert recorder.of(Reconnected)[0].session_id == "session-2"


def test_sync_client_reconnects_and_resends_the_last_session_update(server: FakeServer) -> None:
    client = ExpressionMeasurementClient(api_key="test-key", environment=environment(server))
    items: typing.List[typing.Any] = []
    with connect_audio(client.audio, session_update=UPDATE, initial_delay=0.04) as session:
        session.send_audio_frame(bytes([1, 0]))
        until(lambda: len(server.latest.received) == 2)
        threading.Timer(0.05, lambda: server.latest.end("server_shutdown", 1012)).start()
        for item in session:
            items.append(item)
            if isinstance(item, Reconnected):
                session.send_audio_frame(bytes([2, 0]))
                session.send_session_close()
    assert server.connections[1].received == [
        {"type": "session.update", "measurement_timer_ms": 5000},
        2,
        {"type": "session.close"},
    ]
    assert [item for item in items if isinstance(item, Reconnected)] == [
        Reconnected(previous_session_id="session-1", session_id="session-2", code=1012, attempts=1)
    ]
    assert session.ended is not None and session.ended.session_closed_reason == "client_request"


@pytest.mark.parametrize(
    "code, followup",
    [
        (1012, "immediate"),
        (1006, "immediate"),
        (1001, "immediate"),
        (1013, "backoff"),
        (1011, "backoff"),
        (1014, "backoff"),
        (1000, "stop"),
        (1008, "stop"),
        (1009, "stop"),
        (4000, "stop"),
    ],
)
def test_followup_for_close_codes(code: int, followup: str) -> None:
    assert followup_for(Ending(code=code, opened=True, client_closing=False)) == followup


@pytest.mark.parametrize(
    "status, followup", [(503, "backoff"), (502, "backoff"), (429, "backoff"), (401, "stop"), (403, "stop")]
)
def test_followup_for_handshake_statuses(status: int, followup: str) -> None:
    assert followup_for(Ending(code=1006, opened=False, client_closing=False, http_status=status)) == followup


def test_delays() -> None:
    backoff = Backoff()
    assert delay_for(1, "immediate", backoff, lambda: 0.999) < 0.25
    assert delay_for(1, "backoff", backoff, lambda: 0) == 0.5
    assert delay_for(3, "backoff", backoff, lambda: 0) == 2.0
    assert delay_for(10, "backoff", backoff, lambda: 1) == 30.0
