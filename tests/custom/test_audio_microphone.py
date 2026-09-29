import asyncio
import importlib
import re
import sys
import threading
import time
import types
import typing

import numpy as np
import numpy.typing as npt
import pytest

from hume_expression_measurement.audio import Microphone, MissingDependencyError


class _PortAudioError(Exception):
    pass


class _FakeStream:
    """Delivers audio only when a test calls `emit`, on the calling thread."""

    def __init__(self, fail_on_start: bool, fail_on_stop: bool, start_seconds: float, **settings: typing.Any) -> None:
        self.settings = settings
        self.stopped = False
        self.stopped_while_starting = False
        self.closed = False
        self._fail_on_start = fail_on_start
        self._fail_on_stop = fail_on_stop
        self._start_seconds = start_seconds
        self._starting = False
        self._active = False

    def start(self) -> None:
        if self._fail_on_start:
            raise _PortAudioError("Device unavailable")
        self._starting = True
        time.sleep(self._start_seconds)
        self._starting = False
        self._active = True

    def emit(self, block: npt.NDArray[np.float32]) -> None:
        buffer = block.reshape(-1, 1).copy()
        self.settings["callback"](buffer, len(block), None, None)
        # PortAudio reuses the buffer for the next block.
        buffer.fill(np.nan)

    def end(self) -> None:
        """Stops the stream the way PortAudio does when the device goes away."""
        if self._active:
            self._active = False
            self.settings["finished_callback"]()

    def stop(self) -> None:
        # sounddevice ignores the error when PortAudio fails to stop, and the stream never reports its end.
        if self._fail_on_stop:
            return
        self.stopped_while_starting = self._starting
        self.end()
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class _FakeSoundDevice:
    """Looks up devices and raises errors the way sounddevice 0.5 does."""

    PortAudioError = _PortAudioError

    def __init__(
        self,
        devices: typing.List[typing.Dict[str, typing.Any]],
        default_input: typing.Optional[int] = 0,
        fail_on_start: bool = False,
        fail_on_stop: bool = False,
        start_seconds: float = 0.0,
    ) -> None:
        self.devices = devices
        self.streams: typing.List[_FakeStream] = []
        self._default_input = default_input
        self._fail_on_start = fail_on_start
        self._fail_on_stop = fail_on_stop
        self._start_seconds = start_seconds

    def query_devices(
        self, device: typing.Union[int, str, None] = None, kind: typing.Optional[str] = None
    ) -> typing.Any:
        if kind is None:
            return self.devices
        if device is None:
            if self._default_input is None:
                raise _PortAudioError("Error querying device -1")
            device = self._default_input
        if isinstance(device, str):
            matches = [
                index
                for index, info in enumerate(self.devices)
                if device in info["name"] and info["max_input_channels"] > 0
            ]
            if not matches:
                raise ValueError(f"No input device matching {device!r}")
            device = matches[0]
        info = self.devices[device]
        if info["max_input_channels"] < 1:
            raise ValueError(f"Not an input device: {info['name']!r}")
        return info

    def InputStream(self, **settings: typing.Any) -> _FakeStream:
        stream = _FakeStream(self._fail_on_start, self._fail_on_stop, self._start_seconds, **settings)
        self.streams.append(stream)
        return stream


def _device(name: str, inputs: int = 1, sample_rate: float = 48000.0) -> typing.Dict[str, typing.Any]:
    return {"name": name, "max_input_channels": inputs, "default_samplerate": sample_rate}


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeSoundDevice) -> None:
    monkeypatch.setitem(sys.modules, "sounddevice", typing.cast(types.ModuleType, fake))


def _tone(seconds: float = 1.0) -> npt.NDArray[np.float32]:
    t = np.arange(int(48000 * seconds)) / 48000
    return (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)


def _constant(value: float, samples: int = 160) -> npt.NDArray[np.float32]:
    return np.full(samples, value, dtype=np.float32)


def _pcm(value: float) -> bytes:
    return np.full(160, round(value * 32768), dtype="<i2").tobytes()


async def _collect(microphone: Microphone) -> typing.List[bytes]:
    return [frame async for frame in microphone]


