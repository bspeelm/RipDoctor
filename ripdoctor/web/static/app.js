import { Editor, fmt, fmtLong } from "/editor.js";
import { Player } from "/audio.js";
import { canCheck, crossed, drawHistory, drawMeter, fromEarlier, historyAfter, liveLevels, overlapping, slugify, splitEdges } from "/ripui.js";

const $ = (s) => {
  const e = document.querySelector(s);
  // A missing node used to throw on the first property set and abort the whole
  // boot. Warn loudly, keep going: a stub is always better than a blank app.
  if (!e) { console.warn(`RipDoctor: no element for ${s}`); return document.createElement("div"); }
  return e;
};
const el = (t, cls, txt) => { const e = document.createElement(t); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: "same-origin", ...opts });
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("json") ? await r.json() : null;
  if (!r.ok) {
    // The status comes with it: some refusals are a caller's business rather
    // than a failure - "one of those is already running" being the one that
    // matters, because it means the work asked for is happening.
    const e = new Error(body?.error || `${r.status} ${r.statusText}`);
    e.status = r.status;
    e.needsConfirm = !!body?.needs_confirm;
    e.stale = !!body?.stale;
    e.busy = !!body?.busy;
    throw e;
  }
  return body;
}
const postJSON = (path, obj) =>
  api(path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(obj) });

const S = {
  slug: null, album: null, side: null,
  meta: null,                     // {album, artist, date, sides, ready, tracks_by_side}
  bySide: {},                     // letter -> [track]
  sideData: {},                   // letter -> {duration, windowMs, rms, peak, band, gaps}
  dirty: false,
};

let ed, player, ripTimer, tpEd, tpPlayer;
// True while a stop is being waited out. The readout has one owner then: the
// meter poll and the wait were both writing it on different intervals, so it
// alternated between what each of them thought was happening.
let settling = false;

// ------------------------------------------------------------------ status

function status(msg, sticky) {
  $("#status").textContent = msg || "";
  if (msg && !sticky) setTimeout(() => { if ($("#status").textContent === msg) $("#status").textContent = ""; }, 4000);
}
// The console is the pipeline tools' own stdout. It overlaps the finished-tracks
// list on a good run and diverges on a bad one, which is the whole reason to
// keep both - so it collapses rather than disappearing, and anything loud
// reopens it.
function logline(s, loud = false) {
  $("#logwrap").hidden = false;
  const L = $("#log");
  L.textContent += s + "\n"; L.scrollTop = L.scrollHeight;
  if (loud) setLogCollapsed(false);
}

function setLogCollapsed(c) {
  $("#logwrap").classList.toggle("collapsed", c);
  $("#logtoggle").textContent = c ? "show" : "hide";
  try { localStorage.setItem("ripdoctor:log:collapsed", c ? "1" : ""); } catch {}
}
// Every edit saves itself. `settle` is how long to wait for the next one, so a held key is
// not a save a press; null leaves the save to whoever finishes the edit, as a drag does.
const NUDGE_SETTLE = 500;
let saveTimer = null;
let saveRun = null;                   // the one save in flight, so they stay in order
let saveEdits = 0;
let saveFail = null;                  // {why, stale, busy}, on screen until a save lands

function markDirty(d = true, settle = 0) {
  S.dirty = d;
  clearTimeout(saveTimer);
  saveTimer = null;
  if (d) {
    saveEdits += 1;
    if (settle != null) saveTimer = setTimeout(flushSave, settle);
  } else saveFail = null;
  showCutRail();
}

function flushSave() {
  clearTimeout(saveTimer);
  saveTimer = null;
  saveRun ??= saveLoop().finally(() => { saveRun = null; showCutRail(); });
  return saveRun;
}

// One at a time, from what is on screen when it goes, and again if anything changed while
// it went: the last state always lands. What was typed is not touched by a failure.
async function saveLoop() {
  while (S.dirty && S.slug && !saveTimer) {
    const edits = saveEdits, alignWas = !$("#realign").hidden;
    showCutRail();
    try {
      const { released, seen } = await postJSON(`/api/plan/${S.slug}`, { ...buildDoc(), seen: S.meta.seen });
      S.meta.seen = seen;
      saveFail = null;
      if (edits === saveEdits) S.dirty = false;
      saidSaved(released, alignWas);
      refreshCutRail();
    } catch (e) {
      saveFail = { why: e.status ? e.message : "The server could not be reached. This tries again by itself.", stale: e.stale, busy: e.busy };
      // A job ending and a connection coming back are not things a person should have to watch for.
      if (e.busy || !e.status) saveTimer = setTimeout(flushSave, e.busy ? 2000 : 5000);
      return false;
    }
  }
  return !saveFail;
}

async function saidSaved(released, alignWas) {
  await refreshAlignButton();
  const said = [];
  if (released?.length) said.push(`${counted(released.length, "track", "tracks")} (${released.join(", ")}) kept ${released.length === 1 ? "its time" : "their times"} from the earlier capture, and ${released.length === 1 ? "is" : "are"} marked for that.`);
  // Said once, as it happens. From here the rail names only what still works.
  if (alignWas && $("#realign").hidden) said.push("Saved on this capture, so Align from archive is no longer offered. First pass, Checked after listening, and moving a boundary remain.");
  if (said.length) status(said.join(" "), true);
}

// `keepalive` is what lets a request outlive the page that sent it. With one already in
// flight this page's digest is out of date, and it is the same page's work that moved it.
function saveOnLeaving() {
  if (!S.dirty || !S.slug) return;
  const doc = saveRun ? buildDoc() : { ...buildDoc(), seen: S.meta.seen };
  fetch(`/api/plan/${S.slug}`, { method: "POST", keepalive: true, credentials: "same-origin",
                                 headers: { "content-type": "application/json" }, body: JSON.stringify(doc) });
}

// What was saved, when this page did not save it: a side finished by a rip, or a tab
// beside this one. A page with something of its own in hand is left alone, and is told at
// its own save.
async function noticeDisk() {
  const typing = document.activeElement?.closest?.("#cutview input, #cutview textarea");
  if (!S.slug || !S.meta || S.dirty || saveRun || S.job || cutOpening || typing || player?.playing
      || document.hidden || document.querySelector("dialog[open]")
      || (document.body.dataset.mode ?? "cut") !== "cut") return;
  const slug = S.slug;
  let now;
  try { now = await api(`/api/seen/${slug}`); } catch { return; }
  if (S.slug !== slug || S.dirty || (now.seen === S.meta.seen && now.captures === S.meta.captures)) return;
  const side = S.side;
  S.sideData = {};
  await openAlbum(slug);
  if (side && side !== S.side && S.meta.sides.includes(side)) await openSide(side);
  status("reloaded: this record changed on disk");
}

// ------------------------------------------------------------ envelope I/O

async function fetchEnvelope(url, expectWindows) {
  const r = await fetch(url, { credentials: "same-origin" });
  if (!r.ok) throw new Error("envelope not ready");
  const buf = await r.arrayBuffer();
  const dv = new DataView(buf);
  const magic = String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3));
  if (magic !== "CAE1") throw new Error("bad envelope file");
  const n = dv.getUint32(8, true), windowMs = dv.getUint32(12, true);
  // A stale envelope has a perfectly valid header and the WRONG LENGTH, so the
  // editor stretches it across the new timeline and draws floor past its end.
  // That is how a 162 s bad rip kept being rendered over a re-ripped 22 min
  // side, silently, for as long as the browser held the cached response. The
  // length is the only thing that catches it, so check it and refuse.
  if (expectWindows && n !== expectWindows)
    throw new Error(`stale envelope: got ${n} windows, expected ${expectWindows}`
                    + ` — reload with cache disabled (Ctrl-Shift-R)`);
  if (buf.byteLength < 16 + 3 * n) throw new Error("truncated envelope file");
  return {
    windowMs,
    rms: new Uint8Array(buf, 16, n),
    peak: new Uint8Array(buf, 16 + n, n),
    band: new Uint8Array(buf, 16 + 2 * n, n),
  };
}

// --------------------------------------------------------------- prepare

async function ensurePrepared() {
  const rec = S.meta.recording || [];
  const missing = S.meta.sides.filter((s) => !S.meta.ready[s] && !rec.includes(s));
  if (!missing.length) return;
  const box = $("#prepare"); box.hidden = false;
  try {
    try {
      await postJSON(`/api/prepare/${S.slug}`, {});
    } catch (e) {
      // Already running is not a refusal to do the work, it is the work. Two
      // openAlbum calls overlap routinely - an auto-stop reopens the record
      // while a click is reopening it too - and the second one used to throw.
      if (e.status !== 409) throw e;
    }
    await pollPrepare(box);
  } finally {
    box.hidden = true;                    // even when the poll throws
  }
  S.meta = await api(`/api/album/${S.slug}`);
}

async function pollPrepare(box) {
  for (;;) {
    // The job, not the endpoint that starts one. The prepare endpoint takes a
    // POST and nothing else, so polling it answered "no such route" every time
    // and no prepare has ever reported its progress. It stayed invisible
    // because the failure was swallowed and the work carried on regardless.
    const j = await api(`/api/job/${S.slug}`);
    box.querySelector("span").textContent =
      j.error ? `failed: ${j.error}` : `preparing ${j.current || ""} — ${j.detail} (${j.finished}/${j.total})`;
    box.querySelector("i").style.width = `${(j.finished / j.total) * 100}%`;
    if (j.error) throw new Error(j.error);
    if (j.done) return;
    await new Promise((r) => setTimeout(r, 1200));
  }
}

// Split, Import and Archive run as background jobs: the POST starts one and
// returns 202 straight away, and the result arrives by polling. Prepare has
// always worked this way; this generalises it. The elapsed count is measured
// client-side rather than from the job's `started` timestamp, because the
// browser and the server are different machines with independent clocks.
async function runJob(slug, path, body, note) {
  const t0 = Date.now();
  await postJSON(path, body || {});
  S.job = note || "a job";
  ed.frozen = true;
  try {
    for (;;) {
      await new Promise((r) => setTimeout(r, 800));
      const j = await api(`/api/job/${slug}`);
      if (j.done) {
        if (j.error) throw new Error(j.error);
        return j.result;
      }
      if (note) status(`${note}… ${Math.round((Date.now() - t0) / 1000)}s`, true);
    }
  } finally {
    S.job = null;
    ed.frozen = false;
  }
}

// What a job is reading must not move under it, so a boundary, a track added or removed and
// a check are refused while it runs, and say so. Text waits and saves when it ends.
function refusedByJob() {
  if (S.job) status(`Not changed: ${S.job} is running on this record. Try again when it finishes.`, true);
  return !!S.job;
}

// ------------------------------------------------------------ album/side

// Nothing loaded: blank the canvas, empty the panels, disable the actions.
function clearAlbum(message) {
  S.slug = null; S.meta = { sides: [], album: "", artist: "", date: "", ready: {}, recording: [] };
  S.bySide = {}; S.sideData = {}; S.side = null;
  player?.stop();
  ed?.clear();
  $("#sides").innerHTML = "";
  $("#tracks tbody").innerHTML = "";
  $("#sp-letter").textContent = "—";
  $("#sp-meta").textContent = message || "";
  $("#review").innerHTML = `<p class="dim">${message || "no album loaded"}</p>`;
  $("#ro-time").textContent = "—";
  $("#ro-track").textContent = "";
  $("#ro-gap").textContent = ""; $("#ro-gap").className = "";
  if (ed) showCutTools();
  // per-album, and loadRipSides only runs from openAlbum - so without this it
  // keeps listing captures for a record that is no longer loaded
  $("#ripsides").hidden = true;
  for (const id of ["#split", "#import", "#archive", "#firstpass", "#addtrack", "#splittrack"])
    $(id).disabled = true;
  cutAsked = { review: null, arc: null };
  markDirty(false);
}

// ----------------------------------------------------------------- the rail
//
// Where a record is in rip -> first pass -> check -> cut -> import -> archive, and
// what the next step needs, so that "why is that button grey" has an answer on screen.

const CUT_STEPS = ["rip", "firstpass", "check", "cut", "import", "archive"];

// What only the server knows. It is asked when a record opens and when a job ends; an edit
// redraws from what the page already holds, so a nudge costs no request.
let cutAsked = { review: null, arc: null };
let cutCutting = false;

const cutBox = (id) => document.getElementById(`cut-st-${id}`);
const cutStep = (id, ...said) => railStep(cutBox(id), ...said);
const blue = (id, on) => $(id).classList.toggle("ghost", !on);
const namedList = (names) => {
  if (names.length < 2) return names.join("");
  const shown = names.slice(0, 3);
  const more = names.length - shown.length;
  return more ? `${shown.join(", ")} and ${more} more` : `${shown.slice(0, -1).join(", ")} and ${shown.at(-1)}`;
};

async function refreshCutRail() {
  const slug = S.slug;
  const asked = { review: null, arc: null, recut: false };
  if (slug) {
    // A step that could not be asked says Waiting, and its own button still reports why.
    try {
      const cuts = await api(`/api/review/${slug}`);
      asked.review = cuts.tracks.length;
      asked.recut = cuts.older_than_plan;
    } catch { asked.review = null; }
    try { asked.arc = await api(`/api/archive/${slug}?album=${encodeURIComponent(S.meta.album || "")}`); } catch { asked.arc = null; }
  }
  if (S.slug !== slug) return;          // another record opened while these were answered
  cutAsked = asked;
  showCutRail();
}

function railRip(f) {
  const sides = f.sides;
  const letters = namedList(sides);
  $("#cut-rip").textContent = sides.length ? "Rip another side" : "Rip a side";
  blue("#cut-rip", !sides.length);
  $("#cut-rip").hidden = f.archived;
  if (f.archived) return cutStep("rip", "done", counted(sides.length, "side", "sides"), "In the archive.");
  if (f.recording.length) return cutStep("rip", "run", "Recording", `Side ${namedList(f.recording)} is still being recorded.`);
  if (!sides.length) return cutStep("rip", "todo", "No sides", "Record a side here, or bring one in from Upload.");
  return cutStep("rip", "done", counted(sides.length, "side", "sides"), `${sides.length === 1 ? "Side" : "Sides"} ${letters}.`);
}

function sidesFromEarlier() {
  return (S.meta?.sides ?? []).filter((letter) => (S.bySide[letter] || []).some(fromEarlier));
}

function railFirstPass(f) {
  const n = f.tracks.length;
  $("#firstpass").textContent = n ? "Run again" : "First pass";
  blue("#firstpass", !!f.sides.length && !n);
  if (!f.sides.length) return cutStep("firstpass", "wait", "Waiting", "Waiting on a side.");
  if (!n) return cutStep("firstpass", "ready", "Not run", "Find the release and fit its tracks to the gaps, or add tracks by hand.");
  const earlier = sidesFromEarlier();
  if (earlier.length) {
    blue("#firstpass", true);
    return cutStep("firstpass", "todo", "Captured again", `Side ${namedList(earlier)} was captured again after its boundaries were placed. Run this again${$("#realign").hidden ? "" : ", or align from the archive. Any edit saved here puts the side on this capture and ends that offer"}.`);
  }
  return cutStep("firstpass", "done", S.meta.mbid ? "Matched" : "By hand",
                 `${counted(n, "track", "tracks")} across ${counted(f.sides.length, "side", "sides")}.`);
}

function railCheck(f) {
  $("#cut-review").hidden = !cutDoubts().length;
  if (!f.tracks.length) return cutStep("check", "wait", "Waiting", "Waiting on the first pass.");
  if (f.forced.length) {
    const titles = namedList(f.forced.map((t) => t.title));
    return cutStep("check", "todo", `${f.forced.length} to check`, `${titles} did not fit the gaps and ${f.forced.length === 1 ? "was" : "were"} placed from the catalogue.`);
  }
  const blocking = "Nothing placed from the catalogue is waiting on your ear.";
  const byLength = f.tracks.filter(placedByLength);
  if (byLength.length) {
    return cutStep("check", "done", `${byLength.length} by length`, `${namedList(byLength.map((t) => t.title))} had no gap near a boundary, so ${byLength.length === 1 ? "it was" : "they were"} placed by length. Worth a listen, and Checked records that you did; Cut does not wait on ${byLength.length === 1 ? "it" : "them"}.`);
  }
  return cutStep("check", "done", f.tracks.some((t) => t.settled === "ear") ? "Checked" : "Clear", blocking);
}

// Never disabled by an unsaved edit: pressing it saves first. A track still to check does
// stop it, and the server says the same to anything that asks past this button. ADR-064.
function railCut(f) {
  const n = counted(f.tracks.length, "track", "tracks");
  const files = counted(f.review ?? 0, "file", "files");
  $("#split").disabled = !S.slug || !f.tracks.length || cutCutting || !!f.overlaps.length || !!f.forced.length || !!f.earlier.length;
  blue("#split", !!f.tracks.length && !f.review && (!f.imported || !!f.arc?.captured_again) && !f.forced.length && !f.overlaps.length && !f.earlier.length);
  if (cutCutting) return cutStep("cut", "run", "Cutting", `${n} going into review/.`);
  if (!f.tracks.length) return cutStep("cut", "wait", "Waiting", "Waiting on the first pass.");
  if (f.overlaps.length) return cutStep("cut", "todo", "Overlap", `Tracks ${namedList(f.overlaps.map((t) => t.number))} overlap. Move a boundary so they do not, and this can run.`);
  // Cut again from the new audio, this would be times from the old: being in the library
  // already is no reason to let it, and a re-rip is when a filed record gets here.
  if (f.earlier.length) {
    const held = f.earlier.length === 1 ? "1 track still has its time" : `${f.earlier.length} tracks still have their times`;
    const align = $("#realign").hidden ? "" : ", or Align from archive";
    return cutStep("cut", "wait", "Waiting", `Side ${namedList(sidesFromEarlier())} was captured again, so ${held} from the earlier capture. Run First pass${align}. Anything you have moved stays where it is; a track you have listened to on this capture can be marked Checked.`);
  }
  if (f.forced.length && !f.imported) return cutStep("cut", "wait", "Waiting", `Waiting on ${f.forced.length} to check.`);
  if (f.review) return f.recut
    ? cutStep("cut", "ready", "Changed", `${files} in review/, older than what you see.`)
    : cutStep("cut", "done", "Cut", `${files} in review/.`);
  if (f.arc?.captured_again) return cutStep("cut", "ready", "Ready", `${n} will be written to review/ from this capture. The library's came from an earlier one.`);
  if (f.imported) return cutStep("cut", "done", "Cut", "The files have moved to the library.");
  return cutStep("cut", "ready", "Ready", `${n} will be written to review/.`);
}

function railImport(f) {
  // Unknown is not nothing: if the server could not say, the button stays pressable.
  $("#import").disabled = !S.slug || (f.review === 0 && !f.imported) || !!f.arc?.capturing;
  if (f.arc?.capturing) return cutStep("import", "wait", "Waiting", f.arc.why);
  blue("#import", !!f.review);
  if (f.imported && f.review) return cutStep("import", "ready", "Not filed", `${counted(f.review, "cut track", "cut tracks")} in review/ and not in the library yet. What the library holds came from an earlier import.`);
  if (f.imported) return cutStep("import", "done", "In library", `${counted(f.arc.library_tracks, "track", "tracks")} in the library.`);
  if (f.review) return cutStep("import", "ready", "Ready", "Tag the cut tracks and place them in the library.");
  return cutStep("import", "wait", "Waiting", "Waiting on the cut.");
}

function railArchive(f) {
  blue("#archive", !!f.arc?.ready);
  if (f.archived) return cutStep("archive", "done", "Archived", "The raw sides are in the archive.");
  if (f.arc?.ready) {
    const going = (f.arc.will_replace || []).map((n) => n.replace(/^side-|\.flac$/g, ""));
    const replacing = going.length ? ` Side ${going.join(" and ")} replaces the earlier capture in the archive, which goes once the new one reads back.` : "";
    const unfiled = f.review ? ` The ${counted(f.review, "cut track", "cut tracks")} in review/ are cleared unfiled, and can be cut again.` : "";
    return cutStep("archive", "ready", "Ready", `Put the raw sides away and clean up.${replacing}${unfiled}`);
  }
  const why = f.arc?.why || (f.overlaps.length ? "Waiting on the overlapping tracks" : "Enabled once the album is in the library");
  return cutStep("archive", "wait", "Waiting", `${why[0].toUpperCase()}${why.slice(1).replace(/\.$/, "")}.`);
}

// The names are read from the fields, so the heading says what a save would write.
function showCutHead(f) {
  const artist = $("#md-artist").value.trim() || S.meta?.artist;
  const album = $("#md-album").value.trim() || S.meta?.album;
  $("#cut-what").textContent = S.slug
    ? (artist && album ? `${artist} \u2014 ${album}` : artist || album || S.slug)
    : "No record open";
  const pill = $("#cut-saved");
  const failed = saveFail && !saveFail.busy;
  pill.hidden = !S.dirty && !saveFail && !f.tracks.length && !f.archived;
  pill.dataset.kind = failed ? "bad" : S.dirty ? "todo" : "done";
  pill.querySelector("span").textContent = failed ? "Not saved"
    : saveFail ? "Waiting to save" : S.dirty ? "Saving" : f.archived ? "Archived" : "Saved";
  $("#cut-save-why").hidden = !saveFail;
  $("#cut-save-why").firstChild.textContent = saveFail ? `${saveFail.why} ` : "";
  $("#cut-save-retry").hidden = !failed;
  $("#cut-save-retry").textContent = saveFail?.stale ? "Load what is saved" : "Retry";
}

function showCutRail() {
  const tracks = Object.values(S.bySide).flat();
  const { review, arc, recut } = cutAsked;
  const f = {
    sides: S.meta?.sides ?? [],
    recording: S.meta?.recording ?? [],
    tracks,
    forced: tracks.filter((t) => t.forced),
    earlier: tracks.filter(fromEarlier),
    overlaps: tracks.filter((t) => trackOverlaps(t).length),
    review,
    recut,
    arc,
    imported: !!arc?.library_tracks,
    archived: albumsHeld.find((a) => a.slug === S.slug)?.where === "archive",
  };
  railRip(f);
  railFirstPass(f);
  railCheck(f);
  railCut(f);
  railImport(f);
  railArchive(f);
  showCutHead(f);
  const now = CUT_STEPS.find((id) => cutBox(id).dataset.kind !== "done");
  railBarSays($("#cut-rail-bar"), now ? cutBox(now) : null);
}

// What the Check step counts, all of it, in side order. A settled track has been heard.
function cutDoubts() {
  return (S.meta?.sides ?? [])
    .filter((letter) => !(S.meta.recording || []).includes(letter))
    .flatMap((letter) => (S.bySide[letter] || []).filter(canCheck).map((t) => ({ letter, t })));
}

// They come round from whichever is selected, as Tapes' do, forwards or back.
async function reviewNext(way) {
  const by = way === -1 ? -1 : 1;
  const doubts = cutDoubts();
  if (!doubts.length) return;
  const at = doubts.findIndex((d) => d.letter === S.side && d.t.number === ed.sel?.track);
  const from = at < 0 && by < 0 ? 0 : at;
  const { letter, t } = doubts[(from + by + doubts.length) % doubts.length];
  try {
    if (letter !== S.side) await openSide(letter);
    select(t.number, "start");
    ed.fitRange(t.start, t.end);
    document.querySelector("#tracks tr.sel")?.scrollIntoView({ block: "nearest" });
    // On a phone the open rail is in front of what was just chosen.
    $("#cut-rail").classList.remove("open");
    $("#cut-rail-bar").setAttribute("aria-expanded", false);
    $("#cut-wave").scrollIntoView({ block: "nearest" });
  } catch (e) { status(`could not open side ${letter}: ${e.message}`, true); }
}

function enableActions() {
  for (const id of ["#split", "#import", "#firstpass", "#addtrack", "#splittrack"])
    $(id).disabled = false;
}

// The old-format notice is a one-shot. It describes a permanent condition -
// those albums are finished and archived - so repeating it on every refresh is
// noise, not information.
let oldFormatNoted = false;

let albumsHeld = [];

// The only way to say "a record that does not exist yet": the endpoint lists
// directories that already hold a side, so a new one cannot come from it.
const NEW_ALBUM = "";

function renderAlbums(keep) {
  const sel = $("#album");
  const q = $("#album-q").value.trim().toLowerCase();
  sel.innerHTML = "";
  const fresh = el("option", "", "— new album —");
  fresh.value = NEW_ALBUM;              // an option without one takes its label
  sel.appendChild(fresh);

  let shown = 0;
  for (const a of albumsHeld) {
    const name = a.artist && a.album ? `${a.artist} — ${a.album}` : a.slug;
    const label = a.where === "archive" ? `${name}  (archived)` : name;
    const hit = !q || label.toLowerCase().includes(q) || a.slug.toLowerCase().includes(q);
    // The record in hand stays selectable whatever the filter says, or typing
    // would quietly change which record the page is about.
    if (!hit && a.slug !== keep) continue;
    const o = el("option", "", label);
    o.value = a.slug;
    sel.appendChild(o);
    if (hit) shown++;
  }
  sel.value = albumsHeld.some((a) => a.slug === keep) ? keep : NEW_ALBUM;
  $("#album-q").placeholder = q && !shown ? "nothing matches that" : "filter…";
  return sel.value;
}

// `keep` names the record to land on. Omit it to stay where you are - which on
// a fresh load is nothing, because the page opens ready to start a record
// rather than aimed at whichever one happens to sort first.
async function refreshAlbums(keep) {
  const { albums } = await api("/api/albums?archive=1");
  albumsHeld = albums.filter((a) => !a.old_format);
  const want = keep === undefined ? (S.slug || NEW_ALBUM) : keep;
  const pick = renderAlbums(want);

  // Only worth mentioning for albums still in raw/, where an unreadable plan
  // actually blocks work. An archived album is finished - imported and filed -
  // so its plan format is nobody's problem and saying so is just noise.
  const stuck = albums.filter((a) => a.old_format && a.where !== "archive");
  if (stuck.length && !oldFormatNoted) {
    oldFormatNoted = true;
    logline(`not editable here, superseded "cuts" plan format: `
            + `${stuck.map((a) => a.slug).join(", ")} — run First pass again to rebuild it`);
  }
  if (pick === NEW_ALBUM) { startNewAlbum(); return; }
  await openAlbum(pick);
}

// Nothing selected is a real state rather than an empty one: it is where you
// are before the first side of a record exists.
function startNewAlbum() {
  clearAlbum("");
  $("#album").value = NEW_ALBUM;
  for (const id of ["#rip-artist", "#rip-album"]) {
    $(id).value = ""; $(id).readOnly = false; $(id).title = "";
  }
  setSideChoices([]);
  loadPunchTracks();            // it follows the selection, and there is none
  const none = !albumsHeld.length;
  status(none ? "no records yet — rip a side to start one" : "", none);
}

let cutOpening = 0;

async function openAlbum(slug) {
  cutOpening += 1;
  try { await openedAlbum(slug); } finally { cutOpening -= 1; }
}

async function openedAlbum(slug) {
  // the transcript refers to the old album's tracks by number; replaying it
  // against a different record is worse than starting fresh
  S.slug = slug;
  status("loading…", true);
  S.meta = await api(`/api/album/${slug}`);
  // What the record is, before anything is encoded. Building a waveform takes
  // minutes on a fresh side, and all of this comes from the document.
  showRecord();
  // A record opens whether or not its waveform could be built. Letting this
  // through left the header on "loading…" and every panel below it showing the
  // previous record - the side tabs, the captures on disk, the re-rip picker -
  // because none of the code after this point ran.
  let unprepared = "";
  try {
    await ensurePrepared();
  } catch (e) {
    unprepared = e.message;
    logline(`could not prepare ${slug}: ${e.message}`);
  }
  S.sideData = {};
  renderSideTabs();             // the record was re-read; a side may have stopped
  const rec = S.meta.recording || [];
  const first = S.meta.sides.find((s) => !rec.includes(s));
  if (first) await openSide(first);
  else status(`every side of ${slug} is still recording`, true);
  loadReview();
  refreshAlignButton();
  refreshArchiveButton();
  refreshCutRail();             // the encode may have taken minutes
  markDirty(false);
  status(unprepared ? `${slug} is open, but its waveform is not: ${unprepared}` : "",
         !!unprepared);
}

