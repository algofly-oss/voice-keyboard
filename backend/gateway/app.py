"""Streaming gateway in front of the Whisper server.

The browser streams raw 16 kHz mono PCM over a WebSocket while the user
speaks. Silero VAD cuts the audio into utterances at pauses, and each one is
transcribed while recording continues, so only the last few seconds are left
to process when the user stops.

It also passes the OpenAI-compatible /v1/* HTTP API through, so this is the
only port the backend exposes.
"""

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import time
import wave
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from faster_whisper.vad import get_vad_model  # Silero VAD, bundled with the Whisper image

import hinglish
from accounts import RateLimiter, Sessions, Store, validate_credentials
from starlette.background import BackgroundTask
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

WHISPER_URL = os.environ.get("WHISPER_URL", "http://127.0.0.1:8001").rstrip("/")
DEFAULT_MODEL = os.environ.get("WHISPER_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2")
# "translate" types an English translation of any spoken language (see start.sh).
TRANSLATE = os.environ.get("WHISPER_VARIANT", "transcribe") == "translate"
# Language new browsers start with ("" = detect automatically); users can change it.
DEFAULT_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "en").strip()
API_KEY = os.environ.get("API_KEY") or None
AUTH_FILE = Path(os.environ.get("AUTH_FILE", "/data/auth.json"))


def load_auth() -> dict:
    try:
        stored = json.loads(AUTH_FILE.read_text())
        return stored if isinstance(stored, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_auth(**fields):
    """Merge fields into the persisted auth file (API key, install token)."""
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps({**load_auth(), **fields}) + "\n")
    try:
        AUTH_FILE.chmod(0o600)
    except OSError:
        pass


def load_runtime_api_key() -> str | None:
    return load_auth().get("api_key") or API_KEY


RUNTIME_API_KEY = load_runtime_api_key()  # optional; gives scripts the admin account's access
# WEB_PASSWORD only seeds the first (admin) account when the database is empty.
WEB_PASSWORD = os.environ.get("WEB_PASSWORD") or RUNTIME_API_KEY
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
SESSION_SECRET = os.environ.get("SESSION_SECRET") or API_KEY or "development-session-secret"
ALLOW_SIGNUPS = os.environ.get("ALLOW_SIGNUPS", "false").strip().lower() in {"1", "true", "yes", "on"}
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", "/data/voice-keyboard.db"))
# One Whisper instance serves every account; requests beyond this wait their turn.
WHISPER_CONCURRENCY = int(os.environ.get("WHISPER_CONCURRENCY", "1"))

SAMPLE_RATE = 16000
WINDOW = 512                        # Silero VAD analyses 32 ms windows
WINDOW_MS = WINDOW * 1000 / SAMPLE_RATE
MIN_SPEECH_MS = 250                 # utterances with less speech than this are dropped
BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "static"
CLIENT_DIR = BASE_DIR / "desktop"
RELEASES_DIR = Path(os.environ.get("RELEASES_DIR", "/data/releases"))
WEB_SOURCE = Path(os.environ.get("WEB_SOURCE", "/opt/web/index.html"))
if not WEB_SOURCE.exists():
    WEB_SOURCE = BASE_DIR.parent.parent / "web" / "index.html"


class Policy(NamedTuple):
    pause_ms: int  # silence that ends an utterance ...
    min_ms: int    # ... once at least this much audio is collected
    soft_ms: int   # after this, cut at the next gap between words
    hard_ms: int   # never let an utterance grow beyond this


# Live typing wants text quickly; batch mode gives Whisper longer context.
# Live typing speed, chosen in the web UI: shorter pieces type sooner, longer
# pieces give Whisper more context (translation benefits most).
PACES = {
    "instant": Policy(pause_ms=300, min_ms=600, soft_ms=3000, hard_ms=6000),
    "fast": Policy(pause_ms=400, min_ms=1000, soft_ms=5000, hard_ms=8000),
    "balanced": Policy(pause_ms=500, min_ms=1800, soft_ms=6500, hard_ms=10000),
    "accurate": Policy(pause_ms=600, min_ms=2500, soft_ms=8000, hard_ms=12000),
}
DEFAULT_PACE = "fast"
BATCH = Policy(pause_ms=600, min_ms=6000, soft_ms=20000, hard_ms=28000)

# Whisper ends every chunk with a full stop and capitalizes the next one. When
# a cut falls mid-sentence, these words are lower-cased again at the seam.
TRAILING_ELLIPSIS = re.compile(r"(?:\s*…|\s*\.(?:\s*\.)+)+\s*$")  # "...", "…", ". . ."
LEADING_ELLIPSIS = re.compile(r"^\s*(?:…\s*|\.(?:\s*\.)+\s*)+")

LOWERCASE_AT_SEAM = set(
    "a an the and or but so to of in on at for with from by as is are was were be been it its "
    "this that these those there then than if when while because which who what where how not "
    "no my your our their his her we you they he she me us them".split()
)

log = logging.getLogger("gateway")
logging.getLogger("httpx").setLevel(logging.WARNING)  # per-request lines include signed download URLs
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
whisper = httpx.AsyncClient(base_url=WHISPER_URL, timeout=httpx.Timeout(300, connect=5))
whisper_slots = asyncio.Semaphore(WHISPER_CONCURRENCY)  # FIFO queue in front of the shared model

store = Store(DATABASE_PATH)
sessions = Sessions(SESSION_SECRET, store)
login_limiter = RateLimiter(limit=10, window=300)


def migrate_single_user_setup():
    """Before accounts existed there was one password and one install token.
    Turn them into the admin account so existing logins and installs keep working."""
    if store.user_count() or not WEB_PASSWORD:
        return
    store.create_user(ADMIN_USERNAME, WEB_PASSWORD, admin=True,
                      install_token=load_auth().get("enrollment_token"))
    log.info("Created admin account %r from the previous single-user setup", ADMIN_USERNAME)


