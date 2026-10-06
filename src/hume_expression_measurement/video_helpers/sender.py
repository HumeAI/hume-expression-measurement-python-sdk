# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

import asyncio
import bisect
import collections
import collections.abc
import contextlib
import dataclasses
import itertools
import json
import math
import time
import typing
import weakref

from ..reconnect import AsyncReconnectingVideoSocket
from ..types.error import Error
from ..types.error_code import ErrorCode
from ..types.session_close import SessionClose
from ..types.video_measurement_result import VideoMeasurementResult
from ..types.video_session_closed import VideoSessionClosed
from ..video.socket_client import AsyncVideoSocketClient, VideoSocketClientResponse
from .frame import VideoFrame
from websockets.exceptions import ConnectionClosed

# A larger message ends the session. The limit is documented as 2 MB without saying whether that
# is 2,000,000 or 2,097,152 bytes, so the smaller is used.
_MAX_MESSAGE_BYTES = 2_000_000

# The documented send rate is 3 frames per second. Starting with 1 frame of allowance keeps the
# client from ever getting ahead of that rate, counted from when streaming starts. Up to 2 frames of
# allowance can build up, so that a live frame arriving early just after a late one is sent rather
# than dropped.
_STARTING_FRAMES = 1
_BURST_FRAMES = 2
_FRAMES_PER_SECOND = 3

# The slowest rate the client falls back to, so that a recorded frame waits at most 2 seconds
# before it is sent again.
_MIN_FRAMES_PER_SECOND = 0.5

# Every frame gets exactly one reply, in the order the frames were sent: a result or one of these
# errors. Other errors answer text messages or end the session.
_FRAME_ERRORS = frozenset({"invalid_image_frame", "rate_limited", "internal_error"})

# The close code for a connection that ended without a close frame.
_ABNORMAL_CLOSURE = 1006

_SESSION_CLOSE = SessionClose(type="session.close")

# Sockets that have served a stream. A session cannot be resumed, so a socket serves one stream.
_streamed_sockets: "weakref.WeakSet[AsyncVideoSocketClient]" = weakref.WeakSet()


@dataclasses.dataclass(frozen=True)
class VideoResultReply:
    """The measurements of one frame."""

    timestamp_ms: float
    """The frame's `timestamp_ms`."""

    result: VideoMeasurementResult
    """The `measurement.result` for the frame, listing up to 32 detected faces."""

    error: None = None
    """Always None, so that any reply's `error` can be checked."""


@dataclasses.dataclass(frozen=True)
class VideoErrorReply:
    """The error that rejected one frame."""

    timestamp_ms: float
    """The frame's `timestamp_ms`."""

    error: Error
    """Why the frame was not measured. `code` says what happened."""

    result: None = None
    """Always None, so that any reply's `result` can be checked."""


VideoReply = typing.Union[VideoResultReply, VideoErrorReply]
"""The reply to one frame: its measurements, or the error that rejected it."""


class SessionEndedError(Exception):
    """
    Raised by `stream_video` when the session ends before the frames do: because of a
    session-ending error, because the server shut down or closed an idle session, or because the
    connection closed without the session closing first. A session cannot be resumed; open a new
    connection to continue.
    """

    closed: typing.Optional[VideoSessionClosed]
    """The server's last message, with the reason the session ended and its totals. None when the connection closed without it."""

    code: typing.Optional[ErrorCode]
    """The code of the error that ended the session, when `closed.reason` is "error"."""

    def __init__(
        self,
        message: str,
        *,
        closed: typing.Optional[VideoSessionClosed] = None,
        code: typing.Optional[ErrorCode] = None,
    ) -> None:
        super().__init__(message)
        self.closed = closed
        self.code = code