// Everything a record's own document answers: its names, its sides, and what
// the Rip and Punch panels are aimed at. None of it waits on an encode, and it
// used to - so choosing a record left both panels describing the previous one
// until the waveform finished. ADR-051.
function showRecord() {
  S.bySide = {};
  for (const letter of S.meta.sides) {
    S.bySide[letter] = (S.meta.tracks_by_side[letter] || []).map((t) => ({ ...t }));
  }
  enableActions();
  $("#md-artist").value = S.meta.artist || "";
  $("#md-album").value = S.meta.album || "";
  $("#md-date").value = S.meta.date || "";
  loadArtState();
  renderSideTabs();
  loadRipSides();               // per-album, so it follows the selection
  // The record is open, so the side it has not got yet is the one you are
  // about to record.
  showRecordInRipPanel(S.meta.artist, S.meta.album, S.meta.sides);
  loadPunchTracks();            // Punch follows the selection too
  cutAsked = { review: null, arc: null };
  showCutRail();
  refreshCutRail();             // none of it waits on an encode either
}

function renderSideTabs() {
  const box = $("#sides"); box.innerHTML = "";
  const rec = S.meta.recording || [];
  for (const letter of S.meta.sides) {
    const recording = rec.includes(letter);
    const b = el("button", `${letter === S.side ? "on " : ""}${recording ? "rec" : ""}`,
                 recording ? `Side ${letter} ●rec` : `Side ${letter}`);
    b.disabled = recording;
    b.title = recording ? "still being recorded — stop the rip, and it shows up here" : `side ${letter}`;
    b.onclick = () => openSide(letter);
    box.appendChild(b);
  }
}

async function openSide(letter) {
  S.side = letter;
  renderSideTabs();
  if ((S.meta.recording || []).includes(letter)) {
    status(`side ${letter} is still recording — stop the rip, and it shows up here`, true);
    return;
  }
  player?.stop();          // otherwise the old side keeps playing under the new one
  cutOpen = null;
  if (!S.sideData[letter]) {
    const v = S.meta.ready[letter]?.v || "";
    const [env, gaps] = await Promise.all([
      // ?v= is the capture's fingerprint: same audio -> cache hit, re-rip -> miss
      fetchEnvelope(`/api/env/${S.slug}/${letter}${v ? `?v=${v}` : ""}`,
                    S.meta.ready[letter]?.windows),
      api(`/api/gaps/${S.slug}/${letter}`),
    ]);
    S.sideData[letter] = { ...env, gaps, duration: S.meta.ready[letter].duration };
  }
  const d = S.sideData[letter];
  ed.load(d, S.bySide[letter]);
  player.load(`/api/audio/${S.slug}/${letter}`
              + (S.meta.ready[letter]?.v ? `?v=${S.meta.ready[letter].v}` : ""));
  $("#sp-letter").textContent = letter;
  renderTracks();
}

function showSideMeta() {
  const d = S.sideData[S.side];
  if (!d) return;
  $("#sp-meta").textContent = [`side ${S.side}`, fmt(d.duration), counted(tracks().length, "track", "tracks"),
    counted(d.gaps.band.gaps.length, "silent region", "silent regions"), `threshold ${d.gaps.band.threshold} dB`].join(" · ");
}

// ------------------------------------------------------------ track table

function tracks() { return S.bySide[S.side] || []; }
function trackByNum(n) { return tracks().find((t) => t.number === n); }

// Nothing settled it, so the fit did, or the catalogue if the track is forced. A plan that
// does not say what the fit rested on is only called fitted. ADR-064.
const BOUNDARY_WORDS = { hand: ["Set by you", "good"], ear: ["Checked by ear", "good"] };
const PLACED_WORDS = { gap: ["Fitted to gap", "good"], length: ["Placed by length", "quiet"], earlier: ["From an earlier capture", "warn"] };
function trackBoundary(t) {
  const over = trackOverlaps(t);
  if (over.length) return [`Overlaps ${namedList(over.map((x) => x.number))}`, "bad"];
  return BOUNDARY_WORDS[t.settled] ?? (fromEarlier(t) ? PLACED_WORDS.earlier
    : t.forced ? ["From catalogue · check", "warn"] : PLACED_WORDS[t.placed] ?? ["Fitted", "good"]);
}
const trackOverlaps = (t) => overlapping(t, sideOf(t)).sort((a, b) => a.start - b.start);
const sideOf = (t) => Object.values(S.bySide).find((list) => list.includes(t)) ?? [];
const placedByLength = (t) => !t.settled && !t.forced && t.placed === "length";

let saidOverlap = false;

// A boundary moved is the person's, and nothing later moves it.
function setByHand(t) {
  t.forced = false;
  t.settled = "hand";
  t.placed = "";
  const over = trackOverlaps(t);
  if (over.length) status(`${t.number} \u201c${t.title}\u201d still overlaps ${over.map((x) => `${x.number} \u201c${x.title}\u201d`).join(" and ")}. Cut will not run until they no longer overlap.`, true);
  else if (saidOverlap) status("no tracks overlap now");
  saidOverlap = !!over.length;
}

// A move past the track beside it does not happen, whichever way it was asked for, and
// says why. Nothing else moved, so nothing was decided for them. ADR-064.
function refusedCrossing(t, edge, at) {
  const other = crossed(t, edge, at, sideOf(t));
  if (other) {
    const which = edge === "start" ? "ends" : "starts";
    status(`Not moved: that overlaps track ${other.number} \u201c${other.title}\u201d, which ${which} at ${fmt(other[edge === "start" ? "end" : "start"])}. Move that one first.`, true);
  }
  return !!other;
}

// The row is read-only, and what edits it opens beneath it, one at a time. The open track is
// held by the object, since a split renumbers what comes after it.
let cutOpen = null;
let cutRedrawing = false;             // inputs leaving the page now are not a person leaving them
let cutPressing = false, cutRedrawOwed = false;
const trackKeys = new WeakMap();
let trackKeysMade = 0;
function trackKey(t) {
  if (!trackKeys.has(t)) trackKeys.set(t, `track-${++trackKeysMade}`);
  return trackKeys.get(t);
}

function trackRowView(t) {
  const tr = el("tr", ed.sel && ed.sel.track === t.number ? "sel" : "");
  tr.onclick = () => pressRow(t);
  tr.append(el("td", "c-num num", t.number));
  const title = el("td", "c-title", t.title);
  title.title = t.title;
  tr.append(title);
  for (const edge of ["start", "end"]) {
    const c = el("td", `t t-${edge}`, fmt(t[edge]));
    c.title = `${t[edge].toFixed(2)} s`;
    c.onclick = (e) => { e.stopPropagation(); select(t.number, edge, true); ed.fitRange(t.start, t.end); };
    tr.append(c);
  }
  const len = t.end - t.start;
  tr.append(el("td", "t t-len", fmt(len)));
  const dr = el("td", "drift");
  if (t.forced) {
    dr.textContent = "—";
    dr.title = "placed from the catalogue, not found — this column is zero by "
             + "construction and cannot tell you whether the boundary is right";
  } else if (t.cat) {
    const d = len - t.cat;
    dr.textContent = (d >= 0 ? "+" : "") + d.toFixed(2);
    if (Math.abs(d) > 10) dr.className = "drift big";
  } else dr.textContent = "—";
  tr.append(dr);
  const bnd = statusCell(trackBoundary(t));
  bnd.classList.add("c-bnd-cell");
  if (fromEarlier(t)) bnd.title = "this side was captured again after this was placed, so these times are positions on audio that has gone: run First pass or align, or listen to it here and press Checked";
  else if (t.forced) bnd.title = "placed from the catalogue, not found: check it by ear, and moving it clears this";
  else if (placedByLength(t)) bnd.title = "no gap near this boundary, so its place comes from the catalogue's length: worth a listen, and Checked records that you did. Cut does not wait on it";
  tr.append(bnd);
  return tr;
}

// Pressing the open row again leaves it as it is, marker and view included.
function pressRow(t) {
  if (cutOpen === t && ed.sel?.track === t.number) return;
  select(t.number, "start");
  ed.fitRange(t.start, t.end);
  // Stacked on a phone, the waveform is not beside the row that was pressed.
  if (matchMedia("(max-width:48rem)").matches) $("#cut-wave").scrollIntoView({ block: "nearest" });
}

// A field is the page's own from the first key to when it is left, which writes it into the
// track. Writing a figure as it is typed would drag the marker through every digit.
function trackField(t, label, name, wide, shown, write) {
  const [box, input] = fieldInput(label, name, shown, wide);
  const leave = () => {
    if (cutRedrawing || input.value === input.dataset.saved) return;
    const changed = write(input) !== false;
    input.dataset.saved = input.value;
    if (!changed) return;
    markDirty();
    ed.render(); updateReadout(); renderTracks();
  };
  input.onblur = leave;
  input.onkeydown = (e) => { if (e.key === "Enter") leave(); };
  return [box, input];
}

function renumber(t, input) {
  const was = t.number;
  const now = parseInt(input.value, 10);
  if (!(now > 0) || now === was) { input.value = was; return false; }
  t.number = now;
  if (ed.sel?.track === was) ed.sel.track = now;
  if (cutPicked?.track === was) cutPicked.track = now;
}

// Bound as a drag is: inside the side, and never past the track's other edge.
function typedEdge(t, edge, input) {
  const v = parseFloat(input.value);
  const at = Math.max(0, Math.min(S.sideData[S.side].duration, v));
  const same = !Number.isFinite(v) || round2(v) === round2(t[edge]) || refusedByJob() || refusedCrossing(t, edge, at);
  if (!same) {
    t[edge] = round2(edge === "start" ? Math.min(at, t.end - 0.2) : Math.max(at, t.start + 0.2));
    setByHand(t);
  }
  input.value = t[edge].toFixed(2);
  return !same;
}

function removeTrack(t) {
  if (refusedByJob()) return;
  if (!confirm(`Delete track ${t.number} “${t.title}”?`)) return;
  S.bySide[S.side] = tracks().filter((x) => x !== t);
  if (cutOpen === t) cutOpen = null;
  if (ed.sel?.track === t.number) { ed.sel = null; cutPicked = null; }
  markDirty(); renderTracks(); ed.setTracks(tracks()); updateReadout();
}

function trackOpenView(t) {
  const tr = el("tr", "cut-open");
  tr.dataset.open = trackKey(t);
  tr.dataset.key = trackKey(t);
  const td = el("td");
  td.colSpan = 7;
  const fields = el("div", "cut-fields");
  const [num, numInput] = trackField(t, "Number", "number", false, String(t.number), (input) => renumber(t, input));
  numInput.type = "number"; numInput.min = "1"; numInput.step = "1";
  const [title] = trackField(t, "Title", "title", true, t.title, (input) => { t.title = input.value; });
  fields.append(num, title);
  for (const [edge, label] of [["start", "Start (s)"], ["end", "End (s)"]]) {
    const [box, input] = trackField(t, label, edge, false, t[edge].toFixed(2), (i) => typedEdge(t, edge, i));
    input.type = "number"; input.step = "0.01"; input.min = "0";
    input.dataset.edge = edge;
    fields.append(box);
  }
  const moves = el("div", "cut-moves");
  moves.append(smallButton("Remove", "delete this track", () => removeTrack(t), "ghost tiny tp-danger"),
               smallButton("Close", "close this row", () => { cutOpen = null; renderTracks(); }));
  fields.append(moves);
  td.append(fields);
  tr.append(td);
  return tr;
}

function renderTracks() {
  showSideMeta();
  showCutTools();
  // A field left by pressing something in this table writes itself, and taking away the
  // button under the pointer would lose the press. The rows wait for the press to finish.
  if (cutPressing) { cutRedrawOwed = true; return; }
  const tb = $("#tracks tbody");
  const typed = typedInRows(tb);
  const rows = [];
  for (const t of tracks()) {
    rows.push(trackRowView(t));
    if (cutOpen === t) rows.push(trackOpenView(t));
  }
  cutRedrawing = true;
  tb.replaceChildren(...rows);
  cutRedrawing = false;
  putBackTyped(typed, tb);
}

function wireTrackPresses() {
  $("#tracks").addEventListener("pointerdown", () => { cutPressing = true; });
  const finished = () => {
    if (!cutPressing) return;
    // After the click this press is about to make, which fires once this event is over.
    setTimeout(() => {
      cutPressing = false;
      if (cutRedrawOwed) { cutRedrawOwed = false; renderTracks(); }
    });
  };
  addEventListener("pointerup", finished);
  addEventListener("pointercancel", finished);
}

// ------------------------------------------------------------------ toolbar
//
// The buttons and the keys are two ways to one set of actions.

const selectedTrack = () => (ed.sel ? trackByNum(ed.sel.track) ?? null : null);

function playOrPause() {
  const t = selectedTrack();
  player.toggle(player.playing ? null : (t ? t[ed.sel.edge] - 3 : ed.playhead));
}

function auditionSelected() {
  const t = selectedTrack();
  if (t) player.audition(t[ed.sel.edge]);
}

// A nudge sets a boundary for good, so a track chosen from its row is not enough: the marker
// has to have been asked for, by pressing it, by its Start or End cell or button, or by E.
let cutPicked = null;
const cutArmed = () => !!ed.sel && cutPicked?.track === ed.sel.track && cutPicked.edge === ed.sel.edge;

function nudgeSelected(step) {
  const t = selectedTrack();
  if (!t) return false;
  if (!cutArmed()) {
    status("Choose Start or End first (E), or press a marker, and then this moves it.", true);
    return false;
  }
  if (refusedByJob() || refusedCrossing(t, ed.sel.edge, t[ed.sel.edge] + step)) return true;
  if (ed.sel.edge === "start") t.start = Math.min(t.start + step, t.end - 0.2);
  else t.end = Math.max(t.end + step, t.start + 0.2);
  setByHand(t);
  markDirty(true, NUDGE_SETTLE); ed.render(); updateReadout(); renderTracks();
  return true;
}

function chooseEdge(edge) {
  if (ed.sel) select(ed.sel.track, edge, true);
}

// The first press chooses the marker the readout is showing, and the next ones swap it.
function swapEdge() {
  if (!ed.sel) return;
  const chosen = cutPicked?.track === ed.sel.track;
  chooseEdge(chosen ? (ed.sel.edge === "start" ? "end" : "start") : ed.sel.edge);
}

// Stops at either end of the side, and a whole-side view stays one.
function stepTrack(by) {
  const list = tracks();
  if (!list.length) return;
  const at = list.findIndex((t) => t.number === ed.sel?.track);
  const next = list[at < 0 ? (by > 0 ? 0 : list.length - 1) : Math.max(0, Math.min(list.length - 1, at + by))];
  const whole = !!ed.side && ed.t1 - ed.t0 >= ed.side.duration - 0.01;
  select(next.number, "start");
  if (!whole) ed.fitRange(next.start, next.end);
  document.querySelector("#tracks tr.sel")?.scrollIntoView({ block: "nearest" });
}

// The boundaries stay, and the doubt goes. It is an edit like any other.
function checkSelected() {
  const t = selectedTrack();
  if (!canCheck(t)) return;
  if (refusedByJob()) return;
  t.forced = false;
  t.settled = "ear";
  markDirty(); updateReadout(); renderTracks();
  status(`“${t.title}” checked by ear`);
}

function zoomToSelected() {
  const t = selectedTrack();
  if (t) ed.fitRange(t.start, t.end);
}

function showCutTools() {
  const none = !selectedTrack();
  for (const id of ["#cut-audition", "#cut-zoom-track"]) $(id).disabled = none;
  for (const id of ["#cut-prev", "#cut-next"]) $(id).disabled = !tracks().length;
  const armed = cutArmed();
  for (const edge of ["start", "end"]) {
    const b = $(`#cut-edge-${edge}`);
    b.disabled = none;
    b.classList.toggle("on", armed && ed.sel.edge === edge);
  }
  for (const id of ["#cut-minus", "#cut-plus"]) $(id).disabled = !armed;
  for (const id of ["#cut-play", "#cut-split", "#cut-zoom-side", "#cut-in", "#cut-out"]) $(id).disabled = !ed.side;
  const doubts = cutDoubts().length;
  $("#cut-doubt").disabled = !doubts || !ed.side;
  toolLabel("#cut-doubt", doubts ? `Next doubt (${doubts})` : "No doubts");
  $("#cut-checked").disabled = !canCheck(selectedTrack());
  $("#cut-checked").classList.toggle("primary", !!selectedTrack()?.forced);
  $("#cut-play").querySelector("span").textContent = player.playing ? "Pause" : "Play";
}

const round2 = (v) => Math.round(v * 100) / 100;

// Split the track under the playhead in two. Where the edges go is ripui.js's.
function splitAtPlayhead() {
  if (refusedByJob()) return;
  const at = ed.playhead;
  const list = tracks();
  const t = list.find((x) => x.start < at && at < x.end);
  if (!t) { status("playhead is not inside a track — click inside one first", true); return; }
  const edges = splitEdges(t, at, S.sideData[S.side].gaps?.band?.gaps);
  if (edges.refused) { status(edges.refused, true); return; }

  // everything at or after the insertion point shifts up one, across all sides
  const newNum = t.number + 1;
  for (const x of Object.values(S.bySide).flat()) if (x.number >= newNum) x.number += 1;

  list.push({ number: newNum, title: "Untitled", start: round2(edges.start), end: t.end, settled: "hand" });
  t.end = round2(edges.end);
  setByHand(t);
  list.sort((a, b) => a.start - b.start);
  markDirty(); renderTracks(); ed.setTracks(list);
  select(newNum, "start");
  status(edges.note);
}

function select(num, edge, picked = false) {
  ed.sel = num == null ? null : { track: num, edge };
  cutPicked = picked && num != null ? { track: num, edge } : null;
  if (num != null) cutOpen = trackByNum(num) ?? cutOpen;
  ed.render(); renderTracks(); updateReadout();
}

// ---------------------------------------------------------------- readout

function updateReadout() {
  const sel = ed.sel;
  const armed = cutArmed();
  $("#ro-armed").textContent = armed ? `${sel.edge === "start" ? "Start" : "End"} chosen` : "";
  $("#ro-armed").className = armed ? sel.edge : "";
  if (!sel) {
    $("#ro-time").textContent = fmt(ed.playhead);
    $("#ro-track").textContent = ""; $("#ro-gap").textContent = ""; $("#ro-gap").className = "";
    return;
  }
  const t = trackByNum(sel.track);
  if (!t) return;
  const v = t[sel.edge];
  $("#ro-time").textContent = `${fmt(v)}  ${v.toFixed(2)}s`;
  const len = t.end - t.start;
  const d = t.cat ? len - t.cat : null;
  $("#ro-track").textContent =
    `${armed ? `${sel.edge} of ` : ""}${t.number} “${t.title}” — ${len.toFixed(2)}s` +
    (t.forced ? "  — forced, placed from the catalogue; check it by ear"
     : d == null ? "" : ` (cat ${t.cat.toFixed(2)}, ${d >= 0 ? "+" : ""}${d.toFixed(2)})`);

  sayGap($("#ro-gap"), v, S.sideData[S.side].gaps.band.gaps);
}

// the nudge2 question: is this cut still inside a real silence?
function sayGap(ro, v, gs) {
  let inside = null, nearest = null, nd = 1e9;
  for (const g of gs) {
    if (g.lo <= v && v <= g.hi) { inside = g; break; }
    const dd = Math.min(Math.abs(v - g.lo), Math.abs(v - g.hi));
    if (dd < nd) { nd = dd; nearest = g; }
  }
  if (inside) {
    ro.className = "good";
    ro.textContent = `in gap ${inside.lo.toFixed(2)}–${inside.hi.toFixed(2)} (margin ${Math.min(v - inside.lo, inside.hi - v).toFixed(2)}s)`;
  } else {
    ro.className = "bad";
    ro.textContent = nearest
      ? `NOT in a gap — nearest ${nearest.lo.toFixed(2)}–${nearest.hi.toFixed(2)}, ${nd.toFixed(2)}s away`
      : "NOT in a gap";
  }
}

// ----------------------------------------------------------------- artwork

async function loadArtState() {
  const box = $("#art-state");
  box.textContent = "—";
  if (!S.slug) return;
  try {
    const r = await api(`/api/review/${S.slug}`);
    box.textContent = r.tracks.length
      ? `${r.tracks.length} cut files ready for art`
      : "nothing cut yet — art attaches to the library copy";
  } catch { box.textContent = "—"; }
}

