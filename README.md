# RipDoctor

Capture a vinyl side, find the track boundaries, cut them where you want them.

**Status: early. Not yet usable.** The package installs and the architecture
tests pass; the algorithm is being ported layer by layer. See
[docs/decisions.md](docs/decisions.md) for what has been decided and why, and
the phase table below for what exists.

## The problem it solves

Splitting a recorded vinyl side into tracks is usually done by detecting
silence: pick a threshold in decibels, call anything quieter a gap, cut there.
On most records this works.

On a sparse pressing it cannot work, because there is no such threshold. A real
inter-track gap and a quiet passage inside a song both sit near −43 dB
full-band, and no number separates them. Tuning the threshold trades one failure
for the other. On one album this produced two tracks that were 29 seconds too
long and 22 seconds too short.

Restrict the measurement to 1–3 kHz and the same two things separate by about
20 dB. Vinyl's noise — plinth rumble, arm handling, warp — is bass-heavy, and on
a quiet pressing there is nothing at all above 8 kHz. The 1–3 kHz band is where
music is and noise is not.

| | full band | 1–3 kHz |
|---|---|---|
| music | −26 | −40 |
| arm up, being handled | −36 | −67 |
| silent groove | −64 | −85 |
| dead air, needle up | −89 | −93 |

Full band puts music and arm-rumble 10 dB apart. The band lane puts them 27 dB
apart. Every boundary decision in this tool keys off that.

These are measurements from one signal chain, not universal constants. See
[docs/thresholds.md](docs/thresholds.md) for what each number means and how to
measure your own.

## What it does

**Records**, if the turntable is attached to the machine running it, with a live
meter showing the band level so you can see what the stylus is doing. It also
accepts a WAV or FLAC you recorded somewhere else; neither path is
second-class.

**Fits** boundaries by requiring two independent constraints to agree: the cut
must fall inside a measured gap, and it must fall at the catalogue duration from
the previous cut. Where they disagree, the cut is clamped and the disagreement
is reported rather than absorbed.

**Cuts** each track with its own start and end, so the groove between tracks is
discarded instead of being attached to whichever neighbour is nearer.

**Verifies by ear**, which is the part that matters. Automatic detection finds
where music *dominates*; only a person finds where music *is*. So the tool does
not ask you to scrub through a track and judge. It mixes a 1400 Hz tick at the
exact cut instant and asks one question: does the tick land in the gap, or on
the music?

## What it is not

- Not a library manager. It writes tagged files into a directory and stops.
  Whatever you use to serve or sync your music watches that directory; RipDoctor
  never calls it.
- Not a restoration tool. No click removal, no declicking, no noise reduction.
- Not a cataloguing app.
- Not fully automatic, and not trying to be. The last judgement is yours.

## Install

Nothing to install yet. When there is:

```
pip install ripdoctor
```

The base install has **no Python dependencies**. It needs `ffmpeg`, `ffprobe`,
`flac` and `metaflac` on the system, plus `arecord` if you want to record.
`ripdoctor doctor` reports which of those are missing.

## Phases

| Phase | State |
|---|---|
| 0 — scaffold, decisions, budgets, architecture tests | in progress |
| 1 — the detection and fitting core, and its tests | not started |
| 2 — audio seam, config, doctor; offline CLI | not started |
| 3 — capture | not started |
| 4 — web interface | not started |
| 5 — tagging and import; 1.0 | not started |

## Development

```
make check     # lint, types, tests, budgets
make test
```

The core layer computes over decibel envelopes and touches nothing — no
subprocess, no filesystem, no clock — so the whole algorithm is testable with
no ffmpeg, no sound card and no audio files. That boundary is enforced by
`tests/test_architecture.py`, not by convention.

Size budgets are failing checks. Do not raise one to make a change fit; retire
something, or write a decision record explaining why the number moved.

## Prior work

[VinylFlow](https://github.com/olimic1000/vinylflow) (MIT) covers adjacent
ground and is worth your attention if RipDoctor is not what you want. It takes
a recording you already made and splits, tags and exports it, with an
interactive waveform editor for adjusting boundaries.

The differences are in scope rather than quality. VinylFlow does not record, so
capture happens elsewhere and the file is uploaded to it; RipDoctor records on
the machine that holds the library, so the file is written once and never moves.
VinylFlow previews the resulting track; RipDoctor is built to judge the boundary
itself, which is a different question. VinylFlow splits on full-band silence
detection with a tunable threshold, which is the approach this project exists
because of.

## Licence

MIT. See [LICENSE](LICENSE).
