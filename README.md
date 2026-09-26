# Hume Expression Measurement Python SDK

The official Python client for the [Hume Expression Measurement API](https://dev.hume.ai/docs/expression-measurement/overview). The API measures emotional expression in audio and images. Upload a file to receive every measurement in one response, or stream audio or JPEG images over a WebSocket to receive measurements while the media is still arriving.

The SDK provides synchronous and asynchronous clients for the upload, realtime, and run endpoints, typed models for every request, response, and message, and API key authentication.

1. [Documentation](https://dev.hume.ai/docs/expression-measurement/overview)
2. [Python quickstart](https://dev.hume.ai/docs/expression-measurement/quickstart/python)
3. [Voice upload guide](https://dev.hume.ai/docs/expression-measurement/voice/upload)
4. [Face upload guide](https://dev.hume.ai/docs/expression-measurement/face/upload)
5. [Voice realtime guide](https://dev.hume.ai/docs/expression-measurement/voice/realtime)
6. [Face realtime guide](https://dev.hume.ai/docs/expression-measurement/face/realtime)
7. [Runs guide](https://dev.hume.ai/docs/expression-measurement/runs)
8. [API reference](https://dev.hume.ai/reference/expression-measurement)

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

Keep API keys on a server. To use the API from a web page, relay through your server. See [Authentication](https://dev.hume.ai/docs/expression-measurement/authentication).

## Preparing audio

The voice endpoints accept 16-bit PCM at 16 kHz, mono. The upload endpoint takes it as a WAV file or as headerless little-endian samples, and the realtime endpoint takes headerless samples only. Convert a recording with ffmpeg:

```bash
ffmpeg -i recording.wav -f s16le -acodec pcm_s16le -ac 1 -ar 16000 speech.pcm
```

## Quickstart: upload

The upload endpoints take media you already have and return every measurement in one response.

### Voice

```python
from hume_expression_measurement import ExpressionMeasurementClient

client = ExpressionMeasurementClient()

with open("speech.pcm", "rb") as audio:
    response = client.voice.measure(file=("speech.pcm", audio, "application/octet-stream"))

for measurement in response.measurements:
    top = ", ".join(f"{score.name} {score.probability:.2f}" for score in measurement.expressions[:3])
    print(f"utterance {measurement.utterance_id}, {measurement.audio_start_ms} to {measurement.audio_end_ms} ms: {top}")
```

The tuple gives the part a filename and a content type. `application/octet-stream` labels the file as headerless samples; to upload a WAV file, pass `audio/wav`. The response lists every utterance in `utterances` and every measurement in `measurements`, each with the `utterance_id` it belongs to. A request holds up to 25MB, a little over 13 minutes of audio. The [voice upload guide](https://dev.hume.ai/docs/expression-measurement/voice/upload) covers the request, the response, and every error.

### Face

```python
from hume_expression_measurement import ExpressionMeasurementClient

client = ExpressionMeasurementClient()

with open("photo.jpg", "rb") as image:
    response = client.face.measure(file=[("photo.jpg", image, "image/jpeg")])

for result in response.measurements:
    for face in result.faces:
        top = ", ".join(f"{score.name} {score.probability:.2f}" for score in face.expressions[:3])
        print(f"image {result.frame_id}, face {face.face_id} at {face.bbox}: {top}")
```

`file` is a list, so one request can carry several JPEG images. `measurements` holds one result per image, in the order sent, and `frame_id` is the image's position in the list. The [face upload guide](https://dev.hume.ai/docs/expression-measurement/face/upload) covers image limits, detection settings, and tracking faces across requests.

## Quickstart: realtime

The realtime endpoints take media that is still arriving, such as audio from a live microphone or images from a camera, and send measurements as they are produced.

### Voice

Stream the audio in frames, close the session, and read the results.

```python
from hume_expression_measurement import ExpressionMeasurementClient, SessionClose

client = ExpressionMeasurementClient()

# 3200 bytes is 100 ms of audio at 16 kHz, 16-bit, mono.
FRAME_SIZE = 3200

with client.voice.connect() as socket:
    with open("speech.pcm", "rb") as audio:
        while frame := audio.read(FRAME_SIZE):
            socket.send_audio_frame(frame)
    socket.send_voice_session_close(SessionClose(type="session.close"))

    for message in socket:
        if message.type == "measurement.result":
            top = ", ".join(f"{score.name} {score.probability:.2f}" for score in message.expressions[:3])
            print(f"utterance {message.utterance_id}, {message.audio_start_ms} to {message.audio_end_ms} ms: {top}")
        elif message.type == "error":
            print(f"{message.code}: {message.message}")
        elif message.type == "session.closed":
            print(f"closed after {message.produced.measurements} measurements")
            break
```

The server detects speech, groups it into utterances, and sends a `measurement.result` every 3 seconds of speech while an utterance continues. It accepts the first 30 seconds of audio immediately and then twice real time, so a long recording must be paced. The [voice realtime guide](https://dev.hume.ai/docs/expression-measurement/voice/realtime) covers the send rate, utterances, and the message flow.

### Face

The face endpoint accepts one complete JPEG image per frame and answers each accepted frame with one `measurement.result` listing every face it detected.

```python
from hume_expression_measurement import ExpressionMeasurementClient, SessionClose

client = ExpressionMeasurementClient()

with client.face.connect() as socket:
    with open("photo.jpg", "rb") as image:
        socket.send_image_frame(image.read())
    socket.send_face_session_close(SessionClose(type="session.close"))

    for message in socket:
        if message.type == "measurement.result":
            for face in message.faces:
                top = ", ".join(f"{score.name} {score.probability:.2f}" for score in face.expressions[:3])
                print(f"face {face.face_id} at {face.bbox}: {top}")
        elif message.type == "error":
            print(f"{message.code}: {message.message}")
        elif message.type == "session.closed":
            break
```

`bbox` is `[x0, y0, x1, y1]` in pixels of the submitted image. `face_id` links the same face across frames within a session. The [face realtime guide](https://dev.hume.ai/docs/expression-measurement/face/realtime) covers frame limits, the send rate, and face tracking.

### Handling messages

Iterating a socket yields one typed model per message. Each model has a `type` field, so a chain of `if` statements on `message.type` is enough to dispatch. `recv()` returns the next message when you would rather pull one at a time.

| Message | Voice | Face | Meaning |
|---|---|---|---|
| `session.created` | yes | yes | Sent on connect with the session ID and the default configuration. |
| `session.updated` | yes | yes | Confirms a `session.update` and states the configuration in force. |
| `utterance.start`, `utterance.end` | yes | no | Bracket one continuous stretch of speech. |
| `measurement.result` | yes | yes | Scores for one window of an utterance, or for the faces in one image. |
| `error` | yes | yes | A rejected frame or a failed session, with a `code` and whether it is `retryable`. |
| `session.closed` | yes | yes | The last message. States why the session ended and totals what was received and produced. |

## Async client

`AsyncExpressionMeasurementClient` has the same interface with `async` methods and iteration.

```python
import asyncio

from hume_expression_measurement import AsyncExpressionMeasurementClient, SessionClose


async def main() -> None:
    client = AsyncExpressionMeasurementClient()

    with open("speech.pcm", "rb") as audio:
        response = await client.voice.measure(file=("speech.pcm", audio, "application/octet-stream"))
    print(f"{len(response.measurements)} measurements")

    async with client.voice.connect() as socket:
        with open("speech.pcm", "rb") as audio:
            while frame := audio.read(3200):
                await socket.send_audio_frame(frame)
        await socket.send_voice_session_close(SessionClose(type="session.close"))

        async for message in socket:
            if message.type == "measurement.result":
                print(message.expressions[:3])
            elif message.type == "session.closed":
                break


asyncio.run(main())
```

## Scores

Every measurement is a list of scores, each pairing a `name` with a `probability` from 0 to 1 that the expression is present. Scores are sorted by descending probability, and only scores that pass a threshold are returned, so arrays vary in length and may be empty. Voice results carry `expressions`, each kept against a threshold set for that expression, and `voice_attributes`, kept at 0.85 or above. Face results carry `expressions` and `descriptions` for each face, both kept above 0.1. [Scores](https://dev.hume.ai/docs/expression-measurement/scores) explains how to read them.

## Configuration

These settings apply to both upload and realtime requests.

| Setting | Endpoint | Default | Range |
|---|---|---|---|
| `measurement_timer_ms` | Voice | 3000 | 3000 to 10000 ms of speech between measurements |
| `face.threshold` | Face | 0.9 | 0 to 1, the minimum detection confidence for a face to be measured |
| `face.min_size` | Face | 60 | 1 or more, the shortest side of a face's bounding box in pixels |

On an upload, pass the settings as `config`:

```python
from hume_expression_measurement import FaceFileConfig, FaceFileConfigFace, VoiceFileConfig

response = client.voice.measure(file=audio_file, config=VoiceFileConfig(measurement_timer_ms=5000))
response = client.face.measure(file=images, config=FaceFileConfig(face=FaceFileConfigFace(threshold=0.8, min_size=40)))
```

In a session, send `session.update` before the first frame. The server replies with `session.updated`. The configuration locks once the first frame is accepted, and a field omitted from `session.update` returns to its default.

```python
from hume_expression_measurement import VoiceSessionUpdate

socket.send_voice_session_update(VoiceSessionUpdate(type="session.update", measurement_timer_ms=5000))
```

```python
from hume_expression_measurement import FaceConfigUpdate, FaceSessionUpdate

socket.send_face_session_update(
    FaceSessionUpdate(type="session.update", face=FaceConfigUpdate(threshold=0.8, min_size=40))
)
```

## Runs

Every upload request that is measured and every realtime session is recorded as a run. A run's ID is the `run_id` of an upload response or the `session_id` of a session. The run endpoints return a run's status, configuration, and event log. They do not return measurements, so keep the results you need from the upload response or the session.

```python
from hume_expression_measurement import ExpressionMeasurementClient

client = ExpressionMeasurementClient()

page = client.runs.list(endpoint="voice", mode="realtime")
for summary in page.runs:
    print(summary.run_id, summary.status, summary.metered_seconds)

run = client.runs.get("01926f3a-5b1c-7d2e-8f40-3a9b7c1d2e5f")
print(run.status, run.close_reason)

events = client.runs.list_events("01926f3a-5b1c-7d2e-8f40-3a9b7c1d2e5f")
for event in events.events:
    print(event.seq, event.kind)
```

`runs.list` and `runs.list_events` return one page at a time. Pass a page's `next_cursor` back as `cursor` to fetch the next one; the last page has none. The [runs guide](https://dev.hume.ai/docs/expression-measurement/runs) covers filters, run status, and every event kind.

## Errors

The upload and run methods raise `hume_expression_measurement.core.ApiError` when a request fails. `status_code` holds the HTTP status, and `body` holds the error body, whose `code` says what went wrong.

```python
from hume_expression_measurement.core import ApiError

try:
    response = client.face.measure(file=[("photo.jpg", image, "image/jpeg")])
except ApiError as error:
    print(f"{error.status_code}: {error.body}")
```

Problems during a session arrive as `error` messages rather than exceptions. A rejected frame, such as `invalid_audio_frame`, `invalid_image_frame`, or `rate_limited`, is discarded and the session continues. `config_invalid`, `message_too_large`, a voice `internal_error`, and five consecutive face `internal_error` messages end the session, and `session.closed` follows with `reason` set to `error`. `connect()` raises `ApiError` when the WebSocket handshake fails. A status code of 401 means the API key was rejected.

[Errors](https://dev.hume.ai/docs/expression-measurement/errors) lists every code and whether to retry.

## Contributing

Most of this SDK is generated by [Fern](https://buildwithfern.com) from the API definition, and edits to generated files are overwritten on the next generation. [CONTRIBUTING.md](CONTRIBUTING.md) explains how to build and test the project and how to add code that persists across regenerations. To report a problem, open an issue in this repository.

## License

[MIT](LICENSE)
