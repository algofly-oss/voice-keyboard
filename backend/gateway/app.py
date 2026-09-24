"""Streaming gateway in front of the Whisper server.

The browser streams raw 16 kHz mono PCM over a WebSocket while the user
speaks. Silero VAD cuts the audio into utterances at pauses, and each one is
transcribed while recording continues, so only the last few seconds are left
to process when the user stops.

It also passes the OpenAI-compatible /v1/* HTTP API through, so this is the
only port the backend exposes.
"""

import asyncio
import io
import json
import logging
import os
import time
import wave
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import NamedTuple

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from faster_whisper.vad import get_vad_model  # Silero VAD, bundled with the Whisper image
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, StreamingResponse

WHISPER_URL = os.environ.get("WHISPER_URL", "http://127.0.0.1:8001").rstrip("/")
DEFAULT_MODEL = os.environ.get("WHISPER_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2")
API_KEY = os.environ.get("API_KEY") or None

SAMPLE_RATE = 16000
WINDOW = 512                        # Silero VAD analyses 32 ms windows
WINDOW_MS = WINDOW * 1000 / SAMPLE_RATE
MIN_SPEECH_MS = 250                 # utterances with less speech than this are dropped


class Policy(NamedTuple):
    pause_ms: int  # silence that ends an utterance ...
    min_ms: int    # ... once at least this much audio is collected
    soft_ms: int   # after this, cut at the next gap between words
    hard_ms: int   # never let an utterance grow beyond this


# Live typing wants text quickly; batch mode gives Whisper longer context.
LIVE = Policy(pause_ms=400, min_ms=1000, soft_ms=5000, hard_ms=8000)
BATCH = Policy(pause_ms=600, min_ms=6000, soft_ms=20000, hard_ms=28000)

# Whisper ends every chunk with a full stop and capitalizes the next one. When
# a cut falls mid-sentence, these words are lower-cased again at the seam.
LOWERCASE_AT_SEAM = set(
    "a an the and or but so to of in on at for with from by as is are was were be been it its "
    "this that these those there then than if when while because which who what where how not "
    "no my your our their his her we you they he she me us them".split()
)

log = logging.getLogger("gateway")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
whisper = httpx.AsyncClient(base_url=WHISPER_URL, timeout=httpx.Timeout(300, connect=5))


def whisper_headers() -> dict:
    return {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}


def to_wav(pcm: np.ndarray) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.astype("<i2").tobytes())
    return out.getvalue()


async def transcribe(pcm: np.ndarray, model: str, language: str, prompt: str) -> str:
    data = {"model": model, "response_format": "json", "temperature": "0"}
    if language:
        data["language"] = language
    if prompt:
        data["prompt"] = prompt
    r = await whisper.post(
        "/v1/audio/transcriptions",
        data=data,
        files={"file": ("audio.wav", to_wav(pcm), "audio/wav")},
        headers=whisper_headers(),
    )
    if r.status_code != 200:
        raise RuntimeError(f"Whisper returned {r.status_code}: {r.text[:200]}")
    return r.json().get("text", "").strip()


class Segmenter:
    """Cuts a live stream into utterances using Silero voice activity detection.

    push() and flush() return (pcm, at_pause) pairs; at_pause is False when the
    cut was forced mid-speech.
    """

    def __init__(self, policy: Policy):
        frames = lambda ms: max(1, int(ms / WINDOW_MS))
        self.pause, self.min, self.soft, self.hard = map(frames, policy)
        self.min_speech = frames(MIN_SPEECH_MS)
        self.vad = get_vad_model()
        self.state, self.context = self.vad.get_initial_states(1)
        self.pending = np.zeros(0, np.int16)
        self.frames: list[np.ndarray] = []
        self.probs: list[float] = []
        self.flags: list[bool] = []
        self.speaking = False
        self.silence_run = 0

    def push(self, pcm: np.ndarray) -> list[tuple[np.ndarray, bool]]:
        out = []
        self.pending = np.concatenate([self.pending, pcm])
        while len(self.pending) >= WINDOW:
            frame, self.pending = self.pending[:WINDOW], self.pending[WINDOW:]
            prob, self.state, self.context = self.vad(
                frame.astype(np.float32)[None] / 32768, self.state, self.context, SAMPLE_RATE
            )
            prob = float(np.squeeze(prob))
            # Hysteresis keeps short dips inside a word from counting as silence.
            if prob >= 0.5:
                self.speaking = True
            elif prob < 0.35:
                self.speaking = False
            self.frames.append(frame)
            self.probs.append(prob)
            self.flags.append(self.speaking)
            self.silence_run = 0 if self.speaking else self.silence_run + 1
            n = len(self.frames)
            if not any(self.flags):
                if n > 20:  # Only silence so far: keep a ~300 ms lead-in.
                    del self.frames[:-10], self.probs[:-10], self.flags[:-10]
            elif self.silence_run >= self.pause and n >= self.min:
                out.extend(self._cut(n, True))
            elif n >= self.soft and not self.speaking:
                out.extend(self._cut(n, False))  # a short gap between words
            elif n >= self.hard:
                tail = self.probs[-int(2000 / WINDOW_MS):]  # least speech-like point of the last 2 s
                out.extend(self._cut(n - len(tail) + int(np.argmin(tail)) + 1, False))
        return out

    def flush(self) -> list[tuple[np.ndarray, bool]]:
        if self.pending.size:
            self.frames.append(self.pending)
            self.flags.append(False)
            self.probs.append(0.0)
            self.pending = np.zeros(0, np.int16)
        return self._cut(len(self.frames), True)

    def _cut(self, index: int, at_pause: bool) -> list[tuple[np.ndarray, bool]]:
        segment, speech = self.frames[:index], sum(self.flags[:index])
        del self.frames[:index], self.probs[:index], self.flags[:index]
        if not segment or speech < self.min_speech:
            return []
        return [(np.concatenate(segment), at_pause)]