def stream_video(
    socket: AsyncVideoSocketClient, frames: typing.AsyncIterable[VideoFrame], *, live: bool = False
) -> "VideoStream":
    """
    Streams frames to a realtime video session and yields the reply to each frame, paired with that
    frame's `timestamp_ms`.

    Frames are sent no faster than the session's send rate allows. When the server rejects a frame
    with `rate_limited`, the rate is halved, down to one frame every 2 seconds, and it climbs back
    by one frame per second for each second of frames the server accepts. The rejected frame is
    dropped from a live source and sent again from a recording. A recorded frame that fails with a
    retryable `internal_error` is sent once more. A frame over the 2 MB message limit would end the
    session, so it is not sent and is yielded as a `message_too_large` error.

    Replies are yielded in the order of the frames. Once the frames run out and every frame has its
    reply, the session is closed. `closed` then holds the session's totals, and the server closes
    the connection.

    The stream reads every message from the socket while it runs. Send any `session.update` before
    streaming, because the configuration locks at the first frame. If iteration ends before the
    session does, such as by leaving the loop early or an error from `frames`, the session ends and
    `frames` is stopped. A loop left early cleans up once the stream's iterator is garbage
    collected, and if the connection has closed by then, the closed connection is what ends the
    session. Wrap `aiter(stream)` in `contextlib.aclosing` to clean up as soon as the block exits.

    Parameters
    ----------
    socket : AsyncVideoSocketClient
        A socket from `client.video.connect()` on an `AsyncExpressionMeasurementClient`. A socket
        serves one stream, because a session cannot be resumed.

    frames : typing.AsyncIterable[VideoFrame]
        The frames to send, such as from `iter_video_frames`.

    live : bool
        Whether the frames come from a live source, such as a camera, rather than a recording. A
        live frame that the send rate has no room for is dropped, so that results keep up with the
        source. A recorded frame waits and is sent again, so that every frame is measured.

    Returns
    -------
    VideoStream
        The replies, to iterate once with `async for`.

    Raises
    ------
    TypeError
        frames is not an async iterable, or socket is from `reconnecting()`.

    RuntimeError
        The socket has already served a stream.

    SessionEndedError
        From iteration, if the session ends before the frames do, or the connection closes without
        the session closing first.

    Errors from `frames` are raised from iteration as they are. A source from `iter_video_frames`
    starts ffmpeg on the first frame request, so its errors arrive after the session has started.

    Examples
    --------
    from hume_expression_measurement import AsyncExpressionMeasurementClient
    from hume_expression_measurement.video_helpers import iter_video_frames, stream_video

    client = AsyncExpressionMeasurementClient(api_key="YOUR_API_KEY")
    async with client.video.connect() as socket:
        async for reply in stream_video(socket, iter_video_frames("interview.mp4")):
            if reply.result is not None:
                print(reply.timestamp_ms, reply.result.faces)
            else:
                print(reply.timestamp_ms, reply.error.code)
    """
    if not isinstance(frames, collections.abc.AsyncIterable):
        raise TypeError("frames must be an async iterable of frames, such as from iter_video_frames.")
    # Type checkers reject this socket, but code that is not type checked can still pass it. A
    # reconnect would start a new session partway through the stream, whose replies would be
    # paired with frames the previous session never answered.
    if isinstance(socket, AsyncReconnectingVideoSocket):
        raise TypeError(
            "stream_video does not reconnect. Pass a socket from `client.video.connect()` rather than from `reconnecting()`."
        )
    if socket in _streamed_sockets:
        raise RuntimeError(
            "This socket has already served a stream. Open a new one with `client.video.connect()` for each stream."
        )
    _streamed_sockets.add(socket)
    return VideoStream._start(socket, frames, live)


@dataclasses.dataclass
class _Entry:
    # Position in the input, counting only frames that are sent or rejected before sending.
    index: int
    frame: VideoFrame
    # Which send this was, counting from 1, or 0 before the frame is sent.
    send_number: int = 0
    resent: bool = False


