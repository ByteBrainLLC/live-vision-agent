# Live Vision Agent Build Specification (v2, Build-Ready)

**Document purpose:** A complete, corrected, implementation-ready specification for building a local, real-time AI agent that hears through the microphone, sees through the webcam, responds out loud through speakers/headphones, and prints a terminal transcript. This version remediates the API-contract errors found in v1, adds the missing implementation details that would have caused build failures, and hardens the design against security and reliability exposures before we take it to an actual build.

**Source basis:** Original v1 spec (tutorial-derived) plus validation against current Google Gemini Live API documentation and the `google-genai` Python SDK as of 2026-06-12.

**What changed from v1 (read this first):**

1. `send_realtime_input(video=...)` corrected to `send_realtime_input(media=...)` for image frames. This was the single most likely cause of a hard build failure.
2. `system_instruction` now wrapped in `types.Content(parts=[types.Part(text=...)])` instead of a raw string.
3. `asyncio.Queue` and all other instance state explicitly initialized in `__init__`.
4. `LiveVisionAgent.close()` body fully specified.
5. Transcription config field names flagged for verification and shown with the current pattern.
6. `asyncio.TaskGroup` adopted for clean concurrent-task cancellation (Python 3.13).
7. Added: startup banner, `input_audio_transcription`, `thinking_level=minimal`, `media_resolution`, GoAway handling, session-duration reality, and a proactive Windows PyAudio install path.
8. New Section 13: Security, Privacy, and Vulnerability Hardening (expanded and made actionable).

---

## 1. Executive Summary

Build a local Python application named `live-vision-agent` that opens one persistent Gemini Live API session and runs four concurrent loops inside an `asyncio.TaskGroup`:

1. **Microphone upload loop:** captures 16 kHz mono PCM audio and streams it to Gemini Live.
2. **Camera upload loop:** captures one webcam frame per second, converts it to JPEG bytes, and streams it to Gemini Live.
3. **Response receive loop:** receives model audio chunks, output transcript text, interruption signals, and connection-lifecycle messages (GoAway).
4. **Speaker playback loop:** plays model audio back through the local speaker/headphones at 24 kHz.

The finished MVP should feel like a live camera-and-voice agent, not a request-response chatbot. The user speaks naturally, the agent listens continuously, the user can hold something up to the camera, and the agent describes what it sees and speaks out loud. If the user interrupts while the agent is talking, playback stops quickly and the agent listens to the new turn.

---

## 2. Product Intent

### 2.1 Primary User Experience

The user runs a terminal command, grants camera/mic access if prompted by the OS, and begins speaking. The agent should:

- Print a clear startup banner once connected, so the terminal is never silent at launch.
- Listen continuously through the default microphone.
- See one webcam JPEG frame per second.
- Speak responses out loud through the default speaker or headphones.
- Print a readable transcript of the model's spoken responses (and optionally the user's speech) in the terminal.
- Handle interruptions without a "send" button.
- Shut down cleanly with `Ctrl+C`, releasing mic, speaker, and webcam.

### 2.2 Core Framing

This is a **live multimodal agent prototype**. Not a chat interface with file upload. Not a text-only bot. Not a frame-by-frame computer-vision demo. It is a continuous audio-video conversation loop.

### 2.3 Phase 1 Non-Goals

Do not build these in the MVP unless explicitly requested later: browser UI, mobile app, cloud deployment, multi-user support, user accounts/auth, persistent memory or database, video/audio recording, screenshot/frame storage, external tool/function calling, RAG, custom object detection, or complex logging beyond terminal output.

One reliability item that v1 listed as a non-goal is being **promoted into the MVP in a minimal form**: GoAway detection and a clean session-end message (see Section 4.6). Full reconnect/resumption stays Phase 2, but the MVP must not silently die after ~10 minutes without telling the user why.

---

## 3. Technical Architecture

### 3.1 High-Level Flow

```text
Microphone, 16 kHz PCM  ─┐
                         ├──> Live Vision Agent ──WebSocket──> Gemini Live API
Webcam, 1 JPEG/sec      ─┘              │                         │
                                        │                         │
                                        ├<── audio, 24 kHz PCM <──┘
                                        │
                                        ├──> Speaker/headphones
                                        │
                                        └──> Terminal transcript
```

### 3.2 Runtime Pattern

Run a single Gemini Live API session. Inside that session, run four tasks concurrently inside an `asyncio.TaskGroup`:

```text
Task 1: _stream_microphone()
Task 2: _stream_camera()
Task 3: _receive_responses()
Task 4: _play_audio()
```

Use `asyncio` for concurrency. Use `asyncio.to_thread(...)` around blocking local-device calls: PyAudio mic reads, PyAudio speaker writes, and OpenCV camera reads + JPEG encode.

`asyncio.TaskGroup` (Python 3.11+, available in 3.13) is used instead of bare `asyncio.gather()` because if any one loop raises (WebSocket close, device error), the TaskGroup cancels the siblings cleanly and surfaces the exception. This is exactly the lifecycle behavior we want and removes a whole class of "one loop died and the rest hung" bugs.

### 3.3 Why This Design Matters

