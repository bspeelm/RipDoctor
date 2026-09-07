"""Requests and responses as values, with no socket anywhere.

This is the layer that makes the rest of the web tests possible: if a request
is a value, a route is a function, and everything above the socket can be
exercised the way the core is.
"""

from __future__ import annotations

import json

import pytest

from ripdoctor.web import http as H

# --------------------------------------------------------------- requests


def test_the_query_is_split_off_the_path() -> None:
    r = H.Request.of("GET", "/api/sides?slug=album&side=a")
    assert r.path == "/api/sides"
    assert r.query == {"slug": "album", "side": "a"}


def test_a_path_with_no_query_has_none() -> None:
    assert H.Request.of("GET", "/healthz").query == {}


def test_headers_are_matched_whatever_their_case() -> None:
    """Clients send Content-Type, content-type and CONTENT-TYPE."""
    r = H.Request.of("POST", "/x", headers={"Content-Type": "application/json"})
    assert r.header("content-type") == "application/json"
    assert r.header("CONTENT-TYPE") == "application/json"


def test_a_missing_header_is_absent_rather_than_an_error() -> None:
    assert H.Request.of("GET", "/x").header("range") is None


def test_a_cookie_is_read_out_of_the_header() -> None:
    r = H.Request.of("GET", "/x", headers={"Cookie": "rd_session=abc; other=1"})
    assert r.cookie("rd_session") == "abc"
    assert r.cookie("absent") is None


def test_a_malformed_cookie_is_not_an_authenticated_request() -> None:
    """And it is not a 500 either. A broken cookie header is simply nobody."""
    r = H.Request.of("GET", "/x", headers={"Cookie": "=====;;;"})
    assert r.cookie("rd_session") is None


# -------------------------------------------------------------- json body


def test_a_json_body_is_decoded() -> None:
    r = H.Request.of("POST", "/x", body=json.dumps({"slug": "album"}).encode())
    assert r.json() == {"slug": "album"}


def test_an_empty_body_is_an_empty_object() -> None:
    assert H.Request.of("POST", "/x").json() == {}


def test_a_malformed_body_is_a_400_not_a_traceback() -> None:
    r = H.Request.of("POST", "/x", body=b"{not json")
    with pytest.raises(H.HttpError) as e:
        r.json()
    assert e.value.status == 400


def test_a_json_body_that_is_not_an_object_is_refused() -> None:
    """Every route reads fields off it, and a list has none."""
    r = H.Request.of("POST", "/x", body=b"[1, 2, 3]")
    with pytest.raises(H.HttpError) as e:
        r.json()
    assert e.value.status == 400


# -------------------------------------------------------------- responses


def test_a_json_response_carries_its_type_and_parses_back() -> None:
    r = H.ok({"ok": True})
    assert r.content_type == H.JSON and r.json() == {"ok": True}


def test_an_error_response_names_what_went_wrong() -> None:
    r = H.fail(404, "no such album")
    assert r.status == 404 and r.json()["error"] == "no such album"


def test_a_file_response_guesses_its_type_from_the_name() -> None:
    assert H.file_at("/pool/side-a.flac").content_type == "audio/flac"
    assert H.file_at("/pool/app.js").content_type.startswith("text/javascript")
    assert H.file_at("/pool/thing.unknown").content_type == H.OCTETS


def test_nothing_ever_allows_a_cross_origin_read() -> None:
    """One origin, one browser, on a LAN. The absence is the policy."""
    names = {name for name, _ in H.SECURITY_HEADERS}
    assert "Access-Control-Allow-Origin" not in names
    assert "X-Content-Type-Options" in names


def test_a_header_can_be_added_without_rebuilding_the_response() -> None:
    r = H.ok({"ok": True}).with_header("Set-Cookie", "x=1")
    assert ("Set-Cookie", "x=1") in r.headers


# ----------------------------------------------------------------- ranges


def test_no_range_header_means_the_whole_file() -> None:
    assert H.parse_range(None, 1000) is None


def test_an_open_ended_range_runs_to_the_end() -> None:
    """What a browser sends when it seeks: `bytes=N-`."""
    assert H.parse_range("bytes=500-", 1000) == (500, 999)


def test_a_closed_range_is_clamped_to_the_file() -> None:
    assert H.parse_range("bytes=0-9", 1000) == (0, 9)
    assert H.parse_range("bytes=990-2000", 1000) == (990, 999)


def test_a_suffix_range_counts_back_from_the_end() -> None:
    assert H.parse_range("bytes=-100", 1000) == (900, 999)


def test_a_suffix_longer_than_the_file_is_the_whole_file() -> None:
    assert H.parse_range("bytes=-5000", 1000) == (0, 999)


@pytest.mark.parametrize("bad", ["bytes=1000-", "bytes=1500-1600", "bytes=-", "junk"])
def test_a_range_that_cannot_be_served_is_refused_not_ignored(bad: str) -> None:
    """Answering 200 to an unsatisfiable range gives the player the wrong bytes
    with no way to tell."""
    with pytest.raises(H.Unsatisfiable):
        H.parse_range(bad, 1000)
