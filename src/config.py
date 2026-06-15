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
# gemini-3.1-flash-live-preview drops its connection often (keepalive timeout
# ~40-80s after a turn), BUT it is the only Live model here that emits resumable
# session handles. Paired with SESSION_RESUMPTION + AUTO_RECONNECT below, each
# drop is survived seamlessly WITH conversation context intact (verified:
# the agent recalls facts told before a reconnect).
# Alternative: gemini-2.5-flash-native-audio-latest is steadier per-connection
# but cannot resume — any drop wipes the conversation. The spec's original
# fallback (gemini-2.5-flash-live-preview) no longer exists in the API.
MODEL: str = "gemini-3.1-flash-live-preview"
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
# google-genai 2.8.0 enums: types.MediaResolution / types.ThinkingLevel
MEDIA_RESOLUTION: str = "MEDIA_RESOLUTION_MEDIUM"  # _LOW | _MEDIUM | _HIGH
THINKING_LEVEL: str = "MINIMAL"                    # MINIMAL | LOW | MEDIUM | HIGH
CWC_TRIGGER_TOKENS: int = 25_600
CWC_TARGET_TOKENS: int = 12_800

# --- Reliability ---
# gemini-3.1-flash-live drops its WebSocket every ~40-80s (keepalive bug). To
# keep the conversation seamless we rotate connections PROACTIVELY using
# make-before-break: open the next (resumed) connection and switch the loops over
# to it BEFORE the old one dies, so there is no dead-air gap waiting for a drop
# to be detected.
AUTO_RECONNECT: bool = True
RECONNECT_DELAY_SECONDS: float = 0.4    # only used after an *unexpected* drop
MAX_RECONNECT_FAILURES: int = 10        # consecutive failed connect attempts before giving up

# Proactively rotate the connection this many seconds after it opens, before the
# keepalive death (observed anywhere from ~20s to ~80s — variable). Kept low so
# the seamless make-before-break rotation usually wins the race against a drop.
# Lower = more seamless but more frequent handoffs (more resume overhead).
PROACTIVE_ROTATE_SECONDS: float = 20.0
# Prefer to rotate during a pause; if the agent is still speaking at the deadline,
# wait up to this long for silence before rotating anyway (avoids mid-sentence
# rotation, which resumption may not continue smoothly).
ROTATE_SPEAKING_GRACE_SECONDS: float = 5.0

# WebSocket keepalive: detect a genuinely dead connection fast (the SDK otherwise
# inherits the websockets default ~20s ping timeout = ~20s of dead air).
WS_PING_INTERVAL_SECONDS: float = 5.0
WS_PING_TIMEOUT_SECONDS: float = 6.0
WS_OPEN_TIMEOUT_SECONDS: float = 12.0

# Session resumption: carry conversation context across reconnects so the agent
# does not forget the chat every time the connection recycles.
# PRIVACY TRADEOFF: when True, Google caches your audio/video/text server-side
# for ~2h to enable the resume. This intentionally reverses the original spec's
# "resumption off" stance — see README privacy section. Set False for zero
# server-side retention (at the cost of context loss on every reconnect).
SESSION_RESUMPTION: bool = True

# --- Local HUD server (src/server.py) ---
SERVER_HOST: str = "127.0.0.1"   # localhost only; never bind 0.0.0.0 without auth
SERVER_PORT: int = 8800
AUTO_OPEN_BROWSER: bool = True

# --- Behavior toggles ---
PRINT_INPUT_TRANSCRIPTION: bool = True   # show what the user said, not just the model
SHOW_CAMERA_PREVIEW: bool = False        # debug only: local preview window of captured frames

SYSTEM_PROMPT: str = """
You are a sharp, friendly assistant with live access to the user's camera.
You can see what they show you in real time. Answer out loud.
Keep responses short and conversational. When the user shows you something,
describe or reason about what you actually see. Do not claim certainty when
visual evidence is unclear.
""".strip()