- Mic and camera must stream continuously while the model is generating.
- The receive loop must not block playback.
- Playback needs a queue so bursty model audio plays back at normal listening speed.
- The interruption signal must drain pending playback so the agent stops talking when the user cuts in.
- The app runs as a single process with no external infrastructure for the MVP.

---

## 4. Official Live API Contract to Respect

### 4.1 Input and Output Modalities

The Gemini Live API is a stateful WebSocket service for real-time voice and vision. For this build: audio + images in, audio out (plus transcription text).

| Direction | Data | Required format |
|---|---|---|
| User microphone to API | Audio | Raw 16-bit PCM, mono, 16 kHz, little-endian |
| User webcam to API | Image frames | JPEG frames, max 1 frame per second |
| API to local speaker | Audio | Raw 16-bit PCM, mono, 24 kHz, little-endian |
| API protocol | Session | Stateful WebSocket connection |

### 4.2 Model

Default model:

```python
MODEL = "gemini-3.1-flash-live-preview"
```

Fallback only if the default is unavailable in the target account/region:

```python
MODEL = "gemini-2.5-flash-live-preview"
```

Keep the model value centralized in `src/config.py`. Model names and availability change; this is the single place to edit.

### 4.3 Voice

```python
VOICE = "Zephyr"
```

Centralized in `src/config.py`. Other Gemini Live voices may be substituted later.

### 4.4 Response Modality

```python
response_modalities=["AUDIO"]
```

Do not use text-only output for this MVP. The user-facing transcript comes from audio transcription output, not from switching the model into text mode.

### 4.5 Context Window Compression

Enable context window compression with a sliding window so the session can run long without dying as context fills:

```python
context_window_compression=types.ContextWindowCompressionConfig(
    trigger_tokens=25_600,
    sliding_window=types.SlidingWindow(target_tokens=12_800),
)
```

### 4.6 Session Lifetime and GoAway (new in v2)

Reality check that the MVP must account for:

- A single Live connection lasts roughly **10 minutes** before the server terminates it.
- The server sends a **GoAway** message about **60 seconds before** the connection ends.
- Context window compression extends the *session* (the conversation context) but the *connection* still recycles on its own clock.
- **Important privacy note:** Session resumption works by caching your text, audio, video, and model outputs server-side for up to 2 hours. If zero data retention matters to you, do NOT enable session resumption. The MVP deliberately leaves it off.

MVP requirement: in `_receive_responses()`, detect `response.go_away` and print a friendly notice with the time left, then allow the session to end cleanly. Full auto-reconnect using resumption handles is Phase 2. The point for the MVP is: no silent death.

```python
if getattr(message, "go_away", None) is not None:
    secs = getattr(message.go_away, "time_left", None)
    print(f"\n[session ending in ~{secs}; this is expected on the Live API ~10-min connection limit]")
```

### 4.7 Thinking Level (new in v2)

Gemini 3.1 Flash Live uses `thinking_level` (values: `minimal`, `low`, `medium`, `high`) rather than the older `thinking_budget`. For a low-latency voice agent, set it to `minimal`. If the installed SDK does not expose this field on the config type, skip it (the model defaults to minimal for this model family) rather than erroring.

### 4.8 Media Resolution (new in v2)

The Live API accepts a `media_resolution` setting (`low` / `medium` / `high`) controlling token usage and detail for video input. Lower = cheaper + faster, higher = better detail recognition. Start at `medium` for a laptop demo. Keep it in config.

---

## 5. Development Environment

### 5.1 Platform Assumptions

Primary target: Windows laptop/desktop, Python 3.13, local webcam, local microphone, headphones/earbuds strongly recommended.

Secondary target: macOS or Linux may work; PyAudio install steps differ (see 5.4).

### 5.2 Python Version

Pin Python 3.13. Do **not** use 3.14: PyAudio Windows wheels may be unavailable and pip may try to compile C locally and fail. (3.13 is also required for the `asyncio.TaskGroup` usage, though TaskGroup is actually available from 3.11+.)

### 5.3 Dependency Baseline

Known-good environment (verified current as of 2026-06-12; `google-genai==2.8.0` is the latest on PyPI):

```text
google-genai==2.8.0
opencv-python==4.13.0.92
pyaudio==0.2.14
pillow==12.2.0
python-dotenv==1.2.2
Python 3.13
```

Guidance:
- Prefer these exact versions.
- If the resolver cannot install them, use latest compatible versions, then freeze with `uv.lock`.
- Do not silently change model or SDK patterns without checking current Google Live API docs.

### 5.4 PyAudio Install Paths (new in v2, proactive)

PyAudio depends on PortAudio (a C library), which is the #1 real-world setup failure.

**Windows (most reliable):**
```bash
# Try the normal path first:
uv add pyaudio
# If it fails to build, use a prebuilt wheel:
uv pip install pipwin
pipwin install pyaudio
# Or download a matching cp313 wheel and: uv pip install path\to\PyAudio-...-cp313-...whl
```

**macOS:**
```bash
brew install portaudio
uv add pyaudio
```

**Linux (Debian/Ubuntu):**
```bash
sudo apt-get install -y portaudio19-dev
uv add pyaudio
```

---

