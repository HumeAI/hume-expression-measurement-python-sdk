import asyncio
import contextlib
import json
import math
import os
import pathlib
import re
import shlex
import sys
import typing

import pytest

from hume_expression_measurement.video_helpers import FfmpegNotFoundError, VideoFrame, iter_video_frames

_RUNS_A_SHELL_SCRIPT = pytest.mark.skipif(sys.platform == "win32", reason="runs a shell script in place of ffmpeg")


# The smallest byte sequence the splitter accepts as an image: start, scan header, scan data, end.
def _jpeg(fill: int) -> bytes:
    return bytes([0xFF, 0xD8, 0xFF, 0xDA, 0x00, 0x03, 0x00, fill, fill, 0xFF, 0xD9])


# Writes a script that stands in for ffmpeg. It saves its arguments to args.json, and its body runs
# with the test's directory as `directory` and `images` concatenated as `images`.
def _fake_ffmpeg(directory: pathlib.Path, body: str, images: typing.Sequence[bytes] = ()) -> pathlib.Path:
    (directory / "images.jpg").write_bytes(b"".join(images))
    script = directory / "fake_ffmpeg.py"
    script.write_text(
        "\n".join(
            [
                "import json, os, pathlib, sys, time",
                f"directory = pathlib.Path({str(directory)!r})",
                '(directory / "args.json").write_text(json.dumps(sys.argv[1:]))',
                'images = (directory / "images.jpg").read_bytes()',
                body,
            ]
        )
    )
    path = directory / "ffmpeg"
    # A shell script runs the Python one, because a shebang line cannot hold a path with spaces.
    path.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n')
    path.chmod(0o755)
    return path


def _ffmpeg_args(directory: pathlib.Path) -> typing.List[str]:
    return json.loads((directory / "args.json").read_text())


async def _collect(frames: typing.AsyncIterable[VideoFrame]) -> typing.List[VideoFrame]:
    return [frame async for frame in frames]


@_RUNS_A_SHELL_SCRIPT
async def test_each_image_ffmpeg_writes_is_yielded_with_its_time_in_the_video(tmp_path: pathlib.Path) -> None:
    images = [_jpeg(1), _jpeg(2), _jpeg(3)]
    ffmpeg_path = _fake_ffmpeg(tmp_path, "sys.stdout.buffer.write(images)", images)

    frames = await _collect(iter_video_frames("interview.mp4", fps=2, ffmpeg_path=ffmpeg_path))

    assert frames == [
        VideoFrame(data=images[0], timestamp_ms=0),
        VideoFrame(data=images[1], timestamp_ms=500),
        VideoFrame(data=images[2], timestamp_ms=1000),
    ]


@_RUNS_A_SHELL_SCRIPT
async def test_ffmpeg_is_asked_for_jpeg_images_of_the_file_at_the_requested_rate_and_width(
    tmp_path: pathlib.Path,
) -> None:
    ffmpeg_path = _fake_ffmpeg(tmp_path, "")

    await _collect(iter_video_frames("interview.mp4", fps=2.5, max_width=640, ffmpeg_path=ffmpeg_path))
    args = _ffmpeg_args(tmp_path)

    assert args[args.index("-i") : args.index("-i") + 2] == ["-i", "interview.mp4"]
    assert args[args.index("-vf") + 1] == "fps=2.5,scale='min(iw,640)':-2"
    assert args[-7:] == ["-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "5", "-"]
    assert "-re" not in args


@_RUNS_A_SHELL_SCRIPT
async def test_realtime_paces_ffmpeg_to_the_videos_own_speed(tmp_path: pathlib.Path) -> None:
    ffmpeg_path = _fake_ffmpeg(tmp_path, "")

    await _collect(iter_video_frames("interview.mp4", realtime=True, ffmpeg_path=ffmpeg_path))
    args = _ffmpeg_args(tmp_path)

    assert args[args.index("-i") - 1] == "-re"
    assert args[args.index("-vf") + 1] == "fps=3.0,scale='min(iw,1280)':-2"


@_RUNS_A_SHELL_SCRIPT
async def test_images_written_before_ffmpeg_fails_are_yielded_then_its_error_output_is_raised(
    tmp_path: pathlib.Path,
) -> None:
    body = "\n".join(
        [
            "sys.stdout.buffer.write(images)",
            "sys.stdout.buffer.flush()",
            'sys.stderr.write("interview.mp4: Invalid data found when processing input\\n")',
            "sys.exit(1)",
        ]
    )
    ffmpeg_path = _fake_ffmpeg(tmp_path, body, [_jpeg(1)])
    frames = iter_video_frames("interview.mp4", ffmpeg_path=ffmpeg_path)

    assert await anext(frames) == VideoFrame(data=_jpeg(1), timestamp_ms=0)
    with pytest.raises(
        RuntimeError,
        match=re.escape("ffmpeg exited with code 1: interview.mp4: Invalid data found when processing input"),
    ):
        await anext(frames)


async def test_ffmpeg_that_is_not_at_the_given_path_raises_ffmpeg_not_found_error(tmp_path: pathlib.Path) -> None:
    ffmpeg_path = tmp_path / "missing" / "ffmpeg"

    with pytest.raises(FfmpegNotFoundError, match=re.escape(f"ffmpeg was not found at {ffmpeg_path}. Install it")):
        await _collect(iter_video_frames("interview.mp4", ffmpeg_path=ffmpeg_path))


@_RUNS_A_SHELL_SCRIPT
async def test_closing_the_frames_early_stops_ffmpeg(tmp_path: pathlib.Path) -> None:
    body = "\n".join(
        [
            '(directory / "pid").write_text(str(os.getpid()))',
            "while True:",
            "    sys.stdout.buffer.write(images)",
            "    sys.stdout.buffer.flush()",
            "    time.sleep(0.01)",
        ]
    )
    ffmpeg_path = _fake_ffmpeg(tmp_path, body, [_jpeg(1)])

    async def take_one_frame() -> None:
        async with contextlib.aclosing(iter_video_frames("interview.mp4", ffmpeg_path=ffmpeg_path)) as frames:
            async for _ in frames:
                break

    # Bounded, because closing waits for ffmpeg to exit, which a fake that is never stopped does not.
    await asyncio.wait_for(take_one_frame(), 10)

    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "pid").read_text()), 0)


@pytest.mark.parametrize("fps", [0, -1, 3.5, math.nan])
def test_fps_out_of_range_is_rejected_at_call_time(fps: float) -> None:
    with pytest.raises(ValueError, match="fps must be above 0 and at most 3"):
        iter_video_frames("interview.mp4", fps=fps)


@pytest.mark.parametrize("fps", ["5", True])
def test_fps_that_is_not_a_number_is_rejected_at_call_time(fps: object) -> None:
    with pytest.raises(TypeError, match=f"fps must be a number, got {fps!r}"):
        iter_video_frames("interview.mp4", fps=typing.cast(typing.Any, fps))


def test_max_width_below_16_or_not_an_integer_is_rejected_at_call_time() -> None:
    with pytest.raises(ValueError, match="max_width must be at least 16, got 15"):
        iter_video_frames("interview.mp4", max_width=15)
    with pytest.raises(TypeError, match="max_width must be an integer, got 640.5"):
        iter_video_frames("interview.mp4", max_width=typing.cast(typing.Any, 640.5))
