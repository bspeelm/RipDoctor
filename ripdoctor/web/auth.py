"""Single-user authentication.

The server is reachable on a LAN and can write to the pool and run ffmpeg, so a
login is the compensating control for not being loopback-only.

Password: PBKDF2-HMAC-SHA256 with a per-install random salt. Session: HMAC-
SHA256 over a compact payload in an HttpOnly SameSite=Strict cookie. There is no
server-side session table, so a restart does not log anybody out and there is
nothing to grow unbounded.

Time is passed in rather than read, so expiry and throttling are tested without
waiting for either.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ITERATIONS = 600_000
SESSION_TTL = 30 * 24 * 3600.0
COOKIE = "ripdoctor_session"

# Not marked Secure: this is served over plain HTTP to a browser on the same
# network, and a Secure cookie would simply never be sent back.
COOKIE_FLAGS = "HttpOnly; SameSite=Strict; Path=/"


class BadCredentials(Exception):
    """The credentials file is unreadable or does not say what it must."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


@dataclass(frozen=True, slots=True)
class Credentials:
    """What this install knows about its one user."""

    user: str
    salt: bytes
    digest: bytes
    secret: bytes
    iterations: int = ITERATIONS
    created: int = 0

    def verify(self, user: str, password: str) -> bool:
        """Both halves compared in constant time, and both always compared.

        Returning early on a wrong username makes the response measurably
        faster for a name that does not exist, which is how an account name
        gets found without ever guessing a password.
        """
        right_user = hmac.compare_digest(user or "", self.user)
        got = hash_password(password or "", self.salt, self.iterations)
        right_password = hmac.compare_digest(got, self.digest)
        return right_user and right_password


def create(user: str, password: str, *, iterations: int = ITERATIONS) -> Credentials:
    salt = secrets.token_bytes(16)
    return Credentials(
        user=user,
        salt=salt,
        digest=hash_password(password, salt, iterations),
        secret=secrets.token_bytes(32),
        iterations=iterations,
        created=int(time.time()),
    )


def read(path: str | Path) -> Credentials:
    try:
        data = json.loads(Path(path).read_text())
        return Credentials(
            user=str(data["user"]),
            salt=bytes.fromhex(data["salt"]),
            digest=bytes.fromhex(data["hash"]),
            secret=bytes.fromhex(data["secret"]),
            iterations=int(data.get("iterations", ITERATIONS)),
            created=int(data.get("created", 0)),
        )
    except (ValueError, KeyError, TypeError) as e:
        raise BadCredentials(f"{path} is not a credentials file: {e}") from e


def write(path: str | Path, creds: Credentials) -> None:
    """Write the file with its mode already set.

    Created 0600 before any content goes into it: opening it world-readable and
    tightening it afterwards leaves the session secret readable, briefly, to
    anything that happens to look.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(
            {
                "user": creds.user,
                "salt": creds.salt.hex(),
                "hash": creds.digest.hex(),
                "secret": creds.secret.hex(),
                "iterations": creds.iterations,
                "created": creds.created,
            },
            f,
            indent=1,
        )


def load_or_create(
    path: str | Path,
    user: str,
    password: str = "",
    *,
    iterations: int = ITERATIONS,
) -> tuple[Credentials, str | None]:
    """The credentials on disk, or a new set. Returns any generated password.

    A generated password exists only in the return value: it is hashed on the
    way to disk and cannot be recovered from the file, so the caller has to show
    it once or it is gone.
    """
    if Path(path).is_file():
        return read(path), None
    generated = None if password else secrets.token_urlsafe(15)
    creds = create(user, password or generated or "", iterations=iterations)
    write(path, creds)
    return creds, generated


@dataclass(frozen=True, slots=True)
class Sessions:
    """Signed cookies. No table, so nothing to expire or grow."""

    secret: bytes
    ttl: float = SESSION_TTL

    def issue(self, user: str, *, now: Callable[[], float] = time.time) -> str:
        payload = json.dumps(
            {"u": user, "e": int(now() + self.ttl), "n": secrets.token_hex(8)},
            separators=(",", ":"),
        ).encode()
        return f"{_b64e(payload)}.{_b64e(self._sign(payload))}"

    def check(
        self, token: str | None, *, now: Callable[[], float] = time.time
    ) -> str | None:
        """The username for a valid unexpired session, or nothing."""
        if not token or "." not in token:
            return None
        body, _, signature = token.partition(".")
        try:
            payload = _b64d(body)
            if not hmac.compare_digest(self._sign(payload), _b64d(signature)):
                return None
            claims = json.loads(payload)
        except (ValueError, TypeError):
            return None
        if not isinstance(claims, dict) or float(claims.get("e", 0)) < now():
            return None
        user = claims.get("u")
        return str(user) if isinstance(user, str) else None

    def _sign(self, payload: bytes) -> bytes:
        return hmac.new(self.secret, payload, hashlib.sha256).digest()


def cookie(token: str, ttl: float = SESSION_TTL) -> str:
    return f"{COOKIE}={token}; {COOKIE_FLAGS}; Max-Age={int(ttl)}"


def cleared_cookie() -> str:
    return f"{COOKIE}=; {COOKIE_FLAGS}; Max-Age=0"


@dataclass
class Throttle:
    """Failed logins per address, in a window.

    Bounded on purpose: an unbounded table is a way to spend a machine's memory
    from outside it, which is a strange thing to build into a login.
    """

    window: float = 60.0
    limit: int = 5
    tracked: int = 1000
    attempts: dict[str, list[float]] = field(default_factory=dict)

    def blocked(self, ip: str, now: float) -> bool:
        recent = [t for t in self.attempts.get(ip, []) if now - t < self.window]
        if recent:
            self.attempts[ip] = recent
        else:
            self.attempts.pop(ip, None)
        return len(recent) >= self.limit

    def failed(self, ip: str, now: float) -> None:
        if len(self.attempts) >= self.tracked and ip not in self.attempts:
            self._forget(now)
        self.attempts.setdefault(ip, []).append(now)

    def clear(self, ip: str) -> None:
        self.attempts.pop(ip, None)

    def _forget(self, now: float) -> None:
        stale = [
            ip
            for ip, hits in self.attempts.items()
            if all(now - t >= self.window for t in hits)
        ]
        for ip in stale:
            del self.attempts[ip]
        if len(self.attempts) >= self.tracked:
            # Every tracked address is inside its window, which is an attack
            # rather than traffic. Forget the oldest rather than grow.
            oldest = sorted(self.attempts, key=lambda k: max(self.attempts[k]))
            for ip in oldest[: len(oldest) // 2]:
                del self.attempts[ip]
