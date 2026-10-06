# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

import contextlib
import os
import typing

from ._ffmpeg import check_fps, check_max_width, ffmpeg_args, read_images
from .frame import VideoFrame


def iter_video_frames(
    path: typing.Union[str, "os.PathLike[str]"],
    *,
    fps: float = 3,
    max_width: int = 1280,
    realtime: bool = False,
    ffmpeg_path: typing.Union[str, "os.PathLike[str]"] = "ffmpeg",
) -> typing.AsyncGenerator[VideoFrame, None]:
    """
    Reads a video file with ffmpeg and yields its frames as JPEG images ready to send with
    `send_image_frame`.

    Frames are taken `fps` times per second of video, and each frame's `timestamp_ms` is its
    position in the video, to the nearest millisecond. Any format ffmpeg can decode works. ffmpeg
    must be installed; it is found on PATH unless `ffmpeg_path` is given.

    ffmpeg starts when iteration starts, so errors about the file are raised by the first
    `__anext__()` rather than by this call. Closing the generator stops ffmpeg. A loop left early
    closes it once the generator is garbage collected; wrap it in `contextlib.aclosing` to close
    it as soon as the block exits.

    Parameters
    ----------
    path : typing.Union[str, os.PathLike[str]]
        Path to the video file.

    fps : float
        Frames per second to take from the video, above 0 and at most 3.

    max_width : int
        Frames wider than this many pixels are scaled down to it, keeping their shape. At least 16.

    realtime : bool
        Produce frames no faster than the video plays, as a live source would. By default they are
        produced as fast as they are consumed.

    ffmpeg_path : typing.Union[str, os.PathLike[str]]
        Path to the ffmpeg executable. By default it is found on PATH.

    Yields
    ------
    VideoFrame
        One frame as a JPEG image, with its position in the video in milliseconds.

    Raises
    ------
    TypeError
        fps is not a number, or max_width is not an integer.

    ValueError
        fps or max_width is out of range.

    FfmpegNotFoundError
        From iteration, if ffmpeg is not on PATH or not at `ffmpeg_path`.

    RuntimeError
        From iteration, if ffmpeg fails, for example because the file does not exist or is not a
        video. The message includes ffmpeg's error output.

    Examples
    --------
    from hume_expression_measurement.video_helpers import iter_video_frames

    async for frame in iter_video_frames("interview.mp4", fps=2):
        print(frame.timestamp_ms, len(frame.data))
    """
    fps = check_fps(fps)
    input_args = ["-re"] if realtime else []
    input_args += ["-i", os.fspath(path)]
    args = ffmpeg_args(input_args, fps, check_max_width(max_width))
    return _read_frames(ffmpeg_path, args, fps)


async def _read_frames(
    ffmpeg_path: typing.Union[str, "os.PathLike[str]"], args: typing.List[str], fps: float
) -> typing.AsyncGenerator[VideoFrame, None]:
    # Closing this generator does not close one it iterates, which would leave ffmpeg running until
    # the inner generator is garbage collected.
    async with contextlib.aclosing(read_images(ffmpeg_path, args)) as images:
        index = 0
        async for data in images:
            yield VideoFrame(data=data, timestamp_ms=round(index * 1000 / fps))
            index += 1
