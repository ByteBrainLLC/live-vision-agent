"""Local HUD server: serves the browser UI and bridges it to the agent.

The browser is a dashboard and remote control only — mic, speaker, and webcam
stay in this Python process, and the API key never reaches the page.
Run: PYTHONPATH=. uv run python -m src.server
"""
import asyncio
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.agent import LiveVisionAgent
from src.config import SERVER_HOST, SERVER_PORT, AUTO_OPEN_BROWSER

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI()
# Serve brand assets (bytebrain-wordmark.png, bytebrain-icon.png) and any other
# files from src/static/ at /static. The page itself stays at "/".
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
_clients: set[asyncio.Queue] = set()
_agent: LiveVisionAgent | None = None


def _broadcast(event: dict) -> None:
    for queue in list(_clients):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # Slow browser tab: drop the oldest event, keep the stream live.
            try:
                queue.get_nowait()
                queue.put_nowait(event)
            except asyncio.QueueEmpty:
                pass


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


async def _handle_command(msg: dict) -> None:
    if _agent is None:
        return
    cmd = msg.get("cmd")
    if cmd == "mic":
        _agent.mic_muted = bool(msg.get("muted"))
    elif cmd == "camera":
        _agent.camera_paused = bool(msg.get("paused"))
    elif cmd == "restart":
        await _agent.restart_session()
    _broadcast(
        {
            "type": "controls",
            "mic_muted": _agent.mic_muted,
            "camera_paused": _agent.camera_paused,
        }
    )


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    _clients.add(queue)
    if _agent is not None:
        queue.put_nowait(
            {
                "type": "controls",
                "mic_muted": _agent.mic_muted,
                "camera_paused": _agent.camera_paused,
            }
        )

    async def sender() -> None:
        while True:
            await websocket.send_json(await queue.get())

    async def receiver() -> None:
        while True:
            await _handle_command(await websocket.receive_json())

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(sender())
            tg.create_task(receiver())
    except* Exception:
        pass  # browser tab closed/reloaded; nothing to clean up but the queue
    finally:
        _clients.discard(queue)


async def main() -> None:
    global _agent
    _agent = LiveVisionAgent(on_event=_broadcast)
    server = uvicorn.Server(
        uvicorn.Config(app, host=SERVER_HOST, port=SERVER_PORT, log_level="warning")
    )
    url = f"http://{SERVER_HOST}:{SERVER_PORT}"
    print(f"HUD: {url}")
    if AUTO_OPEN_BROWSER:
        asyncio.get_running_loop().call_later(1.0, webbrowser.open, url)
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_agent.run())
            tg.create_task(server.serve())
    except* asyncio.CancelledError:
        pass
    finally:
        _agent.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")
