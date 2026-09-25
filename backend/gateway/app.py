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
import html
import json
import logging
import os
import plistlib
import secrets
import time
import uuid
import wave
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs, quote

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from faster_whisper.vad import get_vad_model  # Silero VAD, bundled with the Whisper image
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


RUNTIME_API_KEY = load_runtime_api_key()
WEB_PASSWORD = os.environ.get("WEB_PASSWORD") or RUNTIME_API_KEY
SESSION_SECRET = os.environ.get("SESSION_SECRET") or API_KEY or "development-session-secret"

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
    WEB_SOURCE = BASE_DIR.parent.parent.parent / "esp32" / "web" / "index.html"


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


def authorized(token: str | None) -> bool:
    return RUNTIME_API_KEY is None or token == RUNTIME_API_KEY


@app.get("/health")
async def health():
    try:
        ok = (await whisper.get("/health", timeout=3)).status_code == 200
    except httpx.HTTPError:
        ok = False
    return JSONResponse({"status": "ok" if ok else "starting", "whisper": ok}, status_code=200 if ok else 503)


@app.get("/", include_in_schema=False)
async def web_ui():
    """Serve the same polished UI used by the ESP32, in backend mode."""
    page = WEB_SOURCE.read_text(encoding="utf-8")
    page = page.replace("<script>", "<script>window.BACKEND_MODE=true;document.documentElement.classList.add('backend-mode');</script><script>", 1)
    return HTMLResponse(page)


# Release asset -> (directory under RELEASES_DIR, media type).
RELEASE_FILES = {"VoiceKeyboard.apk": ("android", "application/vnd.android.package-archive"),
                 "VoiceKeyboard.ipa": ("ios", "application/octet-stream"),         # unsigned, for SideStore
                 "VoiceKeyboard-AdHoc.ipa": ("ios", "application/octet-stream"),   # signed for registered iPhones
                 "ios-adhoc.json": ("ios", "application/json")}                   # expiry and devices of that build
# Desktop client: one self-contained binary per OS/CPU, fetched by /client/install.*
DESKTOP_BINARIES = [f"voice-keyboard-{os_}-{arch}{'.exe' if os_ == 'windows' else ''}"
                    for os_ in ("linux", "darwin", "windows") for arch in ("amd64", "arm64")]
RELEASE_FILES.update({name: ("desktop", "application/octet-stream") for name in DESKTOP_BINARIES})
IOS_DEVICES_FILE = Path(os.environ.get("IOS_DEVICES_FILE", "/data/ios_devices.json"))
IOS_ADHOC_WORKFLOW = "voice-keyboard-ios-adhoc.yml"


def release_path(filename: str) -> Path | None:
    path = RELEASES_DIR / RELEASE_FILES[filename][0] / filename
    return path if path.is_file() else None


def adhoc_info() -> dict:
    try:
        return json.loads((release_path("ios-adhoc.json") or Path("/nonexistent")).read_text())
    except (OSError, ValueError):
        return {}


def registered_ios_devices() -> dict:
    try:
        return json.loads(IOS_DEVICES_FILE.read_text())
    except (OSError, ValueError):
        return {}


# Mobile builds are published as GitHub releases tagged voice-keyboard-v*. The
# gateway mirrors the newest one into RELEASES_DIR so /downloads can serve it
# (release assets of a private repository need a token phones do not have).
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
RELEASE_TAG_PREFIX = "voice-keyboard-v"
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
            partial.replace(target)  # atomic, so /downloads never serves half a file
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
            log.info("Mobile release %s is current", tag) if tag else log.info("No %s* release on %s yet", RELEASE_TAG_PREFIX, GITHUB_REPO)
        except Exception as e:  # GitHub unreachable or token missing; keep serving what we have.
            log.warning("Release sync failed: %s", e)
        await asyncio.sleep(RELEASE_SYNC_SECONDS)


