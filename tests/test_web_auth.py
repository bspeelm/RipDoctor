"""Login, sessions and throttling, with the clock passed in.

Expiry and rate limits are tested without waiting for either, which is the only
way these get tested at all.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from ripdoctor.web import auth as A

# Real iterations take a third of a second per verification, which is the point
# of them. Tests use few: nothing here is about how slow the hash is.
FAST = 1000


def creds(user: str = "listener", password: str = "secret") -> A.Credentials:
    return A.create(user, password, iterations=FAST)


# ------------------------------------------------------------- passwords


def test_the_right_password_verifies() -> None:
    assert creds().verify("listener", "secret")


@pytest.mark.parametrize(
    ("user", "password"), [("listener", "wrong"), ("other", "secret"), ("", "")]
)
def test_anything_else_does_not(user: str, password: str) -> None:
    assert not creds().verify(user, password)


def test_the_password_is_not_stored() -> None:
    c = creds()
    assert b"secret" not in c.digest
    assert c.digest != A.hash_password("secret", b"different salt", FAST)


def test_two_installs_of_the_same_password_do_not_match() -> None:
    """A per-install salt: the same password hashes differently everywhere, so
    one stolen file says nothing about another."""
    assert creds().digest != creds().digest


def test_both_halves_are_always_compared() -> None:
    """Returning early on an unknown username makes the answer measurably
    faster for a name that does not exist, which finds the account name without
    guessing a password."""
    import inspect

    source = inspect.getsource(A.Credentials.verify)
    assert "compare_digest" in source
    assert "return" not in source.split("right_user")[1].split("right_password")[0]


# ------------------------------------------------------------- the file


def test_the_file_is_created_private_before_anything_is_written(
    tmp_path: Path,
) -> None:
    """It holds the session secret. Opening it world-readable and tightening it
    afterwards leaves it readable, briefly, to anything that looks."""
    target = tmp_path / "sub" / "auth.json"
    A.write(target, creds())
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"credentials are {mode:o}"


def test_credentials_round_trip_through_disk(tmp_path: Path) -> None:
    target = tmp_path / "auth.json"
    original = creds()
    A.write(target, original)
    assert A.read(target) == original


def test_a_file_that_is_not_credentials_is_reported_clearly(tmp_path: Path) -> None:
    target = tmp_path / "auth.json"
    target.write_text(json.dumps({"user": "listener"}))
    with pytest.raises(A.BadCredentials, match="credentials file"):
        A.read(target)


def test_a_first_run_generates_a_password_and_returns_it_once(
    tmp_path: Path,
) -> None:
    """It is hashed on the way to disk, so the caller shows it or it is gone."""
    target = tmp_path / "auth.json"
    c, generated = A.load_or_create(target, "listener", iterations=FAST)
    assert generated and len(generated) > 12
    assert c.verify("listener", generated)
    assert generated not in target.read_text()


def test_a_second_run_reads_what_is_there_rather_than_replacing_it(
    tmp_path: Path,
) -> None:
    """Regenerating would lock the user out of their own install."""
    target = tmp_path / "auth.json"
    first, _ = A.load_or_create(target, "listener", iterations=FAST)
    again, generated = A.load_or_create(target, "listener", iterations=FAST)
    assert again == first and generated is None


def test_a_supplied_password_is_used_and_not_reported(tmp_path: Path) -> None:
    c, generated = A.load_or_create(
        tmp_path / "a.json", "listener", "mine", iterations=FAST
    )
    assert generated is None and c.verify("listener", "mine")


# ------------------------------------------------------------- sessions


def clock(t: float):  # type: ignore[no-untyped-def]
    return lambda: t


def test_a_session_names_its_user() -> None:
    s = A.Sessions(secret=b"0" * 32)
    assert (
        s.check(s.issue("listener", now=clock(1000.0)), now=clock(1000.0)) == "listener"
    )


def test_a_session_expires() -> None:
    s = A.Sessions(secret=b"0" * 32, ttl=60.0)
    token = s.issue("listener", now=clock(1000.0))
    assert s.check(token, now=clock(1059.0)) == "listener"
    assert s.check(token, now=clock(1061.0)) is None


def test_a_session_signed_by_another_install_is_refused() -> None:
    """There is no session table, so the signature is the whole of the check."""
    issued = A.Sessions(secret=b"0" * 32).issue("listener")
    assert A.Sessions(secret=b"1" * 32).check(issued) is None


def test_an_edited_session_is_refused() -> None:
    s = A.Sessions(secret=b"0" * 32)
    _body, _, sig = s.issue("listener").partition(".")
    forged = A._b64e(b'{"u":"root","e":99999999999}')
    assert s.check(f"{forged}.{sig}") is None


@pytest.mark.parametrize("bad", ["", "no-dot", "a.b", "....", "x." + "y" * 40])
def test_rubbish_is_nobody_rather_than_an_error(bad: str) -> None:
    assert A.Sessions(secret=b"0" * 32).check(bad) is None


def test_two_sessions_for_one_user_differ() -> None:
    """A nonce, so a token is not a stable identifier for the account."""
    s = A.Sessions(secret=b"0" * 32)
    assert s.issue("listener", now=clock(1.0)) != s.issue("listener", now=clock(1.0))


# -------------------------------------------------------------- cookies


def test_the_cookie_cannot_be_read_by_script_or_sent_across_sites() -> None:
    text = A.cookie("token")
    assert "HttpOnly" in text and "SameSite=Strict" in text and "Path=/" in text


def test_logging_out_expires_the_cookie_rather_than_hoping() -> None:
    assert "Max-Age=0" in A.cleared_cookie()


def test_the_cookie_is_not_marked_secure() -> None:
    """Served over plain HTTP on a LAN; a Secure cookie is never sent back."""
    assert "Secure" not in A.cookie("token")


# ------------------------------------------------------------ throttling


def test_a_run_of_failures_blocks_the_address() -> None:
    t = A.Throttle(window=60.0, limit=3)
    for i in range(3):
        assert not t.blocked("10.0.0.5", 100.0 + i)
        t.failed("10.0.0.5", 100.0 + i)
    assert t.blocked("10.0.0.5", 103.0)


def test_the_block_lifts_when_the_window_passes() -> None:
    t = A.Throttle(window=60.0, limit=3)
    for i in range(3):
        t.failed("10.0.0.5", 100.0 + i)
    assert not t.blocked("10.0.0.5", 200.0)


def test_one_address_does_not_block_another() -> None:
    t = A.Throttle(window=60.0, limit=1)
    t.failed("10.0.0.5", 100.0)
    assert t.blocked("10.0.0.5", 100.0) and not t.blocked("10.0.0.6", 100.0)


def test_a_success_clears_what_went_before() -> None:
    t = A.Throttle(window=60.0, limit=3)
    for i in range(3):
        t.failed("10.0.0.5", 100.0 + i)
    t.clear("10.0.0.5")
    assert not t.blocked("10.0.0.5", 103.0)


def test_the_table_is_bounded() -> None:
    """An unbounded one is a way to spend this machine's memory from outside
    it, which is a strange thing to build into a login."""
    t = A.Throttle(window=60.0, limit=5, tracked=50)
    for i in range(500):
        t.failed(f"10.0.{i // 256}.{i % 256}", 100.0 + i * 0.001)
    assert len(t.attempts) <= 50