async function uploadArt(file) {
  $("#art-err").textContent = "";
  $("#art-state").textContent = `uploading ${file.name}…`;
  try {
    const buf = await file.arrayBuffer();
    const r = await fetch(`/api/artwork/${S.slug}/upload`, {
      method: "POST", credentials: "same-origin",
      headers: { "content-type": file.type || "application/octet-stream" },
      body: buf,
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || `${r.status}`);
    $("#art-state").textContent =
      `${j.size} · embedded in ${j.embedded}/${j.tracks} tracks`;
    // the import dialog can trigger this too, and it is modal - so the Album
    // panel underneath is exactly where the result cannot be seen
    if (!$("#imp-art-wrap").hidden) {
      $("#imp-art-what").textContent =
        `${j.size} · embedded in ${j.embedded}/${j.tracks} tracks`;
      $("#imp-art-wrap").hidden = j.embedded === j.tracks;
    }
    status("cover art embedded");
  } catch (e) {
    $("#art-err").textContent = e.message;
    if (!$("#imp-art-wrap").hidden) $("#imp-art-err").textContent = e.message;
    loadArtState();
  }
}

// ------------------------------------------------------------------ review

async function loadReview() {
  const box = $("#review");
  try {
    const r = await api(`/api/review/${S.slug}`);
    box.innerHTML = "";
    if (!r.tracks.length) { box.appendChild(el("p", "dim", "nothing cut yet")); return; }
    for (const t of r.tracks) {
      const a = el("a", "", t.name);
      a.href = "#";
      a.onclick = (e) => {
        e.preventDefault();
        player.stop();
        player.load(`/api/review/${S.slug}/${t.index}`);
        player.toggle(0);
        status(`playing ${t.name}`);
      };
      box.appendChild(a);
    }
  } catch { box.innerHTML = ""; box.appendChild(el("p", "dim", "nothing cut yet")); }
}

// ------------------------------------------------------------------ save

function buildDoc() {
  return {
    album: $("#md-album").value.trim() || S.meta.album,
    artist: $("#md-artist").value.trim() || S.meta.artist,
    date: $("#md-date").value.trim() || S.meta.date,
    mbid: S.meta.mbid || "",
    sides: S.meta.sides.map((letter) => ({
      letter,
      tracks: (S.bySide[letter] || []).slice().sort((a, b) => a.start - b.start),
    })).filter((s) => s.tracks.length),
  };
}

// Waits for what is on screen to be on disk, and reports whether it got there, so a caller
// that must not proceed on a stale plan can stop.
async function save() {
  let landed = await flushSave();
  while (landed && S.dirty) landed = await flushSave();
  if (!landed) status(`Not saved: ${saveFail?.why}`, true);
  return landed;
}

async function split() {
  // The cutter reads the plan from disk, so an unsaved edit is not in it. This
  // used to ask whether to cut from the last saved copy instead, which is a
  // question with one sensible answer and a dozen chances to press the wrong
  // one - three nudged boundaries and an absent-minded OK cut the old ones.
  if (S.dirty && !(await save())) return;
  cutCutting = true;
  showCutRail();
  status("cutting…", true);
  try {
    const r = await runJob(S.slug, `/api/split/${S.slug}`, {}, "cutting");
    const bad = r.unreadable || [];
    status(`${r.tracks} tracks cut` + (bad.length ? ` - ${bad.length} unreadable` : ""),
           bad.length > 0);
    // Named rather than counted: a track that will not decode is one to cut
    // again, and knowing which costs nothing to report.
    if (bad.length) logline(`these did not read back: ${bad.join(", ")}`, true);
    loadReview();
  } catch (e) { status(`cut failed: ${e.message}`, true); logline(e.message, true); }
  cutCutting = false;
  await refreshCutRail();
}

// ----------------------------------------------------------------- archive

async function refreshArchiveButton() {
  const b = $("#archive");
  b.disabled = true;
  if (!S.slug) return;
  try {
    const st = await api(`/api/archive/${S.slug}?album=${encodeURIComponent(S.meta.album || "")}`);
    b.disabled = !st.ready;
    b.title = st.ready
      ? `archive ${st.sides.length} raw sides and clean up`
      : (st.why || "not ready");
  } catch { /* leave it disabled */ }
}

async function openArchive() {
  $("#arc-err").textContent = "";
  $("#arc-plan").textContent = "checking…";
  $("#arcdlg").showModal();
  try {
    const st = await api(`/api/archive/${S.slug}?verify=1&album=${encodeURIComponent(S.meta.album || "")}`);
    const lines = [];
    lines.push(`library: ${st.library_path || "(not found)"}  — ${st.library_tracks} tracks`);
    lines.push("");
    lines.push("archive to " + st.will_archive_to + ":");
    for (const sd of st.sides)
      lines.push(`  ${sd.name.padEnd(22)} ${(sd.bytes / 1e6).toFixed(0)} MB   `
        + (sd.valid === null ? "" : sd.valid ? "valid — copy as-is" : "TRUNCATED — re-encode"));
    if ((st.will_replace || []).length) {
      lines.push("");
      lines.push("replacing in the archive (the earlier take goes):");
      for (const name of st.will_replace) lines.push("  " + name);
    }
    lines.push("");
    lines.push("then remove:");
    for (const d of st.will_remove) lines.push("  " + d);
    if (!st.ready) lines.push("\nNOT READY: " + st.why);
    lastSurvey = st;
    $("#arc-plan").textContent = lines.join("\n");
    $("#arc-go").disabled = !st.ready;
  } catch (e) {
    $("#arc-plan").textContent = "";
    $("#arc-err").textContent = e.message;
  }
}

let lastSurvey = {};

async function archiveRun() {
  // S.slug moves when the picker is rebuilt below, so remember what was archived
  const done = S.slug;
  const replacing = (lastSurvey.will_replace || []).length;
  if (!confirm("Archive the raw sides and delete raw/, review/ and the cache?\n\n"
             + "Each side is verified in archive/ before anything is removed."
             + (replacing
                ? `\n\n${replacing} archived side(s) are being replaced. The earlier `
                  + `take is kept until the new one reads back, then goes.`
                : ""))) return;
  $("#arc-err").textContent = "";
  $("#arc-go").disabled = true;
  $("#arc-plan").textContent = "archiving — re-encoding truncated sides, this takes a minute…";
  try {
    const r = await runJob(S.slug, `/api/archive/${S.slug}`,
                             { album: S.meta.album }, "archiving");
    const lines = ["archived to " + r.archive_dir, ""];
    for (const a of r.archived) lines.push(`  ${a.side.padEnd(22)} ${a.seconds}s  ${(a.bytes / 1e6).toFixed(0)} MB  verified`);
    lines.push("", ...r.notes.map((n) => "  " + n), "", "removed:", ...r.removed.map((d) => "  " + d));
    $("#arc-plan").textContent = lines.join("\n");
    await refreshCutRail();
    $("#arcdlg").close();
    resetCaptureAfterArchive(done);
    await refreshAlbums(NEW_ALBUM);   // that record is finished; start clean

    await loadOrphans();
    status("archived and cleaned up");   // last, since opening the picker's choice clears it
  } catch (e) {
    $("#arc-err").textContent = e.message;
    $("#arc-go").disabled = false;
  }
}

// ------------------------------------------------------------------ import

// Whether beets will be asked to match this record, or told not to. The
// difference decides what the tags say, so it is said in the dialog rather
// than inferred from an empty field. ADR-049.
function showCatalogueState() {
  const byPlan = $("#imp-fromplan").checked;
  $("#imp-nocat").hidden = !byPlan && !!$("#imp-mbid").value.trim();
  // Left readable rather than removed: which release it would have used is
  // worth seeing while deciding not to use it.
  for (const id of ["#imp-mbid", "#imp-search"]) $(id).disabled = byPlan;
  const noRelease = byPlan || !$("#imp-mbid").value.trim();
  const merge = $('#imp-dupes option[value="merge"]');
  merge.disabled = noRelease;
  $("#imp-nomerge").hidden = !noRelease;
  if (noRelease && $("#imp-dupes").value === "merge") $("#imp-dupes").value = "keep";
}

// The release the import will actually use. Empty is a decision here, not an
// absence, so it is sent as one - the server falls back to the spec only for a
// caller that says nothing at all.
function importMbid() {
  return $("#imp-fromplan").checked ? "" : $("#imp-mbid").value.trim();
}

function openImport() {
  $("#imp-mbid").value = S.meta.mbid || "";
  $("#imp-fromplan").checked = false;
  $("#imp-dupes").value = "replace";
  showCatalogueState();
  $("#imp-err").textContent = "";
  $("#imp-outwrap").hidden = true;
  $("#imp-done").hidden = true;
  $("#imp-pick").hidden = false;
  $("#imp-go").hidden = true;
  $("#imp-next").hidden = true;
  $("#imp-go").hidden = false;
  $("#imp-close").textContent = "Close";
  $("#imp-results").innerHTML = "";
  $("#imp-relabel-wrap").hidden = true;
  $("#imp-stale-wrap").hidden = true;
  // What the record is called, from the plan the import tags from. Editable,
  // they fed only the search box and a confirm quoting them back. ADR-048.
  const g = guessFromSlug(S.slug);
  $("#imp-artist").value = S.meta.artist || g.artist;
  $("#imp-album").value = S.meta.album || g.album;
  $("#imp-artist").readOnly = $("#imp-album").readOnly = true;
  $("#imp-artist").title = $("#imp-album").title =
    "what the saved plan says — pick a release below and re-label to change it";
  $("#imp-replace").checked = false;
  $("#imp-go").disabled = false;
  // Hidden until this record has been asked about. Left showing, it was the
  // previous record's answer - naming another album's directory as the one
  // about to be written into - and it stayed if the question failed.
  $("#imp-replace-wrap").hidden = true;
  $("#imp-replace-what").textContent = "";
  $("#impdlg").showModal();
  checkExisting();
}

// A release picked at import time is usually picked because the one from First
// pass was wrong - most often a CD edition against an LP that carries extra
// tracks. Re-fitting is the wrong answer by then: the boundaries are already
// correct and hard-won. Offer a relabel whenever the counts differ, and say so
// plainly when they match so nothing needs doing.
let relabelPick = null;

function offerRelabel(rel) {
  relabelPick = rel;
  const mine = Object.values(S.bySide).flat().length;
  const wrap = $("#imp-relabel-wrap");
  wrap.hidden = false;
  $("#imp-relabel-what").textContent =
    `${rel.title}  ·  ${rel.date || "----"}  ·  ${rel.formats.join(",") || "?"}`
    + `\n${rel.tracks} tracks on the release   ·   ${mine} tracks in this cut`
    + (rel.tracks === mine ? "\nsame count — safe to re-label"
                           : "\nCOUNT DIFFERS — re-labelling will refuse rather "
                             + "than shift every title by one");
  $("#imp-relabel").disabled = rel.tracks !== mine;
}

async function relabelRun() {
  if (!relabelPick) return;
  if (!confirm(`Re-label ${Object.values(S.bySide).flat().length} tracks from `
             + `"${relabelPick.title}"?\n\nTitles, numbers and the release id `
             + `change. No boundary moves.`)) return;
  if (S.dirty && !(await save())) return;
  $("#imp-err").textContent = "";
  $("#imp-relabel").disabled = true;
  try {
    // The release is the whole request: the titles, the artist and the date all
    // come from it. Sending the fields as well implied they were consulted.
    const r = await postJSON(`/api/relabel/${S.slug}`, { mbid: relabelPick.id });
    $("#imp-relabel-wrap").hidden = true;
    S.sideData = {};
    await openAlbum(S.slug);
    $("#imp-mbid").value = r.mbid;
    showCatalogueState();
    checkExisting();
    status(`re-labelled ${r.applied} tracks from ${r.release} — boundaries untouched`);
  } catch (e) {
    $("#imp-err").textContent = e.message;
  }
  $("#imp-relabel").disabled = false;
}

// An import that lands with no art used to be reported nowhere, which meant
// finding out from a blank tile in a player days later. Ask at the moment it
// happens, and offer both answers.
// Offered after an import, the moment it is worth acting on. It was written
// and never called, so the panel could only appear from inside itself.
async function offerArt() {
  const wrap = $("#imp-art-wrap");
  $("#imp-art-err").textContent = "";
  wrap.hidden = true;
  try {
    const art = await api(`/api/artwork/${S.slug}`);
    const tracks = art.tracks || [];
    const withArt = tracks.filter((t) => t.has_art).length;
    if (!tracks.length || withArt === tracks.length) return;
    $("#imp-art-what").textContent =
      `${withArt} of ${tracks.length} tracks carry a picture`
      + (art.cover ? `   ·   ${art.cover.name} is on disk but not embedded` : "");
    wrap.hidden = false;
  } catch { /* the record may not be filed yet; the panel simply stays hidden */ }
}

async function artFetch() {
  $("#imp-art-err").textContent = "";
  const b = $("#imp-art-fetch");
  const was = b.textContent;
  b.disabled = true; b.textContent = "searching…";
  try {
    const r = await postJSON(`/api/artwork/${S.slug}/fetch`, {});
    $("#imp-art-what").textContent =
      `${r.size} from ${r.source} · embedded in ${r.embedded}/${r.tracks} tracks`;
    $("#imp-art-wrap").hidden = r.embedded === r.tracks;
    status("cover art fetched and embedded");
    loadArtState();
  } catch (e) {
    // the message carries what each source actually returned, which is the
    // difference between "try Bandcamp" and "upload something yourself"
    $("#imp-art-err").textContent = e.message;
  }
  b.disabled = false; b.textContent = was;
}

async function runImport() {
  const tracks = Object.values(S.bySide).flat().length;
  // One question. There used to be two in a row, and the second quoted the
  // artist and album out of fields the import did not use - so it could ask
  // about a name and then tag with a different one.
  const mbid = importMbid();
  const how = mbid ? "matched against the release below"
                   : "tagged from the plan, with no catalogue match";
  if (!confirm(`Tag and place ${tracks} tracks as "${S.meta.artist} \u2014 `
             + `${S.meta.album}"?\n\nThey are ${how}.`
             + `\nThe files move out of review/.`)) return;
  // The gate finds a filed record by the release the plan names, so what is filed
  // under must be what it names, a pasted id as much as a picked one.
  if (mbid && mbid !== S.meta.mbid) { S.meta.mbid = mbid; markDirty(); }
  if (S.dirty && !(await save())) return;      // the importer reads the plan from disk
  $("#imp-err").textContent = "";
  $("#imp-out").textContent = "tagging and placing\u2026";
  $("#imp-outwrap").hidden = false;
  $("#imp-go").disabled = true;
  try {
    const r = await runJob(S.slug, `/api/import/${S.slug}`,
                           { mbid, duplicates: $("#imp-dupes").value,
                             replace: $("#imp-replace").checked },
                           "importing");
    $("#imp-summary").textContent = `${r.tracks} tracks \u2192 ${r.library}`;
    $("#imp-done").hidden = false;
    $("#imp-pick").hidden = true;      // every choice in it has been acted on
    showTranscript(r);   // collected all along, and hidden on every path
    $("#imp-go").hidden = true;
    $("#imp-close").textContent = "Not yet";
    await loadArtState();
    await loadReview();
    await refreshArchiveButton();
    await refreshCutRail();
    offerArt();
    $("#imp-next").hidden = $("#archive").disabled;
    status("imported into the library");
  } catch (e) {
    $("#imp-err").textContent = e.message;
    $("#imp-outwrap").hidden = true;
    $("#imp-go").disabled = false;
  }
}

function showTranscript(r) {
  const notes = (r.notes || []).map((n) => "  " + n);
  const text = [...notes, r.output || ""].join("\n").trim();
  $("#imp-out").textContent = text || "nothing to report";
  $("#imp-outwrap").hidden = false;
  $("#imp-outwrap").open = notes.length > 0;
}

// -------------------------------------------------------------- first pass

function guessFromSlug(slug) {
  const [a, b] = slug.split("---");
  const un = (x) => (x || "").replace(/-/g, " ").trim();
  return { artist: un(a), album: un(b) };
}

let fpReleases = [];

function openFirstPass() {
  const held = Object.values(S.bySide).flat();
  $("#fp-again").hidden = !held.length;
  $("#fp-again").textContent = `This fits all ${S.meta.sides.length} sides again, ${counted(held.length, "track", "tracks")}. `
    + "Boundaries you set or checked stay where they are.";
  const marks = held.filter((t) => t.settled).length;
  $("#fp-over-row").hidden = !marks;
  $("#fp-over").checked = false;
  $("#fp-over-what").textContent = `also replace the boundaries I set, on ${counted(marks, "track", "tracks")}`;
  const g = guessFromSlug(S.slug);
  $("#fp-artist").value = S.meta.artist || g.artist;
  $("#fp-album").value = S.meta.album || g.album;
  fpReleases = [];
  renderReleases();
  $("#fp-err").textContent = "";
  $("#fp-report").hidden = true;
  $("#fp-done").hidden = true;
  $("#fp-cancel").hidden = false;
  $("#fp-force-row").hidden = true;
  $("#fp-force").checked = false;
  $("#fpdlg").showModal();
}

function renderReleaseList(box, releases, onPick, { requireDurations = true } = {}) {
  box.innerHTML = "";
  for (const r of releases) {
    const dur = r.durations == null ? "?" : (r.durations === 0 ? "NO DURATIONS" :
      `${Math.round(r.total / 60)} min`);
    // MusicBrainz keeps pressing qualifiers here, not in the title — without it
    // a widely reissued record returns dozens of rows that all read the same
    const label = [r.title, r.disambiguation, r.date || "----", r.country || "--", r.formats.join(",")]
      .filter(Boolean).join(" \u00b7 ");
    const dead = requireDurations && r.durations === 0;
    const b = el("button", dead ? "nodur" : (r.vinyl ? "vinyl" : ""));
    b.append(el("span", null, label), el("span", null, `${r.tracks} tracks \u00b7 ${dur}`));
    b.type = "button";
    b.disabled = dead;
    if (dead) b.title = "no track durations in MusicBrainz — nothing to fit against";
    b.onclick = () => onPick(r);
    box.appendChild(b);
  }
  if (!releases.length) box.innerHTML = '<p class="dim">nothing found</p>';
}

async function searchReleases(artist, album, box, onPick, opts) {
  box.innerHTML = '<p class="dim">searching…</p>';
  const qs = new URLSearchParams({ artist, album });
  const { releases } = await api(`/api/mb/search?${qs}`);
  renderReleaseList(box, releases, onPick, opts);
  return releases;
}

function renderReleases() { renderReleaseList($("#fp-results"), fpReleases, fpRun); }

async function fpSearch() {
  $("#fp-err").textContent = "";
  $("#fp-results").innerHTML = "<p class=\"dim\">searching…</p>";
  try {
    fpReleases = await searchReleases($("#fp-artist").value, $("#fp-album").value,
                                      $("#fp-results"), fpRun);
  } catch (e) {
    fpReleases = []; $("#fp-results").innerHTML = "";
    $("#fp-err").textContent = e.message;
  }
}

// Named one by one, since each is a judgement of theirs this pass did not hold.
function notHeld(released = []) {
  if (!released.length) return "";
  const lines = released.map((x) => `  ${x.number} \u201c${x.title}\u201d on side ${x.side}: ${x.why}`);
  return `Fitted again, not kept where you had them:\n${lines.join("\n")}\n\n`;
}

function startedOver(replaced = []) {
  if (!replaced.length) return "";
  const lines = replaced.map((x) => `  ${x.number} \u201c${x.title}\u201d on side ${x.side}`);
  return `Fitted again, as you asked, over what you had set:\n${lines.join("\n")}\n\n`;
}

async function fpRun(rel) {
  $("#fp-err").textContent = "";
  // The server holds what is saved, so a boundary moved a moment ago would be fitted over.
  if (S.dirty && !(await save())) return;
  $("#fp-report").hidden = true;
  $("#fp-results").innerHTML = "<p class=\"dim\">fitting…</p>";
  try {
    const r = await runJob(S.slug, `/api/firstpass/${S.slug}`,
                           { mbid: rel.id, force: $("#fp-force").checked, start_over: $("#fp-over").checked },
                           "fitting");
    $("#fp-results").innerHTML = "";
    $("#fp-report").hidden = false;
    const kept = r.kept ? `${counted(r.kept, "track", "tracks")} you set or checked left where ${r.kept === 1 ? "it was" : "they were"}` : "";
    $("#fp-report").textContent = (r.warning ? `WARNING: ${r.warning}\n\n` : "") + notHeld(r.released) + startedOver(r.replaced) + (kept ? `${kept}.\n\n` : "") + r.report;
    $("#fp-done").hidden = false;
    $("#fp-cancel").hidden = true;
    S.sideData = {};
    await openAlbum(S.slug);
    const forced = (r.forced || []).join(", ");
    status(forced
      ? `first pass applied — side ${forced} placed from the catalogue alone, check it by ear`
      : "first pass applied — read the delta column, then check by ear");
  } catch (e) {
    // keep the release list up so another one can be tried straight away
    renderReleases();
    $("#fp-err").textContent = e.message;
    // Offered only after a refusal: a side that fits is never placed this way.
    $("#fp-force-row").hidden = false;
  }
}

// ----------------------------------------------------------------- pre-cut

let pcPicked = null, pcMbid = "";

async function loadPrecut() {
  try {
    const held = await api("/api/precut");
    const staged = held.staged || [];
    $("#pc-tracks").textContent = staged.length
      ? `${staged.length} files unpacked and waiting` : "";
    $("#pc-import").hidden = !staged.length || !pcMbid;
    $("#pc-cancel").hidden = !held.upload && !staged.length;
    if (held.upload && !staged.length)
      $("#pc-what").textContent =
        `${held.upload.name} — ${mb(held.upload.have)} of ${mb(held.upload.total)} here`;
  } catch (e) { $("#pc-err").textContent = e.message; }
}

async function sendPrecut(file) {
  $("#pc-err").textContent = ""; $("#pc-done").textContent = "";
  $("#pc-report").hidden = true; $("#pc-import").hidden = true;
  $("#pc-go").disabled = true; $("#pc-cancel").hidden = false;
  try {
    const begun = await postJSON("/api/precut/begin",
      { name: file.name, size: file.size, replace: true });
    let at = begun.have, failures = 0;
    while (at < file.size) {
      const end = Math.min(at + begun.chunk, file.size);
      try {
        at = (await pushChunk(`/api/precut/chunk?at=${at}`,
                              file.slice(at, end))).have;
        failures = 0;
      } catch (e) {
        const said = /is at (\d+)/.exec(e.message);
        if (e.status === 409 && said) { at = Number(said[1]); continue; }
        if (++failures >= TRIES) throw e;
        continue;
      }
      setBar(at / file.size, "#pc-bar");
      $("#pc-state").textContent = `${mb(at)} of ${mb(file.size)}`;
    }
    $("#pc-state").textContent = "unpacking, and asking beets what it is…";
    const found = await runJob("precut", "/api/precut/finish", {}, "reading it");
    setBar(1, "#pc-bar");
    pcMbid = found.mbid || "";
    $("#pc-state").textContent = "";
    $("#pc-tracks").textContent = `${found.tracks.length} tracks`
      + (found.art.length ? `, ${found.art.length} image` : "");
    $("#pc-report").hidden = false;
    $("#pc-report").textContent = found.report;
    $("#pc-duprow").hidden = !pcMbid;
    $("#pc-import").hidden = !pcMbid;
    status(pcMbid
      ? "read the match, then import it"
      : "beets did not settle on one release — nothing will be filed");
    if (!pcMbid)
      $("#pc-err").textContent =
        "beets offered no single release. Check the tags in the download, or "
        + "import it with beets directly.";
  } catch (e) {
    $("#pc-err").textContent = e.message;
    $("#pc-go").textContent = "Resume";
  } finally {
    $("#pc-go").disabled = !pcPicked;
  }
}

// Replace removes what is filed, so it says how much and waits for a tick, as a record does.
async function showPrecutReplace() {
  const replacing = $("#pc-dup").value === "replace";
  $("#pc-replace").checked = false;
  $("#pc-replace-wrap").hidden = !replacing;
  $("#pc-import").disabled = replacing;
  if (!replacing) return;
  try {
    const { tracks, where, refused } = await api(`/api/precut/filed?mbid=${encodeURIComponent(pcMbid)}`);
    $("#pc-replace-what").textContent = refused
      || (tracks ? `${counted(tracks, "track", "tracks")} of this release at ${where}` : "None of this release is filed, so nothing is removed.");
  } catch (e) {
    $("#pc-replace-what").textContent = e.message;
  }
}

async function takePrecut() {
  $("#pc-err").textContent = "";
  $("#pc-import").disabled = true;
  $("#pc-state").textContent = "importing…";
  try {
    const done = await runJob("precut", "/api/precut/take",
      { mbid: pcMbid, duplicates: $("#pc-dup").value, replace: $("#pc-replace").checked }, "importing");
    $("#pc-state").textContent = "";
    $("#pc-done").textContent = `${done.tracks} tracks filed`
      + (done.library ? ` at ${done.library}` : "")
      + (done.notes.length ? ` — ${done.notes.join("; ")}` : "");
    $("#pc-import").hidden = true;
    $("#pc-cancel").hidden = true;
    $("#pc-what").textContent = "Nothing chosen yet";
    $("#pc-tracks").textContent = "";
    pcPicked = null; pcMbid = "";
    setBar(0, "#pc-bar");
    status("filed — replaygain and the cover were applied on the way in");
  } catch (e) {
    $("#pc-err").textContent = e.message;
  } finally {
    $("#pc-import").disabled = false;
  }
}

async function cancelPrecut() {
  if (!confirm("Drop the upload and the unpacked files?")) return;
  try {
    await postJSON("/api/precut/cancel", {});
    pcPicked = null; pcMbid = "";
    $("#pc-what").textContent = "Nothing chosen yet";
    $("#pc-tracks").textContent = ""; $("#pc-report").hidden = true;
    $("#pc-import").hidden = true; $("#pc-cancel").hidden = true;
    $("#pc-go").textContent = "Upload"; $("#pc-go").disabled = true;
    setBar(0, "#pc-bar");
  } catch (e) { $("#pc-err").textContent = e.message; }
}

// -------------------------------------------------------------------- tapes

// Only this tab's place, so a browser that refuses storage just starts at Cut.
function remember(key, value) {
  try { sessionStorage.setItem(key, value); } catch { /* the place is a convenience */ }
}
function recalled(key) {
  try { return sessionStorage.getItem(key); } catch { return null; }
}

// A tape is not a record: it has its own store and never reaches the record picker,
// so it has its own beside the tabs. ADR-063.
let tape = null, tapePicked = null, tapeWatching = false;
let tpOpen = null;                    // the song whose row is open
let tpDraft = null;                   // a song being added, not yet on the list
let tpNaming = false;                 // the New tape form is showing
let tpJobStep = null;                 // the rail's step a running job belongs to
let tpLanded = null;                  // the tape a song has been selected on
let tpListAsk = null;                 // a list waiting to be said twice
const tpRows = new Map();             // what a row remembers across a redraw of the table
let tpKeys = [];                      // each row's key, by place, which a move carries along
let tpKeyed = null;                   // the list those keys are for
let tpNewKey = 0;
const tpUnsent = new Map();           // "key/field" -> text sent and not yet known to be saved
let tpHeldBack = false;               // the last save neither landed nor was refused
let tpRedrawing = false;              // inputs leaving the page now are not a person leaving them
let tpWatchOp = "";                   // the job being watched
let tpSaving = false;                 // a list is on its way, so what the page holds is about to change
let tpSaves = 0;                      // answers to what a row sent, to tell a reply overtaken by one
let tpAllAsk = false;                 // Albums is asking about filing all from the list
let tpErrIsJobs = false;              // what the error line says came from a job
let tpRailOn = null;                  // the step the rail last scrolled to
let tpGone = false;                   // the server is not answering
let tpMisses = 0;                     // heartbeats in a row with no answer

const mss = (seconds) => {
  const whole = Math.round(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
};
const clock = (seconds) => {
  const whole = Math.round(seconds);
  if (whole < 3600) return mss(whole);
  const minutes = Math.floor(whole / 60) % 60;
  return `${Math.floor(whole / 3600)}:${String(minutes).padStart(2, "0")}:${String(whole % 60).padStart(2, "0")}`;
};
const signed = (n) => `${n > 0 ? "+" : ""}${n.toFixed(1)} s`;

async function loadTapes(keep) {
  try {
    const found = await api("/api/tapes");
    const pick = $("#tp-pick");
    pick.replaceChildren(...found.tapes.map((t) => {
      const o = el("option", null, `${t.name} (${t.songs})${t.mismatch ? " - mismatch" : ""}${t.done ? " - archived" : ""}`);
      o.value = t.id;
      return o;
    }));
    // Only an action clears this: it runs after a job, whose error is still being read.
    if (found.unreadable.length && !$("#tp-err").textContent)
      $("#tp-err").textContent = `could not read: ${found.unreadable.join(", ")}`;
    const wanted = keep || tape?.id || recalled("rd-tape");
    if (found.tapes.some((t) => t.id === wanted)) pick.value = wanted;
    if (pick.value) await openTape(pick.value);
    else showTape(null);
  } catch (e) { tpFail(e); }
}

async function openTape(id) {
  try {
    await flushNudge();
    if (tape?.id !== id) { tpOpen = null; tpDraft = null; tpLanded = null; tpListAsk = null; tpRows.clear(); tpKeyed = null; tpUnsent.clear(); }
    showTape(await api(`/api/tapes/${id}`));
    if (tape.job && !tape.job.done) watchTape();
    else if (!$("#tp-err").textContent) {
      $("#tp-err").textContent = jobErrorStands(tape) ? tape.job?.error || tape.prepare_failed : "";
      tpErrIsJobs = true;
    }
  } catch (e) { tpFail(e); }
}

async function createTape() {
  $("#tp-err").textContent = "";
  try {
    const made = await postJSON("/api/tapes", { name: $("#tp-name").value });
    $("#tp-name").value = "";
    tpNaming = false;
    await loadTapes(made.id);
  } catch (e) { tpFail(e); }
}

function showNaming(on) {
  tpNaming = on;
  showTape(tape);
  if (on) $("#tp-name").focus();
}

// A row's place changes when a song moves, so what was pressed or typed on a row is held
// by its key until its turn comes. These are the page's: the server knows only places.
function keyRows(t) {
  if (tpSaving || (tpKeyed === t.list_seen && tpKeys.length === t.songs.length)) return;
  tpKeys = t.songs.map(() => ++tpNewKey);
  tpKeyed = t.list_seen;
}
const keyOf = (number) => tpKeys[number - 1];
const numberOf = (key) => tpKeys.indexOf(key) + 1;

// A job's error stays until a person acts or it stops being true: Cut's once every song is
// cut, File's once every song is filed, and any once the job has been forgotten for an edit.
function jobErrorStands(t) {
  if (!t.job?.error) return !!t.prepare_failed;
  return !((t.job.op === "cut" && t.all_cut) || (t.job.op === "file" && t.all_filed));
}

function showTape(t) {
  tape = t;
  if (t) remember("rd-tape", t.id);
  if (t) keyRows(t);
  if (t && tpErrIsJobs && !jobErrorStands(t)) $("#tp-err").textContent = "";
  const naming = !t || tpNaming;
  $("#tp-new-form").hidden = !naming;
  $("#tp-new-cancel").hidden = !t;
  $("#tp-main-body").hidden = naming;
  $("#tp-rail").hidden = naming;
  $("#tapeview").classList.toggle("no-rail", naming);
  if (!t) {
    tpPlayer.stop();
    tpDrawn = tpAudio = null;
    tpEd.clear();
    return;
  }
  if (tpOpen != null && !songOf(tpOpen)) tpOpen = null;
  // An archived tape shown before this one left them all off.
  for (const c of $("#tapepane").querySelectorAll("input, button")) c.disabled = false;
  $("#tp-what").textContent = t.name;
  const said = [t.source, t.duration ? clock(t.duration) : "",
    `${t.songs.length} song${t.songs.length === 1 ? "" : "s"}`].filter(Boolean);
  $("#tp-meta").textContent = said.join(" · ");
  showStart(t);
  showEar(t);
  showRail(t);
  showSongs(t);
  // Opening the tape again during a job would otherwise offer what it would refuse.
  if (tapeWatching) for (const b of TAPE_JOB_BUTTONS) $(b).disabled = true;
  if (cutWouldWait(t)) $("#tp-cut").disabled = false;
  if (t.playlist.done) archivedControls();
  showNext();
  autoPrepare(t);
  landOn(t);
}

// A tape opens on what needs its person, or else on its first song. The found edges
// are not touched, and nothing is chosen for a nudge.
function landOn(t) {
  if (tpLanded === t.id || tapeWatching || t.prepare_needed) return;
  if (!t.songs.some((s) => s.start != null)) return;
  const doubt = t.ear.filter((r) => r.doubtful).map((r) => r.number).sort((a, b) => a - b)[0];
  const open = t.songs.find((s) => !s.skip && !s.settled_by)?.number;
  tapeSelect(doubt ?? open ?? t.songs[0].number, "start", false);
}

// With its audio and list both in, a tape needs no button to go on. A run that failed
// has said why, so nothing starts it again until a person does: the server keeps that
// and says what is owed, which a job in progress or a song already answered is not.
let tpPrepareSoon = null, tpPrepareStarting = false;
let tpActing = 0;                     // what a person pressed that has not been answered
let tpCutWaits = false;               // Cut was pressed during an automatic lookup
const tpStopped = new Set();          // tapes whose last job ended in error, until a person acts

function autoPrepare(t) {
  if (!t.prepare_needed || tapeWatching || (t.job && !t.job.done) || tpStopped.has(t.id)) return;
  // The next field of a row would be refused while the job holds the tape, and what a
  // person just pressed would be too, so this waits for both.
  if (tpActing || document.activeElement?.closest?.("#tp-rows")) {
    clearTimeout(tpPrepareSoon);
    tpPrepareSoon = setTimeout(() => { if (tape?.id === t.id) autoPrepare(tape); }, 1500);
    return;
  }
  // A save draws the page twice, and the job's first answer takes longer than that.
  if (tpPrepareStarting) return;
  tpPrepareStarting = true;
  postTapeJob(`/api/tapes/${t.id}/prepare`, {}).finally(() => { tpPrepareStarting = false; });
}

// A cut needs nothing a lookup finds, and its songs' boundaries are settled, so what is
// only an automatic lookup holding the tape for a moment has Cut waiting rather than off.
const cutWouldWait = (t) => !!t && tapeWatching && t.job?.op === "prepare" && !t.job.done
  && !t.cut_refusal && t.audio && !t.all_cut;

// Run as soon as the job holding the tape ends, unless that left it refused.
function cutAfterLookup(id) {
  if (!tpCutWaits || tape?.id !== id) return;
  tpCutWaits = false;
  if (tape.cut_refusal) { $("#tp-err").textContent = tape.cut_refusal; return; }
  cutTape();
}

// An archived tape stays as it is, so nothing here may offer to change it. Its
// waveform can still be looked around, and its playlist can still be asked about.
function archivedControls() {
  for (const id of ["#tp-file-pick", "#tp-csv-pick", "#tp-list-add", "#tp-process", "#tp-match", "#tp-rematch", "#tp-all-list"])
    $(id).disabled = true;
  for (const c of $("#tapepane").querySelectorAll("input, select, button")) c.disabled = true;
  for (const c of $("#tapepane").querySelectorAll("[data-nav]")) c.disabled = !tpEd.side;
  // The album it was filed under is not the tape, and can still take a cover.
  for (const c of $("#tapepane").querySelectorAll("[data-cover]")) c.disabled = false;
}

// What a new tape still needs, said where the waveform will be.
function showStart(t) {
  const hasAudio = t.audio || !!t.upload;
  const placed = t.songs.some((s) => s.start != null);
  $("#tp-need-file").hidden = hasAudio;
  $("#tp-need-list").hidden = t.songs.length > 0 || !!tpDraft;
  $("#tp-need-find").hidden = tapeWatching || t.prepare_needed
    || !(t.audio || (t.upload && t.upload.have === t.upload.total)) || !t.songs.length;
  $("#tp-start").hidden = placed || t.playlist.done
    || [$("#tp-need-file"), $("#tp-need-list"), $("#tp-need-find")].every((b) => b.hidden);
}

// ---- the rail

const TP_STEPS = ["recording", "list", "boundaries", "albums", "cut", "file", "playlist", "archive"];
const JOB_STEP = { audio: "recording", boundaries: "boundaries", albums: "albums", tape: "boundaries", match: "albums", cut: "cut", file: "file", playlist: "playlist", archive: "archive" };

const songsNamed = (numbers) => {
  const first = numbers.slice(0, 6).join(", ");
  const more = numbers.length > 6 ? ` and ${numbers.length - 6} more` : "";
  return `${numbers.length === 1 ? "Song" : "Songs"} ${first}${more}`;
};

const counted = (n, one, many) => `${n} ${n === 1 ? one : many}`;

const stepBox = (id) => document.getElementById(`tp-st-${id}`);

// `why` is what will not fit in a line, kept one press away.
function railStep(box, kind, state, detail, why = "") {
  box.dataset.kind = kind;
  box.querySelector(".step-state").textContent = state;
  box.querySelector(".step-detail").textContent = detail;
  const more = box.querySelector(".step-why");
  more.hidden = !why;
  more.querySelector("p").textContent = why;
}

const tpStep = (id, ...said) => railStep(stepBox(id), ...said);

// On a phone a rail is this one line, saying what is next, until it is opened.
function railBarSays(bar, box) {
  bar.querySelector("span").textContent = box
    ? `${box.querySelector("b").textContent}: ${box.querySelector(".step-state").textContent}` : "Done";
  bar.dataset.kind = box?.dataset.kind ?? "done";
}

function railOpens(rail, bar) {
  bar.onclick = () => {
    const open = rail.classList.toggle("open");
    bar.setAttribute("aria-expanded", open);
    if (open) rail.scrollIntoView({ block: "start" });
  };
}

// Only what this step can do, and the blue one is the next thing to press.
function tpAction(id, show, primary = false, disabled = false) {
  const b = $(id);
  b.hidden = !show;
  b.disabled = disabled;
  b.classList.toggle("ghost", !primary);
}

function stepRecording(t) {
  const partial = !!t.upload && t.upload.have < t.upload.total;
  const failed = !!tapePicked && !tapeWatching && partial;
  const here = t.audio || (t.upload && !partial);
  tpAction("#tp-file-pick", !here && !t.playlist.done, !partial, false);
  $("#tp-file-pick").textContent = partial ? "Choose the same file…" : "Choose a file…";
  tpAction("#tp-go", failed, true);
  tpAction("#tp-cancel", !!t.upload);
  if (t.playlist.done) return tpStep("recording", "done", "Archived", t.source);
  if (t.audio) return tpStep("recording", "done", "Uploaded", [t.source, t.duration ? clock(t.duration) : ""].filter(Boolean).join(" · "));
  if (t.upload && !partial) return tpStep("recording", "done", "Here", `${t.upload.name}, ${mb(t.upload.total)}, not read yet.`);
  if (partial) return tpStep("recording", "todo", "Part-way", `${t.upload.name}: ${mb(t.upload.have)} of ${mb(t.upload.total)}. Choose the same file to carry on.`);
  return tpStep("recording", "todo", "No file", "The one uncut recording of the whole tape.");
}

function stepList(t) {
  const has = t.songs.length > 0;
  tpAction("#tp-csv-pick", true, !has);
  tpAction("#tp-list-add", !has && !tpDraft);
  if (has) return tpStep("list", "done", counted(t.songs.length, "song", "songs"), "Edit any song by opening its row.");
  return tpStep("list", "todo", "Empty", "Load a CSV, or add the songs by hand.");
}

function stepBoundaries(t) {
  const placed = t.songs.some((s) => s.start != null);
  const doubts = t.ear.filter((r) => r.doubtful).map((r) => r.number).sort((a, b) => a - b);
  const canFind = t.songs.length > 0 && (t.audio || (t.upload && t.upload.have === t.upload.total));
  tpAction("#tp-process", canFind || placed, !placed, !canFind || !!t.place_refusal);
  $("#tp-process").textContent = placed ? "Find again" : "Find boundaries";
  tpAction("#tp-review-next", doubts.length > 0, true);
  if (!canFind && !placed) return tpStep("boundaries", "wait", "Waiting", "Needs the recording and the song list.");
  if (!placed) return tpStep("boundaries", "ready", "Not run", "Find where each song begins and ends.");
  if (doubts.length) {
    const lead = t.mismatch ? "The list and the audio disagree. " : "";
    return tpStep("boundaries", "todo", `${doubts.length} to check`,
      `${lead}${songsNamed(doubts)} ${doubts.length === 1 ? "needs" : "need"} listening to.`, t.mismatch);
  }
  if (t.place_refusal) return tpStep("boundaries", "done", "All checked", "Every song is in the library, so there is nothing to find again.");
  return tpStep("boundaries", "done", "All checked", "Every boundary is set, accepted or skipped.");
}

function stepAlbums(t) {
  const live = t.songs.filter((s) => !s.skip);
  const unasked = live.filter((s) => !s.proposal && !s.settled_by).length;
  const open = live.filter((s) => !s.settled_by).map((s) => s.number);
  const failed = live.filter((s) => !s.settled_by && s.proposal?.status === "failed").length;
  tpAction("#tp-match", t.songs.length > 0 && unasked > 0, true);
  tpAction("#tp-rematch", t.songs.length > 0 && !unasked && open.length > 0);
  const ready = t.from_list.numbers;
  const onlyThose = ready.length > 0 && ready.length === open.length;
  tpAction("#tp-next-album", open.length > 0 && !unasked, !onlyThose);
  tpAction("#tp-all-list", ready.length > 0 && !tapeWatching, onlyThose);
  $("#tp-all-list").textContent = `File ${counted(ready.length, "song", "songs")} from my list`;
  showAllAsk(ready, open.length - ready.length - t.from_list.no_album, t.from_list.no_album);
  if (!t.songs.length) return tpStep("albums", "wait", "Waiting", "Needs the song list.");
  if (unasked === live.length && live.length) return tpStep("albums", "ready", "Not run", "Find the album each song belongs to.");
  if (unasked) return tpStep("albums", "ready", `${unasked} not looked up`, `${songsNamed(open)} still ${open.length === 1 ? "needs" : "need"} an album.`);
  if (failed) return tpStep("albums", "fail", `${failed} failed`, `${counted(failed, "lookup", "lookups")} could not finish. Look again to retry.`);
  if (open.length) return tpStep("albums", "todo", `${open.length} to choose`, `${songsNamed(open)} ${open.length === 1 ? "needs" : "need"} an album chosen.`);
  return tpStep("albums", "done", "All chosen", "Every song knows its album.");
}

// One question, saying what counts: no match at all, and an album to file it under.
function showAllAsk(ready, choosing, noAlbum) {
  const box = $("#tp-all-ask");
  tpAllAsk = tpAllAsk && ready.length > 0 && !tapeWatching;
  box.hidden = !tpAllAsk;
  if (!tpAllAsk) { box.replaceChildren(); return; }
  const said = [
    `${counted(ready.length, "song has", "songs have")} no match at all, so each is tagged from your list, with no release, cover or date.`,
    choosing > 0 ? (choosing === 1 ? "1 song with something to choose stays as it is." : `${choosing} songs with something to choose stay as they are.`) : "",
    noAlbum ? `${counted(noAlbum, "song", "songs")} with no album on your list ${noAlbum === 1 ? "stays" : "stay"} too.` : "",
  ].filter(Boolean).join(" ");
  box.replaceChildren(askView(said, "File them", () => {
    tpAllAsk = false;
    settleSong(() => postJSON(`/api/tapes/${tape.id}/from-list`, { numbers: ready }));
  }, () => { tpAllAsk = false; showRail(tape); }));
}

// The cut's own refusal names every song; a rail line counts what it is waiting on.
function cutWaits(t) {
  const live = t.songs.filter((s) => !s.skip);
  const unplaced = live.filter((s) => s.start == null || s.end == null).length;
  const noRoom = live.filter((s) => s.start != null && s.end != null && s.end <= s.start).length;
  const doubts = t.ear.filter((r) => r.doubtful).length;
  return [
    unplaced ? "the boundaries" : "",
    noRoom ? counted(noRoom, "song with no room between its boundaries", "songs with no room between their boundaries") : "",
    doubts ? counted(doubts, "boundary to check", "boundaries to check") : "",
  ].filter(Boolean);
}

function stepCut(t) {
  const failed = t.ear.filter((r) => r.cut === "failed").map((r) => r.number);
  const stale = t.ear.filter((r) => r.cut === "out of date").length;
  const live = t.ear.filter((r) => r.cut !== "skipped" && r.cut !== "not needed").length;
  const skipped = t.songs.filter((s) => s.skip).map((s) => s.number);
  const left = skipped.length ? ` ${songsNamed(skipped)} left out, by your choice.` : "";
  const refused = !!t.cut_refusal || !t.audio;
  tpAction("#tp-cut", !t.playlist.done && !t.all_cut, !refused, refused);
  $("#tp-cut").textContent = failed.length || stale ? "Cut again" : "Cut";
  if (tpCutWaits) return tpStep("cut", "ready", "Next", "Cuts as soon as the lookup finishes.");
  if (t.playlist.done) return tpStep("cut", "done", "Cleared", "The cuts were cleared once the songs were proved filed.");
  if (!t.songs.length) return tpStep("cut", "wait", "Waiting", "Needs the song list.");
  if (t.all_cut && !t.cut_refusal) return tpStep("cut", "done", "Cut", `${counted(live, "file", "files")} written.${left}`);
  if (t.cut_refusal) {
    const waits = cutWaits(t);
    return tpStep("cut", "wait", "Waiting", waits.length ? `Waiting on ${waits.join(" and ")}.` : t.cut_refusal);
  }
  if (!t.audio) return tpStep("cut", "wait", "Waiting", "Needs the recording.");
  if (failed.length) return tpStep("cut", "fail", `${failed.length} failed`, `${songsNamed(failed)} could not be cut. Open the row to see why.`);
  if (stale) return tpStep("cut", "ready", `${stale} out of date`, `${counted(stale, "boundary has", "boundaries have")} moved since the cut.`);
  return tpStep("cut", "ready", "Ready", `${counted(live, "song", "songs")} will be cut.${left}`);
}

const FILING_NEEDS_A_LOOK = ["failed", "unknown"];

function stepFile(t) {
  const started = t.filing.some((o) => o.state !== "pending" && o.state !== "skipped");
  const failed = started ? t.filing.filter((o) => FILING_NEEDS_A_LOOK.includes(o.state)).map((o) => o.number).sort((a, b) => a - b) : [];
  // Stopped on its way to beets: the row says not confirmed, and only File again answers.
  const unsure = t.songs.filter((s) => s.import_begun && s.filing === "pending").map((s) => s.number);
  const refused = !!t.file_refusal;
  tpAction("#tp-file-songs", !t.playlist.done && !t.all_filed, !refused, refused);
  $("#tp-file-songs").textContent = failed.length || unsure.length ? "File again" : "File";
  const coverless = t.filing.filter((o) => o.no_cover).map((o) => o.number).sort((a, b) => a - b);
  if (t.all_filed) {
    return tpStep("file", "done", "Filed", `Every song is in the library.${coverless.length
      ? ` ${songsNamed(coverless)} found no cover.` : ""}`);
  }
  if (failed.length) return tpStep("file", "fail", `${failed.length} need a look`, `${songsNamed(failed)} did not finish. File again retries only those.`);
  if (unsure.length) return tpStep("file", "todo", `${unsure.length} not confirmed`, `${songsNamed(unsure)} may have reached the library. File again looks before it imports anything.`);
  const albums = t.songs.filter((s) => !s.skip && !s.settled_by).length;
  if (albums) return tpStep("file", "wait", "Waiting", `Waiting on ${t.all_cut ? "" : "the cut and "}${counted(albums, "album to choose", "albums to choose")}.`);
  if (refused) return tpStep("file", "wait", "Waiting", "Waiting on the cut.");
  return tpStep("file", "ready", "Ready", "Each album gets its own run through beets.");
}

const PLAYLIST_WORDS = {
  written: "file rewritten", unchanged: "file unchanged, so left untouched",
  unknown: "the library could not be asked", nothing: "nothing written",
  failed: "the file could not be written",
};

function stepPlaylist(t) {
  const p = t.playlist;
  const started = t.filing.some((o) => o.state !== "pending" && o.state !== "skipped");
  // A write that could not happen stays unwritten until asked again, so it is what to press.
  const stuck = !!p.outcome && p.outcome !== "written" && p.outcome !== "unchanged" && t.songs.some((s) => !s.skip);
  tpAction("#tp-playlist", true, started && (!p.outcome || stuck), !started);
  $("#tp-playlist").textContent = p.outcome ? "Write again" : "Write playlist";
  const named = (n) => t.songs.find((s) => s.number === n)?.title ?? "no longer on the list";
  const told = t.job?.result?.notes ?? [];
  const why = [...new Set([
    p.outcome ? `Last rebuild ${new Date(p.at * 1000).toLocaleString()}: ${PLAYLIST_WORDS[p.outcome] ?? p.outcome}.` : "",
    p.path, p.note, ...told,
    ...p.left_out.map((g) => `Song ${g.number} (${named(g.number)}): ${g.why}`),
  ])].filter(Boolean).join("\n");
  if (!p.outcome) return tpStep("playlist", started ? "ready" : "wait", started ? "Ready" : "Waiting", started ? "No playlist written yet." : "Waiting on File.", why);
  if (p.outcome !== "written" && p.outcome !== "unchanged")
    return tpStep("playlist", "todo", "Not written", `${PLAYLIST_WORDS[p.outcome] ?? p.outcome}.${stuck ? " Write again once the library is there." : ""}`, why);
  // The file is all this knows about: whether the server shows it is the server's to say.
  const server = "The music server imports it within about a minute, and an admin there may need to make imported playlists public.";
  const name = p.path.split("/").pop();
  if (p.left_out.length)
    return tpStep("playlist", "todo", "File written", `${p.songs} of ${t.songs.length} songs, ${p.left_out.length} left out.`, [server, why].join("\n"));
  return tpStep("playlist", "done", "File written", name, [server, why].join("\n"));
}

function stepArchive(t) {
  const p = t.playlist;
  const unfiled = t.filing.every((o) => o.state === "pending" || o.state === "skipped");
  tpAction("#tp-archive", !p.done, !p.archive_refusal, !!p.archive_refusal);
  if (p.done) return tpStep("archive", "done", "Archived", "Copied and read back. The song list stays.", p.archived.file);
  if (unfiled) return tpStep("archive", "wait", "Waiting", "Waits until the songs are filed.");
  if (p.archive_refusal) return tpStep("archive", "wait", "Waiting", "Waits until every song is proved in the library.", p.archive_refusal);
  return tpStep("archive", "ready", "Ready", "Puts the uncut file away, once it is read back.");
}

// What is next is the first step not done, and the thing to press is its blue button.
function stepNow() {
  const id = TP_STEPS.find((s) => stepBox(s).dataset.kind !== "done");
  if (!id) return null;
  const button = [...stepBox(id).querySelectorAll(".step-acts button")]
    .find((b) => !b.hidden && !b.disabled && !b.classList.contains("ghost"));
  return { id, button };
}

// The rail can be longer than the window, and the header is always on screen, so the next
// action is in both. A step that has just become current is scrolled to, once, so a person
// who scrolled the rail themselves is left where they are.
function showNext() {
  const now = stepNow();
  const act = $("#tp-next-act");
  const running = tapeWatching && !cutWouldWait(tape);
  act.hidden = !now?.button || running;
  act.textContent = now?.button?.textContent ?? "";
  act.onclick = () => now?.button?.click();
  const box = now && stepBox(now.id);
  railBarSays($("#tp-rail-bar"), box);
  if (!now || tpRailOn === now.id) return;
  tpRailOn = now.id;
  box.scrollIntoView({ block: "nearest" });
}

function showRail(t) {
  stepRecording(t);
  stepList(t);
  stepBoundaries(t);
  stepAlbums(t);
  stepCut(t);
  stepFile(t);
  stepPlaylist(t);
  stepArchive(t);
  if (tpJobStep) stepBusy(tpJobStep);
  showLeft();
}

function showLeft() {
  const kinds = TP_STEPS.map((id) => stepBox(id).dataset.kind);
  const left = kinds.filter((k) => k !== "done").length;
  const pill = $("#tp-left");
  pill.querySelector("span").textContent = left ? counted(left, "step left", "steps left") : "Done";
  pill.dataset.kind = !left ? "done" : kinds.includes("fail") ? "fail" : kinds.includes("todo") ? "todo" : "ready";
}

function stepBusy(id) {
  const box = stepBox(id);
  box.dataset.kind = "run";
  box.querySelector(".step-state").textContent = "Working";
  box.append($("#tp-progress"));
  $("#tp-progress").hidden = false;
}

function stepIdle() {
  $("#tp-progress").hidden = true;
  $("#tp-state").textContent = "";
  setBar(0, "#tp-bar");
}

// ---- the table

const CHOICE_HELD = {
  filed: "in the library, so this stays as it was filed",
  already: "in the library, so this stays as it was filed",
  unknown: "not confirmed, so this waits until File again gets an answer",
};

function boundaryWord(song, row) {
  if (song.skip) return ["Skipped", "bad"];
  if (song.start == null) return ["Not placed", "mute"];
  if (song.set_by === "ear") return ["Set by you", "good"];
  if (song.heard) return ["Accepted", "good"];
  if (row?.doubtful) return ["Check", "warn"];
  return ["Agrees", "good"];
}

function releaseWord(song, rel) {
  if (song.skip) return ["—", "mute"];
  if (song.import_begun || song.filing === "unknown") return ["Not confirmed", "warn"];
  if (song.filing === "filed") return ["Filed", "good", song.no_cover ? "warn" : null];
  if (song.filing === "already") return ["In your library", "good"];
  if (song.filing === "failed") return ["Filing failed", "bad"];
  if (song.proposal?.status === "owned") return ["In your library", "good"];
  // The dot asks for a look at a choice made for them, which a person's own has had.
  const noted = rel?.edition_note ? "warn" : null;
  if (song.settled_by === "catalogue") return ["Matched", "good", noted];
  if (song.settled_by) return [song.proposal?.choice ? "Your choice" : "From your list", "good"];
  if (rel?.maybe_copy) return ["Your copy?", "warn"];
  const p = song.proposal;
  if (!p) return ["Not looked up", "mute"];
  if (p.status === "failed") return ["Lookup failed", "bad"];
  if (p.candidates.length > 1) return [`${p.candidates.length} to choose`, "warn"];
  return [p.candidates.length ? "To confirm" : "No match", "warn"];
}

const releaseOf = (t, n) => t.releases?.find((r) => r.number === n) ?? null;
const rowState = (n) => {
  if (!tpRows.has(n)) tpRows.set(n, {});
  return tpRows.get(n);
};

function statusCell(word) {
  const td = el("td", "tp-col-status");
  const box = el("span", `tp-status is-${word[1]}`);
  box.append(el("i", word[2] ? `is-${word[2]}` : null), el("span", null, word[0]));
  td.append(box);
  return td;
}

function songRowView(song, row) {
  const tr = el("tr", [tpOpen === song.number ? "sel" : "", song.skip ? "skipped" : ""].join(" ").trim());
  tr.dataset.song = song.number;
  tr.onclick = () => {
    tapeSelect(song.number, "start", true);
    // Stacked on a phone, the waveform is not beside the row that was pressed.
    if (matchMedia("(max-width:48rem)").matches && !$("#tp-wave-wrap").hidden)
      $("#tp-wave-wrap").scrollIntoView({ block: "start" });
  };
  const choice = song.proposal?.choice;
  const rel = releaseOf(tape, song.number);
  const album = rel?.copy?.album || (song.settled_by && choice?.release_title) || song.album || "no album";
  const who = el("td", "tp-col-song");
  const line = el("div", "tp-who");
  line.append(el("span", "tp-title", song.title), el("span", "tp-artist", song.artist));
  who.append(line);
  who.title = `${song.title}, ${song.artist}`;
  const albumCell = el("td", "tp-col-album", album);
  albumCell.title = album;
  const span = el("td", "tp-col-span mono",
    song.start != null && song.end != null ? `${clock(song.start)}–${clock(song.end)}` : "");
  const length = el("td", "tp-col-len mono");
  length.append(el("span", null, mss(song.length)));
  if (song.delta != null)
    length.append(el("span", Math.abs(song.delta) > 3 ? "tp-off warned" : "tp-off", signed(song.delta)));
  tr.append(el("td", "num tp-col-num", song.number), who, albumCell, span, length,
            statusCell(boundaryWord(song, row)), statusCell(releaseWord(song, rel)));
  return tr;
}

function fieldInput(label, name, value, wide) {
  const box = el("label", wide ? "wide" : null, label);
  const input = el("input");
  input.dataset.field = name;
  input.value = value;
  input.autocomplete = "off";
  box.append(input);
  return [box, input];
}

// Edits are the whole list by hand, to the route that takes it, so what the route
// says about a list holds for each edit and the page never holds unsaved text.
const listNow = () => tape.songs.map((s, i) => ({ key: tpKeys[i], title: s.title, artist: s.artist, album: s.album, length: s.length }));

// What the route says decides whether anything is lost. A job button waits for this: a
// click that blurs an edited field sends the edit first, and the job has to see it.
let tpListSaved = Promise.resolve();

// One at a time, each built from the tape the last one left, since the route refuses a
// list made from one it no longer holds. `seen` is for a list asked about earlier. The
// open row stays the open row wherever this leaves it, unless `openAfter` names another.
function sendList(build, openAfter, sure = false, seen = null) {
  tpActing += 1;
  const turn = tpListSaved.then(() => saveList(build(), openAfter, sure, seen ?? tape.list_seen));
  tpListSaved = turn.catch(() => false).finally(() => { tpActing -= 1; });
  return turn;
}

// Everything a row sends takes its turn with the rest, or a press would meet the save a
// click just caused, or be built from a song's place before a move.
function rowTurn(work) {
  tpActing += 1;
  const turn = tpListSaved.then(work);
  tpListSaved = turn.catch(() => false).finally(() => { tpActing -= 1; });
  return turn;
}

async function saveList(rows, openAfter, sure, seen) {
  $("#tp-err").textContent = "";
  waveSay("");
  await flushNudge();
  const url = `/api/tapes/${tape.id}/songs`;
  const songs = rows.map(({ key, ...song }) => song);
  tpHeldBack = false;
  tpSaving = true;
  try {
    const after = await postJSON(url, sure ? { songs, seen, confirm: true } : { songs, seen });
    const open = openAfter ?? keyOf(tpOpen);
    tpSaving = false;
    tpSaves += 1;
    tpKeys = rows.map((r) => r.key);
    tpKeyed = after.list_seen;
    tpOpen = numberOf(open) || null;
    tpRows.clear();
    tpStopped.delete(tape.id);
    showTape(after);
    await loadTapes(tape.id);
    return true;
  } catch (e) {
    tpSaving = false;
    if (e.stale) {
      tpListAsk = null;
      await reloadTape();
    }
    tpHeldBack = e.busy || !e.status;
    if (e.needsConfirm) tpListAsk = { said: e.message, songs: rows, openAfter, seen, row: tpOpen };
    else tpFail(e);
    showSongs(tape);
    return false;
  }
}

async function reloadTape() {
  try { showTape(await api(`/api/tapes/${tape.id}`)); } catch { /* the reason being said still stands, and the page keeps what it last knew */ }
}

// Asked in the row that pressed it: the answer belongs beside the button.
function askView(said, yes, onYes, onNo) {
  const box = el("div", "tp-ask");
  box.append(el("span", null, said),
             smallButton(yes, "yes, knowing that", onYes, "tiny"),
             smallButton("Cancel", "leave things as they are", onNo));
  return box;
}

function listAskView() {
  const { said, songs, openAfter, seen } = tpListAsk;
  const plain = said.charAt(0).toUpperCase() + said.slice(1);
  return askView(`${plain}.`, "Go ahead", async () => {
    tpListAsk = null;
    const draft = tpDraft;
    tpDraft = null;
    if (!await sendList(() => songs, openAfter, true, seen)) { tpDraft = draft; showTape(tape); }
  }, () => { tpListAsk = null; showSongs(tape); });
}

// Once, when the person has left the field with something different in it. What a refusal
// turned down goes back to the truth; what could not be asked yet stays on the row and is
// sent again when it can be.
async function editField(key, input) {
  const name = input.dataset.field, value = input.value;
  const held = `${key}/${name}`;
  input.dataset.saved = value;
  tpUnsent.set(held, value);
  const saved = await sendList(() => {
    const songs = listNow();
    const row = songs.find((s) => s.key === key);
    if (row) row[name] = value;
    return songs;
  });
  if (!saved && tpHeldBack) {
    // The row says it is waiting, and that is true of a job a person is watching run.
    $("#tp-err").textContent = "";
    showSongs(tape);
    return;
  }
  tpUnsent.delete(held);
  if (!saved) showSongs(tape);
}

function leftField(key, input) {
  if (!tpRedrawing && input.value !== input.dataset.saved) editField(key, input);
}

function sendUnsent() {
  for (const [held, value] of [...tpUnsent]) {
    const [key, field] = held.split("/");
    editField(Number(key), { dataset: { field }, value });
  }
}

// By the key: pressed twice, or behind a save, it goes on moving the song it was pressed
// for, where a place would swap whichever song had just arrived there.
function moveSong(key, by) {
  sendList(() => {
    const songs = listNow();
    const from = songs.findIndex((s) => s.key === key);
    const to = from + by;
    if (from < 0 || to < 0 || to >= songs.length) return songs;
    songs.splice(to, 0, songs.splice(from, 1)[0]);
    return songs;
  });
}

function removeSong(key) {
  sendList(() => listNow().filter((s) => s.key !== key));
}

function edgesFromRow(key, box) {
  const edge = (name) => box.querySelector(`input[data-edge=${name}]`).value.trim();
  if (!edge("start") || !edge("end")) {
    waveSay("");
    $("#tp-err").textContent = `song ${numberOf(key)}: both edges need a number of seconds - a blank one is not 0`;
    showSongs(tape);
    return;
  }
  const [start, end] = [Number(edge("start")), Number(edge("end"))];
  rowTurn(() => saveTapeEdges(numberOf(key), start, end));
}

function edgeInput(song, name, label, box) {
  const wrap = el("label", null, label);
  const input = el("input");
  const key = keyOf(song.number);
  input.type = "number"; input.step = "0.01"; input.min = "0";
  input.dataset.edge = name;
  input.dataset.field = name;
  input.value = song[name];
  const leave = () => {
    if (tpRedrawing || input.value === input.dataset.saved) return;
    input.dataset.saved = input.value;
    edgesFromRow(key, box);
  };
  input.onblur = leave;
  input.onkeydown = (e) => { if (e.key === "Enter") leave(); };
  wrap.append(input);
  return wrap;
}

const MISMATCH_SAYS = "the list and the audio disagreed across this tape, and this song has not been judged";

function smallButton(text, title, act, cls = "ghost tiny") {
  const b = el("button", cls, text);
  b.type = "button"; b.title = title;
  b.onclick = (e) => { e.stopPropagation(); act(); };
  return b;
}

// Answers replace the page's rows, and a pending nudge belongs to the row it left. A
// refusal that only wants saying twice is handed back, to be asked where it was pressed.
async function settleSong(send, onAsk) {
  return rowTurn(async () => {
    $("#tp-err").textContent = "";
    await flushNudge();
    try {
      const after = await send({});
      tpSaves += 1;
      showTape(after);
    } catch (e) {
      if (e.needsConfirm && onAsk) onAsk(e);
      else tpFail(e);
    }
  });
}

const SHOWN_OPTIONS = 4;

function farWords(song, o) {
  const off = o.delta ?? (o.length == null ? null : o.length - song.length);
  if (off == null) return "";
  return `${Math.abs(off).toFixed(0)} s ${off > 0 ? "longer" : "shorter"} than your list. Use it anyway?`;
}

function chooseRelease(song, release, o, more = {}) {
  const state = rowState(song.number);
  const key = keyOf(song.number);
  state.ask = null;
  state.open = false;
  return settleSong((extra) => postJSON(`/api/tapes/${tape.id}/songs/${numberOf(key)}/release`, { release, ...more, ...extra }), (e) => {
    state.ask = { release, said: (o && farWords(song, o)) || `${e.message}.`, more };
    state.open = true;
    showSongs(tape);
  });
}

function offWords(song, o) {
  const off = o.delta ?? (o.length == null ? null : o.length - song.length);
  if (off == null) return ["no length", ""];
  if (Math.abs(off) < 0.05) return ["same length", ""];
  return [signed(off), Math.abs(off) > 3 ? "warned" : ""];
}

// One line, in columns, so that a list of editions reads down rather than across.
function optionLine(song, o, kind) {
  const state = rowState(song.number);
  const box = el("div", `tp-opt ${kind}`.trim());
  const go = el("button", "tp-opt-go");
  go.type = "button";
  go.disabled = kind === "on";
  go.title = kind === "on" ? "chosen" : "file this song under this album";
  // One release can be offered with two recordings, and this line is one of them.
  const mine = o.recording ? { recording: o.recording } : {};
  go.onclick = (e) => { e.stopPropagation(); chooseRelease(song, o.release, o, mine); };
  const name = el("span", "tp-opt-album");
  name.append(el("span", null, o.release_title));
  const named = [song.title.toLowerCase(), o.release_title.toLowerCase()];
  if (o.track_title && !named.includes(o.track_title.toLowerCase()))
    name.append(el("span", "tp-opt-track", o.track_title));
  const [off, offClass] = offWords(song, o);
  go.append(el("i"), name,
    el("span", "tp-opt-year", o.year ?? o.date?.slice(0, 4) ?? ""),
    el("span", "tp-opt-kind", o.kind ?? ""),
    el("span", "tp-opt-place", o.number ? `${o.number}${o.tracks ? ` of ${o.tracks}` : ""}` : ""),
    el("span", `tp-opt-off ${offClass}`.trim(), off),
    el("span", "tp-opt-lib", o.in_library ? "In your library" : ""));
  box.append(go);
  if (state.ask?.release === o.release && (state.ask.more?.recording ?? "") === (o.recording ?? "")) {
    const key = keyOf(song.number);
    box.append(askView(state.ask.said, "Use it", () => {
      const { more } = state.ask;
      state.ask = null;
      settleSong((extra) => postJSON(`/api/tapes/${tape.id}/songs/${numberOf(key)}/release`,
        { release: o.release, ...more, ...extra, confirm: true }));
    }, () => { state.ask = null; showSongs(tape); }));
  }
  return box;
}

async function runSearch(song, title, artist) {
  const state = rowState(song.number);
  state.title = title; state.artist = artist;
  state.said = "Searching…";
  state.open = true;
  $("#tp-err").textContent = "";
  showSongs(tape);
  try {
    const got = await postJSON(`/api/tapes/${tape.id}/songs/${song.number}/search`, { title, artist });
    state.said = got.options.length ? "" : got.reason || "Nothing found.";
    state.all = false;
    showTape(await api(`/api/tapes/${tape.id}`));
  } catch (e) {
    state.said = "";
    tpFail(e);
    showSongs(tape);
  }
}

function searchForm(song) {
  const state = rowState(song.number);
  const form = el("form", "tp-search");
  const box = (name, value, label) => {
    const input = el("input");
    input.dataset.field = `search-${name}`;
    input.value = value;
    input.autocomplete = "off";
    input.setAttribute("aria-label", label);
    return input;
  };
  const title = box("title", state.title ?? song.title, "title to search for");
  const artist = box("artist", state.artist ?? song.artist, "artist to search for");
  const go = el("button", "ghost tiny", "Search");
  go.disabled = state.said === "Searching…";
  form.append(title, artist, go);
  form.onsubmit = (e) => { e.preventDefault(); runSearch(song, title.value, artist.value); };
  form.onclick = (e) => e.stopPropagation();
  if (state.said) form.append(el("span", "dim tp-search-said", state.said));
  return form;
}

function byIdForm(song) {
  const by = el("div", "tp-by-id");
  const id = el("input");
  id.placeholder = "release id or link";
  id.setAttribute("aria-label", "release id or link");
  const track = el("input");
  track.dataset.field = "track";
  track.placeholder = "track";
  track.setAttribute("aria-label", "track number, if the title is not found");
  by.append(id, track, smallButton("Use", "look this release up and file the song under it",
    () => chooseRelease(song, id.value, null, { track: track.value })));
  const state = rowState(song.number);
  if (state.ask && !state.ask.said.endsWith("anyway?")) {
    const key = keyOf(song.number);
    by.append(askView(state.ask.said, "Use it", () => {
      const { release, more } = state.ask;
      state.ask = null;
      settleSong((extra) => postJSON(`/api/tapes/${tape.id}/songs/${numberOf(key)}/release`,
        { release, ...more, ...extra, confirm: true }));
    }, () => { state.ask = null; showSongs(tape); }));
  }
  return by;
}

function releaseOptions(song, rel) {
  const change = el("div", "tp-release");
  const state = rowState(song.number);
  const key = keyOf(song.number);
  const current = song.proposal?.choice;
  const others = rel?.options ?? [];
  const isCurrent = (c) => current && c.release === current.release && c.recording === current.recording;
  // A release given by hand is on no list of candidates, and is still the current one.
  const offered = current?.release && !others.some(isCurrent) ? [current, ...others] : others;
  const chosen = song.settled_by ? offered.filter(isCurrent) : [];
  const ordered = [...chosen, ...offered.filter((c) => !chosen.includes(c))];
  const shown = state.all ? ordered : ordered.slice(0, SHOWN_OPTIONS);
  if (shown.length) {
    const pick = el("div", "tp-opts");
    shown.forEach((c, i) => pick.append(optionLine(song, c, chosen.includes(c) ? "on" : i === 0 ? "best" : "")));
    if (ordered.length > shown.length) {
      pick.append(smallButton(`${ordered.length - shown.length} more`, "show every option",
        () => { state.all = true; showSongs(tape); }, "tp-quiet"));
    }
    change.append(pick);
  }
  // What the library holds, or may hold, was filed as this choice, and the route
  // refuses another.
  const held = CHOICE_HELD[song.import_begun ? "unknown" : song.filing];
  if (held) {
    for (const c of change.querySelectorAll("input, select, button")) c.disabled = true;
    change.append(el("div", "dim", held));
    return change;
  }
  change.append(searchForm(song));
  const quiet = el("div", "tp-rel-quiet");
  if (song.settled_by || !others.length || song.proposal) {
    const wayForward = song.proposal?.status === "none" && !song.settled_by;
    quiet.append(smallButton("File from my list", "tag this song from the list, with no release",
      () => settleSong(() => postJSON(`/api/tapes/${tape.id}/songs/${numberOf(key)}/from-list`, {})),
      wayForward ? "tiny" : "tp-quiet"));
  }
  quiet.append(smallButton("Use a release id", "name a release the search did not find",
    () => { state.byId = !state.byId; showSongs(tape); }, "tp-quiet"));
  change.append(quiet);
  if (state.byId) change.append(byIdForm(song));
  return change;
}

function noteBox(said, ...after) {
  const box = el("div", "tp-note");
  const body = el("div");
  body.append(el("span", null, said), ...after);
  box.append(el("i"), body);
  return box;
}

// A choice put in what each answer does to a person's folders. The current one is marked.
function editionNoteView(song, note) {
  const state = rowState(song.number);
  const options = releaseOf(tape, song.number)?.options ?? [];
  const box = noteBox(note.text);
  const choices = el("div", "tp-choices");
  for (const c of note.choices) {
    const b = el("button", c.current ? "tp-choice on" : "tp-choice");
    b.type = "button";
    b.disabled = c.current;
    b.append(el("b", null, c.label), el("span", null, c.said));
    b.onclick = (e) => {
      e.stopPropagation();
      chooseRelease(song, c.release, options.find((o) => o.release === c.release && o.recording === c.recording),
        c.recording ? { recording: c.recording } : {});
    };
    choices.append(b);
  }
  box.append(choices);
  const asked = note.choices.find((c) => c.release === state.ask?.release);
  if (asked) {
    const key = keyOf(song.number);
    box.append(askView(state.ask.said, "Use it", () => {
      state.ask = null;
      settleSong((extra) => postJSON(`/api/tapes/${tape.id}/songs/${numberOf(key)}/release`,
        { release: asked.release, recording: asked.recording, ...extra, confirm: true }));
    }, () => { state.ask = null; showSongs(tape); }));
  }
  return box;
}

function maybeCopyView(song, path) {
  return noteBox(`This might be your copy at ${path}.`,
    useCopyButton({ number: song.number, unconfirmed: path }),
    el("span", "dim", "or choose a release."));
}

function releaseBlock(song, t) {
  const rel = releaseOf(t, song.number);
  const box = el("div", "tp-rel");
  const state = rowState(song.number);
  const copy = rel?.copy;
  const finished = song.filing === "filed" || song.filing === "already";
  if (finished) return null;
  if (copy) {
    const line = el("p", "tp-owned", `In your library: ${copy.album_artist} / ${copy.album}`);
    box.append(line, el("p", "dim tp-path", copy.path));
  }
  if (rel?.maybe_copy) box.append(maybeCopyView(song, rel.maybe_copy));
  if (rel?.edition_note) box.append(editionNoteView(song, rel.edition_note));
  const nothing = song.proposal?.status === "none" && rel?.options.length;
  const words = rel?.note || (song.settled_by || nothing || rel?.maybe_copy ? "" : song.proposal?.reason) || "";
  // The filing outcome above this says it first when that is what the words are.
  if (words && words !== t.filing.find((f) => f.number === song.number)?.note) {
    const line = el("p", "dim tp-rel-why", words);
    line.title = words;
    box.append(line);
  }
  if (rel?.library_note && !rel.library_note.includes(rel.maybe_copy || "\0")) box.append(el("p", "dim", rel.library_note));
  if (!song.proposal && !song.settled_by) box.append(el("p", "dim", "Not looked up yet."));
  const options = releaseOptions(song, rel);
  if (!song.settled_by) {
    box.append(options);
    return box;
  }
  // Settled, only the choice stays out; changing it is one press away.
  const first = options.querySelector(".tp-opt.on");
  // A note's choices already say which release this is.
  if (first && !rel?.edition_note) box.append(first);
  else if (!copy && !rel?.edition_note) box.append(el("p", "dim", "From your list, with no release."));
  const more = el("details", "tp-more");
  more.open = !!state.open;
  more.ontoggle = () => { state.open = more.open; };
  more.append(el("summary", null, copy ? "Choose a release instead" : "Change the album"), options);
  box.append(more);
  return box;
}

// The playlist is written from what filing recorded, so once anything is filed it is
// told straight away.
function useCopyButton(o) {
  const key = keyOf(o.number);
  return smallButton("Use it", "this is the song, and it needs no filing", async () => {
    await settleSong(() => postJSON(`/api/tapes/${tape.id}/songs/${numberOf(key)}/use-copy`, { path: o.unconfirmed }));
    const started = tape.filing.some((f) => f.state !== "pending" && f.state !== "skipped");
    if (!$("#tp-err").textContent && started) writePlaylist();
  }, "tiny");
}

let tpCoverFor = null;                // the song an image being chosen is for

function noCoverView(song) {
  const upload = smallButton("Upload an image", "embedded in the album's songs, and set as its art", () => {
    tpCoverFor = song.number;
    $("#tp-cover-file").click();
  });
  upload.dataset.cover = "";
  return noteBox("No cover found.", upload);
}

async function uploadCover(number, file) {
  $("#tp-err").textContent = "";
  waveSay(`Sending ${file.name}…`);
  try {
    const after = await pushChunk(`/api/tapes/${tape.id}/songs/${number}/cover`, file);
    showTape(after);
    const c = after.cover;
    waveSay(c.already ? "That album has a cover now, so this image was not used."
      : [`Cover set: ${c.size}, in ${c.embedded} of ${c.tracks} of the album's songs.`,
         c.failed?.length ? `Not embedded: ${c.failed.join("; ")}.` : "",
         c.note ? `beets did not record it as the album's art: ${c.note}.` : ""].filter(Boolean).join(" "));
  } catch (e) {
    waveSay("");
    tpFail(e);
  }
}

const FILED_WORDS = {
  filed: "Filed at", already: "In your library at", failed: "Filing failed", unknown: "Not confirmed",
};

function filedBlock(song, t) {
  const o = t.filing.find((f) => f.number === song.number);
  if (!o || o.state === "pending" || o.state === "skipped") return null;
  const looks = FILING_NEEDS_A_LOOK.includes(o.state);
  const box = el("div", "tp-filed");
  const where = o.unconfirmed ? "" : o.path;
  box.append(el("p", looks ? "tp-warn" : "tp-owned", `${FILED_WORDS[o.state] ?? o.state}${where ? ` ${where}` : ""}`));
  if (o.note) box.append(el("p", looks ? "tp-warn" : "dim", o.note));
  if (o.no_cover) box.append(noCoverView(song));
  return box;
}

function songOpenView(song, row, t) {
  const tr = el("tr", "tp-open");
  tr.dataset.open = song.number;
  const td = el("td");
  td.colSpan = 7;
  const box = el("div", "tp-open-box");
  const fields = el("div", "tp-fields");
  const key = keyOf(song.number);
  tr.dataset.key = key;
  const edit = (label, name, value, wide) => {
    const [wrap, input] = fieldInput(label, name, value, wide);
    input.onblur = () => leftField(key, input);
    input.onkeydown = (e) => { if (e.key === "Enter") leftField(key, input); };
    fields.append(wrap);
  };
  edit("Title", "title", song.title, true);
  edit("Artist", "artist", song.artist, true);
  edit("Album", "album", song.album, true);
  edit("List length", "length", mss(song.length));
  if (song.start != null && song.end != null) {
    fields.append(edgeInput(song, "start", "Start (s)", box), edgeInput(song, "end", "End (s)", box));
  }
  const moves = el("div", "tp-moves");
  moves.append(smallButton("↑", "move up", () => moveSong(key, -1)),
               smallButton("↓", "move down", () => moveSong(key, 1)),
               smallButton("Remove", "take off the list", () => removeSong(key), "ghost tiny tp-danger"),
               smallButton("Close", "close this row", () => { tpOpen = null; showSongs(tape); }));
  fields.append(moves);
  box.append(fields);
  if ([...tpUnsent.keys()].some((held) => held.startsWith(`${key}/`))) box.append(el("p", "dim", unsentWords()));
  if (tpListAsk?.row === song.number) box.append(listAskView());

  const said = song.reason || (row?.doubtful ? MISMATCH_SAYS : "");
  if (said) box.append(el("p", row?.doubtful ? "tp-warn" : "dim", said));
  const inside = t.inside.filter((i) => i.song === song.number);
  if (inside.length) {
    const hears = el("div", "tp-hears");
    for (const g of inside) {
      const seek = smallButton(`Hear the silence at ${fmtLong(g.lo)} to ${fmtLong(g.hi)}`,
        "play from just before this silence", () => tapeHearSilence(g.lo, g.hi));
      seek.disabled = !t.audio;
      hears.append(seek);
    }
    box.append(hears);
  }
  // Archiving clears the cuts of songs already proved to be in the library.
  const cutSaid = !row || song.skip ? "" : t.playlist.done && row.cut === "not cut" ? "cleared" : row.cut;
  if (cutSaid && !["not cut", "cut", "not needed"].includes(cutSaid)) {
    box.append(el("p", cutSaid === "failed" || cutSaid === "out of date" ? "tp-warn" : "dim",
      `Cut: ${cutSaid}${cutSaid === "failed" && song.cut?.why ? `: ${song.cut.why}` : ""}`));
  }
  const filed = filedBlock(song, t);
  if (filed) box.append(filed);
  const release = song.skip ? null : releaseBlock(song, t);
  if (release) box.append(release);
  td.append(box);
  tr.append(td);
  return tr;
}

function draftRowView() {
  const tr = el("tr", "tp-open");
  tr.dataset.open = "draft";
  const td = el("td");
  td.colSpan = 7;
  const box = el("div", "tp-open-box");
  const fields = el("div", "tp-fields");
  const add = (label, name, wide, hint) => {
    const [wrap, input] = fieldInput(label, name, tpDraft[name], wide);
    if (hint) input.placeholder = hint;
    input.oninput = () => { tpDraft[name] = input.value; };
    fields.append(wrap);
  };
  add("Title", "title", true);
  add("Artist", "artist", true);
  add("Album", "album", true);
  add("List length", "length", false, "m:ss");
  const moves = el("div", "tp-moves");
  moves.append(smallButton("Add", "put this song at the end of the list", addDraft, "tiny"),
               smallButton("Cancel", "forget this song", () => { tpDraft = null; showTape(tape); }));
  fields.append(moves);
  box.append(fields);
  if (tpListAsk?.row === "draft") box.append(listAskView());
  td.append(box);
  tr.append(td);
  return tr;
}

function startDraft() {
  tpDraft = tpDraft ?? { title: "", artist: "", album: "", length: "" };
  showTape(tape);
  $("#tp-rows").querySelector('[data-field="title"]')?.focus();
}

async function addDraft() {
  const key = ++tpNewKey;
  const draft = tpDraft;
  tpDraft = null;
  if (!await sendList(() => [...listNow(), { ...draft, key }], key)) {
    tpDraft = draft;
    if (tpListAsk) tpListAsk.row = "draft";
    showTape(tape);
  }
}

// A redraw rebuilds every input, so what is being typed goes back afterwards: a job's
// progress or the save of the field before must not take it. A field is unsaved from
// the first key to when it is left, which sends it, and a refusal then shows the truth.
// Taking a focused input off the page makes a browser say it was left, which it was not,
// so `tpRedrawing` says otherwise for that moment.
function typedInRows(box = $("#tp-rows")) {
  const typed = [];
  for (const input of box.querySelectorAll("tr[data-open] input[data-field]")) {
    const focused = document.activeElement === input;
    if (focused || input.value !== input.dataset.saved) {
      const row = input.closest("tr");
      typed.push({ row: row.dataset.key ?? row.dataset.open, field: input.dataset.field, value: input.value, focused,
                   at: focused && input.type === "text" ? [input.selectionStart, input.selectionEnd] : null });
    }
  }
  return typed;
}

function putBackTyped(typed, box = $("#tp-rows")) {
  for (const input of box.querySelectorAll("input[data-field]")) input.dataset.saved = input.value;
  for (const [held, value] of tpUnsent) {
    const [key, field] = held.split("/");
    const input = box.querySelector(`tr[data-key="${key}"] input[data-field="${field}"]`);
    if (input) input.value = value;
  }
  for (const was of typed) {
    const row = was.row === "draft" ? `[data-open="draft"]` : `[data-key="${was.row}"]`;
    const input = box.querySelector(`tr${row} input[data-field="${was.field}"]`);
    if (!input) continue;
    input.value = was.value;
    if (!was.focused) continue;
    input.focus();
    if (was.at) input.setSelectionRange(...was.at);
  }
}

function showSongs(t) {
  const has = t.songs.length > 0 || !!tpDraft;
  $("#tp-table-wrap").hidden = !has;
  if (!has) { $("#tp-rows").replaceChildren(); return; }
  const typed = typedInRows();
  const rows = [];
  for (const song of t.songs) {
    const row = t.ear.find((r) => r.number === song.number);
    rows.push(songRowView(song, row));
    if (tpOpen === song.number) rows.push(songOpenView(song, row, t));
  }
  if (tpDraft) rows.push(draftRowView());
  tpRedrawing = true;
  $("#tp-rows").replaceChildren(...rows);
  tpRedrawing = false;
  $("#tp-add").hidden = !!tpDraft;
  putBackTyped(typed);
  $("#tp-rows").querySelector(".tp-ask")?.scrollIntoView({ block: "nearest" });
  if (t.playlist.done) archivedControls();
}

// ---- the waveform

// It is the same editor a rip's Cut screen has, on its own canvas, and a song's edges
// reach the server only when a marker is let go.
let tpDrawn = null, tpAudio = null;
let tpPicked = null;                  // the edge a person chose, which − + and arrows may move
let tpNudge = null;                   // {tape, song, seen, start, end, timer}, not yet saved

const songOf = (n) => tape?.songs.find((s) => s.number === n);
const earOf = (n) => tape?.ear.find((r) => r.number === n);

const STANDING = (song) => (song.skip ? "–" : song.set_by === "ear" ? "•" : song.heard ? "✓"
  : CHOICE_HELD[song.filing] ? "⚑" : "");

function tapeTracks(t) {
  return t.songs.filter((s) => s.start != null && s.end != null).map((s) => ({
    number: s.number, title: s.title, start: s.start, end: s.end, cat: s.length,
    flag: !!earOf(s.number)?.doubtful, mark: STANDING(s),
  }));
}

// A found edge sits in a gap's quiet interior, which can reach a window past the
// gap's stated end, so both are what counts as being in the gap.
const tapeGaps = (t) => t.gaps.map(([lo, hi], i) => {
  const [quietLo, quietHi] = t.quiet[i];
  return { lo: Math.min(lo, quietLo), hi: Math.max(hi, quietHi) };
});

function waveSay(text, warn) {
  const say = $("#tp-wave-say");
  say.textContent = text || "";
  say.className = warn ? "tp-warn" : "dim";
}

// The window comes from the real length, which is the grid boundaries were placed
// on, so what is drawn at an instant is what was measured there.
async function loadTapeWave(t, drawn) {
  try {
    const env = await fetchEnvelope(`/api/tapes/${t.id}/envelope`);
    if (tpDrawn !== drawn) return;
    tpEd.load({ ...env, windowMs: (t.duration * 1000) / env.peak.length, duration: t.duration,
                gaps: { band: { gaps: tapeGaps(t) } } },
              tapeTracks(tape));
    if (tpOpen != null) tapeSelect(tpOpen, "start", false);
    tapeReadout();
    showTapeTools();
  } catch (e) {
    tpDrawn = null;
    waveSay(`no waveform: ${e.message}`, true);
  }
}

function showTapeWave(t) {
  tpEd.frozen = tapeHeld() || t.playlist.done;
  const drawn = t.waveform ? `${t.id}/${t.created}` : null;
  if (drawn !== tpDrawn) {
    tpPlayer.stop();
    tpAudio = null;
    tpDrawn = drawn;
    tpEd.clear();
    waveSay(drawn ? "" : "This tape has no waveform kept. Find boundaries measures it, and leaves any boundary you set where it is.", !drawn);
    if (drawn) loadTapeWave(t, drawn);
  } else if (tpEd.side) {
    tpEd.side.gaps.band.gaps = tapeGaps(t);
    tpEd.setTracks(tapeTracks(t));
    tapeReadout();
  }
  showTapeTools();
}

function showEar(t) {
  const placed = t.songs.some((s) => s.start != null);
  $("#tp-wave-wrap").hidden = !placed;
  if (!placed) {
    tpPlayer.stop();
    tpDrawn = tpAudio = null;
    tpEd.clear();
    return;
  }
  showTapeWave(t);
}

const toolLabel = (id, text) => { $(id).querySelector("span").textContent = text; };
const tapeWhole = () => !!tpEd.side && tpEd.t1 - tpEd.t0 >= tpEd.side.duration - 0.01;

function showTapeView() {
  const whole = tapeWhole();
  $("#tp-whole").classList.toggle("on", whole);
  $("#tp-song").classList.toggle("on", !!tpEd.side && !whole);
}

function showTapeTools() {
  const done = !!tape?.playlist.done, wave = !!tpEd.side;
  const song = wave ? songOf(tpEd.sel?.track) : null;
  const doubts = tape?.ear.filter((r) => r.doubtful).length ?? 0;
  const none = !tape?.songs.length;
  for (const id of ["#tp-prev", "#tp-next"]) $(id).disabled = none;
  $("#tp-doubt").disabled = !doubts || !wave || done;
  toolLabel("#tp-doubt", doubts ? `Next doubt (${doubts})` : "No doubts");
  const picked = !!song && tpPicked?.track === song.number;
  const moves = !!song && !tpEd.frozen;
  for (const edge of ["start", "end"]) {
    const b = $(`#tp-edge-${edge}`);
    b.disabled = !moves;
    b.classList.toggle("on", picked && tpPicked.edge === edge);
  }
  for (const id of ["#tp-minus", "#tp-plus"]) $(id).disabled = !moves || !picked;
  $("#tp-play").disabled = !tape?.audio;
  toolLabel("#tp-play", tpPlayer.playing ? "Stop" : song ? "Play song" : "Play");
  $("#tp-hear").disabled = !tape?.audio || !song;
  $("#tp-accept").disabled = done || !song || song.set_by === "ear";
  toolLabel("#tp-accept", song?.heard ? "Take back" : "Accept");
  $("#tp-accept").classList.toggle("primary", !!song && !!earOf(song.number)?.doubtful);
  $("#tp-skip").disabled = done || !song;
  toolLabel("#tp-skip", song?.skip ? "Unskip" : "Skip");
  $("#tp-song").disabled = done || !song;
  for (const id of ["#tp-in", "#tp-out", "#tp-whole"]) $(id).disabled = !wave;
  showTapeView();
}

function tapeReadout() {
  const sel = tpEd.sel, t = sel && tpEd.tracks.find((x) => x.number === sel.track);
  if (!t) {
    $("#tp-ro-time").textContent = fmtLong(tpEd.playhead);
    $("#tp-ro-song").textContent = ""; $("#tp-ro-len").textContent = "";
    $("#tp-ro-gap").textContent = ""; $("#tp-ro-gap").className = "";
    return;
  }
  const v = t[sel.edge], len = t.end - t.start, d = len - t.cat;
  $("#tp-ro-time").textContent = `${fmtLong(v)}  ${v.toFixed(2)}s`;
  $("#tp-ro-song").textContent = `${sel.edge} of ${t.number} “${t.title}”`;
  $("#tp-ro-len").textContent = `${len.toFixed(2)}s (list ${t.cat.toFixed(2)}, ${d >= 0 ? "+" : ""}${d.toFixed(2)})`;
  sayGap($("#tp-ro-gap"), v, tpEd.side?.gaps.band.gaps ?? []);
}

function tapeSelect(num, edge, fit, picked = false) {
  if (tpNudge && tpNudge.song !== num) flushNudge();
  if (num != null) tpLanded = tape.id;
  tpEd.sel = num == null ? null : { track: num, edge };
  tpPicked = picked && num != null ? { track: num, edge } : null;
  const t = tpEd.tracks.find((x) => x.number === num);
  if (fit && t) tpEd.fitRange(t.start, t.end); else tpEd.render();
  tapeReadout();
  showTapeTools();
  if (num == null) return;
  const opening = tpOpen !== num;
  if (opening) tpListAsk = null;
  tpOpen = num;
  waveSay("");
  if (opening) showSongs(tape);
  rowIntoView(num);
}

// The table's own head stays over its top, which a browser's scrolling into view does not
// allow for. All of the row and what opened under it if that fits, and its top if not. On
// a phone the table does not scroll by itself, and the page's way is the right one.
function rowIntoView(num) {
  const row = document.querySelector(`#tp-rows tr[data-song="${num}"]`);
  if (!row) return;
  const wrap = $("#tp-table-wrap");
  if (getComputedStyle(wrap).overflowY === "visible") { row.scrollIntoView({ block: "nearest" }); return; }
  const under = row.nextElementSibling?.matches(".tp-open") ? row.nextElementSibling : row;
  const head = $("#tp-table thead").offsetHeight;
  const box = wrap.getBoundingClientRect();
  const top = row.getBoundingClientRect().top - box.top - head;
  const bottom = under.getBoundingClientRect().bottom - box.bottom;
  if (top < 0) wrap.scrollTop += top;
  else if (bottom > 0) wrap.scrollTop += Math.min(top, bottom + 1);
}

// Whole stays whole, so stepping along a long tape does not lose the view of it.
function stepSong(by) {
  if (!tape?.songs.length) return;
  const here = tpEd.sel?.track ?? tpOpen ?? (by > 0 ? 0 : tape.songs.length + 1);
  const next = Math.max(1, Math.min(tape.songs.length, here + by));
  tapeSelect(next, "start", !tapeWhole());
}

function stepDoubt(by) {
  if (!tape || !tpEd.side) return;
  const doubts = tape.ear.filter((r) => r.doubtful).map((r) => r.number).sort((a, b) => a - b);
  if (!doubts.length) return;
  const here = tpEd.sel?.track ?? 0;
  const next = by > 0 ? doubts.find((n) => n > here) ?? doubts[0]
    : doubts.findLast((n) => n < here) ?? doubts.at(-1);
  tapeSelect(next, "start", true);
}

function stepAlbum() {
  const open = tape.songs.filter((s) => !s.skip && !s.settled_by).map((s) => s.number);
  if (!open.length) return;
  const next = open.find((n) => n > (tpOpen ?? 0)) ?? open[0];
  tapeSelect(next, "start", true);
}

// Asked for only when something is to be played: a long tape's file is gigabytes,
// and opening its page should not start reading it.
function tapeAudioReady() {
  if (!tape?.audio) return false;
  const url = `/api/tapes/${tape.id}/audio`;
  if (tpAudio !== url) { tpPlayer.load(url); tpAudio = url; }
  return true;
}

// A paused song carries on from the playhead; from anywhere else it plays from its top.
function tapePlay() {
  if (!tapeAudioReady()) return;
  if (tpPlayer.playing) { tpPlayer.toggle(null); return; }
  const t = tpEd.sel && tpEd.tracks.find((x) => x.number === tpEd.sel.track);
  const at = tpEd.playhead;
  tpPlayer.toggle(t && (at < t.start || at >= t.end) ? t.start : at);
  if (t) tpPlayer.stopAt = t.end;
}

function tapeHear(at) {
  if (tapeAudioReady()) tpPlayer.audition(at);
}

function tapeHearSelected() {
  const t = tpEd.sel && tpEd.tracks.find((x) => x.number === tpEd.sel.track);
  if (t) tapeHear(t[tpEd.sel.edge]);
}

function tapeHearSilence(lo, hi) {
  if (!tapeAudioReady()) return;
  if (tpEd.side) tpEd.fitRange(Math.max(0, lo - 4), hi + 4);
  tpPlayer.stop();
  tpPlayer.toggle(Math.max(0, lo - 4));
}

// What a move means from here: only this song moved, and its cut no longer matches.
function moveSaid(after, num) {
  const stale = after.ear.filter((r) => r.cut === "out of date").length;
  return [
    `Song ${num} is yours now, and nothing will move it.`,
    stale ? `${stale} cut${stale === 1 ? " is" : "s are"} now out of date - press Cut again.` : "",
  ].filter(Boolean).join(" ");
}

async function saveTapeEdges(num, start, end) {
  if (tpNudge?.tape === tape.id && tpNudge.song === num) dropNudge();
  else await flushNudge();
  tpEd.frozen = true;
  try {
    const seen = songOf(num);
    const after = await postJSON(`/api/tapes/${tape.id}/songs/${num}/edges`,
      { start, end, seen_start: seen.start, seen_end: seen.end });
    showTape(after);
    if (tpEd.sel?.track === num) tapeSelect(num, tpEd.sel.edge, false, tpPicked?.track === num);
    waveSay(moveSaid(after, num));
  } catch (e) {
    tpEd.frozen = tapeHeld() || !!tape?.playlist.done;
    if (tape) tpEd.setTracks(tapeTracks(tape));
    tapeReadout();
    // What this page believed may be what was wrong, so the markers show the truth.
    try { if (tape) showTape(await api(`/api/tapes/${tape.id}`)); } catch { /* it was asked again only to be sure, and the reason below still stands */ }
    waveSay(`Not moved: ${e.message}`, true);
  }
}

function tapeEdgeDropped(num, edge, landed) {
  if (!landed) {
    tpEd.setTracks(tapeTracks(tape));
    waveSay("the drag was interrupted, so nothing moved", true);
    return;
  }
  const t = tpEd.tracks.find((x) => x.number === num);
  const key = keyOf(num);
  rowTurn(() => saveTapeEdges(numberOf(key), t.start, t.end));
}

function dropNudge() {
  const n = tpNudge;
  if (n) clearTimeout(n.timer);
  tpNudge = null;
  return n;
}

// What a person nudged is saved whatever happens next. It belongs to its tape and song,
// which may not be the ones on screen by the time it goes.
async function flushNudge() {
  const n = dropNudge();
  if (!n) return;
  if (tape?.id === n.tape) { await saveTapeEdges(n.song, n.start, n.end); return; }
  try { await postJSON(nudgeUrl(n), nudgeBody(n)); }
  catch (e) { if (e.status) $("#tp-err").textContent = `song ${n.song} was not moved: ${e.message}`; else lostContact(); }
}

const nudgeUrl = (n) => `/api/tapes/${n.tape}/songs/${n.song}/edges`;
const nudgeBody = (n) => ({ start: n.start, end: n.end, seen_start: n.seen[0], seen_end: n.seen[1] });

// `keepalive` is what lets a request outlive the page that sent it.
function flushNudgeOnLeaving() {
  const n = dropNudge();
  if (n) fetch(nudgeUrl(n), { method: "POST", keepalive: true, credentials: "same-origin",
                              headers: { "content-type": "application/json" }, body: JSON.stringify(nudgeBody(n)) });
}

function nudgeTapeEdge(t, edge, by) {
  if (tpEd.frozen) return;
  t[edge] += by;
  tpEd.render(); tapeReadout();
  const song = songOf(t.number);
  const seen = tpNudge?.song === t.number ? tpNudge.seen : [song.start, song.end];
  dropNudge();
  tpNudge = { tape: tape.id, song: t.number, seen, start: t.start, end: t.end };
  tpNudge.timer = setTimeout(() => rowTurn(flushNudge), 500);
  waveSay(`Song ${t.number} becomes yours when this saves, and nothing will move it after that.`);
}

// A nudge sets a boundary for good, so a song chosen from its row is not enough: the
// edge has to have been asked for, by its marker or by Start, End or E.
function nudgeChosen(by) {
  const sel = tpEd.sel, t = sel && tpEd.tracks.find((x) => x.number === sel.track);
  if (!t) return false;
  if (tpPicked?.track !== sel.track || tpPicked.edge !== sel.edge) {
    waveSay("Choose Start or End first (E), or press a marker, and then this moves it.");
    return false;
  }
  nudgeTapeEdge(t, sel.edge, by);
  return true;
}

function tapeChooseEdge(edge) {
  const num = tpEd.sel?.track;
  if (num != null) tapeSelect(num, edge, false, true);
}

// The first press chooses the edge the readout is showing, and the next ones swap it.
function tapeSwapEdge() {
  const sel = tpEd.sel;
  if (!sel) return;
  const chosen = tpPicked?.track === sel.track;
  tapeChooseEdge(chosen ? (sel.edge === "start" ? "end" : "start") : sel.edge);
}

// A verdict is sent with the edges this page holds once any nudge has saved, so one on a
// song moved elsewhere is refused. What the page believed is then what was wrong.
function tapeJudge(send) {
  return settleSong(async () => {
    let after;
    try {
      after = await send();
    } catch (e) {
      if (e.status === 409 && tape) showTape(await api(`/api/tapes/${tape.id}`));
      throw e;
    }
    waveSay("");
    return after;
  });
}
function seenOf(key) {
  const { start, end } = songOf(numberOf(key));
  return start == null ? {} : { seen_start: start, seen_end: end };
}
function tapeAccept(song) {
  const key = keyOf(song.number);
  return tapeJudge(() => postJSON(`/api/tapes/${tape.id}/songs/${numberOf(key)}/heard`, { heard: !song.heard, ...seenOf(key) }));
}
function tapeSkip(song) {
  const key = keyOf(song.number);
  return tapeJudge(() => postJSON(`/api/tapes/${tape.id}/songs/${numberOf(key)}/skip`, { skip: !song.skip, ...seenOf(key) }));
}

function zoomBy(editor, by) {
  if (!editor.side) return;
  const span = (editor.t1 - editor.t0) * by;
  const sel = editor.sel && editor.tracks.find((x) => x.number === editor.sel.track);
  editor.centerOn(sel ? sel[editor.sel.edge] : (editor.t0 + editor.t1) / 2, span);
}

function tapeShowSong() {
  const t = tpEd.tracks.find((x) => x.number === tpEd.sel?.track);
  if (t) tpEd.fitRange(t.start, t.end);
}

// Every key but the two nudges presses its button, so a key does what the button
// would and a disabled button refuses it.
const TAPE_KEYS = {
  k: "#tp-prev", j: "#tp-next", a: "#tp-hear", Enter: "#tp-accept", x: "#tp-skip",
};
const NUDGE_KEYS = { ",": -1, "<": -1, ".": 1, ">": 1, ArrowLeft: -1, ArrowRight: 1 };

function tapeKey(e) {
  // Alt-left is the browser going back. Shift is this page's own larger step.
  if (e.altKey || e.ctrlKey || e.metaKey) return;
  const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  if (NUDGE_KEYS[e.key]) {
    if (nudgeChosen(NUDGE_KEYS[e.key] * (e.shiftKey ? 0.5 : 0.05))) e.preventDefault();
  } else if (e.code === "Space") {
    e.preventDefault();
    // A button keeps the focus after a press, and would be pressed again by this.
    if (e.target.tagName === "BUTTON") e.target.blur();
    tapePlay();
  } else if (TAPE_KEYS[key]) {
    e.preventDefault(); $(TAPE_KEYS[key]).click();
  } else if (key === "n") {
    e.preventDefault(); stepDoubt(e.shiftKey ? -1 : 1);
  } else if (key === "e") {
    e.preventDefault(); tapeSwapEdge();
  } else if (key === "f" || key === "0") {
    e.preventDefault(); tpEd.fit();
  } else if (key === "z") {
    e.preventDefault();
    if (tapeWhole()) tapeShowSong(); else tpEd.fit();
  } else if (key === "?") {
    e.preventDefault(); $("#helpdlg").showModal();
  } else if (key === "Escape") { tpPlayer.stop(); }
}

function wireTapeWave() {
  tpEd = new Editor($("#tp-cv"),
    (num, edge, at) => {
      const t = tpEd.tracks.find((x) => x.number === num);
      if (t) { t[edge] = at; tpEd.render(); tapeReadout(); }
    },
    (num, edge) => tapeSelect(num, edge, false, true),
    (at) => { if (tpPlayer.playing) tpPlayer.seek(at); },
    { onDrop: tapeEdgeDropped, long: true, onView: showTapeView, onSong: (num) => tapeSelect(num, "start", false) });
  tpPlayer = new Player((at) => { tpEd.playhead = at; tpEd.render(); if (!tpEd.sel) tapeReadout(); });
  for (const change of ["play", "pause", "ended"]) tpPlayer.el.addEventListener(change, showTapeTools);
  // The rail coming and going changes the canvas's width with no window resize.
  new ResizeObserver(() => tpEd.render()).observe($("#tp-cv"));
  $("#tp-vol").oninput = (e) => tpPlayer.setVolume(e.target.value / 100);
  addEventListener("pagehide", flushNudgeOnLeaving);
  setInterval(heartbeat, 4000);
  $("#tp-prev").onclick = () => stepSong(-1);
  $("#tp-next").onclick = () => stepSong(1);
  $("#tp-doubt").onclick = () => stepDoubt(1);
  $("#tp-edge-start").onclick = () => tapeChooseEdge("start");
  $("#tp-edge-end").onclick = () => tapeChooseEdge("end");
  $("#tp-minus").onclick = () => nudgeChosen(-0.05);
  $("#tp-plus").onclick = () => nudgeChosen(0.05);
  $("#tp-play").onclick = tapePlay;
  $("#tp-hear").onclick = tapeHearSelected;
  $("#tp-in").onclick = () => zoomBy(tpEd, 0.5);
  $("#tp-out").onclick = () => zoomBy(tpEd, 2);
  $("#tp-whole").onclick = () => tpEd.fit();
  $("#tp-song").onclick = tapeShowSong;
  $("#tp-accept").onclick = () => tapeAccept(songOf(tpEd.sel?.track));
  $("#tp-skip").onclick = () => tapeSkip(songOf(tpEd.sel?.track));
}

// ---- jobs and arrivals

async function startTapeJob(path, body) {
  tpActing += 1;
  try {
    await tpListSaved;
    await postTapeJob(path, body);
  } finally { tpActing -= 1; }
}

async function postTapeJob(path, body) {
  $("#tp-err").textContent = "";
  await flushNudge();
  try {
    await postJSON(path, body);
    watchTape();
  } catch (e) { tpFail(e); }
}

function cutTape() {
  if (tpPrepareStarting || cutWouldWait(tape)) {
    tpCutWaits = true;
    showRail(tape);
    return undefined;
  }
  return startTapeJob(`/api/tapes/${tape.id}/cut`, {});
}
const fileTape = () => startTapeJob(`/api/tapes/${tape.id}/file`, {});
const writePlaylist = () => startTapeJob(`/api/tapes/${tape.id}/playlist`, {});
function archiveTape() {
  if (confirm(`Archive ${tape.name}? Its uncut file is copied and read back, then this copy and its cuts are cleared. The song list stays.`))
    startTapeJob(`/api/tapes/${tape.id}/archive`, {});
}

async function sendTapeList(file) {
  $("#tp-err").textContent = "";
  const url = `/api/tapes/${tape.id}/list`;
  try {
    try {
      showTape(await pushChunk(url, file));
    } catch (e) {
      if (!e.needsConfirm) throw e;
      if (!confirm(`${e.message}. Load it?`)) return;
      showTape(await pushChunk(`${url}?confirm=1`, file));
    }
    await loadTapes(tape.id);
    status(`${tape.songs.length} songs read from ${file.name}`);
  } catch (e) { tpFail(e); }
}

async function sendTape(file) {
  $("#tp-err").textContent = "";
  $("#tp-go").hidden = true;
  const id = tape.id;
  stepBusy("recording");
  try {
    const begun = await postJSON(`/api/tapes/${id}/upload/begin`,
      { name: file.name, size: file.size });
    let at = begun.have, failures = 0;
    while (at < file.size) {
      const end = Math.min(at + begun.chunk, file.size);
      try {
        at = (await pushChunk(`/api/tapes/${id}/upload/chunk?at=${at}`,
                              file.slice(at, end))).have;
        failures = 0;
      } catch (e) {
        const said = /is at (\d+)/.exec(e.message);
        if (e.status === 409 && said) { at = Number(said[1]); continue; }
        if (++failures >= TRIES) throw e;
        continue;
      }
      setBar(at / file.size, "#tp-bar");
      $("#tp-state").textContent = `${mb(at)} of ${mb(file.size)}`;
      status(`uploading ${file.name}: ${mb(at)} of ${mb(file.size)}`, true);
    }
    status("");
    tapePicked = null;
    tpStopped.delete(id);
    stepIdle();
    await openTape(id);
  } catch (e) {
    stepIdle();
    tpFail(e);
    status("");
    try { showTape(await api(`/api/tapes/${id}`)); } catch { /* the reason above is what matters, and the page keeps what it last knew */ }
  }
}

async function cancelTapeUpload() {
  if (!confirm("Drop what has arrived of this upload?")) return;
  try {
    await postJSON(`/api/tapes/${tape.id}/upload/cancel`, {});
    tapePicked = null;
    stepIdle();
    await openTape(tape.id);
  } catch (e) { tpFail(e); }
}

const processTape = () => startTapeJob(`/api/tapes/${tape.id}/process`, {});
const matchTape = (again) => startTapeJob(`/api/tapes/${tape.id}/match`, { again });

// A job that only asks the catalogue saves a song at a time, so a person can carry on
// beside it. Any other holds the tape, and a marker is not to be moved.
const ASKS_AS_IT_GOES = ["prepare", "match"];
const tapeHeld = () => tapeWatching && !ASKS_AS_IT_GOES.includes(tpWatchOp);

function unsentWords() {
  if (tapeHeld()) return "Not saved yet: it saves when this job finishes.";
  return "Not saved yet: it saves when the server can be reached.";
}

const TAPE_JOB_BUTTONS = ["#tp-process", "#tp-match", "#tp-rematch", "#tp-cut", "#tp-file-songs", "#tp-playlist", "#tp-archive"];

// An answer has a status, whatever it says. Anything else never got there, and that is
// one message, which goes by itself, in place of a browser's words for it that stay.
function tpFail(e) {
  tpErrIsJobs = false;
  if (e.status) $("#tp-err").textContent = e.message;
  else lostContact();
}

function lostContact() {
  tpGone = true;
  $("#tp-gone").hidden = false;
}

function regained() {
  tpGone = false;
  tpMisses = 0;
  $("#tp-gone").hidden = true;
  status("");
  if (!tape || tapeWatching) return;
  reloadTape().then(sendUnsent);
}

// Asked while the page is being looked at, so that a person learns of a lost connection
// before they press something, and of its return with nothing pressed.
async function heartbeat() {
  if (document.body.dataset.mode !== "tapes" || document.hidden) return;
  try {
    await api("/api/me");
  } catch (e) {
    if (!e.status) {
      tpMisses += 1;
      if (tpMisses >= 2) lostContact();
      return;
    }
  }
  tpMisses = 0;
  if (tpGone) regained();
}

function serverGone() {
  lostContact();
  status("The server can't be reached. Trying again…", true);
  if (tpJobStep) stepBox(tpJobStep).querySelector(".step-state").textContent = "No contact";
}

// Also what a reload lands in when a job is still going.
async function watchTape() {
  if (tapeWatching) return;
  tapeWatching = true;
  const { id, key } = tape;
  for (const b of TAPE_JOB_BUTTONS) $(b).disabled = true;
  $("#tp-next-act").hidden = true;
  tpEd.frozen = true;
  let missed = 0, answered = -1;
  try {
    for (;;) {
      // Another tape was opened. Its own job, if it has one, is what is watched now.
      if (tape?.id !== id) break;
      let j;
      try {
        j = await api(`/api/job/${key}`);
        if (missed >= TRIES && tpJobStep) stepBusy(tpJobStep);
        if (tpGone) regained();
        missed = 0;
      } catch (e) {
        // Anything that answered is an answer. Nothing else says whether the job is
        // still going, so this only keeps asking, which can do nothing twice, and comes
        // back by itself. A server that restarted says so when it does answer: a 404.
        if (e.status) throw e;
        if (++missed >= TRIES) serverGone();
        await new Promise((r) => setTimeout(r, 1500));
        continue;
      }
      tpWatchOp = j.op;
      if (!tpNudge && tape?.id === id) tpEd.frozen = tapeHeld() || tape.playlist.done;
      const stepNow = JOB_STEP[j.phase] ?? JOB_STEP[j.op];
      const moved = stepNow && tpJobStep !== stepNow;
      if (moved) {
        tpJobStep = stepNow;
        stepBusy(tpJobStep);
        showLeft();
      }
      // What a job finds is saved as it goes, so the rows and the other steps can show it.
      if ((moved || (tpJobStep === "albums" && j.finished !== answered)) && tape?.id === id) {
        answered = j.finished;
        // A person may be saving beside this, and an older answer must not undo what theirs left.
        try {
          const saves = tpSaves;
          const fresh = await api(`/api/tapes/${id}`);
          if (saves === tpSaves && !tpSaving) showTape(fresh);
        } catch { /* the job is what is being watched, and it says the rest */ }
      }
      if (j.total) setBar(j.finished / j.total, "#tp-bar");
      const doing = j.current ? `${j.current}: ${j.detail}` : j.detail || "starting";
      $("#tp-state").textContent = doing;
      status(doing, true);
      if (j.done) {
        status("");
        const parts = j.op === "prepare" ? [j.result?.boundaries, j.result?.albums] : [];
        const said = [j.error, ...parts].filter(Boolean).join(" ");
        if (said) $("#tp-err").textContent = said;
        tpErrIsJobs = !!said;
        if (j.error) tpStopped.add(id); else tpStopped.delete(id);
        break;
      }
      await new Promise((r) => setTimeout(r, 800));
    }
  } catch (e) {
    status("");
    tpStopped.add(id);
    $("#tp-err").textContent = e.status === 404
      ? "the server no longer knows that job, which it forgets half an hour after one ends and when it restarts - what it finished is kept, so start it again if anything is left"
      : e.message;
  } finally {
    tapeWatching = false;
    tpWatchOp = "";
    tpJobStep = null;
    stepIdle();
  }
  if (tape?.id !== id) {
    if (tape?.job && !tape.job.done) watchTape();
    else if (tape) showTape(tape);
    return;
  }
  await loadTapes(id);
  cutAfterLookup(id);
  if (tape?.id === id) sendUnsent();
}

async function removeTape() {
  if (!confirm(`Remove ${tape.name}? Its audio and list are moved aside, not deleted. An upload still arriving is dropped. This does not remove its playlist from the music server.`)) return;
  try {
    const gone = await postJSON(`/api/tapes/${tape.id}/remove`, {});
    tape = null;
    await loadTapes();
    status(`moved to ${gone.moved_to}`);
  } catch (e) { tpFail(e); }
}

// -------------------------------------------------------------------- modes

// Every page writes to the status line and the console. Cut gives them a place in its own
// column; a page without one has the status in the header and the console along the foot.
function placeShared(mode) {
  if (mode === "cut") {
    $("#cut-msg").append($("#status"));
    $("#cut-scroll").append($("#logwrap"));
  } else {
    $("#tp-head").before($("#status"));
    $("#app").append($("#logwrap"));
  }
}

function setMode(mode) {
  document.body.dataset.mode = mode;
  remember("rd-mode", mode);
  for (const b of document.querySelectorAll("#modes button"))
    b.classList.toggle("on", b.dataset.mode === mode);
  // Rip and Upload are one job from two sources, so they share a column and the
  // names in it. Punch is its own form. Only the two that own the capture
  // device get the pane beside it - an upload has no capture to meter.
  const source = mode === "rip" || mode === "punch" || mode === "upload"
    || mode === "precut" || mode === "tapes";
  const metering = mode === "rip" || mode === "punch";
  placeShared(mode);
  $("#cutview").hidden = source;
  $("#captureview").hidden = !source || mode === "tapes";
  $("#tapeview").hidden = mode !== "tapes";
  $("#tp-head").hidden = mode !== "tapes";
  // A pre-cut album is not a record: it has no sides and nothing to cut, so
  // the record's own fields have no bearing on it. ADR-062.
  $("#sourceform").hidden = mode === "punch" || mode === "precut" || mode === "tapes";
  $("#precutform").hidden = mode !== "precut";
  $("#precutpane").hidden = mode !== "precut";
  $("#punchform").hidden = mode !== "punch";
  $("#ripbits").hidden = mode !== "rip";
  $("#uploadbits").hidden = mode !== "upload";
  $("#capturepane").hidden = !metering;
  $("#uploadpane").hidden = mode !== "upload";
  $("#source-title").textContent = mode === "upload" ? "Upload a side" : "Rip a side";
  if (mode === "rip") ripSlug();
  if (metering) startRipPoll(); else stopRipPoll();
  if (mode === "rip") loadOrphans();
  if (mode === "punch") loadPunchTracks();
  if (mode === "upload") loadIncoming();
  if (mode === "precut") loadPrecut();
  if (mode === "tapes") { player?.stop(); status(""); loadTapes(); } else { tpPlayer?.stop(); flushNudge(); }
  if (!source) ed.render();
}

// --------------------------------------------------------------------- rip

// A re-rip has to land on the SAME slug as the album it is replacing, or the
// archived capture cannot be found and align has nothing to correlate against.
// Deriving the slug from retyped text makes that hinge on spelling the artist
// identically twice, so when an existing album is picked the slug is pinned to
// it and the name fields go read-only.
function ripSlug() {
  // An upload always becomes side a, whatever the Rip tab has lined up next.
  const side = document.body.dataset.mode === "upload" ? "a" : $("#rip-side").value.trim();
  if (S.slug) {
    const sides = (S.meta.sides || []).join(" ") || "none";
    $("#rip-slug").textContent =
      `→ raw/${S.slug}/side-${side}.flac   ·   existing sides: ${sides}`;
    return S.slug;
  }
  const a = $("#rip-artist").value.trim(), b = $("#rip-album").value.trim();
  const slug = a && b ? slugify(a, b) : "";
  $("#rip-slug").textContent = slug ? `→ raw/${slug}/side-${side}.flac` : "";
  return slug;
}

// ------------------------------------------------------------------ upload

// A file that was recorded somewhere else. Rip and Upload are the same job -
// get audio into the record the selector names - so they share the column and
// the names in it, and everything after the side lands is identical.

let picked = null;
let sending = false;

function setBar(fraction, sel = "#up-bar") {
  $(sel).querySelector("i").style.width =
    `${Math.max(0, Math.min(1, fraction)) * 100}%`;
}

const mb = (n) => `${(n / 1e6).toFixed(0)} MB`;

function showPicked(file) {
  picked = file;
  $("#up-what").textContent = file ? file.name : "Nothing chosen yet";
  const slug = ripSlug();
  $("#up-target").textContent = file
    ? `${mb(file.size)}   →   side a of ${slug || "a record you have not named yet"}`
    : "";
  $("#up-go").disabled = !file || sending;
}

// The page cannot re-read a File across a reload, so resuming means picking the
// same file again. The sidecar's name and size are what confirm it is the one.
async function loadIncoming() {
  showPicked(null);
  for (const id of ["#up-err", "#up-done", "#up-state"]) $(id).textContent = "";
  setBar(0);
  $("#up-cancel").hidden = true;
  const slug = ripSlug();
  if (!slug) return;
  try {
    const held = await api(`/api/ingest/${slug}`);
    if (!held.exists) return;
    setBar(held.total ? held.have / held.total : 0);
    $("#up-state").textContent =
      `${held.name} — ${mb(held.have)} of ${mb(held.total)} already here. `
      + `Pick the same file again to carry on.`;
    $("#up-cancel").hidden = false;
  } catch (e) { $("#up-err").textContent = e.message; }
}

// method first, deliberately: the frontend test's POST matcher does not allow a
// `)` between the path and `method:`, and file.slice(...) has one.
async function sendPiece(slug, at, blob) {
  return pushChunk(`/api/ingest/${slug}/chunk?at=${at}`, blob);
}

async function pushChunk(url, blob) {
  const r = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/octet-stream" },
    body: blob,
  });
  const body = await r.json().catch(() => null);
  if (!r.ok) {
    const e = new Error((body && body.error) || `chunk failed: ${r.status}`);
    e.status = r.status;
    e.needsConfirm = !!body?.needs_confirm;
    throw e;
  }
  return body;
}

