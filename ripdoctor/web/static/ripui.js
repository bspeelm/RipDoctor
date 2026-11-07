// Rip panel: device pick, metadata, transport, and the live meter.
//
// The meter is the point. Four states have to be told apart while the needle is
// going down, and full-band level alone cannot do it — arm-up handling rumble
// reads -32 dB full band, only 9 dB below music. In the 1-3 kHz band the same
// two states are 27 dB apart. So the band bar is drawn bigger and the zones are
// labelled with what they mean physically.

// Re-measured 2026-08-26 on a chain whose converter sits after the phono gain
// rather than at line level: stylus UP reads -75 dB full band, because the
// converter now carries that gain's noise. The same edge measured -89 on the
// line-level chain these numbers were inherited from. The needle-up boundary
// was -80, so the meter called "stylus in the air" a groove. Only this edge is
// moved: the groove and quiet-music edges below still want a real measurement
// and are left as inherited values rather than guesses.
export const ZONES = [
  { lo: -120, hi: -68, label: "dead air / needle up", color: "#2a2e35" },
  { lo: -68, hi: -60, label: "groove", color: "#2f4a3a" },
  { lo: -60, hi: -45, label: "quiet music", color: "#3f5a2f" },
  { lo: -45, hi: 0, label: "music", color: "#4a5a2f" },
];
export const BAND_ZONES = [
  { lo: -120, hi: -92, label: "dead air", color: "#2a2e35" },
  { lo: -92, hi: -72, label: "silent groove", color: "#2f4a3a" },
  { lo: -72, hi: -58, label: "arm-up rumble", color: "#5a3f2f" },
  { lo: -58, hi: -44, label: "quiet music", color: "#3f5a2f" },
  { lo: -44, hi: 0, label: "music", color: "#4a5a2f" },
];
export const FLOOR = -100;

// What the input is reading now, or nothing. A finished capture's status keeps its last
// reading for as long as it is the last capture, and that is not a level.
//
// Deliberately no peak, held or otherwise: this is a 42 ms window and it jumps every poll,
// which is unreadable. `ripdoctor probe` is where gain is set.
// Past this a reading is one the capture loop could not replace. A loop keeping up reads
// once a second, and the age includes the recorder's settle of less than that.
const READING_STALE_SECONDS = 5;
export const liveLevels = (st) =>
  (st?.running && !(st.reading_age > READING_STALE_SECONDS) && st.levels) || null;

// Where a level sits on the scale both meters share, 0 at the floor and 1 at full scale.
// Below the floor reads as the floor: the scale ends there, and it does not bend to fit.
export const levelAt = (db) => (Math.max(FLOOR, Math.min(0, db)) - FLOOR) / (0 - FLOOR);

export const zoneOf = (db, zones) =>
  zones.find((z) => db >= z.lo && db < z.hi) ?? (db >= 0 ? zones.at(-1) : zones[0]);

const readable = (db) => typeof db === "number" && Number.isFinite(db);
const minus = (text) => String(text).replace("-", "\u2212");
const SANS = "ui-sans-serif,system-ui", MONO = "ui-monospace,SFMono-Regular,Menlo,monospace";
const INK = "#e6e8ea", DIM = "#8b9199", FAINT = "#6b7480", GRID = "rgba(15,17,20,.55)";

// A canvas drawn at the screen's own pixels, or nothing while it is hidden.
function pen(cv) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if (!w || !h) return null;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  }
  const c = cv.getContext("2d");
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, w, h);
  return { c, w, h };
}

// A zone's name in the room it has, a word to a line if it must. Too narrow for that, it
// goes without, and the readout beside the bar still names the zone the needle is in.
function labelLines(c, label, room, most) {
  const lines = [];
  for (const word of label.split(" ")) {
    const joined = lines.length ? `${lines.at(-1)} ${word}` : word;
    if (lines.length && c.measureText(joined).width <= room) lines[lines.length - 1] = joined;
    else lines.push(word);
  }
  const fits = lines.length <= most && lines.every((l) => c.measureText(l).width <= room);
  return fits ? lines : [];
}

const METER_STACKS_BELOW = 480;

