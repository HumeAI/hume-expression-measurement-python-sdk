import sys
import typing

import numpy as np
import numpy.typing as npt
import pytest

from hume_expression_measurement.audio import MissingDependencyError, Resampler


def _tone(frequency: float, sample_rate: int, seconds: float = 1.0, amplitude: float = 0.5) -> npt.NDArray[np.float32]:
    t = np.arange(int(sample_rate * seconds)) / sample_rate
    return (amplitude * np.sin(2 * np.pi * frequency * t)).astype(np.float32)


def _convert(resampler: Resampler, audio: npt.NDArray[np.float32]) -> npt.NDArray[np.int16]:
    pcm = resampler.process(audio) + resampler.flush()
    return np.frombuffer(pcm, dtype="<i2")


def _middle_rms(samples: npt.NDArray[np.int16]) -> float:
    cut = len(samples) // 10
    middle = samples[cut:-cut].astype(np.float64) / 32768.0
    return float(np.sqrt(np.mean(middle**2)))


@pytest.mark.parametrize("sample_rate", [8000, 11025, 44100, 48000])
def test_tone_keeps_its_frequency_level_and_duration(sample_rate: int) -> None:
    out = _convert(Resampler(sample_rate=sample_rate, channels=1, sample_format="float32"), _tone(1000, sample_rate))

    assert abs(len(out) - 16000) <= 1
    spectrum = np.abs(np.fft.rfft(out))
    assert np.fft.rfftfreq(len(out), 1 / 16000)[np.argmax(spectrum)] == pytest.approx(1000, abs=1)
    assert _middle_rms(out) == pytest.approx(0.5 / np.sqrt(2), rel=0.01)


def test_tone_above_the_output_nyquist_frequency_is_removed() -> None:
    out = _convert(Resampler(sample_rate=48000, channels=1, sample_format="float32"), _tone(10000, 48000))

    assert _middle_rms(out) < 1e-3


def test_splitting_the_stream_into_blocks_does_not_change_the_output() -> None:
    audio = np.random.default_rng(0).uniform(-0.5, 0.5, 48000).astype(np.float32)
    whole = Resampler(sample_rate=48000, channels=1, sample_format="float32")
    split = Resampler(sample_rate=48000, channels=1, sample_format="float32")

    expected = whole.process(audio) + whole.flush()
    actual = b"".join(split.process(block) for block in np.split(audio, [1000, 1001, 5800])) + split.flush()

    assert actual == expected


def test_16k_mono_int16_passes_through_unchanged() -> None:
    pcm = np.random.default_rng(1).integers(-32768, 32768, 1600, dtype=np.int16).astype("<i2").tobytes()
    resampler = Resampler(sample_rate=16000, channels=1)

    assert resampler.process(pcm) + resampler.flush() == pcm


def test_channels_are_averaged_and_bytes_may_split_a_frame() -> None:
    stereo = np.array([[1000, 3000], [-32768, -32768], [101, 100]], dtype="<i2").tobytes()
    resampler = Resampler(sample_rate=16000, channels=2)

    pcm = resampler.process(stereo[:3]) + resampler.process(stereo[3:]) + resampler.flush()

    assert np.frombuffer(pcm, dtype="<i2").tolist() == [2000, -32768, 100]


def test_float64_and_interleaved_arrays_convert_like_float32_frames() -> None:
    stereo = np.stack([_tone(1000, 44100), _tone(300, 44100)], axis=1)
    expected = _convert(Resampler(sample_rate=44100, channels=2, sample_format="float32"), stereo)

    float64 = _convert(Resampler(sample_rate=44100, channels=2, sample_format="float64"), stereo.astype(np.float64))
    interleaved = _convert(Resampler(sample_rate=44100, channels=2, sample_format="float32"), stereo.reshape(-1))

    assert float64.tolist() == expected.tolist()
    assert interleaved.tolist() == expected.tolist()


def test_float_input_beyond_full_scale_is_clipped() -> None:
    resampler = Resampler(sample_rate=16000, channels=1, sample_format="float32")

    out = _convert(resampler, np.array([1.5, -1.5, 0.25], dtype=np.float32))

    assert out.tolist() == [32767, -32768, 8192]


def test_flush_rejects_a_stream_that_ends_partway_through_a_frame_and_resets() -> None:
    resampler = Resampler(sample_rate=16000, channels=2)
    resampler.process(b"\x01\x02\x03")

    with pytest.raises(ValueError, match="3 bytes into a sample frame of 4 bytes"):
        resampler.flush()
    assert resampler.process(b"\x00\x00\x00\x00") + resampler.flush() == b"\x00\x00"


def test_resampler_can_take_a_new_stream_after_flush() -> None:
    resampler = Resampler(sample_rate=48000, channels=1, sample_format="float32")
    first = _convert(resampler, _tone(1000, 48000))

    second = _convert(resampler, _tone(1000, 48000))

    assert second.tolist() == first.tolist()


def test_array_with_the_wrong_dtype_or_shape_is_rejected() -> None:
    resampler = Resampler(sample_rate=16000, channels=2)

    with pytest.raises(TypeError, match="must have dtype int16"):
        resampler.process(np.zeros((4, 2), dtype=np.float32))
    with pytest.raises(ValueError, match=r"shaped \(frames, 2\)"):
        resampler.process(np.zeros((4, 3), dtype=np.int16))


@pytest.mark.parametrize("sample_rate", [0, 2**32])
def test_sample_rate_out_of_range_is_rejected(sample_rate: int) -> None:
    with pytest.raises(ValueError, match="sample_rate must be between 1 and 4294967295"):
        Resampler(sample_rate=sample_rate, channels=1)


def test_sample_rate_that_is_not_an_integer_is_rejected() -> None:
    with pytest.raises(TypeError, match="sample_rate must be an integer, got nan"):
        Resampler(sample_rate=typing.cast(typing.Any, float("nan")), channels=1)


def test_channel_count_that_is_not_an_integer_is_rejected() -> None:
    with pytest.raises(TypeError, match="channels must be an integer, got 2.0"):
        Resampler(sample_rate=48000, channels=typing.cast(typing.Any, 2.0))


def test_missing_soxr_names_the_install_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "soxr", None)

    with pytest.raises(MissingDependencyError, match=r'pip install "hume-expression-measurement\[audio\]"'):
        Resampler(sample_rate=48000, channels=1)
