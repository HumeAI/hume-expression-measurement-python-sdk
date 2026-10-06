import asyncio
import contextlib
import json
import struct
import time
import typing

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from hume_expression_measurement import AsyncExpressionMeasurementClient, ExpressionMeasurementClientEnvironment
from hume_expression_measurement.reconnect import reconnecting
from hume_expression_measurement.video.socket_client import AsyncVideoSocketClient
from hume_expression_measurement.video_helpers import (
    SessionEndedError,
    VideoFrame,
    VideoReply,
    VideoStream,
    stream_video,
)

# Every wait in these tests is bounded, so that a regression fails rather than hangs CI.
_TIMEOUT = 10

_Message = typing.Dict[str, typing.Any]
# Runs on the fake server with the connection that sent the frame, for replies other than messages.
_Action = typing.Callable[["_FakeServer", ServerConnection], typing.Awaitable[None]]
_Respond = typing.Callable[[int, int], typing.Sequence[typing.Union[_Message, _Action]]]

_RESULT: _Message = {"type": "measurement.result", "faces": []}


def _error(code: str, retryable: bool = True) -> _Message:
    return {"type": "error", "code": code, "message": f"{code} message", "retryable": retryable}


# Plays the server's side of a realtime video session. `respond` returns the messages to send, and
# actions to run, for each frame received, given the frame's first byte and how many times that
# frame has arrived. A `session.close` is answered with `session.closed` and a normal close.
class _FakeServer:
    def __init__(self, respond: _Respond) -> None:
        self.received: typing.List[int] = []
        self.arrivals: typing.List[float] = []
        self.close_codes: typing.List[typing.Optional[int]] = []
        self._respond = respond
        self._results = 0

    async def handle(self, connection: ServerConnection) -> None:
        try:
            async for data in connection:
                if isinstance(data, str):
                    if json.loads(data)["type"] == "session.close":
                        await self.close_session(connection, "client_request", 1000)
                    continue
                self.received.append(data[0])
                self.arrivals.append(time.monotonic())
                attempt = self.received.count(data[0])
                for reply in self._respond(data[0], attempt):
                    if callable(reply):
                        await reply(self, connection)
                        continue
                    if reply["type"] == "measurement.result":
                        reply = {**reply, "frame_id": self._results}
                        self._results += 1
                    await connection.send(json.dumps(reply))
        except ConnectionClosed:
            pass
        finally:
            await connection.wait_closed()
            self.close_codes.append(connection.close_code)

    async def close_session(self, connection: ServerConnection, reason: str, code: int) -> None:
        closed = {
            "type": "session.closed",
            "session_id": "session-1",
            "reason": reason,
            "received": {"frames": self._results},
            "produced": {"measurements": self._results},
        }
        await connection.send(json.dumps(closed))
        await connection.close(code)


def _closing_session(reason: str, code: int) -> _Action:
    async def action(server: _FakeServer, connection: ServerConnection) -> None:
        await server.close_session(connection, reason, code)

    return action


@contextlib.asynccontextmanager
async def _serving(
    respond: _Respond,
) -> typing.AsyncIterator[typing.Tuple[AsyncExpressionMeasurementClient, _FakeServer]]:
    server = _FakeServer(respond)
    # No size limit, so that the server receives the frames the client must not send.
    async with serve(server.handle, "127.0.0.1", 0, max_size=None) as listener:
        port = listener.sockets[0].getsockname()[1]
        environment = ExpressionMeasurementClientEnvironment(
            base="http://127.0.0.1", production=f"ws://127.0.0.1:{port}"
        )
        yield AsyncExpressionMeasurementClient(api_key="test-key", environment=environment), server


@contextlib.asynccontextmanager
async def _connect(respond: _Respond) -> typing.AsyncIterator[typing.Tuple[AsyncVideoSocketClient, _FakeServer]]:
    async with _serving(respond) as (client, server), client.video.connect() as socket:
        yield socket, server


# Each frame's first byte identifies it to the fake server, and its timestamp is 100 times that.
def _frame(frame_id: int, size: int = 1) -> VideoFrame:
    return VideoFrame(data=bytes([frame_id]) + bytes(size - 1), timestamp_ms=frame_id * 100)


async def _frames_of(*frames: VideoFrame) -> typing.AsyncIterator[VideoFrame]:
    for frame in frames:
        yield frame


async def _live_frames(count: int, interval: float) -> typing.AsyncIterator[VideoFrame]:
    for frame_id in range(count):
        yield _frame(frame_id)
        await asyncio.sleep(interval)