migrate_single_user_setup()


def whisper_headers() -> dict:
    return {"Authorization": f"Bearer {RUNTIME_API_KEY}"} if RUNTIME_API_KEY else {}


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
    if language and not TRANSLATE:  # translation detects the spoken language itself
        data["language"] = language
    if prompt:
        data["prompt"] = prompt
    async with whisper_slots:  # dictations of several accounts share the one model in turn
        r = await whisper.post(
            "/v1/audio/translations" if TRANSLATE else "/v1/audio/transcriptions",
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
        self.open_end = False   # the last piece already ends with a space

    def add(self, text: str, at_pause: bool) -> str:
        # Whisper marks a phrase it thinks was cut off with "..."; type a space
        # instead, so the next piece simply continues the sentence.
        text = LEADING_ELLIPSIS.sub("", text)  # a piece continuing a cut-off phrase: "...belong"
        trimmed = TRAILING_ELLIPSIS.sub("", text)
        ends_open, text = trimmed != text, trimmed
        if not text:
            return ""
        separator = "" if not self.pieces or self.open_end else " "
        if self.held_stop:
            first, _, rest = text.partition(" ")
            if first.lower() in LOWERCASE_AT_SEAM and first != "I":
                text = first.lower() + (" " + rest if rest else "")  # the sentence continues
            else:
                separator = ". "  # it really was a sentence end
        self.held_stop = not ends_open and not at_pause and text.endswith(".") and not text.endswith("..")
        if self.held_stop:
            text = text[:-1]
        if ends_open:
            text += " "
        self.open_end = ends_open
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
    tasks = [asyncio.create_task(warm_up()), asyncio.create_task(sync_releases_forever())]
    yield
    for task in tasks:
        task.cancel()
    await whisper.aclose()


app = FastAPI(title="Voice keyboard gateway", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def api_key_user(token: str | None):
    """The optional API_KEY acts as the admin account (scripts, curl)."""
    if RUNTIME_API_KEY and token and secrets.compare_digest(token, RUNTIME_API_KEY):
        return store.admin()
    return None


def request_user(request: Request):
    bearer = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    return sessions.user(request.cookies.get("vk_session")) or api_key_user(bearer or request.query_params.get("token"))


def require_user(request: Request):
    user = request_user(request)
    if not user:
        raise HTTPException(401, "Login required")
    return user


def ws_user(ws: WebSocket):
    return sessions.user(ws.cookies.get("vk_session")) or api_key_user(request_token(ws))


@app.get("/health")
async def health():
    try:
        ok = (await whisper.get("/health", timeout=3)).status_code == 200
    except httpx.HTTPError:
        ok = False
    return JSONResponse({"status": "ok" if ok else "starting", "whisper": ok}, status_code=200 if ok else 503)


@app.get("/", include_in_schema=False)
async def web_ui():
    return HTMLResponse(WEB_SOURCE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


@app.get("/esp32", include_in_schema=False)
async def esp32_setup_page():
    """Sets up an ESP32 board from the browser: flashes it over Web Serial, then configures it."""
    return HTMLResponse((WEB_SOURCE.parent / "esp32.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-cache"})


# Icons for tabs, bookmarks and home-screen shortcuts; fixed names only.
WEB_ASSETS = {"favicon.ico": ("icons/favicon.ico", "image/x-icon"),
              "manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
              **{f"icons/{name}": (f"icons/{name}", kind) for name, kind in (
                  ("icon.svg", "image/svg+xml"), ("icon-32.png", "image/png"), ("icon-192.png", "image/png"),
                  ("icon-512.png", "image/png"), ("apple-touch-icon.png", "image/png"))},
              # Espressif's flasher (Apache-2.0), vendored so /esp32 works without internet access.
              "vendor/esptool-js-0.7.0.mjs": ("vendor/esptool-js-0.7.0.mjs", "text/javascript")}


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/manifest.webmanifest", include_in_schema=False)
@app.get("/icons/{name}", include_in_schema=False)
@app.get("/vendor/{name}", include_in_schema=False)
async def web_asset(request: Request):
    asset = WEB_ASSETS.get(request.url.path.lstrip("/"))
    if not asset:
        raise HTTPException(404, "Not found")
    return FileResponse(WEB_SOURCE.parent / asset[0], media_type=asset[1],
                        headers={"Cache-Control": "public, max-age=86400"})


# Desktop client: one self-contained binary per OS/CPU, fetched by /client/install.*
DESKTOP_BINARIES = [f"vkeyboard-{os_}-{arch}{'.exe' if os_ == 'windows' else ''}"
                    for os_ in ("linux", "darwin", "windows") for arch in ("amd64", "arm64")]
# ESP32 firmware: one merged image per chip, flashed at 0x0 by the /esp32 page.
FIRMWARE_CHIPS = {"esp32": "ESP32", "esp32c3": "ESP32-C3"}
FIRMWARE_BINARIES = [f"vkeyboard-{chip}.bin" for chip in FIRMWARE_CHIPS]
# Release asset -> (directory under RELEASES_DIR, media type).
RELEASE_FILES = {name: ("desktop", "application/octet-stream") for name in DESKTOP_BINARIES} | {
    name: ("firmware", "application/octet-stream") for name in FIRMWARE_BINARIES}


def release_path(filename: str) -> Path | None:
    path = RELEASES_DIR / RELEASE_FILES[filename][0] / filename
    return path if path.is_file() else None


# Client builds are published as GitHub releases tagged v<version>. The
# gateway mirrors the newest one into RELEASES_DIR so /client can serve it
# (release assets of a private repository need a token the installers do not have).
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
RELEASE_TAG_PREFIX = "v"
RELEASE_SYNC_SECONDS = int(os.environ.get("RELEASE_SYNC_SECONDS", "3600"))


async def sync_releases() -> str | None:
    """Download assets of the newest release that are new or changed. Returns its tag."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    async with httpx.AsyncClient(base_url="https://api.github.com", headers=headers, timeout=60,
                                 follow_redirects=True) as github:
        response = await github.get(f"/repos/{GITHUB_REPO}/releases", params={"per_page": 30})
        response.raise_for_status()
        release = next((r for r in response.json() if not r["draft"] and not r["prerelease"]
                        and r["tag_name"].startswith(RELEASE_TAG_PREFIX)), None)
        if not release:
            return None
        state_file = RELEASES_DIR / "release.json"
        try:
            state = json.loads(state_file.read_text())
        except (OSError, ValueError):
            state = {}
        for filename, (directory, _) in RELEASE_FILES.items():
            asset = next((a for a in release["assets"] if a["name"] == filename), None)
            if not asset:
                continue
            version = f"{release['tag_name']}:{asset['id']}:{asset['updated_at']}"
            if state.get(filename) == version and release_path(filename):
                continue
            target = RELEASES_DIR / directory / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(".part")
            async with github.stream("GET", asset["url"], headers={"Accept": "application/octet-stream"}) as download:
                download.raise_for_status()
                with partial.open("wb") as out:
                    async for chunk in download.aiter_bytes():
                        out.write(chunk)
            partial.replace(target)  # atomic, so /client never serves half a file
            state[filename] = version
            state["tag"] = release["tag_name"]
            state_file.write_text(json.dumps(state) + "\n")
            log.info("Downloaded %s from release %s", filename, release["tag_name"])
        return release["tag_name"]


# ---- Client updates ----
# Desktop clients update themselves: the server offers the newest build it
# mirrors (at connect, after a new release syncs, or from the web app's Update
# button). ESP32 boards have no room for over-the-air updates; they update
# from the /esp32 page.
AUTO_UPDATE = os.environ.get("AUTO_UPDATE", "true").lower() not in ("0", "false", "no")


def version_key(text: str | None) -> tuple[int, ...] | None:
    """(1, 5, 2) for "1.5.2" or "v1.5.2"; None for "dev" and other builds."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(map(int, m.groups())) if m else None


_file_hashes: dict[str, tuple[float, str]] = {}


def file_sha256(path: Path) -> str:
    mtime = path.stat().st_mtime
    cached = _file_hashes.get(str(path))
    if not cached or cached[0] != mtime:
        with path.open("rb") as f:
            cached = (mtime, hashlib.file_digest(f, "sha256").hexdigest())
        _file_hashes[str(path)] = cached
    return cached[1]


def latest_build(platform: str) -> dict | None:
    """The newest build this server has for a client's platform ("darwin/arm64", "ESP32-C3")."""
    if platform.upper().startswith("ESP32"):
        chip = "esp32c3" if platform.upper() == "ESP32-C3" else "esp32"
        path = release_path(f"vkeyboard-{chip}.bin")
        return {"version": firmware_version(path), "url": "/esp32"} if path else None
    os_, _, arch = platform.partition("/")
    name = f"vkeyboard-{os_}-{arch}{'.exe' if os_ == 'windows' else ''}"
    if name not in RELEASE_FILES or not (path := release_path(name)):
        return None
    try:
        tag = json.loads((RELEASES_DIR / "release.json").read_text()).get(name, "").split(":", 1)[0]
    except (OSError, ValueError):
        return None
    return {"version": tag.removeprefix(RELEASE_TAG_PREFIX), "url": f"/client/{name}",
            "sha256": file_sha256(path), "size": path.stat().st_size}


def update_for(device) -> dict | None:
    """The update to offer a desktop client, or None when it is current (or a dev build)."""
    build = latest_build(device["platform"])
    have, latest = version_key(device["version"]), version_key(build and build["version"])
    if not build or "sha256" not in build or not have or not latest or latest <= have:
        return None
    return {"type": "update", **build}


async def offer_update(ws: WebSocket, device, manual: bool = False) -> bool:
    update = update_for(device)
    if not update and manual and (build := latest_build(device["platform"])) and "sha256" in build:
        update = {"type": "update", **build}  # the button reinstalls even a current or dev build
    if not update:
        return False
    log.info("Offering %s %s to %r (%s)", "update" if not manual else "requested update",
             update["version"], device["name"], device["version"])
    await ws.send_json({**update, "manual": manual})
    return True


async def offer_updates_to_everyone():
    """After a release syncs: every connected, outdated desktop client."""
    for room in list(rooms.values()):
        for device_id, ws in list(room.devices.items()):
            if device := store._one("SELECT * FROM devices WHERE id=?", (device_id,)):
                try:
                    await offer_update(ws, device)
                except Exception:
                    pass


async def sync_releases_forever():
    if not GITHUB_REPO:
        return
    while True:
        try:
            tag = await sync_releases()
            log.info("Client release %s is current", tag) if tag else log.info("No %s* release on %s yet", RELEASE_TAG_PREFIX, GITHUB_REPO)
            # After every sync, not only on a new tag: clients that connected while
            # it was still downloading were offered nothing. Current ones ignore it.
            if tag and AUTO_UPDATE:
                await offer_updates_to_everyone()
        except Exception as e:  # GitHub unreachable or token missing; keep serving what we have.
            log.warning("Release sync failed: %s", e)
        await asyncio.sleep(RELEASE_SYNC_SECONDS)


def device_mac(machine_id: str) -> str | None:
    """ESP32 boards identify as esp32-<MAC>; shown so identical boards can be told apart."""
    hex_ = machine_id.removeprefix("esp32-")
    if hex_ == machine_id or len(hex_) != 12:
        return None
    return ":".join(hex_[i:i + 2] for i in range(0, 12, 2)).upper()


def device_json(device, room: "Room", selected_id) -> dict:
    return {"id": device["id"], "name": device["name"], "description": device["description"],
            "platform": device["platform"], "version": device["version"], "mac": device_mac(device["machine_id"]),
            "online": device["id"] in room.devices,
            # ESP32 boards report whether a computer is connected over Bluetooth; null for others.
            "bluetooth": room.bluetooth.get(device["id"]) if device["id"] in room.devices else None,
            "selected": device["id"] == selected_id, "lastSeen": device["last_seen"],
            **client_update_info(device)}


def client_update_info(device) -> dict:
    """For the Clients list: the newest version for this client, and whether it is behind."""
    build = latest_build(device["platform"] or "")
    have, latest = version_key(device["version"]), version_key(build and build["version"])
    return {"latest": build["version"] if build else None, "outdated": bool(have and latest and latest > have),
            # Desktop clients from 1.5.3 on install updates themselves; older ones need the install command once.
            "selfUpdate": bool(have and have >= SELF_UPDATE_SINCE) and not (device["platform"] or "").upper().startswith("ESP32")}


SELF_UPDATE_SINCE = (1, 5, 3)


@app.get("/api/status")
async def backend_status(request: Request):
    user = require_user(request)
    room = room_for(user)
    devices = [device_json(d, room, user["selected_device_id"]) for d in store.devices(user["id"])]
    return {"user": user["username"], "admin": bool(user["is_admin"]), "devices": devices,
            "selected": next((d for d in devices if d["selected"]), None)}


@app.post("/api/type")
async def backend_type(request: Request):
    """Text from the on-screen keyboard, typed by the active client like dictation."""
    user = require_user(request)
    text = str((await request.json()).get("text") or "")[:2000]
    if text:
        await send_keyboard(room_for(user), {"type": "segment", "text": text})
    return {"ok": True}


CLIENT_ERRORS_PER_MINUTE = 30  # per connection, so a broken client cannot flood the database
LIVE_LOG_PER_SECOND = 20       # lines relayed to an open live log, per connection


@app.get("/api/client-errors")
async def client_errors(request: Request):
    """Errors the account's clients reported, newest first. ?device=ID for one;
    an admin can add ?all=1 for every account."""
    user = require_user(request)
    params = request.query_params
    try:
        device = int(params["device"]) if params.get("device") else None
        limit = max(1, min(500, int(params.get("limit") or 100)))
    except ValueError:
        raise HTTPException(400, "device and limit must be numbers")
    everyone = params.get("all") == "1" and bool(user["is_admin"])
    rows = store.client_errors(None if everyone else user["id"], device, limit)
    return {"errors": [{"at": r["at"], "message": r["message"], "device": r["device_id"], "client": r["name"],
                        "platform": r["platform"], "version": r["version"], **({"user": r["username"]} if everyone else {})}
                       for r in rows]}


def esp32_socket(user, device_id: int) -> WebSocket:
    device = store.device(user["id"], device_id)
    if not device or not (device["platform"] or "").upper().startswith("ESP32"):
        raise HTTPException(404, "No such ESP32 board")
    ws = room_for(user).devices.get(device_id)
    if ws is None:
        raise HTTPException(409, "The board is offline")
    return ws


@app.post("/api/devices/{device_id}/pairing")
async def device_pairing(device_id: int, request: Request):
    """ESP32: pairing mode for two minutes. It drops its connection and refuses
    devices it was paired with, so a new device can find and pair it."""
    user = require_user(request)
    await esp32_socket(user, device_id).send_json({"type": "pairing", "seconds": 120})
    return {"ok": True, "seconds": 120}


@app.post("/api/devices/{device_id}/logs")
async def device_logs(device_id: int, request: Request):
    """ESP32: ?on=1 streams its log lines to the web app (as device-log events) for up to 10 minutes; ?on=0 stops."""
    user = require_user(request)
    await esp32_socket(user, device_id).send_json({"type": "logs", "on": request.query_params.get("on") == "1"})
    return {"ok": True}


@app.post("/api/devices/{device_id}/bluetooth")
async def device_bluetooth(device_id: int, request: Request):
    """ESP32 boards: ?on=0 disconnects the Bluetooth keyboard (the tablet or phone
    shows its on-screen keyboard again), ?on=1 offers it again."""
    user = require_user(request)
    device = store.device(user["id"], device_id)
    if not device or not (device["platform"] or "").upper().startswith("ESP32"):
        raise HTTPException(404, "No such ESP32 board")
    ws = room_for(user).devices.get(device_id)
    if ws is None:
        raise HTTPException(409, "The board is offline")
    await ws.send_json({"type": "bluetooth", "on": request.query_params.get("on") == "1"})
    return {"ok": True}


@app.post("/api/devices/{device_id}/update")
async def update_device_now(device_id: int, request: Request):
    """The web app's Update button: the client installs the newest build now."""
    user = require_user(request)
    device = store.device(user["id"], device_id)
    if not device:
        raise HTTPException(404, "No such client")
    if (device["platform"] or "").upper().startswith("ESP32"):
        raise HTTPException(400, "ESP32 boards update from the setup page (/esp32)")
    ws = room_for(user).devices.get(device_id)
    if ws is None:
        raise HTTPException(409, "The client is offline; it updates itself when it connects")
    if not await offer_update(ws, device, manual=True):
        raise HTTPException(409, "This server has no build for this client yet")
    return {"ok": True}


@app.post("/api/devices/{device_id}/select")
async def select_device(device_id: int, request: Request):
    user = require_user(request)
    if not store.device(user["id"], device_id):
        raise HTTPException(404, "No such client")
    store.select_device(user["id"], device_id)
    await announce_selection(room_for(store.user(user["id"])))
    return {"ok": True}


@app.patch("/api/devices/{device_id}")
async def rename_device(device_id: int, request: Request):
    """Changes the name and/or the description; a field left out is kept."""
    user = require_user(request)
    body = await request.json()
    changes = {}
    if "name" in body:
        changes["name"] = str(body.get("name") or "").strip()[:80]
        if not changes["name"]:
            raise HTTPException(400, "The name cannot be empty")
    if "description" in body:
        changes["description"] = str(body.get("description") or "").strip()[:200]
    if not changes or not store.device(user["id"], device_id):
        raise HTTPException(400, "Invalid name or client")
    store.update_device(device_id, **changes)
    room_for(user).broadcast({"type": "devices"})
    return {"ok": True}


@app.delete("/api/devices/{device_id}")
async def remove_device(device_id: int, request: Request):
    """Revokes the computer's credential; it has to be installed again."""
    user = require_user(request)
    if not store.delete_device(user["id"], device_id):
        raise HTTPException(404, "No such client")
    room = room_for(store.user(user["id"]))
    if (client := room.devices.pop(device_id, None)) is not None:
        try:
            await client.close(code=4401, reason="This client was removed")
        except Exception:
            pass
    await announce_selection(room)
    return {"ok": True}


# A named key, or a combination: "ctrl+c", "alt+tab", "ctrl+shift+left", "meta+c"
# (meta: Command on macOS, the Windows key, Super on Linux).
NAMED_KEYS = "backspace|enter|up|down|left|right|escape|tab|space|f(?:1[0-2]|[1-9])"
KEY_NAME = re.compile(rf"(?:{NAMED_KEYS})|(?:(?:ctrl|alt|shift|meta)\+)+(?:[a-z0-9]|{NAMED_KEYS})")


@app.post("/api/key")
async def backend_key(request: Request):
    user = require_user(request)
    key = request.query_params.get("k", "")
    state = request.query_params.get("s", "press")
    if not KEY_NAME.fullmatch(key) or state not in {"down", "hold", "up", "press"}:
        raise HTTPException(400, "Unsupported key")
    # Space goes out as text so already-installed desktop clients can type it.
    message = {"type": "segment", "text": " "} if key == "space" else {"type": "key", "key": key, "state": state}
    await send_keyboard(room_for(user), message)
    return {"ok": True}


@app.get("/api/settings")
async def backend_settings(request: Request):
    user = require_user(request)
    return {"liveTyping": True, "language": DEFAULT_LANGUAGE, "afterText": "none", "maxSeconds": 600,
            "model": DEFAULT_MODEL, "prompt": "", "variant": "translate" if TRANSLATE else "transcribe",
            "pace": DEFAULT_PACE, **account_prefs(user),
            # With Caddy's local CA, install commands trust it on first contact.
            "localCa": os.environ.get("VK_PROTOCOL", "https") in ("https", "both") and os.environ.get("VK_TLS", "internal") == "internal"}


# Preferences of the account, shared by all its browsers: (name, type, default).
ACCOUNT_PREFS = {"touchpad": (bool, True), "functionKeys": (bool, False), "reverseScroll": (bool, False)}


def account_prefs(user) -> dict:
    saved = store.user_prefs(user)
    return {name: saved.get(name, default) for name, (_, default) in ACCOUNT_PREFS.items()}


@app.post("/api/prefs")
async def set_prefs(request: Request):
    """Changes account preferences; every open web UI of the account applies them at once."""
    user = require_user(request)
    body = await request.json()
    changes = {k: v for k, v in body.items() if k in ACCOUNT_PREFS and isinstance(v, ACCOUNT_PREFS[k][0])}
    if not changes or len(changes) != len(body):
        raise HTTPException(400, f"Unknown preference or wrong type; known: {', '.join(ACCOUNT_PREFS)}")
    store.update_user_prefs(user["id"], changes)
    room_for(user).broadcast({"type": "prefs", **changes})
    return account_prefs(store.user(user["id"]))


@app.api_route("/api/enroll", methods=["GET", "POST"])
async def enroll(request: Request):
    """GET returns the account's install token; POST replaces it.

    The token does not expire. It keeps working for new installs until the user
    generates a new one; computers already installed keep their own
    credentials and stay connected.
    """
    user = require_user(request)
    token = store.rotate_install_token(user["id"]) if request.method == "POST" else user["install_token"]
    # The web UI builds the install commands from the address in the browser.
    return {"token": token}


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "")


async def credentials_from(request: Request) -> tuple[str, str]:
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
        return str(body.get("username") or "").strip(), str(body.get("password") or "")
    form = parse_qs((await request.body()).decode("utf-8"))
    return (form.get("username") or [""])[0].strip(), (form.get("password") or [""])[0]


def start_session(request: Request, response: Response, user):
    response.set_cookie("vk_session", sessions.issue(user), max_age=86400 * 30, httponly=True,
                        secure=public_base_url(request).startswith("https://"), samesite="lax")


def signups_open() -> bool:
    return ALLOW_SIGNUPS or store.user_count() == 0  # the very first account can always be created


@app.get("/api/auth")
async def auth_info(request: Request):
    user = sessions.user(request.cookies.get("vk_session"))
    return {"signupsAllowed": signups_open(), "user": user["username"] if user else None}


@app.post("/api/signup")
async def signup(request: Request, response: Response):
    if not signups_open():
        raise HTTPException(403, "Sign-ups are disabled on this server")
    if not login_limiter.allow(client_ip(request)):
        raise HTTPException(429, "Too many attempts, wait a moment")
    username, password = await credentials_from(request)
    if (problem := validate_credentials(username, password)):
        login_limiter.failed(client_ip(request))
        raise HTTPException(400, problem)
    try:
        user_id = store.create_user(username, password, admin=store.user_count() == 0)
    except Exception:
        raise HTTPException(409, "That username is taken")
    log.info("New account %r", username)
    start_session(request, response, store.user(user_id))
    return {"ok": True}


@app.post("/api/login")
async def backend_login(request: Request, response: Response):
    if not login_limiter.allow(client_ip(request)):
        raise HTTPException(429, "Too many attempts, wait a moment")
    username, password = await credentials_from(request)
    if not username and (admin := store.admin()):  # blank username = admin (old login page)
        username = admin["username"]
    user = store.login(username, password)
    if not user:
        login_limiter.failed(client_ip(request))
        raise HTTPException(401, "Wrong username or password")
    start_session(request, response, user)
    return {"ok": True}


@app.post("/api/logout")
async def backend_logout(request: Request, response: Response):
    response.delete_cookie("vk_session")
    return {"ok": True}


@app.post("/api/account")
async def update_account(request: Request, response: Response):
    """Change the username and/or password; the current password confirms it."""
    user = require_user(request)
    body = await request.json()
    if not store.login(user["username"], str(body.get("current") or "")):
        raise HTTPException(403, "The current password is wrong")
    username = str(body.get("username") or user["username"]).strip()
    new = str(body.get("new") or "")
    if (problem := validate_credentials(username, new or "unchanged-password")):
        raise HTTPException(400, problem)
    if username != user["username"]:
        try:
            store.set_username(user["id"], username)
        except Exception:
            raise HTTPException(409, "That username is taken")
        log.info("Account %r renamed to %r", user["username"], username)
    if new:
        store.set_password(user["id"], new)  # signs out every other session of this account
        start_session(request, response, store.user(user["id"]))
    return {"ok": True, "username": username}


@app.get("/client/install.sh", include_in_schema=False)
async def install_sh():
    return FileResponse(CLIENT_DIR / "install.sh", media_type="text/plain")


@app.get("/client/trust-ca.sh", include_in_schema=False)
async def trust_ca_sh():
    return FileResponse(CLIENT_DIR / "trust-ca.sh", media_type="text/plain")


@app.get("/client/trust-ca.ps1", include_in_schema=False)
async def trust_ca_ps1(request: Request):
    """Filled in with the address it was fetched from, so `irm … | iex` needs no argument."""
    script = (CLIENT_DIR / "trust-ca.ps1").read_text().replace("__VK_SERVER__", public_base_url(request))
    return Response(script, media_type="text/plain")


@app.get("/client/install.ps1", include_in_schema=False)
async def install_ps1():
    return FileResponse(CLIENT_DIR / "install.ps1", media_type="text/plain")


@app.get("/client/{name}", include_in_schema=False)
async def client_binary(name: str):
    if name not in RELEASE_FILES:
        raise HTTPException(404, "Unknown client build")
    path = release_path(name)
    if not path:
        raise HTTPException(404, "Client not published yet; run tools/release.sh")
    return FileResponse(path, media_type="application/octet-stream", filename=name,
                        headers={"Cache-Control": "no-cache"})


def firmware_version(path: Path) -> str:
    """The version compiled into a merged image: the app (at 0x10000) starts with
    a 24-byte image header and an 8-byte segment header, then esp_app_desc_t."""
    with path.open("rb") as f:
        f.seek(0x10000 + 32)
        desc = f.read(48)
    if len(desc) < 48 or int.from_bytes(desc[:4], "little") != 0xABCD5432:
        return ""
    return desc[16:48].split(b"\0", 1)[0].decode("ascii", "replace")


@app.get("/api/firmware")
async def firmware_info(request: Request):
    """The ESP32 firmware this server can flash, per chip, for the /esp32 page."""
    require_user(request)
    chips = {}
    for chip, label in FIRMWARE_CHIPS.items():
        name = f"vkeyboard-{chip}.bin"
        if path := release_path(name):
            chips[chip] = {"label": label, "url": f"/client/{name}", "size": path.stat().st_size,
                           "version": firmware_version(path)}
    return {"chips": chips}


@app.websocket("/v1/keyboard")
async def keyboard(ws: WebSocket):
    """A desktop client of one account; it types that account's dictation when selected.

    client -> {"type":"hello","client":"<name>","machine":"<stable id>","platform":"linux/amd64","version":"1.2.0"}
    server -> {"type":"ready","device":7,"client":"<name>","credential":"…","selected":true}
    server -> {"type":"selected","selected":false}      when the user picks another computer
    server -> {"type":"segment","text":"…"} / {"type":"key","key":"enter","state":"press"}
    server -> {"type":"update","version":"1.5.3","url":"/client/…","sha256":"…","size":…,"manual":false}
              a newer build: the desktop client installs it and restarts
    client -> {"type":"status","bluetooth":"connected"|"waiting"|"off"}   ESP32 boards, on every change
    server -> {"type":"bluetooth","on":false}   ESP32: drop and stop offering the Bluetooth keyboard
              (a tablet then shows its on-screen keyboard); true offers it again
    client -> {"type":"log","level":"error","message":"…"}   an error on the client, stored for troubleshooting
    client -> {"type":"log","level":"info","message":"…"}    ESP32, while its live log is open: relayed, not stored
    server -> {"type":"pairing","seconds":120}   ESP32: drop the connection and refuse paired devices until a new one pairs
    server -> {"type":"logs","on":true}          ESP32: stream all log lines (10 minutes at most)
    The token is the account's install token (first pairing), the device's
    credential, or a credential issued before accounts existed.
    """
    await ws.accept()
    token = request_token(ws) or ""
    device = store.device_by_credential(token)
    install_user = None if device else store.user_by_install_token(token)
    legacy = not device and not install_user and (valid_client_credential(token) or api_key_user(token) is not None)
    if not (device or install_user or legacy):
        await ws.close(code=4401, reason="Invalid install token or credential")
        return
    try:
        hello = json.loads(await ws.receive_text())
        if hello.get("type") != "hello":
            await ws.close(code=4400, reason="Expected hello")
            return
        name = str(hello.get("client") or "computer")[:80]
        info = {"platform": str(hello.get("platform") or "")[:40], "version": str(hello.get("version") or "")[:40]}
        credential = token
        if device:
            store.update_device(device["id"], last_seen=int(time.time()), **{k: v for k, v in info.items() if v})
        elif install_user:
            machine = str(hello.get("machine") or f"name:{name}")[:120]
            device, credential = store.register_device(install_user["id"], machine, name, **info)
            log.info("Paired %r for %r", name, install_user["username"])
        else:
            # Installed before accounts existed: it belongs to the admin, keyed by its old client id.
            admin = store.admin()
            if not admin:
                await ws.close(code=4401, reason="No account to attach this client to")
                return
            legacy_id = client_id_from_credential(token) if valid_client_credential(token) else name
            device = (store.device_by_machine(admin["id"], f"legacy:{legacy_id}")
                      or store.register_device(admin["id"], f"legacy:{legacy_id}", name, **info)[0])
        user = store.user(device["user_id"])
        if user["selected_device_id"] is None:  # an account's first computer types by default
            store.select_device(user["id"], device["id"])
            user = store.user(user["id"])
        room = room_for(user)
        if (old := room.devices.get(device["id"])) is not None and old is not ws:
            asyncio.create_task(old.close(code=4000, reason="Replaced by a newer connection"))
        room.devices[device["id"]] = ws
        await ws.send_json({"type": "ready", "device": device["id"], "client": device["name"],
                            "credential": credential, "selected": room.selected == device["id"]})
        if AUTO_UPDATE:
            await offer_update(ws, store._one("SELECT * FROM devices WHERE id=?", (device["id"],)))
        await announce_selection(room, skip=ws)
        if room.selected == device["id"]:
            await flush_pending(room, ws)
        error_times: list[float] = []
        info_times: list[float] = []
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("text"):
                data = json.loads(message["text"])
                if data.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
                elif data.get("type") == "log" and data.get("level") == "info":
                    # A line for the live log in the web app (ESP32): passed on, not kept.
                    now = time.monotonic()
                    info_times[:] = [t for t in info_times if now - t < 1][-LIVE_LOG_PER_SECOND:]
                    if len(info_times) < LIVE_LOG_PER_SECOND:
                        info_times.append(now)
                        room.broadcast({"type": "device-log", "device": device["id"], "level": "info",
                                        "line": str(data.get("message") or "")[:300]})
                elif data.get("type") == "log" and data.get("level") == "error":
                    # Error reports from the client, kept per client (GET /api/client-errors).
                    now = time.monotonic()
                    error_times[:] = [t for t in error_times if now - t < 60][-CLIENT_ERRORS_PER_MINUTE:]
                    if len(error_times) < CLIENT_ERRORS_PER_MINUTE:
                        error_times.append(now)
                        text = str(data.get("message") or "")[:1000]
                        store.add_client_error(device["id"], text)
                        room.broadcast({"type": "device-log", "device": device["id"], "level": "error", "line": text[:300]})
                        log.warning("Client %r (%s) reported: %s", device["name"], device["platform"], text)
                elif data.get("type") == "status" and data.get("bluetooth") in ("connected", "waiting", "off", "pairing"):
                    room.bluetooth[device["id"]] = data["bluetooth"]
                    room.broadcast({"type": "devices"})
    except (WebSocketDisconnect, ValueError, KeyError):
        pass
    finally:
        if device is not None:
            for room in rooms.values():
                if room.devices.get(device["id"]) is ws:
                    room.devices.pop(device["id"], None)
                    room.bluetooth.pop(device["id"], None)
                    room.broadcast({"type": "devices"})
            store.update_device(device["id"], last_seen=int(time.time()))


class Room:
    """One account's live state: its web UIs (watchers), connected computers, and dictation."""

    def __init__(self):
        self.watchers: set[WebSocket] = set()
        self.devices: dict[int, WebSocket] = {}  # device id -> connected desktop client
        self.bluetooth: dict[int, str] = {}       # device id -> "connected" / "waiting" / "off" (ESP32 boards)
        self.selected: int | None = None          # the device that types
        self.pending: list[tuple[float, dict]] = []  # typed while the selected device was offline
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


rooms: defaultdict[int, Room] = defaultdict(Room)  # keyed by user id


def room_for(user) -> Room:
    room = rooms[user["id"]]
    room.selected = user["selected_device_id"]
    return room


async def announce_selection(room: Room, skip: WebSocket | None = None):
    """Tells each connected computer whether it is the one that types, and the web UIs to refresh."""
    for device_id, client in list(room.devices.items()):
        if client is not skip:
            try:
                await client.send_json({"type": "selected", "selected": device_id == room.selected})
            except Exception:
                room.devices.pop(device_id, None)
    room.broadcast({"type": "devices"})


def request_token(ws: WebSocket) -> str | None:
    return ws.query_params.get("token") or ws.headers.get("authorization", "").removeprefix("Bearer ").strip() or None


def public_base_url(request: Request) -> str:
    """The origin the client used, so changing domains needs no configuration."""
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",", 1)[0].strip()
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc)).split(",", 1)[0].strip()
    return f"{scheme}://{host}".rstrip("/")


