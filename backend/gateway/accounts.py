"""Users, their desktop clients (devices) and sessions, stored in SQLite.

Each user has an install token (embedded in the install command), any number
of devices, and one selected device that receives their dictation. Device
credentials are random secrets stored only as SHA-256 hashes, so a leaked
database does not leak working credentials, and a device can be revoked.
"""

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                 INTEGER PRIMARY KEY,
    username           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash      TEXT NOT NULL,
    is_admin           INTEGER NOT NULL DEFAULT 0,
    install_token      TEXT NOT NULL UNIQUE,
    selected_device_id INTEGER,
    session_epoch      INTEGER NOT NULL DEFAULT 0,
    created_at         INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    machine_id      TEXT NOT NULL,
    name            TEXT NOT NULL,
    platform        TEXT NOT NULL DEFAULT '',
    version         TEXT NOT NULL DEFAULT '',
    credential_hash TEXT NOT NULL UNIQUE,
    created_at      INTEGER NOT NULL,
    last_seen       INTEGER,
    UNIQUE (user_id, machine_id)
);
"""

USERNAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789._-@")


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(candidate.hex(), digest)
    except (ValueError, TypeError):
        return False


def validate_credentials(username: str, password: str) -> str | None:
    """Returns an error message, or None when acceptable for a new account."""
    if not 3 <= len(username) <= 40 or not set(username.lower()) <= USERNAME_CHARS:
        return "Use 3–40 letters, digits, or . _ - @ for the username"
    if len(password) < 8:
        return "Use at least 8 characters for the password"
    return None


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)

    def _q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _one(self, sql: str, args=()) -> sqlite3.Row | None:
        rows = self._q(sql, args)
        return rows[0] if rows else None

    # Users

    def user_count(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM users")["n"]

    def create_user(self, username: str, password: str, admin: bool = False,
                    install_token: str | None = None) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO users (username, password_hash, is_admin, install_token, created_at) VALUES (?,?,?,?,?)",
                (username, hash_password(password), int(admin), install_token or secrets.token_urlsafe(24),
                 int(time.time())))
            return cur.lastrowid

    def user(self, user_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM users WHERE id=?", (user_id,))

    def login(self, username: str, password: str) -> sqlite3.Row | None:
        user = self._one("SELECT * FROM users WHERE username=?", (username,))
        if user and check_password(password, user["password_hash"]):
            return user
        check_password(password, "scrypt$00$00")  # similar timing for unknown users
        return None

    def set_password(self, user_id: int, password: str):
        # Bumping the epoch signs out every existing session of this user.
        self._q("UPDATE users SET password_hash=?, session_epoch=session_epoch+1 WHERE id=?",
                (hash_password(password), user_id))

    def admin(self) -> sqlite3.Row | None:
        return self._one("SELECT * FROM users WHERE is_admin=1 ORDER BY id LIMIT 1")

    def user_by_install_token(self, token: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM users WHERE install_token=?", (token,)) if token else None

    def rotate_install_token(self, user_id: int) -> str:
        token = secrets.token_urlsafe(24)
        self._q("UPDATE users SET install_token=? WHERE id=?", (token, user_id))
        return token

    def select_device(self, user_id: int, device_id: int | None):
        self._q("UPDATE users SET selected_device_id=? WHERE id=?", (device_id, user_id))

    # Devices

    def register_device(self, user_id: int, machine_id: str, name: str, platform: str, version: str
                        ) -> tuple[sqlite3.Row, str]:
        """Creates the device, or re-pairs the same machine; returns it with a new credential."""
        credential = "vkd_" + secrets.token_urlsafe(32)
        now = int(time.time())
        with self._lock:
            self._db.execute(
                """INSERT INTO devices (user_id, machine_id, name, platform, version, credential_hash, created_at, last_seen)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT (user_id, machine_id) DO UPDATE SET
                     name=excluded.name, platform=excluded.platform, version=excluded.version,
                     credential_hash=excluded.credential_hash, last_seen=excluded.last_seen""",
                (user_id, machine_id, name, platform, version, _hash_secret(credential), now, now))
        device = self._one("SELECT * FROM devices WHERE user_id=? AND machine_id=?", (user_id, machine_id))
        return device, credential

    def device_by_credential(self, credential: str) -> sqlite3.Row | None:
        if not credential:
            return None
        return self._one("SELECT * FROM devices WHERE credential_hash=?", (_hash_secret(credential),))

    def device_by_machine(self, user_id: int, machine_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM devices WHERE user_id=? AND machine_id=?", (user_id, machine_id))

    def device(self, user_id: int, device_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM devices WHERE id=? AND user_id=?", (device_id, user_id))

    def devices(self, user_id: int) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM devices WHERE user_id=? ORDER BY created_at", (user_id,))

    def update_device(self, device_id: int, **fields):
        allowed = {k: v for k, v in fields.items() if k in {"name", "platform", "version", "last_seen"}}
        if allowed:
            sets = ", ".join(f"{k}=?" for k in allowed)
            self._q(f"UPDATE devices SET {sets} WHERE id=?", (*allowed.values(), device_id))

    def delete_device(self, user_id: int, device_id: int) -> bool:
        with self._lock:
            deleted = self._db.execute("DELETE FROM devices WHERE id=? AND user_id=?", (device_id, user_id)).rowcount
            self._db.execute("UPDATE users SET selected_device_id=NULL WHERE id=? AND selected_device_id=?",
                             (user_id, device_id))
        return bool(deleted)


class Sessions:
    """Signed, stateless session cookies: user id, session epoch and expiry."""

    def __init__(self, secret: str, store: Store, days: int = 30):
        self._secret, self._store, self._ttl = secret.encode(), store, days * 86400

    def issue(self, user: sqlite3.Row) -> str:
        payload = f"{user['id']}:{user['session_epoch']}:{int(time.time()) + self._ttl}:{secrets.token_hex(8)}"
        return payload + ":" + hmac.new(self._secret, payload.encode(), "sha256").hexdigest()

    def user(self, token: str | None) -> sqlite3.Row | None:
        try:
            payload, signature = (token or "").rsplit(":", 1)
            if not hmac.compare_digest(signature, hmac.new(self._secret, payload.encode(), "sha256").hexdigest()):
                return None
            user_id, epoch, expiry, _ = payload.split(":")
            if int(expiry) < time.time():
                return None
            user = self._store.user(int(user_id))
            return user if user and user["session_epoch"] == int(epoch) else None
        except (ValueError, TypeError):
            return None


class RateLimiter:
    """At most `limit` attempts per key (client IP) in a sliding window."""

    def __init__(self, limit: int = 10, window: float = 300):
        self._limit, self._window, self._hits = limit, window, {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = [t for t in self._hits.get(key, []) if now - t < self._window]
        allowed = len(hits) < self._limit
        if allowed:
            hits.append(now)
        self._hits[key] = hits
        return allowed
