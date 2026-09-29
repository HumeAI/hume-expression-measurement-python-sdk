import pathlib
import struct
import wave

import numpy as np
import pytest

from hume_expression_measurement.audio import iter_wav_frames


def _write_wav(path: pathlib.Path, *, sample_rate: int, channels: int, sample_width: int, data: bytes) -> pathlib.Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(data)
    return path


def test_16k_mono_16_bit_file_is_split_into_frames_unchanged(tmp_path: pathlib.Path) -> None:
    pcm = np.random.default_rng(0).integers(-32768, 32768, 4000, dtype=np.int16).astype("<i2").tobytes()
    path = _write_wav(tmp_path / "speech.wav", sample_rate=16000, channels=1, sample_width=2, data=pcm)

    frames = list(iter_wav_frames(path))

    assert [len(frame) for frame in frames] == [3200, 3200, 1600]
    assert b"".join(frames) == pcm


def test_48k_stereo_24_bit_file_is_converted_to_16k_mono(tmp_path: pathlib.Path) -> None:
    t = np.arange(48000) / 48000
    tone = np.round(0.5 * np.sin(2 * np.pi * 440 * t) * (1 << 23)).astype("<i4")
    stereo = np.repeat(tone, 2)
    # 24-bit samples are the low three bytes of each little-endian 32-bit value.
    data = stereo.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    path = _write_wav(tmp_path / "speech.wav", sample_rate=48000, channels=2, sample_width=3, data=data)

    frames = list(iter_wav_frames(path, frame_ms=250))
    out = np.frombuffer(b"".join(frames), dtype="<i2")

    assert all(len(frame) == 8000 for frame in frames[:-1])
    assert 0 < len(frames[-1]) <= 8000
    assert abs(len(out) - 16000) <= 1
    assert np.abs(out).max() == pytest.approx(0.5 * 32768, rel=0.01)


def test_8_bit_samples_are_read_as_unsigned(tmp_path: pathlib.Path) -> None:
    path = _write_wav(tmp_path / "speech.wav", sample_rate=16000, channels=1, sample_width=1, data=bytes([128, 255, 0]))

    frames = list(iter_wav_frames(path))

    assert np.frombuffer(b"".join(frames), dtype="<i2").tolist() == [0, 32512, -32768]


def test_floating_point_wav_is_rejected_with_a_conversion_command(tmp_path: pathlib.Path) -> None:
    samples = struct.pack("<4f", 0.0, 0.1, -0.1, 0.0)
    fmt = struct.pack("<HHIIHH", 3, 1, 16000, 64000, 4, 32)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(samples)) + samples
    path = tmp_path / "float.wav"
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)

    with pytest.raises(ValueError, match="ffmpeg -i input.wav -ac 1 -ar 16000 -sample_fmt s16 output.wav"):
        next(iter_wav_frames(path))


@pytest.mark.parametrize("frame_ms", [0, 10001])
def test_frame_duration_out_of_range_is_rejected_at_call_time(tmp_path: pathlib.Path, frame_ms: int) -> None:
    with pytest.raises(ValueError, match="frame_ms must be between 1 and 10000"):
        iter_wav_frames(tmp_path / "unused.wav", frame_ms=frame_ms)
