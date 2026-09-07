// Rip panel: device pick, metadata, transport, and the live meter.
//
// The meter is the point. Four states have to be told apart while the needle is
// going down, and full-band level alone cannot do it — arm-up handling rumble
// reads -32 dB full band, only 9 dB below music. In the 1-3 kHz band the same
// two states are 27 dB apart. So the band bar is drawn bigger and the zones are
// labelled with what they mean physically.

// Measured on the Waxwing optical chain 2026-08-26: stylus UP reads -75 dB full
// band, because the ADC now sits after the phono gain and carries its noise -
// the old PCM2900C line-level floor was -89. The needle-up boundary was -80, so
// the meter called "stylus in the air" a groove. Only this edge is moved: the
// groove and quiet-music edges below still want a real measurement on this
// chain and are left as inherited values rather than guesses.
const ZONES = [
  { lo: -120, hi: -68, label: "dead air / needle up", color: "#2a2e35" },
  { lo: -68, hi: -60, label: "groove", color: "#2f4a3a" },
  { lo: -60, hi: -45, label: "quiet music", color: "#3f5a2f" },
  { lo: -45, hi: 0, label: "music", color: "#4a5a2f" },
];
const BAND_ZONES = [
  { lo: -120, hi: -92, label: "dead air", color: "#2a2e35" },
  { lo: -92, hi: -72, label: "silent groove", color: "#2f4a3a" },
  { lo: -72, hi: -58, label: "arm-up rumble", color: "#5a3f2f" },
  { lo: -58, hi: -44, label: "quiet music", color: "#3f5a2f" },
  { lo: -44, hi: 0, label: "music", color: "#4a5a2f" },
];
const FLOOR = -100;

export function drawMeter(cv, levels) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if (cv.width !== Math.round(w * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
  const c = cv.getContext("2d");
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, w, h);

  const rows = [
    { name: "full band", db: levels?.full, zones: ZONES, y: 18, hh: 26 },
    { name: "1–3 kHz", db: levels?.band, zones: BAND_ZONES, y: 84, hh: 40 },
  ];
  const x0 = 92, x1 = w - 60;
  const X = (db) => x0 + (Math.max(FLOOR, Math.min(0, db)) - FLOOR) / (0 - FLOOR) * (x1 - x0);

  c.font = "11px ui-sans-serif,system-ui";
  for (const r of rows) {
    for (const z of r.zones) {
      const a = X(z.lo), b = X(z.hi);
      c.fillStyle = z.color;
      c.fillRect(a, r.y, b - a, r.hh);
      c.fillStyle = "rgba(230,232,234,0.55)";
      const tw = c.measureText(z.label).width;
      if (b - a > tw + 8) c.fillText(z.label, a + 4, r.y + r.hh - 6);
    }
    c.fillStyle = "#8b9199";
    c.textAlign = "right";
    c.fillText(r.name, x0 - 8, r.y + r.hh / 2 + 4);
    c.textAlign = "left";
    if (typeof r.db === "number") {
      const x = X(r.db);
      c.fillStyle = "#e6e8ea";
      c.fillRect(x - 1.5, r.y - 5, 3, r.hh + 10);
      c.font = "600 12px ui-monospace,monospace";
      c.fillText(`${r.db.toFixed(1)}`, Math.min(x + 6, x1 + 4), r.y - 8);
      c.font = "11px ui-sans-serif,system-ui";
    }
  }
  c.strokeStyle = "#2a2e35";
  c.fillStyle = "#5a626c";
  c.font = "10px ui-monospace,monospace";
  for (let db = -100; db <= 0; db += 20) {
    const x = X(db);
    c.beginPath(); c.moveTo(x + 0.5, 8); c.lineTo(x + 0.5, h - 18); c.stroke();
    c.fillText(`${db}`, x + 2, h - 6);
  }
}

export function slugify(artist, album) {
  return `${artist} - ${album}`.replace(/[ /]/g, "-").replace(/[^A-Za-z0-9\-_]/g, "");
}
