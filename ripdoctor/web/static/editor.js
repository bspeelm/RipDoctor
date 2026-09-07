// Canvas editor: waveform lane, 1-3 kHz gap lane, overview strip.
//
// The 1-3 kHz lane is the reason this tool exists.  On a quiet pressing the
// full-band waveform cannot distinguish an inter-track gap from a quiet
// passage - both sit near -43 dB - while the 1-3 kHz band separates them by
// about 20 dB.  So the waveform is for orientation and the band lane is for
// deciding where the cut goes.

const DB_FLOOR = -95;          // bottom of both lanes
const HIT_PX = 7;              // how close a click must be to grab a marker
const OVERVIEW_H = 42;

const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
export const fmt = (t) => {
  const m = Math.floor(t / 60), s = t - m * 60;
  return `${m}:${s.toFixed(2).padStart(5, "0")}`;
};

export class Editor {
  constructor(canvas, onChange, onSelect, onSeek) {
    this.cv = canvas;
    this.ctx = canvas.getContext("2d");
    this.onChange = onChange;         // (track, edge, seconds) -> void
    this.onSelect = onSelect;         // (track|null, edge|null) -> void
    this.onSeek = onSeek;             // (seconds) -> void, clicking the canvas
    this.side = null;                 // {duration, windowMs, rms, peak, band, gaps}
    this.tracks = [];
    this.t0 = 0; this.t1 = 1;
    this.playhead = 0;
    this.sel = null;                  // {track, edge}
    this.drag = null;
    this._bind();
  }

  load(side, tracks) {
    this.side = side;
    this.tracks = tracks;
    this.t0 = 0; this.t1 = side.duration;
    this.playhead = tracks.length ? tracks[0].start : 0;
    this.sel = null;
    this.render();
  }

  setTracks(tracks) { this.tracks = tracks; this.render(); }

  /** Forget the loaded side and paint an empty canvas. Used when the album goes
   *  away underneath us - archiving moves it out of raw/, and leaving the old
   *  waveform on screen implies it is still there to edit. */
  clear() {
    this.side = null;
    this.tracks = [];
    this.sel = null;
    this.drag = null;
    this.playhead = 0;
    this.t0 = 0; this.t1 = 1;
    this.render();
  }

  // ------------------------------------------------------------- geometry

  layout() {
    const w = this.cv.clientWidth, h = this.cv.clientHeight;
    const usable = h - OVERVIEW_H;
    return {
      w, h,
      wave: { top: 0, h: Math.round(usable * 0.55) },
      band: { top: Math.round(usable * 0.55), h: usable - Math.round(usable * 0.55) },
      over: { top: usable, h: OVERVIEW_H },
    };
  }

  xOf(t) { return (t - this.t0) / (this.t1 - this.t0) * this.cv.clientWidth; }
  tOf(x) { return this.t0 + x / this.cv.clientWidth * (this.t1 - this.t0); }
  dbY(db, lane) {
    const f = clamp((db - DB_FLOOR) / (0 - DB_FLOOR), 0, 1);
    return lane.top + lane.h - f * lane.h;
  }

  // -------------------------------------------------------------- drawing

  render() {
    const dpr = window.devicePixelRatio || 1;
    const w = this.cv.clientWidth, h = this.cv.clientHeight;
    if (this.cv.width !== Math.round(w * dpr) || this.cv.height !== Math.round(h * dpr)) {
      this.cv.width = Math.round(w * dpr);
      this.cv.height = Math.round(h * dpr);
    }
    const c = this.ctx;
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.clearRect(0, 0, w, h);
    const L = this.layout();

    c.fillStyle = "#0f1114"; c.fillRect(0, 0, w, L.over.top);
    c.fillStyle = "#101418"; c.fillRect(0, L.over.top, w, L.over.h);
    if (!this.side) return;

    this._gaps(c, L);
    this._wave(c, L.wave);
    this._band(c, L.band);
    this._grid(c, L);
    this._ghosts(c, L);
    this._markers(c, L);
    this._playhead(c, L);
    this._overview(c, L.over);
  }