## 6. Project Setup Commands

Use `uv`.

```bash
uv init live-vision-agent
cd live-vision-agent
uv python pin 3.13
uv add google-genai opencv-python pyaudio pillow python-dotenv
```

Create `.env` in the project root:

```bash
GEMINI_API_KEY=your_api_key_here
```

Create `.env.example`:

```bash
GEMINI_API_KEY=
```

Create `.gitignore`:

```gitignore
.env
.venv/
__pycache__/
*.pyc
.DS_Store
*.log
.session_handle
```

(`.session_handle` and `*.log` are pre-emptively ignored so a future Phase-2 resumption handle or debug log can never be committed by accident.)

---

## 7. Required File Structure

```text
live-vision-agent/
├── .env
├── .env.example
├── .gitignore
├── README.md
├── pyproject.toml
└── src/
    ├── __init__.py        # may be empty, but MUST exist (package marker for `python -m src.agent`)
    ├── config.py
    ├── camera.py
    ├── audio_io.py
    └── agent.py
```

Optional after MVP:

```text
└── tests/
    ├── test_config.py
    ├── test_camera.py
    └── test_audio_io.py
```

---

## 8. Module Specifications

### 8.1 `src/config.py`

**Purpose:** Centralize env loading, constants, model/voice config, audio contract, camera sizing, system prompt, and a fail-fast key check.

**Requirements:**
- Load `.env` with `python-dotenv` before any client is created.
- Read `GEMINI_API_KEY`.
- Fail fast with a clear error if the key is missing (and never print the key value).
- Keep every tunable constant here.

```python
import os
import pyaudio
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

def require_api_key() -> str:
    """Return the key or raise a clear, non-leaking error."""
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is missing. Create a .env file in the project root "
            "with GEMINI_API_KEY=your_key and run from the project root."
        )
    return GEMINI_API_KEY

# --- Model / voice ---
MODEL: str = "gemini-3.1-flash-live-preview"   # fallback: "gemini-2.5-flash-live-preview"
VOICE: str = "Zephyr"                          # other Gemini Live voices may be substituted

# --- Audio contract ---
AUDIO_FORMAT: int = pyaudio.paInt16
CHANNELS: int = 1
SEND_SAMPLE_RATE: int = 16_000
RECEIVE_SAMPLE_RATE: int = 24_000
CHUNK_SIZE: int = 1_024

# --- Camera ---
FRAME_INTERVAL_SECONDS: float = 1.0
MAX_FRAME_SIZE: tuple[int, int] = (768, 768)
JPEG_QUALITY: int = 85

# --- Live tuning ---
MEDIA_RESOLUTION: str = "medium"   # low | medium | high
THINKING_LEVEL: str = "minimal"    # minimal | low | medium | high (lowest latency = minimal)
CWC_TRIGGER_TOKENS: int = 25_600
CWC_TARGET_TOKENS: int = 12_800

# --- Behavior toggles ---
PRINT_INPUT_TRANSCRIPTION: bool = True   # show what the user said, not just the model

SYSTEM_PROMPT: str = """
You are a sharp, friendly assistant with live access to the user's camera.
You can see what they show you in real time. Answer out loud.
Keep responses short and conversational. When the user shows you something,
describe or reason about what you actually see. Do not claim certainty when
visual evidence is unclear.
""".strip()
```

**Verification:**
```bash
PYTHONPATH=. uv run python -c "from src.config import GEMINI_API_KEY; print(bool(GEMINI_API_KEY))"
```
Expected: `True`

---

### 8.2 `src/camera.py`

**Purpose:** Own webcam access and return one JPEG byte payload per call.

**Requirements:**
- Use OpenCV `cv2.VideoCapture`, opened once at startup (not per frame).
- Default index `0`; allow `device_index` override for an external cam.
- Convert BGR→RGB before Pillow. (OpenCV decodes/encodes in BGR; skipping this gives blue-tinted frames the model misreads.)
- Resize to fit inside `MAX_FRAME_SIZE` with `image.thumbnail(...)` so aspect ratio is preserved (no stretch/crop).
- Encode JPEG in memory via `io.BytesIO` at `JPEG_QUALITY`.
- Return `bytes` on success, `None` on read failure.
- `close()` releases the device.
- If the camera cannot open, raise a clear `RuntimeError` telling the user to close other camera apps.

**Why Pillow and not just `cv2.imencode`:** we deliberately use Pillow because it gives us correct color (after the BGR→RGB conversion) AND aspect-ratio-preserving downscale in one place. Do not "simplify" this to a bare `cv2.imencode('.jpg', frame)` call, since that skips the color fix and the aspect-preserving resize.

```python
import io
import cv2
from PIL import Image
from src.config import MAX_FRAME_SIZE, JPEG_QUALITY


class Camera:
    def __init__(self, device_index: int = 0) -> None:
        self._capture = cv2.VideoCapture(device_index)
        if not self._capture.isOpened():
            raise RuntimeError(
                f"Could not open camera at index {device_index}. "
                "Close other apps using the webcam (Zoom, Teams, browser tabs, OBS) "
                "and try again, or pass Camera(device_index=1) for an external cam."
            )

    def read_jpeg_frame(self) -> bytes | None:
        ok, frame_bgr = self._capture.read()
        if not ok or frame_bgr is None:
            return None
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(frame_rgb)
        image.thumbnail(MAX_FRAME_SIZE)  # preserves aspect ratio
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
        return buffer.getvalue()

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None
```

