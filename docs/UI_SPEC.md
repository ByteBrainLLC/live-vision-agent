# Live Vision Agent — UI Specification (Phase 3)

**Status:** Draft v1 · 2026-06-12
**Builds on:** [BUILD_SPEC.md](BUILD_SPEC.md) Phase 3 roadmap
**Shell decision:** Local web UI (Python backend + browser page at localhost)
**Visual direction:** Mission-control HUD (dark, live telemetry feel)

---

## Problem Statement

The agent works, but its only interface is a scrolling terminal: transcripts interleave with status lines, there's no way to see what the camera sees, no way to mute the mic or pause the camera without killing the process, and the ~10-minute connection limit arrives as a one-line notice that's easy to miss. For a tool whose whole point is *live* multimodal presence, the terminal hides the liveness — the user can't tell at a glance whether the agent is hearing them, seeing them, or about to disconnect.

## Goals

1. **Glanceable liveness** — within 1 second of looking at the screen, the user knows: mic live/muted, camera live/paused, agent listening/speaking, and time remaining on the connection.
2. **Control without restart** — mute/unmute mic and pause/resume camera take effect in <500 ms without dropping the session.
3. **Readable conversation** — user and agent turns render as a distinct, scrollable transcript, not interleaved print statements.
4. **No new privacy surface** — the API key never reaches the browser; nothing new is written to disk; the server binds to 127.0.0.1 only.
5. **Cool factor** — the HUD aesthetic (waveform, camera feed, session ring) makes the demo feel like mission control, not a webpage with buttons.

## Non-Goals

- **No browser-side media capture or playback.** Mic, speaker, and webcam stay in the Python process (PyAudio/OpenCV as today). The browser is a dashboard and remote control, not a media client — this avoids WebRTC entirely and keeps the audio path identical to the proven MVP. (Phase 5 territory.)
- **No remote access / multi-device.** Localhost only. Exposing the UI off-machine would require auth and ephemeral tokens (spec §13.1, Phase 5).
- **No persistence.** No transcript history across sessions, no settings DB beyond `config.py` / `.env`. Nothing to encrypt because nothing is stored.
- **No auto-reconnect.** GoAway is *surfaced* beautifully, but reconnection remains Phase 2 backend work; the UI just gets a "Reconnect" button wired to whatever the backend offers.
- **No mobile layout.** Desktop browser, single breakpoint. It's a laptop demo tool.

## Architecture (constrains the requirements)

```text
┌────────────────────────────  Python process  ───────────────────────────┐
│  LiveVisionAgent loops (mic / camera / receive / playback — unchanged)  │
│            │ events (transcripts, status, frames)   ▲ commands          │
│            ▼                                        │                   │
│  FastAPI + WebSocket hub  ── serves static HUD page at 127.0.0.1:PORT  │
└──────────────────────────────────────────────────────────────────────────┘
                       │ one WebSocket, JSON messages
                       ▼
            Browser HUD (vanilla JS or lightweight framework)
```

- **One new dependency set:** `fastapi`, `uvicorn` (+ static files). No Node toolchain required to *run*.
- **Event channel (server → browser):** `transcript.user`, `transcript.agent`, `status.listening|speaking|interrupted`, `session.goaway {seconds_left}`, `session.closed`, `camera.frame {jpeg base64}` (reuses the same 1 FPS frames already captured — zero extra camera reads), `levels.mic {rms}` for the waveform.
- **Command channel (browser → server):** `mic.mute`, `mic.unmute`, `camera.pause`, `camera.resume`, `session.restart {voice?, model?}`.
- **Key safety:** the browser receives only events above. The Gemini key, the Live session, and all Google traffic remain in Python (§13.1 honored by construction).

## User Stories

- As the operator, I want to see the live camera frame the agent is seeing, so I can frame objects correctly before asking about them.
- As the operator, I want one-click mic mute, so I can talk to someone in the room without the agent responding.
- As the operator, I want to pause the camera, so I can keep talking while something sensitive is in view.
- As the operator, I want user and agent transcripts visually separated with timestamps, so I can review what was said without scrollback archaeology.
- As the operator, I want a visible countdown when GoAway arrives, so the session ending never surprises me mid-demo.
- As the operator, I want to pick a different voice or model and restart the session from the UI, so I don't have to edit config.py and relaunch.
- As the operator, I want an unmistakable "agent is speaking" indicator, so I know when interrupting will cut it off.
- As the operator, I want the UI to show a clear disconnected state with a reconnect action, so a dead session never looks alive.

## Requirements

### P0 — Must-have (the UI doesn't ship without these)