  _sampleRange(lane, t) {
    const win = this.side.windowMs / 1000;
    return clamp(Math.floor(t / win), 0, lane.length - 1);
  }

  _gaps(c, L) {
    const g = this.side.gaps?.band?.gaps || [];
    c.fillStyle = "rgba(95,211,141,0.09)";
    for (const gp of g) {
      const x0 = this.xOf(gp.lo), x1 = this.xOf(gp.hi);
      if (x1 < 0 || x0 > L.w) continue;
      c.fillRect(x0, 0, Math.max(1, x1 - x0), L.over.top);
    }
  }

  _wave(c, lane) {
    const { peak } = this.side, w = this.cv.clientWidth;
    const win = this.side.windowMs / 1000;
    const mid = lane.top + lane.h / 2;
    c.strokeStyle = "#3f7fa8"; c.lineWidth = 1; c.beginPath();
    for (let x = 0; x < w; x++) {
      const a = this._sampleRange(peak, this.tOf(x));
      const b = this._sampleRange(peak, this.tOf(x + 1));
      let m = -200;
      for (let i = a; i <= b; i++) if (peak[i] > m) m = peak[i];
      const db = m * 0.5 - 127.5;
      const f = clamp((db - DB_FLOOR) / (0 - DB_FLOOR), 0, 1);
      const half = (f * lane.h) / 2;
      c.moveTo(x + 0.5, mid - half); c.lineTo(x + 0.5, mid + half);
    }
    c.stroke();
  }

  _band(c, lane) {
    const { band } = this.side, w = this.cv.clientWidth;
    // threshold line: what the gap detector called silence
    const th = this.side.gaps?.band?.threshold;
    if (th != null) {
      c.strokeStyle = "rgba(230,196,104,0.45)"; c.setLineDash([4, 4]); c.lineWidth = 1;
      c.beginPath();
      const y = this.dbY(th, lane);
      c.moveTo(0, y); c.lineTo(w, y); c.stroke(); c.setLineDash([]);
      c.fillStyle = "rgba(230,196,104,0.7)"; c.font = "10px ui-monospace,monospace";
      c.fillText(`gap threshold ${th.toFixed(1)} dB`, 6, y - 3);
    }
    c.strokeStyle = "#e0a24a"; c.lineWidth = 1.2; c.beginPath();
    let first = true;
    for (let x = 0; x < w; x++) {
      const a = this._sampleRange(band, this.tOf(x));
      const b = this._sampleRange(band, this.tOf(x + 1));
      let m = -200;
      for (let i = a; i <= b; i++) if (band[i] > m) m = band[i];
      const y = this.dbY(m * 0.5 - 127.5, lane);
      if (first) { c.moveTo(x + 0.5, y); first = false; } else c.lineTo(x + 0.5, y);
    }
    c.stroke();
    c.fillStyle = "#6b7480"; c.font = "10px ui-sans-serif,system-ui";
    c.fillText("1–3 kHz", 6, lane.top + 12);
  }

  _grid(c, L) {
    const span = this.t1 - this.t0;
    const step = span > 900 ? 120 : span > 300 ? 60 : span > 90 ? 20 : span > 30 ? 5 : span > 8 ? 1 : 0.5;
    c.strokeStyle = "#20242b"; c.fillStyle = "#5a626c";
    c.font = "10px ui-monospace,monospace"; c.lineWidth = 1;
    for (let t = Math.ceil(this.t0 / step) * step; t < this.t1; t += step) {
      const x = Math.round(this.xOf(t)) + 0.5;
      c.beginPath(); c.moveTo(x, 0); c.lineTo(x, L.over.top); c.stroke();
      c.fillText(fmt(t), x + 3, L.over.top - 4);
    }
  }