@app.get("/downloads", include_in_schema=False)
async def downloads(request: Request):
    """Install page for the mobile keyboards."""
    base = public_base_url(request)
    ios_url = os.environ.get("IOS_DOWNLOAD_URL", "")
    logged_in = authenticated_request(request)
    ios = []
    if request.query_params.get("registered"):
        ios.append('<p class="note">This iPhone is registered. A signed build that includes it is being prepared '
                   'on GitHub, which takes about 10 minutes. Come back and tap <b>Install on this iPhone</b>.</p>')
    if ios_url:
        ios.append(f'<a class="button" href="{html.escape(ios_url, quote=True)}">Install from TestFlight / App Store</a>')
    # Registering a device uses one of the 100 yearly Ad Hoc slots, so it needs a login.
    ios.append('<a class="button secondary" href="/api/ios/register.mobileconfig">1. Register this iPhone</a>'
               '<p class="muted">Once per iPhone. Safari downloads a profile: '
               'open Settings → Profile Downloaded → Install. It only reports the device ID to this server.</p>'
               if logged_in else '<p class="muted"><a href="/">Log in</a> on this iPhone to register it for the signed install.</p>')
    info = adhoc_info()
    if release_path("VoiceKeyboard-AdHoc.ipa") and info:
        manifest = quote(f"{base}/downloads/ios/manifest.plist", safe="")
        ios += [f'<a class="button" href="itms-services://?action=download-manifest&amp;url={manifest}">2. Install on this iPhone</a>',
                f'<p class="muted">Signed for {len(info.get("devices", []))} registered device(s). '
                f'Valid until {html.escape(info.get("expires", "")[:10])}; it is re-signed automatically before then, '
                'so just tap Install again when this date changes.</p>']
    if release_path("VoiceKeyboard.ipa"):
        ipa = quote(f"{base}/downloads/ios/VoiceKeyboard.ipa", safe="")
        ios.append('<details><summary>No Apple developer account? Sideload instead</summary>'
                   f'<a class="button" href="sidestore://install?url={ipa}">Install with SideStore</a>'
                   f'<a class="button secondary" href="altstore://install?url={ipa}">Install with AltStore</a>'
                   '<a class="plain" href="/downloads/ios/VoiceKeyboard.ipa">Download the unsigned .ipa</a>'
                   '<p class="muted">SideStore and AltStore sign the app with your own Apple ID. '
                   'With a free Apple ID it must be refreshed every 7 days.</p></details>')
    android = ('<a class="button" href="/downloads/android/VoiceKeyboard.apk">Download APK</a>'
               '<p class="muted">Allow the one-time “install unknown apps” prompt when asked.</p>'
               if release_path("VoiceKeyboard.apk") else '<p class="muted">No Android build published yet.</p>')
    if logged_in:
        # The installed app registers the voicekeyboard:// scheme and exchanges the
        # install token at /api/pair, so pairing is a single tap on the phone.
        pairing = """<p>Open the app once, then tap below to pair this phone with the server.</p>
<button class="button" id="pair">Pair this phone</button><p class="muted" id="pairStatus"></p>
<script>document.getElementById('pair').onclick=async()=>{const s=document.getElementById('pairStatus');
const r=await fetch('/api/enroll');if(!r.ok){s.textContent='Could not create a pairing code.';return}
const t=await r.json();location.href='voicekeyboard://pair?server='+encodeURIComponent(location.origin)+'&token='+encodeURIComponent(t.token);
s.textContent='If nothing opened, install the app first.'}</script>"""
    else:
        pairing = '<p>After installing, <a href="/">log in to the web app</a> on this phone and return here to pair it in one tap.</p>'
    return HTMLResponse("""<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Voice Keyboard downloads</title><style>
body{font:16px system-ui,sans-serif;max-width:42rem;margin:2.5rem auto;padding:0 1rem;color:#f2f3f5;background:#1e1f22}
h2{font-size:1.1rem;margin:2rem 0 .5rem}
a.button,button.button{display:block;width:100%;box-sizing:border-box;text-align:center;font:inherit;font-weight:600;border:0;cursor:pointer;background:#5865f2;color:white;text-decoration:none;padding:.9rem;border-radius:.7rem;margin:.6rem 0}
a.button.secondary{background:#383a40}
.muted{color:#b5bac1;font-size:.9rem;margin:.4rem 0}
a{color:#8ea1ff}a.plain{display:block;text-align:center;margin:.4rem 0}
.note{background:rgba(35,165,90,.14);color:#5fd08f;padding:.8rem 1rem;border-radius:.7rem}
details{margin-top:1rem}summary{cursor:pointer;color:#b5bac1}
</style><h1>Voice Keyboard</h1><p>Install the keyboard on this phone, then pair it with your server.</p>
<h2>Android</h2>""" + android + "<h2>iPhone / iPad</h2>" + "".join(ios) + "<h2>Pair</h2>" + pairing +
                        """<p><a href="/">Back to web app</a></p></html>""")


