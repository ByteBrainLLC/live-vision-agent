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
# google-genai 2.8.0 enums: types.MediaResolution / types.ThinkingLevel
MEDIA_RESOLUTION: str = "MEDIA_RESOLUTION_MEDIUM"  # _LOW | _MEDIUM | _HIGH
THINKING_LEVEL: str = "MINIMAL"                    # MINIMAL | LOW | MEDIUM | HIGH
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
