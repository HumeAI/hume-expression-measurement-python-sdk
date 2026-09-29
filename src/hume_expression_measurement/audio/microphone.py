# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

import asyncio
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


class _Signal(enum.Enum):
    FINISHED = enum.auto()
    SUPERSEDED = enum.auto()
    OVERFLOWED = enum.auto()


_Queue = asyncio.Queue[typing.Union[npt.NDArray[np.float32], _Signal]]


class Microphone:
    """
    Captures audio from an input device and yields it as frames ready to send with
    `send_audio_frame`: signed 16-bit little-endian PCM, mono, 16 kHz.

    The device is opened at its own default sample rate and converted with `Resampler`, so any
    microphone works whatever its native format. Only the device's first channel is recorded.

    Open one with `Microphone.open` rather than constructing it.

    Each `async for` yields audio recorded from the moment it starts. Audio recorded while
    nothing iterates is discarded, so a paused loop never resumes with stale audio, and starting
    a new `async for` ends any earlier one. When the `async with` block exits, an `async for`
    running in another task yields the rest of the audio and then ends. If the audio stream
    stops on its own before then, which some audio systems do when the device is disconnected,
    iteration raises RuntimeError. Iteration also raises RuntimeError if more than 10 seconds of
    recorded audio builds up waiting for the loop, and the waiting audio is discarded.

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
                # Scheduled so it runs after the blocks PortAudio handed to the event loop before
                # stopping, and even when stopping fails, so no `async for` is left waiting.
                microphone._loop.call_soon(microphone._finish)

    def _setup(self, info: _DeviceInfo, frame_ms: int) -> None:
        self.device_name = info["name"]
        self.device_sample_rate = int(info["default_samplerate"])
        self._frame_ms = frame_ms
        self._loop = asyncio.get_running_loop()
        # Guards `_queue` and `_backlog`, which PortAudio's thread reads for every block.
        self._lock = threading.Lock()
        # None while nothing iterates, which is what discards audio recorded between loops.
        self._queue: typing.Optional[_Queue] = None
        # A block counts from the moment PortAudio hands it over until it is consumed or discarded,
        # whichever iteration it was recorded for, so blocks waiting behind a busy event loop stay
        # within the cap even when iterations are replaced.
        self._backlog = 0
        self._max_backlog = self.device_sample_rate * _MAX_BACKLOG_SECONDS
        self._closing = False
        self._finished = False

    def __aiter__(self) -> typing.AsyncIterator[bytes]:
        queue: _Queue = asyncio.Queue()
        with self._lock:
            if self._queue is not None:
                self._end_iteration(self._queue, _Signal.SUPERSEDED)
            self._queue = None if self._finished else queue
        return self._iter_frames(queue)

    async def _iter_frames(self, queue: _Queue) -> typing.AsyncIterator[bytes]:
        resampler = Resampler(sample_rate=self.device_sample_rate, channels=1, sample_format="float32")
        frames = FrameBuffer(self._frame_ms)
        try:
            if not self._finished:
                while True:
                    item = await queue.get()
                    if isinstance(item, _Signal):
                        break
                    with self._lock:
                        self._backlog -= len(item)
                    for frame in frames.push(resampler.process(item)):
                        yield frame
                if item is _Signal.SUPERSEDED:
                    return
                if item is _Signal.OVERFLOWED:
                    raise RuntimeError(
                        f"More than {_MAX_BACKLOG_SECONDS} seconds of audio from {self.device_name} built up "
                        "waiting for the `async for` loop, which means the loop body is too slow for live audio."
                    )
                for frame in frames.push(resampler.flush()):
                    yield frame
                rest = frames.finish()
                if rest is not None:
                    yield rest
            if not self._closing:
                raise RuntimeError(
                    f"The audio stream from {self.device_name} stopped, "
                    "for example because the device was disconnected."
                )
        finally:
            # A loop exited with `break` reaches here only once the event loop finalizes the
            # generator, and by then a newer `async for` may own the queue.
            with self._lock:
                if self._queue is queue:
                    self._queue = None
                self._discard_waiting(queue)

    # PortAudio calls this on its own thread and reuses `indata` after it returns. Each block is
    # assigned to an iteration here, when it is recorded, because a callback waiting behind a busy
    # event loop would otherwise reach whichever `async for` is current once it finally runs.
    def _on_block(self, indata: npt.NDArray[np.float32], _frames: int, _time: typing.Any, _status: typing.Any) -> None:
        with self._lock:
            queue = self._queue
            if queue is None:
                return
            if self._backlog + len(indata) > self._max_backlog:
                self._queue = None
                self._loop.call_soon_threadsafe(self._overflow, queue)
                return
            self._backlog += len(indata)
            self._loop.call_soon_threadsafe(self._deliver, queue, indata[:, 0].copy())

    # PortAudio calls this after the last `_on_block`, so the signal is queued behind every block.
    def _on_finished(self) -> None:
        self._loop.call_soon_threadsafe(self._finish)

    def _deliver(self, queue: _Queue, block: npt.NDArray[np.float32]) -> None:
        with self._lock:
            if self._queue is queue:
                queue.put_nowait(block)
            else:
                self._backlog -= len(block)

    def _overflow(self, queue: _Queue) -> None:
        with self._lock:
            self._end_iteration(queue, _Signal.OVERFLOWED)

    # Called with the lock held.
    def _end_iteration(self, queue: _Queue, signal: _Signal) -> None:
        # Emptied first so the iteration sees the signal next instead of yielding the audio waiting
        # ahead of it, and so that audio's memory is released even if the loop never reads again.
        self._discard_waiting(queue)
        queue.put_nowait(signal)

    # Called with the lock held.
    def _discard_waiting(self, queue: _Queue) -> None:
        while not queue.empty():
            item = queue.get_nowait()
            if not isinstance(item, _Signal):
                self._backlog -= len(item)

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        with self._lock:
            queue = self._queue
        if queue is not None:
            queue.put_nowait(_Signal.FINISHED)


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