class Joiner:
    """Joins utterances into one text, undoing Whisper's sentence punctuation at
    cuts that were not real pauses. Each piece already carries its separator,
    so pieces can be typed one by one as they arrive.
    """

    def __init__(self):
        self.pieces: list[str] = []
        self.held_stop = False  # full stop removed at the last forced cut

    def add(self, text: str, at_pause: bool) -> str:
        if not text:
            return ""
        separator = " " if self.pieces else ""
        if self.held_stop:
            first, _, rest = text.partition(" ")
            if first.lower() in LOWERCASE_AT_SEAM and first != "I":
                text = first.lower() + (" " + rest if rest else "")  # the sentence continues
            else:
                separator = ". "  # it really was a sentence end
        self.held_stop = not at_pause and text.endswith(".") and not text.endswith("..")
        if self.held_stop:
            text = text[:-1]
        self.pieces.append(separator + text)
        return self.pieces[-1]

    def close(self) -> str:
        """The closing full stop if one is still held back."""
        if not self.held_stop:
            return ""
        self.held_stop = False
        self.pieces.append(".")
        return "."

    @property
    def text(self) -> str:
        return "".join(self.pieces)


async def warm_up():
    """Load the model onto the GPU at start-up instead of on the first dictation."""
    silence = np.zeros(SAMPLE_RATE, np.int16)
    for _ in range(60):
        try:
            await transcribe(silence, DEFAULT_MODEL, "en", "")
            log.info("Model %s loaded", DEFAULT_MODEL)
            return
        except Exception as e:  # Whisper still starting.
            log.info("Waiting for Whisper: %s", e)
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(warm_up())
    yield
    task.cancel()
    await whisper.aclose()


