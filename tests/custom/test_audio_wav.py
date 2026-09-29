import pathlib
import struct
import typing
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


def _write_raw_wav(
    path: pathlib.Path,
    *,
    tag: int,
    sample_rate: int,
    channels: int,
    bits: int,
    data: bytes,
    extensible: bool = False,
    data_size: typing.Optional[int] = None,
) -> pathlib.Path:
    block_align = channels * bits // 8
    fmt = struct.pack(
        "<HHIIHH", 0xFFFE if extensible else tag, channels, sample_rate, sample_rate * block_align, block_align, bits
    )
    if extensible:
        subformat = struct.pack("<H", tag) + bytes.fromhex("000000001000800000aa00389b71")
        fmt += struct.pack("<HHI", 22, bits, 0) + subformat
    # An odd-sized chunk before the data exercises the padding byte that follows it.
    info = b"INFOISFT\x05\x00\x00\x00Lavf\x00"
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"LIST" + struct.pack("<I", len(info)) + info + b"\x00"
    body += b"data" + struct.pack("<I", len(data) if data_size is None else data_size) + data
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
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


def test_extensible_header_reads_like_the_basic_header(tmp_path: pathlib.Path) -> None:
    data = np.random.default_rng(1).integers(0, 256, 48000 * 2 * 3, dtype=np.uint8).tobytes()
    basic = _write_wav(tmp_path / "basic.wav", sample_rate=48000, channels=2, sample_width=3, data=data)
    extensible = _write_raw_wav(
        tmp_path / "extensible.wav", tag=1, sample_rate=48000, channels=2, bits=24, data=data, extensible=True
    )

    assert list(iter_wav_frames(extensible)) == list(iter_wav_frames(basic))


@pytest.mark.parametrize(("bits", "extensible"), [(32, False), (64, True)])
def test_floating_point_samples_are_converted(tmp_path: pathlib.Path, bits: int, extensible: bool) -> None:
    samples = np.array([0.0, 0.25, -0.5, 1.5], dtype=f"<f{bits // 8}").tobytes()
    path = _write_raw_wav(
        tmp_path / "float.wav", tag=3, sample_rate=16000, channels=1, bits=bits, data=samples, extensible=extensible
    )

    frames = list(iter_wav_frames(path))

    assert np.frombuffer(b"".join(frames), dtype="<i2").tolist() == [0, 8192, -16384, 32767]


def test_data_of_unknown_size_is_read_to_the_end_of_the_file(tmp_path: pathlib.Path) -> None:
    pcm = np.arange(-800, 800, dtype="<i2").tobytes()
    path = _write_raw_wav(
        tmp_path / "piped.wav", tag=1, sample_rate=16000, channels=1, bits=16, data=pcm, data_size=0xFFFFFFFF
    )

    assert b"".join(iter_wav_frames(path)) == pcm


def test_file_ending_partway_through_a_sample_is_read_to_the_last_whole_sample(tmp_path: pathlib.Path) -> None:
    pcm = np.array([100, -200, 300], dtype="<i2").tobytes()
    path = _write_wav(tmp_path / "interrupted.wav", sample_rate=16000, channels=1, sample_width=2, data=pcm)
    path.write_bytes(path.read_bytes()[:-1])

    assert b"".join(iter_wav_frames(path)) == pcm[:4]


def test_compressed_wav_is_rejected_with_a_conversion_command(tmp_path: pathlib.Path) -> None:
    path = _write_raw_wav(tmp_path / "adpcm.wav", tag=2, sample_rate=16000, channels=1, bits=4, data=bytes(100))

    with pytest.raises(ValueError, match="format tag 0x0002.*ffmpeg -i input.wav -ac 1 -ar 16000 -sample_fmt s16"):
        next(iter_wav_frames(path))


@pytest.mark.parametrize("frame_ms", [0, 10001])
def test_frame_duration_out_of_range_is_rejected_at_call_time(tmp_path: pathlib.Path, frame_ms: int) -> None:
    with pytest.raises(ValueError, match="frame_ms must be between 1 and 10000"):
        iter_wav_frames(tmp_path / "unused.wav", frame_ms=frame_ms)