class VideoStream:
    """The replies to a stream of frames, from `stream_video`. It can be iterated once."""

    _socket: AsyncVideoSocketClient
    _frames: typing.AsyncIterable[VideoFrame]
    _live: bool
    _iterated: bool
    _dropped: int
    _closed: typing.Optional[VideoSessionClosed]

    def __init__(self) -> None:
        raise TypeError("Start a VideoStream with `stream_video(socket, frames)`.")

    @classmethod
    def _start(
        cls, socket: AsyncVideoSocketClient, frames: typing.AsyncIterable[VideoFrame], live: bool
    ) -> "VideoStream":
        stream = cls.__new__(cls)
        stream._socket = socket
        stream._frames = frames
        stream._live = live
        stream._iterated = False
        stream._dropped = 0
        stream._closed = None
        return stream

    @property
    def dropped_frames(self) -> int:
        """Live frames that were not sent because the send rate had no room for them."""
        return self._dropped

    @property
    def closed(self) -> typing.Optional[VideoSessionClosed]:
        """The server's last message, with why the session ended and its totals. Set once the session has ended."""
        return self._closed

    def __aiter__(self) -> typing.AsyncGenerator[VideoReply, None]:
        if self._iterated:
            raise RuntimeError("A video stream can be iterated only once.")
        self._iterated = True
        # The stream keeps no reference to the generator, so a loop left early releases it, and
        # garbage collection closes it, which ends the session.
        return self._run()

    async def _run(self) -> typing.AsyncGenerator[VideoReply, None]:
        socket = self._socket
        live = self._live
        frames = self._frames.__aiter__()
        wake = asyncio.Event()
        messages: typing.Deque[VideoSocketClientResponse] = collections.deque()

        async def read() -> ConnectionClosed:
            try:
                while True:
                    try:
                        messages.append(await socket.recv())
                    except json.JSONDecodeError:
                        # Every reply is JSON, so other text answers no frame. Iterating the
                        # socket skips it too.
                        continue
                    wake.set()
            except ConnectionClosed as closed:
                return closed

        reader = asyncio.ensure_future(read())
        reader.add_done_callback(lambda _: wake.set())
        pull: typing.Optional["asyncio.Future[typing.Optional[VideoFrame]]"] = None
        frames_done = False

        allowance = _Allowance()
        indexes = itertools.count()
        replies: typing.Dict[int, VideoReply] = {}
        # Recorded frames waiting to be sent, in input order.
        waiting: typing.List[_Entry] = []
        in_flight: typing.Deque[_Entry] = collections.deque()
        next_reply = 0
        close_sent = False
        last_error: typing.Optional[Error] = None

        async def send(entry: _Entry, send_number: int) -> None:
            entry.send_number = send_number
            try:
                await socket.send_image_frame(entry.frame.data)
            except ConnectionClosed:
                # The reader ends with the same close once it has read the messages that arrived
                # before it, which can include the session's end. A send to a closed connection
                # fails without yielding to the event loop, so the reader must be awaited.
                await asyncio.wait([reader])
                return
            in_flight.append(entry)

        async def accept(frame: VideoFrame) -> None:
            if len(frame.data) > _MAX_MESSAGE_BYTES:
                replies[next(indexes)] = VideoErrorReply(timestamp_ms=frame.timestamp_ms, error=_too_large(frame))
            elif not live:
                waiting.append(_Entry(index=next(indexes), frame=frame))
            else:
                send_number = allowance.take(time.monotonic())
                if send_number is None:
                    self._dropped += 1
                else:
                    await send(_Entry(index=next(indexes), frame=frame), send_number)

        def handle(message: VideoSocketClientResponse) -> None:
            nonlocal last_error
            if isinstance(message, VideoSessionClosed):
                self._closed = message
            elif isinstance(message, VideoMeasurementResult):
                if in_flight:
                    entry = in_flight.popleft()
                    replies[entry.index] = VideoResultReply(timestamp_ms=entry.frame.timestamp_ms, result=message)
                    allowance.speed_up(entry.send_number, time.monotonic())
            elif isinstance(message, Error):
                last_error = message
                if message.code in _FRAME_ERRORS and in_flight:
                    handle_frame_error(in_flight.popleft(), message)

        def handle_frame_error(entry: _Entry, error: Error) -> None:
            limited = error.code == "rate_limited"
            if limited:
                allowance.slow_down(entry.send_number, time.monotonic())
            if not live and limited:
                send_later(entry)
            elif not live and error.code == "internal_error" and error.retryable and not entry.resent:
                entry.resent = True
                send_later(entry)
            else:
                replies[entry.index] = VideoErrorReply(timestamp_ms=entry.frame.timestamp_ms, error=error)

        def send_later(entry: _Entry) -> None:
            bisect.insort(waiting, entry, key=lambda waiting_entry: waiting_entry.index)

        try:
            while True:
                wake.clear()
                while messages:
                    handle(messages.popleft())

                reply = replies.pop(next_reply, None)
                if reply is not None:
                    next_reply += 1
                    yield reply
                    continue

                if self._closed is not None:
                    if close_sent and self._closed.reason == "client_request":
                        return
                    raise _session_ended(self._closed, last_error)
                if reader.done():
                    closed = reader.result()
                    raise _connection_ended(closed) from closed
                if pull is not None and pull.done():
                    frame = pull.result()
                    pull = None
                    if frame is None:
                        frames_done = True
                    else:
                        await accept(frame)
                    continue

                delay: typing.Optional[float] = None
                if waiting:
                    now = time.monotonic()
                    delay = allowance.delay(now)
                    send_number = allowance.take(now) if delay <= 0 else None
                    if send_number is not None:
                        await send(waiting.pop(0), send_number)
                        continue
                elif not frames_done and pull is None:
                    pull = asyncio.ensure_future(_next_frame(frames))
                    pull.add_done_callback(lambda _: wake.set())
                elif frames_done and not in_flight and not close_sent:
                    close_sent = True
                    try:
                        await socket.send_video_session_close(_SESSION_CLOSE)
                    except ConnectionClosed:
                        await asyncio.wait([reader])
                    continue

                await _wait(wake, delay)
        finally:
            connection_open = not reader.done()
            reader.cancel()
            if connection_open and self._closed is None and not close_sent:
                # The server answers by delivering the results still in progress, sending
                # session.closed and closing the connection.
                with contextlib.suppress(ConnectionClosed):
                    await socket.send_video_session_close(_SESSION_CLOSE)
            await _stop(frames, pull, frames_done)
            await asyncio.wait([reader])
            if not reader.cancelled():
                # Retrieved so that asyncio does not report a failure the stream had no need to raise.
                reader.exception()