const TRIES = 3;

async function uploadFile(file) {
  const slug = ripSlug();
  if (!slug) { $("#up-err").textContent = "name the record first"; return; }
  $("#up-err").textContent = ""; $("#up-done").textContent = "";
  sending = true; $("#up-go").disabled = true; $("#up-cancel").hidden = false;
  try {
    const begun = await postJSON(`/api/ingest/${slug}/begin`, {
      name: file.name, size: file.size, type: file.type,
      artist: $("#rip-artist").value.trim(),
      album: $("#rip-album").value.trim(),
      date: $("#md-date").value.trim(),
      replace: true,
    });
    let at = begun.have, failures = 0;
    while (at < file.size) {
      const end = Math.min(at + begun.chunk, file.size);
      try {
        at = (await sendPiece(slug, at, file.slice(at, end))).have;
        failures = 0;
      } catch (e) {
        // The server knows where it actually is; a mismatch is answered with
        // the real offset rather than a restart.
        const said = /is at (\d+)/.exec(e.message);
        if (e.status === 409 && said) { at = Number(said[1]); continue; }
        if (++failures >= TRIES) throw e;
        continue;
      }
      setBar(at / file.size);
      $("#up-state").textContent = `${mb(at)} of ${mb(file.size)}`;
    }
    $("#up-state").textContent = "normalising…";
    const done = await runJob(slug, `/api/ingest/${slug}/finish`, {}, "normalising");
    setBar(1);
    $("#up-state").textContent = "";
    $("#up-done").textContent =
      `wrote side-${done.side}.flac — ${fmt(done.seconds)}, ${mb(done.bytes)}`;
    $("#up-cancel").hidden = true;
    // Where a finished rip leaves you: the record open and measured, and you
    // choose when to go to Cut. setMode is only ever called by a tab click.
    await refreshAlbums(slug);
    status("side a is ready — check the boundaries in Cut");
  } catch (e) {
    $("#up-err").textContent = e.message;
    $("#up-go").textContent = "Resume";
  } finally {
    sending = false;
    $("#up-go").disabled = !picked;
  }
}

