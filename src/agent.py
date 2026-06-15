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
    PROACTIVE_ROTATE_SECONDS,
    ROTATE_SPEAKING_GRACE_SECONDS,
    WS_PING_INTERVAL_SECONDS,
    WS_PING_TIMEOUT_SECONDS,
    WS_OPEN_TIMEOUT_SECONDS,
)
from src.camera import Camera
from src.audio_io import AudioIO


class _AgentStop(Exception):
    """Internal signal: the supervisor has stopped for good (give-up/shutdown)."""


class LiveVisionAgent:
    def __init__(self, on_event=None) -> None:
        # Fast keepalive so a genuinely dead socket is detected in ~6s, not ~20s.
        # These kwargs are filtered by the SDK down to what websockets.connect
        # accepts (verified: ping_interval/ping_timeout/open_timeout all pass).
        http_options = types.HttpOptions(
            async_client_args={
                "ping_interval": WS_PING_INTERVAL_SECONDS,
                "ping_timeout": WS_PING_TIMEOUT_SECONDS,
                "open_timeout": WS_OPEN_TIMEOUT_SECONDS,
            }
        )
        self._client = genai.Client(api_key=require_api_key(), http_options=http_options)
        self._camera = Camera()
        self._audio = AudioIO()
        self._playback_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._session = None        # always points to the current live session
        self._on_event = on_event
        self._speaking = False
        self._shutdown = asyncio.Event()
        self._rotate_now = asyncio.Event()  # set by restart_session() to force a rotate
        # Latest session-resumption handle: lets the next connection continue the
        # same logical conversation instead of starting blank. None on first connect.
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
        # The send loops and playback run for the whole lifetime; the supervisor
        # keeps a live session under them and rotates it make-before-break.
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._stream_microphone())
                tg.create_task(self._stream_camera())
                tg.create_task(self._play_audio())
                tg.create_task(self._session_supervisor())
        except* _AgentStop:
            pass  # supervisor gave up or shut down cleanly
        except* asyncio.CancelledError:
            pass  # expected on Ctrl+C

    async def _open_session(self):
        """Open one Live connection and return (cm, session). Raises on failure."""
        cm = self._client.aio.live.connect(model=MODEL, config=self._build_config())
        session = await cm.__aenter__()
        return cm, session

    async def _session_supervisor(self) -> None:
        """Maintain exactly one live session, rotating proactively (make-before-
        break) so the ~40-80s keepalive drop never shows up as dead air."""
        failures = 0
        cm = None
        recv_task = None
        first = True
        try:
            while not self._shutdown.is_set():
                self._emit("status", state="connecting" if first else "reconnecting")
                try:
                    new_cm, new_session = await self._open_session()
                except Exception as exc:
                    failures += 1
                    print(f"\n[connect failed ({failures}/{MAX_RECONNECT_FAILURES}): {exc}]")
                    if failures >= MAX_RECONNECT_FAILURES:
                        print("[giving up after repeated connection failures]")
                        break
                    await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                    continue
                failures = 0

                # Make-before-break swap: new session is fully handshaked here, so
                # switch sends/receives to it, THEN tear the old one down.
                old_cm, old_recv = cm, recv_task
                self._session = new_session
                cm = new_cm
                self._rotate_now.clear()
                recv_task = asyncio.create_task(self._receive_responses(new_session))
                await self._teardown(old_cm, old_recv)

                if first:
                    print("\n=== Live Vision Agent connected ===")
                    print("Speak naturally. Show objects to the camera. Press Ctrl+C to quit.\n")
                    first = False
                elif SESSION_RESUMPTION and self._resumption_handle is not None:
                    print("\n[rotated connection — conversation continues]")
                self._emit("status", state="speaking" if self._speaking else "listening")
                self._emit("controls", mic_muted=self.mic_muted, camera_paused=self.camera_paused)

                reason = await self._wait_until_rotate(recv_task)
                if reason == "drop":
                    if not AUTO_RECONNECT:
                        break
                    print("\n[connection dropped — reconnecting]")
                    await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                # reason == "proactive" or "forced": loop back and make-before-break
        finally:
            self._shutdown.set()
            await self._teardown(cm, recv_task)
            self._session = None
            self._emit("status", state="disconnected")
        # Reached only on a deliberate stop (give-up / no-reconnect drop), NOT on
        # Ctrl+C cancellation (which propagates through the finally above). Signal
        # the sibling loops to stop.
        raise _AgentStop()

    async def _teardown(self, cm, recv_task) -> None:
        if recv_task is not None:
            recv_task.cancel()
            try:
                await recv_task
            except (asyncio.CancelledError, Exception):
                pass
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass

    async def _wait_until_rotate(self, recv_task: asyncio.Task) -> str:
        """Block until it's time to rotate. Returns why: 'drop' (the connection
        died), 'forced' (restart requested), or 'proactive' (scheduled)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + PROACTIVE_ROTATE_SECONDS
        while True:
            if self._shutdown.is_set():
                return "drop"
            if recv_task.done():
                return "drop"
            if self._rotate_now.is_set():
                return "forced"
            if AUTO_RECONNECT:
                now = loop.time()
                if now >= deadline and (
                    not self._speaking or now >= deadline + ROTATE_SPEAKING_GRACE_SECONDS
                ):
                    return "proactive"
            await asyncio.sleep(0.2)

    async def restart_session(self) -> None:
        """Ask the supervisor to rotate the connection now (HUD restart button)."""
        self._rotate_now.set()

    async def _send_realtime(self, **kwargs) -> None:
        """Send to the current session, tolerating the brief mid-rotation window
        where it may be missing or just-closed."""
        session = self._session
        if session is None:
            await asyncio.sleep(0.05)
            return
        try:
            await session.send_realtime_input(**kwargs)
        except websockets.exceptions.ConnectionClosed:
            pass  # rotating; the next iteration targets the new session

    async def _stream_microphone(self) -> None:
        mic = self._audio.open_mic()
        while not self._shutdown.is_set():
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
            await self._send_realtime(
                audio=types.Blob(
                    data=chunk,
                    mime_type=f"audio/pcm;rate={SEND_SAMPLE_RATE}",
                )
            )

    async def _stream_camera(self) -> None:
        while not self._shutdown.is_set():
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
                await self._send_realtime(
                    video=types.Blob(data=frame, mime_type="image/jpeg")
                )
            await asyncio.sleep(FRAME_INTERVAL_SECONDS)

    async def _receive_responses(self, session) -> None:
        try:
            async for message in session.receive():
                # Connection-lifecycle: GoAway arrives shortly before the end
                if getattr(message, "go_away", None) is not None:
                    secs = getattr(message.go_away, "time_left", None)
                    self._emit("goaway", time_left=str(secs))

                # Stash the latest resumption handle (only from the current
                # session, so a dying old session can't regress it).
                sru = getattr(message, "session_resumption_update", None)
                if (
                    sru is not None
                    and getattr(sru, "resumable", False)
                    and session is self._session
                ):
                    new_handle = getattr(sru, "new_handle", None)
                    if new_handle:
                        self._resumption_handle = new_handle

                content = getattr(message, "server_content", None)
                if content is None:
                    continue

                # Ignore content from a session being rotated out, to avoid
                # double audio / stray transcript during a handoff.
                if session is not self._session:
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
        except websockets.exceptions.ConnectionClosed:
            pass  # this session ended; the supervisor will rotate

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
