import { Editor, fmt } from "/editor.js";
import { Player } from "/audio.js";
import { drawMeter, slugify } from "/ripui.js";

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
  if (!r.ok) throw new Error(body?.error || `${r.status} ${r.statusText}`);
  return body;
}
const postJSON = (path, obj) =>
  api(path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(obj) });

const S = {
  slug: null, album: null, side: null,
  earPassOverride: null,          // set by align, consumed by the next save
  meta: null,                     // {album, artist, date, sides, ready, tracks_by_side}
  bySide: {},                     // letter -> [track]
  sideData: {},                   // letter -> {duration, windowMs, rms, peak, band, gaps}
  dirty: false,
};

let ed, player, ripTimer;

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
function markDirty(d = true) {
  S.dirty = d;
  $("#save").textContent = d ? "Save •" : "Save";
  if (d) saveDraft(); else dropDraft();
}

// ------------------------------------------------------------ envelope I/O

async function fetchEnvelope(slug, side, v, expectWindows) {
  // ?v= is the capture's fingerprint: same audio -> cache hit, re-rip -> miss
  const r = await fetch(`/api/env/${slug}/${side}${v ? `?v=${v}` : ""}`,
                        { credentials: "same-origin" });
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
    await postJSON(`/api/prepare/${S.slug}`, {});
    await pollPrepare(box);
  } finally {
    box.hidden = true;                    // even when the poll throws
  }
  S.meta = await api(`/api/album/${S.slug}`);
}

