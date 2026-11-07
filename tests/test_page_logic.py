"""What the page's own numbers do, run for real rather than read as text.

These need node, which is not this project's dependency. The with-tools job runs
them and requires that they ran; anywhere without node they stand aside.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "ripdoctor" / "web" / "static" / "ripui.js"

pytestmark = [
    pytest.mark.needs_node,
    pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed"),
]


def call(name: str, *args: object) -> dict:  # type: ignore[type-arg]
    program = (
        f"import {{ {name} }} from {json.dumps(RULES.as_uri())};"
        f"console.log(JSON.stringify({name}(...{json.dumps(args)}) ?? null));"
    )
    done = subprocess.run(  # noqa: S603 - argv list, never shell=True
        [str(shutil.which("node")), "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout)  # type: ignore[no-any-return]


def check_js(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv list, never shell=True
        [sys.executable, str(ROOT / "scripts" / "check_js.py"), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )


TRACK = {"start": 100.0, "end": 200.0}


def test_a_silent_region_spanning_the_track_is_nothing_to_split() -> None:
    """It placed the edges from the gap's own ends, at 1.5 s and 448.65 s of a side,
    and the track came out with a negative length. Issue #53."""
    got = call("splitEdges", TRACK, 150.0, [{"lo": 0.0, "hi": 449.95}])
    assert "nothing to split" in got["refused"]
    assert "one silent region" in got["refused"]


def test_a_silence_running_back_to_the_tracks_start_has_no_music_before_it() -> None:
    got = call("splitEdges", TRACK, 104.0, [{"lo": 90.0, "hi": 110.0}])
    assert "first part would have no music" in got["refused"]


def test_a_silence_running_on_to_the_tracks_end_has_no_music_after_it() -> None:
    got = call("splitEdges", TRACK, 196.0, [{"lo": 190.0, "hi": 210.0}])
    assert "second part would have no music" in got["refused"]


def test_a_gap_inside_the_track_is_shared_the_way_a_fit_shares_it() -> None:
    got = call("splitEdges", TRACK, 150.0, [{"lo": 145.0, "hi": 155.0}])
    assert got["end"] == 146.5 and got["start"] == 153.7


def test_a_gap_too_small_for_both_paddings_is_shared_at_its_middle() -> None:
    got = call("splitEdges", TRACK, 150.0, [{"lo": 149.0, "hi": 151.0}])
    assert got["end"] == got["start"] == 150.0


def test_outside_any_gap_the_split_lands_on_the_playhead() -> None:
    got = call("splitEdges", TRACK, 150.0, [{"lo": 20.0, "hi": 30.0}])
    assert got["end"] == got["start"] == 150.0


def test_edges_never_leave_the_track_being_split() -> None:
    for at in (100.6, 125.0, 150.0, 199.4):
        got = call("splitEdges", TRACK, at, [{"lo": 120.0, "hi": 180.0}])
        if "refused" not in got:
            assert TRACK["start"] < got["end"] <= got["start"] < TRACK["end"]


def test_a_part_shorter_than_half_a_second_is_refused() -> None:
    got = call("splitEdges", TRACK, 100.2, [])
    assert got["refused"] == "too close to an existing boundary"


def test_two_tracks_sharing_audio_overlap_and_two_meeting_do_not() -> None:
    a = {"number": 1, "start": 0.0, "end": 10.0}
    assert call("overlapping", a, [a, {"number": 2, "start": 9.0, "end": 20.0}])
    assert not call("overlapping", a, [{"number": 2, "start": 10.0, "end": 20.0}])
    assert not call("overlapping", a, [{"number": 2, "start": 11.0, "end": 20.0}])