def _describe(reply: VideoReply) -> str:
    return f"{reply.timestamp_ms:g} {reply.error.code if reply.error is not None else 'result'}"


async def _collect(stream: VideoStream) -> typing.List[str]:
    async def collect() -> typing.List[str]:
        return [_describe(reply) async for reply in stream]

    return await asyncio.wait_for(collect(), _TIMEOUT)


async def _until(condition: typing.Callable[[], bool]) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), _TIMEOUT)


# Answers like a server whose send rate is lower than documented: a burst of `burst` frames, then
# `per_second` frames per second, rejecting the rest with `rate_limited`.
def _limited_to(per_second: float, burst: float) -> _Respond:
    allowance = burst
    updated_at = time.monotonic()

    def respond(frame_id: int, attempt: int) -> typing.List[_Message]:
        nonlocal allowance, updated_at
        now = time.monotonic()
        allowance = min(burst, allowance + (now - updated_at) * per_second)
        updated_at = now
        if allowance < 1:
            return [_error("rate_limited")]
        allowance -= 1
        return [_RESULT]

    return respond


# Answers no frame until frame `last` first arrives, then answers every frame so far, so that every
# frame is sent before any reply arrives. Otherwise the order of sends would vary between runs,
# because a recorded frame is read only when no rejected frame is waiting to be sent again.
def _held_until(last: int, respond: _Respond) -> _Respond:
    held: typing.List[typing.Union[_Message, _Action]] = []

    def held_respond(frame_id: int, attempt: int) -> typing.List[typing.Union[_Message, _Action]]:
        if attempt > 1:
            return list(respond(frame_id, attempt))
        held.extend(respond(frame_id, attempt))
        if frame_id < last:
            return []
        replies = list(held)
        held.clear()
        return replies

    return held_respond


async def test_each_reply_is_paired_with_its_frame_when_rejections_are_interleaved() -> None:
    replies: typing.Dict[int, typing.List[_Message]] = {
        1: [_error("invalid_image_frame")],
        # Answers a text message, so it consumes no frame.
        2: [_error("message_invalid"), _RESULT],
        3: [_error("internal_error", retryable=False)],
    }
    async with _connect(lambda frame_id, attempt: replies.get(frame_id, [_RESULT])) as (socket, _):
        frames = _frames_of(*(_frame(frame_id) for frame_id in range(5)))

        assert await _collect(stream_video(socket, frames, live=True)) == [
            "0 result",
            "100 invalid_image_frame",
            "200 result",
            "300 internal_error",
            "400 result",
        ]


async def test_the_session_is_closed_when_the_frames_run_out_and_its_totals_are_kept() -> None:
    async with _connect(lambda frame_id, attempt: [_RESULT]) as (socket, server):
        stream = stream_video(socket, _frames_of(_frame(0), _frame(1)))

        await _collect(stream)

        assert stream.closed is not None
        assert stream.closed.reason == "client_request"
        assert stream.closed.received.frames == 2
        assert stream.dropped_frames == 0
        await _until(lambda: server.close_codes == [1000])


async def test_a_live_frame_rejected_with_rate_limited_is_dropped() -> None:
    respond: _Respond = lambda frame_id, attempt: [_error("rate_limited")] if frame_id == 1 else [_RESULT]
    async with _connect(respond) as (socket, server):
        replies = await _collect(stream_video(socket, _frames_of(_frame(0), _frame(1), _frame(2)), live=True))

        assert replies == ["0 result", "100 rate_limited", "200 result"]
        assert server.received == [0, 1, 2]


async def test_rejected_recorded_frames_are_sent_again_after_a_wait_and_replies_keep_frame_order() -> None:
    respond: _Respond = lambda frame_id, attempt: (
        [_error("rate_limited")] if frame_id in (1, 2) and attempt == 1 else [_RESULT]
    )
    async with _connect(_held_until(3, respond)) as (socket, server):
        frames = _frames_of(*(_frame(frame_id) for frame_id in range(4)))

        replies = await _collect(stream_video(socket, frames))

        assert replies == ["0 result", "100 result", "200 result", "300 result"]
        assert server.received == [0, 1, 2, 3, 1, 2]
        # The first rejection halves the rate to 2 frames per second, so the first resend waits
        # one frame at that rate. The second frame was sent before the first rejection arrived,
        # so its rejection does not halve the rate again, which would double the wait.
        assert 0.45 <= server.arrivals[4] - server.arrivals[1] < 0.9