async function pollPrepare(box) {
  for (;;) {
    const j = await api(`/api/prepare/${S.slug}`);
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
  for (;;) {
    await new Promise((r) => setTimeout(r, 800));
    const j = await api(`/api/job/${slug}`);
    if (j.done) {
      if (j.error) throw new Error(j.error);
      return j.result;
    }
    if (note) status(`${note}… ${Math.round((Date.now() - t0) / 1000)}s`, true);
  }
}

// ------------------------------------------------------------ album/side

// Nothing loaded: blank the canvas, empty the panels, disable the actions.
function clearAlbum(message) {
  S.slug = null; S.meta = { sides: [], album: "", artist: "", date: "", ready: {}, recording: [] };
  S.bySide = {}; S.sideData = {}; S.side = null;
  player?.stop();
  ed?.clear();
  $("#pipeline").innerHTML = "";
  $("#sides").innerHTML = "";
  $("#tracks tbody").innerHTML = "";
  $("#sp-letter").textContent = "—";
  $("#sp-meta").textContent = message || "";
  $("#review").innerHTML = `<p class="dim">${message || "no album loaded"}</p>`;
  $("#ro-time").textContent = "—";
  $("#ro-track").textContent = "";
  $("#ro-gap").textContent = ""; $("#ro-gap").className = "";
  // per-album, and loadRipSides only runs from openAlbum - so without this it
  // keeps listing captures for a record that is no longer loaded
  $("#ripsides").hidden = true;
  for (const id of ["#save", "#split", "#import", "#archive", "#firstpass", "#revert",
                    "#addtrack", "#splittrack"])
    $(id).disabled = true;
  markDirty(false);
}

// Where you are in rip -> first pass -> cut -> import -> archive, and what the
// next step needs. The information already existed (the archive gate computes
// most of it); it was just never shown, so "why is that button grey" had no
// answer on screen.
async function renderPipeline() {
  const box = $("#pipeline");
  if (!S.slug) { box.innerHTML = ""; return; }
  const tracks = Object.values(S.bySide).flat();
  let review = 0, arc = null;
  try { review = (await api(`/api/review/${S.slug}`)).tracks.length; } catch {}
  try { arc = await api(`/api/archive/${S.slug}?album=${encodeURIComponent(S.meta.album || "")}`); } catch {}

  const imported = !!(arc && arc.library_tracks);
  const steps = [
    { k: "rip", label: `rip · ${S.meta.sides.length} side${S.meta.sides.length === 1 ? "" : "s"}`,
      done: S.meta.sides.length > 0 },
    { k: "fit", label: `first pass · ${tracks.length} tracks`, done: tracks.length > 0,
      why: "no tracks yet — run First pass, or add them by hand" },
    { k: "cut", label: `cut · ${review || (imported ? "moved" : 0)}`, done: review > 0 || imported,
      why: "nothing in review/ — press Cut tracks" },
    { k: "imp", label: `import${imported ? " · " + arc.library_tracks : ""}`, done: imported,
      why: "not in the library yet — press Import" },
    { k: "arc", label: "archive", done: !!(arc && !arc.sides.length),
      why: arc && arc.why ? arc.why : "ready to archive" },
  ];
  const nextIdx = steps.findIndex((x) => !x.done);
  box.innerHTML = "";
  steps.forEach((st, i) => {
    if (i) box.appendChild(el("span", "sep", "›"));
    const cls = st.done ? "st done" : (i === nextIdx ? "st now" : "st");
    const e = el("span", cls, (st.done ? "✓ " : "") + st.label);
    if (!st.done && st.why) e.title = st.why;
    box.appendChild(e);
  });
  if (nextIdx >= 0 && steps[nextIdx].why)
    box.appendChild(el("span", "why", steps[nextIdx].why));
}

function enableActions() {
  for (const id of ["#save", "#split", "#import", "#firstpass", "#revert",
                    "#addtrack", "#splittrack"])
    $(id).disabled = false;
}

// The old-format notice is a one-shot. It describes a permanent condition -
// those albums are finished and archived - so repeating it on every refresh is
// noise, not information.
let oldFormatNoted = false;

async function refreshAlbums(keep) {
  const inc = $("#incarch").checked;
  const { albums } = await api(`/api/albums${inc ? "?archive=1" : ""}`);
  const usable = albums.filter((a) => !a.old_format);
  const skipped = albums.length - usable.length;
  const sel = $("#album"); sel.innerHTML = "";
  for (const a of usable) {
    const o = el("option", "", a.where === "archive" ? `${a.slug}  (archived)` : a.slug);
    o.value = a.slug;
    sel.appendChild(o);
  }
  if (!usable.length) {
    // Nothing in raw/ is normal once everything has been archived, and this
    // reports it rather than papering over it. `archived` is never ticked for
    // you: those albums are finished, and
    // auto-loading one puts a completed record on the canvas looking like
    // pending work. An empty picker is the honest answer - it leaves S.slug
    // null, which is why every handler that needs an album has to say so
    // (§3) rather than quietly doing nothing.
    const msg = inc ? "no editable albums found"
                    : "nothing in raw/ — rip a side, or tick “archived” to browse finished ones";
    clearAlbum(msg);
    status(msg, true);
    return;
  }
  const pick = usable.some((a) => a.slug === keep) ? keep : usable[0].slug;
  sel.value = pick;
  // Only worth mentioning for albums still in raw/, where an unreadable plan
  // actually blocks work. An archived album is finished - imported and filed -
  // so its plan format is nobody's problem and saying so is just noise.
  const stuck = albums.filter((a) => a.old_format && a.where !== "archive");
  if (stuck.length && !oldFormatNoted) {
    oldFormatNoted = true;
    logline(`not editable here, superseded "cuts" plan format: `
            + `${stuck.map((a) => a.slug).join(", ")} — re-run refit2.py to migrate`);
  }
  await openAlbum(pick);
}

async function openAlbum(slug) {
  // the transcript refers to the old album's tracks by number; replaying it
  // against a different record is worse than starting fresh
  S.slug = slug;
  status("loading…", true);
  S.meta = await api(`/api/album/${slug}`);
  await ensurePrepared();
  S.bySide = {};
  for (const letter of S.meta.sides) {
    S.bySide[letter] = (S.meta.tracks_by_side[letter] || []).map((t) => ({ ...t }));
  }
  S.sideData = {};
  enableActions();
  $("#md-artist").value = S.meta.artist || "";
  $("#md-album").value = S.meta.album || "";
  $("#md-date").value = S.meta.date || "";
  loadArtState();
  renderSideTabs();
  const rec = S.meta.recording || [];
  const first = S.meta.sides.find((s) => !rec.includes(s));
  if (first) await openSide(first);
  else {
    status(`every side of ${slug} is still recording`, true);
    renderSideTabs();
  }
  loadReview();
  loadRipSides();               // per-album, so it follows the selection
  refreshAlignButton();
  refreshArchiveButton();
  renderPipeline();
  markDirty(false);
  status("");
}

function renderSideTabs() {
  const box = $("#sides"); box.innerHTML = "";
  const rec = S.meta.recording || [];
  for (const letter of S.meta.sides) {
    const recording = rec.includes(letter);
    const b = el("button", `${letter === S.side ? "on " : ""}${recording ? "rec" : ""}`,
                 recording ? `${letter} ●rec` : letter);
    b.disabled = recording;
    b.title = recording ? "still being recorded — stop the rip, then hit Revert" : `side ${letter}`;
    b.onclick = () => openSide(letter);
    box.appendChild(b);
  }
}

async function openSide(letter) {
  S.side = letter;
  renderSideTabs();
  if ((S.meta.recording || []).includes(letter)) {
    status(`side ${letter} is still recording — stop the rip, then hit Revert`, true);
    return;
  }
  player?.stop();          // otherwise the old side keeps playing under the new one
  if (!S.sideData[letter]) {
    const v = S.meta.ready[letter]?.v || "";
    const [env, gaps] = await Promise.all([
      fetchEnvelope(S.slug, letter, v, S.meta.ready[letter]?.windows),
      api(`/api/gaps/${S.slug}/${letter}`),
    ]);
    S.sideData[letter] = { ...env, gaps, duration: S.meta.ready[letter].duration };
  }
  const d = S.sideData[letter];
  ed.load(d, S.bySide[letter]);
  player.load(`/api/audio/${S.slug}/${letter}`
              + (S.meta.ready[letter]?.v ? `?v=${S.meta.ready[letter].v}` : ""));
  $("#sp-letter").textContent = letter;
  $("#sp-meta").textContent =
    `${fmt(d.duration)} · ${d.gaps.band.gaps.length} silent regions · threshold ${d.gaps.band.threshold} dB`;
  renderTracks();
}

// ------------------------------------------------------------ track table

function tracks() { return S.bySide[S.side] || []; }
function trackByNum(n) { return tracks().find((t) => t.number === n); }

function renderTracks() {
  const tb = $("#tracks tbody"); tb.innerHTML = "";
  for (const t of tracks()) {
    const tr = el("tr");
    if (ed.sel && ed.sel.track === t.number) tr.className = "sel";

    const num = el("td"); const ni = el("input"); ni.className = "num";
    ni.value = t.number; ni.type = "number";
    ni.onchange = () => { t.number = parseInt(ni.value, 10) || t.number; markDirty(); renderTracks(); ed.render(); };
    num.appendChild(ni); tr.appendChild(num);

    const ti = el("input"); ti.value = t.title;
    ti.onchange = () => { t.title = ti.value; markDirty(); };
    const td = el("td"); td.appendChild(ti); tr.appendChild(td);

    for (const edge of ["start", "end"]) {
      const c = el("td", "t", fmt(t[edge]));
      c.title = `${t[edge].toFixed(2)} s`;
      c.onclick = () => { select(t.number, edge); ed.fitRange(t.start, t.end); };
      tr.appendChild(c);
    }

    const len = t.end - t.start;
    tr.appendChild(el("td", "t", fmt(len)));
    const dr = el("td", "drift");
    if (t.cat) {
      const d = len - t.cat;
      dr.textContent = (d >= 0 ? "+" : "") + d.toFixed(2);
      if (Math.abs(d) > 10) dr.className = "drift big";
    } else dr.textContent = "—";
    tr.appendChild(dr);

    const del = el("td");
    const db = el("button", "ghost", "×");
    db.title = "delete track";
    db.onclick = () => {
      if (!confirm(`Delete track ${t.number} “${t.title}”?`)) return;
      S.bySide[S.side] = tracks().filter((x) => x !== t);
      markDirty(); renderTracks(); ed.setTracks(tracks());
    };
    del.appendChild(db); tr.appendChild(del);
    tb.appendChild(tr);
  }
}

const round2 = (v) => Math.round(v * 100) / 100;

// ------------------------------------------------------------------- drafts
//
// Marker edits live only in browser memory, so a reload or a crash loses them.
// These are a per-browser convenience only — the plan on disk stays
// authoritative, and a draft is discarded the moment you Save.
const DRAFT = (slug) => `ripdoctor:draft:${slug}`;
let draftTimer = null;

function saveDraft() {
  if (!S.slug || !S.dirty) return;
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => {
    try {
      localStorage.setItem(DRAFT(S.slug), JSON.stringify({
        at: Date.now(), side: S.side, bySide: S.bySide,
      }));
    } catch { /* private mode, quota — a draft is a nicety, never a blocker */ }
  }, 800);
}