function drawRow(c, row, left, right) {
  const X = (db) => left + levelAt(db) * (right - left);
  for (const z of row.zones) {
    const a = X(z.lo), b = X(z.hi);
    if (b <= a) continue;
    c.fillStyle = z.color;
    c.fillRect(a, row.y, b - a, row.h);
  }
  if (readable(row.db)) {
    c.fillStyle = "rgba(230,232,234,.13)";
    c.fillRect(left, row.y, X(row.db) - left, row.h);
  }
  c.font = `10px ${SANS}`;
  c.textBaseline = "middle";
  c.textAlign = "left";
  for (const z of row.zones) {
    const a = X(z.lo), b = X(z.hi);
    const lines = labelLines(c, z.label, b - a - 6, row.h >= 26 ? 2 : 1);
    c.fillStyle = "rgba(230,232,234,.72)";
    lines.forEach((l, i) => c.fillText(l, a + 3, row.y + row.h / 2 + (i - (lines.length - 1) / 2) * 12));
  }
  if (readable(row.db)) {
    const x = X(row.db);
    c.fillStyle = "#0f1114";
    c.fillRect(x - 2.5, row.y - 3, 5, row.h + 6);
    c.fillStyle = INK;
    c.fillRect(x - 1.5, row.y - 3, 3, row.h + 6);
  }
}

// One bar above the other on the same scale, so a level reads the same on both. On a wide
// page the name and reading sit either side of each bar; on a phone they sit above it, and
// the bar gets all of the width.
export function drawMeter(cv, levels) {
  const p = pen(cv);
  if (!p) return;
  const { c, w, h } = p;
  const stacked = w < METER_STACKS_BELOW;
  const left = stacked ? 0 : 76, right = stacked ? w - 1 : w - 112;
  const over = stacked ? 16 : 0;
  const rows = [
    { name: "Full band", db: levels?.full, zones: ZONES, y: 3 + over, h: 28, weight: 400 },
    { name: "1\u20133 kHz", db: levels?.band, zones: BAND_ZONES, y: 3 + over + 28 + 12 + over, h: 48, weight: 600 },
  ];
  for (const row of rows) {
    drawRow(c, row, left, right);
    const said = readable(row.db)
      ? { db: minus(row.db.toFixed(1)), zone: zoneOf(row.db, row.zones).label }
      : { db: "\u2014", zone: "" };
    const mid = row.y + row.h / 2;
    c.textBaseline = "middle";
    c.textAlign = "left";
    c.font = `${row.weight} 12px ${SANS}`;
    c.fillStyle = row.weight > 400 ? INK : DIM;
    if (stacked) {
      c.fillText(row.name, 0, row.y - 11);
      c.textAlign = "right";
      c.font = `600 12px ${MONO}`;
      c.fillStyle = INK;
      const db = `${said.db} dB`;
      c.fillText(db, w, row.y - 11);
      const dbw = c.measureText(db).width;
      c.font = `11px ${SANS}`;
      c.fillStyle = DIM;
      c.fillText(said.zone, w - dbw - 8, row.y - 11);
    } else {
      c.fillText(row.name, 0, mid);
      c.textAlign = "right";
      c.font = `600 13px ${MONO}`;
      c.fillStyle = INK;
      c.fillText(said.db, w, said.zone ? mid - 7 : mid);
      c.font = `10.5px ${SANS}`;
      c.fillStyle = DIM;
      c.fillText(said.zone, w, mid + 7);
    }
  }
  const scale = rows[1].y + rows[1].h + 6;
  c.font = `10px ${MONO}`;
  c.textBaseline = "top";
  for (let db = FLOOR; db <= 0; db += 20) {
    const x = left + levelAt(db) * (right - left);
    c.fillStyle = GRID;
    for (const row of rows) c.fillRect(x, row.y, 1, row.h);
    c.fillStyle = FAINT;
    c.textAlign = db === FLOOR ? "left" : db === 0 ? "right" : "center";
    c.fillText(db === 0 ? "0 dB" : minus(db), x, scale);
  }
}

// ------------------------------------------------------------- the band's history

const HISTORY_SECONDS = 30;

// The longest a reading is taken to stand for, a little over the capture's one a second.
// Anything longer between two polls is a gap, and is left as one rather than drawn across.
const HOLD_SECONDS = 1.5;
// What one on its own stands for: the poll that found it.
const POLL_SECONDS = 0.5;

// The readings still inside the window, and this one if there is one. A poll with no
// level adds nothing, so what it leaves is a gap and not a level.
export function historyAfter(seen, at, band) {
  const kept = seen.filter((s) => at - s.at < HISTORY_SECONDS + HOLD_SECONDS);
  return readable(band) ? [...kept, { at, db: band }] : kept;
}

