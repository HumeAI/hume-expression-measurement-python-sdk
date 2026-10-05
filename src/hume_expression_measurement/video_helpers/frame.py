# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

import dataclasses


@dataclasses.dataclass(frozen=True)
class VideoFrame:
    """One video frame, ready to send with `send_image_frame`."""

    data: bytes
    """One complete JPEG image."""

    timestamp_ms: float
    """
    When the frame was captured, in milliseconds: media time for a file, and time since capture
    started for a live source.
    """
