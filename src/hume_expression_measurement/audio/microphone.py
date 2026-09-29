# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

import asyncio
import collections
import contextlib
import enum
import threading
import typing

import numpy as np
import numpy.typing as npt
from ._optional import MissingDependencyError, import_optional
from ._pcm import FrameBuffer, check_frame_ms
from .resampler import Resampler

_PORTAUDIO_MISSING = (
    "sounddevice is installed, but the PortAudio library it records through is not. Install PortAudio "
    "with your system package manager, for example `sudo apt-get install libportaudio2` on Debian or Ubuntu."
)
# PortAudio keeps recording when the loop falls behind, so the audio waiting for it needs a cap.
_MAX_BACKLOG_SECONDS = 10


class _DeviceInfo(typing.TypedDict):
    name: str
    max_input_channels: int
    default_samplerate: float


class _InputStream(typing.Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


_Callback = typing.Callable[[npt.NDArray[np.float32], int, typing.Any, typing.Any], None]


class _SoundDevice(typing.Protocol):
    PortAudioError: typing.Type[Exception]

    def query_devices(
        self, device: typing.Union[int, str, None] = ..., kind: typing.Optional[str] = ...
    ) -> typing.Any: ...

    def InputStream(
        self,
        *,
        samplerate: float,
        channels: int,
        dtype: str,
        device: typing.Union[int, str, None],
        callback: _Callback,
        finished_callback: typing.Callable[[], None],
    ) -> _InputStream: ...


class _Status(enum.Enum):
    WAITING = enum.auto()
    FINISHED = enum.auto()
    REPLACED = enum.auto()
    OVERFLOWED = enum.auto()


class Microphone:
    """
    Captures audio from an input device and yields it as frames ready to send with
    `send_audio_frame`: signed 16-bit little-endian PCM, mono, 16 kHz.

    The device is opened at its own default sample rate and converted with `Resampler`, so any
    microphone works whatever its native format. Only the device's first channel is recorded.

    Open one with `Microphone.open` rather than constructing it.

    Each `async for` yields only audio recorded after it starts, so a paused loop never resumes
    with stale audio, and starting a new `async for` ends any earlier one. When the `async with`
    block exits, an `async for` running in another task yields the rest of the audio and then
    ends. If the audio stream stops on its own before then, which some audio systems do when the
    device is disconnected, iteration raises RuntimeError. Iteration also raises RuntimeError if
    more than 10 seconds of recorded audio builds up waiting for the loop, and the waiting audio
    is discarded.

    On macOS the first recording triggers a permission prompt for the terminal or app running
    Python. If access is denied, the device records silence rather than raising an error.

    Examples
    --------
    from hume_expression_measurement.audio import Microphone

    async with Microphone.open() as microphone:
        async for frame in microphone:
            await socket.send_audio_frame(frame)
    """

    device_name: str
    """Name of the input device being recorded."""

    device_sample_rate: int
    """Sample rate, in Hz, the device records at before conversion to 16 kHz."""

    def __init__(self) -> None:
        raise TypeError("Open a Microphone with `async with Microphone.open() as microphone:`.")

    @classmethod
    @contextlib.asynccontextmanager
    async def open(
        cls, *, device: typing.Union[int, str, None] = None, frame_ms: int = 100
    ) -> typing.AsyncIterator["Microphone"]:
        """
        Starts recording and stops when the `async with` block exits.

        Parameters
        ----------
        device : typing.Union[int, str, None]
            The input device, as a sounddevice device index or a substring of its name. None
            records from the system's default input device.

        frame_ms : int
            Duration of each frame in milliseconds, from 1 to 10000.

        Raises
        ------
        MissingDependencyError
            The audio extra, or the PortAudio system library, is not installed.

        ValueError
            frame_ms is out of range, or the device does not exist or is not an input device.
            The message lists the available input devices.

        sounddevice.PortAudioError
            The device exists but could not be opened, for example because another application
            holds it exclusively.
        """
        check_frame_ms(frame_ms)
        # Loaded here so a missing soxr is reported before recording rather than at the first `async for`.
        import_optional("soxr")
        # PortAudio stays on the event loop thread. Cancelling an asyncio.to_thread call leaves its
        # worker thread running, so a cancelled open could leak the stream or stop it mid-start,
        # and PortAudio does not allow overlapping calls on a stream.
        sounddevice, info = _find_input_device(device)
        microphone = cls.__new__(cls)
        microphone._setup(info, frame_ms)
        stream = sounddevice.InputStream(
            samplerate=microphone.device_sample_rate,
            channels=1,
            dtype="float32",
            device=device,
            callback=microphone._on_block,
            finished_callback=microphone._on_finished,
        )
        try:
            stream.start()
            yield microphone
        finally:
            microphone._closing = True
            try:
                _stop_and_close(stream)
            finally:
                # PortAudio reports the end when the stream stops, but sounddevice ignores a failure to
                # stop, so the end is also signalled here.
                microphone._on_finished()

    def _setup(self, info: _DeviceInfo, frame_ms: int) -> None:
        self.device_name = info["name"]
        self.device_sample_rate = int(info["default_samplerate"])
        self._frame_ms = frame_ms
        self._loop = asyncio.get_running_loop()
        self._closing = False
        self._changed = asyncio.Event()
        # Guards the state below, which PortAudio's thread updates for every block.
        self._lock = threading.Lock()
        # Numbers each `async for`, so an earlier one can tell it was replaced.
        self._iteration = 0
        self._blocks: typing.Deque[npt.NDArray[np.float32]] = collections.deque()
        self._backlog = 0
        self._max_backlog = self.device_sample_rate * _MAX_BACKLOG_SECONDS
        self._overflowed = False
        self._finished = False
        self._wake_scheduled = False

    def __aiter__(self) -> typing.AsyncIterator[bytes]:
        with self._lock:
            self._iteration += 1
            iteration = self._iteration
            self._blocks.clear()
            self._backlog = 0
            self._overflowed = False
        self._changed.set()
        return _Frames(self, iteration)

    # PortAudio calls this on its own thread and reuses `indata` after it returns.
    def _on_block(self, indata: npt.NDArray[np.float32], _frames: int, _time: typing.Any, _status: typing.Any) -> None:
        with self._lock:
            if self._overflowed:
                return
            if self._backlog + len(indata) > self._max_backlog:
                # Released here, because a loop that fell behind may never read again.
                self._blocks.clear()
                self._backlog = 0
                self._overflowed = True
            else:
                self._blocks.append(indata[:, 0].copy())
                self._backlog += len(indata)
        self._wake()

    # PortAudio calls this after the last `_on_block`.
    def _on_finished(self) -> None:
        with self._lock:
            self._finished = True
        self._wake()

    # Only one wakeup is scheduled at a time, so an event loop too busy to run them does not pile them up.
    def _wake(self) -> None:
        with self._lock:
            if self._wake_scheduled:
                return
            self._wake_scheduled = True
        self._loop.call_soon_threadsafe(self._woken)

    def _woken(self) -> None:
        with self._lock:
            self._wake_scheduled = False
        self._changed.set()

    def _status(self, iteration: int) -> typing.Optional[_Status]:
        with self._lock:
            if iteration != self._iteration:
                return _Status.REPLACED
            if self._overflowed:
                return _Status.OVERFLOWED
            return None

    def _take(self) -> typing.Union[npt.NDArray[np.float32], _Status]:
        with self._lock:
            if self._blocks:
                block = self._blocks.popleft()
                self._backlog -= len(block)
                return block
            # Read under the same lock as the empty check, because PortAudio can record a last
            # block between the two.
            return _Status.FINISHED if self._finished else _Status.WAITING


class _Frames:
    def __init__(self, microphone: Microphone, iteration: int) -> None:
        self._microphone = microphone
        self._iteration = iteration
        self._resampler = Resampler(sample_rate=microphone.device_sample_rate, channels=1, sample_format="float32")
        self._frames = FrameBuffer(microphone._frame_ms)
        self._ready: typing.Deque[bytes] = collections.deque()
        self._flushed = False
        self._reading = False
        self._ended = False

    def __aiter__(self) -> "_Frames":
        return self

    async def __anext__(self) -> bytes:
        if self._ended:
            raise StopAsyncIteration
        if self._reading:
            raise RuntimeError(
                f"Another task is already waiting for a frame from {self._microphone.device_name}, "
                "and only one task at a time can read from a microphone."
            )
        self._reading = True
        try:
            return await self._read()
        except (StopAsyncIteration, RuntimeError):
            # A cancelled read does not end the iteration, so a timeout around `anext()` can be retried.
            self._ended = True
            raise
        finally:
            self._reading = False

    async def _read(self) -> bytes:
        microphone = self._microphone
        while True:
            microphone._changed.clear()
            # Checked before every frame, so a replaced or overflowed iteration returns none of the
            # frames it has already converted.
            status = microphone._status(self._iteration)
            if status is _Status.REPLACED:
                raise StopAsyncIteration
            if status is _Status.OVERFLOWED:
                raise RuntimeError(
                    f"More than {_MAX_BACKLOG_SECONDS} seconds of audio from {microphone.device_name} built up "
                    "waiting for the `async for` loop, which means the loop body is too slow for live audio."
                )
            if self._ready:
                return self._ready.popleft()
            # Taken only once the converted frames are used up, so audio the loop has not reached
            # stays in the store and counts toward the cap.
            item = microphone._take()
            if isinstance(item, np.ndarray):
                self._ready.extend(self._frames.push(self._resampler.process(item)))
            elif item is _Status.WAITING:
                await microphone._changed.wait()
            elif not self._flushed:
                self._flushed = True
                self._ready.extend(self._frames.push(self._resampler.flush()))
                rest = self._frames.finish()
                if rest is not None:
                    self._ready.append(rest)
            elif microphone._closing:
                raise StopAsyncIteration
            else:
                raise RuntimeError(
                    f"The audio stream from {microphone.device_name} stopped, "
                    "for example because the device was disconnected."
                )


def _find_input_device(device: typing.Union[int, str, None]) -> typing.Tuple[_SoundDevice, _DeviceInfo]:
    sounddevice = _load_sounddevice()
    try:
        info = typing.cast(_DeviceInfo, sounddevice.query_devices(device, kind="input"))
    except (ValueError, sounddevice.PortAudioError) as err:
        reason = "There is no default input device" if device is None else str(err)
        raise ValueError(f"{reason}. {_input_devices(sounddevice)}") from err
    return sounddevice, info


def _load_sounddevice() -> _SoundDevice:
    try:
        return typing.cast(_SoundDevice, import_optional("sounddevice"))
    except OSError as err:
        raise MissingDependencyError(_PORTAUDIO_MISSING, name="sounddevice") from err


def _input_devices(sounddevice: _SoundDevice) -> str:
    devices = typing.cast(typing.Sequence[_DeviceInfo], sounddevice.query_devices())
    inputs = [f"{index}: {info['name']}" for index, info in enumerate(devices) if info["max_input_channels"] > 0]
    if not inputs:
        return "No input devices are available."
    return "Available input devices: " + "; ".join(inputs) + "."


def _stop_and_close(stream: _InputStream) -> None:
    try:
        stream.stop()
    finally:
        stream.close()