app = FastAPI(title="Voice keyboard gateway", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def authorized(token: str | None) -> bool:
    return API_KEY is None or token == API_KEY


@app.get("/health")
async def health():
    try:
        ok = (await whisper.get("/health", timeout=3)).status_code == 200
    except httpx.HTTPError:
        ok = False
    return JSONResponse({"status": "ok" if ok else "starting", "whisper": ok}, status_code=200 if ok else 503)


class Room:
    """Web UIs of one keyboard: watchers see the active dictation mirrored."""

    def __init__(self):
        self.watchers: set[WebSocket] = set()
        self.active: dict | None = None  # owner, started, live, ws, joiner

    def snapshot(self) -> dict:
        a = self.active
        if not a:
            return {"type": "state", "active": False}
        return {"type": "state", "active": True, "owner": a["owner"], "live": a["live"],
                "elapsed": time.monotonic() - a["started"], "text": a["joiner"].text}

    def broadcast(self, message: dict):
        for watcher in list(self.watchers):
            asyncio.create_task(self._send(watcher, message))

    async def _send(self, watcher: WebSocket, message: dict):
        try:
            await watcher.send_json(message)
        except Exception:
            self.watchers.discard(watcher)


rooms: defaultdict[str, Room] = defaultdict(Room)


@app.websocket("/v1/events")
async def events(ws: WebSocket):
    """Keeps every open web UI of a keyboard in sync.

    server -> {"type":"state","active":true,"owner":"...","elapsed":3.2,"text":"...","live":true}
    server -> {"type":"state","active":false,"owner":"...","final":"..."}  when a dictation ends
    server -> {"type":"segment","owner":"...","text":"..."} / {"type":"level","owner":"...","v":0.4}
    client -> {"type":"stop"} or {"type":"cancel"}  to end another device's dictation
    """
    await ws.accept()  # Accept first so the browser sees the 4401 close code.
    if not authorized(ws.query_params.get("token")):
        await ws.close(code=4401, reason="Invalid API key")
        return
    room = rooms[ws.query_params.get("room") or "default"]
    room.watchers.add(ws)
    try:
        await ws.send_json(room.snapshot())
        while True:
            kind = (await ws.receive_json()).get("type")
            if kind in ("stop", "cancel") and room.active:
                await room.active["ws"].send_json({"type": "remote_" + kind})
    except Exception:
        pass
    finally:
        room.watchers.discard(ws)


@app.websocket("/v1/stream")
async def stream(ws: WebSocket):
    """Protocol:
    client -> {"type":"start","language":"en","prompt":"...","model":"...","live":true,
               "room":"voice-keyboard","client":"<id>"}
    client -> binary frames of 16 kHz mono little-endian int16 PCM
    client -> {"type":"level","v":0.4}  (optional, mirrored to other web UIs)
    client -> {"type":"stop"}  (or {"type":"cancel"})
    server -> {"type":"segment","text":"..."}   as each utterance is transcribed; includes its
                                                leading separator, so pieces concatenate
    server -> {"type":"final","text":"..."}     the whole text, then closes
    server -> {"type":"remote_stop"} / {"type":"remote_cancel"}  another web UI asked to end
    server -> {"type":"error","message":"..."}
    """
    await ws.accept()  # Accept first so the browser sees the 4401 close code.
    if not authorized(ws.query_params.get("token")):
        await ws.close(code=4401, reason="Invalid API key")
        return
    try:
        start = json.loads(await ws.receive_text())
    except (WebSocketDisconnect, ValueError):
        return
    model = start.get("model") or DEFAULT_MODEL
    language = start.get("language") or ""
    user_prompt = start.get("prompt") or ""
    owner = str(start.get("client") or id(ws))
    room = rooms[start.get("room") or "default"]
    if room.active:
        # One dictation per keyboard; two would type over each other.
        await ws.send_json({"type": "error", "message": "Another device is already dictating"})
        await ws.close()
        return

    queue: asyncio.Queue[tuple[np.ndarray, bool] | None] = asyncio.Queue()
    joiner = Joiner()
    live = bool(start.get("live"))
    room.active = {"owner": owner, "started": time.monotonic(), "live": live, "ws": ws, "joiner": joiner}
    room.broadcast(room.snapshot())
    final = None

    async def send_piece(piece: str):
        if piece:
            await ws.send_json({"type": "segment", "text": piece})
            room.broadcast({"type": "segment", "owner": owner, "text": piece})

    async def worker():
        while (item := await queue.get()) is not None:
            segment, at_pause = item
            # The end of the previous utterance keeps wording and casing consistent.
            prompt = " ".join(filter(None, [user_prompt, joiner.text[-200:]]))
            await send_piece(joiner.add(await transcribe(segment, model, language, prompt), at_pause))
        await send_piece(joiner.close())

    task = asyncio.create_task(worker())
    segmenter = Segmenter(LIVE if live else BATCH)
    received = utterances = 0  # for the session log line
    ended = "disconnect"
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                task.cancel()
                return
            if message.get("bytes"):
                chunk = message["bytes"]
                received += len(chunk)
                for segment in segmenter.push(np.frombuffer(chunk[: len(chunk) // 2 * 2], "<i2")):
                    utterances += 1
                    queue.put_nowait(segment)
            elif message.get("text"):
                data = json.loads(message["text"])
                kind = data.get("type")
                if kind == "level":
                    room.broadcast({"type": "level", "owner": owner, "v": data.get("v", 0)})
                elif kind == "cancel":
                    ended = "cancel"
                    task.cancel()
                    await ws.close()
                    return
                elif kind == "stop":
                    ended = "stop"
                    break
            if task.done():
                break
        for segment in segmenter.flush():
            utterances += 1
            queue.put_nowait(segment)
        await queue.put(None)
        await task
        final = joiner.text
        await ws.send_json({"type": "final", "text": final})
        await ws.close()
    except WebSocketDisconnect:
        task.cancel()
    except Exception as e:
        ended = "error"
        task.cancel()
        log.exception("Stream failed")
        try:
            await ws.send_json({"type": "error", "message": str(e)})
            await ws.close()
        except Exception:
            pass
    finally:
        log.info("Stream %s (%s): %.1f s of audio, %d utterances, ended by %s",
                 owner[:8], "live" if live else "batch", received / 2 / SAMPLE_RATE, utterances, ended)
        if room.active and room.active["ws"] is ws:
            room.active = None
            room.broadcast({"type": "state", "active": False, "owner": owner, "final": final})


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def passthrough(path: str, request: Request):
    """OpenAI-compatible endpoints (e.g. /v1/audio/transcriptions) of the Whisper server."""
    auth = request.headers.get("authorization", "")
    if not authorized(auth.removeprefix("Bearer ").strip() or request.query_params.get("token")):
        raise HTTPException(401, "Invalid API key")
    headers = {"content-type": request.headers.get("content-type", "")} | whisper_headers()
    upstream = whisper.build_request(
        request.method, f"/v1/{path}", params=request.query_params, headers=headers, content=request.stream()
    )
    response = await whisper.send(upstream, stream=True)
    return StreamingResponse(
        response.aiter_raw(),
        status_code=response.status_code,
        media_type=response.headers.get("content-type"),
        background=BackgroundTask(response.aclose),
    )
