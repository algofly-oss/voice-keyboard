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
import hmac
import io
import json
import logging
import os
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

from accounts import RateLimiter, Sessions, Store, validate_credentials
from starlette.background import BackgroundTask
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

WHISPER_URL = os.environ.get("WHISPER_URL", "http://127.0.0.1:8001").rstrip("/")
DEFAULT_MODEL = os.environ.get("WHISPER_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2")
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
    if language:
        data["language"] = language
    if prompt:
        data["prompt"] = prompt
    async with whisper_slots:  # dictations of several accounts share the one model in turn
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


# Icons for tabs, bookmarks and home-screen shortcuts; fixed names only.
WEB_ASSETS = {"favicon.ico": ("icons/favicon.ico", "image/x-icon"),
              "manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
              **{f"icons/{name}": (f"icons/{name}", kind) for name, kind in (
                  ("icon.svg", "image/svg+xml"), ("icon-32.png", "image/png"), ("icon-192.png", "image/png"),
                  ("icon-512.png", "image/png"), ("apple-touch-icon.png", "image/png"))}}


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/manifest.webmanifest", include_in_schema=False)
@app.get("/icons/{name}", include_in_schema=False)
async def web_asset(request: Request):
    asset = WEB_ASSETS.get(request.url.path.lstrip("/"))
    if not asset:
        raise HTTPException(404, "Not found")
    return FileResponse(WEB_SOURCE.parent / asset[0], media_type=asset[1],
                        headers={"Cache-Control": "public, max-age=86400"})


# Desktop client: one self-contained binary per OS/CPU, fetched by /client/install.*
DESKTOP_BINARIES = [f"vkeyboard-{os_}-{arch}{'.exe' if os_ == 'windows' else ''}"
                    for os_ in ("linux", "darwin", "windows") for arch in ("amd64", "arm64")]
# Release asset -> (directory under RELEASES_DIR, media type).
RELEASE_FILES = {name: ("desktop", "application/octet-stream") for name in DESKTOP_BINARIES}


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


async def sync_releases_forever():
    if not GITHUB_REPO:
        return
    while True:
        try:
            tag = await sync_releases()
            log.info("Client release %s is current", tag) if tag else log.info("No %s* release on %s yet", RELEASE_TAG_PREFIX, GITHUB_REPO)
        except Exception as e:  # GitHub unreachable or token missing; keep serving what we have.
            log.warning("Release sync failed: %s", e)
        await asyncio.sleep(RELEASE_SYNC_SECONDS)


def device_json(device, room: "Room", selected_id) -> dict:
    return {"id": device["id"], "name": device["name"], "platform": device["platform"],
            "version": device["version"], "online": device["id"] in room.devices,
            "selected": device["id"] == selected_id, "lastSeen": device["last_seen"]}


@app.get("/api/status")
async def backend_status(request: Request):
    user = require_user(request)
    room = room_for(user)
    devices = [device_json(d, room, user["selected_device_id"]) for d in store.devices(user["id"])]
    return {"user": user["username"], "admin": bool(user["is_admin"]), "devices": devices,
            "selected": next((d for d in devices if d["selected"]), None)}


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
    user = require_user(request)
    name = str((await request.json()).get("name") or "").strip()[:80]
    if not name or not store.device(user["id"], device_id):
        raise HTTPException(400, "Invalid name or client")
    store.update_device(device_id, name=name)
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


@app.post("/api/key")
async def backend_key(request: Request):
    user = require_user(request)
    key = request.query_params.get("k", "")
    state = request.query_params.get("s", "press")
    if key not in {"backspace", "up", "down", "left", "right", "enter", "space"} or state not in {"down", "hold", "up", "press"}:
        raise HTTPException(400, "Unsupported key")
    # Space goes out as text so already-installed desktop clients can type it.
    message = {"type": "segment", "text": " "} if key == "space" else {"type": "key", "key": key, "state": state}
    await send_keyboard(room_for(user), message)
    return {"ok": True}


