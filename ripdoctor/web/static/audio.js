// Playback against the Opus proxy, plus the 1400 Hz tick.
//
// The tick is carried over from sampler3.py because it answers the only
// question that matters at a boundary: does it land in the gap, or on top of
// music?  Here it is scheduled live at the marker instead of being baked into
// a rendered clip.

const TICK_HZ = 1400, TICK_MS = 60;

export class Player {
  constructor(onTime) {
    this.el = new Audio();
    this.el.preload = "auto";
    this.gain = 1.0;
    this.onTime = onTime;
    this.ctx = null;
    this.tickAt = null;
    this.stopAt = null;
    this._raf = null;
    this.el.addEventListener("play", () => this._loop());
    this.el.addEventListener("pause", () => cancelAnimationFrame(this._raf));
    this.el.addEventListener("ended", () => cancelAnimationFrame(this._raf));
  }

  load(url) {
    this.el.src = url;
    this.tickAt = this.stopAt = null;
  }

  get time() { return this.el.currentTime; }
  get playing() { return !this.el.paused && !this.el.ended; }

  _audioCtx() {
    if (!this.ctx) this.ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (this.ctx.state === "suspended") this.ctx.resume();
    return this.ctx;
  }

  _scheduleTick(inSeconds) {
    const ctx = this._audioCtx();
    const t = ctx.currentTime + Math.max(0, inSeconds);
    const osc = ctx.createOscillator(), g = ctx.createGain();
    osc.frequency.value = TICK_HZ;
    // short ramps so the tick is a click, not a pop with its own transient
    g.gain.setValueAtTime(0, t);
    // scale the tick with the player: on a fixed gain it is startlingly loud
    // over quiet material
    const peak = 0.22 * this.gain;
    g.gain.linearRampToValueAtTime(peak, t + 0.004);
    g.gain.setValueAtTime(peak, t + TICK_MS / 1000 - 0.004);
    g.gain.linearRampToValueAtTime(0, t + TICK_MS / 1000);
    osc.connect(g).connect(ctx.destination);
    osc.start(t);
    osc.stop(t + TICK_MS / 1000 + 0.01);
  }

  _loop() {
    const step = () => {
      const t = this.el.currentTime;
      this.onTime?.(t);
      if (this.tickAt != null && t >= this.tickAt - 0.25) {
        this._scheduleTick(this.tickAt - t);
        this.tickAt = null;
      }
      if (this.stopAt != null && t >= this.stopAt) {
        this.el.pause();
        this.stopAt = null;
      }
      this._raf = requestAnimationFrame(step);
    };
    cancelAnimationFrame(this._raf);
    this._raf = requestAnimationFrame(step);
  }

  seek(t) { try { this.el.currentTime = Math.max(0, t); } catch {} }

  setVolume(v) { this.gain = v; this.el.volume = v; }

  toggle(from) {
    if (this.playing) { this.el.pause(); return; }
    if (from != null) this.seek(from);
    this.tickAt = this.stopAt = null;
    this._audioCtx();
    this.el.play().catch(() => {});
  }

  /** Play around a marker with the tick landing exactly on it. */
  audition(t, pre = 4, post = 4) {
    this._audioCtx();
    this.seek(Math.max(0, t - pre));
    this.tickAt = t;
    this.stopAt = t + post;
    this.el.play().catch(() => {});
  }

  stop() { this.el.pause(); this.tickAt = this.stopAt = null; }
}