def valid_client_credential(token: str | None) -> bool:
    try:
        encoded_client, expiry, signature = (token or "").rsplit(":", 2)
        payload = f"{encoded_client}:{expiry}"
        expected = hmac.new(SESSION_SECRET.encode(), payload.encode(), "sha256").hexdigest()
        return int(expiry) >= int(time.time()) and hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False


def client_id_from_credential(token: str) -> str | None:
    try:
        encoded_client = token.rsplit(":", 2)[0]
        return base64.urlsafe_b64decode(encoded_client.encode()).decode()
    except (ValueError, TypeError):
        return None


PENDING_SECONDS = 60  # text for an offline client is kept this long, then dropped


async def send_keyboard(room: Room, message: dict):
    """Only the account's selected computer types. If it is briefly offline
    (network drop, restart), the text waits and is typed when it reconnects."""
    client = room.devices.get(room.selected)
    if client is not None:
        try:
            await client.send_json(message)
            return
        except Exception:
            room.devices.pop(room.selected, None)
    if room.selected is not None:
        room.pending.append((time.monotonic(), message))
        del room.pending[:-500]


def pointer_message(data: dict) -> dict | None:
    """A checked copy of a touchpad message for the clients, or None."""
    action = data.get("action")
    if action in ("move", "scroll"):
        try:
            limit = 4000 if action == "move" else 100
            dx, dy = (max(-limit, min(limit, int(data.get(k) or 0))) for k in ("dx", "dy"))
        except (TypeError, ValueError):
            return None
        return {"type": "pointer", "action": action, "dx": dx, "dy": dy} if dx or dy else None
    if action in ("click", "press", "release") and data.get("button") in ("left", "right", "middle"):
        return {"type": "pointer", "action": action, "button": data["button"]}
    return None