@app.get("/api/settings")
async def backend_settings(request: Request):
    require_user(request)
    return {"liveTyping": True, "language": "", "afterText": "none", "maxSeconds": 600,
            "model": DEFAULT_MODEL, "prompt": ""}


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
    user = store.login(username or ADMIN_USERNAME, password)  # blank username = admin (old login page)
    if not user:
        raise HTTPException(401, "Wrong username or password")
    start_session(request, response, user)
    return {"ok": True}


@app.post("/api/logout")
async def backend_logout(request: Request, response: Response):
    response.delete_cookie("vk_session")
    return {"ok": True}


@app.post("/api/password")
async def change_password(request: Request, response: Response):
    user = require_user(request)
    body = await request.json()
    if not store.login(user["username"], str(body.get("current") or "")):
        raise HTTPException(403, "The current password is wrong")
    new = str(body.get("new") or "")
    if (problem := validate_credentials(user["username"], new)):
        raise HTTPException(400, problem)
    store.set_password(user["id"], new)  # signs out every other session of this account
    start_session(request, response, store.user(user["id"]))
    return {"ok": True}


@app.get("/client/install.sh", include_in_schema=False)
async def install_sh():
    return FileResponse(CLIENT_DIR / "install.sh", media_type="text/plain")


@app.get("/client/install.ps1", include_in_schema=False)
async def install_ps1():
    return FileResponse(CLIENT_DIR / "install.ps1", media_type="text/plain")


@app.get("/client/{name}", include_in_schema=False)
async def client_binary(name: str):
    if name not in DESKTOP_BINARIES:
        raise HTTPException(404, "Unknown client build")
    path = release_path(name)
    if not path:
        raise HTTPException(404, "Desktop client not published yet; run tools/release.sh")
    return FileResponse(path, media_type="application/octet-stream", filename=name)


@app.websocket("/v1/keyboard")
async def keyboard(ws: WebSocket):
    """A desktop client of one account; it types that account's dictation when selected.

    client -> {"type":"hello","client":"<name>","machine":"<stable id>","platform":"linux/amd64","version":"1.2.0"}
    server -> {"type":"ready","device":7,"client":"<name>","credential":"…","selected":true}
    server -> {"type":"selected","selected":false}      when the user picks another computer
    server -> {"type":"segment","text":"…"} / {"type":"key","key":"enter","state":"press"}
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
        await announce_selection(room, skip=ws)
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("text"):
                data = json.loads(message["text"])
                if data.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
    except (WebSocketDisconnect, ValueError, KeyError):
        pass
    finally:
        if device is not None:
            for room in rooms.values():
                if room.devices.get(device["id"]) is ws:
                    room.devices.pop(device["id"], None)
                    room.broadcast({"type": "devices"})
            store.update_device(device["id"], last_seen=int(time.time()))


class Room:
    """One account's live state: its web UIs (watchers), connected computers, and dictation."""

    def __init__(self):
        self.watchers: set[WebSocket] = set()
        self.devices: dict[int, WebSocket] = {}  # device id -> connected desktop client
        self.selected: int | None = None          # the device that types
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


async def send_keyboard(room: Room, message: dict):
    """Only the account's selected computer types."""
    client = room.devices.get(room.selected)
    if client is None:
        return
    try:
        await client.send_json(message)
    except Exception:
        room.devices.pop(room.selected, None)


@app.websocket("/v1/events")
async def events(ws: WebSocket):
    """Keeps every open web UI of a keyboard in sync.

    server -> {"type":"state","active":true,"owner":"...","elapsed":3.2,"text":"...","live":true}
    server -> {"type":"state","active":false,"owner":"...","final":"..."}  when a dictation ends
    server -> {"type":"segment","owner":"...","text":"..."} / {"type":"level","owner":"...","v":0.4}
    client -> {"type":"stop"} or {"type":"cancel"}  to end another device's dictation
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
    model = start.get("model") or DEFAULT_MODEL
    language = start.get("language") or ""
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
    if not request_user(request):
        raise HTTPException(401, "Login or API key required")
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
