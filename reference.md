# Reference
## voice
<details><summary><code>client.voice.<a href="src/hume_expression_measurement/voice/client.py">measure</a>(...) -> VoiceFileResponse</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Send the audio as the `file` part and, to change the defaults, settings as a JSON `config` part. The response holds every utterance found in the audio and every measurement taken, the same results a realtime session would stream for the same audio.

## How measurements are produced

The server detects when speech starts and stops. Each continuous stretch of speech is an **utterance**, listed in `utterances` with its span in the audio. Every measurement in `measurements` carries the `utterance_id` it belongs to, the span of audio it covers as `audio_start_ms` and `audio_end_ms`, and a `trigger` saying why it was produced. All times count from the start of the audio.

While an utterance is in progress, the server produces a measurement every `measurement_timer_ms` milliseconds of speech, with `trigger` set to `interval`. Each measurement covers the most recent 10 seconds of the utterance, or the whole utterance if it is shorter than that. Consecutive interval measurements therefore overlap; at the maximum `measurement_timer_ms` of 10000 they meet end to end instead.

When an utterance ends, the server produces a final measurement with `trigger` set to `speech_ended`, unless less than one second of speech has passed since the previous measurement ended. Every utterance produces at least one measurement: an utterance shorter than `measurement_timer_ms` has no interval measurements, so its final measurement is always produced. Audio that ends while speech is in progress ends the utterance.

## Limits

| Limit | Value |
|---|---|
| Request size | 25MB, the whole multipart body |
| Audio format | 16-bit PCM, 16 kHz, mono, as a WAV file or headerless samples |

A request over the size limit is rejected with `audio_too_long`. Audio in another container, such as MP3 or FLAC, is rejected with `unsupported_media_type`; convert it before sending. A WAV file at another sample rate, channel count or sample format is rejected with `audio_decode_failed` rather than converted.
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

client.voice.measure(
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

**config:** `typing.Optional[VoiceFileConfig]` 
    
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

## face
<details><summary><code>client.face.<a href="src/hume_expression_measurement/face/client.py">measure</a>(...) -> FaceFileResponse</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Send one or more images, each as a `file` part, and, to change the defaults, settings as a JSON `config` part. The response holds one result per image, in the order the parts were sent, with the measurements for every face found in it. An image with no faces is a normal result with an empty `faces` array.

## Detection and measurement

The server detects the faces in each image. It does not measure a face whose detection confidence is below `face.threshold`, or whose bounding box is under `face.min_size` pixels on its shorter side. Of the rest, it measures up to 64, most confident first, and lists them in `faces` in that order. Each measured face carries its bounding box, its detection confidence, a `face_id`, and two sets of scores: `expressions` for emotional expressions such as amusement or interest, and `descriptions` for visible facial actions such as a smile or a jaw drop.

`face_id` tracks a face across the images of one request. A face normally keeps the same ID while it stays in view; a face that leaves the view and returns may receive a new ID. IDs do not identify a person. To keep IDs consistent across requests, set `return_face_state` and pass the returned `face_state` as `face_state` in the next request; otherwise IDs start again from 0 in every request.

## Limits

| Limit | Value |
|---|---|
| Request size | 25MB, the whole multipart body |
| Image size | 2MB |
| Pixels per image | 8,294,400 (the area of a 3840x2160 image) |
| Faces measured per image | 64 |

The request is measured as a whole: an image that is not a JPEG, is over a size limit or cannot be decoded fails the request with `unsupported_media_type`, `image_too_large` or `image_decode_failed`, and no result is returned for any image.
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

client.face.measure(
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

**config:** `typing.Optional[FaceFileConfig]` 
    
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

The problems the run reported, in order: frames it rejected and failures it hit. A run that went well has none, and only realtime sessions record any.
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

