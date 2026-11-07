"""The page's auto-save, run under node, with a server that answers as told.

These need node, which is not this project's dependency. The with-tools job runs
them and requires that they ran; anywhere without node they stand aside.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

RULES = (
    Path(__file__).resolve().parent.parent / "ripdoctor" / "web" / "static" / "ripui.js"
)

pytestmark = [
    pytest.mark.needs_node,
    pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed"),
]

# A page holding one number. `doc` reads it when a save goes, as the real one reads
# the screen. A post waits until `release()` when `hold` is set, and throws what
# `refuse` holds. Timers run a hundred times fast.
RIG = """
import { autosaver } from %s;
const realTimeout = setTimeout;
globalThis.setTimeout = (f, ms = 0) => realTimeout(f, ms / 100);
const pause = (ms) => new Promise((r) => realTimeout(r, ms));
const S = { dirty: false, slug: "a-record", meta: { seen: "s0" }, value: 0 };
const posts = [], told = [];
let inFlight = 0, most = 0, hold = false, refuse = null, gate = null;
const release = () => { hold = false; gate?.(); gate = null; };
const saver = autosaver({
  state: S,
  doc: () => ({ value: S.value }),
  post: async (slug, body) => {
    posts.push({ slug, ...body });
    most = Math.max(most, ++inFlight);
    if (hold) await new Promise((r) => { gate = r; });
    await pause(1);
    inFlight -= 1;
    if (refuse) throw refuse;
    return { seen: `s${posts.length}`, released: [] };
  },
  changed: () => {},
  saving: () => S.value,
  saved: (released, before) => told.push({ before }),
  refused: (why) => told.push({ refused: why }),
});
const { markDirty, flushSave, save } = saver;
const failure = (message, more = {}) =>
  Object.assign(new Error(message), { status: 409, ...more });
const pill = () => ({ dirty: S.dirty, fail: saver.failure() });
"""


def scenario(body: str) -> dict:  # type: ignore[type-arg]
    """Runs `body` after the rig; what it returns comes back as the answer."""
    program = (
        RIG % json.dumps(RULES.as_uri())
        + f"const out = await (async () => {{ {body} }})();"
        + "console.log(JSON.stringify(out));"
    )
    done = subprocess.run(  # noqa: S603 - argv list, never shell=True
        [str(shutil.which("node")), "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout)  # type: ignore[no-any-return]


def test_saves_go_one_at_a_time_and_the_last_state_lands() -> None:
    got = scenario("""
        hold = true;
        S.value = 1; markDirty();
        await pause(10);
        S.value = 2; markDirty();
        await pause(10);
        S.value = 3; markDirty();
        await pause(10);
        const whileHeld = posts.length;
        release();
        await pause(40);
        return { whileHeld, posts, most, ...pill() };
    """)
    assert got["whileHeld"] == 1, "the next waits for the one in flight"
    assert got["most"] == 1
    assert got["posts"][-1]["value"] == 3, "the last state is what ends up on disk"
    assert [p["seen"] for p in got["posts"]] == [
        f"s{i}" for i in range(len(got["posts"]))
    ], "each was made from what the one before left"
    assert not got["dirty"]


def test_a_failed_save_keeps_the_edit_says_why_and_stops_a_caller() -> None:
    got = scenario("""
        refuse = failure("the plan has changed", { stale: true });
        S.value = 1; markDirty();
        await pause(20);
        const shown = { ...pill(), value: S.value };
        const landed = await save();
        return { shown, landed, told, tries: posts.length };
    """)
    assert got["shown"]["dirty"], "a refused edit does not read as saved"
    assert got["shown"]["fail"]["why"] == "the plan has changed"
    assert got["shown"]["fail"]["stale"]
    assert got["shown"]["value"] == 1, "what was typed stays"
    assert got["landed"] is False, "Cut, First pass and Import stop on this"
    assert {"refused": "the plan has changed"} in got["told"]
    assert got["tries"] == 2, (
        "a stale save waits for a person, and does not retry alone"
    )