# Paces frames at the documented send rate, so that frames are not sent only to be rejected. It
# adapts to the rate the server accepts: it halves on each rejection and climbs back slowly while
# frames are accepted, so a server whose limit is lower than documented rejects a few frames rather
# than most of them.
class _Allowance:
    def __init__(self) -> None:
        self._rate: float = _FRAMES_PER_SECOND
        self._frames: float = _STARTING_FRAMES
        # A monotonic clock, so that a change to the system clock cannot stall sending.
        self._updated_at = time.monotonic()
        self._sends = 0
        # The number of sends before the last slowdown. Replies to those frames say nothing about
        # the rate since then. Send numbers are used rather than times, which can tie.
        self._slowed_after = 0

    # Returns the seconds until a frame can be sent.
    def delay(self, now: float) -> float:
        self._refill(now)
        if self._frames >= 1:
            return 0
        # Rounded up to a whole millisecond, so that the allowance is whole when the wait ends.
        return math.ceil((1 - self._frames) * 1000 / self._rate) / 1000

    # Returns the send's number, counting from 1, or None when there is no room for it.
    def take(self, now: float) -> typing.Optional[int]:
        self._refill(now)
        if self._frames < 1:
            return None
        self._frames -= 1
        self._sends += 1
        return self._sends

    # A rejection means the server's allowance was empty when the frame arrived.
    def slow_down(self, send_number: int, now: float) -> None:
        if send_number <= self._slowed_after:
            return
        self._rate = max(_MIN_FRAMES_PER_SECOND, self._rate / 2)
        self._frames = 0
        self._updated_at = now
        self._slowed_after = self._sends

    # Adds one frame per second for each second of accepted frames.
    def speed_up(self, send_number: int, now: float) -> None:
        if send_number <= self._slowed_after or self._rate >= _FRAMES_PER_SECOND:
            return
        self._refill(now)
        self._rate = min(_FRAMES_PER_SECOND, self._rate + 1 / self._rate)

    def _refill(self, now: float) -> None:
        # Once slowed, the client allows a burst of at most one second, since the server's burst
        # may be as small as its rate.
        slowed = self._slowed_after > 0
        capacity = min(_BURST_FRAMES, max(1, self._rate)) if slowed else _BURST_FRAMES
        self._frames = min(capacity, self._frames + (now - self._updated_at) * self._rate)
        self._updated_at = now