async function cancelIncoming() {
  const slug = ripSlug();
  if (!slug) return;
  if (!confirm("Discard what has arrived so far? This cannot be undone.")) return;
  try {
    const r = await postJSON(`/api/ingest/${slug}/cancel`, {});
    status(`discarded ${mb(r.freed_bytes)}`);
    $("#up-go").textContent = "Upload";
    await loadIncoming();
    if (r.forgot) await refreshAlbums(NEW_ALBUM);
  } catch (e) { $("#up-err").textContent = e.message; }
}

// Archiving ends that record's life in the working area - raw/, review/ and the
// cache are gone - so the capture pane has to stop naming it. Without this the
// fields survive until a page reload: the artist and album of the record you
// just finished, a side letter bumped past the end of it by `ripStop`, and a
// `→ raw/<slug>/side-c.flac` line pointing into a directory archive deleted a
// moment ago.
//
// Scoped to the album actually archived. If the fields hold a different name it
// was typed ahead of the next rip, and wiping it would destroy your typing;
// that check also covers a capture running for some other album, since the
// pane is then describing the capture in flight.
//
// Punch is deliberately left alone: it works on archived albums by design, so
// having just archived one is a reason to keep it selected, not to clear it.
function resetCaptureAfterArchive(slug) {
  if (!slug || S.slug !== slug) return;
  startNewAlbum();
  $("#rip-done").textContent = "";
  $("#rip-err").textContent = "";
}

