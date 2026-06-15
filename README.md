# Live Vision Agent

A local Python prototype that connects your microphone and webcam to the Gemini
Live API so you can speak to an AI agent, show it things through your camera,
hear spoken responses, and read a terminal transcript.

This is a local development prototype. It does not write audio, video, frames,
or transcripts to your disk. **Session resumption is enabled** so the
conversation survives the Live API's frequent connection drops — this means
Google caches your audio/video/text server-side for ~2 hours (see Privacy). Set
`SESSION_RESUMPTION = False` in `src/config.py` for zero server-side retention.

## What it does

One persistent Gemini Live session runs four concurrent loops:

1. **Microphone upload** — streams 16 kHz mono 16-bit PCM audio to the API.
2. **Camera upload** — streams one webcam JPEG frame per second (resized within
   768×768, aspect ratio preserved).
3. **Response receive** — queues model audio for playback, prints the spoken
   transcript, handles interruptions and the GoAway connection notice.
4. **Speaker playback** — plays model audio at 24 kHz mono 16-bit PCM.

Speak naturally; interrupt mid-answer and the agent stops and listens.

## Hardware requirements

- A webcam (default device index 0).
- A microphone and speakers — **headphones strongly recommended** to stop the
  agent from hearing itself.
- Windows is the primary target (macOS/Linux work with the PyAudio steps below).

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.13 (pinned via
`.python-version`; do **not** use 3.14 — PyAudio wheels may be unavailable).

```bash
uv sync
```

### If PyAudio fails to install

PyAudio depends on the PortAudio C library and is the most common setup failure.

**Windows:** `uv sync` should pick up the prebuilt cp313 wheel. If it tries to
compile and fails:

```bash
uv pip install pipwin
pipwin install pyaudio
# or download a matching cp313 wheel and: uv pip install path\to\PyAudio-...-cp313-...whl
```

**macOS:**

```bash
brew install portaudio
uv sync
```

**Linux (Debian/Ubuntu):**

```bash
sudo apt-get install -y portaudio19-dev
uv sync
```

### API key

Create a `.env` file in the project root (copy `.env.example`):

```bash
GEMINI_API_KEY=your_api_key_here
```

