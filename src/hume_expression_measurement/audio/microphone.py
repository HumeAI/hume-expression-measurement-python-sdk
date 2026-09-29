# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

import asyncio
import contextlib
import enum
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
    iteration raises RuntimeError.

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
        # PortAudio is called directly rather than through asyncio.to_thread. Cancelling a task
        # does not stop its worker thread, so a cancelled open could leave a stream open or stop
        # it while it is still starting, and PortAudio does not allow overlapping calls on a stream.
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
            _stop_and_close(stream)
            # Scheduled so it runs after the blocks PortAudio handed to the event loop before stopping.
            microphone._loop.call_soon(microphone._finish)

    def _setup(self, info: _DeviceInfo, frame_ms: int) -> None:
        self.device_name = info["name"]
        self.device_sample_rate = int(info["default_samplerate"])
        self._frame_ms = frame_ms
        self._loop = asyncio.get_running_loop()
        # The queue of the current `async for`, or None when nothing iterates.
        self._queue: typing.Optional[_Queue] = None
        self._closing = False
        self._finished = False

    def __aiter__(self) -> typing.AsyncIterator[bytes]:
        if self._queue is not None:
            self._queue.put_nowait(_Signal.SUPERSEDED)
        queue: _Queue = asyncio.Queue()
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
                    for frame in frames.push(resampler.process(item)):
                        yield frame
                if item is _Signal.SUPERSEDED:
                    return
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
            if self._queue is queue:
                self._queue = None

    # PortAudio calls this on its own thread and reuses `indata` after it returns.
    def _on_block(self, indata: npt.NDArray[np.float32], _frames: int, _time: typing.Any, _status: typing.Any) -> None:
        self._loop.call_soon_threadsafe(self._receive, indata[:, 0].copy())

    # PortAudio calls this after the last `_on_block`, so the signal is queued behind every block.
    def _on_finished(self) -> None:
        self._loop.call_soon_threadsafe(self._finish)

    def _receive(self, block: npt.NDArray[np.float32]) -> None:
        if self._queue is not None:
            self._queue.put_nowait(block)

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self._queue is not None:
            self._queue.put_nowait(_Signal.FINISHED)


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