// Replacing deletes files, so it is opt-in on every single import: the checkbox
// is cleared each time and Import stays disabled until it is ticked.
// A failed carry deliberately keeps the stashed file, so report where it is
// rather than just that the art is missing.
function artNote(a) {
  if (!a) return "";
  if (a.error) return `   ·   ART NOT RE-EMBEDDED: ${a.error} (kept at ${a.kept})`;
  return `   ·   art carried over (${a.size}, ${a.embedded}/${a.tracks} tracks)`;
}

// The tick is about the files named when it was given, so a different answer takes it
// back: a release changed after ticking would otherwise replace files nobody agreed to.
let offered = "";

function showReplaceOffer(existing, { keepTick = false } = {}) {
  const box = $("#imp-replace-wrap");
  const named = existing ? `${existing.tracks}|${existing.where}|${existing.folders}` : "";
  if (!keepTick || named !== offered) $("#imp-replace").checked = false;
  offered = named;
  if (!existing) {
    box.hidden = true;
    $("#imp-go").disabled = false;
    return;
  }
  box.hidden = false;
  // The server's own selection, the one Replace removes, so what is named is what goes.
  const more = existing.folders > 1 ? ` and ${existing.folders - 1} more folders` : "";
  $("#imp-replace-what").textContent =
    `${existing.tracks} files of ${existing.artist} \u2014 ${existing.album}, `
    + `in ${existing.where}${more}`
    + (existing.refused ? `\nReplace is refused: ${existing.refused}` : "");
  $("#imp-go").disabled = !$("#imp-replace").checked;
}