  // Faint marks where the catalogue duration says a boundary should land.
  _ghosts(c, L) {
    c.strokeStyle = "rgba(139,145,153,0.5)"; c.setLineDash([2, 3]); c.lineWidth = 1;
    c.fillStyle = "rgba(139,145,153,0.8)"; c.font = "10px ui-monospace,monospace";
    for (const t of this.tracks) {
      if (!t.cat) continue;
      const g = t.start + t.cat;
      const x = this.xOf(g);
      if (x < -40 || x > L.w + 40) continue;
      c.beginPath(); c.moveTo(x, L.wave.top); c.lineTo(x, L.over.top); c.stroke();
      c.fillText(`cat ${t.number}`, x + 3, L.wave.top + 24);
    }
    c.setLineDash([]);
  }

  _markers(c, L) {
    for (const t of this.tracks) {
      for (const edge of ["start", "end"]) {
        const x = this.xOf(t[edge]);
        if (x < -30 || x > L.w + 30) continue;
        const on = this.sel && this.sel.track === t.number && this.sel.edge === edge;
        c.strokeStyle = edge === "start" ? "#5fd38d" : "#e6705a";
        c.lineWidth = on ? 2.5 : 1.4;
        c.beginPath(); c.moveTo(x, 0); c.lineTo(x, L.over.top); c.stroke();
        c.fillStyle = c.strokeStyle;
        const label = `${t.number}${edge === "start" ? "▶" : "■"}`;
        c.font = on ? "bold 11px ui-monospace,monospace" : "11px ui-monospace,monospace";
        const tw = c.measureText(label).width;
        const lx = edge === "start" ? x + 3 : x - tw - 3;
        c.fillRect(lx - 2, L.wave.top + 2, tw + 4, 14);
        c.fillStyle = "#0b0d10";
        c.fillText(label, lx, L.wave.top + 13);
      }
    }
  }

  _playhead(c, L) {
    const x = this.xOf(this.playhead);
    if (x < 0 || x > L.w) return;
    c.strokeStyle = "#e6e8ea"; c.lineWidth = 1;
    c.beginPath(); c.moveTo(x + 0.5, 0); c.lineTo(x + 0.5, L.over.top); c.stroke();
  }

  _overview(c, lane) {
    const { peak } = this.side, w = this.cv.clientWidth;
    const mid = lane.top + lane.h / 2;
    c.strokeStyle = "#2f3a44"; c.lineWidth = 1; c.beginPath();
    for (let x = 0; x < w; x++) {
      const a = Math.floor(x / w * peak.length), b = Math.floor((x + 1) / w * peak.length);
      let m = -200;
      for (let i = a; i <= b && i < peak.length; i++) if (peak[i] > m) m = peak[i];
      const f = clamp((m * 0.5 - 127.5 - DB_FLOOR) / (0 - DB_FLOOR), 0, 1);
      c.moveTo(x + 0.5, mid - f * lane.h / 2); c.lineTo(x + 0.5, mid + f * lane.h / 2);
    }
    c.stroke();
    const d = this.side.duration;
    const x0 = this.t0 / d * w, x1 = this.t1 / d * w;
    c.strokeStyle = "#5aa9e6"; c.lineWidth = 1;
    c.strokeRect(x0 + 0.5, lane.top + 0.5, Math.max(2, x1 - x0), lane.h - 1);
    c.fillStyle = "rgba(90,169,230,0.12)";
    c.fillRect(x0, lane.top, Math.max(2, x1 - x0), lane.h);
  }

  // ---------------------------------------------------------- interaction

  hit(x, y) {
    const L = this.layout();
    if (y >= L.over.top) return null;
    let best = null;
    for (const t of this.tracks) {
      for (const edge of ["start", "end"]) {
        const d = Math.abs(this.xOf(t[edge]) - x);
        if (d <= HIT_PX && (!best || d < best.d)) best = { track: t.number, edge, d };
      }
    }
    return best;
  }

