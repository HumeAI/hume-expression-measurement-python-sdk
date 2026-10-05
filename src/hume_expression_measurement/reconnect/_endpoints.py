# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

import typing

from ..audio.client import AsyncAudioClient, AudioClient
from ..audio.socket_client import AudioSocketClientResponse
from ..core.request_options import RequestOptions
from ..types.audio_session_update import AudioSessionUpdate
from ..types.session_close import SessionClose
from ..types.video_session_update import VideoSessionUpdate
from ..video.client import AsyncVideoClient, VideoClient
from ..video.socket_client import VideoSocketClientResponse
from ._policy import Backoff
from ._session import AsyncReconnectingSession, ReconnectingSession, _Driver

# The 30 seconds of audio the server accepts at once: 16 kHz, 2 bytes a sample.
MAX_QUEUED_AUDIO_BYTES = 30 * 16_000 * 2
# The 8 frames the server accepts at once.
MAX_QUEUED_FRAMES = 8

_CLOSE = SessionClose(type="session.close")


class AsyncReconnectingAudioSession(AsyncReconnectingSession[AudioSocketClientResponse, AudioSessionUpdate]):
    async def send_audio_frame(self, frame: bytes) -> None:
        """Sends PCM audio, or queues it while reconnecting. The queue holds 30 seconds of audio and drops the oldest beyond that."""
        await self._send_media(frame)


class ReconnectingAudioSession(ReconnectingSession[AudioSocketClientResponse, AudioSessionUpdate]):
    def send_audio_frame(self, frame: bytes) -> None:
        """Sends PCM audio, or queues it while reconnecting. The queue holds 30 seconds of audio and drops the oldest beyond that."""
        self._send_media(frame)


class AsyncReconnectingVideoSession(AsyncReconnectingSession[VideoSocketClientResponse, VideoSessionUpdate]):
    async def send_image_frame(self, frame: bytes) -> None:
        """Sends a JPEG frame, or queues it while reconnecting. The queue holds 8 frames and drops the oldest beyond that."""
        await self._send_media(frame)


class ReconnectingVideoSession(ReconnectingSession[VideoSocketClientResponse, VideoSessionUpdate]):
    def send_image_frame(self, frame: bytes) -> None:
        """Sends a JPEG frame, or queues it while reconnecting. The queue holds 8 frames and drops the oldest beyond that."""
        self._send_media(frame)


@typing.overload
def connect_audio(
    audio: AsyncAudioClient,
    *,
    session_update: typing.Optional[AudioSessionUpdate] = None,
    max_attempts: int = 6,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    request_options: typing.Optional[RequestOptions] = None,
) -> AsyncReconnectingAudioSession: ...


@typing.overload
def connect_audio(
    audio: AudioClient,
    *,
    session_update: typing.Optional[AudioSessionUpdate] = None,
    max_attempts: int = 6,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    request_options: typing.Optional[RequestOptions] = None,
) -> ReconnectingAudioSession: ...


def connect_audio(
    audio: typing.Union[AsyncAudioClient, AudioClient],
    *,
    session_update: typing.Optional[AudioSessionUpdate] = None,
    max_attempts: int = 6,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    request_options: typing.Optional[RequestOptions] = None,
) -> typing.Union[AsyncReconnectingAudioSession, ReconnectingAudioSession]:
    """
    A realtime audio session that reconnects when the server restarts (close code 1012), the
    connection drops (1006), or the server is overloaded (1013, or HTTP 503 on the handshake). Use
    it as a context manager, `async with` for the async client, which opens the first session.

    Each reconnect starts a new server session with a new session ID, sends it the last
    `session.update`, then sends the audio queued in the meantime. Iteration yields `Reconnecting`
    before each attempt and `Reconnected` before the new session's `session.created`. Audio the old
    session had not measured gets no result.

    It does not reconnect after `session.close`, an idle timeout, a session-ending error, or a
    rejected API key. Entering raises `ApiError` if the first session cannot start.
    """
    backoff = Backoff(max_attempts=max_attempts, initial_delay=initial_delay, max_delay=max_delay)
    driver: _Driver[AudioSessionUpdate] = _Driver(
        open=lambda: audio.connect(request_options=request_options),
        send_update=lambda socket, update: socket.send_audio_session_update(update),
        send_close=lambda socket: socket.send_audio_session_close(_CLOSE),
        send_media=lambda socket, frame: socket.send_audio_frame(frame),
        media_cost=len,
        max_queued=MAX_QUEUED_AUDIO_BYTES,
    )
    if isinstance(audio, AsyncAudioClient):
        return AsyncReconnectingAudioSession(driver, session_update, backoff)
    return ReconnectingAudioSession(driver, session_update, backoff)


@typing.overload
def connect_video(
    video: AsyncVideoClient,
    *,
    session_update: typing.Optional[VideoSessionUpdate] = None,
    max_attempts: int = 6,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    request_options: typing.Optional[RequestOptions] = None,
) -> AsyncReconnectingVideoSession: ...


@typing.overload
def connect_video(
    video: VideoClient,
    *,
    session_update: typing.Optional[VideoSessionUpdate] = None,
    max_attempts: int = 6,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    request_options: typing.Optional[RequestOptions] = None,
) -> ReconnectingVideoSession: ...


def connect_video(
    video: typing.Union[AsyncVideoClient, VideoClient],
    *,
    session_update: typing.Optional[VideoSessionUpdate] = None,
    max_attempts: int = 6,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    request_options: typing.Optional[RequestOptions] = None,
) -> typing.Union[AsyncReconnectingVideoSession, ReconnectingVideoSession]:
    """
    A realtime video session that reconnects when the server restarts (close code 1012), the
    connection drops (1006), or the server is overloaded (1013, or HTTP 503 on the handshake). Use
    it as a context manager, `async with` for the async client, which opens the first session.

    Each reconnect starts a new server session with a new session ID, sends it the last
    `session.update`, then sends the frames queued in the meantime. Iteration yields `Reconnecting`
    before each attempt and `Reconnected` before the new session's `session.created`. Frames the old
    session had not answered get no reply, so on `Reconnected` clear any queue that pairs replies
    with frames. Face IDs restart in the new session.

    It does not reconnect after `session.close`, an idle timeout, a session-ending error, or a
    rejected API key. Entering raises `ApiError` if the first session cannot start.
    """
    backoff = Backoff(max_attempts=max_attempts, initial_delay=initial_delay, max_delay=max_delay)
    driver: _Driver[VideoSessionUpdate] = _Driver(
        open=lambda: video.connect(request_options=request_options),
        send_update=lambda socket, update: socket.send_video_session_update(update),
        send_close=lambda socket: socket.send_video_session_close(_CLOSE),
        send_media=lambda socket, frame: socket.send_image_frame(frame),
        media_cost=lambda frame: 1,
        max_queued=MAX_QUEUED_FRAMES,
    )
    if isinstance(video, AsyncVideoClient):
        return AsyncReconnectingVideoSession(driver, session_update, backoff)
    return ReconnectingVideoSession(driver, session_update, backoff)