// Rows without files read as a copy already there and stop an import part-way,
// so they are shown before it is started rather than after. ADR-050.
function showStale(stale) {
  $("#imp-stale-wrap").hidden = !stale;
  if (stale) $("#imp-stale-what").textContent =
    `${stale.tracks} rows registered at ${stale.where}`;
}

async function clearStale() {
  const b = $("#imp-stale-clear");
  const was = b.textContent;
  b.disabled = true; b.textContent = "clearing\u2026";
  try {
    const r = await postJSON(`/api/library/clear-stale/${S.slug}`, { mbid: importMbid() });
    status(`cleared ${r.cleared} stale rows`);
    await checkExisting();
  } catch (e) {
    $("#imp-err").textContent = e.message;
  }
  b.disabled = false; b.textContent = was;
}

// Ask up front rather than finding out from a failed import. True regardless of
// which path is taken, so it does not wait for a release id.
// Only the latest question is answered: the release can change while the library is
// being listed. Import waits for the answer, since unticked it would file a second copy.
let existingAsked = 0;

async function checkExisting() {
  const asked = ++existingAsked;
  $("#imp-err").textContent = "";
  $("#imp-go").disabled = true;
  try {
    const mbid = encodeURIComponent(importMbid());
    const { existing, stale } = await api(`/api/library/existing/${S.slug}?mbid=${mbid}`);
    if (asked !== existingAsked) return;
    showStale(stale);
    showReplaceOffer(existing, { keepTick: true });
  } catch {
    if (asked !== existingAsked) return;
    showStale(null);
    // The import itself still refuses, so this is only the early warning - but
    // a warning that could not be checked must not read as one that was.
    showReplaceOffer(null, { keepTick: true });
  }
}

// The first letter this record has not got. Bumping the previous one by a
// character gave `{` after side z, and offered to record over side a whenever
// the panel was opened fresh on a record that already had one.
function nextFreeSide(sides) {
  const have = new Set(sides || []);
  for (let c = 97; c <= 122; c++) {
    const letter = String.fromCharCode(c);
    if (!have.has(letter)) return letter;
  }
  return "";
}

// The side to record. A side the record already has is a re-rip of it; the
// first one it has not got finishes a rip in progress. Offering only the
// latter meant a four-side record proposed `e`, so re-ripping side b was a
// correction rather than a choice. ADR-047.
function setSideChoices(sides) {
  const sel = $("#rip-side");
  const next = nextFreeSide(sides);
  sel.innerHTML = "";
  for (const letter of sides) {
    const o = el("option", "", `${letter}  (re-rip)`);
    o.value = letter;
    sel.appendChild(o);
  }
  if (next) {
    const o = el("option", "", sides.length ? `${next}  (next)` : next);
    o.value = next;
    sel.appendChild(o);
  }
  // Always the next one, never what was showing. This is rebuilt when the
  // record changes and when a side finishes recording, and both of those want
  // the side that comes next - keeping the old value left it pointing at the
  // side just captured, ready to record over it.
  sel.value = next || (sides[sides.length - 1] || "");
  ripSlug();
}

// The Rip panel takes its record from the selector at the top of the page.
// There is nothing else to choose it with, so the names are what the record is
// called rather than something to type over; a new one is started up there too.
function showRecordInRipPanel(artist, album, sides) {
  $("#rip-artist").value = artist || "";
  $("#rip-album").value = album || "";
  // read-only rather than disabled: still selectable and copyable, and it
  // makes the reason visible instead of the field just going dead
  $("#rip-artist").readOnly = $("#rip-album").readOnly = true;
  $("#rip-artist").title = $("#rip-album").title =
    "what this record is called — pick “— new album —” above to start another";
  setSideChoices(sides || []);
}

// A rip is a child of this service, so anything that restarts it strands a
// partial WAV. The audio is fine - it just never got encoded.
async function loadOrphans() {
  const box = $("#orphans");
  try {
    const { orphans } = await api("/api/rip/orphans");
    if (!orphans.length) { box.hidden = true; return; }
    box.hidden = false;
    box.innerHTML = "<h4>Interrupted capture</h4>";
    for (const o of orphans) {
      const m = Math.floor(o.seconds / 60), sec = Math.round(o.seconds % 60);
      const p = el("p", "", `${o.slug} side ${o.side} — ${m}:${String(sec).padStart(2, "0")} `
        + `(${(o.bytes / 1e6).toFixed(0)} MB) was cut short. The audio is intact; `
        + `it just never got encoded.`);
      const row = el("div", "row");
      const keep = el("button", "", o.kind === "punch" ? "Keep this punch" : `Keep as side ${o.side}`);
      keep.onclick = async () => {
        keep.disabled = true;
        try {
          const r = await postJSON("/api/rip/salvage",
                                   { slug: o.slug, side: o.side, kind: o.kind });
          await loadOrphans(); await refreshAlbums(o.slug);
          status(`salvaged side ${r.side} — ${fmt(r.duration)}`);
        } catch (e) { $("#rip-err").textContent = e.message; keep.disabled = false; }
      };
      const drop = el("button", "ghost", "Discard");
      drop.onclick = async () => {
        if (!confirm(`Delete the unfinished capture of side ${o.side}? This cannot be undone.`)) return;
        try {
          // Discard, not salvage with a flag on it: salvage ignored the flag
          // and encoded the capture, so a person who confirmed "this cannot be
          // undone" got a finished side instead of an empty orphan list.
          await postJSON("/api/rip/discard",
                         { slug: o.slug, side: o.side, kind: o.kind });
          await loadOrphans();
        } catch (e) { $("#rip-err").textContent = e.message; }
      };
      row.append(keep, drop);
      box.append(p, row);
    }
  } catch { box.hidden = true; }
}

// Finished captures for the selected album, each with a way out. `rip.start`
// refuses while side-X.flac exists, so without this a side taken at the wrong
// speed - or off the wrong input - means deleting files by hand, which leaves
// the analysis cache and the plan entry behind describing audio that is gone.
// "Align from archive" only means anything where a saved time is still on the archived
// capture and a fresh one is in raw/. A save ends that, so it is asked again after one.
async function refreshAlignButton() {
  const b = $("#realign");
  if (!S.slug || !S.meta?.sides?.length) { b.hidden = true; return; }
  try {
    const { sides } = await api(`/api/align/${S.slug}`);
    b.hidden = !sides.length;
    // The rail offers this by name, and it was drawn before this was known.
    showCutRail();
  } catch { /* leave it hidden */ }
}

async function loadRipSides() {
  const box = $("#ripsides");
  if (!S.slug) { box.hidden = true; return; }
  try {
    const { sides } = await api(`/api/rip/sides/${S.slug}`);
    if (!sides.length) { box.hidden = true; return; }
    box.hidden = false;
    box.innerHTML = "<h4>Sides on this record</h4>";
    for (const o of sides) {
      const row = el("div", "side");
      row.append(el("b", "", o.side));
      row.append(el("span", "", `${(o.bytes / 1e6).toFixed(0)} MB`
                                + (o.recording ? " · recording now" : "")));
      const open = el("button", "linky", "Open in Cut");
      open.type = "button";
      open.disabled = o.recording;
      open.onclick = () => openInCut(o.side);
      row.append(open);
      const drop = el("button", "ghost", "Discard");
      drop.disabled = o.recording;
      drop.onclick = async () => {
        if (!confirm(`Delete side ${o.side} of ${o.slug}?\n\n`
            + `The capture, its waveform cache and its entry in the plan all go. `
            + `This cannot be undone — you would have to rip the side again.`)) return;
        drop.disabled = true;
        try {
          const r = await postJSON("/api/rip/discard-side",
                                   { slug: o.slug, side: o.side });
          if (r.notes.length) logline(r.notes.join("\n"));
          await loadRipSides();
          await refreshAlbums(S.slug);
          status(`discarded side ${r.side} — freed ${(r.freed_bytes / 1e6).toFixed(0)} MB`);
        } catch (e) { $("#rip-err").textContent = e.message; drop.disabled = false; }
      };
      row.append(drop);
      box.append(row);
    }
  } catch { box.hidden = true; }
}

// The Cut tab, on a side of the record already open here.
function openInCut(letter) {
  setMode("cut");
  if (letter && S.sideData && letter !== S.side) openSide(letter)
    .catch((e) => status(`could not open side ${letter}: ${e.message}`, true));
}

// Auto-stop is a server-side timer, so the browser cannot hold it off - it can
// only ask for the clock to be restarted. The dialog is therefore a prompt, not
// a gate: if nobody is watching, the capture stops exactly as it would have.
let autostopShown = false;

function closeAutostopWarning(action) {
  const dlg = $("#autostopdlg");
  if (dlg.open) dlg.close();
  autostopShown = false;
  Promise.resolve(action()).catch((e) => { $("#rip-err").textContent = e.message; });
}

function updateAutostopWarning(st, running) {
  const dlg = $("#autostopdlg");
  const armed = running && st.autostop !== false && st.music != null;
  const left = armed ? Math.round((st.dwell || 120) - (st.quiet_for || 0)) : null;
  const warn = st.warn || 20;

  if (!armed || left == null || left > warn) {
    // the passage got loud again, the capture ended, or auto-stop was turned
    // off from elsewhere - either way there is nothing left to decide
    if (dlg.open) dlg.close();
    autostopShown = false;
    return;
  }
  $("#as-count").textContent = Math.max(0, left);
  $("#as-below").textContent = Math.round(st.below ?? 20);
  $("#as-quiet").textContent = `${Math.round(st.quiet_for)}s`;
  if (!autostopShown) {
    autostopShown = true;
    if (!dlg.open) dlg.showModal();
    $("#as-keep").focus();
  }
}

let ripDevices = [];

function fillRates(info) {
  const d = ripDevices.find((x) => x.id === $("#rip-device").value);
  const sel = $("#rip-rate");
  const prev = sel.value || localStorage.getItem("ripdoctor:rip:rate") || "48000";
  sel.innerHTML = "";
  // what the device actually accepts — offering 96k on hardware that cannot do
  // it just moves the failure into the middle of a side
  // A device that is busy cannot be asked what it accepts, and busy is the
  // ordinary case here: a capture is running. What it is configured for is
  // known either way, and is the only honest answer when it cannot be asked.
  const rates = (d && d.rates && d.rates.length) ? d.rates
              : (info && info.rate ? [info.rate] : []);
  // The configured rate, when this machine has one - it is the rate the
  // thresholds were measured at and the one the chain is actually running.
  const want = info && info.rate ? String(info.rate) : prev;
  for (const r of rates) {
    const o = el("option", "", `${r} Hz${String(r) === want ? "  (configured)" : ""}`);
    o.value = String(r);
    sel.appendChild(o);
  }
  sel.value = rates.map(String).includes(want)
    ? want
    : String(rates.includes(48000) ? 48000 : rates[0]);
  fillFormats(info);
}

const DEPTH = { S16_LE: "16-bit", S24_3LE: "24-bit", S32_LE: "32-bit" };

// Default to the WIDEST the device offers, not the narrowest. Capturing a
// 24-bit source as 16 throws the low eight bits away, silently - which is most
// of the reason for having a wider path in the first place.
function fillFormats(info) {
  const d = ripDevices.find((x) => x.id === $("#rip-device").value);
  const sel = $("#rip-format");
  const configured = (info && info.format) || "";
  const prev = configured
    || sel.value
    || localStorage.getItem("ripdoctor:rip:format")
    || "";
  sel.innerHTML = "";
  // Never a value of this file's own invention. A guess offered here is
  // recorded at: the configured depth is absent from a made-up list, so the
  // selection falls to the guess and a side is captured at the wrong width.
  const asked = !!(d && d.formats && d.formats.length);
  const formats = asked ? d.formats : (configured ? [configured] : []);
  for (const f of formats) {
    const best = asked && formats[0] === f ? "  (best here)" : "";
    const o = el("option", "", `${DEPTH[f] || f}${best}`);
    o.value = f;
    sel.appendChild(o);
  }
  if (!formats.length) {
    const o = el("option", "", "could not ask the device");
    o.value = ""; o.disabled = true;
    sel.appendChild(o);
  }
  sel.value = formats.includes(prev) ? prev : (formats[0] || "");
}

async function loadDevices() {
  try {
    const info = await api("/api/rip/devices");
    const devices = info.devices;
    ripDevices = devices;
    const sel = $("#rip-device"); sel.innerHTML = "";
    for (const d of devices) {
      const o = el("option", "", `${d.id} — ${d.name}${d.configured ? "  (configured)" : ""}`);
      o.value = d.id;
      sel.appendChild(o);
    }
    // The configured one, never simply the first. Sorted by card number, the
    // first is whatever the motherboard calls its own audio - which is how a
    // rip runs against an onboard codec with its input set to Rear Mic.
    if (devices.some((d) => d.id === info.configured)) {
      sel.value = info.configured;
    } else if (!devices.length) {
      $("#rip-err").textContent = "no capture device found — is the interface plugged in?";
    } else {
      sel.value = "";
      $("#rip-err").textContent = info.configured
        ? `${info.configured} is configured but this machine cannot see it — check the cable`
        : "no capture device is configured — set capture_device, or pick one below";
    }
    fillRates(info);
    sel.onchange = () => fillRates(info);
    const remember = () => {
      try {
        localStorage.setItem("ripdoctor:rip:rate", $("#rip-rate").value);
        localStorage.setItem("ripdoctor:rip:format", $("#rip-format").value);
      } catch {}
    };
    $("#rip-rate").onchange = remember;
    $("#rip-format").onchange = remember;   // depth is a choice too, not a side effect
  } catch (e) { $("#rip-err").textContent = e.message; }
}

// The band's last half minute, as this page polled it. It is drawing and nothing else: no
// request is made for it, and nothing that records reads it.
let bandSeen = [];

function showLevels(levels) {
  const at = performance.now() / 1000;
  bandSeen = historyAfter(bandSeen, at, levels?.band);
  drawMeter($("#meter"), levels);
  drawHistory($("#meter-history"), bandSeen, at);
}

function startRipPoll() {
  stopRipPoll();
  const tick = async () => {
    try {
      const st = await api("/api/rip/status");
      const on = st.running;
      $("#rip-go").disabled = on;
      $("#rip-stop").disabled = !on;
      $("#rip-abandon").disabled = !on;
      // one device, one capture: Record punch has to follow the same gate
      if (punchState) $("#punch-go").disabled = on || !!currentPunchTrack()?.punch;
      // Listen works idle too: a digital-only chain has no analogue monitoring
      // path, so this is the only way to hear the record at all.
      $("#rip-listen").disabled = false;
      // A capture ending kills the WAV-tail stream, so drop the player; the
      // human can hit Listen again and get the idle passthrough instead.
      if (!on && monitorEl && monitorEl.error) stopListen();
      updateAutostopWarning(st, on);
      const auto = $("#rip-auto");
      // keep the checkbox honest: the running capture owns the setting, not the
      // form, so a reload or a second tab shows what is actually in force
      if (on && st.autostop != null) $("#rip-autostop").checked = st.autostop;
      if (st.warning) {
        auto.textContent = `⚠ ${st.warning}`;
        auto.className = "err";
      } else if (st.reason) {
        auto.className = "dim";
        auto.textContent = `auto-stopped — ${st.reason}`;
      } else if (on && st.autostop === false) {
        auto.textContent = st.music != null
          ? `music level ${st.music} dB · auto-stop OFF — stop this side yourself`
          : "auto-stop OFF — stop this side yourself";
      } else if (on && st.music != null) {
        const q = st.quiet_for || 0;
        auto.textContent = q > 5
          ? `quiet for ${Math.round(q)}s — auto-stops at ${Math.round(st.dwell)}s`
          : `music level ${st.music} dB · auto-stop armed`;
      } else { auto.textContent = ""; auto.className = "dim"; }
      // While a stop is being waited out, the readout belongs to whoever is
      // waiting. Two writers on different intervals made it alternate between
      // "recording - N MB" and "stopping...", and the elapsed time snapped to
      // zero halfway through an encode that had not finished.
      if (!settling) $("#rip-elapsed").textContent = on ? fmt(st.elapsed) : "0:00.00";
      // Overruns are dropped samples - a permanent click in the archive. This
      // is the difference between "the monitor stuttered" (harmless, it reads
      // the file) and "the capture dropped audio" (redo the side).
      if (on && st.overruns) {
        $("#rip-done").textContent =
          `⚠ ${st.overruns} dropped buffer(s), ${st.overrun_ms} ms total — `
          + `this side has gaps in it and should be re-ripped`;
        $("#rip-done").className = "err";
      } else if (on) {
        $("#rip-done").className = "dim";
      }
      $("#rip-open-cut").hidden = on;
      if (!settling) {
        $("#rip-state").toggleAttribute("data-on", on);
        $("#rip-state").textContent = on
          ? `recording ${st.slug} side ${st.side} — ${(st.bytes / 1e6).toFixed(0)} MB`
            + (st.overruns ? `  ·  ${st.overruns} DROPPED` : "")
          : (st.stage === "encoding" ? "encoding…" : "idle");
      }
      showLevels(liveLevels(st));
    } catch {
      // The next poll says what is going on. Until then a needle left where it was
      // would be a level nobody measured.
      showLevels(null);
    }
  };
  tick();
  ripTimer = setInterval(tick, 500);
}
function stopRipPoll() { if (ripTimer) clearInterval(ripTimer); ripTimer = null; }

// Live monitoring. The capture device is held by arecord, so this plays a
// stream the server transcodes from the WAV being written — a couple of
// seconds behind, which is enough to hear that the needle is tracking.
let monitorEl = null;
// A pending auto-retry is state too. Without this, stopping during the retry
// window let the timer fire afterwards and start listening again on its own.
let listenRetry = null;

function listenButton(on) {
  const b = $("#rip-listen");
  b.textContent = on ? "Stop listening" : "Listen";
  b.classList.toggle("on", on);
}

function stopListen() {
  if (listenRetry) { clearTimeout(listenRetry); listenRetry = null; }
  if (!monitorEl) { listenButton(false); return; }
  const el = monitorEl;
  // Cleared BEFORE teardown so nothing that fires afterwards can mistake a
  // deliberate stop for a failure to start.
  monitorEl = null;
  el.onerror = null;
  el.pause();
  // NOT `src = ""`. An empty src resolves to the page's own URL, which is not
  // audio, so the element fires a real error event - and the error handler then
  // printed "could not start listening" every single time you pressed Stop.
  el.removeAttribute("src");
  el.load();
  listenButton(false);
}

// `auto` is the start that comes free with a recording: it retries quietly and
// never writes to the error line, because the human did not ask for it and a
// message they cannot act on is worse than silence.
function startListen({ auto = false, tries = 0 } = {}) {
  if (monitorEl) return;
  if (listenRetry) { clearTimeout(listenRetry); listenRetry = null; }
  // When idle the server opens the capture device itself, so it needs to know
  // which one and at what rate/depth; during a rip these are ignored and it
  // tails the WAV instead.
  const qs = new URLSearchParams({
    t: String(Date.now()),
    device: $("#rip-device").value || "",
    rate: String(parseInt($("#rip-rate").value, 10) || 48000),
    format: $("#rip-format").value || "S16_LE",
  });
  const el = new Audio(`/api/rip/monitor?${qs}`);
  monitorEl = el;
  el.volume = (parseInt($("#rip-vol").value, 10) || 80) / 100;

  const fail = (why, noRetry) => {
    if (monitorEl !== el) return;        // already stopped on purpose
    monitorEl = null;
    listenButton(false);
    // A rip that has only just started has not written its WAV yet, and
    // monitor_stream refuses until it has ("the capture has not written
    // anything yet"). That is a race, not a fault - wait and try again.
    if (auto && !noRetry && tries < 4) {
      listenRetry = setTimeout(() => {
        listenRetry = null;
        startListen({ auto, tries: tries + 1 });
      }, 600);
      return;
    }
    if (!auto) $("#rip-err").textContent =
      "could not start listening" + (why ? `: ${why}` : "");
  };

  el.onerror = () => fail("");
  el.play().then(() => {
    if (monitorEl !== el) return;
    listenButton(true);
  }).catch((e) => {
    // Autoplay refusal will not improve by retrying; the button still works.
    fail(e && e.message, e && e.name === "NotAllowedError");
  });
}

function toggleListen() {
  if (monitorEl) stopListen(); else startListen();
}

// ------------------------------------------------------------------ punch
//
// Re-record one track and swap it into the library. Recording reuses the whole
// rip path - device gate, meter, auto-stop, Listen, overrun counting - by
// passing kind:"punch", which only changes the filename. `punch-7.flac` does
// not match `side-*.flac`, so nothing else on the box can mistake it for a side.

let punchState = null;

// Punch works on the record the page is about, like everything else.
function punchSlug() { return S.slug || ""; }
function punchNum() { return parseInt($("#punch-track").value, 10) || 0; }


async function loadPunchTracks() {
  const sel = $("#punch-track");
  const slug = punchSlug();
  sel.innerHTML = "";
  punchState = null;
  $("#punch-found").hidden = true;
  $("#punch-err").textContent = "";
  if (!slug) { $("#punch-info").textContent = ""; return; }
  try {
    punchState = await api(`/api/punch/${slug}`);
  } catch (e) { $("#punch-err").textContent = e.message; return; }
  if (punchState.why) $("#punch-info").textContent = punchState.why;
  for (const t of punchState.tracks) {
    // A track can only be punched if there is something to fit against and
    // something to replace. Saying which is missing beats a dead entry.
    const bad = !t.side_audio ? "no side audio" : (!t.in_library ? "not in library" : "");
    const o = el("option", "", `${String(t.number).padStart(2, " ")}  ${t.title}`
      + `  (${fmt(t.length || 0)}, side ${t.side})`
      + (t.punch ? "  ← punch recorded" : "") + (bad ? `  — ${bad}` : ""));
    o.value = String(t.number);
    o.disabled = !!bad;
    sel.appendChild(o);
  }
  showPunchTrack();
}

function currentPunchTrack() {
  const n = punchNum();
  return (punchState?.tracks || []).find((t) => t.number === n) || null;
}

function showPunchTrack() {
  const t = currentPunchTrack();
  const recording = !$("#rip-stop").disabled;
  $("#punch-found").hidden = true;
  if (!t) {
    $("#punch-info").textContent = punchState?.why || "";
    $("#punch-go").disabled = true;
    $("#punch-locate").hidden = $("#punch-discard").hidden = true;
    return;
  }
  $("#punch-info").textContent =
    `${t.title} — side ${t.side}, ${t.start.toFixed(2)}–${t.end.toFixed(2)} on the `
    + `archived side (${fmt(t.length)})`
    + (t.punch ? `   ·   punch recorded: ${fmt(t.punch.seconds)}, `
                 + `${(t.punch.bytes / 1e6).toFixed(0)} MB` : "");
  $("#punch-go").disabled = recording || !!t.punch;
  $("#punch-locate").hidden = !t.punch;
  $("#punch-discard").hidden = !t.punch;
}

async function punchRecord() {
  const t = currentPunchTrack();
  if (!t) return;
  $("#punch-err").textContent = ""; $("#punch-done").textContent = "";
  if (!confirm(`Record a punch for ${t.number}. ${t.title}?\n\n`
             + `Drop the needle a few seconds BEFORE it and lift AFTER it — the `
             + `fit needs music on both sides of the track. Stop & encode ends it.`)) return;
  try {
    await postJSON("/api/rip/start", {
      slug: punchSlug(), side: String(t.number), kind: "punch",
      device: $("#rip-device").value,
      rate: parseInt($("#rip-rate").value, 10) || 48000,
      format: $("#rip-format").value || "S16_LE",
      autostop: false,          // a punch is short and deliberate; never guess
      force_device: $("#rip-force").checked });
    stopListen();
    startListen({ auto: true });
    status("punch recording — Stop & encode when the track has finished");
  } catch (e) { $("#punch-err").textContent = e.message; }
}