async def _next_frame(frames: typing.AsyncIterator[VideoFrame]) -> typing.Optional[VideoFrame]:
    try:
        return await frames.__anext__()
    except StopAsyncIteration:
        return None


async def _wait(wake: asyncio.Event, delay: typing.Optional[float]) -> None:
    timer = None if delay is None else asyncio.get_running_loop().call_later(delay, wake.set)
    try:
        await wake.wait()
    finally:
        if timer is not None:
            timer.cancel()


async def _stop(
    frames: typing.AsyncIterator[VideoFrame],
    pull: typing.Optional["asyncio.Future[typing.Optional[VideoFrame]]"],
    frames_done: bool,
) -> None:
    if pull is not None and not pull.done():
        # Cancelled rather than awaited, because a live source can wait indefinitely for its next
        # frame, and an async generator cannot be closed while a request for one is pending.
        pull.cancel()
        await asyncio.wait([pull])
    failed = pull is not None and not pull.cancelled() and pull.exception() is not None
    aclose = getattr(frames, "aclose", None)
    if frames_done or failed or aclose is None:
        return
    # The stream is already ending, so an error from the source's cleanup has nowhere useful to go.
    with contextlib.suppress(Exception):
        await aclose()


def _too_large(frame: VideoFrame) -> Error:
    return Error(
        type="error",
        code="message_too_large",
        message=(
            f"The frame is {len(frame.data)} bytes, over the {_MAX_MESSAGE_BYTES}-byte message limit, and was "
            "not sent, because sending it would end the session."
        ),
        retryable=False,
    )


def _session_ended(closed: VideoSessionClosed, last_error: typing.Optional[Error]) -> SessionEndedError:
    if closed.reason == "error" and last_error is not None:
        return SessionEndedError(
            f"The session ended with error {last_error.code}: {last_error.message}", closed=closed, code=last_error.code
        )
    return SessionEndedError(f"The session ended before the frames did, with reason {closed.reason}.", closed=closed)


def _connection_ended(closed: ConnectionClosed) -> SessionEndedError:
    cause = closed.__cause__
    # An EOFError says only that the connection ended without a close frame, which code 1006 says
    # too. A CancelledError says only that the client failed the connection itself, as it does
    # when a keepalive ping goes unanswered or a write fails, and the close frame it sent, if any,
    # says why.
    if cause is not None and not isinstance(cause, (EOFError, asyncio.CancelledError)):
        return SessionEndedError(f"The connection failed before the session ended: {cause}")
    close = closed.rcvd if closed.rcvd is not None else closed.sent
    code = _ABNORMAL_CLOSURE if close is None else close.code
    reason = "" if close is None else close.reason
    return SessionEndedError(
        f"The connection closed with code {code} before the session ended{f': {reason}' if reason else '.'}"
    )