def test_a_move_past_the_neighbour_is_crossed_and_one_undoing_an_overlap_is_not() -> (
    None
):
    one = {"number": 1, "start": 0.0, "end": 10.0}
    two = {"number": 2, "start": 12.0, "end": 20.0}
    both = [one, two]
    assert call("crossed", one, "end", 13.0, both)["number"] == 2
    assert call("crossed", two, "start", 9.0, both)["number"] == 1
    assert call("crossed", one, "end", 12.0, both) is None, "meeting is not crossing"
    assert call("crossed", two, "start", 10.0, both) is None
    assert call("crossed", one, "start", 1.0, both) is None, "nothing before it"
    assert call("crossed", two, "end", 99.0, both) is None, "nothing after it"
    saved_over = [{"number": 1, "start": 0.0, "end": 15.0}, two]
    assert call("crossed", saved_over[0], "end", 13.0, saved_over) is None
    assert call("crossed", two, "start", 13.0, saved_over) is None
    assert call("crossed", two, "start", 11.0, saved_over)["number"] == 1
    assert call("crossed", saved_over[0], "end", 16.0, saved_over)["number"] == 2


def test_a_row_from_an_earlier_capture_ends_once_a_person_has_marked_it() -> None:
    assert call("fromEarlier", {"placed": "earlier", "settled": ""}) is True
    assert call("fromEarlier", {"placed": "earlier", "settled": "ear"}) is False
    assert call("fromEarlier", {"placed": "gap", "settled": ""}) is False


def test_checked_is_offered_on_a_doubt_and_not_on_a_fit_or_a_settled_track() -> None:
    assert call("canCheck", {"forced": True, "settled": ""}) is True
    assert call("canCheck", {"placed": "earlier", "settled": ""}) is True
    assert call("canCheck", {"placed": "length", "settled": ""}) is True
    assert call("canCheck", {"placed": "length", "settled": "hand"}) is False
    assert call("canCheck", {"placed": "gap", "settled": ""}) is False
    assert call("canCheck", {"placed": "earlier", "settled": "ear"}) is False
    assert call("canCheck", None) is False


def test_a_finished_captures_last_reading_is_not_what_the_input_is_doing() -> None:
    """The status keeps it after the stop, so the needle stayed up and the line under
    it said `monitoring input` about a capture that had ended."""
    reading = {"full": -30.0, "band": -40.0}
    assert call("liveLevels", {"running": True, "levels": reading}) == reading
    assert call("liveLevels", {"running": False, "levels": reading}) is None
    assert call("liveLevels", {"running": True}) is None
    assert call("liveLevels", None) is None


