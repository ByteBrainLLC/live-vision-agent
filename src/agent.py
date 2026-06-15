import asyncio
import base64

import numpy as np
import websockets.exceptions
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
    AUTO_RECONNECT,
    RECONNECT_DELAY_SECONDS,
    MAX_RECONNECT_FAILURES,
    SESSION_RESUMPTION,
)
from src.camera import Camera
from src.audio_io import AudioIO


class LiveVisionAgent:
    def __init__(self, on_event=None) -> None:
        self._client = genai.Client(api_key=require_api_key())
        self._camera = Camera()
        self._audio = AudioIO()
        self._playback_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._session = None
        self._on_event = on_event
        self._speaking = False
        # Latest session-resumption handle: lets a reconnect continue the same
        # logical conversation instead of starting blank. None on first connect.
        self._resumption_handle: str | None = None
        # Control flags, toggled live by the HUD server (or any embedder).
        self.mic_muted = False
        self.camera_paused = False

    def _emit(self, type_: str, **data) -> None:
        if self._on_event is not None:
            self._on_event({"type": type_, **data})

    def _build_config(self) -> types.LiveConnectConfig:
        # Field names verified against google-genai 2.8.0 (see docs/BUILD_SPEC.md §8.4).
        resumption = None
        if SESSION_RESUMPTION:
            # Empty handle on first connect requests one; later it resumes context.
            resumption = types.SessionResumptionConfig(handle=self._resumption_handle)
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
            session_resumption=resumption,
            context_window_compression=types.ContextWindowCompressionConfig(
                trigger_tokens=CWC_TRIGGER_TOKENS,
                sliding_window=types.SlidingWindow(target_tokens=CWC_TARGET_TOKENS),
            ),
        )

    async def run(self) -> None:
        consecutive_failures = 0
        while True:
            self._emit("status", state="connecting")
            try:
                connection_lost = await self._run_session()
                consecutive_failures = 0
            except BaseExceptionGroup:
                raise  # a non-connection error inside the loops is a real bug — surface it
            except Exception as exc:
                # Failed before the session was usable (network down, bad model name,
                # quota). Retry a few times, then give up loudly.
                consecutive_failures += 1
                print(
                    f"\n[connect failed ({consecutive_failures}/{MAX_RECONNECT_FAILURES}): {exc}]"
                )
                connection_lost = True
                if consecutive_failures >= MAX_RECONNECT_FAILURES:
                    print("[giving up after repeated connection failures]")
                    self._emit("status", state="disconnected")
                    return
            if not (connection_lost and AUTO_RECONNECT):
                self._emit("status", state="disconnected")
                return
            self._emit("status", state="reconnecting")
            if SESSION_RESUMPTION and self._resumption_handle is not None:
                print("\n[reconnecting — resuming conversation]")
            else:
                print(
                    "\n[reconnecting — fresh session, prior context not retained]"
                )
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def _run_session(self) -> bool:
        """Run one Live connection to completion. Returns True if it was lost
        (reconnectable) and False on deliberate shutdown."""
        connection_lost = False
        config = self._build_config()
        async with self._client.aio.live.connect(model=MODEL, config=config) as session:
            self._session = session
            self._speaking = False
            self._drain_playback_queue()
            print("\n=== Live Vision Agent connected ===")
            print("Speak naturally. Show objects to the camera. Press Ctrl+C to quit.\n")
            self._emit("status", state="listening")
            self._emit("controls", mic_muted=self.mic_muted, camera_paused=self.camera_paused)
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self._stream_microphone())
                    tg.create_task(self._stream_camera())
                    tg.create_task(self._receive_responses())
                    tg.create_task(self._play_audio())
            except* websockets.exceptions.ConnectionClosed:
                # Covers the polite ~10-min recycle and the abrupt keepalive
                # timeouts the preview backends are prone to.
                connection_lost = True
                print("\n[connection lost — the Live API WebSocket closed]")
            except* asyncio.CancelledError:
                pass  # expected on shutdown
        self._session = None
        return connection_lost

    async def restart_session(self) -> None:
        """Force the current session closed; run() reconnects automatically."""
        session = self._session
        if session is not None:
            await session.close()

    async def _stream_microphone(self) -> None:
        mic = self._audio.open_mic()
        while True:
            chunk = await asyncio.to_thread(
                mic.read, CHUNK_SIZE, exception_on_overflow=False
            )
            if self._on_event is not None:
                if self.mic_muted:
                    rms = 0.0
                else:
                    samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
                    rms = float(np.sqrt(np.mean(samples**2))) / 32768.0
                self._emit("miclevel", rms=round(rms, 4))
            if self.mic_muted:
                continue  # keep draining the device so unmute resumes instantly
            await self._session.send_realtime_input(
                audio=types.Blob(
                    data=chunk,
                    mime_type=f"audio/pcm;rate={SEND_SAMPLE_RATE}",
                )
            )

    async def _stream_camera(self) -> None:
        while True:
            if self.camera_paused:
                await asyncio.sleep(FRAME_INTERVAL_SECONDS)
                continue
            frame = await asyncio.to_thread(self._camera.read_jpeg_frame)
            if frame is not None:
                if self._on_event is not None:
                    self._emit(
                        "frame", jpeg_b64=base64.b64encode(frame).decode("ascii")
                    )
                # Image frames go via video=. The media= kwarg serializes to the
                # deprecated realtime_input.media_chunks, which the Live API rejects.
                await self._session.send_realtime_input(
                    video=types.Blob(data=frame, mime_type="image/jpeg")
                )
            await asyncio.sleep(FRAME_INTERVAL_SECONDS)

    async def _receive_responses(self) -> None:
        async for message in self._session.receive():
            # Connection-lifecycle: GoAway arrives shortly before the connection ends
            if getattr(message, "go_away", None) is not None:
                secs = getattr(message.go_away, "time_left", None)
                print(
                    f"\n[session ending in ~{secs}; expected on the Live connection limit]"
                )
                self._emit("goaway", time_left=str(secs))

            # Stash the latest resumption handle so the next connect continues
            # this conversation instead of starting blank.
            sru = getattr(message, "session_resumption_update", None)
            if sru is not None and getattr(sru, "resumable", False):
                new_handle = getattr(sru, "new_handle", None)
                if new_handle:
                    self._resumption_handle = new_handle

            content = getattr(message, "server_content", None)
            if content is None:
                continue

            if getattr(content, "interrupted", False):
                self._drain_playback_queue()
                self._set_speaking(False)
                self._emit("turn", event="interrupted")

            model_turn = getattr(content, "model_turn", None)
            if model_turn:
                for part in model_turn.parts:
                    inline = getattr(part, "inline_data", None)
                    if inline and inline.data:
                        self._playback_queue.put_nowait(inline.data)

            out_tx = getattr(content, "output_transcription", None)
            if out_tx and out_tx.text:
                print(out_tx.text, end="", flush=True)
                self._emit("transcript", role="agent", text=out_tx.text)

            in_tx = getattr(content, "input_transcription", None)
            if in_tx and in_tx.text:
                if PRINT_INPUT_TRANSCRIPTION:
                    print(f"\n[you] {in_tx.text}", flush=True)
                self._emit("transcript", role="user", text=in_tx.text)

            if getattr(content, "turn_complete", False):
                self._emit("turn", event="complete")

    async def _play_audio(self) -> None:
        speaker = self._audio.open_speaker()
        while True:
            chunk = await self._playback_queue.get()
            self._set_speaking(True)
            await asyncio.to_thread(speaker.write, chunk)
            self._playback_queue.task_done()
            if self._playback_queue.empty():
                self._set_speaking(False)

    def _set_speaking(self, speaking: bool) -> None:
        if self._speaking != speaking:
            self._speaking = speaking
            self._emit("status", state="speaking" if speaking else "listening")

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
