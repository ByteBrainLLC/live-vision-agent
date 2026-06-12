import asyncio

from google import genai
from google.genai import types

from src.config import (
    require_api_key,
    MODEL,
    VOICE,
    SYSTEM_PROMPT,
    MEDIA_RESOLUTION,
    THINKING_LEVEL,
    SEND_SAMPLE_RATE,
    CHUNK_SIZE,
    FRAME_INTERVAL_SECONDS,
    CWC_TRIGGER_TOKENS,
    CWC_TARGET_TOKENS,
    PRINT_INPUT_TRANSCRIPTION,
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
        # Field names verified against google-genai 2.8.0 (see docs/BUILD_SPEC.md §8.4).
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            media_resolution=MEDIA_RESOLUTION,
            system_instruction=types.Content(parts=[types.Part(text=SYSTEM_PROMPT)]),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)
                )
            ),
            thinking_config=types.ThinkingConfig(thinking_level=THINKING_LEVEL),
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
                # Image frames go via video=. The media= kwarg serializes to the
                # deprecated realtime_input.media_chunks, which the Live API rejects.
                await self._session.send_realtime_input(
                    video=types.Blob(data=frame, mime_type="image/jpeg")
                )
            await asyncio.sleep(FRAME_INTERVAL_SECONDS)

    async def _receive_responses(self) -> None:
        async for message in self._session.receive():
            # Connection-lifecycle: GoAway arrives ~60s before the ~10-min connection ends
            if getattr(message, "go_away", None) is not None:
                secs = getattr(message.go_away, "time_left", None)
                print(
                    f"\n[session ending in ~{secs}; expected on the ~10-min Live connection limit]"
                )

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
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Ctrl+C lands here on Windows after the event loop unwinds; devices
        # are already released by main()'s finally.
        print("\nShutting down...")