async def test_a_recording_slows_to_a_server_rate_lower_than_documented_rather_than_resending_most_frames() -> None:
    async with _connect(_limited_to(2, 2)) as (socket, server):
        frames = [_frame(frame_id) for frame_id in range(10)]

        replies = await _collect(stream_video(socket, _frames_of(*frames)))

        assert replies == [f"{frame.timestamp_ms:g} result" for frame in frames]
        # The first 7 frames go out before any reply, and the 5 beyond the server's burst are
        # rejected. After that, few frames are sent only to be rejected.
        assert len(server.received) <= 10 + 5 + 3


async def test_live_frames_are_dropped_before_sending_when_the_server_rate_is_lower_than_documented() -> None:
    async with _connect(_limited_to(2, 2)) as (socket, _):
        stream = stream_video(socket, _live_frames(60, 0.033), live=True)

        replies = await _collect(stream)

        assert len([reply for reply in replies if reply.endswith("rate_limited")]) < 8
        assert stream.dropped_frames > 45


async def test_a_recorded_frame_is_sent_once_more_after_a_retryable_internal_error_but_a_live_one_is_not() -> None:
    respond: _Respond = lambda frame_id, attempt: [_error("internal_error")] if frame_id == 1 else [_RESULT]

    async with _connect(_held_until(2, respond)) as (socket, server):
        recorded = await _collect(stream_video(socket, _frames_of(_frame(0), _frame(1), _frame(2))))
        recorded_frames = server.received
    async with _connect(_held_until(2, respond)) as (socket, server):
        live = await _collect(stream_video(socket, _frames_of(_frame(0), _frame(1), _frame(2)), live=True))
        live_frames = server.received

    assert recorded == ["0 result", "100 internal_error", "200 result"]
    assert recorded_frames == [0, 1, 2, 1]
    assert live == ["0 result", "100 internal_error", "200 result"]
    assert live_frames == [0, 1, 2]


async def test_a_frame_of_2_000_000_bytes_is_sent_but_a_larger_one_never_is_because_it_would_end_the_session() -> None:
    async with _connect(lambda frame_id, attempt: [_RESULT]) as (socket, server):
        frames = _frames_of(_frame(0, size=2_000_000), _frame(1, size=2_000_001), _frame(2))

        replies = await _collect(stream_video(socket, frames))

        assert replies == ["0 result", "100 message_too_large", "200 result"]
        assert server.received == [0, 2]


async def test_the_code_of_a_session_ending_error_is_raised_after_the_replies_before_it() -> None:
    respond: _Respond = lambda frame_id, attempt: (
        [_error("config_invalid", retryable=False), _closing_session("error", 1008)] if frame_id == 1 else [_RESULT]
    )
    async with _connect(respond) as (socket, _):
        replies: typing.List[str] = []

        async def collect() -> None:
            async for reply in stream_video(socket, _frames_of(_frame(0), _frame(1), _frame(2))):
                replies.append(_describe(reply))

        with pytest.raises(SessionEndedError) as raised:
            await asyncio.wait_for(collect(), _TIMEOUT)

        assert replies == ["0 result"]
        assert raised.value.code == "config_invalid"
        assert raised.value.closed is not None
        assert raised.value.closed.reason == "error"


@pytest.mark.parametrize("reason", ["idle_timeout", "server_shutdown"])
async def test_a_session_that_ends_before_the_frames_do_raises(reason: str) -> None:
    async def close_soon(server: _FakeServer, connection: ServerConnection) -> None:
        async def close() -> None:
            await asyncio.sleep(0.05)
            await server.close_session(connection, reason, 1000)

        asyncio.ensure_future(close())

    async def frames() -> typing.AsyncIterator[VideoFrame]:
        yield _frame(0)
        await asyncio.Event().wait()

    async with _connect(lambda frame_id, attempt: [_RESULT, close_soon]) as (socket, _):
        replies: typing.List[str] = []

        async def collect() -> None:
            async for reply in stream_video(socket, frames()):
                replies.append(_describe(reply))

        with pytest.raises(SessionEndedError) as raised:
            await asyncio.wait_for(collect(), _TIMEOUT)

        assert replies == ["0 result"]
        assert raised.value.code is None
        assert raised.value.closed is not None
        assert raised.value.closed.reason == reason


async def test_a_connection_that_drops_raises_with_its_close_code() -> None:
    async def drop(server: _FakeServer, connection: ServerConnection) -> None:
        await connection.close(1011)

    async with _connect(lambda frame_id, attempt: [drop] if frame_id == 1 else [_RESULT]) as (socket, _):
        replies: typing.List[str] = []

        async def collect() -> None:
            async for reply in stream_video(socket, _frames_of(_frame(0), _frame(1))):
                replies.append(_describe(reply))

        with pytest.raises(
            SessionEndedError, match="^The connection closed with code 1011 before the session ended.$"
        ) as raised:
            await asyncio.wait_for(collect(), _TIMEOUT)

        assert replies == ["0 result"]
        assert raised.value.closed is None