// Each reading as a stretch of the window, 0 the oldest edge and 1 now.
export function historyBars(seen, at) {
  const where = (t) => 1 - (at - t) / HISTORY_SECONDS;
  const bars = [];
  seen.forEach((s, i) => {
    const before = seen[i - 1]?.at;
    const since = before !== undefined && s.at - before <= HOLD_SECONDS ? before : s.at - POLL_SECONDS;
    if (where(s.at) <= 0) return;
    bars.push({ from: Math.max(0, where(since)), to: where(s.at), db: s.db });
  });
  return bars;
}

// The same scale and the same zones as the bar above it, stood on end: the floor is the
// bottom and full scale is the top, whatever this window happens to hold, so a quiet
// stretch looks quiet and not like a graph that has zoomed in on it.
export function drawHistory(cv, seen, at) {
  const p = pen(cv);
  if (!p) return;
  const { c, w, h } = p;
  const foot = 14, right = w - 30;
  const bottom = h - foot;
  const Y = (db) => bottom * (1 - levelAt(db));
  for (const z of BAND_ZONES) {
    const top = Y(z.hi), low = Y(z.lo);
    if (low <= top) continue;
    c.fillStyle = z.color;
    c.fillRect(0, top, right, low - top);
  }
  const bars = historyBars(seen, at);
  for (const b of bars) {
    const x0 = b.from * right, x1 = b.to * right, y = Y(b.db);
    c.fillStyle = "rgba(230,232,234,.2)";
    c.fillRect(x0, y, Math.max(1, x1 - x0), bottom - y);
    c.fillStyle = INK;
    c.fillRect(x0, y - 1, Math.max(1, x1 - x0), 2);
  }
  c.font = `10px ${MONO}`;
  c.textAlign = "right";
  for (let db = FLOOR; db <= 0; db += 20) {
    const y = Y(db);
    c.fillStyle = GRID;
    c.fillRect(0, Math.min(y, bottom - 1), right, 1);
    c.fillStyle = FAINT;
    c.textBaseline = db === 0 ? "top" : db === FLOOR ? "bottom" : "middle";
    c.fillText(minus(db), w, y);
  }
  // Last, and with a dark edge: whatever the readings did, the names stay readable.
  c.font = `10px ${SANS}`;
  c.textBaseline = "middle";
  c.textAlign = "left";
  c.lineWidth = 3;
  c.strokeStyle = "rgba(15,17,20,.75)";
  for (const z of BAND_ZONES) {
    const top = Y(z.hi), low = Y(z.lo);
    if (low - top < 10) continue;
    c.strokeText(z.label, 6, (top + low) / 2);
    c.fillStyle = INK;
    c.fillText(z.label, 6, (top + low) / 2);
  }
  c.font = `10px ${MONO}`;
  c.textBaseline = "bottom";
  c.fillStyle = FAINT;
  c.textAlign = "left";
  c.fillText("30 s ago", 0, h);
  c.textAlign = "right";
  c.fillText("now", right, h);
  if (!bars.length) {
    c.textAlign = "center";
    c.textBaseline = "middle";
    c.font = `12px ${SANS}`;
    c.fillStyle = DIM;
    c.fillText("nothing to show until a capture is running", right / 2, Y(-8));
  }
}

export function slugify(artist, album) {
  return `${artist} - ${album}`.replace(/[ /]/g, "-").replace(/[^A-Za-z0-9\-_]/g, "");
}

// What a boundary may do, worked out from numbers alone so it can be run without a page.

const LEAD = 1.3, TAIL = 1.5;

// Neither part of a split may be shorter than this.
const SMALLEST = 0.5;

const secs = (v) => `${v.toFixed(2)} s`;

// Where a split puts the two new edges, or why there is nothing to split.
//
// Inside a silence between two pieces of music the edges go the way a fit places them: tail
// after the outgoing music, lead before the incoming, so the second track gets its lead-in.
// A silence that reaches the track's own start or end has music on one side at most, and
// its edges say nothing about this track. Outside a gap the split lands exactly where the
// playhead is, because a "split here" that quietly moves somewhere else is worse than a
// blunt one.
export function splitEdges(track, at, gaps) {
  const g = (gaps || []).find((x) => x.lo <= at && at <= x.hi);
  let end = at, start = at, note = "split at playhead";
  if (g) {
    const span = `${secs(g.lo)} to ${secs(g.hi)}`;
    if (g.lo <= track.start && g.hi >= track.end)
      return { refused: `nothing to split: all of this track is inside one silent region (${span}), so there is no music either side of the playhead` };
    if (g.lo <= track.start)
      return { refused: `nothing to split: the silence under the playhead (${span}) runs back to this track's start, so the first part would have no music. Move the playhead into the music` };
    if (g.hi >= track.end)
      return { refused: `nothing to split: the silence under the playhead (${span}) runs on to this track's end, so the second part would have no music. Move the playhead into the music` };
    end = Math.min(g.lo + TAIL, g.hi - 0.2);
    start = Math.max(g.hi - LEAD, g.lo + 0.2);
    if (start < end) end = start = (g.lo + g.hi) / 2;
    note = `split inside gap ${g.lo.toFixed(2)}–${g.hi.toFixed(2)}`;
  }
  if (end - track.start < SMALLEST || track.end - start < SMALLEST)
    return { refused: "too close to an existing boundary" };
  return { end, start, note };
}