async def send_pointer(room: Room, message: dict):
    """Like send_keyboard, but never queued: stale pointer moves must not replay later."""
    client = room.devices.get(room.selected)
    if client is not None:
        try:
            await client.send_json(message)
        except Exception:
            pass


async def flush_pending(room: Room, ws: WebSocket):
    fresh = [m for t, m in room.pending if time.monotonic() - t < PENDING_SECONDS]
    room.pending.clear()
    if fresh:
        log.info("Delivering %d queued messages to a reconnected client", len(fresh))
    for message in fresh:
        await ws.send_json(message)


@app.websocket("/v1/events")
async def events(ws: WebSocket):
    """Keeps every open web UI of a keyboard in sync.

    server -> {"type":"state","active":true,"owner":"...","elapsed":3.2,"text":"...","live":true}
    server -> {"type":"state","active":false,"owner":"...","final":"..."}  when a dictation ends
    server -> {"type":"segment","owner":"...","text":"..."} / {"type":"level","owner":"...","v":0.4}
    server -> {"type":"devices"}  the client list changed; fetch /api/status
    server -> {"type":"prefs","touchpad":true}  an account preference changed (POST /api/prefs)
    server -> {"type":"device-log","device":2,"level":"info","line":"…"}  a line from an ESP32's live log
    client -> {"type":"stop"} or {"type":"cancel"}  to end another device's dictation
    client -> {"type":"pointer","action":"move"|"scroll","dx":3,"dy":-1} or
              {"type":"pointer","action":"click"|"press"|"release","button":"left"|"right"|"middle"}
              (press/release: a held button, for dragging)
              from the touchpad, passed on to the active client
    """
    await ws.accept()  # Accept first so the browser sees the 4401 close code.
    if not (user := ws_user(ws)):
        await ws.close(code=4401, reason="Login required")
        return
    room = room_for(user)
    room.watchers.add(ws)
    try:
        await ws.send_json(room.snapshot())
        while True:
            data = await ws.receive_json()
            kind = data.get("type")
            if kind in ("stop", "cancel") and room.active:
                await room.active["ws"].send_json({"type": "remote_" + kind})
            elif kind == "pointer" and (message := pointer_message(data)):
                await send_pointer(room_for(user), message)
    except Exception:
        pass
    finally:
        room.watchers.discard(ws)