async function punchLocate() {
  const t = currentPunchTrack();
  if (!t) return;
  $("#punch-err").textContent = "";
  $("#punch-locate").disabled = true;
  try {
    const r = await runJob(punchSlug(), `/api/punch/${punchSlug()}/locate`,
                           { number: t.number }, "correlating");
    const f = r.fit;
    $("#punch-fit").textContent =
      `matched at r=${f.r}` + (f.check_miss != null ? `, midpoint off by ${f.check_miss}s` : "")
      + `   ·   offset ${f.offset}s, `
      + (f.scale_assumed ? "speed assumed identical (track too short to measure)"
                         : `drift ${f.drift_ms_per_min} ms/min`);
    $("#punch-start").value = r.start.toFixed(3);
    $("#punch-end").value = r.end.toFixed(3);
    punchLen();
    $("#punch-found").hidden = false;
    status(`found it — ${fmt(r.length)} against ${fmt(t.length)} archived`);
  } catch (e) { $("#punch-err").textContent = e.message; }
  $("#punch-locate").disabled = false;
}

function punchLen() {
  const a = parseFloat($("#punch-start").value), b = parseFloat($("#punch-end").value);
  $("#punch-len").value = (isFinite(a) && isFinite(b) && b > a) ? (b - a).toFixed(2) : "";
}

let punchAudio = null;
function punchPlay() {
  if (punchAudio) { punchAudio.pause(); punchAudio = null; $("#punch-play").textContent = "Hear this cut"; return; }
  const qs = new URLSearchParams({ number: String(punchNum()),
    start: $("#punch-start").value || "0", end: $("#punch-end").value || "0" });
  punchAudio = new Audio(`/api/punch/${punchSlug()}/audio?${qs}`);
  punchAudio.volume = (parseInt($("#rip-vol").value, 10) || 80) / 100;
  punchAudio.onended = () => { punchAudio = null; $("#punch-play").textContent = "Hear this cut"; };
  punchAudio.onerror = () => { punchAudio = null; $("#punch-play").textContent = "Hear this cut"; };
  punchAudio.play().then(() => { $("#punch-play").textContent = "Stop"; })
    .catch((e) => { $("#punch-err").textContent = e.message; punchAudio = null; });
}

async function punchApply() {
  const t = currentPunchTrack();
  if (!t) return;
  const a = parseFloat($("#punch-start").value), b = parseFloat($("#punch-end").value);
  if (!(isFinite(a) && isFinite(b) && b > a)) {
    $("#punch-err").textContent = "start and end must be numbers, end after start";
    return;
  }
  if (!confirm(`Replace "${t.title}" in the library with ${fmt(b - a)} of the punch?\n\n`
             + `The old file and the punch capture move to archive/_punched/. `
             + `Tags and cover art are carried across from the file being replaced.`)) return;
  $("#punch-err").textContent = "";
  $("#punch-apply").disabled = true;
  try {
    const r = await postJSON(`/api/punch/${punchSlug()}/apply`,
                             { number: t.number, start: a, end: b });
    $("#punch-done").textContent =
      `replaced ${t.number}. ${t.title} — was ${fmt(r.was_seconds)}, now ${fmt(r.now_seconds)}`
      + (r.art_carried ? ", art carried" : ", NO ART on the old file")
      + `   ·   old take kept at ${r.backup}`;
    if (punchAudio) punchPlay();
    await loadPunchTracks();
    status("track replaced");
  } catch (e) { $("#punch-err").textContent = e.message; }
  $("#punch-apply").disabled = false;
}

async function punchDiscard() {
  const t = currentPunchTrack();
  if (!t || !t.punch) return;
  if (!confirm(`Throw away the punch capture for "${t.title}"? It is not in the `
             + `library and is not archived; this deletes it.`)) return;
  try {
    await postJSON(`/api/punch/${punchSlug()}/discard`, { number: t.number });
    await loadPunchTracks();
    status("punch discarded");
  } catch (e) { $("#punch-err").textContent = e.message; }
}

async function ripStart() {
  $("#rip-err").textContent = ""; $("#rip-done").textContent = "";
  const slug = ripSlug();
  if (!slug) { $("#rip-err").textContent = "artist and album are required"; return; }
  try {
    await postJSON("/api/rip/start", {
      slug, side: $("#rip-side").value.trim(), device: $("#rip-device").value,
      // The names go with the capture. They were typed into this form and
      // lived nowhere else, so a reload left the record with audio, a slug
      // and no way to start its second side.
      artist: $("#rip-artist").value.trim(), album: $("#rip-album").value.trim(),
      rate: parseInt($("#rip-rate").value, 10) || 48000,
      format: $("#rip-format").value || "S16_LE",
      autostop: $("#rip-autostop").checked,
      force_device: $("#rip-force").checked });
    $("#rip-force-wrap").hidden = true; $("#rip-force").checked = false;
    // Hearing the record is part of starting a rip, not a second decision.
    // Standalone Listen stays for cueing with nothing recording.
    //
    // Tear down first. If you were already listening, that stream was the idle
    // passthrough - and rip.start has just called _stop_passthru() to take the
    // device back from it, so it is dead whatever the element still thinks.
    // Without this, `if (monitorEl) return` would skip the restart and leave
    // the button reading "Stop listening" over silence.
    stopListen();
    startListen({ auto: true });
  } catch (e) {
    $("#rip-err").textContent = e.message;
    // a device refusal is recoverable, so offer the override next to the error
    // rather than leaving Start looking broken
    if (/turntable|capture device/.test(e.message)) $("#rip-force-wrap").hidden = false;
  }
}

// Stopping is not finishing. The request only asks the capture to stop; the
// side then has to be encoded, which for a twenty-minute WAV is most of a
// minute. Reporting at the moment of asking showed `NaN MB` for a file that
// did not exist yet, and - worse - looked for unfinished captures while the
// one just made was still on disk, so a completed rip was offered back as
// wreckage to salvage.
async function whenCaptureSettles(limit = 600) {
  const state = $("#rip-state");
  for (let i = 0; i < limit; i++) {
    const st = await api("/api/rip/status");
    if (!st.running && st.stage !== "encoding") return st;
    // Named rather than "encoding…", because a side takes most of a minute
    // and a person who has just pressed Stop wants to know it was heard.
    state.className = "dim working";
    state.textContent = st.stage === "encoding"
      ? `encoding side ${st.side} — a full side takes about a minute`
      : "stopping the capture…";
    await new Promise((r) => setTimeout(r, 800));
  }
  throw new Error("the capture is taking longer than expected to finish");
}

async function ripStop() {
  // Claimed before anything is awaited. Claiming it when the wait began left
  // the meter poll a round trip to write the recording line back over the
  // answer to a button somebody had already pressed.
  settling = true;
  $("#rip-err").textContent = "";
  $("#rip-state").className = "dim working";
  $("#rip-state").textContent = "stopping the capture…";
  $("#rip-stop").disabled = true;
  try {
    await postJSON("/api/rip/stop", {});
    const r = await whenCaptureSettles();
    if (r.error) throw new Error(r.error);
    if (r.kind === "punch") {
      // Not a side: no letter to bump, and the next step is Find the track.
      $("#rip-done").textContent =
        `wrote punch-${r.side}.flac — ${fmt(r.duration)}, ${(r.bytes / 1e6).toFixed(0)} MB`;
      await loadPunchTracks();
      status("punch captured — now Find the track");
      return;
    }
    $("#rip-done").textContent =
      `wrote side-${r.side}.flac — ${fmt(r.duration)}, ${(r.bytes / 1e6).toFixed(0)} MB`
      + (r.overruns ? `   ·   ${r.overruns} overruns, ${r.overrun_ms} ms lost` : "");
    // refreshAlbums reopens the record, which reloads the re-rip list and
    // moves the side letter to the one it has not got - worked out from what
    // is on disk rather than by bumping the letter in the box
    await refreshAlbums(r.slug);
    await loadOrphans();
  } catch (e) {
    $("#rip-err").textContent = e.message;
  } finally {
    // A stop that failed before the wait began would otherwise leave the
    // sweep running over a capture nothing is doing anything about.
    // The button belongs to the meter poll, which re-enables it when
    // something is recording; another writer here is read elsewhere as
    // "a capture is running" and would be lying.
    settling = false;
    $("#rip-state").className = "dim";
  }
}

// ------------------------------------------------------------------- boot

// A drag lost part way moved nothing, as on Tapes. One let go saves, once.
let cutDragFrom = null;
function cutEdgeDropped(num, edge, landed) {
  const was = cutDragFrom;
  cutDragFrom = null;
  if (!was) return;
  if (!landed) {
    Object.assign(was.tr, { start: was.start, end: was.end, forced: was.forced, settled: was.settled, placed: was.placed });
    status("the drag was interrupted, so nothing moved", true);
    ed.render(); updateReadout(); renderTracks();
  }
  markDirty();
}

function wire() {
  ed = new Editor($("#cv"),
    (num, edge, t) => {                       // marker dragged
      const tr = trackByNum(num); if (!tr) return;
      const d = S.sideData[S.side].duration;
      t = Math.max(0, Math.min(d, t));
      if (refusedCrossing(tr, edge, t)) return;
      cutDragFrom ??= { tr, start: tr.start, end: tr.end, forced: tr.forced, settled: tr.settled, placed: tr.placed };
      if (edge === "start") tr.start = Math.min(t, tr.end - 0.2);
      else tr.end = Math.max(t, tr.start + 0.2);
      setByHand(tr);
      markDirty(true, null); ed.render(); updateReadout(); renderTracks();
    },
    (num, edge) => { select(num, edge, true); },
    (t) => { if (player.playing) player.seek(t); },
    { doubt: canCheck, onSong: (num) => select(num, "start"), onDrop: cutEdgeDropped });

  player = new Player((t) => { ed.playhead = t; ed.render(); });
  new ResizeObserver(() => ed.render()).observe($("#cv"));
  for (const type of ["play", "pause", "ended"]) player.el.addEventListener(type, showCutTools);
  $("#cut-prev").onclick = () => stepTrack(-1);
  $("#cut-next").onclick = () => stepTrack(1);
  $("#cut-edge-start").onclick = () => chooseEdge("start");
  $("#cut-edge-end").onclick = () => chooseEdge("end");
  $("#cut-minus").onclick = (e) => nudgeSelected(e.shiftKey ? -0.5 : -0.05);
  $("#cut-plus").onclick = (e) => nudgeSelected(e.shiftKey ? 0.5 : 0.05);
  $("#cut-play").onclick = playOrPause;
  $("#cut-audition").onclick = auditionSelected;
  $("#cut-checked").onclick = checkSelected;
  $("#cut-split").onclick = splitAtPlayhead;
  $("#cut-zoom-track").onclick = zoomToSelected;
  $("#cut-zoom-side").onclick = () => ed.fit();
  showCutTools();
  wireTrackPresses();
  wirePressedControl();
  wireTapeWave();
  $("#vol").oninput = (e) => player.setVolume(e.target.value / 100);
  $("#helpbtn").onclick = () => $("#helpdlg").showModal();
  $("#help-close").onclick = () => $("#helpdlg").close();

  $("#cut-save-retry").onclick = () => {
    if (!saveFail?.stale) { flushSave(); return; }
    if (!confirm("Load what is saved? What this page shows and could not save goes.")) return;
    markDirty(false);
    S.sideData = {};
    openAlbum(S.slug);
  };
  addEventListener("pagehide", saveOnLeaving);
  setInterval(noticeDisk, 4000);
  $("#split").onclick = split;
  $("#album-q").oninput = () => renderAlbums(S.slug);
  $("#firstpass").onclick = openFirstPass;
  $("#cut-review").onclick = reviewNext;
  $("#cut-doubt").onclick = reviewNext;
  $("#cut-in").onclick = () => zoomBy(ed, 0.5);
  $("#cut-out").onclick = () => zoomBy(ed, 2);
  $("#cut-rip").onclick = () => setMode("rip");
  railOpens($("#cut-rail"), $("#cut-rail-bar"));
  $("#import").onclick = openImport;
  $("#art-upload").onclick = () => $("#art-file").click();
  $("#art-file").onchange = (e) => { if (e.target.files[0]) uploadArt(e.target.files[0]); e.target.value = ""; };
  for (const id of ["#md-artist", "#md-album", "#md-date"]) {
    $(id).oninput = () => showCutRail();    // the heading says what these are about to save
    $(id).onchange = () => markDirty();
  }
  $("#imp-search").onclick = async () => {
    $("#imp-err").textContent = "";
    try {
      await searchReleases($("#imp-artist").value, $("#imp-album").value,
                           $("#imp-results"), (r) => {
                             $("#imp-mbid").value = r.id;
                             showCatalogueState();
                             checkExisting();
                             $("#imp-results").innerHTML = "";
                             // Picking here used to set the id in the box and
                             // nowhere else: the plan kept the release chosen in
                             // First pass, so the Archive gate went on querying
                             // an id that no longer described the album.
                             S.meta.mbid = r.id;
    markDirty();          // buildDoc sends this, so picking a release is an edit
                             offerRelabel(r);
                           }, { requireDurations: false });
    } catch (e) { $("#imp-err").textContent = e.message; }
  };
  $("#imp-relabel").onclick = relabelRun;
  $("#punch-track").onchange = showPunchTrack;
  $("#punch-go").onclick = punchRecord;
  $("#punch-locate").onclick = punchLocate;
  $("#punch-discard").onclick = punchDiscard;
  $("#punch-apply").onclick = punchApply;
  $("#punch-play").onclick = punchPlay;
  for (const id of ["#punch-start", "#punch-end"]) $(id).oninput = punchLen;
  $("#imp-replace").onchange = () => {
    $("#imp-go").disabled = !$("#imp-replace").checked;
  };
  $("#imp-art-fetch").onclick = artFetch;
  $("#imp-art-upload").onclick = () => $("#art-file").click();
  $("#imp-art-skip").onclick = () => { $("#imp-art-wrap").hidden = true; };
  $("#imp-stale-clear").onclick = clearStale;
  $("#tp-new-form").onsubmit = (e) => { e.preventDefault(); createTape(); };
  $("#tp-new").onclick = () => showNaming(true);
  $("#tp-new-cancel").onclick = () => showNaming(false);
  $("#tp-pick").onchange = (e) => { $("#tp-err").textContent = ""; openTape(e.target.value); };
  const pickFile = () => $("#tp-file").click();
  $("#tp-file-pick").onclick = pickFile;
  $("#tp-start-file").onclick = pickFile;
  $("#tp-cover-file").onchange = (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file && tpCoverFor != null) uploadCover(tpCoverFor, file);
  };
  $("#tp-file").onchange = (e) => {
    tapePicked = e.target.files[0] || null;
    e.target.value = "";
    if (tapePicked) sendTape(tapePicked);
  };
  $("#tp-go").onclick = () => { if (tapePicked) sendTape(tapePicked); };
  $("#tp-cancel").onclick = cancelTapeUpload;
  const pickList = () => $("#tp-csv").click();
  $("#tp-csv-pick").onclick = pickList;
  $("#tp-start-csv").onclick = pickList;
  $("#tp-csv").onchange = (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) sendTapeList(file);
  };
  $("#tp-add").onclick = startDraft;
  $("#tp-list-add").onclick = startDraft;
  $("#tp-start-add").onclick = startDraft;
  $("#tp-process").onclick = processTape;
  $("#tp-start-find").onclick = processTape;
  $("#tp-review-next").onclick = () => stepDoubt(1);
  $("#tp-match").onclick = () => matchTape(false);
  $("#tp-rematch").onclick = () => matchTape(true);
  $("#tp-next-album").onclick = stepAlbum;
  $("#tp-all-list").onclick = () => { tpAllAsk = true; showRail(tape); };
  railOpens($("#tp-rail"), $("#tp-rail-bar"));
  $("#tp-cut").onclick = cutTape;
  $("#tp-file-songs").onclick = fileTape;
  $("#tp-playlist").onclick = writePlaylist;
  $("#tp-archive").onclick = archiveTape;
  $("#tp-remove").onclick = removeTape;
  $("#pc-pick").onclick = () => $("#pc-file").click();
  $("#pc-file").onchange = (e) => {
    pcPicked = e.target.files[0] || null;
    e.target.value = "";
    $("#pc-go").disabled = !pcPicked;
    $("#pc-what").textContent = pcPicked
      ? `${pcPicked.name} — ${mb(pcPicked.size)}` : "Nothing chosen yet";
  };
  $("#pc-go").onclick = () => { if (pcPicked) sendPrecut(pcPicked); };
  $("#pc-import").onclick = takePrecut;
  $("#pc-cancel").onclick = cancelPrecut;
  $("#up-pick").onclick = () => $("#up-file").click();
  $("#up-file").onchange = (e) => {
    showPicked(e.target.files[0]);
    e.target.value = "";   // so re-picking the same file fires again
  };
  $("#up-go").onclick = () => { if (picked) uploadFile(picked); };
  $("#up-cancel").onclick = cancelIncoming;
  for (const id of ["#rip-artist", "#rip-album"])
    $(id).addEventListener("input", () => { if (picked) showPicked(picked); });
  $("#imp-mbid").oninput = showCatalogueState;
  $("#imp-mbid").onchange = checkExisting;
  $("#imp-fromplan").onchange = () => { showCatalogueState(); checkExisting(); };
  $("#imp-go").onclick = runImport;
  $("#imp-close").onclick = () => $("#impdlg").close();
  $("#pc-dup").onchange = showPrecutReplace;
  $("#pc-replace").onchange = () => { $("#pc-import").disabled = !$("#pc-replace").checked; };
  $("#imp-next").onclick = () => { $("#impdlg").close(); openArchive(); };
  $("#archive").onclick = openArchive;
  $("#arc-go").onclick = archiveRun;
  $("#arc-close").onclick = () => $("#arcdlg").close();
  $("#fp-search").onclick = fpSearch;
  $("#fp-cancel").onclick = () => $("#fpdlg").close();
  $("#fp-done").onclick = () => $("#fpdlg").close();
  for (const b of document.querySelectorAll("#modes button"))
    b.onclick = () => setMode(b.dataset.mode);
  $("#as-keep").onclick = () => closeAutostopWarning(() => postJSON("/api/rip/snooze", {}));
  $("#as-off").onclick = () => closeAutostopWarning(async () => {
    await postJSON("/api/rip/snooze", {});
    await postJSON("/api/rip/autostop", { on: false });
    $("#rip-autostop").checked = false;
  });
  $("#as-stop").onclick = () => closeAutostopWarning(ripStop);
  $("#rip-autostop").onchange = async () => {
    const on = $("#rip-autostop").checked;
    try {
      // only meaningful while something is recording; otherwise the checkbox is
      // just the value that will be sent with the next Start.
      const st = await api("/api/rip/status");
      if (!st.running) return;
      await postJSON("/api/rip/autostop", { on });
      status(on ? "auto-stop on" : "auto-stop off — stop this side yourself");
    } catch (e) { $("#rip-err").textContent = e.message; }
  };
  $("#imp-replace").onchange = (e) => { $("#imp-go").disabled = !e.target.checked; };
  $("#rip-vol").oninput = () => {
    const v = (parseInt($("#rip-vol").value, 10) || 0) / 100;
    if (monitorEl) monitorEl.volume = v;
    try { localStorage.setItem("ripdoctor:rip:vol", $("#rip-vol").value); } catch {}
  };
  try {
    const v = localStorage.getItem("ripdoctor:rip:vol");
    if (v) $("#rip-vol").value = v;
  } catch {}
  $("#rip-go").onclick = ripStart;
  $("#rip-stop").onclick = () => { $("#rip-stop").disabled = true; ripStop(); };
  $("#rip-abandon").onclick = async () => {
    // The point of Abandon is skipping the encode, so it must not route through
    // ripStop(). Confirm hard: this is unrecoverable and it is a button sitting
    // right next to Stop.
    const st = await api("/api/rip/status").catch(() => null);
    const mins = st?.elapsed ? ` (${Math.floor(st.elapsed / 60)}m ${Math.round(st.elapsed % 60)}s so far)` : "";
    if (!confirm(`Abandon this capture${mins}?\n\n`
        + `The audio is deleted without being encoded. Use this when the take is `
        + `already wrong — wrong speed, wrong input, mistracking. This cannot be undone.`)) return;
    $("#rip-abandon").disabled = $("#rip-stop").disabled = true;
    try {
      const r = await postJSON("/api/rip/abandon", {});
      status(`abandoned side ${r.side} after ${fmt(r.seconds)} — `
             + `${(r.freed_bytes / 1e6).toFixed(0)} MB discarded`);
      if (r.note) logline(r.note);
      await loadRipSides();
      await refreshAlbums(S.slug);
    } catch (e) {
      $("#rip-err").textContent = e.message;
      $("#rip-abandon").disabled = false;
    }
  };
  $("#rip-listen").onclick = toggleListen;
  $("#rip-open-cut").onclick = () => openInCut();
  $("#up-open-cut").onclick = () => openInCut();
  // A fresh load starts on a new album with empty fields and side a. These used
  // to be restored from localStorage, so opening the Rip panel showed whatever
  // record was last ripped - which is both confusing and, with a pinned re-rip,
  // a way to start recording onto an album you did not mean. Within a session
  // the side letter still auto-bumps after each Stop, so the a-then-b flow is
  // unaffected.
  for (const id of ["#rip-artist", "#rip-album"]) {
    $(id).value = "";
    $(id).oninput = ripSlug;
  }
  $("#rip-side").onchange = ripSlug;
  try {
    for (const id of ["#rip-artist", "#rip-album", "#rip-side"])
      localStorage.removeItem(`ripdoctor:rip:${id}`);
  } catch {}
  ripSlug();
  loadDevices();
  loadOrphans();
  loadRipSides();

  $("#logtoggle").onclick = () =>
    setLogCollapsed(!$("#logwrap").classList.contains("collapsed"));
  $("#logclear").onclick = () => { $("#log").textContent = ""; $("#logwrap").hidden = true; };
  try { setLogCollapsed(localStorage.getItem("ripdoctor:log:collapsed") === "1"); } catch {}
  $("#realign").onclick = async () => {
    if (S.dirty && !(await save())) return;
    await refreshAlignButton();
    if ($("#realign").hidden) return;       // what just saved put the side on this capture
    $("#realign").disabled = true;
    try {
      const r = await runJob(S.slug, `/api/align/${S.slug}`, {}, "aligning");
      for (const a of r.aligned) {
        S.bySide[a.side] = a.tracks.map((t) => ({ ...t }));
        logline(`side ${a.side}: offset ${a.offset > 0 ? "+" : ""}${a.offset}s, `
          + `drift ${a.drift_ms_per_min} ms/min, worst check ${a.check_miss}s `
          + `— ${a.tracks.length} boundaries moved`);
      }
      for (const p of r.problems) logline(`side ${p.side}: ${p.why}`);
      // the audio genuinely changed, so nobody has listened to these yet
      markDirty();
      renderTracks();
      ed.setTracks(tracks());
      updateReadout();
      const n = r.aligned.reduce((a, x) => a + x.tracks.length, 0);
      status(r.aligned.length
        ? `aligned ${n} boundaries across ${r.aligned.length} side(s) — listen`
        : "nothing could be aligned — see the log", !r.aligned.length);
    } catch (e) {
      status(`align failed: ${e.message}`, true); logline(e.message, true);
    }
    $("#realign").disabled = false;
  };
  $("#album").onchange = async (e) => {
    if (S.dirty && !(await save()) && !confirm("This record's last change is not saved. Leave it anyway?")) {
      e.target.value = S.slug || NEW_ALBUM;   // put the picker back
      return;
    }
    if (e.target.value === NEW_ALBUM) { startNewAlbum(); return; }
    try { await openAlbum(e.target.value); }
    catch (err) { status(`load failed: ${err.message}`, true); logline(String(err.stack || err), true); }
  };
  $("#splittrack").onclick = splitAtPlayhead;
  $("#addtrack").onclick = () => {
    if (refusedByJob()) return;
    const list = tracks();
    const at = ed.playhead;
    const num = 1 + Math.max(0, ...Object.values(S.bySide).flat().map((t) => t.number));
    list.push({ number: num, title: "Untitled", start: at, end: Math.min(at + 60, S.sideData[S.side].duration), settled: "hand" });
    list.sort((a, b) => a.start - b.start);
    markDirty(); renderTracks(); ed.setTracks(list);
  };

  addEventListener("keydown", (e) => {
    if (["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName)) return;
    // a shortcut firing behind an open dialog once split a track while the
    // First-pass dialog had focus
    if (document.querySelector("dialog[open]")) return;
    if (e.key === "Enter" && !e.ctrlKey && !e.metaKey && !e.altKey && (e.repeat || enterIsTheControls(e))) return;
    if (document.body.dataset.mode === "tapes") { tapeKey(e); return; }
    // Alt-left is the browser going back, and ctrl-S its save. Shift is these keys' larger step.
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.key === "?") { e.preventDefault(); $("#helpdlg").showModal(); return; }
    if (e.key === "Escape") { player.stop(); return; }
    // The record's editor is behind the other pages, where its keys would act unseen. It is
    // the page a load starts on, before any mode has been set.
    if ((document.body.dataset.mode ?? "cut") !== "cut") return;
    const t = selectedTrack();
    if (e.code === "Space") {
      e.preventDefault();
      // A button keeps the focus after a press, and would be pressed again by this.
      if (e.target.tagName === "BUTTON") e.target.blur();
      playOrPause();
    } else if (e.key === "a" || e.key === "A") {
      if (t) { e.preventDefault(); auditionSelected(); }
    } else if (e.key === "Enter") {
      e.preventDefault(); $("#cut-checked").click();
    } else if (NUDGE_KEYS[e.key]) {
      if (nudgeSelected((e.shiftKey ? 0.5 : 0.05) * NUDGE_KEYS[e.key])) e.preventDefault();
    } else if (e.key === "n" || e.key === "N") {
      e.preventDefault(); reviewNext(e.shiftKey ? -1 : 1);
    } else if (e.key === "e" || e.key === "E") {
      e.preventDefault(); swapEdge();
    } else if (e.key === "j" || e.key === "J") {
      e.preventDefault(); $("#cut-next").click();
    } else if (e.key === "k" || e.key === "K") {
      e.preventDefault(); $("#cut-prev").click();
    } else if (e.key === "s" || e.key === "S") {
      e.preventDefault(); splitAtPlayhead();
    } else if (e.key === "f" || e.key === "F" || e.key === "0") {
      e.preventDefault(); ed.fit();
    } else if (e.key === "z" || e.key === "Z") {
      if (t) { e.preventDefault(); zoomToSelected(); }
    }
  });

}

// Enter on a control reached from the keyboard presses that control. One that only kept the
// focus after a mouse press lets go of it, or the key would press it again. `:focus-visible`
// cannot tell them apart here, since a browser turns it on for the key being asked about.
const CONTROLS = "button,a[href],summary";
let pressedControl = null;
function wirePressedControl() {
  addEventListener("pointerdown", (e) => { pressedControl = e.target.closest?.(CONTROLS) ?? null; }, true);
  addEventListener("focusout", (e) => { if (e.target === pressedControl) pressedControl = null; });
}

function enterIsTheControls(e) {
  const control = e.target.closest?.(CONTROLS);
  if (!control) return false;
  if (control !== pressedControl) return true;
  e.preventDefault();
  control.blur();
  return false;
}

async function boot() {
  try {
    await api("/api/me");
  } catch { $("#login").hidden = false; $("#app").hidden = true; return; }
  $("#login").hidden = true; $("#app").hidden = false;
  if (!ed) wire();
  // Anything that fails from here on must surface in the UI, not vanish into
  // an unhandled rejection - the app is already visible and would just sit
  // there looking empty.
  try {
    await refreshAlbums();
    // A reload on a tape stays there; a job still going is picked up by opening it.
    if (recalled("rd-mode") === "tapes") setMode("tapes");
  } catch (e) {
    status(`load failed: ${e.message}`, true);
    logline(String(e.stack || e));
  }
}

$("#loginform").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  $("#loginerr").textContent = "";
  try {
    await postJSON("/api/login", { user: f.get("user"), password: f.get("password") });
    await boot();
  } catch (err) { $("#loginerr").textContent = err.message; }
});

boot();