| # | Requirement | Acceptance criteria |
|---|---|---|
| P0-1 | **HUD page served locally** | `uv run python -m src.server` opens the agent *and* serves the HUD at `http://127.0.0.1:8800`; binds 127.0.0.1 only; page loads with no external CDN dependencies (works offline). |
| P0-2 | **Live camera panel** | Shows the most recent JPEG frame the agent sent (≤1 s stale); paused state shows a frozen frame with a "CAMERA PAUSED" overlay; failure shows an error state, never a stale frame pretending to be live. |
| P0-3 | **Transcript pane** | User turns and agent turns are visually distinct (color + alignment); auto-scrolls during conversation; partial agent transcript streams in token-by-token as today. |
| P0-4 | **Mic mute / camera pause toggles** | Toggling stops audio chunks / frame sends within 500 ms without closing the Live session; state is unmistakable (icon + color change); mic mute keeps the PyAudio stream open and drops chunks. |
| P0-5 | **Status indicators** | Distinct visual states for: connecting, listening, agent speaking, interrupted, disconnected. "Speaking" is driven by playback-queue activity, not guesses. |
| P0-6 | **GoAway countdown** | When GoAway arrives, a countdown (ring or timer) appears with seconds remaining; at zero the UI flips to the disconnected state with a restart affordance. No silent death. |
| P0-7 | **Privacy posture unchanged** | No new disk writes (no frame caching, no transcript files); key absent from all browser-delivered code and WebSocket payloads; a `grep` of served assets for the key pattern finds nothing. |

### P1 — Nice-to-have (fast follows, build if momentum allows)

| # | Requirement | Acceptance criteria |
|---|---|---|
| P1-1 | **Mic level waveform** | Live RMS-driven bars/waveform while listening; flatlines when muted. Sells the "it hears you" feel. |
| P1-2 | **Voice + model selectors** | Dropdowns listing config-defined options; changing either prompts "restart session to apply?" and performs a clean close + reconnect with the new `LiveConnectConfig`. |
| P1-3 | **Session timer ring** | Elapsed-time ring around the status indicator that fills toward the ~10-min mark, turning amber on GoAway. |
| P1-4 | **Interrupt flash** | When `interrupted` fires and the playback queue drains, the speaking indicator visibly cuts off (animation), confirming the interruption worked. |
| P1-5 | **Keyboard shortcuts** | `M` mute, `C` camera pause, `R` restart — shown in a footer legend. |

### P2 — Future considerations (design so these stay possible)

- **Auto-reconnect on GoAway** (Phase 2 backend): the `session.restart` command and disconnected-state UI should be reusable as-is when the backend learns to reconnect itself.
- **Ephemeral-token remote mode** (Phase 5): keep all command handling server-side so swapping localhost for an authed tunnel doesn't change the UI contract.
- **Transcript export** (explicit user action only, never automatic — privacy posture stays opt-in).
- **Multiple camera/mic device pickers** — config plumbing exists (`device_index`); UI slot reserved in the settings drawer.

## Visual Direction (mission-control HUD)

- **Layout:** camera feed top-left as the hero panel; transcript fills the right column; bottom bar holds mic waveform, mute/pause toggles, status indicator, and session ring. Settings (voice/model) in a slide-over drawer, not the main surface.
- **Palette:** near-black background (#0a0e14-ish), one accent for agent activity (cyan/teal glow), a second for warnings (amber — GoAway), red reserved exclusively for disconnected/error. Monospace for telemetry (timestamps, timer), humanist sans for transcript text.
- **Motion:** subtle — waveform and speaking pulse are the only persistent animations; everything else animates only on state change. No scanline gimmicks that obscure the camera feed.
- **Tone:** instrument panel, not video game. Every glowing element must encode real state.

## Success Metrics (right-sized for a personal prototype)

- **Glance test:** a first-time viewer can answer "is it listening, what does it see, how long is left?" within 5 seconds of seeing the screen. (Demo it to one person; ask.)
- **Latency regression:** voice round-trip feels no slower than the terminal version (the UI adds no blocking work to the four loops — event emission is fire-and-forget).
- **Control latency:** mute/pause visibly take effect in <500 ms.
- **Stability:** one full session (connect → GoAway → restart) with zero UI-caused exceptions in the Python process.

## Open Questions

1. **(engineering, blocking)** Event hub integration: do the four loops publish to an `asyncio.Queue` consumed by the WebSocket broadcaster (cleanest), or does the server wrap `LiveVisionAgent` with callbacks? Decide before scaffolding `src/server.py`.
2. **(engineering, non-blocking)** Should `camera.frame` events push at capture rate (1 FPS, free) or offer a faster preview-only rate (extra camera reads, more CPU)? Default: capture rate; revisit if 1 FPS preview feels too laggy for framing objects.
3. **(engineering, non-blocking)** Auto-open the browser on launch (`webbrowser.open`) or print the URL? Default: print URL + auto-open behind a config toggle.
4. **(product, non-blocking)** Does the terminal transcript stay on when the UI is running (belt-and-suspenders) or go quiet behind a `HEADLESS` toggle? Default: keep printing; it costs nothing.

## Phasing

- **Milestone 1 — wiring (no visuals):** `src/server.py` with FastAPI + WebSocket hub; events flowing; an ugly test page proving transcript + frames + mute work end-to-end. *This de-risks everything.*
- **Milestone 2 — P0 HUD:** real layout, dark theme, status states, GoAway countdown.
- **Milestone 3 — P1 polish:** waveform, selectors + restart flow, timer ring, shortcuts.

No hard deadlines; each milestone is independently demoable. Dependency: none on Phase 2 reconnect work — the UI treats "session ended" as a terminal state with a manual restart until that lands.