function dropDraft(slug) {
  try { localStorage.removeItem(DRAFT(slug || S.slug)); } catch {}
}

function readDraft(slug) {
  try {
    const raw = localStorage.getItem(DRAFT(slug));
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

function offerDraft(slug) {
  const d = readDraft(slug);
  if (!d || !d.bySide) return false;
  const when = new Date(d.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const n = Object.values(d.bySide).flat().length;
  if (!confirm(`Unsaved changes to this album from ${when} (${n} tracks).\n\n`
             + `Restore them? Cancel discards the draft and loads what is on disk.`)) {
    dropDraft(slug);
    return false;
  }
  for (const [letter, tracks] of Object.entries(d.bySide))
    S.bySide[letter] = tracks.map((t) => ({ ...t }));
  markDirty(true);
  return true;
}

// Split the track under the playhead in two.
//
// If the playhead sits inside a detected silence, the two new edges are placed
// the way refit2 places them - tail after the outgoing music, lead before the
// incoming - so the second track gets its lead-in instead of starting on the
// first note. Outside a gap the split lands exactly where you clicked, because
// a "split here" that quietly moves somewhere else is worse than a blunt one.
function splitAtPlayhead() {
  const at = ed.playhead;
  const list = tracks();
  const t = list.find((x) => x.start < at && at < x.end);
  if (!t) { status("playhead is not inside a track — click inside one first", true); return; }
  if (at - t.start < 0.5 || t.end - at < 0.5) { status("too close to an existing boundary", true); return; }

  const LEAD = 1.3, TAIL = 1.5;
  let prevEnd = at, nextStart = at, note = "split at playhead";
  const g = (S.sideData[S.side].gaps?.band?.gaps || []).find((x) => x.lo <= at && at <= x.hi);
  if (g) {
    prevEnd = Math.min(g.lo + TAIL, g.hi - 0.2);
    nextStart = Math.max(g.hi - LEAD, g.lo + 0.2);
    if (nextStart < prevEnd) prevEnd = nextStart = (g.lo + g.hi) / 2;
    note = `split inside gap ${g.lo.toFixed(2)}–${g.hi.toFixed(2)}`;
  }

  // everything at or after the insertion point shifts up one, across all sides
  const newNum = t.number + 1;
  for (const x of Object.values(S.bySide).flat()) if (x.number >= newNum) x.number += 1;

  list.push({ number: newNum, title: "Untitled", start: round2(nextStart), end: t.end });
  t.end = round2(prevEnd);
  list.sort((a, b) => a.start - b.start);
  markDirty(); renderTracks(); ed.setTracks(list);
  select(newNum, "start");
  status(note);
}

function select(num, edge) {
  ed.sel = num == null ? null : { track: num, edge };
  ed.render(); renderTracks(); updateReadout();
}

// ---------------------------------------------------------------- readout

function updateReadout() {
  const sel = ed.sel;
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
    `${sel.edge} of ${t.number} “${t.title}” — ${len.toFixed(2)}s` +
    (d == null ? "" : ` (cat ${t.cat.toFixed(2)}, ${d >= 0 ? "+" : ""}${d.toFixed(2)})`);

  // the nudge2 question: is this cut still inside a real silence?
  const gs = S.sideData[S.side].gaps.band.gaps;
  let inside = null, nearest = null, nd = 1e9;
  for (const g of gs) {
    if (g.lo <= v && v <= g.hi) { inside = g; break; }
    const dd = Math.min(Math.abs(v - g.lo), Math.abs(v - g.hi));
    if (dd < nd) { nd = dd; nearest = g; }
  }
  const ro = $("#ro-gap");
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
    const r = await fetch(`/api/artwork/${S.slug}`, {
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

async function save() {
  try {
    await postJSON(`/api/plan/${S.slug}`, buildDoc());
    S.earPassOverride = null;
    markDirty(false);
    dropDraft();
    status("saved plan + spec");
  } catch (e) { status(`save failed: ${e.message}`, true); alert(`Save failed:\n${e.message}`); }
}

async function split() {
  if (S.dirty && !confirm("You have unsaved changes. Cut using the last saved plan?")) return;
  $("#split").disabled = true;
  status("cutting…", true);
  try {
    const r = await runJob(S.slug, `/api/split/${S.slug}`, {}, "cutting");
    logline(r.stdout || r.stderr);
    status(r.ok ? "split complete" : "split failed");
    loadReview();
    renderPipeline();
  } catch (e) { status(`split failed: ${e.message}`, true); logline(e.message, true); }
  $("#split").disabled = false;
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
    lines.push("");
    lines.push("then remove:");
    for (const d of st.will_remove) lines.push("  " + d);
    if (!st.ready) lines.push("\nNOT READY: " + st.why);
    $("#arc-plan").textContent = lines.join("\n");
    $("#arc-go").disabled = !st.ready;
  } catch (e) {
    $("#arc-plan").textContent = "";
    $("#arc-err").textContent = e.message;
  }
}

async function archiveRun() {
  // S.slug moves when the picker is rebuilt below, so remember what was archived
  const done = S.slug;
  if (!confirm("Archive the raw sides and delete raw/, review/ and the cache?\n\n"
             + "Each side is verified in archive/ before anything is removed.")) return;
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
    status("archived and cleaned up");
    await renderPipeline();
    $("#arcdlg").close();
    resetCaptureAfterArchive(done);
    await refreshAlbums();
    await loadRerip();          // the album moved raw/ -> archive/
    await loadOrphans();
  } catch (e) {
    $("#arc-err").textContent = e.message;
    $("#arc-go").disabled = false;
  }
}

// ------------------------------------------------------------------ import

function openImport() {
  $("#imp-mbid").value = S.meta.mbid || "";
  $("#imp-err").textContent = "";
  $("#imp-outwrap").hidden = true;
  $("#imp-done").hidden = true;
  $("#imp-go").hidden = true;
  $("#imp-next").hidden = true;
  $("#imp-go").hidden = false;
  $("#imp-close").textContent = "Close";
  $("#imp-results").innerHTML = "";
  $("#imp-relabel-wrap").hidden = true;
  const g = guessFromSlug(S.slug);
  $("#imp-artist").value = S.meta.artist || g.artist;
  $("#imp-album").value = S.meta.album || g.album;
  $("#imp-replace").checked = false;
  $("#imp-go").disabled = false;
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
             + `change. No boundary moves. Unsaved edits to titles are lost.`)) return;
  $("#imp-err").textContent = "";
  $("#imp-relabel").disabled = true;
  try {
    const r = await postJSON(`/api/relabel/${S.slug}`, {
      mbid: relabelPick.id, artist: $("#imp-artist").value,
      album: $("#imp-album").value, date: relabelPick.date });
    $("#imp-relabel-wrap").hidden = true;
    S.sideData = {};
    await openAlbum(S.slug);
    $("#imp-mbid").value = r.mbid;
    status(`re-labelled ${r.applied} tracks from ${r.release} — boundaries untouched`);
  } catch (e) {
    $("#imp-err").textContent = e.message;
  }
  $("#imp-relabel").disabled = false;
}

// An import that lands with no art used to be reported nowhere, which meant
// finding out from a blank tile in a player days later. Ask at the moment it
// happens, and offer both answers.
function offerArt(art) {
  const wrap = $("#imp-art-wrap");
  $("#imp-art-err").textContent = "";
  if (!art || art.complete) { wrap.hidden = true; return; }
  wrap.hidden = false;
  $("#imp-art-what").textContent =
    `${art.embedded} of ${art.tracks} tracks carry a picture`
    + (art.cover_file ? "   ·   cover.jpg is on disk but not embedded" : "");
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
  if (S.dirty && !confirm("Import the saved plan? Unsaved edits are not in it."))
    return;
  if (!confirm(`Tag and place ${tracks} tracks as "${$("#imp-artist").value} \u2014 `
             + `${$("#imp-album").value}"?\n\nThe files move out of review/.`)) return;
  $("#imp-err").textContent = "";
  $("#imp-out").textContent = "tagging and placing\u2026";
  $("#imp-outwrap").hidden = false;
  $("#imp-go").disabled = true;
  try {
    const r = await runJob(S.slug, `/api/import/${S.slug}`, {}, "importing");
    $("#imp-summary").textContent = `${r.tracks} tracks \u2192 ${r.library}`;
    $("#imp-done").hidden = false;
    $("#imp-outwrap").hidden = true;
    $("#imp-go").hidden = true;
    $("#imp-close").textContent = "Not yet";
    await loadArtState();
    await loadReview();
    await refreshArchiveButton();
    await renderPipeline();
    $("#imp-next").hidden = $("#archive").disabled;
    status("imported into the library");
  } catch (e) {
    $("#imp-err").textContent = e.message;
    $("#imp-outwrap").hidden = true;
    $("#imp-go").disabled = false;
  }
}

// -------------------------------------------------------------- first pass

function guessFromSlug(slug) {
  const [a, b] = slug.split("---");
  const un = (x) => (x || "").replace(/-/g, " ").trim();
  return { artist: un(a), album: un(b) };
}

let fpReleases = [];

function openFirstPass() {
  const existing = Object.values(S.bySide).flat().length;
  if (existing && !confirm(
      `This replaces the plan for all ${S.meta.sides.length} sides — ${existing} tracks, `
      + `including any boundaries you have already corrected. Continue?`)) return;
  const g = guessFromSlug(S.slug);
  $("#fp-artist").value = S.meta.artist || g.artist;
  $("#fp-album").value = S.meta.album || g.album;
  fpReleases = [];
  renderReleases();
  $("#fp-err").textContent = "";
  $("#fp-report").hidden = true;
  $("#fp-done").hidden = true;
  $("#fp-cancel").hidden = false;
  $("#fpdlg").showModal();
}

function renderReleaseList(box, releases, onPick, { requireDurations = true } = {}) {
  box.innerHTML = "";
  for (const r of releases) {
    const dur = r.durations == null ? "?" : (r.durations === 0 ? "NO DURATIONS" :
      `${Math.round(r.total / 60)} min`);
    // MusicBrainz keeps pressing qualifiers here, not in the title — without it
    // a widely reissued record returns dozens of rows that all read the same
    const label = r.title + (r.disambiguation ? `  \u00b7 ${r.disambiguation}` : "");
    const dead = requireDurations && r.durations === 0;
    const b = el("button", dead ? "nodur" : (r.vinyl ? "vinyl" : ""),
      `${r.date || "----"}  ${r.country || "--"}  ${(r.formats.join(",") + "          ").slice(0, 14)}  ${r.tracks} tracks  ${dur}   ${label}`);
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

async function fpRun(rel) {
  $("#fp-err").textContent = "";
  $("#fp-report").hidden = true;
  $("#fp-results").innerHTML = "<p class=\"dim\">fitting…</p>";
  try {
    const r = await runJob(S.slug, `/api/firstpass/${S.slug}`,
                           { mbid: rel.id }, "fitting");
    $("#fp-results").innerHTML = "";
    $("#fp-report").hidden = false;
    $("#fp-report").textContent = (r.warning ? `WARNING: ${r.warning}\n\n` : "") + r.report;
    $("#fp-done").hidden = false;
    $("#fp-cancel").hidden = true;
    S.sideData = {};
    await openAlbum(S.slug);
    status("first pass applied — read the delta column, then check by ear");
  } catch (e) {
    // keep the release list up so another one can be tried straight away
    renderReleases();
    $("#fp-err").textContent = e.message;
  }
}

// -------------------------------------------------------------------- modes

function setMode(mode) {
  for (const b of document.querySelectorAll("#modes button"))
    b.classList.toggle("on", b.dataset.mode === mode);
  // Rip and Punch are separate modes over ONE capture pane: same device, same
  // transport, same meter. Only the left-hand form swaps.
  const capturing = mode === "rip" || mode === "punch";
  $("#cv").hidden = capturing;
  $("#captureview").hidden = !capturing;
  $("#ripform").hidden = mode !== "rip";
  $("#punchform").hidden = mode !== "punch";
  $("#readout").hidden = capturing;
  $("#lower").hidden = capturing;
  if (capturing) startRipPoll(); else stopRipPoll();
  if (mode === "rip") loadOrphans();
  if (mode === "punch") loadPunchAlbums();
  if (!capturing) ed.render();
}

// --------------------------------------------------------------------- rip

// A re-rip has to land on the SAME slug as the album it is replacing, or the
// archived capture cannot be found and align has nothing to correlate against.
// Deriving the slug from retyped text makes that hinge on spelling the artist
// identically twice, so when an existing album is picked the slug is pinned to
// it and the name fields go read-only.
let ripPinned = null;
let ripPinnedSides = "";

function ripSlug() {
  const side = $("#rip-side").value.trim();
  if (ripPinned) {
    $("#rip-slug").textContent =
      `→ raw/${ripPinned}/side-${side}.flac   ·   re-rip · existing sides: ${ripPinnedSides || "none"}`;
    return ripPinned;
  }
  const a = $("#rip-artist").value.trim(), b = $("#rip-album").value.trim();
  const slug = a && b ? slugify(a, b) : "";
  $("#rip-slug").textContent = slug ? `→ raw/${slug}/side-${side}.flac` : "";
  return slug;
}

// Archiving ends that record's life in the working area - raw/, review/ and the
// cache are gone - so the capture pane has to stop naming it. Without this the
// fields survive until a page reload: the artist and album of the record you
// just finished, a side letter bumped past the end of it by `ripStop`, and a
// `→ raw/<slug>/side-c.flac` line pointing into a directory archive deleted a
// moment ago.
//
// A pin is the sharp end of it. The archived album is still in
// `/api/albums?archive=1`, so `renderRerip` re-selects it, the name fields stay
// read-only, and Start would recreate `raw/<slug>/` for a record cleaned up
// thirty seconds earlier - the failure §1 exists to prevent.
//
// Scoped to the album actually archived. If the fields hold a different name it
// was typed ahead of the next rip, and wiping it would destroy your typing;
// that check also covers a capture running for some other album, since the
// pane is then describing the capture in flight.
//
// Punch is deliberately left alone: it works on archived albums by design, so
// having just archived one is a reason to keep it selected, not to clear it.
function resetCaptureAfterArchive(slug) {
  if (!slug || ripSlug() !== slug) return;
  ripPinned = null; ripPinnedSides = "";
  for (const id of ["#rip-artist", "#rip-album"]) {
    $(id).value = ""; $(id).readOnly = false; $(id).title = "";
  }
  $("#rip-side").value = "a";
  $("#rip-done").textContent = "";
  $("#rip-err").textContent = "";
  ripSlug();
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

function showReplaceOffer(existing, { keepTick = false } = {}) {
  const box = $("#imp-replace-wrap");
  if (!keepTick) $("#imp-replace").checked = false;
  if (!existing) {
    box.hidden = true;
    $("#imp-go").disabled = false;
    return;
  }
  box.hidden = false;
  $("#imp-replace-what").textContent =
    `${existing.tracks} files already in ${existing.where}`;
  // Importing writes into that same directory, so a track with the same name
  // is overwritten. The tick is the acknowledgement; nothing is deleted.
  $("#imp-go").disabled = !$("#imp-replace").checked;
}

// Ask up front rather than finding out from a failed import. True regardless of
// which path is taken, so it does not wait for a release id.
async function checkExisting() {
  try {
    const { existing } = await api(`/api/library/existing/${S.slug}`);
    showReplaceOffer(existing, { keepTick: true });
  } catch { /* the import itself still refuses; this is only the early warning */ }
}

let ripAlbums = [];

async function loadRerip() {
  try {
    const { albums } = await api("/api/albums?archive=1");
    // Every record on disk, not only the ones with a saved cut. A side just
    // captured has no plan yet and therefore no artist or album anywhere but
    // in the form somebody typed them into - which a reload throws away. That
    // is exactly the record you want to offer, because side b comes next.
    ripAlbums = albums.map((a) => ({ ...a, ...named(a) }));
  } catch { ripAlbums = []; }
  renderRerip();
}

// A <select> cannot be typed into, and this list only grows. The filter narrows
// the options rather than replacing the control, so the select stays the single
// source of truth for what is pinned.
// What to call a record. The plan when there is one, and otherwise the slug
// read backwards - it was built from an artist and an album, so it gives them
// back, give or take the punctuation. A guess offered for correction, in a
// field that can be corrected.
function named(a) {
  if (a.artist || a.album) return { artist: a.artist, album: a.album };
  return guessFromSlug(a.slug);
}

function renderRerip() {
  const sel = $("#rip-rerip");
  const q = $("#rip-rerip-q").value.trim().toLowerCase();
  const keep = ripPinned;
  sel.innerHTML = "";
  sel.appendChild(el("option", "", "— new album —"));
  let shown = 0;
  for (const a of ripAlbums) {
    const sides = (a.sides || []).join(" ") || "no sides yet";
    const label = `${a.artist} — ${a.album}  (${a.where}: ${sides})`;
    if (q && !label.toLowerCase().includes(q) && !a.slug.toLowerCase().includes(q)) continue;
    const o = el("option", "", label);
    o.value = a.slug;
    o.dataset.artist = a.artist || "";
    o.dataset.album = a.album || "";
    o.dataset.sides = (a.sides || []).join(" ");
    sel.appendChild(o);
    shown++;
  }
  // keep the pinned album selectable even when the filter would hide it
  if (keep && !Array.from(sel.options).some((o) => o.value === keep)) {
    const a = ripAlbums.find((x) => x.slug === keep);
    if (a) {
      const o = el("option", "", `${a.artist} — ${a.album}  (pinned)`);
      o.value = a.slug; o.dataset.artist = a.artist; o.dataset.album = a.album;
      o.dataset.sides = (a.sides || []).join(" ");
      sel.appendChild(o); shown++;
    }
  }
  sel.value = keep || "";
  $("#rip-rerip-q").placeholder = q && !shown
    ? "no album matches that" : "filter by artist or album…";
}

// A rip is a child of this service, so anything that restarts it strands a
// partial WAV. The audio is fine - it just never got encoded.
async function loadOrphans() {
  const box = $("#orphans");
  try {
    const { orphans } = await api("/api/rip/orphans");
    if (!orphans.length) { box.hidden = true; return; }
    box.hidden = false;
    box.innerHTML = "<h4>unfinished capture</h4>";
    for (const o of orphans) {
      const m = Math.floor(o.seconds / 60), sec = Math.round(o.seconds % 60);
      const p = el("p", "", `${o.slug} side ${o.side} — ${m}:${String(sec).padStart(2, "0")} `
        + `(${(o.bytes / 1e6).toFixed(0)} MB) was cut short. The audio is intact; `
        + `it just never got encoded.`);
      const row = el("div", "row");
      const keep = el("button", "", "Keep it — encode to FLAC");
      keep.onclick = async () => {
        keep.disabled = true;
        try {
          const r = await postJSON("/api/rip/salvage", { slug: o.slug, side: o.side });
          status(`salvaged side ${r.side} — ${fmt(r.duration)}`);
          await loadOrphans(); await refreshAlbums(o.slug);
        } catch (e) { $("#rip-err").textContent = e.message; keep.disabled = false; }
      };
      const drop = el("button", "ghost", "Discard");
      drop.onclick = async () => {
        if (!confirm(`Delete the unfinished capture of side ${o.side}? This cannot be undone.`)) return;
        try {
          await postJSON("/api/rip/salvage", { slug: o.slug, side: o.side, discard: true });
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
// "Align from archive" only means anything when the same album exists in both
// places: an archived capture to correlate against, and a fresh one in raw/.
async function refreshAlignButton() {
  const b = $("#realign");
  b.hidden = true;
  if (!S.slug || !S.meta?.sides?.length) return;
  try {
    const { albums } = await api("/api/albums?archive=1");
    // one entry per slug, and raw shadows archive - so the archived original is
    // reported by a flag on the raw entry, not as a second row
    const a = albums.find((x) => x.slug === S.slug);
    b.hidden = !(a && a.where === "raw" && a.archived_copy);
  } catch { /* leave it hidden */ }
}

async function loadRipSides() {
  const box = $("#ripsides");
  if (!S.slug) { box.hidden = true; return; }
  try {
    const { sides } = await api(`/api/rip/sides/${S.slug}`);
    if (!sides.length) { box.hidden = true; return; }
    box.hidden = false;
    box.innerHTML = "<h4>captures on disk</h4>";
    for (const o of sides) {
      const row = el("div", "row");
      row.append(el("span", "", `side ${o.side} — ${(o.bytes / 1e6).toFixed(0)} MB`
                                + (o.recording ? " · recording now" : "")));
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
          status(`discarded side ${r.side} — freed ${(r.freed_bytes / 1e6).toFixed(0)} MB`);
          if (r.notes.length) logline(r.notes.join("\n"));
          await loadRipSides();
          await refreshAlbums(S.slug);
        } catch (e) { $("#rip-err").textContent = e.message; drop.disabled = false; }
      };
      row.append(drop);
      box.append(row);
    }
  } catch { box.hidden = true; }
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
  const rates = (d && d.rates && d.rates.length) ? d.rates : [44100, 48000, 96000];
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

// Default to the WIDEST the device offers, not the narrowest. The Waxwing sends
// 24-bit and capturing it as 16 throws the low 8 bits away, which is most of
// the reason for the optical path in the first place.
function fillFormats(info) {
  const d = ripDevices.find((x) => x.id === $("#rip-device").value);
  const sel = $("#rip-format");
  const prev = (info && info.format)
    || sel.value
    || localStorage.getItem("ripdoctor:rip:format")
    || "";
  sel.innerHTML = "";
  const formats = (d && d.formats && d.formats.length) ? d.formats : ["S16_LE"];
  for (const f of formats) {
    const o = el("option", "", `${DEPTH[f] || f}${formats[0] === f ? "  (best here)" : ""}`);
    o.value = f;
    sel.appendChild(o);
  }
  sel.value = formats.includes(prev) ? prev : formats[0];
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
      // Listen works idle too now - it is the only way to hear the record at
      // all since the Waxwing went digital and its RCA jacks went quiet.
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
      } else if (!on && st.levels) {
        auto.className = "dim";
        // deliberately no peak here: this is a 42 ms window and it jumps every
        // poll, which is unreadable. `ripdoctor probe` is where gain is set.
        auto.textContent = `monitoring input — full ${st.levels.full} dB · `
                         + `1–3 kHz ${st.levels.band} dB`;
      } else { auto.textContent = ""; auto.className = "dim"; }
      $("#rip-elapsed").textContent = on ? fmt(st.elapsed) : "0:00.00";
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
      $("#rip-state").textContent = on
        ? `recording ${st.slug} side ${st.side} — ${(st.bytes / 1e6).toFixed(0)} MB`
          + (st.overruns ? `  ·  ${st.overruns} DROPPED` : "")
        : (st.stage === "encoding" ? "encoding…" : "idle");
      // levels arrive while merely listening too, not only while recording
      drawMeter($("#meter"), st.levels || null);
    } catch {}
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

function punchSlug() { return $("#punch-album").value || ""; }
function punchNum() { return parseInt($("#punch-track").value, 10) || 0; }

async function loadPunchAlbums() {
  const sel = $("#punch-album");
  const keep = sel.value;
  try {
    const { albums } = await api("/api/albums?archive=1");
    sel.innerHTML = "";
    for (const a of albums.slice().sort((x, y) => x.slug.localeCompare(y.slug))) {
      sel.appendChild(el("option", "", a.slug)).value = a.slug;
    }
    if (keep && albums.some((a) => a.slug === keep)) sel.value = keep;
  } catch { /* the panel is optional; the rip form still works */ }
  await loadPunchTracks();
}

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
    const r = await runJob(punchSlug(), `/api/punch/${punchSlug()}/apply`,
                           { number: t.number, start: a, end: b }, "replacing");
    $("#punch-done").textContent =
      `replaced ${r.number}. ${r.title} — was ${fmt(r.was_seconds)}, now ${fmt(r.now_seconds)}`
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
  for (let i = 0; i < limit; i++) {
    const st = await api("/api/rip/status");
    if (!st.running && st.stage !== "encoding") return st;
    $("#rip-state").textContent = st.stage === "encoding" ? "encoding…" : "stopping…";
    await new Promise((r) => setTimeout(r, 800));
  }
  throw new Error("the capture is taking longer than expected to finish");
}

async function ripStop() {
  $("#rip-err").textContent = "";
  $("#rip-state").textContent = "encoding…";
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
    // bump the side letter so the next one is ready to go
    const sv = $("#rip-side").value.trim();
    if (/^[a-z]$/.test(sv)) $("#rip-side").value = String.fromCharCode(sv.charCodeAt(0) + 1);
    await refreshAlbums(r.slug);
    await loadRerip();          // a brand-new album is re-rippable from now on
    await loadOrphans();
  } catch (e) { $("#rip-err").textContent = e.message; }
}

// ------------------------------------------------------------------- boot

function wire() {
  ed = new Editor($("#cv"),
    (num, edge, t) => {                       // marker dragged
      const tr = trackByNum(num); if (!tr) return;
      const d = S.sideData[S.side].duration;
      t = Math.max(0, Math.min(d, t));
      if (edge === "start") tr.start = Math.min(t, tr.end - 0.2);
      else tr.end = Math.max(t, tr.start + 0.2);
      markDirty(); ed.render(); updateReadout(); renderTracks();
    },
    (num, edge) => { ed.sel = num == null ? null : { track: num, edge }; renderTracks(); updateReadout(); },
    (t) => { if (player.playing) player.seek(t); });

  player = new Player((t) => { ed.playhead = t; ed.render(); });
  $("#vol").oninput = (e) => player.setVolume(e.target.value / 100);
  $("#helpbtn").onclick = () => $("#helpdlg").showModal();
  $("#help-close").onclick = () => $("#helpdlg").close();

  $("#save").onclick = save;
  $("#split").onclick = split;
  $("#incarch").onchange = () => refreshAlbums(S.slug);
  $("#firstpass").onclick = openFirstPass;
  $("#import").onclick = openImport;
  $("#art-upload").onclick = () => $("#art-file").click();
  $("#art-file").onchange = (e) => { if (e.target.files[0]) uploadArt(e.target.files[0]); e.target.value = ""; };
  for (const id of ["#md-artist", "#md-album", "#md-date"]) $(id).oninput = () => markDirty();
  $("#imp-search").onclick = async () => {
    $("#imp-err").textContent = "";
    try {
      await searchReleases($("#imp-artist").value, $("#imp-album").value,
                           $("#imp-results"), (r) => {
                             $("#imp-mbid").value = r.id;
                             $("#imp-results").innerHTML = "";
                             // Picking here used to set the id in the box and
                             // nowhere else: the plan kept the release chosen in
                             // First pass, so the Archive gate went on querying
                             // an id that no longer described the album.
                             S.meta.mbid = r.id;
                             offerRelabel(r);
                           }, { requireDurations: false });
    } catch (e) { $("#imp-err").textContent = e.message; }
  };
  $("#imp-relabel").onclick = relabelRun;
  $("#punch-album").onchange = loadPunchTracks;
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
  $("#imp-go").onclick = runImport;
  $("#imp-close").onclick = () => $("#impdlg").close();
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
  $("#rip-rerip-q").oninput = renderRerip;
  $("#rip-rerip").onchange = (e) => {
    const o = e.target.selectedOptions[0];
    ripPinned = e.target.value || null;
    ripPinnedSides = ripPinned ? (o.dataset.sides || "") : "";
    const lock = !!ripPinned;
    if (lock) {
      $("#rip-artist").value = o.dataset.artist;
      $("#rip-album").value = o.dataset.album;
    }
    // read-only rather than disabled: still selectable and copyable, and it
    // makes the reason visible instead of the field just going dead
    if (!lock) {                       // "new album" is a deliberate fresh start
      $("#rip-artist").value = ""; $("#rip-album").value = ""; $("#rip-side").value = "a";
    }
    $("#rip-artist").readOnly = $("#rip-album").readOnly = lock;
    $("#rip-artist").title = $("#rip-album").title =
      lock ? "pinned to the existing album — pick “new album” to edit" : "";
    ripSlug();
  };
  $("#rip-vol").oninput = () => {
    const v = (parseInt($("#rip-vol").value, 10) || 0) / 100;
    if (monitorEl) monitorEl.volume = v;
    try { localStorage.setItem("ripdoctor:rip:vol", $("#rip-vol").value); } catch {}
  };
  try {
    const v = localStorage.getItem("ripdoctor:rip:vol");
    if (v) $("#rip-vol").value = v;
  } catch {}
  $("#rip-test").onclick = async () => {
    const b = $("#rip-test");
    b.disabled = true; $("#rip-err").textContent = "";
    $("#rip-done").textContent = "recording 20 s to check the input…";
    try {
      const r = await postJSON("/api/rip/test", {
        device: $("#rip-device").value,
        rate: parseInt($("#rip-rate").value, 10) || 48000,
        format: $("#rip-format").value || "S16_LE" });
      $("#rip-done").textContent =
        `${r.ok ? "✓" : "✗"} ${r.verdict}  —  ${DEPTH[r.format] || r.format} @ ${r.rate} Hz, `
        + `full ${r.full_rms} dB RMS / ${r.full_peak} dB peak, 1–3 kHz ${r.band_rms} dB`;
      status(r.ok ? "input looks right" : "input does not look right", !r.ok);
    } catch (e) {
      $("#rip-err").textContent = e.message; $("#rip-done").textContent = "";
    }
    b.disabled = false;
  };
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
  $("#rip-side").value = "a";
  $("#rip-side").oninput = ripSlug;
  try {
    for (const id of ["#rip-artist", "#rip-album", "#rip-side"])
      localStorage.removeItem(`ripdoctor:rip:${id}`);
  } catch {}
  ripSlug();
  loadDevices();
  loadOrphans();
  loadRipSides();
  loadRerip();

  $("#logtoggle").onclick = () =>
    setLogCollapsed(!$("#logwrap").classList.contains("collapsed"));
  $("#logclear").onclick = () => { $("#log").textContent = ""; $("#logwrap").hidden = true; };
  try { setLogCollapsed(localStorage.getItem("ripdoctor:log:collapsed") === "1"); } catch {}
  $("#realign").onclick = async () => {
    if (S.dirty && !confirm("Discard unsaved changes and align from the archived cut?")) return;
    $("#realign").disabled = true;
    try {
      const r = await runJob(S.slug, `/api/align/${S.slug}`, {}, "aligning");
      for (const a of r.aligned) {
        S.bySide[a.side] = a.tracks.map((t) => ({ ...t }));
        logline(`side ${a.side}: r=${a.r}, offset ${a.offset > 0 ? "+" : ""}${a.offset}s, `
          + `drift ${a.drift_ms_per_min} ms/min — ${a.tracks.length} boundaries moved`);
      }
      for (const p of r.problems) logline(`side ${p.side}: ${p.why}`);
      // the audio genuinely changed, so nobody has listened to these yet
      S.earPassOverride = "aligned from archive (not yet heard)";
      markDirty();
      renderTracks();
      ed.setTracks(tracks());
      updateReadout();
      const n = r.aligned.reduce((a, x) => a + x.tracks.length, 0);
      status(r.aligned.length
        ? `aligned ${n} boundaries across ${r.aligned.length} side(s) — listen, then Save`
        : "nothing could be aligned — see the log", !r.aligned.length);
    } catch (e) {
      status(`align failed: ${e.message}`, true); logline(e.message, true);
    }
    $("#realign").disabled = false;
  };
  $("#revert").onclick = async () => {
    if (S.dirty && !confirm("Discard unsaved changes and reload the saved plan?")) return;
    dropDraft();
    player.stop();
    S.sideData = {};                       // re-fetch envelopes too, in case a rip finished
    await openAlbum(S.slug);
    status("reloaded from disk");
  };
  $("#album").onchange = async (e) => {
    if (S.dirty && !confirm("Discard unsaved changes to this album?")) {
      e.target.value = S.slug;            // put the picker back
      return;
    }
    try { await openAlbum(e.target.value); }
    catch (err) { status(`load failed: ${err.message}`, true); logline(String(err.stack || err), true); }
  };
  $("#splittrack").onclick = splitAtPlayhead;
  $("#addtrack").onclick = () => {
    const list = tracks();
    const at = ed.playhead;
    const num = 1 + Math.max(0, ...Object.values(S.bySide).flat().map((t) => t.number));
    list.push({ number: num, title: "Untitled", start: at, end: Math.min(at + 60, S.sideData[S.side].duration) });
    list.sort((a, b) => a.start - b.start);
    markDirty(); renderTracks(); ed.setTracks(list);
  };

  addEventListener("keydown", (e) => {
    if (["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName)) return;
    // a shortcut firing behind an open dialog once split a track while the
    // First-pass dialog had focus
    if (document.querySelector("dialog[open]")) return;
    const sel = ed.sel, t = sel && trackByNum(sel.track);
    if (e.code === "Space") {
      e.preventDefault();
      player.toggle(player.playing ? null : (t ? t[sel.edge] - 3 : ed.playhead));
    } else if (e.key === "a" || e.key === "A") {
      if (t) { e.preventDefault(); player.audition(t[sel.edge]); }
    } else if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && t) {
      e.preventDefault();
      const step = (e.shiftKey ? 0.5 : 0.05) * (e.key === "ArrowLeft" ? -1 : 1);
      if (sel.edge === "start") t.start = Math.min(t.start + step, t.end - 0.2);
      else t.end = Math.max(t.end + step, t.start + 0.2);
      markDirty(); ed.render(); updateReadout(); renderTracks();
    } else if (e.key === "s" || e.key === "S") {
      e.preventDefault(); splitAtPlayhead();
    } else if (e.key === "f" || e.key === "F" || e.key === "0") {
      e.preventDefault(); ed.fit();
    } else if (e.key === "z" || e.key === "Z") {
      if (t) { e.preventDefault(); ed.fitRange(t.start, t.end); }
    } else if (e.key === "?") {
      e.preventDefault(); $("#helpdlg").showModal();
    } else if (e.key === "Escape") { player.stop(); }
  });

  addEventListener("beforeunload", (e) => { if (S.dirty) { e.preventDefault(); e.returnValue = ""; } });
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