**Verification:**
```bash
PYTHONPATH=. uv run python -c "from src.camera import Camera; c=Camera(); b=c.read_jpeg_frame(); print(len(b) if b else None); c.close()"
```
Expected: a number roughly 5,000–600,000. Webcam light may blink.

---

### 8.3 `src/audio_io.py`

**Purpose:** Own mic input and speaker output via PyAudio.

**Requirements:**
- One `pyaudio.PyAudio()` instance per session.
- Open mic and speaker streams lazily.
- Mic: 16 kHz mono 16-bit PCM, `CHUNK_SIZE` frames/buffer.
- Speaker: 24 kHz mono 16-bit PCM.
- `close()` stops/closes both streams and terminates PyAudio.

```python
import pyaudio
from src.config import (
    AUDIO_FORMAT, CHANNELS, SEND_SAMPLE_RATE, RECEIVE_SAMPLE_RATE, CHUNK_SIZE,
)


class AudioIO:
    def __init__(self) -> None:
        self._pyaudio = pyaudio.PyAudio()
        self._mic_stream: pyaudio.Stream | None = None
        self._speaker_stream: pyaudio.Stream | None = None

    def open_mic(self) -> pyaudio.Stream:
        if self._mic_stream is None:
            device = self._pyaudio.get_default_input_device_info()
            self._mic_stream = self._pyaudio.open(
                format=AUDIO_FORMAT,
                channels=CHANNELS,
                rate=SEND_SAMPLE_RATE,
                input=True,
                input_device_index=int(device["index"]),
                frames_per_buffer=CHUNK_SIZE,
            )
        return self._mic_stream

    def open_speaker(self) -> pyaudio.Stream:
        if self._speaker_stream is None:
            self._speaker_stream = self._pyaudio.open(
                format=AUDIO_FORMAT,
                channels=CHANNELS,
                rate=RECEIVE_SAMPLE_RATE,
                output=True,
            )
        return self._speaker_stream

    def close(self) -> None:
        for stream in (self._mic_stream, self._speaker_stream):
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
        self._mic_stream = None
        self._speaker_stream = None
        try:
            self._pyaudio.terminate()
        except Exception:
            pass
```

**Verification:**
```bash
PYTHONPATH=. uv run python -c "from src.audio_io import AudioIO; a=AudioIO(); a.open_mic(); a.open_speaker(); print('audio OK'); a.close()"
```
Expected: `audio OK`

---

### 8.4 `src/agent.py`

**Purpose:** Coordinate the Live session, the four loops, the playback queue, transcript printing, interruptions, GoAway, and clean shutdown.

**Client setup:**
```python
from google import genai
self._client = genai.Client(api_key=require_api_key())
```

**Live session config (corrected, v2):**
```python
from google.genai import types

LIVE_CONFIG = types.LiveConnectConfig(
    response_modalities=["AUDIO"],
    media_resolution=MEDIA_RESOLUTION,
    system_instruction=types.Content(
        parts=[types.Part(text=SYSTEM_PROMPT)]
    ),
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)
        )
    ),
    output_audio_transcription=types.AudioTranscriptionConfig(),
    input_audio_transcription=types.AudioTranscriptionConfig(),
    context_window_compression=types.ContextWindowCompressionConfig(
        trigger_tokens=CWC_TRIGGER_TOKENS,
        sliding_window=types.SlidingWindow(target_tokens=CWC_TARGET_TOKENS),
    ),
)
```

**SDK-version guardrail (do this before declaring done):** field names on `LiveConnectConfig` have shifted across SDK releases. Before building, run:
```bash
PYTHONPATH=. uv run python -c "from google.genai import types; print([f for f in types.LiveConnectConfig.model_fields])"
```
Then confirm the exact spellings of `output_audio_transcription`, `input_audio_transcription`, `media_resolution`, and `thinking` / `thinking_level`. If a field is named differently or absent, adapt while preserving the logical intent. If `thinking_level` is not a valid field on this config object, omit it (this model defaults to minimal). Do not invent fields.

