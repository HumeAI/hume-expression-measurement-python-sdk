# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

import asyncio
import contextlib
import typing

import numpy as np
import numpy.typing as npt
from ._optional import MissingDependencyError, import_optional
from ._pcm import FrameBuffer
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
    ) -> _InputStream: ...


class Microphone:
    """
    Captures audio from an input device and yields it as frames ready to send with
    `send_audio_frame`: signed 16-bit little-endian PCM, mono, 16 kHz.

    The device is opened at its own default sample rate and converted with `Resampler`, so any
    microphone works whatever its native format. Only the device's first channel is recorded.

    Open one with `Microphone.open` rather than constructing it. Iteration yields frames until
    the `async with` block exits; breaking out of the loop stops iteration but keeps recording
    until then.

    On macOS the first recording triggers a permission prompt for the terminal or app running
    Python. If access is denied, the device records silence rather than raising an error.

    Examples
    --------
    from hume_expression_measurement.audio import Microphone

    async with Microphone.open() as microphone:
        async for frame in microphone:
            await socket.send_audio_frame(frame)
    """

    def __init__(
        self,
        *,
        device_name: str,
        device_sample_rate: int,
        blocks: "asyncio.Queue[typing.Optional[npt.NDArray[np.float32]]]",
        resampler: Resampler,
        frames: FrameBuffer,
    ) -> None:
        self.device_name = device_name
        """Name of the input device being recorded."""
        self.device_sample_rate = device_sample_rate
        """Sample rate, in Hz, the device records at before conversion to 16 kHz."""
        self._blocks = blocks
        self._resampler = resampler
        self._frames = frames
        self._closed = False

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
            frame_ms is out of range, or the device does not exist or has no input channels.
            The message lists the available input devices.

        sounddevice.PortAudioError
            The device exists but could not be opened, for example because another application
            holds it exclusively.
        """
        frames = FrameBuffer(frame_ms)
        sounddevice = _load_sounddevice()
        info = _input_device(sounddevice, device)
        sample_rate = int(info["default_samplerate"])
        resampler = Resampler(sample_rate=sample_rate, channels=1, sample_format="float32")
        loop = asyncio.get_running_loop()
        blocks: "asyncio.Queue[typing.Optional[npt.NDArray[np.float32]]]" = asyncio.Queue()

        # PortAudio calls this on its own thread and reuses `indata` after it returns.
        def on_block(indata: npt.NDArray[np.float32], _frames: int, _time: typing.Any, _status: typing.Any) -> None:
            loop.call_soon_threadsafe(blocks.put_nowait, indata[:, 0].copy())

        stream = sounddevice.InputStream(
            samplerate=sample_rate, channels=1, dtype="float32", device=device, callback=on_block
        )
        microphone = cls(
            device_name=info["name"],
            device_sample_rate=sample_rate,
            blocks=blocks,
            resampler=resampler,
            frames=frames,
        )
        try:
            stream.start()
            yield microphone
        finally:
            stream.stop()
            stream.close()
            microphone._close()

    def __aiter__(self) -> typing.AsyncIterator[bytes]:
        return self._iter_frames()

    async def _iter_frames(self) -> typing.AsyncIterator[bytes]:
        while not self._closed:
            block = await self._blocks.get()
            if block is None:
                return
            for frame in self._frames.push(self._resampler.process(block)):
                yield frame

    def _close(self) -> None:
        self._closed = True
        self._blocks.put_nowait(None)


def _load_sounddevice() -> _SoundDevice:
    try:
        return typing.cast(_SoundDevice, import_optional("sounddevice"))
    except OSError as err:
        raise MissingDependencyError(_PORTAUDIO_MISSING, name="sounddevice") from err


def _input_device(sounddevice: _SoundDevice, device: typing.Union[int, str, None]) -> _DeviceInfo:
    try:
        info = typing.cast(_DeviceInfo, sounddevice.query_devices(device, kind="input"))
    except (ValueError, sounddevice.PortAudioError) as err:
        raise ValueError(f"No input device matches {device!r} ({err}). {_input_devices(sounddevice)}") from err
    if info["max_input_channels"] < 1:
        raise ValueError(f"{info['name']} has no input channels. {_input_devices(sounddevice)}")
    return info


def _input_devices(sounddevice: _SoundDevice) -> str:
    devices = typing.cast(typing.Sequence[_DeviceInfo], sounddevice.query_devices())
    inputs = [f"{index}: {info['name']}" for index, info in enumerate(devices) if info["max_input_channels"] > 0]
    if not inputs:
        return "No input devices are available."
    return "Available input devices: " + "; ".join(inputs) + "."
