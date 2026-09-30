# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

import os
import struct
import typing

import numpy as np
import numpy.typing as npt
from ._optional import import_optional
from ._pcm import FrameBuffer
from .resampler import Resampler

_FFMPEG_COMMAND = "ffmpeg -i input.wav -ac 1 -ar 16000 -sample_fmt s16 output.wav"

_PCM = 0x0001
_IEEE_FLOAT = 0x0003
_EXTENSIBLE = 0xFFFE
# An extensible header's subformat GUID is the format tag it stands for followed by these bytes.
_SUBFORMAT_GUID_SUFFIX = bytes.fromhex("000000001000800000aa00389b71")
# Writers that cannot seek back to fill in the size, such as ffmpeg writing to a pipe, leave this.
_UNKNOWN_SIZE = 0xFFFFFFFF
_SAMPLE_WIDTHS = {_PCM: (1, 2, 3, 4), _IEEE_FLOAT: (4, 8)}
_SKIP_READ_BYTES = 64 * 1024
# The read size follows from the header's sample rate and channel count, so a crafted header
# could otherwise request an enormous read.
_MAX_READ_BYTES = 1024 * 1024
# The extensible header is all the reader parses. The rest of a longer fmt chunk is skipped,
# because its size comes from the file and can be arbitrarily large.
_FMT_READ_BYTES = 40


class _Format(typing.NamedTuple):
    tag: int
    channels: int
    sample_rate: int
    sample_width: int


def iter_wav_frames(path: typing.Union[str, "os.PathLike[str]"], *, frame_ms: int = 100) -> typing.Iterator[bytes]:
    """
    Reads a WAV file and yields it as frames ready to send with `send_audio_frame`.

    The audio is converted to signed 16-bit little-endian PCM, mono, 16 kHz, whatever the file's
    sample rate and channel count. Integer PCM (8, 16, 24 or 32-bit, and narrower depths such as
    12 or 20-bit stored in those widths) and floating point (32 or 64-bit) are supported, with
    either the basic or the extensible WAV header. Every frame holds
    `frame_ms` milliseconds of audio except the last, which holds what remains. No frame is empty.

    A file that ends partway through a sample, such as an interrupted recording, is read up to
    the last whole sample.

    Frames are produced as fast as the file can be read. The realtime voice endpoint accepts the
    first 30 seconds of audio at once and then 2 seconds of audio per second, so pace the sends
    for a longer file.

    The file is opened when iteration starts, so errors about the file itself are raised by the
    first `next()` rather than by this call.

    Parameters
    ----------
    path : typing.Union[str, os.PathLike[str]]
        Path to the WAV file.

    frame_ms : int
        Duration of each frame in milliseconds, from 1 to 10000.

    Yields
    ------
    bytes
        One frame of signed 16-bit little-endian PCM, mono, 16 kHz.

    Raises
    ------
    MissingDependencyError
        The audio extra is not installed.

    ValueError
        frame_ms is out of range, or the file is not a WAV file this reader supports, such as a
        compressed or RF64 file. Convert other files first, for example with `ffmpeg -i input.wav -ac 1 -ar 16000 -sample_fmt s16 output.wav`.

    Examples
    --------
    from hume_expression_measurement.audio import iter_wav_frames

    for frame in iter_wav_frames("speech.wav"):
        await socket.send_audio_frame(frame)
    """
    frames = FrameBuffer(frame_ms)
    # Loaded here so a missing soxr is reported by this call rather than by the first `next()`.
    import_optional("soxr")
    return _read_frames(os.fspath(path), frame_ms, frames)