@app.websocket("/v1/stream")
async def stream(ws: WebSocket):
    """Protocol:
    client -> {"type":"start","language":"en","prompt":"...","model":"...","live":true,
               "client":"<id>"}   authenticated by the session cookie (or ?token=API_KEY)
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
    if not (user := ws_user(ws)):
        await ws.close(code=4401, reason="Login required")
        return
    try:
        start = json.loads(await ws.receive_text())
    except (WebSocketDisconnect, ValueError):
        return
    model = DEFAULT_MODEL  # exactly one model is loaded; requests cannot load another
    language = start.get("language") or ""
    # "hi-latn" (Hinglish): Whisper recognises Hindi, the text is typed in English
    # letters. English and translation are also kept in English letters, in case
    # Whisper writes a word in an Indian script.
    romanise = language in ("hi-latn", "en") or TRANSLATE
    if language == "hi-latn":
        language = "hi"
    raw_history = ""  # what Whisper wrote (e.g. Devanagari), the best context for its next piece
    user_prompt = start.get("prompt") or ""
    owner = str(start.get("client") or id(ws))
    room = room_for(user)  # accounts dictate independently; one dictation per account
    if room.active:
        # Two dictations of one account would type over each other.
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
            await send_keyboard(room, {"type": "segment", "text": piece, "owner": owner})

    async def worker():
        while (item := await queue.get()) is not None:
            segment, at_pause = item
            # The end of the previous utterance keeps wording and casing consistent.
            nonlocal raw_history
            prompt = " ".join(filter(None, [user_prompt, raw_history[-200:]]))
            text = await transcribe(segment, model, language, prompt)
            raw_history = (raw_history + " " + text).strip()
            await send_piece(joiner.add(hinglish.to_latin(text) if romanise else text, at_pause))
        await send_piece(joiner.close())

    task = asyncio.create_task(worker())
    pace = str(start.get("pace") or DEFAULT_PACE)
    segmenter = Segmenter(PACES.get(pace, PACES[DEFAULT_PACE]) if live else BATCH)
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
    if not request_user(request):
        raise HTTPException(401, "Login or API key required")
    if request.method == "POST" and path in ("audio/transcriptions", "audio/translations"):
        # Whisper would load any model named in the request and keep it in GPU
        # memory for good; always use the configured one, so only one is loaded.
        form = await request.form()
        data: dict[str, list[str]] = {}
        files = []
        for key, value in form.multi_items():
            if hasattr(value, "read"):
                files.append((key, (value.filename, await value.read(), value.content_type)))
            elif key != "model":
                data.setdefault(key, []).append(value)
        data["model"] = [DEFAULT_MODEL]
        upstream = whisper.build_request("POST", f"/v1/{path}", params=request.query_params,
                                         data=data, files=files, headers=whisper_headers())
    else:
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