  zoomTo(t0, t1) {
    const d = this.side.duration;
    let span = clamp(t1 - t0, 0.25, d);
    t0 = clamp(t0, 0, d - span);
    this.t0 = t0; this.t1 = t0 + span;
    this.render();
  }

  /** Whole side. */
  fit() { if (this.side) this.zoomTo(0, this.side.duration); }

  /** Frame one track with a little air either side. */
  fitRange(a, b) {
    const pad = Math.max(1.0, (b - a) * 0.08);
    this.zoomTo(a - pad, b + pad);
  }

  centerOn(t, span) {
    span = span ?? (this.t1 - this.t0);
    this.zoomTo(t - span / 2, t + span / 2);
  }

  _bind() {
    const cv = this.cv;
    cv.addEventListener("wheel", (e) => {
      if (!this.side) return;
      e.preventDefault();
      const L = this.layout();
      if (e.clientY - cv.getBoundingClientRect().top >= L.over.top) return;
      const x = e.clientX - cv.getBoundingClientRect().left;
      const anchor = this.tOf(x);
      const k = Math.exp(e.deltaY * 0.0015);
      const span = clamp((this.t1 - this.t0) * k, 0.25, this.side.duration);
      const frac = x / cv.clientWidth;
      this.zoomTo(anchor - span * frac, anchor - span * frac + span);
    }, { passive: false });

    cv.addEventListener("pointerdown", (e) => {
      if (!this.side) return;
      const r = cv.getBoundingClientRect();
      const x = e.clientX - r.left, y = e.clientY - r.top;
      const L = this.layout();
      cv.setPointerCapture(e.pointerId);
      if (y >= L.over.top) {
        const d = this.side.duration, span = this.t1 - this.t0;
        this.centerOn(x / cv.clientWidth * d, span);
        this.drag = { mode: "overview" };
        return;
      }
      const h = this.hit(x, y);
      if (h) {
        this.sel = { track: h.track, edge: h.edge };
        this.onSelect?.(h.track, h.edge);
        this.drag = { mode: "marker", ...h, moved: false };
      } else {
        this.drag = { mode: "pan", x, t0: this.t0, moved: false };
      }
      this.render();
    });

    cv.addEventListener("pointermove", (e) => {
      const r = cv.getBoundingClientRect();
      const x = e.clientX - r.left, y = e.clientY - r.top;
      if (!this.drag) {
        cv.style.cursor = this.hit(x, y) ? "ew-resize" : "crosshair";
        return;
      }
      if (this.drag.mode === "overview") {
        this.centerOn(x / this.cv.clientWidth * this.side.duration);
      } else if (this.drag.mode === "marker") {
        this.drag.moved = true;
        this.onChange(this.drag.track, this.drag.edge, this.tOf(x));
      } else {
        this.drag.moved = Math.abs(x - this.drag.x) > 3;
        const span = this.t1 - this.t0;
        const dt = (this.drag.x - x) / this.cv.clientWidth * span;
        this.zoomTo(this.drag.t0 + dt, this.drag.t0 + dt + span);
      }
    });

    const up = (e) => {
      if (this.drag && this.drag.mode === "pan" && !this.drag.moved) {
        const r = cv.getBoundingClientRect();
        this.playhead = this.tOf(e.clientX - r.left);
        this.sel = null;
        this.onSelect?.(null, null);
        // Without this the click looks like it does nothing: during playback the
        // player's own callback overwrites `playhead` on the next frame, so the
        // marker you just set is erased within ~16 ms.
        this.onSeek?.(this.playhead);
        this.render();
      }
      this.drag = null;
    };
    cv.addEventListener("pointerup", up);
    cv.addEventListener("pointercancel", () => { this.drag = null; });
    window.addEventListener("resize", () => this.render());
  }
}
