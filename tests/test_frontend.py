"""The front end, checked the way the Python is.

There is no compiler here, so these stand in for one. The first is the check
that earned its place: three times in one session a string-replace edit silently
failed to match and left the JS reaching for a node that was never added, each
time surfacing as a TypeError on null in front of somebody using it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ripdoctor.web.app import App
from ripdoctor.web.auth import Sessions
from ripdoctor.web.routes import build
from ripdoctor.web.service import Service

STATIC = Path(__file__).resolve().parent.parent / "ripdoctor" / "web" / "static"

ID = re.compile(r'id="([A-Za-z0-9_-]+)"')
REACHED = re.compile(r'\$\("#([A-Za-z0-9_-]+)')
# Two shapes: a plain string, and a template literal whose holes may contain
# quotes of their own.
QUOTED = re.compile(r"[\"'](/api/[^\"']*)")
TEMPLATE = re.compile(r"`(/api/[^`]*)`")
HOLE = re.compile(r"\$\{[^{}]*\}")


def scripts() -> list[Path]:
    return sorted(STATIC.glob("*.js"))


def markup() -> str:
    return "".join(p.read_text() for p in sorted(STATIC.glob("*.html")))


def test_there_is_a_front_end_at_all() -> None:
    """Every test below passes trivially against an empty directory."""
    assert scripts(), "no JavaScript was found"
    assert markup(), "no HTML was found"


def test_every_element_the_javascript_reaches_for_exists() -> None:
    ids = set(ID.findall(markup()))
    assert len(ids) > 20, "the markup has almost no elements; the pattern drifted"

    missing = []
    for script in scripts():
        source = script.read_text()
        for found in REACHED.finditer(source):
            if found.group(1) not in ids:
                line = source[: found.start()].count("\n") + 1
                missing.append(f'{script.name}:{line}  $("#{found.group(1)}")')
    assert not missing, "reached for and not in the HTML:\n  " + "\n  ".join(missing)


def test_every_module_the_javascript_imports_is_there() -> None:
    names = {p.name for p in scripts()}
    for script in scripts():
        for found in re.finditer(r'from "/([A-Za-z0-9_.-]+)"', script.read_text()):
            assert found.group(1) in names, f"{script.name} imports {found.group(1)}"


# A call that names a method, and the path it names it for. Matching a path
# alone let two calls through in one session: a prepare polled with GET against
# a POST-only route, and an upload posted to a path that only answers GET. Both
# read as "no such route" in front of somebody using it.
POSTING = re.compile(
    r"postJSON\(\s*[`\"'](/api/[^`\"']*)"
    r"|fetch\(\s*[`\"'](/api/[^`\"']*)[`\"'][^)]*?method:\s*[\"']POST",
    re.S,
)


def a_route_table() -> set[str]:
    service = Service(
        layout=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
        thresholds=None,  # type: ignore[arg-type]
        runner=None,  # type: ignore[arg-type]
        credentials=None,  # type: ignore[arg-type]
        sessions=Sessions(secret=b"0" * 32),
    )
    app: App = build(service)
    return {r.pattern.pattern for r in app.routes}


def posting_routes() -> set[str]:
    service = Service(
        layout=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
        thresholds=None,  # type: ignore[arg-type]
        runner=None,  # type: ignore[arg-type]
        credentials=None,  # type: ignore[arg-type]
        sessions=Sessions(secret=b"0" * 32),
    )
    app: App = build(service)
    return {r.pattern.pattern for r in app.routes if r.method == "POST"}


def matches(pattern: str, path: str) -> bool:
    return (
        re.match(
            pattern.replace("([^/]+)", "X").replace("(\\d+)", "1"),
            path.replace("{}", "X"),
        )
        is not None
    )


@pytest.mark.parametrize("script", scripts(), ids=lambda p: p.name)
def test_every_endpoint_the_page_calls_exists(script: Path) -> None:
    """A page calling a route that is not there fails in the browser, at the
    moment somebody presses the button, with a 404 and no explanation."""
    table = a_route_table()
    source = script.read_text()
    unknown = []
    for literal in sorted(set(QUOTED.findall(source) + TEMPLATE.findall(source))):
        # A hole stands for one path segment - "1", because a track index is
        # matched by a numeric pattern and a slug by any - or for nothing at
        # all, which is what a hole holding an optional query string is.
        stems = {
            HOLE.sub(fill, literal).split("?", 1)[0].rstrip("/") for fill in ("1", "")
        }
        if not any(re.match(p, stem) for p in table for stem in stems):
            unknown.append(literal)
    assert not unknown, f"{script.name} calls routes that do not exist: {unknown}"


@pytest.mark.parametrize("script", scripts(), ids=lambda p: p.name)
def test_every_post_the_page_makes_has_a_post_route(script: Path) -> None:
    """A route answering the wrong method is a 404 with a confusing message.
    Matching the path alone missed a poll sent with GET to a POST-only route,
    and an upload sent with POST to a path that only answers GET."""
    table = posting_routes()
    wrong = []
    for found in POSTING.finditer(script.read_text()):
        literal = found.group(1) or found.group(2)
        stems = {
            HOLE.sub(fill, literal).split("?", 1)[0].rstrip("/") for fill in ("1", "")
        }
        if not any(re.match(p, stem) for p in table for stem in stems):
            wrong.append(literal)
    assert not wrong, f"{script.name} posts to routes that take no POST: {wrong}"


def test_the_record_picker_cannot_set_the_header_width() -> None:
    """A select sizes itself to its widest option, not its selected one, so one
    long album title pushed the last actions off the right edge where nothing
    said they were there.

    `min-width:0` is what lets a flex item shrink below its content at all, and
    without it the rest of the rule does nothing. Asserted because a stylesheet
    has no other gate: the failure is invisible until somebody's screen is
    narrower than the developer's.
    """
    css = (STATIC / "style.css").read_text()
    rule = re.search(r"#album\{([^}]*)\}", css)
    assert rule, "no width rule for the record picker"
    assert "min-width:0" in rule.group(1), "it cannot shrink below its content"
    assert "max-width" in rule.group(1), "it can grow without limit"
    assert re.search(r"header > button[^{]*\{[^}]*flex:none", css), (
        "the header actions can be squeezed off the edge"
    )


def test_the_capture_controls_invent_no_rate_or_depth() -> None:
    """A busy device cannot be asked what it accepts, and busy is the ordinary
    case: a capture is running. A hardcoded fallback then offers a list the
    configured depth is absent from, so the selection falls to the invented
    value and a side records at the wrong width - silently, labelled as the
    best the device can do.

    Asserted on the source because there is no other gate: the failure needs a
    busy sound card to reproduce, and twenty minutes to notice.
    """
    js = (STATIC / "app.js").read_text()
    for body in re.findall(r"function fill(?:Rates|Formats)\(.*?\n\}", js, re.S):
        fallback = re.search(r"\?\s*d\.(?:rates|formats)\s*(.*?);", body, re.S)
        assert fallback, "the device's own answer is no longer what is preferred"
        assert not re.search(r':\s*\[\s*["\']?[A-Z0-9]', fallback.group(1)), (
            f"a capture setting is invented here rather than read:\n{fallback.group(1)}"
        )


def test_a_tape_job_s_error_outlives_the_reload_that_follows_the_job() -> None:
    """`watchTape` sets the error and then calls `loadTapes`: blanking it there leaves
    a failed job showing nothing anywhere. Only an action clears it."""
    source = (STATIC / "app.js").read_text()
    start = source.index("async function loadTapes(")
    body = source[start : source.index("async function openTape(", start)]
    assert '"#tp-err").textContent =' in body
    assert not re.search(r'#tp-err"\)\.textContent = [^;]*: ""', body)
    assert '$("#tp-err").textContent = "";' not in body
    assert '$("#tp-pick").onchange = (e) => { $("#tp-err").textContent = "";' in source


def test_the_tapes_view_fits_a_phone_and_keeps_every_control_reachable() -> None:
    """Thirty rem of form clipped every sentence at 390 px, each table held a share
    of the height and scrolled inside it, and the judge column ran off the edge.
    An error sat under the last button of a long column, off the screen."""
    css = (STATIC / "style.css").read_text()
    narrow = re.search(r"@media \(max-width:48rem\)\{(.*?)\n\}", css, re.S)
    assert narrow, "nothing changes on a narrow screen"
    assert "#captureview{flex-direction:column" in narrow.group(1)
    assert ".ripform{width:100%}" in narrow.group(1)
    assert css.index(".ripform{width:30rem") < narrow.start(), "the wide rule wins"
    scroll = re.search(r"\.tp-scroll\{([^}]*)\}", css)
    assert scroll and "flex:none" in scroll.group(1)
    assert "white-space:normal" in re.search(r"table\.ear td\{([^}]*)\}", css).group(1)  # type: ignore[union-attr]
    assert "position:fixed" in re.search(
        r"#tp-err:not\(:empty\)\{([^}]*)\}", css
    ).group(1)  # type: ignore[union-attr]
    assert "document.body.dataset.mode = mode" in (STATIC / "app.js").read_text()


def test_a_reload_on_a_tape_comes_back_to_that_tape() -> None:
    """It landed on Cut, and on the first tape on the list once Tapes was pressed,
    with any job on the tape left unwatched."""
    js = (STATIC / "app.js").read_text()
    assert 'remember("rd-mode", mode)' in js
    assert 'if (t) remember("rd-tape", t.id)' in js
    assert 'recalled("rd-mode") === "tapes"' in js
    assert 'keep || tape?.id || recalled("rd-tape")' in js


def test_a_refusal_is_said_once_where_it_can_be_acted_on() -> None:
    """The File and Archive blocks repeated the cut's whole refusal, song by song."""
    js = (STATIC / "app.js").read_text()
    assert '"waits for the cut, above"' in js
    assert '"waits until the songs are filed"' in js