def _read_frames(path: str, frame_ms: int, frames: FrameBuffer) -> typing.Iterator[bytes]:
    with open(path, "rb") as file:
        fmt, data_size = _read_header(file, path)
        resampler = Resampler(sample_rate=fmt.sample_rate, channels=fmt.channels, sample_format="float32")
        frame_bytes = fmt.sample_width * fmt.channels
        samples_per_read = min(max(1, fmt.sample_rate * frame_ms // 1000), max(1, _MAX_READ_BYTES // frame_bytes))
        block_bytes = samples_per_read * frame_bytes
        remaining = data_size
        while True:
            data = file.read(block_bytes if remaining is None else min(block_bytes, remaining))
            if remaining is not None:
                remaining -= len(data)
            usable = len(data) - len(data) % frame_bytes
            if usable:
                yield from frames.push(resampler.process(_to_float32(data[:usable], fmt)))
            # Reads from a buffered file return fewer bytes than requested only at the end of the
            # file, even from a pipe.
            if len(data) < block_bytes:
                break
        yield from frames.push(resampler.flush())
    rest = frames.finish()
    if rest is not None:
        yield rest


def _read_header(file: typing.BinaryIO, path: str) -> typing.Tuple[_Format, typing.Optional[int]]:
    riff = file.read(12)
    if riff[:4] == b"RF64":
        raise _unsupported(path, "RF64 files are not supported")
    if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:] != b"WAVE":
        raise _unsupported(path, "no RIFF WAVE header")
    fmt: typing.Optional[_Format] = None
    while True:
        header = file.read(8)
        if len(header) < 8:
            raise _unsupported(path, "no data chunk")
        chunk_id, size = header[:4], struct.unpack("<I", header[4:])[0]
        if chunk_id == b"data":
            if fmt is None:
                raise _unsupported(path, "the data chunk comes before the fmt chunk")
            return fmt, None if size == _UNKNOWN_SIZE else size
        # Chunks with an odd size are followed by a padding byte.
        padded_size = size + size % 2
        if chunk_id == b"fmt ":
            body = file.read(min(size, _FMT_READ_BYTES))
            fmt = _parse_format(body, path)
            _skip(file, padded_size - len(body))
        else:
            _skip(file, padded_size)


def _skip(file: typing.BinaryIO, size: int) -> None:
    if file.seekable():
        file.seek(size, os.SEEK_CUR)
        return
    # A pipe, such as ffmpeg writing to stdout, cannot seek. Reading in pieces keeps a large chunk
    # out of memory.
    while size > 0:
        skipped = len(file.read(min(size, _SKIP_READ_BYTES)))
        if not skipped:
            return
        size -= skipped


def _parse_format(body: bytes, path: str) -> _Format:
    if len(body) < 16:
        raise _unsupported(path, "the fmt chunk is too short")
    tag, channels, sample_rate, _, block_align, bits = struct.unpack("<HHIIHH", body[:16])
    if tag == _EXTENSIBLE:
        if len(body) < 40 or body[26:40] != _SUBFORMAT_GUID_SUFFIX:
            raise _unsupported(path, "unknown extensible subformat")
        tag = struct.unpack("<H", body[24:26])[0]
    if tag not in _SAMPLE_WIDTHS:
        raise _unsupported(path, f"format tag 0x{tag:04X} is compressed or unknown")
    # Integer samples narrower than their container, such as 12-bit audio in 16 bits, fill its
    # high bits and leave the low bits zero, so they decode correctly at the container's width.
    sample_width = (bits + 7) // 8
    if sample_width not in _SAMPLE_WIDTHS[tag] or (tag == _IEEE_FLOAT and bits != sample_width * 8):
        raise _unsupported(path, f"{bits}-bit {'PCM' if tag == _PCM else 'floating point'}")
    if channels < 1 or sample_rate < 1 or block_align != sample_width * channels:
        raise _unsupported(path, "the fmt chunk is inconsistent")
    return _Format(tag, channels, sample_rate, sample_width)


def _unsupported(path: str, reason: str) -> ValueError:
    return ValueError(
        f"{path} is not a supported WAV file ({reason}). Convert it first, for example with: {_FFMPEG_COMMAND}"
    )


def _to_float32(data: bytes, fmt: _Format) -> npt.NDArray[np.float32]:
    if fmt.tag == _IEEE_FLOAT:
        return np.frombuffer(data, dtype="<f4" if fmt.sample_width == 4 else "<f8").astype(np.float32)
    if fmt.sample_width == 1:
        # 8-bit WAV samples are unsigned, centered on 128.
        return (np.frombuffer(data, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if fmt.sample_width == 2:
        return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    if fmt.sample_width == 3:
        raw = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        values = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
        values = np.where(values >= 1 << 23, values - (1 << 24), values)
        return values.astype(np.float32) / float(1 << 23)
    return np.frombuffer(data, dtype="<i4").astype(np.float32) / float(1 << 31)
