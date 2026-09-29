import asyncio
import importlib
import sys
import threading
import types
import typing

import numpy as np
import numpy.typing as npt
import pytest

from hume_expression_measurement.audio import Microphone, MissingDependencyError


class _PortAudioError(Exception):
    pass


class _FakeStream:
    def __init__(
        self, blocks: typing.Sequence[npt.NDArray[np.float32]], fail_on_start: bool, **settings: typing.Any
    ) -> None:
        self.settings = settings
        self.stopped = False
        self.closed = False
        self._blocks = blocks
        self._fail_on_start = fail_on_start
        self._thread: typing.Optional[threading.Thread] = None

    def start(self) -> None:
        if self._fail_on_start:
            raise _PortAudioError("Device unavailable")
        self._thread = threading.Thread(target=self._deliver)
        self._thread.start()

    def _deliver(self) -> None:
        for block in self._blocks:
            self.settings["callback"](block, len(block), None, None)

    def stop(self) -> None:
        if self._thread is not None:
            self._thread.join()
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class _FakeSoundDevice:
    PortAudioError = _PortAudioError

    def __init__(
        self,
        devices: typing.List[typing.Dict[str, typing.Any]],
        blocks: typing.Sequence[npt.NDArray[np.float32]] = (),
        fail_on_start: bool = False,
    ) -> None:
        self.devices = devices
        self.streams: typing.List[_FakeStream] = []
        self._blocks = blocks
        self._fail_on_start = fail_on_start

    def query_devices(
        self, device: typing.Union[int, str, None] = None, kind: typing.Optional[str] = None
    ) -> typing.Any:
        if kind is None:
            return self.devices
        if device is None:
            return self.devices[0]
        if isinstance(device, str):
            matches = [info for info in self.devices if device in info["name"]]
            if not matches:
                raise ValueError(f"No input device matching {device!r}")
            return matches[0]
        return self.devices[device]

    def InputStream(self, **settings: typing.Any) -> _FakeStream:
        stream = _FakeStream(self._blocks, self._fail_on_start, **settings)
        self.streams.append(stream)
        return stream


def _device(name: str, inputs: int = 1, sample_rate: float = 48000.0) -> typing.Dict[str, typing.Any]:
    return {"name": name, "max_input_channels": inputs, "default_samplerate": sample_rate}


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeSoundDevice) -> None:
    monkeypatch.setitem(sys.modules, "sounddevice", typing.cast(types.ModuleType, fake))


async def test_microphone_yields_16k_frames_of_the_recorded_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    t = np.arange(48000) / 48000
    tone = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32).reshape(-1, 1)
    fake = _FakeSoundDevice([_device("Built-in Microphone")], blocks=np.split(tone, 100))
    _install(monkeypatch, fake)

    frames = []
    async with Microphone.open(frame_ms=100) as microphone:
        assert (microphone.device_name, microphone.device_sample_rate) == ("Built-in Microphone", 48000)
        async for frame in microphone:
            frames.append(frame)
            if len(frames) == 9:
                break

    assert [len(frame) for frame in frames] == [3200] * 9
    out = np.frombuffer(b"".join(frames), dtype="<i2")
    spectrum = np.abs(np.fft.rfft(out))
    assert np.fft.rfftfreq(len(out), 1 / 16000)[np.argmax(spectrum)] == pytest.approx(1000, abs=2)
    stream = fake.streams[0]
    assert (stream.settings["samplerate"], stream.settings["channels"]) == (48000, 1)
    assert stream.stopped and stream.closed


async def test_iteration_ends_when_the_microphone_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _FakeSoundDevice([_device("Built-in Microphone")]))

    async def collect(microphone: Microphone) -> typing.List[bytes]:
        return [frame async for frame in microphone]

    async with Microphone.open() as microphone:
        task = asyncio.create_task(collect(microphone))
        await asyncio.sleep(0)

    assert await asyncio.wait_for(task, timeout=1) == []


@pytest.mark.parametrize(
    ("device", "message"),
    [
        (0, "Speakers has no input channels. Available input devices: 1: USB Microphone."),
        ("Studio", "No input device matches 'Studio'.*Available input devices: 1: USB Microphone."),
    ],
)
async def test_unusable_device_lists_the_input_devices(
    monkeypatch: pytest.MonkeyPatch, device: typing.Union[int, str], message: str
) -> None:
    _install(monkeypatch, _FakeSoundDevice([_device("Speakers", inputs=0), _device("USB Microphone")]))

    with pytest.raises(ValueError, match=message):
        async with Microphone.open(device=device):
            pass


async def test_stream_is_closed_when_the_device_fails_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSoundDevice([_device("Built-in Microphone")], fail_on_start=True)
    _install(monkeypatch, fake)

    with pytest.raises(_PortAudioError):
        async with Microphone.open():
            pass
    assert fake.streams[0].closed


async def test_missing_sounddevice_names_the_install_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sounddevice", None)

    with pytest.raises(MissingDependencyError, match=r'pip install "hume-expression-measurement\[audio\]"'):
        async with Microphone.open():
            pass


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