// Its times are positions on a capture since replaced, and nobody has said otherwise.
export const fromEarlier = (t) => t.placed === "earlier" && !t.settled;

// Whether "Checked" has anything to say about this track. A fit's own boundary is not in
// doubt, and marking it would call a thing heard that nothing asked anyone to hear.
export const canCheck = (t) => !!t && !t.settled && (!!t.forced || t.placed === "length" || fromEarlier(t));

// The neighbour this edge would pass by moving to `at`, or nothing. An edge already past it,
// in a plan saved that way, can still move back: that is how the overlap gets undone.
export function crossed(track, edge, at, list) {
  const order = [...list].sort((a, b) => a.number - b.number);
  const here = order.findIndex((x) => x.number === track.number);
  if (edge === "start") {
    const before = order[here - 1];
    return before && at < before.end && at < track.start ? before : null;
  }
  const after = order[here + 1];
  return after && at > after.start && at > track.end ? after : null;
}

// The tracks on this side that share audio with this one. Meeting at a point is not sharing.
export function overlapping(track, list) {
  return list.filter((x) => x !== track && track.start < x.end && x.start < track.end);
}

// Every edit saves itself. `settle` is how long to wait for the next one, so a held key is
// not a save a press; null leaves the save to whoever finishes the edit, as a drag does.
export function autosaver({ state, doc, post, changed, saving, saved, refused }) {
  let timer = null;
  let run = null;                     // the one save in flight, so they stay in order
  let edits = 0;
  let fail = null;                    // {why, stale, busy}, on screen until a save lands

  function markDirty(d = true, settle = 0) {
    state.dirty = d;
    clearTimeout(timer);
    timer = null;
    if (d) {
      edits += 1;
      if (settle != null) timer = setTimeout(flushSave, settle);
    } else fail = null;
    changed();
  }

  function flushSave() {
    clearTimeout(timer);
    timer = null;
    run ??= saveLoop().finally(() => { run = null; changed(); });
    return run;
  }

  // One at a time, from what is on screen when it goes, and again if anything changed while
  // it went: the last state always lands. What was typed is not touched by a failure.
  async function saveLoop() {
    while (state.dirty && state.slug && !timer) {
      const sent = edits, before = saving();
      try {
        const { released, seen } = await post(state.slug, { ...doc(), seen: state.meta.seen });
        state.meta.seen = seen;
        fail = null;
        if (sent === edits) state.dirty = false;
        saved(released, before);
      } catch (e) {
        fail = { why: e.status ? e.message : "The server could not be reached. This tries again by itself.", stale: e.stale, busy: e.busy };
        // A job ending and a connection coming back are not things a person should have to watch for.
        if (e.busy || !e.status) timer = setTimeout(flushSave, e.busy ? 2000 : 5000);
        return false;
      }
    }
    return !fail;
  }

  // Waits for what is on screen to be on disk, and reports whether it got there, so a caller
  // that must not proceed on a stale plan can stop.
  async function save() {
    let landed = await flushSave();
    while (landed && state.dirty) landed = await flushSave();
    if (!landed) refused(fail?.why);
    return landed;
  }

  // Reading a record afresh does not cost what the page has not saved. That goes first, and
  // refused, the page stays as it is. An edit during the read sends it round again, since what
  // came back is from before it. One made while the rest opens is on the fresh tracks, so it
  // is left dirty with its save still coming.
  async function reload(read, show) {
    let seenEdits, got;
    do {
      if (state.dirty && !(await save())) return false;
      seenEdits = edits;
      got = await read();
    } while (seenEdits !== edits);
    await show(got);
    if (seenEdits === edits) markDirty(false);
    return true;
  }

  return { markDirty, flushSave, save, reload, failure: () => fail, running: () => !!run };
}
