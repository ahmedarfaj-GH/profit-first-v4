"""
Auth:
  - Web UI: a single login (username + bcrypt-hashed password from env). Session
    identity is carried in a signed, httponly cookie with an 8h lifetime.
  - Programmatic API: X-API-Key header compared in constant time.
  - Login throttling: repeated failures lock a username / client for a while.

Known limits (tracked in plan.md): one shared account, no server-side session
revocation, in-memory throttle state (resets on restart, single instance only).
"""
import hmac
import os
import threading
import time
from collections import defaultdict, deque

import bcrypt
from fastapi import Header, HTTPException
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SESSION_COOKIE_NAME = "pf_session"
SESSION_MAX_AGE_SECONDS = 8 * 3600
MAX_PASSWORD_LENGTH = 256
BCRYPT_MAX_BYTES = 72  # bcrypt ignores anything beyond this; bcrypt>=5 raises instead


def password_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password_bytes(password), bcrypt.gensalt(rounds=12)).decode("ascii")


# Verified against when the username is wrong, so timing doesn't reveal it.
_DUMMY_HASH = hash_password("timing-equalisation-only")


def _serializer() -> URLSafeTimedSerializer:
    secret = os.environ.get("SESSION_SECRET")
    if not secret:
        raise RuntimeError("SESSION_SECRET is not set — copy .env.example to .env and set a real secret.")
    return URLSafeTimedSerializer(secret, salt="pf-session")


def create_session_token(username: str) -> str:
    return _serializer().dumps({"u": username})


def verify_session_token(token: str | None) -> str | None:
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("u")


def _safe_equal(a: str, b: str) -> bool:
    # compare_digest on str raises for non-ASCII input; compare bytes instead.
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def check_login(username: str, password: str) -> bool:
    expected_user = os.environ.get("UI_LOGIN_USER")
    expected_hash = os.environ.get("UI_LOGIN_PASSWORD_HASH")
    if not expected_user or not expected_hash or len(password) > MAX_PASSWORD_LENGTH:
        return False
    user_ok = _safe_equal(username.strip(), expected_user)
    try:
        password_ok = bcrypt.checkpw(password_bytes(password), (expected_hash if user_ok else _DUMMY_HASH).encode("ascii"))
    except ValueError:  # malformed hash in configuration
        return False
    return user_ok and password_ok


def require_api_key(x_api_key: str | None = Header(default=None)) -> bool:
    expected = os.environ.get("API_KEY")
    if not expected or not x_api_key or not _safe_equal(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")
    return True


class LoginThrottle:
    def __init__(self, max_failures: int = 5, window_seconds: int = 900):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _purge(self, key: str, now: float) -> deque:
        hits = self._failures[key]
        while hits and now - hits[0] > self.window_seconds:
            hits.popleft()
        if not hits:
            self._failures.pop(key, None)
            return deque()
        return hits

    def is_blocked(self, *keys: str) -> bool:
        now = time.monotonic()
        with self._lock:
            return any(len(self._purge(k, now)) >= self.max_failures for k in keys)

    def record_failure(self, *keys: str) -> None:
        now = time.monotonic()
        with self._lock:
            for k in keys:
                self._purge(k, now)
                self._failures[k].append(now)

    def reset(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._failures.pop(k, None)


login_throttle = LoginThrottle()