@app.get("/downloads/ios/manifest.plist", include_in_schema=False)
async def ios_manifest(request: Request):
    """Over-the-air install manifest for the signed Ad Hoc build (itms-services)."""
    info = adhoc_info()
    if not release_path("VoiceKeyboard-AdHoc.ipa") or not info:
        raise HTTPException(404, "No signed iOS build published")
    base = public_base_url(request)
    manifest = {"items": [{
        "assets": [{"kind": "software-package", "url": f"{base}/downloads/ios/VoiceKeyboard-AdHoc.ipa"}],
        "metadata": {"bundle-identifier": info["bundleId"], "bundle-version": info.get("bundleVersion", "1"),
                     "kind": "software", "title": "Voice Keyboard"}}]}
    return Response(plistlib.dumps(manifest), media_type="application/xml")


@app.get("/downloads/{platform}/{filename}", include_in_schema=False)
async def release_file(platform: str, filename: str):
    if RELEASE_FILES.get(filename, ("",))[0] != platform:
        raise HTTPException(404, "Release not found")
    path = release_path(filename)
    if not path:
        raise HTTPException(404, "No release published")
    return FileResponse(path, media_type=RELEASE_FILES[filename][1], filename=filename)


@app.get("/api/ios/register.mobileconfig", include_in_schema=False)
async def ios_register_profile(request: Request):
    """Apple "Profile Service" profile: installing it makes the iPhone POST its UDID to us."""
    if not authenticated_request(request):
        raise HTTPException(401, "Login required")
    base = public_base_url(request)
    profile = {
        "PayloadType": "Profile Service", "PayloadVersion": 1,
        "PayloadIdentifier": "ai.algofly.voicekeyboard.udid", "PayloadUUID": str(uuid.uuid4()),
        "PayloadDisplayName": "Voice Keyboard device registration",
        "PayloadDescription": "Sends this device's ID to your Voice Keyboard server so a signed build can include it. "
                              "Nothing is installed; the profile is removed automatically.",
        "PayloadOrganization": "Voice Keyboard",
        "PayloadContent": {"URL": f"{base}/api/ios/udid?token={quote(current_enrollment())}",
                           "DeviceAttributes": ["UDID", "PRODUCT", "VERSION", "DEVICE_NAME"],
                           "Challenge": secrets.token_hex(8)},
    }
    return Response(plistlib.dumps(profile), media_type="application/x-apple-aspen-config",
                    headers={"Content-Disposition": 'attachment; filename="VoiceKeyboard.mobileconfig"'})


@app.post("/api/ios/udid", include_in_schema=False)
async def ios_register_udid(request: Request):
    """Receives the device attributes from the profile service and starts a signed build."""
    if not valid_enrollment(request.query_params.get("token")):
        raise HTTPException(401, "Registration link was replaced")
    body = await request.body()
    # The body is a CMS-signed plist; the XML payload sits inside it unencrypted.
    start, end = body.find(b"<?xml"), body.find(b"</plist>")
    try:
        attributes = plistlib.loads(body[start:end + len(b"</plist>")])
        udid = str(attributes["UDID"])
    except Exception:
        raise HTTPException(400, "Unrecognised device response")
    name = str(attributes.get("DEVICE_NAME") or attributes.get("PRODUCT") or "iPhone")[:60]
    devices = registered_ios_devices()
    devices[udid] = {"name": name, "product": attributes.get("PRODUCT", ""), "registered": int(time.time())}
    IOS_DEVICES_FILE.parent.mkdir(parents=True, exist_ok=True)
    IOS_DEVICES_FILE.write_text(json.dumps(devices, indent=2) + "\n")
    log.info("Registered iOS device %s (%s)", name, udid[:8])
    try:
        await dispatch_adhoc_build(udid, name)
    except Exception as e:
        log.warning("Could not start the signed iOS build: %s", e)
    # Apple's profile service expects a redirect; Safari then opens that page.
    return Response(status_code=301, headers={"Location": f"{public_base_url(request)}/downloads?registered=1"})


