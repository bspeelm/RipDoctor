"""The front end, checked the way the Python is.

There is no compiler here, so these stand in for one. The first is the check
that earned its place: three times in one session a string-replace edit silently
failed to match and left the JS reaching for a node that was never added, each
time surfacing as a TypeError on null in front of somebody using it.
"""

from __future__ import annotations

import json
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
    assert re.search(r"header \.tabs[^{]*\{[^}]*flex:none", css), (
        "the tabs can be squeezed off the edge"
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
    assert "#tapeview{display:block;overflow-y:auto;overflow-x:hidden}" in narrow.group(
        1
    )
    assert "#tp-table{display:block;min-width:0}" in narrow.group(1)
    assert "#tp-rows tr[data-song]{display:grid" in narrow.group(1)
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
    assert '"Waiting on the cut."' in js
    assert '"Waits until the songs are filed."' in js
    file_step = re.search(r"function stepFile\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "cut_refusal" not in file_step
    assert 'tpStep("file", "wait", "Waiting", "Waiting on the cut.")' in file_step


def test_a_job_lost_to_a_restart_is_said_plainly_and_a_blip_is_waited_out() -> None:
    """It said "no job" and left the last progress on the page."""
    js = (STATIC / "app.js").read_text()
    body = re.search(r"async function watchTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "if (e.status) throw e;" in body
    assert "e.status === 404" in body and "start it again" in body
    assert "if (tapeWatching) for (const b of TAPE_JOB_BUTTONS)" in js


def test_a_pasted_release_keeps_its_place_in_the_edition_picker() -> None:
    """A release from no candidate list left the picker blank, and Use then sent ''."""
    js = (STATIC / "app.js").read_text()
    assert "[current, ...others]" in js


def test_an_archived_tape_offers_nothing_that_would_change_it() -> None:
    """Find boundaries on one said to upload audio, and every other control refused."""
    js = (STATIC / "app.js").read_text()
    assert "if (t.playlist.done) archivedControls()" in js
    body = re.search(r"function archivedControls\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "#tp-process" in body and '$("#tapepane").querySelectorAll' in body


def test_a_song_in_the_library_or_not_confirmed_offers_no_other_release() -> None:
    """Change it stayed live on these songs, only for the route to refuse it."""
    js = (STATIC / "app.js").read_text()
    held = re.search(r"const CHOICE_HELD = \{.*?\n\};", js, re.S).group(0)  # type: ignore[union-attr]
    assert all(f"{state}:" in held for state in ("filed", "already", "unknown"))
    body = re.search(r"function releaseOptions\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
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
    assert first < body.index("nudgeChosen(")


def test_only_an_edge_a_person_chose_can_be_nudged() -> None:
    """A row selects a song, and an arrow key then set its start for good."""
    js = (STATIC / "app.js").read_text()
    keys = re.search(r"function tapeKey\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "nudgeTapeEdge(" not in keys
    assert js.count("nudgeTapeEdge(") == 2, "one definition and the one guarded call"
    body = re.search(r"function nudgeChosen\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
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
    for caller in ("async function openTape(", "async function postTapeJob("):
        start = js.index(caller)
        assert "await flushNudge();" in js[start : js.index("\n}", start)]
    assert "if (tpNudge && tpNudge.song !== num) flushNudge();" in js


def test_an_archived_tape_is_not_offered_a_judgement_again_by_selecting_a_song() -> (
    None
):
    """A row or N ran the toolbar's own update, which turned Accept and Skip back on."""
    js = (STATIC / "app.js").read_text()
    body = re.search(r"function showTapeTools\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "const done = !!tape?.playlist.done" in body
    for button in ("accept", "skip", "song"):
        assert f'$("#tp-{button}").disabled = done' in body


def test_what_needs_a_waveform_waits_for_one_rather_than_throwing() -> None:
    """With none loaded, these reached into a side that was not there."""
    js = (STATIC / "app.js").read_text()
    for function, guard in (
        ("tapeZoom", "if (!tpEd.side) return;"),
        ("stepDoubt", "if (!tape || !tpEd.side) return;"),
        ("tapeHearSilence", "if (tpEd.side) tpEd.fitRange("),
    ):
        body = re.search(rf"function {function}\(.*?\n\}}", js, re.S).group(0)  # type: ignore[union-attr]
        assert guard in body
    body = re.search(r"function showTapeTools\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert '["#tp-in", "#tp-out", "#tp-whole"]) $(id).disabled = !wave' in body


def tape_markup() -> str:
    page = markup()
    start = page.index('<div id="tapeview"')
    return page[start : page.index('<div id="captureview"', start)]


def test_a_tape_s_songs_are_in_one_table() -> None:
    """The same songs in three tables, each with every control on every row."""
    tape = tape_markup()
    assert tape.count("<table") == 1
    head = re.search(r"<thead>(.*?)</thead>", tape, re.S).group(1)  # type: ignore[union-attr]
    assert re.findall(r"<th>(.*?)</th>", head) == [
        "#",
        "Song",
        "Album",
        "Span",
        "Length",
        "Boundary",
        "Release",
    ]
    for gone in ("tp-ear", "tp-review", "tp-filing", "tapeform"):
        assert f'id="{gone}"' not in markup()


def test_the_pipeline_rail_runs_in_order_with_archive_last() -> None:
    steps = re.findall(r'id="tp-st-(\w+)"', tape_markup())
    assert steps == [
        "recording",
        "list",
        "boundaries",
        "albums",
        "cut",
        "file",
        "playlist",
        "archive",
    ]
    js = (STATIC / "app.js").read_text()
    assert f"const TP_STEPS = {json.dumps(steps).replace(', ', ', ')};" in js


def test_the_toolbar_names_the_key_each_button_answers_to() -> None:
    tape = tape_markup()
    for button, key in (
        ("tp-prev", "K"),
        ("tp-next", "J"),
        ("tp-doubt", "N"),
        ("tp-edge-start", "E"),
        ("tp-edge-end", "E"),
        ("tp-minus", ","),
        ("tp-plus", "."),
        ("tp-play", "Space"),
        ("tp-hear", "A"),
        ("tp-accept", "&crarr;"),
        ("tp-skip", "X"),
        ("tp-song", "Z"),
        ("tp-whole", "Z"),
    ):
        found = re.search(rf'id="{button}"[^>]*>.*?<kbd>(.*?)</kbd>', tape)
        assert found and found.group(1) == key, button
    js = (STATIC / "app.js").read_text()
    keys = re.search(r"const TAPE_KEYS = \{.*?\};", js, re.S).group(0)  # type: ignore[union-attr]
    for pair in (
        'k: "#tp-prev"',
        'j: "#tp-next"',
        'a: "#tp-hear"',
        'Enter: "#tp-accept"',
        'x: "#tp-skip"',
    ):
        assert pair in keys
    assert "#tp-hear" not in keys.replace('a: "#tp-hear"', "")
    assert "s:" not in keys and "h:" not in keys


def test_the_tape_page_keeps_its_looks_in_the_stylesheet() -> None:
    assert "style=" not in tape_markup()
    js = (STATIC / "app.js").read_text()
    start = js.index("// ---- the rail")
    section = js[start : js.index("// ---- the waveform", start)]
    assert ".style." not in section


def test_a_row_s_words_go_through_the_list_route_with_what_it_warns() -> None:
    """Nothing unsaved is held on the page, so a job can no longer save a list on the
    way past."""
    js = (STATIC / "app.js").read_text()
    assert "tapeEdited" not in js
    body = re.search(r"async function saveList\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "await flushNudge();" in body
    assert "e.needsConfirm" in body and "confirm: true" in body
    assert "{ songs, seen }" in body
    assert "tpFail(e)" in body


def test_a_row_says_where_a_filed_song_went_and_the_column_says_what_became_of_it() -> (
    None
):
    js = (STATIC / "app.js").read_text()
    word = re.search(r"function releaseWord\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    for state in ("filed", "already", "failed"):
        assert f'song.filing === "{state}"' in word
    filed = re.search(r"function filedBlock\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "FILED_WORDS[o.state]" in filed and "o.path" in filed
    block = re.search(r"function releaseBlock\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "maybeCopyView(song, rel.maybe_copy)" in block


def test_a_running_job_is_shown_on_its_own_step() -> None:
    js = (STATIC / "app.js").read_text()
    jobs = re.search(r"const JOB_STEP = \{.*?\};", js).group(0)  # type: ignore[union-attr]
    for op in ("tape", "match", "cut", "file", "playlist", "archive"):
        assert f"{op}:" in jobs
    body = re.search(r"async function watchTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "stepBusy(tpJobStep)" in body
    assert "status(doing, true)" in body


def test_a_release_far_from_the_list_is_asked_about_and_sent_again() -> None:
    js = (STATIC / "app.js").read_text()
    assert "e.needsConfirm = !!body?.needs_confirm" in js
    body = re.search(r"async function settleSong\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "e.needsConfirm && onAsk" in body and "confirm(" not in body
    line = re.search(r"function optionLine\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "askView(state.ask.said" in line and "confirm: true" in line
    assert "than your list. Use it anyway?" in js


def test_a_copy_found_at_lookup_says_where_it_is() -> None:
    js = (STATIC / "app.js").read_text()
    assert '"owned") return ["In your library", "good"]' in js
    assert "In your library: ${copy.album_artist} / ${copy.album}" in js
    assert 'el("p", "dim tp-path", copy.path)' in js


def test_a_job_button_waits_for_the_edit_its_click_sent() -> None:
    """A click on Match or Find blurred an edited field, and the list it saved raced the
    job that was starting, which is how an edit's boundaries went with nobody asked."""
    js = (STATIC / "app.js").read_text()
    job = re.search(r"async function startTapeJob\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "await tpListSaved;" in job
    for button in ("processTape", "matchTape"):
        said = re.search(rf"const {button} = .*", js).group(0)  # type: ignore[union-attr]
        assert "startTapeJob(" in said
    send = re.search(r"function sendList\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "tpListSaved = " in send
    assert "saveList(build()" in send


def test_a_file_chosen_over_boundaries_is_asked_about_too() -> None:
    js = (STATIC / "app.js").read_text()
    body = re.search(r"async function sendTapeList\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "e.needsConfirm" in body and "?confirm=1" in body


def test_a_cut_is_not_said_to_wait_on_an_album_and_a_file_is() -> None:
    js = (STATIC / "app.js").read_text()
    waits = re.search(r"function cutWaits\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "settled_by" not in waits
    step = re.search(r"function stepFile\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "!s.settled_by" in step


def test_a_tape_goes_on_by_itself_once_its_audio_and_list_are_both_in() -> None:
    js = (STATIC / "app.js").read_text()
    shown = re.search(r"function showTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "autoPrepare(t);" in shown
    body = re.search(r"function autoPrepare\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "t.prepare_needed" in body and "tapeWatching" in body
    assert "/prepare" in body


def test_a_job_of_several_parts_shows_on_the_rail_step_it_is_in() -> None:
    js = (STATIC / "app.js").read_text()
    assert "JOB_STEP[j.phase] ?? JOB_STEP[j.op]" in js


def test_an_option_is_one_line_of_columns_with_no_edition_jargon() -> None:
    """A release was a sentence of format, country and status, four to a row."""
    js = (STATIC / "app.js").read_text()
    line = re.search(r"function optionLine\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    for column in ("album", "year", "kind", "place", "off", "lib"):
        assert f"tp-opt-{column}" in line
    assert "editionText" not in js and "o.status" not in line
    assert "const SHOWN_OPTIONS = 4;" in js
    assert "state.all = true" in js


def test_a_pasted_id_is_out_of_sight_until_asked_for() -> None:
    js = (STATIC / "app.js").read_text()
    body = re.search(r"function releaseOptions\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert '"Use a release id"' in body
    assert "if (state.byId) change.append(byIdForm(song))" in body
    assert "searchForm(song)" in body


def test_a_row_is_searched_from_its_own_words_and_settles_nothing() -> None:
    js = (STATIC / "app.js").read_text()
    form = re.search(r"function searchForm\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "state.title ?? song.title" in form and "state.artist ?? song.artist" in form
    run = re.search(r"async function runSearch\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "/search`, { title, artist }" in run
    assert "showTape(await api(" in run and "settleSong" not in run


def test_a_copy_that_might_be_this_song_is_one_click_away() -> None:
    js = (STATIC / "app.js").read_text()
    body = re.search(r"function maybeCopyView\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "This might be your copy at ${path}." in body and "useCopyButton(" in body


def test_an_edition_note_is_said_on_the_row_and_offers_the_single() -> None:
    js = (STATIC / "app.js").read_text()
    body = re.search(r"function editionNoteView\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "noteBox(note.text)" in body and "note.choices" in body
    assert 'c.current ? "tp-choice on" : "tp-choice"' in body
    word = re.search(r"function releaseWord\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert 'rel?.edition_note ? "warn" : null' in word


def test_a_list_that_loses_boundaries_is_asked_about_in_its_row() -> None:
    js = (STATIC / "app.js").read_text()
    body = re.search(r"async function saveList\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "tpListAsk = {" in body and "confirm(" not in body
    opened = re.search(r"function songOpenView\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "listAskView()" in opened
    drafted = re.search(r"function draftRowView\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "listAskView()" in drafted


def test_a_tape_opens_on_the_song_that_needs_its_person() -> None:
    js = (STATIC / "app.js").read_text()
    body = re.search(r"function landOn\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "r.doubtful" in body and "!s.settled_by" in body
    assert 'tapeSelect(doubt ?? open ?? t.songs[0].number, "start", false)' in body
    shown = re.search(r"function showTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "landOn(t);" in shown


def test_a_field_being_typed_in_holds_the_lookup_back() -> None:
    """Tabbing from an edited title started the job, and the artist was then refused."""
    js = (STATIC / "app.js").read_text()
    body = re.search(r"function autoPrepare\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert 'closest?.("#tp-rows")' in body and "setTimeout" in body


def test_a_tape_shown_after_an_archived_one_gets_its_controls_back() -> None:
    """Every control on the page stayed off, so the next tape could not take a file."""
    js = (STATIC / "app.js").read_text()
    shown = re.search(r"function showTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    on = shown.index('querySelectorAll("input, button")) c.disabled = false')
    assert on < shown.index("showRail(t);") < shown.index("archivedControls()")


def test_a_filed_song_with_no_cover_offers_the_one_action() -> None:
    js = (STATIC / "app.js").read_text()
    filed = re.search(r"function filedBlock\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "if (o.no_cover) box.append(noCoverView(song))" in filed
    body = re.search(r"function noCoverView\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert '"No cover found."' in body and '"Upload an image"' in body
    upload = re.search(r"async function uploadCover\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "/songs/${number}/cover`" in upload


def test_the_playlist_step_claims_the_file_and_not_the_music_server() -> None:
    js = (STATIC / "app.js").read_text()
    step = re.search(r"function stepPlaylist\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert '"File written"' in step and "p.path.split" in step
    assert "imports it within about a minute" in step
    assert "may need to make imported playlists public" in step
    assert '"Written"' not in step and "visible" not in step


def test_a_line_of_options_sends_the_recording_it_shows() -> None:
    js = (STATIC / "app.js").read_text()
    line = re.search(r"function optionLine\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "{ recording: o.recording }" in line
    assert "chooseRelease(song, o.release, o, mine)" in line
    assert "{ recording: c.recording }" in js


def test_a_note_choice_said_twice_still_sends_its_recording() -> None:
    """The second sending named the release alone, which takes the first recording."""
    js = (STATIC / "app.js").read_text()
    assert "{ release: asked.release, recording: asked.recording, ...extra" in js


def test_find_again_is_not_offered_while_it_would_be_refused() -> None:
    source = (STATIC / "app.js").read_text()
    assert "!canFind || !!t.place_refusal" in source


def test_a_second_field_saved_quickly_is_built_from_what_the_first_left() -> None:
    """A list built when the field was left was stale by the time the one before it had
    answered, and would be refused."""
    js = (STATIC / "app.js").read_text()
    send = re.search(r"function sendList\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "tpListSaved.then(() => saveList(build()" in send
    field = re.search(r"function editField\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "sendList(() => {" in field
    assert "listNow()" in field.split("sendList(() => {")[1]


def test_a_stale_list_loads_the_tape_as_it_stands_and_says_why() -> None:
    js = (STATIC / "app.js").read_text()
    body = re.search(r"async function saveList\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "if (e.stale)" in body
    assert "await reloadTape();" in body


def test_a_redraw_took_typed_text_and_focus_and_now_puts_both_back() -> None:
    js = (STATIC / "app.js").read_text()
    shown = re.search(r"function showSongs\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert shown.index("typedInRows()") < shown.index("replaceChildren(...rows)")
    assert shown.index("replaceChildren(...rows)") < shown.index("putBackTyped(typed)")
    typed = re.search(r"function typedInRows\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "input.value !== input.dataset.saved" in typed
    assert "focused" in typed
    back = re.search(r"function putBackTyped\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    for part in ("input.value = was.value", "input.focus()", "setSelectionRange"):
        assert part in back


def test_a_field_that_was_sent_is_not_kept_as_typed_if_its_save_is_refused() -> None:
    js = (STATIC / "app.js").read_text()
    field = re.search(r"function editField\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "input.dataset.saved = value" in field


def test_cut_pressed_after_an_edit_got_a_409_because_a_lookup_started_first() -> None:
    js = (STATIC / "app.js").read_text()
    auto = re.search(r"function autoPrepare\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "if (tpActing ||" in auto
    assert "postTapeJob(" in auto and "startTapeJob(" not in auto
    start = re.search(r"async function startTapeJob\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "tpActing += 1;" in start and "tpActing -= 1" in start
    send = re.search(r"function sendList\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "tpActing += 1;" in send and "tpActing -= 1" in send


def test_cut_waits_behind_an_automatic_lookup_instead_of_being_turned_off() -> None:
    js = (STATIC / "app.js").read_text()
    cut = re.search(r"function cutTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "cutWouldWait(tape)" in cut and "tpCutWaits = true" in cut
    shown = re.search(r"function showTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert 'if (cutWouldWait(t)) $("#tp-cut").disabled = false;' in shown
    watched = re.search(r"async function watchTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert watched.index("await loadTapes(id)") < watched.index("cutAfterLookup(id)")
    assert "Cuts as soon as the lookup finishes" in js


def test_a_failed_run_is_said_after_a_restart_from_what_the_tape_keeps() -> None:
    js = (STATIC / "app.js").read_text()
    opened = re.search(r"async function openTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "tape.prepare_failed" in opened


def test_a_song_that_needs_no_cut_is_not_counted_or_called_uncut() -> None:
    js = (STATIC / "app.js").read_text()
    step = re.search(r"function stepCut\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert 'r.cut !== "not needed"' in step
    assert '"not needed"].includes(cutSaid)' in js


def test_a_question_about_a_list_could_be_answered_from_a_row_that_never_asked_it() -> (
    None
):
    """It showed on whichever row was open, with Go ahead sending a list nobody there
    had made."""
    js = (STATIC / "app.js").read_text()
    body = re.search(r"async function saveList\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "row: tpOpen" in body
    added = re.search(r"async function addDraft\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert 'tpListAsk.row = "draft"' in added
    opened = re.search(r"function songOpenView\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "tpListAsk?.row === song.number" in opened
    picked = re.search(r"function tapeSelect\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "if (opening) tpListAsk = null;" in picked


def test_two_markers_on_one_spot_could_not_be_told_apart_by_a_long_timeline() -> None:
    js = (STATIC / "editor.js").read_text()
    hit = re.search(r"\n  hit\(x, y.*?\n  \}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "best && this.long ? this._tieBroken(x, reach, best) : best" in hit
    broken = re.search(r"\n  _tieBroken\(.*?\n  \}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "this.sel?.track" in broken
    assert 'x > n.at ? "start" : "end"' in broken


def test_a_move_says_nothing_of_another_song_because_none_moved() -> None:
    js = (STATIC / "app.js").read_text()
    said = re.search(r"function moveSaid\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "Laid around it" not in said and "No longer accepted" not in said


def test_with_the_server_gone_the_page_kept_saying_working_and_now_says_so() -> None:
    js = (STATIC / "app.js").read_text()
    body = re.search(r"async function watchTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "if (++missed >= TRIES) serverGone();" in body
    assert "throw e;" in body.split("serverGone")[0].split("catch (e)")[1]
    assert "if (missed >= TRIES && tpJobStep) stepBusy(tpJobStep);" in body
    gone = re.search(r"function serverGone\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "can't be reached" in gone and "No contact" in gone


def test_keeping_on_asking_for_a_job_sends_nothing_but_the_question() -> None:
    js = (STATIC / "app.js").read_text()
    body = re.search(r"async function watchTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "postJSON" not in body


def test_the_page_never_restarts_a_tape_whose_job_ended_in_error() -> None:
    """Whatever the document says: if its failure could not be saved, the document still
    asks for a run, and every look at the page started another."""
    js = (STATIC / "app.js").read_text()
    body = re.search(r"function autoPrepare\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "tpStopped.has(t.id)" in body
    watched = re.search(r"async function watchTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "if (j.error) tpStopped.add(id); else tpStopped.delete(id);" in watched
    saved = re.search(r"async function saveList\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "tpStopped.delete(tape.id);" in saved


def test_the_rail_said_file_ready_beside_a_row_saying_not_confirmed() -> None:
    js = (STATIC / "app.js").read_text()
    word = re.search(r"function releaseWord\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert '"Not confirmed"' in word and "song.import_begun" in word
    step = re.search(r"function stepFile\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "s.import_begun" in step
    assert step.index("unsure.length) return tpStep") < step.index('"Ready"')


def test_a_redraw_saved_half_typed_text_because_a_removed_input_counts_as_left() -> (
    None
):
    """Partial text went to the server, past the job's end, and cleared its lookup."""
    js = (STATIC / "app.js").read_text()
    shown = re.search(r"function showSongs\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    on = shown.index("tpRedrawing = true;")
    assert (
        on
        < shown.index("replaceChildren(...rows)")
        < shown.index("tpRedrawing = false;")
    )
    left = re.search(r"function leftField\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "!tpRedrawing && input.value !== input.dataset.saved" in left
    opened = re.search(r"function songOpenView\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "input.onblur = () => leftField(key, input)" in opened
    assert "onchange" not in opened
    edge = re.search(r"function edgeInput\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "tpRedrawing" in edge and "onchange" not in edge


def test_a_save_that_could_not_be_asked_yet_stays_on_the_row_and_goes_again() -> None:
    """A refused save left unsaved text on the screen with no way to send it again."""
    js = (STATIC / "app.js").read_text()
    field = re.search(r"async function editField\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "tpUnsent.set(held, value)" in field
    assert "if (!saved && tpHeldBack)" in field
    assert field.index("tpHeldBack") < field.index("tpUnsent.delete(held)")
    body = re.search(r"async function saveList\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "tpHeldBack = e.busy || !e.status;" in body
    watched = re.search(r"async function watchTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "sendUnsent();" in watched
    assert "e.busy = !!body?.busy;" in js


def test_an_edit_behind_a_move_landed_on_the_row_that_arrived_at_its_old_place() -> (
    None
):
    """With One, Two, Three: up on Three, then an edit to its title, gave
    [One, Three, Three (edit)], and Two was gone."""
    js = (STATIC / "app.js").read_text()
    field = re.search(r"async function editField\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "songs.find((s) => s.key === key)" in field
    assert "song.number" not in field
    move = re.search(r"function moveSong\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "songs.findIndex((s) => s.key === key)" in move
    assert "song.number" not in move
    body = re.search(r"async function saveList\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "tpKeys = rows.map((r) => r.key);" in body
    assert "rows.map(({ key, ...song }) => song)" in body


def test_what_a_row_presses_goes_after_a_save_and_to_the_place_its_song_is_now() -> (
    None
):
    js = (STATIC / "app.js").read_text()
    settle = re.search(r"async function settleSong\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "rowTurn(" in settle
    for name in ("tapeAccept", "tapeSkip", "chooseRelease"):
        body = re.search(rf"function {name}\(.*?\n\}}", js, re.S).group(0)  # type: ignore[union-attr]
        assert "numberOf(key)" in body and "song.number}/" not in body


def test_an_older_answer_does_not_undo_a_save_beside_a_lookup() -> None:
    js = (STATIC / "app.js").read_text()
    watched = re.search(r"async function watchTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "if (saves === tpSaves && !tpSaving) showTape(fresh);" in watched
    assert "tpEd.frozen = tapeHeld()" in watched


def test_a_far_option_asked_its_question_inside_a_section_that_had_closed() -> None:
    """On a settled song a click on it collapsed Change the album, question and all."""
    js = (STATIC / "app.js").read_text()
    choose = re.search(r"function chooseRelease\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert choose.index("state.open = false") < choose.index("state.open = true")
    assert choose.index("state.ask = {") < choose.index("state.open = true")


def test_nothing_was_said_of_a_lost_connection_until_a_press_gave_failed_to_fetch() -> (
    None
):
    """And the raw words stayed on the page after the server came back."""
    js = (STATIC / "app.js").read_text()
    fail = re.search(r"function tpFail\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "if (e.status)" in fail and "lostContact()" in fail
    start = js.index("// ---" + "-" * 65 + " tapes")
    tapes = js[start : js.index("// ---" + "-" * 65 + " modes", start)]
    assert tapes.count('$("#tp-err").textContent = e.message;') == 1
    assert '$("#tp-err").textContent = e.message;' in fail
    beat = re.search(r"async function heartbeat\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert 'document.body.dataset.mode !== "tapes"' in beat
    assert "if (tpGone) regained();" in beat
    assert "setInterval(heartbeat" in js
    back = re.search(r"function regained\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert '$("#tp-gone").hidden = true;' in back
    assert "reloadTape().then(sendUnsent)" in back
    assert 'id="tp-gone"' in tape_markup()


def test_a_job_forgotten_after_half_an_hour_was_called_a_restart() -> None:
    js = (STATIC / "app.js").read_text()
    body = re.search(r"async function watchTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "half an hour" in body
    assert "so the server has restarted" not in body


def test_a_hundred_songs_with_no_match_have_one_action_and_one_question() -> None:
    js = (STATIC / "app.js").read_text()
    step = re.search(r"function stepAlbums\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert 'tpAction("#tp-all-list", ready.length > 0' in step
    assert "t.from_list.numbers" in step
    ask = re.search(r"function showAllAsk\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "askView(said" in ask and "no match at all" in ask
    assert "stay as they are" in ask and "with no album" in ask
    assert "/from-list`, { numbers: ready }" in ask
    assert 'id="tp-all-list"' in tape_markup()


def test_a_click_on_the_open_row_closed_it_and_closing_has_its_own_button() -> None:
    js = (STATIC / "app.js").read_text()
    row = re.search(r"function songRowView\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "closing" not in row and "tpOpen = null" not in row
    opened = re.search(r"function songOpenView\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert 'smallButton("Close"' in opened


def test_file_from_my_list_was_faint_on_a_row_where_it_was_the_way_forward() -> None:
    js = (STATIC / "app.js").read_text()
    options = re.search(r"function releaseOptions\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert 'wayForward ? "tiny" : "tp-quiet"' in options
    css = (STATIC / "style.css").read_text()
    primary = re.search(r"\.tool-group button\.primary[^{]*\{([^}]*)\}", css).group(1)  # type: ignore[union-attr]
    assert "var(--accent)" in primary


def test_a_reason_is_on_its_step_and_a_step_waiting_keeps_its_button() -> None:
    """Find again's reason was a tooltip, and File vanished while it waited."""
    js = (STATIC / "app.js").read_text()
    step = re.search(r"function stepBoundaries\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert ".title" not in step and "t.place_refusal) return tpStep" in step
    css = (STATIC / "style.css").read_text()
    assert ".step[data-kind=wait] .step-acts{display:none}" not in css
    playlist = re.search(r"function stepPlaylist\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert '"File written", name,' in playlist


def test_a_copy_that_might_be_this_song_says_its_path_once() -> None:
    js = (STATIC / "app.js").read_text()
    block = re.search(r"function releaseBlock\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "song.settled_by || nothing || rel?.maybe_copy" in block


def test_a_job_s_error_stayed_after_what_it_said_stopped_being_true() -> None:
    js = (STATIC / "app.js").read_text()
    stands = re.search(r"function jobErrorStands\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "t.all_cut" in stands and "t.all_filed" in stands
    shown = re.search(r"function showTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "tpErrIsJobs && !jobErrorStands(t)" in shown


def test_a_title_was_cut_off_beside_its_artist_while_the_album_column_had_room() -> (
    None
):
    css = (STATIC / "style.css").read_text()
    who = re.search(r"\.tp-who\{([^}]*)\}", css).group(1)  # type: ignore[union-attr]
    assert "flex-direction:column" in who
    span = re.search(r"#tp-table \.c-span\{width:(\d+)rem\}", css)
    assert span and int(span.group(1)) >= 9, "h:mm:ss both ways needs this much"


def test_an_opened_row_was_clipped_under_the_table_s_head_and_past_its_foot() -> None:
    js = (STATIC / "app.js").read_text()
    picked = re.search(r"function tapeSelect\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "rowIntoView(num);" in picked and "scrollIntoView" not in picked
    view = re.search(r"function rowIntoView\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert '$("#tp-table thead").offsetHeight' in view
    assert ".tp-open" in view


def test_a_long_title_in_the_readout_cost_the_table_a_line() -> None:
    css = (STATIC / "style.css").read_text()
    assert "#tp-readout > #tp-ro-song{flex:0 1 auto;min-width:0" in css


def test_what_a_person_saw_of_their_empty_error_line_took_table_height() -> None:
    css = (STATIC / "style.css").read_text()
    msg = re.search(r"#tp-msg\{([^}]*)\}", css).group(1)  # type: ignore[union-attr]
    assert "min-height" not in msg
    assert "#tp-msg p:empty{display:none}" in css


def test_opening_another_tape_left_the_first_one_s_job_on_its_rail() -> None:
    js = (STATIC / "app.js").read_text()
    body = re.search(r"async function watchTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "if (tape?.id !== id) break;" in body
    after = body.split("stepIdle();")[1]
    assert after.index("tape?.id !== id") < after.index("await loadTapes(id)")


def test_the_next_thing_to_press_could_be_off_the_end_of_the_rail() -> None:
    js = (STATIC / "app.js").read_text()
    shown = re.search(r"function showTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "showNext();" in shown
    assert shown.index("archivedControls()") < shown.index("showNext();")
    now = re.search(r"function stepNow\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert '.dataset.kind !== "done"' in now
    assert '!b.classList.contains("ghost")' in now
    nxt = re.search(r"function showNext\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "act.onclick = () => now?.button?.click();" in nxt
    assert 'box.scrollIntoView({ block: "nearest" })' in nxt
    assert 'id="tp-next-act"' in tape_markup()


def test_on_a_phone_the_rail_was_a_long_way_above_a_doubtful_song() -> None:
    """1,361 px of it, with nothing pointing to it. It is one line now, and stays."""
    assert 'id="tp-rail-bar"' in tape_markup()
    js = (STATIC / "app.js").read_text()
    assert 'railOpens($("#tp-rail"), $("#tp-rail-bar"))' in js
    assert 'rail.classList.toggle("open")' in js
    css = (STATIC / "style.css").read_text()
    narrow = re.search(r"@media \(max-width:48rem\)\{(.*?)\n\}", css, re.S).group(1)  # type: ignore[union-attr]
    assert ".rail:not(.open) > :not(.rail-bar){display:none}" in narrow
    assert ".rail:not(.open){position:sticky;top:0" in narrow


def test_a_finger_needs_forty_pixels_on_the_toolbar_and_a_row() -> None:
    css = (STATIC / "style.css").read_text()
    narrow = re.search(r"@media \(max-width:48rem\)\{(.*?)\n\}", css, re.S).group(1)  # type: ignore[union-attr]
    assert ".tool-group button{min-height:40px;min-width:40px" in narrow
    for row_button in (".tp-moves button", ".tp-rel-quiet button", ".tp-ask button"):
        assert row_button in narrow
    assert "min-height:40px;min-width:40px}" in narrow


def test_a_marker_s_tag_says_what_it_stands_for_and_a_time_stays_readable() -> None:
    js = (STATIC / "editor.js").read_text()
    assert "if (this.long) cv.title = this._tagAt(x, y);" in js
    tag = re.search(r"\n  _tagAt\(.*?\n  \}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "t.title" in tag and "edge" in tag
    assert js.index("this._markers(c, L);") < js.index("this._timeLabels(c, L);")
    labels = re.search(r"\n  _timeLabels\(.*?\n  \}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "if (!this.long) return;" in labels
    assert "if (this.long) this.labels.push({ label, x });" in js


def test_the_header_s_button_went_on_offering_what_a_job_just_started_refuses() -> None:
    js = (STATIC / "app.js").read_text()
    body = re.search(r"async function watchTape\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert '$("#tp-next-act").hidden = true;' in body
    nxt = re.search(r"function showNext\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "tapeWatching && !cutWouldWait(tape)" in nxt


def test_text_waiting_on_a_job_was_also_called_unchanged_in_red() -> None:
    js = (STATIC / "app.js").read_text()
    field = re.search(r"async function editField\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    held = field.split("if (!saved && tpHeldBack)")[1].split("return;")[0]
    assert '$("#tp-err").textContent = "";' in held


def test_a_row_s_other_ways_forward_are_quiet_and_still_readable() -> None:
    css = (STATIC / "style.css").read_text()
    assert ".tp-rel-quiet button.tp-quiet{color:var(--dim)}" in css


def cut_markup() -> str:
    page = markup()
    start = page.index('<div id="cutview"')
    return page[start : page.index('<dialog id="helpdlg"', start)]


def test_the_record_s_rail_runs_in_the_order_the_work_does() -> None:
    steps = re.findall(r'id="cut-st-(\w+)"', cut_markup())
    assert steps == ["rip", "firstpass", "check", "save", "cut", "import", "archive"]
    js = (STATIC / "app.js").read_text()
    assert f"const CUT_STEPS = {json.dumps(steps)};" in js


@pytest.mark.parametrize(
    ("step", "buttons"),
    [
        ("firstpass", ["firstpass", "realign"]),
        ("check", ["cut-review"]),
        ("save", ["save", "revert"]),
        ("cut", ["split"]),
        ("import", ["import"]),
        ("archive", ["archive"]),
    ],
)
def test_a_moved_button_keeps_its_id_on_its_step(step: str, buttons: list[str]) -> None:
    page = cut_markup()
    start = page.index(f'id="cut-st-{step}"')
    box = page[start : page.index("</section>", start)]
    assert re.findall(r'<button type="button" id="([\w-]+)"', box) == buttons


def test_the_header_is_the_tabs_and_the_record_picker() -> None:
    page = markup()
    head = page[page.index("<header>") : page.index("</header>")]
    assert re.findall(r'id="(album-q|album)"', head) == ["album-q", "album"]
    for moved in (
        "firstpass",
        "realign",
        "revert",
        "save",
        "split",
        "import",
        "archive",
    ):
        assert f'id="{moved}"' not in head
    assert 'id="pipeline"' not in page
    assert "renderPipeline" not in (STATIC / "app.js").read_text()


def test_cut_saves_for_itself_and_no_rail_state_waits_on_a_save() -> None:
    """Asking first is how three nudged boundaries once cut the old ones.

    A track still to check does stop it, which is a different thing. ADR-064."""
    js = (STATIC / "app.js").read_text()
    split = re.search(r"async function split\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "if (S.dirty && !(await save())) return;" in split
    step = re.search(r"function railCut\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    disabled = re.search(r'\$\("#split"\)\.disabled = (.*);', step).group(1)  # type: ignore[union-attr]
    assert "dirty" not in disabled


def test_the_rail_says_why_the_archive_gate_is_shut() -> None:
    js = (STATIC / "app.js").read_text()
    step = re.search(r"function railArchive\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "f.arc?.why" in step


def test_a_catalogue_placed_track_offers_review_and_the_rail_waits_on_it() -> None:
    js = (STATIC / "app.js").read_text()
    assert "forced: tracks.filter((t) => t.forced)" in js
    assert '$("#cut-review").onclick = reviewNext' in js
    step = re.search(r"function railCut\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "|| !!f.forced.length;" in step
    assert "Waiting on ${f.forced.length} to check." in step
    assert step.index("f.forced.length && !f.imported") < step.index("if (f.review)")


def test_every_page_keeps_the_status_line_and_the_console() -> None:
    """They live in Cut's column, and the other pages write to them too."""
    js = (STATIC / "app.js").read_text()
    place = re.search(r"function placeShared\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert '$("#cut-msg").append($("#status"))' in place
    assert '$("#tp-head").before($("#status"))' in place
    assert '$("#app").append($("#logwrap"))' in place
    assert "placeShared(mode);" in js


def test_the_waveform_stays_in_view_while_the_tables_scroll() -> None:
    page = cut_markup()
    assert page.index('id="cv"') < page.index('id="cut-scroll"')
    assert page.index('id="status"') < page.index('id="cut-scroll"')
    assert page.index('id="cut-scroll"') < page.index('id="tracks"')
    css = (STATIC / "style.css").read_text()
    scroller = re.search(r"#cut-scroll\{([^}]*)\}", css).group(1)  # type: ignore[union-attr]
    assert "overflow-y:auto" in scroller
    canvas = re.search(r"#cv\{([^}]*)\}", css).group(1)  # type: ignore[union-attr]
    assert "height:clamp(" in canvas and "flex:1" not in canvas
    js = (STATIC / "app.js").read_text()
    assert 'new ResizeObserver(() => ed.render()).observe($("#cv"))' in js


def test_the_record_s_toolbar_names_the_key_each_button_answers_to() -> None:
    page = cut_markup()
    for button, key in (
        ("cut-prev", "K"),
        ("cut-next", "J"),
        ("cut-edge-start", "E"),
        ("cut-edge-end", "E"),
        ("cut-minus", "&larr;"),
        ("cut-plus", "&rarr;"),
        ("cut-play", "Space"),
        ("cut-audition", "A"),
        ("cut-checked", "&crarr;"),
        ("cut-split", "S"),
        ("cut-zoom-track", "Z"),
        ("cut-zoom-side", "F"),
    ):
        found = re.search(rf'id="{button}"[^>]*>.*?<kbd>(.*?)</kbd>', page)
        assert found and found.group(1) == key, button


def test_the_keys_and_the_toolbar_do_one_thing_each() -> None:
    js = (STATIC / "app.js").read_text()
    for action in ("playOrPause();", "auditionSelected();", "nudgeSelected(("):
        assert action in js.split('addEventListener("keydown"', 1)[1]
    assert '$("#cut-audition").onclick = auditionSelected' in js
    assert '$("#cut-play").onclick = playOrPause' in js
    assert '$("#cut-split").onclick = splitAtPlayhead' in js
    assert "player.audition(" in re.search(  # type: ignore[union-attr]
        r"function auditionSelected\(.*?\n\}", js, re.S
    ).group(0)


def test_a_track_placed_from_the_catalogue_is_marked_on_its_row() -> None:
    js = (STATIC / "app.js").read_text()
    body = re.search(r"function trackRowView\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "trackBoundary(t)" in body
    assert "zero by" in body, "the delta on such a row says nothing, and says so"
    word = re.search(r"function trackBoundary\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert '["From catalogue · check", "warn"]' in word


def test_the_album_panel_holds_the_finished_tracks_beside_the_table() -> None:
    page = cut_markup()
    panel = page[page.index('id="album-panel"') : page.index('id="logwrap"')]
    for field in ("md-artist", "md-album", "md-date", "art-upload", "review"):
        assert f'id="{field}"' in panel
    table = page[page.index('id="cut-table"') : page.index('id="album-panel"')]
    for button in ("addtrack", "splittrack"):
        assert f'id="{button}"' in table
    assert 'id="side-panel"' not in page


def test_the_console_keeps_its_clear_button() -> None:
    page = cut_markup()
    log = page[page.index('id="logwrap"') :]
    assert 'id="logclear"' in log and 'id="logtoggle"' in log and 'id="log"' in log


def test_the_record_s_page_fits_a_phone_the_way_a_tape_s_does() -> None:
    css = (STATIC / "style.css").read_text()
    narrow = re.search(r"@media \(max-width:48rem\)\{(.*?)\n\}", css, re.S).group(1)  # type: ignore[union-attr]
    assert "#cutview{display:block;overflow-y:auto;overflow-x:hidden}" in narrow
    assert "#lower{grid-template-columns:minmax(0,1fr)}" in narrow
    assert "#tracks tbody tr{display:grid" in narrow
    assert 'id="cut-rail-bar"' in cut_markup()
    assert (
        'railOpens($("#cut-rail"), $("#cut-rail-bar"))'
        in (STATIC / "app.js").read_text()
    )


def test_the_boundary_column_says_who_settled_a_track() -> None:
    js = (STATIC / "app.js").read_text()
    assert "Not flagged" not in js
    assert 'hand: ["Set by you", "good"], ear: ["Checked by ear", "good"]' in js
    assert '["Fitted to gap", "good"]' in js


def test_every_way_of_moving_a_boundary_marks_it_set_by_the_person() -> None:
    js = (STATIC / "app.js").read_text()
    helper = re.search(r"function setByHand\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "t.forced = false" in helper and 't.settled = "hand"' in helper
    for function in ("nudgeSelected", "splitAtPlayhead"):
        body = re.search(rf"function {function}\(.*?\n\}}", js, re.S).group(0)  # type: ignore[union-attr]
        assert "setByHand(t)" in body, function
    drag = js[js.index('ed = new Editor($("#cv")') :]
    assert "setByHand(tr)" in drag[: drag.index("player = new Player")]
    checked = re.search(r"function checkSelected\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "t.forced = false" not in js.replace(helper, "").replace(checked, "")
    assert "tr.forced = false" not in js


def test_the_first_pass_no_longer_warns_it_replaces_what_a_person_corrected() -> None:
    js = (STATIC / "app.js").read_text()
    asked = re.search(r"function openFirstPass\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "already corrected" not in asked
    assert "Boundaries you set or checked stay where they are" in asked


def test_checked_keeps_the_boundaries_and_only_a_catalogue_placed_track_offers_it() -> (
    None
):
    js = (STATIC / "app.js").read_text()
    body = re.search(r"function checkSelected\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "if (!t?.forced) return;" in body
    assert "t.forced = false" in body and 't.settled = "ear"' in body
    assert "markDirty()" in body, "it waits for a save like any other edit"
    assert "t.start" not in body and "t.end" not in body
    tools = re.search(r"function showCutTools\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "const doubt = !!selectedTrack()?.forced;" in tools
    assert '$("#cut-checked").disabled = !doubt;' in tools
    assert '$("#cut-checked").onclick = checkSelected' in js


def key_listener() -> str:
    js = (STATIC / "app.js").read_text()
    return js[js.index('addEventListener("keydown", (e) => {') :]


def test_a_browser_shortcut_is_not_taken_for_the_record_s_keys() -> None:
    """Ctrl-S split a track and alt-left nudged a marker while going back."""
    keys = key_listener()
    guard = keys.index("if (e.altKey || e.ctrlKey || e.metaKey) return;")
    assert guard < keys.index("playOrPause();")


def test_the_record_s_keys_did_not_stay_on_the_other_pages() -> None:
    """S on the Rip page split a track in an editor nobody could see."""
    keys = key_listener()
    scoped = keys.index('!== "cut") return;')
    assert scoped < keys.index("playOrPause();")
    assert keys.index('if (e.key === "?")') < scoped


def test_enter_on_a_control_presses_it_only_if_the_keyboard_went_there() -> None:
    """Asked with `:focus-visible`, Chromium said yes for the key being pressed, and
    Enter after a mouse press on Next moved on a track instead of checking this one."""
    js = (STATIC / "app.js").read_text()
    body = re.search(r"function enterIsTheControls\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "if (control !== pressedControl) return true;" in body
    assert "e.preventDefault();" in body and "control.blur();" in body
    keys = key_listener()
    assert keys.index("enterIsTheControls(e)") < keys.index("tapeKey(e)")


def test_selecting_a_track_does_not_arm_a_marker_for_the_arrow_keys() -> None:
    """A row selection on Tapes let an arrow key set a boundary for good."""
    js = (STATIC / "app.js").read_text()
    nudge = re.search(r"function nudgeSelected\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert nudge.index("if (!cutArmed())") < nudge.index("setByHand(t)")
    picks = re.search(r"function select\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "picked = false" in picks
    assert "cutPicked = picked && num != null" in picks
    review = re.search(r"async function reviewNext\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert 'select(t.number, "start");' in review
    step = re.search(r"function stepTrack\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert 'select(next.number, "start");' in step


def test_a_marker_is_armed_only_when_it_was_asked_for() -> None:
    js = (STATIC / "app.js").read_text()
    assert "select(num, edge, true);" in js[js.index('ed = new Editor($("#cv")') :]
    assert "select(t.number, edge, true);" in js
    choose = re.search(r"function chooseEdge\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "select(ed.sel.track, edge, true)" in choose
    assert 'chooseEdge("start")' in js and 'chooseEdge("end")' in js
    assert "swapEdge();" in key_listener()
    tools = re.search(r"function showCutTools\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "$(id).disabled = !armed;" in tools


def test_the_readout_says_which_marker_is_chosen() -> None:
    assert 'id="ro-armed"' in cut_markup()
    js = (STATIC / "app.js").read_text()
    body = re.search(r"function updateReadout\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "chosen`" in body and "#ro-armed" in body


def test_the_keys_are_the_same_on_a_record_and_a_tape() -> None:
    js = (STATIC / "app.js").read_text()
    keys = key_listener()
    for branch, does in (
        ('e.key === "a"', "auditionSelected();"),
        ('e.key === "Enter"', '$("#cut-checked").click();'),
        ('e.key === "s"', "splitAtPlayhead();"),
        ('e.key === "j"', '$("#cut-next").click();'),
        ('e.key === "k"', '$("#cut-prev").click();'),
    ):
        assert keys.index(branch) < keys.index(does), branch
    assert js.index("function tapeKey(") < js.index("function wireTapeWave(")


def test_the_help_dialog_tells_the_truth_about_the_keys() -> None:
    page = (STATIC / "index.html").read_text()
    help_ = page[
        page.index('id="helpdlg"') : page.index("</dialog>", page.index('id="helpdlg"'))
    ]
    assert "<kbd>H</kbd>" not in help_
    assert "<kbd>X</kbd></td><td>on a tape, skip the song" in help_
    assert "<kbd>S</kbd></td><td>on a record, split" in help_
    assert "<kbd>&crarr;</kbd></td><td>on a record" in help_
    assert "<kbd>E</kbd></td><td>choose the start or end" in help_
    assert "<kbd>J</kbd> <kbd>K</kbd></td><td>the next track or song" in help_
    assert "Selecting a track chooses nothing" in help_


def cut_table_code() -> str:
    js = (STATIC / "app.js").read_text()
    return js[
        js.index("function trackRowView(") : js.index("function wireTrackPresses(")
    ]


def test_a_track_s_row_is_read_only_and_what_edits_it_opens_beneath() -> None:
    js = (STATIC / "app.js").read_text()
    row = re.search(r"function trackRowView\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "input" not in row.lower() and "button" not in row.lower()
    opened = re.search(r"function trackOpenView\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    for part in (
        '"Title"',
        '"Start (s)"',
        '"End (s)"',
        '"Remove"',
        '"Close"',
        '"Number"',
    ):
        assert part in opened, part
    assert "c-del" not in (STATIC / "index.html").read_text()
    assert "c-del" not in (STATIC / "style.css").read_text()


def test_pressing_an_open_row_leaves_it_open() -> None:
    js = (STATIC / "app.js").read_text()
    press = re.search(r"function pressRow\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "if (cutOpen === t && ed.sel?.track === t.number) return;" in press
    assert 'select(t.number, "start");' in press, "a row press arms no marker"


def test_a_redraw_of_the_table_keeps_what_is_being_typed() -> None:
    js = (STATIC / "app.js").read_text()
    shown = re.search(r"function renderTracks\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    order = [
        "typedInRows(tb)",
        "cutRedrawing = true;",
        "replaceChildren(...rows)",
        "cutRedrawing = false;",
        "putBackTyped(typed, tb)",
    ]
    places = [shown.index(part) for part in order]
    assert places == sorted(places)
    field = re.search(r"function trackField\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "if (cutRedrawing || input.value === input.dataset.saved) return;" in field
    assert "onchange" not in field and "oninput" not in field


def test_a_field_left_by_pressing_a_button_does_not_take_the_button_away() -> None:
    """Leaving a field redraws the rows between press and release, and the click
    then lands on nothing: Close and Remove did not answer."""
    js = (STATIC / "app.js").read_text()
    shown = re.search(r"function renderTracks\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "if (cutPressing) { cutRedrawOwed = true; return; }" in shown
    wired = re.search(r"function wireTrackPresses\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert (
        '"pointerdown"' in wired
        and '"pointerup"' in wired
        and '"pointercancel"' in wired
    )
    assert "setTimeout(" in wired


def test_every_typed_boundary_is_the_person_s_and_an_unchanged_one_is_not() -> None:
    js = (STATIC / "app.js").read_text()
    typed = re.search(r"function typedEdge\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "setByHand(t)" in typed
    assert "!Number.isFinite(v) || round2(v) === round2(t[edge])" in typed
    assert "input.value === input.dataset.saved) return" in cut_table_code()


def test_no_top_level_name_is_declared_twice() -> None:
    """A second `boundaryWord` is a SyntaxError that stops the whole module, and
    every page goes blank with nothing on screen to say why."""
    for script in scripts():
        names = re.findall(
            r"^(?:async )?(?:function\*? |const |let |class )(\w+)",
            script.read_text(),
            re.M,
        )
        doubled = {n for n in names if names.count(n) > 1}
        assert not doubled, f"{script.name}: {sorted(doubled)}"


def test_the_first_pass_report_says_what_it_left_where_it_was() -> None:
    """The status line clears behind the dialog, so the report is what gets read."""
    js = (STATIC / "app.js").read_text()
    assert "you set or checked left where" in js
    assert '(kept ? `${kept}.\\n\\n` : "") + r.report' in js


def test_a_plan_that_does_not_say_what_a_fit_rested_on_makes_no_claim_about_a_gap() -> (
    None
):
    js = (STATIC / "app.js").read_text()
    words = re.search(r"const PLACED_WORDS = .*", js).group(0)  # type: ignore[union-attr]
    assert 'length: ["Placed by length", "quiet"]' in words
    boundary = re.search(r"function trackBoundary\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert 'PLACED_WORDS[t.placed] ?? ["Fitted", "good"]' in boundary


def test_a_track_placed_by_length_does_not_stop_cut() -> None:
    js = (STATIC / "app.js").read_text()
    cut = re.search(r"function railCut\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert "placedByLength" not in cut and "length" not in cut.replace(".length", "")


def test_a_first_pass_saves_first_so_it_cannot_fit_over_an_unsaved_boundary() -> None:
    """The server holds what the saved plan marks, and a boundary moved since was not
    in it: Run again took it, and reloading the page took the proof."""
    js = (STATIC / "app.js").read_text()
    run = re.search(r"async function fpRun\(.*?\n\}", js, re.S).group(0)  # type: ignore[union-attr]
    assert run.index("if (S.dirty && !(await save())) return;") < run.index("runJob(")
