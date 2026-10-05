# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

"""
Helpers that turn video into frames for the realtime video endpoint, which accepts one complete
JPEG image per message.

The file source, iter_video_frames, requires ffmpeg, found on PATH or passed as ffmpeg_path.
"""

from ._ffmpeg import FfmpegNotFoundError
from .file import iter_video_frames
from .frame import VideoFrame
from .jpeg import JpegSplitter
from .sender import (
    SessionEndedError,
    VideoErrorReply,
    VideoReply,
    VideoResultReply,
    VideoStream,
    stream_video,
)

__all__ = [
    "FfmpegNotFoundError",
    "JpegSplitter",
    "SessionEndedError",
    "VideoErrorReply",
    "VideoFrame",
    "VideoReply",
    "VideoResultReply",
    "VideoStream",
    "iter_video_frames",
    "stream_video",
]