async def dispatch_adhoc_build(udid: str, name: str):
    """Ask GitHub Actions to register the device with Apple and re-sign the Ad Hoc build."""
    if not (GITHUB_REPO and GITHUB_TOKEN):
        raise RuntimeError("GITHUB_REPO/GITHUB_TOKEN not set")
    try:
        ref = json.loads((RELEASES_DIR / "release.json").read_text())["tag"]
    except (OSError, ValueError, KeyError):
        ref = os.environ.get("GITHUB_BUILD_REF", "main")
    async with httpx.AsyncClient(timeout=30) as github:
        response = await github.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{IOS_ADHOC_WORKFLOW}/dispatches",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
            json={"ref": ref, "inputs": {"udid": udid, "name": name}})
        response.raise_for_status()


@app.get("/api/status")
async def backend_status(request: Request):
    if not authenticated_request(request):
        raise HTTPException(401, "Login required")
    clients = [{"id": client_id, "room": room_name}
               for room_name, room in rooms.items() for client_id in room.keyboard]
    return {"ble": bool(clients), "bleEnabled": True, "typing": False, "clients": len(clients),
            "clientList": clients,
            "ip": request.url.hostname or "backend", "hostname": request.url.hostname or "backend"}


@app.post("/api/key")
async def backend_key(request: Request):
    if not authenticated_request(request):
        raise HTTPException(401, "Login required")
    key = request.query_params.get("k", "")
    state = request.query_params.get("s", "press")
    if key not in {"backspace", "up", "down", "left", "right", "enter", "space"} or state not in {"down", "hold", "up", "press"}:
        raise HTTPException(400, "Unsupported key")
    # Space goes out as text so already-installed desktop clients can type it.
    message = {"type": "segment", "text": " "} if key == "space" else {"type": "key", "key": key, "state": state}
    await send_keyboard(rooms[request.query_params.get("room") or "voice-keyboard"], message)
    return {"ok": True}


@app.get("/api/settings")
async def backend_settings(request: Request):
    if not authenticated_request(request):
        raise HTTPException(401, "Login required")
    return {"hostname": "voice-keyboard", "serverUrl": public_base_url(request),
            "apiKey": RUNTIME_API_KEY or "", "liveTyping": True, "language": "", "afterText": "none",
            "maxSeconds": 600, "keyDelayMs": 0, "model": DEFAULT_MODEL, "prompt": ""}


@app.api_route("/api/enroll", methods=["GET", "POST"])
async def enroll(request: Request):
    """GET returns the current install command; POST replaces its token.

    The token does not expire. It keeps working for new installs until the user
    generates a new one; computers and phones already paired keep their own
    credentials and stay connected.
    """
    if not authenticated_request(request):
        raise HTTPException(401, "Login required")
    token = rotate_enrollment() if request.method == "POST" else current_enrollment()
    base = public_base_url(request)
    return {"token": token,
            "shell": f"curl -fsSL {base}/client/install.sh | sh -s -- --server {base} --token {token}",
            "powershell": f"& ([scriptblock]::Create((irm '{base}/client/install.ps1'))) -Server '{base}' -Token '{token}'"}


@app.post("/api/pair")
async def pair(request: Request):
    """Exchange a one-time enrollment token for a mobile keyboard credential."""
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Expected JSON")
    token = str(body.get("token") or "")
    if not valid_enrollment(token):
        raise HTTPException(401, "Pairing code was replaced by a newer one")
    client_id = str(body.get("client") or f"mobile-{secrets.token_hex(4)}")[:120]
    return {"client": client_id, "credential": issue_client_credential(client_id),
            "server": public_base_url(request)}


@app.post("/api/login")
async def backend_login(request: Request, response: Response):
    body = parse_qs((await request.body()).decode("utf-8"))
    password = (body.get("password") or [""])[0]
    if not WEB_PASSWORD or not secrets.compare_digest(password, WEB_PASSWORD):
        raise HTTPException(401, "Invalid password")
    session = session_token()
    response.set_cookie("vk_session", session, max_age=86400 * 30, httponly=True, secure=public_base_url(request).startswith("https://"), samesite="lax")
    return {"ok": True}


@app.post("/api/logout")
async def backend_logout(request: Request, response: Response):
    response.delete_cookie("vk_session")
    return {"ok": True}