async def _next_frame(frames: typing.AsyncIterator[bytes]) -> bytes:
    return await asyncio.wait_for(anext(frames), timeout=1)


async def test_microphone_yields_16k_frames_of_the_recorded_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSoundDevice([_device("Built-in Microphone")])
    _install(monkeypatch, fake)

    async with Microphone.open(frame_ms=100) as microphone:
        assert (microphone.device_name, microphone.device_sample_rate) == ("Built-in Microphone", 48000)
        frames = aiter(microphone)
        for block in np.split(_tone(), 100):
            fake.streams[0].emit(block)
        received = [await _next_frame(frames) for _ in range(9)]

    assert [len(frame) for frame in received] == [3200] * 9
    out = np.frombuffer(b"".join(received), dtype="<i2")
    spectrum = np.abs(np.fft.rfft(out))
    assert np.fft.rfftfreq(len(out), 1 / 16000)[np.argmax(spectrum)] == pytest.approx(1000, abs=2)
    stream = fake.streams[0]
    assert (stream.settings["samplerate"], stream.settings["channels"]) == (48000, 1)
    assert stream.stopped and stream.closed


async def test_audio_recorded_on_another_thread_wakes_a_waiting_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSoundDevice([_device("Built-in Microphone", sample_rate=16000.0)])
    _install(monkeypatch, fake)

    async with Microphone.open(frame_ms=10) as microphone:
        frames = aiter(microphone)
        # Recorded after the event loop has gone idle waiting for the frame, the way PortAudio's
        # thread delivers audio.
        recorder = threading.Timer(0.05, fake.streams[0].emit, args=(_constant(0.25),))
        started = time.monotonic()
        recorder.start()

        assert await asyncio.wait_for(anext(frames), timeout=2) == _pcm(0.25)
        # A wakeup that does not wake the idle event loop still runs once the timeout fires, so
        # only the delay shows it.
        assert time.monotonic() - started < 1
        recorder.join()


async def test_closing_yields_all_recorded_audio_before_iteration_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSoundDevice([_device("Built-in Microphone")])
    _install(monkeypatch, fake)

    async with Microphone.open(frame_ms=100) as microphone:
        collecting = asyncio.create_task(_collect(microphone))
        await asyncio.sleep(0)
        for block in np.split(_tone(0.82), 82):
            fake.streams[0].emit(block)
    frames = await asyncio.wait_for(collecting, timeout=1)

    assert all(frames)
    assert abs(sum(len(frame) for frame in frames) - int(0.82 * 32000)) <= 2


async def test_an_iteration_yields_only_audio_recorded_after_it_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSoundDevice([_device("Built-in Microphone", sample_rate=16000.0)])
    _install(monkeypatch, fake)
    loop = asyncio.get_running_loop()

    async with Microphone.open(frame_ms=10) as microphone:
        stream = fake.streams[0]
        stream.emit(_constant(0.25))
        loop.call_soon(stream.emit, _constant(0.5))
        first = await _next_frame(aiter(microphone))
        stream.emit(_constant(0.75))
        loop.call_soon(stream.emit, _constant(-0.5))
        second = await _next_frame(aiter(microphone))

    assert (first, second) == (_pcm(0.5), _pcm(-0.5))


async def test_starting_a_new_iteration_ends_the_earlier_one(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _FakeSoundDevice([_device("Built-in Microphone")]))

    async with Microphone.open() as microphone:
        older = asyncio.create_task(_collect(microphone))
        await asyncio.sleep(0)
        aiter(microphone)

        assert await asyncio.wait_for(older, timeout=1) == []


async def test_a_superseded_iteration_ends_without_yielding_its_waiting_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSoundDevice([_device("Built-in Microphone", sample_rate=16000.0)])
    _install(monkeypatch, fake)

    async with Microphone.open(frame_ms=10) as microphone:
        older = aiter(microphone)
        # A 30 ms block converts to three frames, so two are still unread when the new iteration starts.
        fake.streams[0].emit(_constant(0.25, 480))
        fake.streams[0].emit(_constant(0.25))
        assert await _next_frame(older) == _pcm(0.25)
        aiter(microphone)

        with pytest.raises(StopAsyncIteration):
            await _next_frame(older)


