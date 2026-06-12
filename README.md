# Live Vision Agent

A local Python prototype that connects your microphone and webcam to the Gemini
Live API so you can speak to an AI agent, show it things through your camera,
hear spoken responses, and read a terminal transcript.

This is a local development prototype. It does not record or store audio, video,
frames, or transcripts. Session resumption is intentionally disabled, so no
conversation data is cached server-side beyond what the live request requires.

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

```bash
PYTHONPATH=. uv run python -m src.agent
```

Wait for the startup banner, then speak. Hold something up to the camera and ask
about it. Press `Ctrl+C` to quit; the mic, speaker, and webcam are released on
exit.

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
- **Session ends after ~10 minutes** — expected; see Known limitations.

## Privacy

- **Your microphone audio and webcam video are streamed to Google's Gemini Live
  API for processing.** This is a cloud service, not an offline/on-device model.
- **Session resumption (server-side caching) is disabled by default.** Enabling
  it would cache your text, audio, video, and model outputs on Google's servers
  for up to ~2 hours.
- Nothing is written to disk: no recordings, no frames, no transcripts beyond
  the terminal output.
- Don't point the camera at anything you wouldn't send to a cloud API:
  documents with PII, payment cards, or screens showing credentials.
- Gemini Live watermarks its output audio — worth knowing if you share outputs.

## Known limitations

- **~10-minute connection limit.** A single Live connection lasts roughly 10
  minutes; the server sends a GoAway notice ~60 seconds before the end, which
  the agent prints to the terminal. Auto-reconnect is a future improvement, not
  part of this MVP.
- One camera, one mic, default devices only.
- Unbounded playback queue (fine for the MVP; bounded queue is a hardening
  item).

## Future improvements

- Auto-reconnect using session-resumption handles (opt-in, with the privacy
  caveat above).
- Audio/camera device selectors and health checks.
- Local web UI with transcript pane and camera preview.
- Tool/function calling.

See [docs/BUILD_SPEC.md](docs/BUILD_SPEC.md) for the full build specification.