**Full class skeleton with corrected calls:**
```python
import asyncio
from google import genai
from google.genai import types

from src.config import (
    require_api_key, MODEL, VOICE, SYSTEM_PROMPT, MEDIA_RESOLUTION,
    SEND_SAMPLE_RATE, CHUNK_SIZE, FRAME_INTERVAL_SECONDS,
    CWC_TRIGGER_TOKENS, CWC_TARGET_TOKENS, PRINT_INPUT_TRANSCRIPTION,
)
from src.camera import Camera
from src.audio_io import AudioIO


class LiveVisionAgent:
    def __init__(self) -> None:
        self._client = genai.Client(api_key=require_api_key())
        self._camera = Camera()
        self._audio = AudioIO()
        self._playback_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._session = None

    def _build_config(self) -> types.LiveConnectConfig:
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            media_resolution=MEDIA_RESOLUTION,
            system_instruction=types.Content(parts=[types.Part(text=SYSTEM_PROMPT)]),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)
                )
            ),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            context_window_compression=types.ContextWindowCompressionConfig(
                trigger_tokens=CWC_TRIGGER_TOKENS,
                sliding_window=types.SlidingWindow(target_tokens=CWC_TARGET_TOKENS),
            ),
        )

    async def run(self) -> None:
        config = self._build_config()
        async with self._client.aio.live.connect(model=MODEL, config=config) as session:
            self._session = session
            print("\n=== Live Vision Agent connected ===")
            print("Speak naturally. Show objects to the camera. Press Ctrl+C to quit.\n")
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self._stream_microphone())
                    tg.create_task(self._stream_camera())
                    tg.create_task(self._receive_responses())
                    tg.create_task(self._play_audio())
            except* asyncio.CancelledError:
                pass  # expected on shutdown

    async def _stream_microphone(self) -> None:
        mic = self._audio.open_mic()
        while True:
            chunk = await asyncio.to_thread(
                mic.read, CHUNK_SIZE, exception_on_overflow=False
            )
            await self._session.send_realtime_input(
                audio=types.Blob(
                    data=chunk,
                    mime_type=f"audio/pcm;rate={SEND_SAMPLE_RATE}",
                )
            )

    async def _stream_camera(self) -> None:
        while True:
            frame = await asyncio.to_thread(self._camera.read_jpeg_frame)
            if frame is not None:
                # NOTE: image frames use media=, NOT video= (v1 bug fixed here)
                await self._session.send_realtime_input(
                    media=types.Blob(data=frame, mime_type="image/jpeg")
                )
            await asyncio.sleep(FRAME_INTERVAL_SECONDS)

    async def _receive_responses(self) -> None:
        async for message in self._session.receive():
            # Connection-lifecycle: GoAway ~60s before the ~10-min connection ends
            if getattr(message, "go_away", None) is not None:
                secs = getattr(message.go_away, "time_left", None)
                print(f"\n[session ending in ~{secs}; expected on the ~10-min Live connection limit]")

            content = getattr(message, "server_content", None)
            if content is None:
                continue

            if getattr(content, "interrupted", False):
                self._drain_playback_queue()

            model_turn = getattr(content, "model_turn", None)
            if model_turn:
                for part in model_turn.parts:
                    inline = getattr(part, "inline_data", None)
                    if inline and inline.data:
                        self._playback_queue.put_nowait(inline.data)

            out_tx = getattr(content, "output_transcription", None)
            if out_tx and out_tx.text:
                print(out_tx.text, end="", flush=True)

            if PRINT_INPUT_TRANSCRIPTION:
                in_tx = getattr(content, "input_transcription", None)
                if in_tx and in_tx.text:
                    print(f"\n[you] {in_tx.text}", flush=True)

    async def _play_audio(self) -> None:
        speaker = self._audio.open_speaker()
        while True:
            chunk = await self._playback_queue.get()
            await asyncio.to_thread(speaker.write, chunk)
            self._playback_queue.task_done()

    def _drain_playback_queue(self) -> None:
        while not self._playback_queue.empty():
            try:
                self._playback_queue.get_nowait()
                self._playback_queue.task_done()
            except asyncio.QueueEmpty:
                break

    def close(self) -> None:
        try:
            self._camera.close()
        finally:
            self._audio.close()


async def main() -> None:
    agent = LiveVisionAgent()
    try:
        await agent.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        agent.close()


if __name__ == "__main__":
    asyncio.run(main())
```

**Run command:**
```bash
PYTHONPATH=. uv run python -m src.agent
```

**Field-name note:** every attribute access on server messages above uses `getattr(..., default)` deliberately, so a renamed/missing field degrades gracefully instead of crashing the receive loop. Once you confirm the exact field names against the installed SDK, you may switch to direct attribute access for clarity.

---

## 9. README Requirements

The `README.md` must include: what the app does; hardware requirements; API key setup; install commands (including the OS-specific PyAudio paths from 5.4); verification commands; run command; common errors and fixes; privacy note (including the session-resumption/data-retention caveat); known limitations (the ~10-min connection limit); and future improvements.

**Suggested opening:**
```markdown
# Live Vision Agent

A local Python prototype that connects your microphone and webcam to the Gemini
Live API so you can speak to an AI agent, show it things through your camera,
hear spoken responses, and read a terminal transcript.

This is a local development prototype. It does not record or store audio, video,
frames, or transcripts. Session resumption is intentionally disabled, so no
conversation data is cached server-side beyond what the live request requires.
```

---

## 10. Acceptance Criteria

**Environment**
- `uv python pin 3.13` works.
- Dependencies install without manual C compilation on the target OS (or via the documented PyAudio fallback).
- `.env` loads; key verification prints `True`.

**Camera**
- `Camera()` opens the default camera; `read_jpeg_frame()` returns JPEG bytes.
- Frames fit within 768×768; aspect ratio preserved; `close()` releases the device.

**Audio**
- `AudioIO()` opens default mic (16 kHz) and speaker (24 kHz), mono 16-bit PCM.
- `close()` releases streams and terminates PyAudio.