async def test_reading_one_iteration_from_two_tasks_at_once_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSoundDevice([_device("Built-in Microphone", sample_rate=16000.0)])
    _install(monkeypatch, fake)

    async with Microphone.open(frame_ms=10) as microphone:
        frames = aiter(microphone)
        waiting = asyncio.ensure_future(anext(frames))
        await asyncio.sleep(0)

        with pytest.raises(RuntimeError, match="Another task is already waiting for a frame from Built-in Microphone"):
            await _next_frame(frames)
        fake.streams[0].emit(_constant(0.25))

        assert await asyncio.wait_for(waiting, timeout=1) == _pcm(0.25)


async def test_a_cancelled_read_leaves_the_iteration_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSoundDevice([_device("Built-in Microphone", sample_rate=16000.0)])
    _install(monkeypatch, fake)

    async with Microphone.open(frame_ms=10) as microphone:
        frames = aiter(microphone)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(anext(frames), timeout=0.01)
        fake.streams[0].emit(_constant(0.25))

        assert await _next_frame(frames) == _pcm(0.25)


async def test_iteration_ends_when_the_microphone_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _FakeSoundDevice([_device("Built-in Microphone")]))

    async with Microphone.open() as microphone:
        collecting = asyncio.create_task(_collect(microphone))
        await asyncio.sleep(0)

    assert await asyncio.wait_for(collecting, timeout=1) == []
    assert await asyncio.wait_for(_collect(microphone), timeout=1) == []


async def test_iteration_ends_when_stopping_the_stream_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSoundDevice([_device("Built-in Microphone")], fail_on_stop=True)
    _install(monkeypatch, fake)

    async with Microphone.open() as microphone:
        collecting = asyncio.create_task(_collect(microphone))
        await asyncio.sleep(0)

    assert await asyncio.wait_for(collecting, timeout=1) == []
    assert fake.streams[0].closed


async def test_audio_left_behind_by_ended_iterations_does_not_count_toward_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeSoundDevice([_device("Built-in Microphone", sample_rate=16000.0)])
    _install(monkeypatch, fake)
    loop = asyncio.get_running_loop()

    def emit_one_second() -> None:
        for _ in range(100):
            fake.streams[0].emit(_constant(0.25))

    async with Microphone.open(frame_ms=10) as microphone:
        # Each iteration leaves 0.99 seconds unread, so 20 of them would exceed the cap if kept.
        for _ in range(20):
            loop.call_soon(emit_one_second)
            assert await _next_frame(aiter(microphone)) == _pcm(0.25)


async def test_stream_that_stops_on_its_own_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSoundDevice([_device("Built-in Microphone")])
    _install(monkeypatch, fake)

    async with Microphone.open() as microphone:
        asyncio.get_running_loop().call_soon(fake.streams[0].end)
        with pytest.raises(RuntimeError, match="audio stream from Built-in Microphone stopped"):
            await asyncio.wait_for(_collect(microphone), timeout=1)


async def test_falling_more_than_10_seconds_behind_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSoundDevice([_device("Built-in Microphone", sample_rate=16000.0)])
    _install(monkeypatch, fake)
    loop = asyncio.get_running_loop()

    async with Microphone.open(frame_ms=10) as microphone:
        stream = fake.streams[0]
        frames = aiter(microphone)
        # Exactly 10 seconds, which is within the cap: a 30 ms block that converts to three
        # frames, then 997 blocks of 10 ms.
        stream.emit(_constant(0.25, 480))
        for _ in range(997):
            stream.emit(_constant(0.25))
        assert await _next_frame(frames) == _pcm(0.25)
        for _ in range(10):
            stream.emit(_constant(0.25))

        # Raised even though two frames of the 30 ms block are still unread.
        with pytest.raises(RuntimeError, match="More than 10 seconds of audio from Built-in Microphone"):
            await _next_frame(frames)
        with pytest.raises(StopAsyncIteration):
            await _next_frame(frames)
        stream.emit(_constant(0.5))
        loop.call_soon(stream.emit, _constant(-0.5))
        frame = await _next_frame(aiter(microphone))

    assert frame == _pcm(-0.5)


