# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

import operator
import typing

SAMPLE_RATE = 16000
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000

# The realtime voice endpoint rejects a frame holding more than 10 seconds of audio.
MAX_FRAME_MS = 10_000


def check_integer(name: str, value: int) -> None:
    # operator.index also accepts NumPy integers, which are not subclasses of int.
    try:
        operator.index(value)
    except TypeError:
        raise TypeError(f"{name} must be an integer, got {value!r}.") from None


def check_frame_ms(frame_ms: int) -> None:
    check_integer("frame_ms", frame_ms)
    if not 1 <= frame_ms <= MAX_FRAME_MS:
        raise ValueError(f"frame_ms must be between 1 and {MAX_FRAME_MS}, got {frame_ms}.")


class FrameBuffer:
    """Cuts a stream of 16 kHz mono s16le bytes into frames of a fixed duration."""

    def __init__(self, frame_ms: int) -> None:
        check_frame_ms(frame_ms)
        self._frame_bytes = frame_ms * BYTES_PER_MS
        self._buffer = bytearray()

    def push(self, pcm: bytes) -> typing.List[bytes]:
        self._buffer += pcm
        frames = []
        while len(self._buffer) >= self._frame_bytes:
            frames.append(bytes(self._buffer[: self._frame_bytes]))
            del self._buffer[: self._frame_bytes]
        return frames

    def finish(self) -> typing.Optional[bytes]:
        rest = bytes(self._buffer)
        self._buffer.clear()
        return rest or None
