# Reference
## audio
<details><summary><code>client.audio.<a href="src/hume_expression_measurement/audio/client.py">measure</a>(...) -> AudioFileResponse</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Send the audio as the `file` part and, to change the defaults, settings as a JSON `config` part. The response holds every utterance found in the audio and every measurement taken, the same results a realtime session would stream for the same audio.

## How measurements are produced

The server detects when speech starts and stops. Each continuous stretch of speech is an **utterance**, listed in `utterances` with its span in the audio.

- **During speech:** a measurement is taken every `measurement_timer_ms` milliseconds of speech, with `trigger` set to `interval`.
- **When speech ends:** a final measurement is taken with `trigger` set to `speech_ended`, unless the previous measurement ended less than one second earlier. Every utterance gets at least one measurement, and audio that ends during speech ends the utterance.
- **Span:** each measurement covers the most recent 10 seconds of the utterance, or the whole utterance if it is shorter, so consecutive measurements can overlap. `audio_start_ms` and `audio_end_ms` give the span, counted from the start of the audio.

## Limits

| Limit | Value |
|---|---|
| Request size | 25MB, the whole multipart body |
| Audio format | 16-bit PCM, 16 kHz, mono, as a WAV file or headerless samples |

The server does not convert audio, so send it in the format above.

- **Too large:** a request over the size limit is rejected with `audio_too_long`.
- **Unsupported formats:** MP3, FLAC, Ogg, MP4 or M4A, and RIFF files other than WAV, are rejected with `unsupported_media_type`. Convert them before sending.
- **Wrong WAV format:** a WAV file at another sample rate, channel count or sample format, one that uses the extensible WAV header, or one that is truncated or malformed is rejected with `audio_decode_failed`.
- **Anything else** is read as headerless samples rather than rejected, so check the format before sending. An empty file, or one with an odd number of bytes, is rejected with `audio_decode_failed`.
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```python
from hume_expression_measurement import ExpressionMeasurementClient
from hume_expression_measurement.environment import ExpressionMeasurementClientEnvironment

client = ExpressionMeasurementClient(
    api_key="<value>",
    environment=ExpressionMeasurementClientEnvironment.PRODUCTION,
)

client.audio.measure(
    file="example_file",
)

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**file:** `core.File` — The audio, as 16-bit PCM at 16 kHz, mono: either a WAV file or the headerless little-endian samples. A WAV file at another sample rate, channel count or sample format is rejected rather than converted.
    
</dd>
</dl>

<dl>
<dd>

**config:** `typing.Optional[AudioFileConfig]` 
    
</dd>
</dl>

<dl>
<dd>

**request_options:** `typing.Optional[RequestOptions]` — Request-specific configuration.
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

## video
<details><summary><code>client.video.<a href="src/hume_expression_measurement/video/client.py">measure</a>(...) -> VideoFileResponse</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Send one or more images, each as a `file` part, and, to change the defaults, settings as a JSON `config` part. The response holds one result per image, in the order the parts were sent. An image with no faces is a normal result with an empty `faces` array.

## Detection and measurement

The server detects the faces in each image and lists them in `faces`. It measures the largest of them, up to the limit below, and lists those first, largest first.

- A face is skipped, and is not listed, if its detection confidence is below `face.threshold` or its bounding box is under `face.min_size` pixels on its shorter side.
- Each measured face carries its bounding box, its detection confidence, a `face_id`, and two sets of scores: `expressions` for emotional expressions such as amusement or interest, and `descriptions` for visible facial actions such as a smile or a jaw drop.
- The faces that were not measured follow, most confident first, up to 32 faces in all. Each carries its bounding box and detection confidence, with `face_id`, `expressions` and `descriptions` null.
- `face_id` tracks a face across the images of one request. A face normally keeps its ID while it is measured, and can get it back after it leaves the view or is no longer among the largest faces. IDs do not identify a person.
- **Across requests:** IDs start again from 0 in every request. To keep them consistent, set `return_face_state` and pass the returned `face_state` as `face_state` in the next request.

## Limits

| Limit | Value |
|---|---|
| Request size | 25MB, the whole multipart body |
| Image size | 2MB |
| Pixels per image | 8,294,400 (the area of a 3840x2160 image) |
| Faces measured per image | 2 by default, the largest; contact support to raise it |
| Faces listed per image | 32 |

Every image is checked before any is measured. One bad image fails the whole request, which then returns no results:

- **Not a JPEG:** rejected with `unsupported_media_type`.
- **Over the image size or pixel limit:** rejected with `image_too_large`.
- **Cannot be decoded:** rejected with `image_decode_failed`, possibly after earlier images were measured.
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```python
from hume_expression_measurement import ExpressionMeasurementClient
from hume_expression_measurement.environment import ExpressionMeasurementClientEnvironment