@app.post("/api/api-key/refresh")
async def refresh_api_key(request: Request):
    if not valid_session(request.cookies.get("vk_session")):
        raise HTTPException(401, "Login required")
    global RUNTIME_API_KEY
    RUNTIME_API_KEY = secrets.token_urlsafe(32)
    save_auth(api_key=RUNTIME_API_KEY)
    return {"apiKey": RUNTIME_API_KEY}


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
    """Authenticated desktop keyboard client receiving transcription segments."""
    await ws.accept()
    query_token = request_token(ws)
    if not authorized(query_token) and not valid_enrollment(query_token) and not valid_client_credential(query_token):
        await ws.close(code=4401, reason="Invalid enrollment token")
        return
    try:
        hello = json.loads(await ws.receive_text())
        if hello.get("type") != "hello":
            await ws.close(code=4400, reason="Expected hello")
            return
        room_name = str(hello.get("room") or "voice-keyboard")[:80]
        client_id = str(hello.get("client") or secrets.token_hex(8))[:120]
        credential_client = client_id_from_credential(query_token) if valid_client_credential(query_token) else None
        if credential_client and credential_client != client_id:
            await ws.close(code=4401, reason="Credential belongs to another client")
            return
        room = rooms[room_name]
        room.keyboard[client_id] = ws
        # The shared install token is exchanged for a per-client credential.
        if valid_enrollment(query_token):
            client_secret = issue_client_credential(client_id)
        else:
            client_secret = query_token
        await ws.send_json({"type": "ready", "room": room_name, "client": client_id,
                            "credential": client_secret})
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
        for room in rooms.values():
            for client_id, client in list(room.keyboard.items()):
                if client is ws:
                    room.keyboard.pop(client_id, None)


class Room:
    """Web UIs of one keyboard: watchers see the active dictation mirrored."""

    def __init__(self):
        self.watchers: set[WebSocket] = set()
        self.keyboard: dict[str, WebSocket] = {}
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


def request_token(ws: WebSocket) -> str | None:
    return ws.query_params.get("token") or ws.headers.get("authorization", "").removeprefix("Bearer ").strip() or None


def authenticated_request(request: Request) -> bool:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    return authorized(token or request.query_params.get("token")) or valid_session(request.cookies.get("vk_session"))


def session_token() -> str:
    payload = f"{secrets.token_urlsafe(24)}:{int(time.time()) + 86400 * 30}"
    signature = hmac.new(SESSION_SECRET.encode(), payload.encode(), "sha256").hexdigest()
    return f"{payload}:{signature}"


def valid_session(token: str | None) -> bool:
    if not token:
        return False
    try:
        nonce, expiry, signature = token.rsplit(":", 2)
        payload = f"{nonce}:{expiry}"
        expected = hmac.new(SESSION_SECRET.encode(), payload.encode(), "sha256").hexdigest()
        return int(expiry) >= int(time.time()) and hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False


def public_base_url(request: Request) -> str:
    configured = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if configured:
        return configured
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",", 1)[0].strip()
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc)).split(",", 1)[0].strip()
    return f"{scheme}://{host}".rstrip("/")


def current_enrollment() -> str:
    return load_auth().get("enrollment_token") or rotate_enrollment()


def rotate_enrollment() -> str:
    token = secrets.token_urlsafe(24)
    save_auth(enrollment_token=token)
    return token


def valid_enrollment(token: str | None) -> bool:
    stored = load_auth().get("enrollment_token")
    return bool(token and stored and secrets.compare_digest(token, stored))


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


def issue_client_credential(client_id: str) -> str:
    encoded_client = base64.urlsafe_b64encode(client_id.encode()).decode()
    expiry = str(int(time.time()) + 365 * 24 * 3600)
    payload = f"{encoded_client}:{expiry}"
    signature = hmac.new(SESSION_SECRET.encode(), payload.encode(), "sha256").hexdigest()
    return f"{payload}:{signature}"


async def send_keyboard(room: Room, message: dict):
    for client_id, client in list(room.keyboard.items()):
        try:
            await client.send_json(message)
        except Exception:
            room.keyboard.pop(client_id, None)


@app.websocket("/v1/events")
async def events(ws: WebSocket):
    """Keeps every open web UI of a keyboard in sync.

    server -> {"type":"state","active":true,"owner":"...","elapsed":3.2,"text":"...","live":true}
    server -> {"type":"state","active":false,"owner":"...","final":"..."}  when a dictation ends
    server -> {"type":"segment","owner":"...","text":"..."} / {"type":"level","owner":"...","v":0.4}
    client -> {"type":"stop"} or {"type":"cancel"}  to end another device's dictation
    """
    await ws.accept()  # Accept first so the browser sees the 4401 close code.
    token = request_token(ws)
    if not authorized(token) and not valid_client_credential(token):
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
    token = request_token(ws)
    if not authorized(token) and not valid_client_credential(token):
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
