# RipDoctor

Capture a vinyl side, find the track boundaries, cut them where you want them.

**Status: complete, and not yet run against a turntable.** Every step works and
is tested end to end - capture, fitting, cutting, the ear check, import and
archive - but only against synthesised and recorded audio. Nothing here has met
a sound card. See [docs/decisions.md](docs/decisions.md) for what has been
decided and why.

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
[docs/method.md](docs/method.md) for what each number means and where it came
from.

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

```
pip install ripdoctor
```

It pulls in [beets](https://beets.io), which does the tagging, the path
formatting and the album-mode ReplayGain a finished record is filed by. Nothing
else comes with it.

The system tools it needs are `ffmpeg`, `ffprobe`, `flac` and `metaflac`, plus
`arecord` if you want to record. `ripdoctor doctor` reports which of those are
missing and what to install for each.

## Commands

```
ripdoctor doctor        what this machine has, what it lacks, and what to do
ripdoctor config        every resolved setting, including where the audio lives
ripdoctor devices       capture devices this machine has, and what each accepts
ripdoctor probe         record twenty seconds and say what arrived
ripdoctor measure       compare this signal chain against the shipped thresholds
ripdoctor record        record one side, metered, stopping itself at the run-out
ripdoctor serve         run the web interface
ripdoctor salvage       finish a capture an interrupted session left behind
ripdoctor lookup        find a release in the catalogue, usable entries first
ripdoctor fit           place track boundaries from a spec and measured levels
ripdoctor split         cut a plan into tracks
ripdoctor check         build tick clips, one per boundary, for listening to
ripdoctor import        tag the cut tracks and place them in the library
ripdoctor archive       confirm a record arrived before the raw sides are cleared
```

`ripdoctor probe` before a record, not after. It turns "drop the needle, wait
twenty minutes, find out it was the wrong input" into a twenty-second question,
and tells the four states apart: music, signal with nothing musical in it, an
empty room, or something merely quiet.

`ripdoctor doctor` is the one to run first. It never needs a working machine -
that is the point of it - and every problem it reports comes with what to do
about it.

The raw sides are never cleared until the record is provably somewhere else -
`ripdoctor archive` counts what actually arrived and says so, asking beets where
it filed things rather than guessing at a path.

Nothing is cut until a plan validates. A track that would end before it starts,
overlap its neighbour, collide on a track number or run past the end of the side
is refused with the reason, rather than written as an empty or duplicated file.

## The web interface

`ripdoctor serve` is the same pipeline with a waveform in front of it, on the
machine that holds the library. It binds to loopback unless told otherwise, and
asks for a login either way: it can write to the pool and run ffmpeg, so a
password is the compensating control for being reachable at all.

What the page adds over the commands is the part that needs eyes and ears. The
1-3 kHz gap curve is drawn under the waveform with the detection threshold on
it, so a boundary is placed against the measurement rather than against a
number in a table. Ghost markers show where the catalogue says each cut should
fall, and the distance between a ghost and a real one is the disagreement the
fitter reported. Every boundary can be auditioned as a tick clip without
leaving the page.

Recording runs on the server rather than in the browser, so the meter and the
auto-stop survive a closed laptop. You can listen to the input while cueing the
needle, which is the only way to tell that the arm is tracking before twenty
minutes have gone by.

## Phases

| Phase | State |
|---|---|
| 0 — scaffold, decisions, budgets, architecture tests | done |
| 1 — the detection and fitting core, and its tests | done |
| 2 — audio seam, config, doctor; offline CLI | done |
| 3 — capture, the live meter, auto-stop | done; not yet run against hardware |
| 7 — validate on the server, then cut over | not started |
| 4 — web interface | done, except listening while cueing (untested against hardware) |
| 5 — tagging and import | done |
| 6 — documentation revision, then 1.0 | revision done; 1.0 not tagged |

## Development

```
make check     # lint, types, tests, budgets
make test
```

The core layer computes over decibel envelopes and touches nothing — no
subprocess, no filesystem, no clock — so the whole algorithm is testable with
no ffmpeg, no sound card and no audio files. That boundary is enforced by
`tests/test_architecture.py`, not by convention.

On Python 3.14 specifically, this suite is occasionally unreliable through no
fault of its own - see ADR-019. A failure whose error is *impossible* (an
unknown opcode, a NameError for something imported at the top of the file) is
the interpreter, not the code. Run it again. CI uses 3.11 to 3.13 and is not
affected.

Size budgets are failing checks. The comment ratio is hard and is never raised;
the others are the author's call. Never write less code to fit a number.

The documentation was written ahead of the code and revised once the code was
complete. What holds it true from here is a set of tests rather than a ratio:
every command the README names is a real subparser and every subparser is
named; every threshold appears in `docs/method.md` with the number the code
actually uses; every element the front end reaches for exists in the markup;
and every endpoint it calls is a route. See ADR-023 and ADR-037.

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
