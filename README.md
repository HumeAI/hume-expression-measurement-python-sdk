# Hume Expression Measurement Python SDK

The official Python client for the [Hume Expression Measurement API](https://hume-expression-measurement.docs.buildwithfern.com). The API measures emotional expression in speech and faces: stream audio or JPEG images over a WebSocket and receive scored measurements while the stream is still arriving.

The SDK provides synchronous and asynchronous clients for both realtime endpoints, typed models for every message, and API key authentication.

1. [Documentation](https://hume-expression-measurement.docs.buildwithfern.com)
2. [Voice realtime guide](https://hume-expression-measurement.docs.buildwithfern.com/docs/expression-measurement/voice/realtime)
3. [Face realtime guide](https://hume-expression-measurement.docs.buildwithfern.com/docs/expression-measurement/face/realtime)
4. [API reference](https://hume-expression-measurement.docs.buildwithfern.com/reference/expression-measurement)

## Requirements

Python 3.10 or later.

## Installation

```bash
pip install hume-expression-measurement
```

## Authentication

Every connection carries your API key in the `X-Hume-Api-Key` header. Pass the key to the client, or set the `HUME_API_KEY` environment variable and construct the client without arguments.

```python
from hume_expression_measurement import ExpressionMeasurementClient

client = ExpressionMeasurementClient(api_key="YOUR_API_KEY")
```

Keep API keys on a server. See [Authentication](https://hume-expression-measurement.docs.buildwithfern.com/docs/expression-measurement/authentication) for how keys and access tokens work.

## Quickstart: voice

The voice endpoint accepts raw audio as signed 16-bit little-endian PCM, mono, at 16 kHz, with no header. Convert a recording with ffmpeg:

```bash
ffmpeg -i recording.wav -f s16le -ac 1 -ar 16000 speech.pcm
```

Stream the file in frames, close the session, and read the results.

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

The server detects speech, groups it into utterances, and sends a `measurement.result` every 3 seconds of speech while an utterance continues. It accepts the first 30 seconds of audio immediately and then twice real time, so a long recording must be paced. The [voice guide](https://hume-expression-measurement.docs.buildwithfern.com/docs/expression-measurement/voice/realtime) covers the send rate, utterances, and the message flow.

### Async client

`AsyncExpressionMeasurementClient` has the same interface with `async` methods and iteration.

```python
import asyncio

from hume_expression_measurement import AsyncExpressionMeasurementClient, SessionClose


async def main() -> None:
    client = AsyncExpressionMeasurementClient()
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

## Quickstart: face

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

`bbox` is `[x0, y0, x1, y1]` in pixels of the submitted image. `face_id` links the same face across frames within a session. The [face guide](https://hume-expression-measurement.docs.buildwithfern.com/docs/expression-measurement/face/realtime) covers frame limits, the send rate, and face tracking.

## Handling messages

Iterating a socket yields one typed model per message. Each model has a `type` field, so a chain of `if` statements on `message.type` is enough to dispatch. `recv()` returns the next message when you would rather pull one at a time.

| Message | Voice | Face | Meaning |
|---|---|---|---|
| `session.created` | yes | yes | Sent on connect with the session ID and the default configuration. |
| `session.updated` | yes | yes | Confirms a `session.update` and states the configuration in force. |
| `utterance.start`, `utterance.end` | yes | no | Bracket one continuous stretch of speech. |
| `measurement.result` | yes | yes | Scores for one window of speech, or for the faces in one image. |
| `error` | yes | yes | A rejected frame or a failed session, with a `code` and whether it is `retryable`. |
| `session.closed` | yes | yes | The last message. States why the session ended and totals what was received and produced. |

Every measurement is a list of scores, each pairing a `name` with a `probability` from 0 to 1. Only scores above 0.1 are returned, sorted by descending probability, so arrays vary in length and may be empty. Voice results carry `expressions` and `voice_attributes`. Face results carry `expressions` and `descriptions` for each face. [Scores](https://hume-expression-measurement.docs.buildwithfern.com/docs/expression-measurement/scores) explains how to read them.

## Configuration

To change a setting, send `session.update` before the first frame. The server replies with `session.updated`. The configuration locks once the first frame is accepted, and a field omitted from `session.update` returns to its default.

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

| Setting | Endpoint | Default | Range |
|---|---|---|---|
| `measurement_timer_ms` | Voice | 3000 | 3000 to 10000 ms of speech between measurements |
| `face.threshold` | Face | 0.9 | 0 to 1, the minimum detection confidence for a face to be measured |
| `face.min_size` | Face | 60 | 1 or more, the shortest side of a face's bounding box in pixels |

## Errors

Problems during a session arrive as `error` messages rather than exceptions. A rejected frame, such as `invalid_audio_frame`, `invalid_image_frame`, or `rate_limited`, is discarded and the session continues. `config_invalid`, `message_too_large`, and a voice `internal_error` end the session, and `session.closed` follows with `reason` set to `error`.

`connect()` raises `hume_expression_measurement.core.ApiError` when the WebSocket handshake fails. A status code of 401 means the API key was rejected.

[Errors](https://hume-expression-measurement.docs.buildwithfern.com/docs/expression-measurement/errors) lists every code and whether the session continues.

## Contributing

Most of this SDK is generated by [Fern](https://buildwithfern.com) from the API definition, and edits to generated files are overwritten on the next generation. [CONTRIBUTING.md](CONTRIBUTING.md) explains how to build and test the project and how to add code that persists across regenerations. To report a problem, open an issue in this repository.

## License

[MIT](LICENSE)