def values(*names: str) -> dict:  # type: ignore[type-arg]
    program = (
        f"import {{ {', '.join(names)} }} from {json.dumps(RULES.as_uri())};"
        f"console.log(JSON.stringify({{ {', '.join(names)} }}));"
    )
    done = subprocess.run(  # noqa: S603 - argv list, never shell=True
        [str(shutil.which("node")), "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout)  # type: ignore[no-any-return]


def test_the_meter_says_what_the_chain_measures_and_nothing_a_mock_drew() -> None:
    """The advice on the page is to drop the needle in the groove zone. Zones from a
    drawing put true silent groove in red and quiet music in `groove`."""
    got = values("ZONES", "BAND_ZONES", "FLOOR")
    assert got["FLOOR"] == -100
    assert [(z["lo"], z["hi"], z["label"]) for z in got["ZONES"]] == [
        (-120, -68, "dead air / needle up"),
        (-68, -60, "groove"),
        (-60, -45, "quiet music"),
        (-45, 0, "music"),
    ]
    assert [(z["lo"], z["hi"], z["label"]) for z in got["BAND_ZONES"]] == [
        (-120, -92, "dead air"),
        (-92, -72, "silent groove"),
        (-72, -58, "arm-up rumble"),
        (-58, -44, "quiet music"),
        (-44, 0, "music"),
    ]


def test_the_scale_is_straight_and_ends_where_it_says() -> None:
    assert call("levelAt", -100) == 0
    assert call("levelAt", 0) == 1
    assert call("levelAt", -50) == 0.5
    assert call("levelAt", -120) == 0, "under the floor reads as the floor"
    assert call("levelAt", 3) == 1


def test_a_level_is_named_by_the_zone_it_is_in() -> None:
    zones = values("BAND_ZONES")["BAND_ZONES"]
    for db, label in [
        (-110.0, "dead air"),
        (-92.0, "silent groove"),
        (-72.1, "silent groove"),
        (-72.0, "arm-up rumble"),
        (-58.0, "quiet music"),
        (-44.0, "music"),
        (0.0, "music"),
    ]:
        assert call("zoneOf", db, zones)["label"] == label, db


def history(*steps: tuple[float, object]) -> list[dict]:  # type: ignore[type-arg]
    seen: list[dict] = []  # type: ignore[type-arg]
    for at, band in steps:
        seen = call("historyAfter", seen, at, band)  # type: ignore[assignment]
    return seen


def test_a_poll_with_no_level_leaves_a_gap_and_not_a_level() -> None:
    seen = history((10.0, -40.0), (10.5, None), (11.0, -41.0))
    assert [s["db"] for s in seen] == [-40.0, -41.0]


def test_the_history_forgets_what_has_left_its_window() -> None:
    seen = history((0.0, -40.0), (10.0, -41.0), (50.0, -42.0))
    assert [s["db"] for s in seen] == [-42.0]


def test_a_stretch_with_no_readings_is_drawn_as_nothing() -> None:
    """A capture stopped and started, or a tab left in the background. Drawing across
    it would be showing levels that nobody read."""
    seen = [{"at": 100.0, "db": -40.0}, {"at": 110.0, "db": -41.0}]
    bars = call("historyBars", seen, 110.0)
    assert len(bars) == 2
    assert bars[0]["to"] <= (30 - 10) / 30 + 1e-9
    assert bars[1]["from"] >= (30 - 0.5 - 0.01) / 30


def test_readings_a_poll_apart_meet() -> None:
    seen = [{"at": 100.0, "db": -40.0}, {"at": 100.5, "db": -41.0}]
    first, second = call("historyBars", seen, 100.5)
    assert first["to"] == second["from"]
    assert second["to"] == 1


def test_a_reading_older_than_the_window_is_not_drawn_at_its_edge() -> None:
    assert call("historyBars", [{"at": 0.0, "db": -40.0}], 31.0) == []


def test_the_real_scripts_parse() -> None:
    done = check_js("--require")
    assert done.returncode == 0, done.stdout
    assert " scripts parse as modules" in done.stdout
    assert not done.stdout.startswith("js: 0 ")


def test_a_duplicate_top_level_name_stops_the_check(tmp_path: Path) -> None:
    (tmp_path / "broken.js").write_text("const twice = 1;\nfunction twice() {}\n")
    done = check_js("--require", "--dir", tmp_path)
    assert done.returncode == 1
    assert "broken.js does not parse" in done.stdout
    assert "already been declared" in done.stdout


def test_any_other_syntax_error_stops_the_check(tmp_path: Path) -> None:
    (tmp_path / "fine.js").write_text("export const a = 1;\n")
    (tmp_path / "broken.js").write_text("export function f( {\n")
    done = check_js("--dir", tmp_path)
    assert done.returncode == 1 and "1 of 2 scripts" in done.stdout


def test_a_module_is_read_as_a_module(tmp_path: Path) -> None:
    """As CommonJS, `import` is the error, and the check would fail on every real
    file or be quietly switched off. This is what says the flag is right."""
    (tmp_path / "module.js").write_text(
        'import { x } from "/other.js";\nexport const y = await x;\n'
    )
    done = check_js("--dir", tmp_path)
    assert done.returncode == 0, done.stdout


def test_a_directory_with_no_scripts_is_not_a_pass(tmp_path: Path) -> None:
    assert check_js("--dir", tmp_path).returncode == 1