async def test_falling_behind_with_frames_shorter_than_blocks_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSoundDevice([_device("Built-in Microphone", sample_rate=16000.0)])
    _install(monkeypatch, fake)

    async with Microphone.open(frame_ms=10) as microphone:
        frames = aiter(microphone)
        # Each 100 ms block converts to ten frames and the loop reads one frame per block, so it
        # falls another 90 ms behind with every block.
        with pytest.raises(RuntimeError, match="More than 10 seconds of audio"):
            for _ in range(200):
                fake.streams[0].emit(_constant(0.25, 1600))
                await _next_frame(frames)


@pytest.mark.parametrize(
    ("device", "default_input", "message"),
    [
        (0, 0, "Not an input device: 'Speakers'. Available input devices: 1: USB Microphone."),
        ("Studio", 0, "No input device matching 'Studio'. Available input devices: 1: USB Microphone."),
        (None, None, "There is no default input device. Available input devices: 1: USB Microphone."),
    ],
)
async def test_unusable_device_lists_the_input_devices(
    monkeypatch: pytest.MonkeyPatch,
    device: typing.Union[int, str, None],
    default_input: typing.Optional[int],
    message: str,
) -> None:
    devices = [_device("Speakers", inputs=0), _device("USB Microphone")]
    _install(monkeypatch, _FakeSoundDevice(devices, default_input=default_input))

    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        async with Microphone.open(device=device):
            pass


async def test_stream_is_closed_when_the_device_fails_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSoundDevice([_device("Built-in Microphone")], fail_on_start=True)
    _install(monkeypatch, fake)

    with pytest.raises(_PortAudioError):
        async with Microphone.open():
            pass
    assert fake.streams[0].closed


async def test_cancelling_while_the_stream_starts_stops_it_once_start_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSoundDevice([_device("Built-in Microphone")], start_seconds=0.2)
    _install(monkeypatch, fake)

    async def record() -> None:
        async with Microphone.open():
            await asyncio.sleep(10)

    recording = asyncio.create_task(record())
    asyncio.get_running_loop().call_later(0.05, recording.cancel)

    with pytest.raises(asyncio.CancelledError):
        await recording
    stream = fake.streams[0]
    assert not stream.stopped_while_starting
    assert stream.stopped and stream.closed


def test_constructing_directly_points_to_open() -> None:
    with pytest.raises(TypeError, match=r"Microphone\.open\(\)"):
        Microphone()


async def test_missing_sounddevice_names_the_install_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sounddevice", None)

    with pytest.raises(MissingDependencyError, match=r'pip install "hume-expression-measurement\[audio\]"'):
        async with Microphone.open():
            pass


async def test_missing_soxr_is_reported_before_the_device_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSoundDevice([_device("Built-in Microphone")])
    _install(monkeypatch, fake)
    monkeypatch.setitem(sys.modules, "soxr", None)

    with pytest.raises(MissingDependencyError, match="soxr"):
        async with Microphone.open():
            pass
    assert fake.streams == []


async def test_missing_portaudio_names_the_system_package(monkeypatch: pytest.MonkeyPatch) -> None:
    import_module = importlib.import_module

    def import_without_portaudio(name: str, package: typing.Optional[str] = None) -> types.ModuleType:
        if name == "sounddevice":
            raise OSError("PortAudio library not found")
        return import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", import_without_portaudio)

    with pytest.raises(MissingDependencyError, match="libportaudio2"):
        async with Microphone.open():
            pass


async def test_a_broken_sounddevice_install_raises_its_own_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import_module = importlib.import_module

    def import_without_cffi(name: str, package: typing.Optional[str] = None) -> types.ModuleType:
        if name == "sounddevice":
            raise ModuleNotFoundError("No module named '_cffi_backend'", name="_cffi_backend")
        return import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", import_without_cffi)

    with pytest.raises(ModuleNotFoundError, match="_cffi_backend"):
        async with Microphone.open():
            pass