def test_a_job_lost_to_a_restart_is_said_plainly_and_a_blip_is_waited_out() -> None:
    """It said "no job" and left the last progress on the page."""
    js = (STATIC / "app.js").read_text()
    body = re.search(r"async function watchTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "if (e.status || ++missed >= TRIES) throw e" in body
    assert "e.status === 404" in body and "start it again" in body
    assert "if (tapeWatching) for (const b of TAPE_JOB_BUTTONS)" in js


def test_a_pasted_release_keeps_its_place_in_the_edition_picker() -> None:
    """A release from no candidate list left the picker blank, and Use then sent ''."""
    js = (STATIC / "app.js").read_text()
    assert "[current, ...others]" in js


def test_an_archived_tape_offers_nothing_that_would_change_it() -> None:
    """Find boundaries on one said to upload audio, and every other control refused."""
    js = (STATIC / "app.js").read_text()
    assert "if (has && t.playlist.done) archivedControls()" in js
    body = re.search(r"function archivedControls\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "#tp-process" in body and '$("#tapepane").querySelectorAll' in body


def test_a_song_in_the_library_or_not_confirmed_offers_no_other_release() -> None:
    """Change it stayed live on these songs, only for the route to refuse it."""
    js = (STATIC / "app.js").read_text()
    held = re.search(r"const CHOICE_HELD = \{.*?\n\};", js, re.S).group(0)  # type: ignore[union-attr]
    assert all(f"{state}:" in held for state in ("filed", "already", "unknown"))
    body = re.search(r"function reviewRow\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    rest = body.split('CHOICE_HELD[song.import_begun ? "unknown" : song.filing]', 1)[1]
    assert 'change.querySelectorAll("input, select, button")' in rest
    assert "c.disabled = true" in rest


def test_a_job_s_error_is_still_there_after_a_reload() -> None:
    js = (STATIC / "app.js").read_text()
    body = re.search(r"async function openTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "tape.job?.error" in body


def test_a_song_on_an_archived_tape_is_not_called_uncut() -> None:
    js = (STATIC / "app.js").read_text()
    assert 't.playlist.done && row.cut === "not cut" ? "cleared" : row.cut' in js


def test_a_tape_s_waveform_is_the_rip_s_editor_and_player_and_not_a_copy_of_them() -> (
    None
):
    js = (STATIC / "app.js").read_text()
    assert 'tpEd = new Editor($("#tp-cv"),' in js
    assert "tpPlayer = new Player(" in js
    assert "class Editor" not in js and "class Player" not in js
    assert sorted(p.name for p in STATIC.glob("*.js")) == [
        "app.js",
        "audio.js",
        "editor.js",
        "ripui.js",
    ]


def test_the_page_says_what_it_last_saw_and_asks_again_when_refused() -> None:
    js = (STATIC / "app.js").read_text()
    body = re.search(r"async function saveTapeEdges\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "seen_start: seen.start, seen_end: seen.end" in body
    assert "showTape(await api(`/api/tapes/${tape.id}`))" in body.split("catch (e)")[1]


def test_a_refused_move_puts_the_marker_back_and_says_why() -> None:
    js = (STATIC / "app.js").read_text()
    body = re.search(r"async function saveTapeEdges\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    rest = body.split("catch (e)", 1)[1]
    assert "tpEd.setTracks(tapeTracks(tape))" in rest
    assert "waveSay(`Not moved: ${e.message}`, true)" in rest


def test_the_tapes_page_keeps_the_rip_s_keys_to_itself() -> None:
    """Space on the Tapes page played whatever record the Cut screen had loaded."""
    js = (STATIC / "app.js").read_text()
    assert 'if (document.body.dataset.mode === "tapes") { tapeKey(e); return; }' in js


def test_opening_a_tape_does_not_start_reading_its_audio() -> None:
    js = (STATIC / "app.js").read_text()
    body = re.search(r"function showTapeWave\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "tpPlayer.load" not in body
    assert "tpPlayer.load(url)" in js


def test_a_wobble_on_a_long_timeline_moves_a_marker_by_the_wobble() -> None:
    """The marker jumped to the pointer past the threshold, so a 3 px wobble saved a
    person-set move of 42 s when pressed by a finger at the edge of its reach."""
    js = (STATIC / "editor.js").read_text()
    assert "const off = this.long ? x - this.xOf(at) : 0;" in js
    assert "this.tOf(x - this.drag.off)" in js


def test_only_a_long_timeline_gets_its_grid_its_clock_or_its_care() -> None:
    js = (STATIC / "editor.js").read_text()
    assert "const step = this.long && span > 3600" in js
    assert "this.long ? fmtLong(t) : fmt(t)" in js
    assert "return this.long && (this.t1 - this.t0)" in js
    assert "this.opts" not in js


def test_a_key_with_a_modifier_is_the_browser_s_and_alt_left_is_not_a_nudge() -> None:
    """Alt-left went back a page, and set the selected boundary as the person's too."""
    js = (STATIC / "app.js").read_text()
    body = re.search(r"function tapeKey\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    first = body.index("if (e.altKey || e.ctrlKey || e.metaKey) return;")
    assert first < body.index("nudgeTapeEdge(")


def test_only_a_marker_a_person_pressed_can_be_nudged() -> None:
    """A row selects a song, and an arrow key then set its start for good."""
    js = (STATIC / "app.js").read_text()
    body = re.search(r"function tapeKey\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    guard = body.index("tpPicked?.track !== sel.track")
    assert guard < body.index("nudgeTapeEdge(")
    assert "(num, edge) => tapeSelect(num, edge, false, true)" in js
    assert 'tapeSelect(song.number, "start", true);' in js


def test_a_nudge_says_whose_it_becomes_and_is_flushed_rather_than_dropped() -> None:
    """A pending nudge was a bare timer, lost with the page or sent for another song."""
    js = (STATIC / "app.js").read_text()
    assert "becomes yours when this saves" in js
    assert "tpNudge = { tape: tape.id, song: t.number, seen," in js
    assert 'addEventListener("pagehide", flushNudgeOnLeaving)' in js
    assert "keepalive: true" in js
    for caller in ("async function openTape(", "async function startTapeJob("):
        start = js.index(caller)
        assert "await flushNudge();" in js[start : js.index("\n}", start)]
    assert "if (tpNudge && tpNudge.song !== num) flushNudge();" in js
