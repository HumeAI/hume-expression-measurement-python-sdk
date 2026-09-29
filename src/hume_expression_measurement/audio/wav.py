# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

import os
import typing
import wave

import numpy as np
import numpy.typing as npt
from ._pcm import FrameBuffer
from .resampler import Resampler

_FFMPEG_COMMAND = "ffmpeg -i input.wav -ac 1 -ar 16000 -sample_fmt s16 output.wav"


def iter_wav_frames(path: typing.Union[str, "os.PathLike[str]"], *, frame_ms: int = 100) -> typing.Iterator[bytes]:
    """
    Reads a PCM WAV file and yields it as frames ready to send with `send_audio_frame`.

    The audio is converted to signed 16-bit little-endian PCM, mono, 16 kHz, whatever the file's
    sample rate, channel count and bit depth (8, 16, 24 or 32-bit integer PCM). Every frame holds
    `frame_ms` milliseconds of audio except the last, which holds what remains. No frame is empty.

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
    ValueError
        frame_ms is out of range, or the file is not integer PCM WAV. Floating-point and
        compressed WAV files are not supported; convert them first, for example with
        `ffmpeg -i input.wav -ac 1 -ar 16000 -sample_fmt s16 output.wav`.

    Examples
    --------
    from hume_expression_measurement.audio import iter_wav_frames

    for frame in iter_wav_frames("speech.wav"):
        await socket.send_audio_frame(frame)
    """
    frames = FrameBuffer(frame_ms)
    return _read_frames(os.fspath(path), frame_ms, frames)


def _read_frames(path: str, frame_ms: int, frames: FrameBuffer) -> typing.Iterator[bytes]:
    try:
        wav = wave.open(path, "rb")
    except (wave.Error, EOFError) as err:
        raise ValueError(
            f"{path} is not an integer PCM WAV file ({err}). Convert it first, for example with: {_FFMPEG_COMMAND}"
        ) from err
    with wav:
        sample_width = wav.getsampwidth()
        channels = wav.getnchannels()
        sample_rate = wav.getframerate()
        resampler = Resampler(sample_rate=sample_rate, channels=channels, sample_format="float32")
        block_frames = max(1, sample_rate * frame_ms // 1000)
        while True:
            data = wav.readframes(block_frames)
            if not data:
                break
            yield from frames.push(resampler.process(_to_float32(data, sample_width)))
        yield from frames.push(resampler.flush())
    rest = frames.finish()
    if rest is not None:
        yield rest


def _to_float32(data: bytes, sample_width: int) -> npt.NDArray[np.float32]:
    if sample_width == 1:
        # 8-bit WAV samples are unsigned, centered on 128.
        return (np.frombuffer(data, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if sample_width == 2:
        return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    if sample_width == 3:
        raw = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        values = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
        values = np.where(values >= 1 << 23, values - (1 << 24), values)
        return values.astype(np.float32) / float(1 << 23)
    return np.frombuffer(data, dtype="<i4").astype(np.float32) / float(1 << 31)