client = ExpressionMeasurementClient(
    api_key="<value>",
    environment=ExpressionMeasurementClientEnvironment.PRODUCTION,
)

client.video.measure(
    file=["example_file"],
)

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**file:** `typing.List[core.File]` — The images, one JPEG per `file` part. Results are numbered by the order of the parts.
    
</dd>
</dl>

<dl>
<dd>

**config:** `typing.Optional[VideoFileConfig]` 
    
</dd>
</dl>

<dl>
<dd>

**request_options:** `typing.Optional[RequestOptions]` — Request-specific configuration.
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

## runs
<details><summary><code>client.runs.<a href="src/hume_expression_measurement/runs/client.py">list</a>(...) -> RunsPage</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Your organization's runs, newest first, one page at a time. The filters combine.
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```python
from hume_expression_measurement import ExpressionMeasurementClient
from hume_expression_measurement.environment import ExpressionMeasurementClientEnvironment

client = ExpressionMeasurementClient(
    api_key="<value>",
    environment=ExpressionMeasurementClientEnvironment.PRODUCTION,
)

client.runs.list()

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**page_size:** `typing.Optional[int]` — How many runs to return.
    
</dd>
</dl>

<dl>
<dd>

**cursor:** `typing.Optional[str]` — The `next_cursor` of the previous page. Pass it back unchanged.
    
</dd>
</dl>

<dl>
<dd>

**user_id:** `typing.Optional[str]` — Only runs made by this user.
    
</dd>
</dl>

<dl>
<dd>

**status:** `typing.Optional[RunStatus]` — Only runs with this status.
    
</dd>
</dl>

<dl>
<dd>

**mode:** `typing.Optional[RunMode]` — Only runs that reached the API this way.
    
</dd>
</dl>

<dl>
<dd>

**endpoint:** `typing.Optional[RunEndpoint]` — Only runs that measured this kind of media.
    
</dd>
</dl>

<dl>
<dd>

**started_before:** `typing.Optional[datetime.datetime]` — Only runs that started before this time, in RFC 3339 format.
    
</dd>
</dl>

<dl>
<dd>

**started_after:** `typing.Optional[datetime.datetime]` — Only runs that started at or after this time, in RFC 3339 format. Consecutive time ranges that share a boundary neither skip a run nor return one twice.
    
</dd>
</dl>

<dl>
<dd>

**request_options:** `typing.Optional[RequestOptions]` — Request-specific configuration.
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.runs.<a href="src/hume_expression_measurement/runs/client.py">get</a>(...) -> Run</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

The run's status, when it started and ended, what it received and stored, and the configuration it ran under.
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```python
from hume_expression_measurement import ExpressionMeasurementClient
from hume_expression_measurement.environment import ExpressionMeasurementClientEnvironment

client = ExpressionMeasurementClient(
    api_key="<value>",
    environment=ExpressionMeasurementClientEnvironment.PRODUCTION,
)

client.runs.get(
    run_id="01926f3a-5b1c-7d2e-8f40-3a9b7c1d2e5f",
)

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**run_id:** `str` — The run's ID: the `session_id` of a realtime session or the `run_id` of a file response.
    
</dd>
</dl>

<dl>
<dd>

**request_options:** `typing.Optional[RequestOptions]` — Request-specific configuration.
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.runs.<a href="src/hume_expression_measurement/runs/client.py">list_events</a>(...) -> RunEventsPage</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

The problems the run reported, in order: frames it rejected and failures it hit. A run that went well has none. Only realtime sessions record them.
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```python
from hume_expression_measurement import ExpressionMeasurementClient
from hume_expression_measurement.environment import ExpressionMeasurementClientEnvironment

client = ExpressionMeasurementClient(
    api_key="<value>",
    environment=ExpressionMeasurementClientEnvironment.PRODUCTION,
)

client.runs.list_events(
    run_id="01926f2c-6d92-7a41-9d26-5e8b0c3f71a9",
)

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**run_id:** `str` — The run's ID: the `session_id` of a realtime session or the `run_id` of a file response.
    
</dd>
</dl>

<dl>
<dd>

**page_size:** `typing.Optional[int]` — How many entries to return.
    
</dd>
</dl>

<dl>
<dd>

**cursor:** `typing.Optional[str]` — The `next_cursor` of the previous page. Pass it back unchanged.
    
</dd>
</dl>

<dl>
<dd>

**request_options:** `typing.Optional[RequestOptions]` — Request-specific configuration.
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