**Live Agent**
- Connects to the Live API and prints the startup banner.
- User asks a spoken question and hears a spoken answer.
- User holds up an object and asks "what is this?"; agent uses webcam context.
- Terminal prints model output transcript (and user input transcript if enabled).
- User can interrupt mid-answer; playback queue drains; agent responds to the new turn.
- GoAway prints a clear notice rather than dying silently.
- `Ctrl+C` exits cleanly; mic, speaker, and webcam are released.

---

## 11. Manual Test Script

1. Start: `PYTHONPATH=. uv run python -m src.agent`
2. Say "Can you hear me?" → agent answers out loud.
3. Hold up a mug/phone/notebook, say "What am I holding?" → describes it (or gives a cautious answer if unclear).
4. While it's answering, say "Stop. Now tell me only the color." → it stops the prior answer fast and responds to the new instruction.
5. Let it run ~10 minutes → confirm the GoAway notice prints rather than a silent freeze.
6. Press `Ctrl+C` → clean exit, devices released.

---

## 12. Common Errors and Fixes

**Missing API key** (`GEMINI_API_KEY is missing`): create `.env` in project root, add the key, run from root, re-run the config verification.

**PyAudio fails on Windows:** wrong Python version or no wheel. `uv python pin 3.13`, then `uv sync`. If still failing, use `pipwin install pyaudio` or a prebuilt cp313 wheel (see 5.4).

**No camera found:** another app owns the webcam, permission denied, or wrong index. Close Zoom/Teams/OBS/browser camera tabs, check OS permissions, try `Camera(device_index=1)`.

**Agent talks over itself / hears itself:** speaker feeding back into mic. Use headphones, lower volume, move mic away.

**Audio is static / too fast / too slow:** sample-rate mismatch. Confirm mic send rate 16 kHz, speaker receive rate 24 kHz, 16-bit PCM mono.

**WebSocket closes with invalid payload:** wrong MIME type, wrong modality, or images sent on the wrong field. Audio MIME must be `audio/pcm;rate=16000`; image MIME must be `image/jpeg`; `response_modalities=["AUDIO"]`; image frames sent via `media=` at ≤1 FPS.

**`TypeError`/unexpected keyword on `send_realtime_input` or `LiveConnectConfig`:** SDK field/parameter renamed. Inspect the installed SDK (`model_fields`, `help(session.send_realtime_input)`) and adapt. This is the most likely point of drift.

**Session terminates after ~10 minutes:** expected connection limit. The MVP prints a GoAway notice; auto-reconnect is Phase 2.

---

## 13. Security, Privacy, and Vulnerability Hardening

This section is expanded in v2 because we're heading toward an actual build. Address these *now*, not at "production."

### 13.1 Secrets and API key
- Never commit `.env`; it's in `.gitignore`. Ship only `.env.example`.
- Never print, log, or echo the raw key. `config.require_api_key()` raises a message that does not include the value.
- Never embed the key in any future browser/client code. For any client-facing version, mint **ephemeral tokens** from a backend (Phase 5), never the long-lived key.
- Treat the key as a billable credential: a leaked key is real money. Rotate immediately if exposed; set a billing budget/alert in Google AI Studio / Cloud.
- Add a `.gitignore` check to the build checklist and, ideally, a pre-commit secret scan (e.g. `gitleaks`) before the first push.

### 13.2 Data retention and the camera/mic
- Default: do **not** record or store audio, video, frames, or transcripts to disk. Terminal-only output.
- Session resumption is **off by default**. Turning it on caches text/audio/video/model-output server-side for up to ~2 hours; only enable it knowingly, and document it for the user if you do.
- Any future debug mode that writes frames/audio must be explicit, opt-in, off by default, and write only to a `.gitignore`d path.
- The webcam light is the user's honest signal: release the camera on exit (we do) so it never stays lit after the process ends.

### 13.3 Supply chain
- Pin exact dependency versions and commit `uv.lock`. Reproducible installs prevent a malicious or broken transitive update from silently entering the build.
- Periodically audit (`uv pip list --outdated`, and a vuln scanner like `pip-audit`) before upgrading. Don't auto-upgrade the SDK without re-validating the API contract.
- `opencv-python`, `pyaudio`, and `pillow` all carry native code; only install from PyPI/official wheels, never random mirrors.

### 13.4 Input / resource safety
- Camera read failures return `None` and are skipped, not retried in a tight loop — prevents a busy-spin if the device drops.
- The mic loop uses `exception_on_overflow=False` so a buffer overrun doesn't crash the stream.
- The playback queue is unbounded by default; if a flood of audio ever arrives, memory could grow. For hardening, consider a `maxsize` on `asyncio.Queue` and drop-oldest behavior. Acceptable to leave unbounded for the MVP, but note it.
- Wrap the top-level `run()` so a single device error tears down all four tasks cleanly (TaskGroup does this) rather than leaving a half-dead process holding the camera.

### 13.5 Privacy posture to state plainly to users
- Audio and video are streamed to Google's Gemini Live API for processing. This is not an offline/on-device model. Say so in the README.
- Gemini 3.1 Flash Live watermarks its output audio. Worth noting if outputs are ever shared.
- Don't point the camera at anything you wouldn't send to a cloud API: documents with PII, payment cards, screens with credentials, etc. Put a one-line caution in the README.