async def test_a_connection_that_ends_without_a_close_frame_raises_with_code_1006() -> None:
    async def hang_up(server: _FakeServer, connection: ServerConnection) -> None:
        connection.transport.close()

    async with _connect(lambda frame_id, attempt: [hang_up] if frame_id == 1 else [_RESULT]) as (socket, _):
        with pytest.raises(SessionEndedError, match="^The connection closed with code 1006 before the session ended.$"):
            await _collect(stream_video(socket, _frames_of(_frame(0), _frame(1))))


async def test_a_connection_error_raises_its_cause_rather_than_the_close_it_causes() -> None:
    async def send_invalid_text(server: _FakeServer, connection: ServerConnection) -> None:
        await connection.send(b"\xff", text=True)

    async with _connect(lambda frame_id, attempt: [send_invalid_text]) as (socket, _):
        with pytest.raises(
            SessionEndedError,
            match="^The connection failed before the session ended: 'utf-8' codec can't decode byte 0xff",
        ):
            await _collect(stream_video(socket, _frames_of(_frame(0))))


# Makes the call websockets makes when the server stops answering keepalive pings, which it would
# otherwise notice only after 20 to 40 seconds.
async def test_a_keepalive_timeout_raises_with_the_close_code_and_reason_the_client_sent() -> None:
    async with _connect(lambda frame_id, attempt: [_RESULT] if frame_id == 0 else []) as (socket, _):

        async def collect() -> None:
            async for _ in stream_video(socket, _frames_of(_frame(0), _frame(1))):
                socket._websocket.fail_connection(1011, "keepalive ping timeout")

        with pytest.raises(
            SessionEndedError,
            match="^The connection closed with code 1011 before the session ended: keepalive ping timeout$",
        ):
            await asyncio.wait_for(collect(), _TIMEOUT)


async def test_a_text_message_that_is_not_json_is_skipped() -> None:
    async def send_text(server: _FakeServer, connection: ServerConnection) -> None:
        await connection.send("not json")

    async with _connect(lambda frame_id, attempt: [send_text, _RESULT]) as (socket, _):
        stream = stream_video(socket, _frames_of(_frame(0), _frame(1)))

        assert await _collect(stream) == ["0 result", "100 result"]
        assert stream.closed is not None
        assert stream.closed.reason == "client_request"


# The server's close frame is written without closing, and the server stops reading, so it never
# answers the client's own close frame. The client is left closing until the connection drops,
# and frames it sends in that time wait for the close rather than fail with an error of their own.
async def test_sending_while_the_connection_is_closing_raises_session_ended_error() -> None:
    async def close_slowly(server: _FakeServer, connection: ServerConnection) -> None:
        transport = connection.transport
        transport.pause_reading()
        transport.write(b"\x88\x02" + struct.pack(">H", 1011))
        asyncio.get_running_loop().call_later(0.4, transport.abort)

    async with _connect(lambda frame_id, attempt: [close_slowly] if frame_id == 0 else []) as (socket, _):
        frames = _frames_of(*(_frame(frame_id) for frame_id in range(9)))

        with pytest.raises(SessionEndedError, match="code 1011"):
            await _collect(stream_video(socket, frames))


async def test_a_second_stream_on_a_socket_raises_at_once() -> None:
    async with _connect(lambda frame_id, attempt: [_RESULT]) as (socket, _):
        await _collect(stream_video(socket, _frames_of(_frame(0))))

        with pytest.raises(RuntimeError, match="This socket has already served a stream"):
            stream_video(socket, _frames_of(_frame(1)))


async def test_frames_that_are_not_an_async_iterable_raise_type_error() -> None:
    async with _connect(lambda frame_id, attempt: [_RESULT]) as (socket, _):
        with pytest.raises(TypeError, match="frames must be an async iterable"):
            stream_video(socket, typing.cast(typing.Any, [_frame(0)]))


async def test_a_socket_from_reconnecting_raises_type_error() -> None:
    async with _serving(lambda frame_id, attempt: [_RESULT]) as (client, _), reconnecting(client.video) as socket:
        with pytest.raises(TypeError, match="stream_video does not reconnect"):
            stream_video(typing.cast(typing.Any, socket), _frames_of(_frame(0)))


