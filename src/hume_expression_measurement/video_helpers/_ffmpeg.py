# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

import asyncio
import contextlib
import numbers
import operator
import os
import typing

from .jpeg import JpegSplitter

# The realtime video endpoint accepts 4 frames per second once its initial allowance is spent.
MAX_FPS = 4

# ffmpeg's scaler fails at a width of 1, and a narrower frame is too small to hold a measurable face.
MIN_WIDTH = 16

# Lower is better quality. At 5, a 1280-wide frame is tens of kilobytes, far below the endpoint's
# 2 MB message limit.
_JPEG_QUALITY = 5

# A damaged input can make ffmpeg log an error for every frame, so only the end is kept.
_MAX_STDERR_BYTES = 16 * 1024

_READ_BYTES = 64 * 1024

_INSTALL_HINT = (
    "Install it with `brew install ffmpeg` on macOS, `sudo apt install ffmpeg` on Debian or Ubuntu, or "
    "`winget install Gyan.FFmpeg` on Windows, or pass the executable's location as ffmpeg_path."
)


class FfmpegNotFoundError(FileNotFoundError):
    """
    Raised when the ffmpeg executable a video helper runs is not at the given path or on PATH.

    The message says how to install it.
    """


def check_integer(name: str, value: int) -> int:
    # operator.index also accepts NumPy integers, which are not subclasses of int.
    try:
        return operator.index(value)
    except TypeError:
        raise TypeError(f"{name} must be an integer, got {value!r}.") from None


def check_fps(fps: float) -> float:
    # A bool is a number in Python, so True would otherwise run at 1 fps.
    if isinstance(fps, bool) or not isinstance(fps, numbers.Real):
        raise TypeError(f"fps must be a number, got {fps!r}.")
    # Written so that NaN fails the check.
    if not 0 < fps <= MAX_FPS:
        raise ValueError(f"fps must be above 0 and at most {MAX_FPS}, got {fps}.")
    # A plain float keeps NumPy scalars out of the frame timestamps.
    return float(fps)


def check_max_width(max_width: int) -> int:
    max_width = check_integer("max_width", max_width)
    if max_width < MIN_WIDTH:
        raise ValueError(f"max_width must be at least {MIN_WIDTH}, got {max_width}.")
    return max_width


def ffmpeg_args(input_args: typing.List[str], fps: float, max_width: int) -> typing.List[str]:
    """Arguments that read `input_args` and write JPEG images to stdout at `fps`, no wider than `max_width`."""
    return [
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        *input_args,
        "-an",
        "-vf",
        # Quoted because the filter graph would otherwise split the expression at its comma.
        f"fps={fps},scale='min(iw,{max_width})':-2",
        "-f",
        "image2pipe",
        "-c:v",
        "mjpeg",
        "-q:v",
        str(_JPEG_QUALITY),
        "-",
    ]


async def read_images(
    ffmpeg_path: typing.Union[str, "os.PathLike[str]"], args: typing.List[str]
) -> typing.AsyncGenerator[bytes, None]:
    """Runs ffmpeg and yields each JPEG image it writes to stdout. Closing the generator stops the process."""
    try:
        process = await asyncio.create_subprocess_exec(
            ffmpeg_path,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as err:
        path = os.fspath(ffmpeg_path)
        where = "on PATH" if path == "ffmpeg" else f"at {path}"
        raise FfmpegNotFoundError(f"ffmpeg was not found {where}. {_INSTALL_HINT}") from err
    stdout = typing.cast(asyncio.StreamReader, process.stdout)
    stderr_tail = asyncio.ensure_future(_read_tail(typing.cast(asyncio.StreamReader, process.stderr)))
    splitter = JpegSplitter()
    try:
        while True:
            chunk = await stdout.read(_READ_BYTES)
            if not chunk:
                break
            for image in splitter.push(chunk):
                yield image
    finally:
        if not stdout.at_eof():
            # Not SIGTERM, which ffmpeg does not act on while it is blocked writing to a full pipe.
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            # Read to the end, because asyncio stops reading a pipe whose unread output fills its
            # buffer, and such a pipe never closes. That would leave the process's transport open,
            # and on some Python versions keep wait() from returning.
            await stdout.read()
        output = (await stderr_tail).decode(errors="replace").strip()
        code = await process.wait()
    if code != 0:
        reason = f"exited with code {code}" if code > 0 else f"was stopped by signal {-code}"
        raise RuntimeError(f"ffmpeg {reason}: {output}" if output else f"ffmpeg {reason}.")


async def _read_tail(stream: asyncio.StreamReader) -> bytes:
    tail = b""
    while True:
        chunk = await stream.read(_READ_BYTES)
        if not chunk:
            return tail
        tail = (tail + chunk)[-_MAX_STDERR_BYTES:]
