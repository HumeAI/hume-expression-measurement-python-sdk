# Hume Expression Measurement Python SDK

The official Python client for the [Hume Expression Measurement API](https://dev.hume.ai/expression-measurement/docs/overview). The API measures emotional expression in audio and images. Upload a file to receive every measurement in one response, or stream audio or JPEG images over a WebSocket to receive measurements while the media is still arriving.

The SDK provides synchronous and asynchronous clients for the upload, realtime, and run endpoints, typed models for every request, response, and message, and API key authentication.

1. [Documentation](https://dev.hume.ai/expression-measurement/docs/overview)
2. [Python quickstart](https://dev.hume.ai/expression-measurement/docs/quickstart/python)
3. [Audio upload guide](https://dev.hume.ai/expression-measurement/docs/audio/upload)
4. [Video upload guide](https://dev.hume.ai/expression-measurement/docs/video/upload)
5. [Audio realtime guide](https://dev.hume.ai/expression-measurement/docs/audio/realtime)
6. [Video realtime guide](https://dev.hume.ai/expression-measurement/docs/video/realtime)
7. [Runs guide](https://dev.hume.ai/expression-measurement/docs/runs)
8. [API reference](https://dev.hume.ai/expression-measurement/reference)

## Requirements

Python 3.10 or later.

## Installation

```bash
pip install hume-expression-measurement
```

## Authentication

Every request and every WebSocket connection carries your API key in the `X-Hume-Api-Key` header. Pass the key to the client, or set the `HUME_API_KEY` environment variable and construct the client without arguments.

```python
from hume_expression_measurement import ExpressionMeasurementClient

client = ExpressionMeasurementClient(api_key="YOUR_API_KEY")
```

Keep API keys on a server. To use the API from a web page, relay through your server. See [Authentication](https://dev.hume.ai/expression-measurement/docs/authentication).

## Preparing audio

The audio endpoints accept 16-bit PCM at 16 kHz, mono. The upload endpoint takes it as a WAV file or as headerless little-endian samples, and the realtime endpoint takes headerless samples only. Convert a recording with ffmpeg:

```bash
ffmpeg -i recording.wav -f s16le -acodec pcm_s16le -ac 1 -ar 16000 speech.pcm
```

To convert in Python instead, or to stream from a microphone, use the [audio helpers](#audio-helpers).

## Quickstart: upload

The upload endpoints take media you already have and return every measurement in one response.

### Audio

```python
from hume_expression_measurement import ExpressionMeasurementClient

client = ExpressionMeasurementClient()

with open("speech.pcm", "rb") as audio:
    response = client.audio.measure(file=("speech.pcm", audio, "application/octet-stream"))

for measurement in response.measurements:
    top = ", ".join(f"{score.name} {score.probability:.2f}" for score in measurement.expressions[:3])
    print(f"utterance {measurement.utterance_id}, {measurement.audio_start_ms} to {measurement.audio_end_ms} ms: {top}")
```

The tuple gives the part a filename and a content type. `application/octet-stream` labels the file as headerless samples; to upload a WAV file, pass `audio/wav`. The response lists every utterance in `utterances` and every measurement in `measurements`, each with the `utterance_id` it belongs to. A request holds up to 25MB, a little over 13 minutes of audio. The [audio upload guide](https://dev.hume.ai/expression-measurement/docs/audio/upload) covers the request, the response, and every error.

### Video

```python
from hume_expression_measurement import ExpressionMeasurementClient

client = ExpressionMeasurementClient()

with open("photo.jpg", "rb") as image:
    response = client.video.measure(file=[("photo.jpg", image, "image/jpeg")])

for result in response.measurements:
    for face in result.faces:
        top = ", ".join(f"{score.name} {score.probability:.2f}" for score in face.expressions[:3])
        print(f"image {result.frame_id}, face {face.face_id} at {face.bbox}: {top}")
```

`file` is a list, so one request can carry several JPEG images. `measurements` holds one result per image, in the order sent, and `frame_id` is the image's position in the list. The [video upload guide](https://dev.hume.ai/expression-measurement/docs/video/upload) covers image limits, detection settings, and tracking faces across requests.

## Quickstart: realtime

The realtime endpoints take media that is still arriving, such as audio from a live microphone or images from a camera, and send measurements as they are produced.

### Audio

Stream the audio in frames, close the session, and read the results.

```python
from hume_expression_measurement import (
    Error,
    ExpressionMeasurementClient,
    SessionClose,
    AudioMeasurementResult,
    AudioSessionClosed,
)

client = ExpressionMeasurementClient()

# 3200 bytes is 100 ms of audio at 16 kHz, 16-bit, mono.
FRAME_SIZE = 3200

with client.audio.connect() as socket:
    with open("speech.pcm", "rb") as audio:
        while frame := audio.read(FRAME_SIZE):
            socket.send_audio_frame(frame)
    socket.send_audio_session_close(SessionClose(type="session.close"))

    for message in socket:
        if isinstance(message, AudioMeasurementResult):
            top = ", ".join(f"{score.name} {score.probability:.2f}" for score in message.expressions[:3])
            print(f"utterance {message.utterance_id}, {message.audio_start_ms} to {message.audio_end_ms} ms: {top}")
        elif isinstance(message, Error):
            print(f"{message.code}: {message.message}")
        elif isinstance(message, AudioSessionClosed):
            print(f"closed after {message.produced.measurements} measurements")
            break
```

The server detects speech, groups it into utterances, and sends a `measurement.result` every 3 seconds of speech while an utterance continues. It accepts the first 30 seconds of audio immediately and then twice real time, so a long recording must be paced. The [audio realtime guide](https://dev.hume.ai/expression-measurement/docs/audio/realtime) covers the send rate, utterances, and the message flow.

### Video

The video endpoint accepts one complete JPEG image per frame and answers each accepted frame with one `measurement.result` listing every face it detected.

```python
from hume_expression_measurement import (
    Error,
    ExpressionMeasurementClient,
    SessionClose,
    VideoMeasurementResult,
    VideoSessionClosed,
)

client = ExpressionMeasurementClient()

with client.video.connect() as socket:
    with open("photo.jpg", "rb") as image:
        socket.send_image_frame(image.read())
    socket.send_video_session_close(SessionClose(type="session.close"))

    for message in socket:
        if isinstance(message, VideoMeasurementResult):
            for face in message.faces:
                top = ", ".join(f"{score.name} {score.probability:.2f}" for score in face.expressions[:3])
                print(f"face {face.face_id} at {face.bbox}: {top}")
        elif isinstance(message, Error):
            print(f"{message.code}: {message.message}")
        elif isinstance(message, VideoSessionClosed):
            break
```

`bbox` is `[x0, y0, x1, y1]` in pixels of the submitted image. `face_id` links the same face across frames within a session. The [video realtime guide](https://dev.hume.ai/expression-measurement/docs/video/realtime) covers frame limits, the send rate, and face tracking. To stream a video file or a live source within the send rate, use the [video helpers](#video-helpers).

### Handling messages

Iterating a socket yields one message at a time, each parsed into the model for its `type`. Check the model with `isinstance`, which also lets a type checker narrow the message to that model's fields. `recv()` returns the next message when you would rather pull one at a time. Every model is exported from `hume_expression_measurement`.

| Message | Audio model | Video model | Meaning |
|---|---|---|---|
| `session.created` | `AudioSessionCreated` | `VideoSessionCreated` | Sent on connect with the session ID and the default configuration. |
| `session.updated` | `AudioSessionUpdated` | `VideoSessionUpdated` | Confirms a `session.update` and states the configuration in force. |
| `utterance.start`, `utterance.end` | `UtteranceStart`, `UtteranceEnd` | None | Bracket one continuous stretch of speech. |
| `measurement.result` | `AudioMeasurementResult` | `VideoMeasurementResult` | Scores for one window of an utterance, or for the faces in one image. |
| `error` | `Error` | `Error` | A rejected frame or a failed session, with a `code` and whether it is `retryable`. |
| `session.closed` | `AudioSessionClosed` | `VideoSessionClosed` | The last message. States why the session ended and totals what was received and produced. |

## Async client

`AsyncExpressionMeasurementClient` has the same interface with `async` methods and iteration.

```python
import asyncio

from hume_expression_measurement import (
    AsyncExpressionMeasurementClient,
    SessionClose,
    AudioMeasurementResult,
    AudioSessionClosed,
)


async def main() -> None:
    client = AsyncExpressionMeasurementClient()

    with open("speech.pcm", "rb") as audio:
        response = await client.audio.measure(file=("speech.pcm", audio, "application/octet-stream"))
    print(f"{len(response.measurements)} measurements")

    async with client.audio.connect() as socket:
        with open("speech.pcm", "rb") as audio:
            while frame := audio.read(3200):
                await socket.send_audio_frame(frame)
        await socket.send_audio_session_close(SessionClose(type="session.close"))

        async for message in socket:
            if isinstance(message, AudioMeasurementResult):
                print(message.expressions[:3])
            elif isinstance(message, AudioSessionClosed):
                break


asyncio.run(main())
```

## Audio helpers

`hume_expression_measurement.audio_helpers` records from a microphone and converts audio at any sample rate from 1 kHz and any channel count into frames for the realtime audio endpoint. Install it with the `audio` extra:

```bash
pip install "hume-expression-measurement[audio]"
```

The extra installs numpy, sounddevice, and soxr. soxr is licensed under the LGPL 2.1 or later and is installed only with this extra. On Linux, sounddevice also needs the PortAudio library, for example `sudo apt-get install libportaudio2` on Debian and Ubuntu. If the extra is missing, importing `hume_expression_measurement.audio_helpers` or calling a helper raises `MissingDependencyError`, whose message gives the install command.

### Microphone

`Microphone.open()` records from the default input device, or the one you pass as `device`, and yields 100 ms frames ready to send. This example records for 10 seconds while printing measurements as they arrive, then closes the session.

```python
import asyncio

from hume_expression_measurement import (
    AsyncExpressionMeasurementClient,
    Error,
    SessionClose,
    AudioMeasurementResult,
    AudioSessionClosed,
)
from hume_expression_measurement.audio_helpers import Microphone


async def main() -> None:
    client = AsyncExpressionMeasurementClient()

    # Opening the microphone first means a missing or busy device fails before a session starts.
    async with Microphone.open() as microphone, client.audio.connect() as socket:

        async def print_measurements() -> None:
            async for message in socket:
                if isinstance(message, AudioMeasurementResult):
                    top = ", ".join(f"{score.name} {score.probability:.2f}" for score in message.expressions[:3])
                    print(f"utterance {message.utterance_id}, {message.audio_start_ms} to {message.audio_end_ms} ms: {top}")
                elif isinstance(message, Error):
                    print(f"{message.code}: {message.message}")
                elif isinstance(message, AudioSessionClosed):
                    break

        receiver = asyncio.create_task(print_measurements())
        try:
            print(f"Recording from {microphone.device_name} for 10 seconds")
            frames_sent = 0
            async for frame in microphone:
                await socket.send_audio_frame(frame)
                frames_sent += 1
                # 100 frames of 100 ms is 10 seconds.
                if frames_sent == 100:
                    break

            await socket.send_audio_session_close(SessionClose(type="session.close"))
            await receiver
        finally:
            receiver.cancel()


asyncio.run(main())
```

The device records at its own sample rate, and the SDK converts its audio to 16 kHz mono. Each `async for` yields only audio recorded after it starts, so a push-to-talk loop that starts a new `async for` for each press never sends audio recorded between presses. If the loop body is too slow and more than 10 seconds of audio builds up, iteration raises a `RuntimeError`. An unknown device, one that is not an input device, or a name that matches several devices raises a `ValueError` that lists the available input devices. On Windows, where each device is listed once per host API, add the host API to the name, for example `"Microphone WASAPI"`, or pass the index. On macOS, the first recording asks permission for the terminal or app running Python; if access is denied, the device records silence.

### WAV files

`iter_wav_frames` reads 8, 16, 24, or 32-bit integer and 32 or 64-bit floating-point WAV at any sample rate from 1 kHz and any channel count and yields 100 ms frames. It replaces the file loop in the [realtime quickstart](#quickstart-realtime):

```python
from hume_expression_measurement.audio_helpers import iter_wav_frames

with client.audio.connect() as socket:
    for frame in iter_wav_frames("recording.wav"):
        socket.send_audio_frame(frame)
    socket.send_audio_session_close(SessionClose(type="session.close"))
```

Frames are produced as fast as the file is read, so pace the sends for a file longer than 30 seconds. Compressed WAV files raise a `ValueError` with an ffmpeg command that converts them.

### Other sources

`Resampler` converts a stream you already have, as bytes or NumPy arrays, block by block. Pass each block to `process`, then call `flush` after the last one. `process` can return an empty result while the filter fills, and the endpoint rejects empty frames, so check before sending. The endpoint also rejects frames longer than 10 seconds, so pass a long recording in blocks of about 100 ms rather than in one call, and pace the sends for a recording longer than 30 seconds.

```python
from hume_expression_measurement.audio_helpers import Resampler

resampler = Resampler(sample_rate=48000, channels=2, sample_format="int16")
for block in blocks:
    if pcm := resampler.process(block):
        socket.send_audio_frame(pcm)
if pcm := resampler.flush():
    socket.send_audio_frame(pcm)
```

## Video helpers

`hume_expression_measurement.video_helpers` streams JPEG frames to the realtime video endpoint with the async client. `stream_video` sends frames within the send rate, handles rejected frames, and pairs every reply with the frame it answers. `iter_video_frames` reads frames from a video file through ffmpeg, and `JpegSplitter` splits a stream of concatenated JPEG images, such as MJPEG, into single frames. The helpers need no extra.

### Streaming frames

`stream_video` takes a socket and any async iterable of `VideoFrame`s, each a JPEG image as `data` with its time in milliseconds as `timestamp_ms`, and yields one reply per frame, in the order of the frames. A reply holds the frame's `timestamp_ms` and either the `result` that measured it or the `error` that rejected it, with the other set to None. This example measures a video file:

```python
import asyncio

from hume_expression_measurement import AsyncExpressionMeasurementClient
from hume_expression_measurement.video_helpers import iter_video_frames, stream_video


async def main() -> None:
    client = AsyncExpressionMeasurementClient()

    async with client.video.connect() as socket:
        stream = stream_video(socket, iter_video_frames("interview.mp4", fps=2))
        async for reply in stream:
            if reply.result is not None:
                for face in reply.result.faces:
                    top = ", ".join(f"{score.name} {score.probability:.2f}" for score in face.expressions[:3])
                    print(f"{reply.timestamp_ms:.0f} ms, face {face.face_id}: {top}")
            else:
                print(f"{reply.timestamp_ms:.0f} ms: {reply.error.code}")
        if stream.closed is not None:
            print(f"{stream.closed.produced.measurements} faces measured")


asyncio.run(main())
```

The server accepts the first 8 frames immediately and then 4 per second. `stream_video` sends the first 7 at once, keeping one in reserve for uneven network timing, and paces the rest. When the server rejects a frame with `rate_limited`, `stream_video` halves its send rate, down to one frame every 2 seconds, and raises it again gradually while frames are accepted, so a tighter limit than expected costs only a few rejected frames. What happens to the rejected frame depends on the source:

1. From a recording, the default, it sends the frame again once the lower rate allows, so every frame is measured. It also sends a frame once more after a retryable `internal_error`.
2. From a live source, with `live=True`, it yields the rejection and moves on, so results keep up with the source. A live frame that arrives when the send rate has no room is not sent at all, and `stream.dropped_frames` counts it.

A frame over the 2 MB message limit would end the session, so it is never sent and is yielded as a `message_too_large` error. Frames that are not valid JPEGs or that exceed 8,294,400 pixels are rejected by the server with `invalid_image_frame`, and the session continues.

When the frames run out, `stream_video` closes the session and yields the remaining replies, and the server closes the connection. `stream.closed` then holds the `session.closed` message with the session's totals. If the session ends before the frames do, because of a session-ending error, an idle timeout, or a dropped connection, the loop raises a `SessionEndedError`. Its `closed` holds the `session.closed` message when one arrived, and its `code` names the error that ended the session, if there was one. A session cannot be resumed, so each stream needs its own socket from `client.video.connect()`, and calling `stream_video` again with a socket it has already used raises a `RuntimeError`.

Leaving the loop early, or an error from the frames, makes `stream_video` ask the server to close the session and stop the frames. After a `break`, this happens once the stream's iterator is garbage collected. To make it happen as soon as the block exits, wrap the iterator in `contextlib.aclosing`:

```python
async with contextlib.aclosing(aiter(stream_video(socket, frames))) as replies:
    async for reply in replies:
        ...
```

`stream_video` reads every message from the socket while it runs, so read messages from the replies instead. The configuration locks at the first frame, so send any `session.update` before streaming. Frames are sent only while the loop asks for the next reply, so keep the loop body short.

### Video files

`iter_video_frames` runs [ffmpeg](https://ffmpeg.org), which must be installed: `brew install ffmpeg` on macOS, `sudo apt install ffmpeg` on Debian or Ubuntu, or `winget install Gyan.FFmpeg` on Windows. It reads any format ffmpeg can decode and yields frames whose `timestamp_ms` is their position in the video. If ffmpeg is not on `PATH` or at `ffmpeg_path`, iteration raises an `FfmpegNotFoundError`, a `FileNotFoundError` whose message says how to install ffmpeg. If ffmpeg fails, iteration raises a `RuntimeError` that includes ffmpeg's output. ffmpeg starts when the first frame is requested, so with `stream_video` these errors are raised from the loop after the session has started, and `stream_video` has asked the server to close the session before they reach you.

| Option | Default | Purpose |
|---|---|---|
| `fps` | 3 | Frames taken per second of video, above 0 and at most 4. |
| `max_width` | 1280 | Wider frames are scaled down to this width, keeping their shape. Narrower frames are left as they are. |
| `realtime` | False | Produce frames no faster than the video plays, as a live source would. By default they are produced as fast as they are sent. |
| `ffmpeg_path` | `ffmpeg` on `PATH` | The ffmpeg executable to run. |

`stream_video` stops ffmpeg when the stream ends. If you iterate `iter_video_frames` yourself, leaving the loop early stops ffmpeg once the generator is garbage collected, or as soon as the block exits if you wrap the generator in `contextlib.aclosing`.

### Live sources

`stream_video` measures frames from any source you can turn into an async iterable, such as a camera your application already captures. Encode each frame as a JPEG, give it a `timestamp_ms`, and pass `live=True`. A camera usually produces more frames than the 4 per second the endpoint accepts; `stream_video` sends what the send rate allows and counts the rest in `stream.dropped_frames`, so capturing fewer frames saves encoding work. Keep frames around 1280 pixels wide.

`JpegSplitter` turns a byte stream of concatenated JPEG images, such as ffmpeg's `image2pipe` output or the body of an MJPEG stream, into single images. Pass each chunk to `push`, which returns the images completed so far and holds back a partial one. This generator yields frames from such a stream, timed from when it starts:

```python
import time
import typing

from hume_expression_measurement.video_helpers import JpegSplitter, VideoFrame, stream_video


async def frames_from(mjpeg: typing.AsyncIterable[bytes]) -> typing.AsyncIterator[VideoFrame]:
    splitter = JpegSplitter()
    start = time.monotonic()
    async for chunk in mjpeg:
        for data in splitter.push(chunk):
            yield VideoFrame(data=data, timestamp_ms=(time.monotonic() - start) * 1000)


async for reply in stream_video(socket, frames_from(mjpeg), live=True):
    print(reply.timestamp_ms, reply.error.code if reply.error is not None else len(reply.result.faces))
```

## Scores

Every measurement contains lists of scores, each pairing a `name` with a `probability` from 0 to 1. Scores are sorted by descending probability, and each list includes only the scores that pass its cutoff, so lists vary in length and may be empty. Audio results carry `expressions`, each included when judged present against a threshold set for that expression, and `voice_attributes`, included at 0.725 or above. Video results carry `expressions` and `descriptions` for each face, both included above 0.1. What `probability` denotes differs from list to list. [Scores](https://dev.hume.ai/expression-measurement/docs/scores) explains how to read them.

## Configuration

These settings apply to both upload and realtime requests.

| Setting | Endpoint | Default | Range |
|---|---|---|---|
| `measurement_timer_ms` | Audio | 3000 | 3000 to 10000 ms of speech between measurements |
| `face.threshold` | Video | 0.9 | 0 to 1, the minimum detection confidence for a face to be measured |
| `face.min_size` | Video | 60 | 1 or more, the shortest side of a face's bounding box in pixels |

On an upload, pass the settings as `config`:

```python
from hume_expression_measurement import AudioFileConfig, VideoFileConfig, VideoFileConfigFace

response = client.audio.measure(file=audio_file, config=AudioFileConfig(measurement_timer_ms=5000))
response = client.video.measure(file=images, config=VideoFileConfig(face=VideoFileConfigFace(threshold=0.8, min_size=40)))
```

In a session, send `session.update` before the first frame. The server replies with `session.updated`. The configuration locks once the first frame is accepted, and a field omitted from `session.update` returns to its default.

```python
from hume_expression_measurement import AudioSessionUpdate

socket.send_audio_session_update(AudioSessionUpdate(type="session.update", measurement_timer_ms=5000))
```

```python
from hume_expression_measurement import FaceConfigUpdate, VideoSessionUpdate

socket.send_video_session_update(
    VideoSessionUpdate(type="session.update", face=FaceConfigUpdate(threshold=0.8, min_size=40))
)
```

## Runs

Every upload request that is measured and every realtime session is recorded as a run. A run's ID is the `run_id` of an upload response or the `session_id` of a session. The run endpoints return a run's status, configuration, and event log. They do not return measurements, so keep the results you need from the upload response or the session.

```python
from hume_expression_measurement import ExpressionMeasurementClient

client = ExpressionMeasurementClient()

page = client.runs.list(endpoint="audio", mode="realtime")
for summary in page.runs:
    print(summary.run_id, summary.status, summary.metered_seconds)

run = client.runs.get("01926f3a-5b1c-7d2e-8f40-3a9b7c1d2e5f")
print(run.status, run.close_reason)

events = client.runs.list_events("01926f3a-5b1c-7d2e-8f40-3a9b7c1d2e5f")
for event in events.events:
    print(event.seq, event.kind)
```

`runs.list` and `runs.list_events` return one page at a time. Pass a page's `next_cursor` back as `cursor` to fetch the next one; the last page has none. The [runs guide](https://dev.hume.ai/expression-measurement/docs/runs) covers filters, run status, and every event kind.

## Errors

The upload and run methods raise `hume_expression_measurement.core.ApiError` when a request fails. `status_code` holds the HTTP status, and `body` holds the error body. For every status except 401, `body` is an `ErrorBody` whose `code` says what went wrong; a 401 carries only a `message`.

```python
from hume_expression_measurement.core import ApiError

try:
    response = client.video.measure(file=[("photo.jpg", image, "image/jpeg")])
except ApiError as error:
    print(f"{error.status_code}: {error.body}")
```

Problems during a session arrive as `error` messages rather than exceptions. A rejected frame, such as `invalid_audio_frame`, `invalid_image_frame`, or `rate_limited`, is discarded and the session continues. `config_invalid`, `message_too_large`, an audio `internal_error`, and five consecutive video `internal_error` messages end the session, and `session.closed` follows with `reason` set to `error`. `connect()` raises `ApiError` when the WebSocket handshake fails. A status code of 401 means the API key was rejected.

[Errors](https://dev.hume.ai/expression-measurement/docs/errors) lists every code and whether to retry.

## Contributing

Most of this SDK is generated by [Fern](https://buildwithfern.com) from the API definition, and edits to generated files are overwritten on the next generation. [CONTRIBUTING.md](CONTRIBUTING.md) explains how to build and test the project and how to add code that persists across regenerations. To report a problem, open an issue in this repository.

## License

[MIT](LICENSE)