### 13.6 What is explicitly out of scope for the MVP (and why that's OK)
- No auth, no multi-user, no network server: the app only makes outbound WebSocket calls to Google and binds no local ports, so the local attack surface is minimal.
- No persistence: nothing to encrypt at rest because nothing is written.
- These become real requirements the moment a UI, server, or multi-user mode is added (Phases 3–5).

---

## 14. Production Hardening Roadmap

**Phase 2 (Reliability):** auto-reconnect using session-resumption handles (retain the latest `session_resumption_update` handle, reconnect on GoAway); structured logging with privacy controls; mic/camera/API health checks; audio + camera device selectors; bounded playback queue; graceful terminal error states.

**Phase 3 (UI):** local web UI or Electron shell; transcript pane; camera preview; mute/unmute; pause-camera; voice + model selectors.

**Phase 4 (Agentic):** tool/function calling (3.1 Flash Live is notably better at this); opt-in memory; object-specific workflows; governance logging; safety/policy controls; role-specific prompts.

**Phase 5 (Deployment):** move the key to a backend; mint ephemeral client tokens; consider WebRTC / LiveKit / Pipecat for production-grade media; add auth, consent, session boundaries, observability, and cost controls.

---

## 15. Implementation Guardrails for an AI Coding Agent

1. Build the MVP first; no UI, DB, auth, memory, or deployment unless asked.
2. Preserve the single-session, four-loop architecture inside an `asyncio.TaskGroup`.
3. Keep all constants in `src/config.py`.
4. Use Python 3.13 and `uv`.
5. Use `asyncio`; wrap blocking camera/audio calls in `asyncio.to_thread(...)`.
6. Mic in: 16 kHz mono 16-bit PCM. Speaker out: 24 kHz mono 16-bit PCM.
7. Send camera as JPEG frames via `media=` (NOT `video=`), ≤1 FPS, resized within 768×768 preserving aspect ratio.
8. `system_instruction` must be a `types.Content`, not a raw string.
9. Initialize `self._playback_queue` and all state in `__init__`.
10. Implement `close()` to release camera + audio; call it in `finally`.
11. Detect GoAway and print a notice; do not die silently.
12. Verify SDK field names against the installed package before declaring done.
13. Never store audio/images/transcripts by default; never print the API key.
14. Use headphones during tests.
15. Run every verification command and the manual test script before declaring completion.

---

## 16. AI Agent Build Prompt

Copy/paste into the coding agent:

```text
You are a senior Python development agent. Build a local MVP called `live-vision-agent`.

GOAL
A Python 3.13 app using the Gemini Live API that lets a user speak via mic, show
objects via webcam, hear spoken AI responses via speakers/headphones, and see a
terminal transcript. It must behave like a real-time vision-and-voice agent, not a
chatbot.

ARCHITECTURE
One persistent Gemini Live session; four concurrent asyncio loops run inside an
asyncio.TaskGroup:
1. Stream mic audio up as raw 16-bit PCM, mono, 16 kHz, little-endian.
2. Stream webcam frames up as JPEG bytes, max 1 FPS, resized within 768x768
   preserving aspect ratio. SEND IMAGE FRAMES VIA media=, NOT video=.
3. Receive responses: push audio chunks to a playback queue, print output
   transcription, detect interruption, and detect GoAway.
4. Play response audio out as raw 16-bit PCM, mono, 24 kHz.

PROJECT STRUCTURE
.env, .env.example, .gitignore, README.md, pyproject.toml,
src/__init__.py (must exist), src/config.py, src/camera.py, src/audio_io.py, src/agent.py

SETUP (uv)
uv init live-vision-agent
uv python pin 3.13
uv add google-genai opencv-python pyaudio pillow python-dotenv
If pyaudio fails to build on Windows, use pipwin install pyaudio or a prebuilt
cp313 wheel. On macOS: brew install portaudio first. On Linux: apt-get install
portaudio19-dev first.

VERIFIED DEPENDENCY BASELINE
google-genai==2.8.0, opencv-python==4.13.0.92, pyaudio==0.2.14, pillow==12.2.0,
python-dotenv==1.2.2, Python 3.13. If exact versions don't resolve, use compatible
current versions, preserve the API contract, and commit uv.lock.

CONFIG (all constants in src/config.py)
- Read GEMINI_API_KEY from .env; fail fast with a clear message if missing; never
  print the key.
- Default model: gemini-3.1-flash-live-preview. Fallback: gemini-2.5-flash-live-preview.
- Voice: Zephyr. response_modalities=["AUDIO"].
- Enable output_audio_transcription AND input_audio_transcription.
- Enable context_window_compression with a sliding window.
- media_resolution="medium". thinking_level="minimal" IF the config type supports it
  (omit if not present; do not invent fields).

SYSTEM PROMPT
"You are a sharp, friendly assistant with live access to the user's camera. You can
see what they show you in real time. Answer out loud. Keep responses short and
conversational. When the user shows you something, describe or reason about what you
actually see. Do not claim certainty when visual evidence is unclear."
Wrap it as types.Content(parts=[types.Part(text=SYSTEM_PROMPT)]).

IMPLEMENTATION DETAILS
- camera.py: class Camera(__init__, read_jpeg_frame, close). OpenCV returns BGR;
  convert to RGB; use Image.thumbnail((768,768)) to preserve aspect ratio; encode JPEG
  in BytesIO. Return bytes or None. Raise a clear RuntimeError if the camera won't open.
- audio_io.py: class AudioIO(__init__, open_mic, open_speaker, close). pyaudio.paInt16,
  1 channel, mic rate 16000, speaker rate 24000, CHUNK_SIZE 1024. close() stops/closes
  both streams and terminates PyAudio.
- agent.py: class LiveVisionAgent(__init__, run, _stream_microphone, _stream_camera,
  _receive_responses, _play_audio, _drain_playback_queue, close).
  * In __init__, create the client, Camera, AudioIO, and self._playback_queue =
    asyncio.Queue().
  * run() opens one Live connection, prints a startup banner, and runs the four loops
    in an asyncio.TaskGroup.
  * Wrap blocking mic.read/speaker.write/camera reads in asyncio.to_thread.
  * On server_content.interrupted, drain the playback queue.
  * On message.go_away, print a notice with time_left (expected ~10-min connection limit).
  * close() releases camera and audio; main() calls it in finally; Ctrl+C exits cleanly.
- Never write audio, images, or transcripts to disk.

SDK GUARDRAIL (DO THIS BEFORE DECLARING DONE)
Field names on LiveConnectConfig and on server messages have drifted across SDK
versions. Inspect the installed SDK and confirm the exact spellings of
output_audio_transcription, input_audio_transcription, media_resolution, thinking
level, go_away, server_content, output_transcription, input_transcription, model_turn,
inline_data. Adapt to the installed SDK while preserving the logical config. Do not
invent fields. Prefer getattr with defaults in the receive loop so a renamed/missing
field degrades gracefully.

VERIFICATION (include in README; run all before completion)
1. PYTHONPATH=. uv run python -c "from src.config import GEMINI_API_KEY; print(bool(GEMINI_API_KEY))"   -> True
2. PYTHONPATH=. uv run python -c "from src.camera import Camera; c=Camera(); b=c.read_jpeg_frame(); print(len(b) if b else None); c.close()"   -> ~5,000-600,000
3. PYTHONPATH=. uv run python -c "from src.audio_io import AudioIO; a=AudioIO(); a.open_mic(); a.open_speaker(); print('audio OK'); a.close()"   -> audio OK
4. PYTHONPATH=. uv run python -m src.agent   -> speak, show object, hear answer, see transcript, interrupt, GoAway notice ~10 min, Ctrl+C clean exit.

SECURITY
- .env never committed; key never printed. Commit uv.lock. README states audio/video
  is streamed to Google's cloud API and that session resumption (server-side caching)
  is disabled by default.

Do not add features beyond this MVP until the above works.
```

---

## 17. Build Completion Checklist

- [ ] Project installs from scratch with `uv`; `uv.lock` committed.
- [ ] Python pinned to 3.13.
- [ ] `.env.example` present; `.env` ignored; no key in git history.
- [ ] API key validation works and never prints the key.
- [ ] Camera verification works (bytes, aspect preserved, releases on close).
- [ ] Audio verification works (16k mic / 24k speaker, releases on close).
- [ ] SDK field names confirmed against installed `google-genai`.
- [ ] Live session connects; startup banner prints.
- [ ] Mic, camera, playback all stream; transcript prints (output, and input if enabled).
- [ ] Image frames sent via `media=`, audio via `audio=`.
- [ ] Interruption drains queued playback.
- [ ] GoAway prints a notice instead of dying silently.
- [ ] `Ctrl+C` releases mic, speaker, and webcam.
- [ ] README covers setup (incl. OS-specific PyAudio), run, troubleshooting, privacy, and the ~10-min limit.

---

## 18. Reference Notes

Use current official Google Gemini Live API documentation as the source of truth for any SDK-level changes in: Live API connection syntax; Live model names; `types.LiveConnectConfig` fields; `types.SpeechConfig`; input/output audio transcription config; context window compression config; session resumption/GoAway behavior; `media_resolution`; thinking level; and tool/function-calling behavior.

Key facts validated for this v2 (as of 2026-06-12):
- `gemini-3.1-flash-live-preview` is the current low-latency audio-to-audio Live model.
- `google-genai==2.8.0` is the latest Python SDK on PyPI.
- Image frames are sent via `send_realtime_input(media=types.Blob(..., mime_type="image/jpeg"))`; audio via `send_realtime_input(audio=types.Blob(..., mime_type="audio/pcm;rate=16000"))`.
- A single Live connection lasts ~10 minutes; the server sends GoAway ~60s before the end; context window compression extends the session, session resumption (off by default here) caches data server-side ~2h.
- Gemini 3.1 Flash Live uses `thinking_level` (default `minimal`) rather than `thinking_budget`; proactive audio and affective dialogue are not yet supported on this model and their config must not be sent.
