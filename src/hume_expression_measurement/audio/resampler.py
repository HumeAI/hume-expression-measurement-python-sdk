# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

import typing

import numpy as np
import numpy.typing as npt
from ._optional import import_optional
from ._pcm import SAMPLE_RATE

SampleFormat = typing.Literal["int16", "float32", "float64"]

_INPUT_DTYPES: typing.Dict[str, "np.dtype[typing.Any]"] = {
    "int16": np.dtype("<i2"),
    "float32": np.dtype("<f4"),
    "float64": np.dtype("<f8"),
}


class _ResampleStream(typing.Protocol):
    def resample_chunk(self, x: npt.NDArray[np.float32], last: bool = False) -> npt.NDArray[np.float32]: ...

    def clear(self) -> None: ...


class _Soxr(typing.Protocol):
    def ResampleStream(
        self, in_rate: float, out_rate: float, num_channels: int, dtype: str = ..., quality: str = ...
    ) -> _ResampleStream: ...


class Resampler:
    """
    Converts PCM audio of any sample rate and channel count to the format the realtime voice
    endpoint accepts: signed 16-bit little-endian PCM, mono, 16 kHz.

    Pass successive blocks of one stream to `process`, then call `flush` once after the last
    block. Filter state carries over between calls, so a stream converts to the same bytes
    whether it arrives in one block or many. After `flush` the resampler is ready for a new
    stream.

    Channels are averaged to mono before resampling.

    Parameters
    ----------
    sample_rate : int
        Sample rate of the input, in Hz.

    channels : int
        Number of channels in the input.

    sample_format : typing.Literal["int16", "float32", "float64"]
        Encoding of the input. "int16" is signed 16-bit PCM, and "float32" and "float64" are
        floating point with full scale at -1.0 and 1.0. Bytes are read as little-endian, and a
        NumPy array must have the matching dtype. `soundfile.read()` returns float64 by default.

    Examples
    --------
    from hume_expression_measurement.audio import Resampler

    resampler = Resampler(sample_rate=48000, channels=2)
    for block in blocks:
        pcm = resampler.process(block)
        if pcm:
            await socket.send_audio_frame(pcm)
    pcm = resampler.flush()
    if pcm:
        await socket.send_audio_frame(pcm)
    """

    def __init__(self, *, sample_rate: int, channels: int, sample_format: SampleFormat = "int16") -> None:
        if sample_rate <= 0:
            raise ValueError(f"sample_rate must be positive, got {sample_rate}.")
        if channels < 1:
            raise ValueError(f"channels must be at least 1, got {channels}.")
        if sample_format not in _INPUT_DTYPES:
            raise ValueError(f'sample_format must be "int16", "float32" or "float64", got {sample_format!r}.')
        soxr = typing.cast(_Soxr, import_optional("soxr"))
        self._channels = channels
        self._sample_format = sample_format
        self._dtype = _INPUT_DTYPES[sample_format]
        self._frame_bytes = self._dtype.itemsize * channels
        # Bytes left over from a block that ended partway through a sample frame.
        self._pending = b""
        self._stream: typing.Optional[_ResampleStream] = None
        if sample_rate != SAMPLE_RATE:
            self._stream = soxr.ResampleStream(sample_rate, SAMPLE_RATE, 1, dtype="float32", quality="HQ")

    def process(self, audio: typing.Union[bytes, bytearray, memoryview, npt.NDArray[typing.Any]]) -> bytes:
        """
        Converts the next block of the stream.

        Parameters
        ----------
        audio : typing.Union[bytes, bytearray, memoryview, numpy.ndarray]
            Interleaved PCM bytes, which may end partway through a sample frame, or a NumPy
            array shaped (frames, channels). A 1-D array holds interleaved samples and must
            contain whole frames.

        Returns
        -------
        bytes
            Signed 16-bit little-endian PCM, mono, 16 kHz. The resampler holds back up to about
            120 ms of audio as filter history, depending on the input rate, so the result can be
            shorter than the input or empty. At 8 kHz, the first 100 ms block returns nothing.
            The realtime voice endpoint rejects empty frames, so check before sending.
        """
        return self._convert(self._to_mono(audio), last=False)

    def flush(self) -> bytes:
        """
        Converts the audio held back as filter history and ends the stream.

        Returns
        -------
        bytes
            The rest of the stream as signed 16-bit little-endian PCM, mono, 16 kHz. Can be
            empty.

        Raises
        ------
        ValueError
            The input ended partway through a sample frame. The resampler is reset either way.
        """
        leftover = len(self._pending)
        if leftover:
            self._reset()
            raise ValueError(
                f"The input ended {leftover} bytes into a sample frame of {self._frame_bytes} bytes. "
                "Every frame holds one sample per channel."
            )
        pcm = self._convert(np.zeros(0, dtype=np.float32), last=True)
        self._reset()
        return pcm

    def _to_mono(
        self, audio: typing.Union[bytes, bytearray, memoryview, npt.NDArray[typing.Any]]
    ) -> npt.NDArray[np.float32]:
        if isinstance(audio, np.ndarray):
            if self._pending:
                raise ValueError(
                    "The previous block of bytes ended partway through a sample frame, so the next block "
                    "must be bytes that complete it, not an array."
                )
            samples = self._array_frames(audio)
        else:
            data = self._pending + bytes(audio)
            usable = len(data) - len(data) % self._frame_bytes
            self._pending = data[usable:]
            samples = np.frombuffer(data[:usable], dtype=self._dtype).reshape(-1, self._channels)
        floats = samples.astype(np.float32)
        if self._sample_format == "int16":
            floats /= 32768.0
        mono = floats.mean(axis=1, dtype=np.float32) if self._channels > 1 else floats[:, 0]
        return np.ascontiguousarray(mono)

    def _array_frames(self, audio: npt.NDArray[typing.Any]) -> npt.NDArray[typing.Any]:
        if audio.dtype != self._dtype.newbyteorder("="):
            raise TypeError(
                f'An array passed to a Resampler with sample_format "{self._sample_format}" must have dtype '
                f"{self._sample_format}, got {audio.dtype}."
            )
        if audio.ndim == 1 and audio.shape[0] % self._channels == 0:
            return audio.reshape(-1, self._channels)
        if audio.ndim == 2 and audio.shape[1] == self._channels:
            return audio
        raise ValueError(
            f"Expected an array shaped (frames, {self._channels}), or (frames,) holding whole frames, "
            f"got shape {audio.shape}."
        )

    def _convert(self, mono: npt.NDArray[np.float32], *, last: bool) -> bytes:
        if self._stream is not None:
            mono = self._stream.resample_chunk(mono, last=last)
        return np.clip(np.rint(mono * 32768.0), -32768, 32767).astype("<i2").tobytes()

    def _reset(self) -> None:
        self._pending = b""
        if self._stream is not None:
            self._stream.clear()
