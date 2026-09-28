"""
Auth:
  - Web UI: per-person accounts stored in the database (bcrypt hashes). The very
    first platform admin is created from UI_LOGIN_USER / UI_LOGIN_PASSWORD_HASH;
    after that the environment values are no longer consulted.
  - Sessions: a signed, httponly cookie (8h) carrying the user id and the
    user's session_version. Changing a password or deactivating an account bumps
    the version, which revokes every existing session server-side.
  - Programmatic API: X-API-Key header compared in constant time. The key is
    platform-wide (all organizations); per-organization keys are tracked in plan.md.
  - Login throttling: repeated failures lock a username / client for a while.

Known limits (tracked in plan.md): platform-wide API key, in-memory throttle
state (resets on restart, single instance only).
"""
import hmac
import os
import re
import threading
import time
from collections import defaultdict, deque

import bcrypt
from fastapi import Header, HTTPException
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app import db

SESSION_COOKIE_NAME = "pf_session"
SESSION_MAX_AGE_SECONDS = 8 * 3600
MAX_PASSWORD_LENGTH = 256
MIN_PASSWORD_LENGTH = 8
# Arabic or English letters, digits, and . _ @ + - (so an email address works); no spaces.
USERNAME_PATTERN = re.compile(r"^[\w.@+-]{3,64}$")
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


def normalize_username(username: str) -> str:
    return username.strip().lower()


def is_valid_username(username: str) -> bool:
    return bool(USERNAME_PATTERN.fullmatch(username))


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"كلمة المرور يجب أن تكون {MIN_PASSWORD_LENGTH} خانات على الأقل"
    if len(password) > MAX_PASSWORD_LENGTH:
        return "كلمة المرور طويلة جدًا"
    return None


def create_session_token(user: dict) -> str:
    return _serializer().dumps({"u": user["id"], "v": user["session_version"]})


def user_from_session_token(token: str | None) -> dict | None:
    """The signed-in user, or None when the token is bad or expired, the account is
    inactive, its organization is suspended, or the session was revoked."""
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("u"), str):
        return None
    user = db.get_user(data["u"])
    if not user or not user["is_active"] or user["session_version"] != data.get("v"):
        return None
    if user["org_id"] and user["org_status"] != "active":
        return None
    return user


def _safe_equal(a: str, b: str) -> bool:
    # compare_digest on str raises for non-ASCII input; compare bytes instead.
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _password_matches(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password_bytes(password), password_hash.encode("ascii"))
    except ValueError:  # malformed hash
        return False


def check_login(username: str, password: str) -> dict | None:
    """The user for these credentials, or None. Unknown usernames still cost one
    bcrypt check so response timing doesn't reveal which accounts exist."""
    if len(password) > MAX_PASSWORD_LENGTH:
        return None
    user = db.get_user_by_username(normalize_username(username))
    if not _password_matches(password, user["password_hash"] if user else _DUMMY_HASH) or not user:
        return None
    if not user["is_active"] or (user["org_id"] and user["org_status"] != "active"):
        return None
    return user


def verify_password(user: dict, password: str) -> bool:
    return len(password) <= MAX_PASSWORD_LENGTH and _password_matches(password, user["password_hash"])


def bootstrap_platform_admin() -> None:
    """Creates the first platform admin from the environment, once. Later changes
    to that account's password are made in the app, never from the environment."""
    username = normalize_username(os.environ.get("UI_LOGIN_USER", ""))
    password_hash = os.environ.get("UI_LOGIN_PASSWORD_HASH", "")
    if not username or not password_hash.startswith("$2") or db.platform_admin_exists():
        return
    db.create_user(username, password_hash, org_id=None, role=db.PLATFORM_ADMIN_ROLE, must_change_password=False)


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
