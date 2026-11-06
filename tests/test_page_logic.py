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
