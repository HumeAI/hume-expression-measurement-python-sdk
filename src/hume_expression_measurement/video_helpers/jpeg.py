# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

import enum
import typing

_MARKER = 0xFF
_SOI = 0xD8
_EOI = 0xD9
_SOS = 0xDA
_TEM = 0x01
_RST0 = 0xD0
_RST7 = 0xD7
# Markers from 0x02 to 0xBF are reserved and never appear in a valid image.
_FIRST_SEGMENT_MARKER = 0xC0
_START_MARKER = bytes([_MARKER, _SOI])


class _State(enum.Enum):
    SEEK = enum.auto()
    MARKER = enum.auto()
    SCAN = enum.auto()


class JpegSplitter:
    """
    Splits a stream of concatenated JPEG images, such as ffmpeg's `image2pipe` output or the body
    of an MJPEG stream, into single images ready to send with `send_image_frame`.

    Pass successive chunks of the stream to `push`, which returns the images completed so far and
    holds back a partial one until the rest arrives. Images are found by walking the JPEG marker
    structure rather than by searching for end markers, so a thumbnail embedded in EXIF data does
    not split an image, and progressive images with several scans come out whole.

    Bytes before an image's start marker are discarded. An image whose structure is broken is
    dropped, and splitting resumes at the next start marker.

    Examples
    --------
    import time

    from hume_expression_measurement.video_helpers import JpegSplitter, VideoFrame, stream_video

    async def frames_from(mjpeg):
        splitter = JpegSplitter()
        start = time.monotonic()
        async for chunk in mjpeg:
            for data in splitter.push(chunk):
                yield VideoFrame(data=data, timestamp_ms=round((time.monotonic() - start) * 1000))

    async for reply in stream_video(socket, frames_from(mjpeg), live=True):
        print(reply.timestamp_ms, reply.error.code if reply.error is not None else len(reply.result.faces))
    """

    def __init__(self) -> None:
        # Starts at the current image's start marker once one is found. Appending to a bytearray
        # copies only the new chunk, whereas rebuilding an immutable buffer on each push copies
        # everything held back, which makes splitting a large image from small chunks quadratic.
        self._buffer = bytearray()
        # Where parsing resumes in the buffer. It can pass the end of the buffer while a segment
        # that need not be read is still arriving.
        self._offset = 0
        self._state = _State.SEEK

    def push(self, chunk: typing.Union[bytes, bytearray, memoryview]) -> typing.List[bytes]:
        """
        Adds the next chunk of the stream.

        Parameters
        ----------
        chunk : typing.Union[bytes, bytearray, memoryview]
            The next bytes of the stream, of any length.

        Returns
        -------
        typing.List[bytes]
            The images this chunk completed, in stream order, each one complete JPEG. A partial
            image left at the end of the stream is never returned.
        """
        self._buffer += chunk
        images: typing.List[bytes] = []
        while self._step(images):
            pass
        return images

    # Advances the parse once, returning False when more input is needed.
    def _step(self, images: typing.List[bytes]) -> bool:
        buffer = self._buffer
        if self._state is _State.SEEK:
            start = buffer.find(_START_MARKER)
            if start < 0:
                # A marker byte at the end may be the first half of a start marker.
                keep = 1 if buffer and buffer[-1] == _MARKER else 0
                del buffer[: len(buffer) - keep]
                return False
            del buffer[:start]
            self._offset = 2
            self._state = _State.MARKER
            return True

        if self._state is _State.MARKER:
            at = self._offset
            if at >= len(buffer):
                return False
            if buffer[at] != _MARKER:
                self._resync()
                return True
            # Any number of 0xFF fill bytes may precede a marker.
            while at + 1 < len(buffer) and buffer[at + 1] == _MARKER:
                at += 1
            if at + 1 >= len(buffer):
                self._offset = at
                return False
            marker = buffer[at + 1]
            if marker == _EOI:
                images.append(bytes(buffer[: at + 2]))
                del buffer[: at + 2]
                self._state = _State.SEEK
                return True
            if marker == _SOI:
                # The previous image was cut short, and a new one starts here.
                del buffer[:at]
                self._offset = 2
                return True
            if marker == _TEM or _RST0 <= marker <= _RST7:
                self._offset = at + 2
                return True
            if marker < _FIRST_SEGMENT_MARKER:
                self._resync()
                return True
            if at + 4 > len(buffer):
                self._offset = at
                return False
            length = buffer[at + 2] << 8 | buffer[at + 3]
            # The length counts its own two bytes.
            if length < 2:
                self._resync()
                return True
            self._offset = at + 2 + length
            if marker == _SOS:
                self._state = _State.SCAN
            return True

        # In entropy-coded data, 0xFF is followed by 0x00 for a literal 0xFF byte, by a restart
        # marker, or by fill bytes. Anything else is a marker that ends the scan.
        at = self._offset
        while True:
            at = buffer.find(_MARKER, at)
            if at < 0:
                self._offset = max(self._offset, len(buffer))
                return False
            if at + 1 >= len(buffer):
                self._offset = at
                return False
            following = buffer[at + 1]
            if following == 0x00 or _RST0 <= following <= _RST7:
                at += 2
            elif following == _MARKER:
                at += 1
            else:
                self._offset = at
                self._state = _State.MARKER
                return True

    # Drops the current image and looks for the next start marker after its own.
    def _resync(self) -> None:
        del self._buffer[:1]
        self._offset = 0
        self._state = _State.SEEK