Get a key from [Google AI Studio](https://aistudio.google.com/). Never commit
`.env`; it is in `.gitignore`. The app never prints or logs the key.

## Verification

Run these from the project root before the first live run:

```bash
# 1. Key loads (expect: True)
PYTHONPATH=. uv run python -c "from src.config import GEMINI_API_KEY; print(bool(GEMINI_API_KEY))"

# 2. Camera works (expect: a number ~5,000-600,000; webcam light may blink)
PYTHONPATH=. uv run python -c "from src.camera import Camera; c=Camera(); b=c.read_jpeg_frame(); print(len(b) if b else None); c.close()"

# 3. Audio devices open (expect: audio OK)
PYTHONPATH=. uv run python -c "from src.audio_io import AudioIO; a=AudioIO(); a.open_mic(); a.open_speaker(); print('audio OK'); a.close()"
```

On Windows PowerShell, set the variable separately:

```powershell
$env:PYTHONPATH = "."
uv run python -c "from src.config import GEMINI_API_KEY; print(bool(GEMINI_API_KEY))"
```

## Run

**With the browser HUD (recommended):**

```bash
PYTHONPATH=. uv run python -m src.server
```

Opens `http://127.0.0.1:8800` — camera view, live transcript, mic waveform,
mute/pause/restart controls (keys `M` / `C` / `R`). The browser is a dashboard
only: mic, speaker, and webcam stay in the Python process, and the API key
never reaches the page. The server binds to localhost only.

**Terminal only:**

```bash
PYTHONPATH=. uv run python -m src.agent
```

Either way: wait for the startup banner, then speak. Hold something up to the
camera and ask about it. Press `Ctrl+C` in the terminal to quit; the mic,
speaker, and webcam are released on exit.

**About connection drops:** on the current model/tier the Live connection
recycles on a hard server clock (~2 minutes), independent of anything in this
app. The agent reconnects automatically; with `SESSION_RESUMPTION = True`
(default) the conversation continues across the reconnect with context intact.
Set it to `False` to reset context each time and keep nothing cached
server-side.

## Common errors and fixes

- **`GEMINI_API_KEY is missing`** — create `.env` in the project root with the
  key and run from the root.
- **PyAudio fails on Windows** — wrong Python version or no wheel. Re-pin with
  `uv python pin 3.13`, then `uv sync`. Still failing: see the PyAudio section
  above.
- **No camera found** — another app owns the webcam (Zoom, Teams, OBS, browser
  tabs), OS permission is denied, or it's the wrong index. Close other apps,
  check OS camera permissions, or try `Camera(device_index=1)`.
- **Agent talks over itself / hears itself** — speaker output is feeding back
  into the mic. Use headphones, lower the volume, or move the mic.
- **Audio is static / too fast / too slow** — sample-rate mismatch. Mic must
  send 16 kHz; speaker must play 24 kHz; both mono 16-bit PCM.
- **WebSocket closes with invalid payload** — wrong MIME type or field. Audio
  MIME must be `audio/pcm;rate=16000`, images `image/jpeg` sent via `video=` at
  ≤1 FPS, and `response_modalities=["AUDIO"]`.
- **`TypeError` / unexpected keyword on `send_realtime_input` or
  `LiveConnectConfig`** — the SDK renamed a field. Inspect the installed SDK
  (`types.LiveConnectConfig.model_fields`,
  `help(session.send_realtime_input)`) and adapt.
- **Frequent connection drops** (`keepalive ping timeout`) — inherent to
  `gemini-3.1-flash-live-preview`. The agent hides them with **make-before-break
  rotation**: it proactively opens the next (resumed) connection and switches to
  it every `PROACTIVE_ROTATE_SECONDS` (default 20s) — before the drop — so the
  conversation continues seamlessly with full context. If a drop still sneaks in
  early, fast keepalive (`WS_PING_TIMEOUT_SECONDS`) detects it in ~6s and
  reconnects. If you'd rather have steadier single connections (losing context on
  any drop), switch to `gemini-2.5-flash-native-audio-latest` and set
  `SESSION_RESUMPTION = False` (that model can't resume). List Live-capable
  models for your key with:
  `uv run python -c "from google import genai; from src.config import require_api_key; [print(m.name) for m in genai.Client(api_key=require_api_key()).models.list() if 'bidiGenerateContent' in (m.supported_actions or [])]"`
- **Session ends after ~10 minutes** — expected; see Known limitations.

## Privacy

- **Your microphone audio and webcam video are streamed to Google's Gemini Live
  API for processing.** This is a cloud service, not an offline/on-device model.
- **Session resumption is ON by default**, which is what lets the conversation
  survive the ~2-minute connection drops. The tradeoff: Google caches your text,
  audio, video, and model outputs server-side for up to ~2 hours to enable the
  resume. Set `SESSION_RESUMPTION = False` in `src/config.py` if you need zero
  server-side retention — the conversation will reset on each reconnect instead.
- Nothing is written to **your disk**: no recordings, no frames, no transcripts
  beyond the terminal/HUD output.
- Don't point the camera at anything you wouldn't send to a cloud API:
  documents with PII, payment cards, or screens showing credentials.
- Gemini Live watermarks its output audio — worth knowing if you share outputs.

## Known limitations

- **Connections recycle every ~2 minutes** on the current model/tier — a hard
  server-side limit, confirmed to fire regardless of config (verified by
  disabling transcription, compression, and even the camera; all still dropped
  at ~2 min). The agent auto-reconnects; with session resumption on (default)
  the conversation continues seamlessly. This is the documented Live API
  behavior, not a bug in this app.
- One camera, one mic, default devices only.
- Unbounded playback queue (fine for the MVP; bounded queue is a hardening
  item).

## Future improvements

- Audio/camera device selectors and health checks.
- Voice + model selectors in the HUD.
- Tool/function calling.

See [docs/BUILD_SPEC.md](docs/BUILD_SPEC.md) for the full build specification.