async def test_a_stream_can_be_iterated_only_once() -> None:
    async with _connect(lambda frame_id, attempt: [_RESULT]) as (socket, _):
        stream = stream_video(socket, _frames_of(_frame(0)))

        await _collect(stream)

        with pytest.raises(RuntimeError, match="A video stream can be iterated only once."):
            aiter(stream)


async def test_an_error_from_the_frames_is_raised_unchanged_and_the_session_is_closed() -> None:
    failure = OSError("The camera disconnected.")

    async def frames() -> typing.AsyncIterator[VideoFrame]:
        yield _frame(0)
        raise failure

    async with _connect(lambda frame_id, attempt: [_RESULT]) as (socket, server):
        with pytest.raises(OSError) as raised:
            await _collect(stream_video(socket, frames()))

        assert raised.value is failure
        await _until(lambda: server.close_codes == [1000])


# Runs on the real clock: the 10 frames are pulled and sent in under a millisecond, and an 8th
# frame needs 250 ms of allowance.
async def test_live_frames_that_arrive_with_no_send_allowance_left_are_dropped() -> None:
    async with _connect(lambda frame_id, attempt: [_RESULT]) as (socket, server):
        stream = stream_video(socket, _frames_of(*(_frame(frame_id) for frame_id in range(10))), live=True)

        replies = await _collect(stream)

        assert len(replies) == 7
        assert stream.dropped_frames == 3
        assert server.received == list(range(7))


async def test_the_first_7_recorded_frames_are_sent_at_once_and_the_rest_at_4_per_second() -> None:
    async with _connect(lambda frame_id, attempt: [_RESULT]) as (socket, server):
        started = time.monotonic()
        replies = await _collect(stream_video(socket, _frames_of(*(_frame(frame_id) for frame_id in range(9)))))

        assert len(replies) == 9
        assert server.arrivals[6] - server.arrivals[0] < 0.1
        # The 2 frames after the burst need 0.5 seconds of allowance. That time is measured from
        # before iteration, when the allowance starts, because a busy machine can delay the first
        # frame's arrival.
        assert server.arrivals[8] - started >= 0.49


async def test_frames_keep_being_paced_when_the_system_clock_is_set_back(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _connect(lambda frame_id, attempt: [_RESULT]) as (socket, _):
        replies: typing.List[VideoReply] = []
        set_back = time.time() - 10 * 60

        async def collect() -> None:
            async for reply in stream_video(socket, _frames_of(*(_frame(frame_id) for frame_id in range(9)))):
                replies.append(reply)
                if len(replies) == 1:
                    monkeypatch.setattr(time, "time", lambda: set_back)

        await asyncio.wait_for(collect(), _TIMEOUT)

        assert len(replies) == 9


async def test_leaving_the_loop_early_closes_the_session_and_stops_the_frames() -> None:
    stopped = False

    async def frames() -> typing.AsyncIterator[VideoFrame]:
        nonlocal stopped
        try:
            for frame_id in range(1_000_000):
                yield _frame(frame_id % 256)
                await asyncio.sleep(0.005)
        finally:
            stopped = True

    async with _connect(lambda frame_id, attempt: [_RESULT]) as (socket, server):
        async for _ in stream_video(socket, frames(), live=True):
            break

        await _until(lambda: server.close_codes == [1000])
        await _until(lambda: stopped)


async def test_leaving_the_loop_early_stops_frames_that_are_waiting_for_their_next_frame() -> None:
    stopped = asyncio.Event()

    async def frames() -> typing.AsyncIterator[VideoFrame]:
        try:
            yield _frame(0)
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async with _connect(lambda frame_id, attempt: [_RESULT]) as (socket, server):
        async for _ in stream_video(socket, frames(), live=True):
            break

        await asyncio.wait_for(stopped.wait(), _TIMEOUT)
        await _until(lambda: server.close_codes == [1000])


async def test_leaving_the_loop_early_closes_frames_the_caller_still_holds() -> None:
    stopped = asyncio.Event()

    async def recording() -> typing.AsyncIterator[VideoFrame]:
        try:
            for frame_id in range(20):
                yield _frame(frame_id)
        finally:
            stopped.set()

    # Held here, so that only the stream can close it.
    frames = recording()
    async with _connect(lambda frame_id, attempt: [_RESULT]) as (socket, server):
        async for _ in stream_video(socket, frames):
            break

        await asyncio.wait_for(stopped.wait(), _TIMEOUT)
        await _until(lambda: server.close_codes == [1000])


def test_constructing_a_stream_directly_points_to_stream_video() -> None:
    with pytest.raises(TypeError, match=r"stream_video\(socket, frames\)"):
        VideoStream()
