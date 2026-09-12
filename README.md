# RipDoctor

Record a vinyl side, split it into tracks, tag them and file them in your music
library.

![The cut panel. A side's waveform in blue, the same side measured in 1-3 kHz in
orange below it, the gap threshold as a dashed line, and the track boundaries as
vertical markers.](docs/images/cut_panel.png)

## What it does

- **Records** a side from a turntable attached to the machine, with a live meter
  and an auto-stop at the run-out.
- **Or takes a file you already have** - a download, or audio captured
  elsewhere - uploaded from the browser and normalised on the way in. Anything
  ffmpeg can read. Everything after that is identical to a rip.
- **Finds the track boundaries** by looking for gaps in the 1-3 kHz band rather
  than at the full-band level, and by checking them against the track durations
  in MusicBrainz.
- **Lets you correct them by ear.** Each boundary can be auditioned as a short
  clip with a tick mixed in at the cut instant, so you hear whether the cut lands
  in the gap or on the music.
- **Cuts, tags and files** the tracks through [beets](https://beets.io), with
  cover art and album-mode ReplayGain.
- **Archives the raw sides** once it has confirmed the tracks reached the
  library.

You drive it from a web page on the machine that holds the library, or from the
command line.

![The rip panel while a side is recording. Elapsed time, the music level, and a
bar showing which zone the 1-3 kHz level is in.](docs/images/rip_panel.png)

## Why 1-3 kHz

Splitting a side into tracks is usually done by full-band silence detection: pick
a threshold in decibels, call anything quieter a gap, cut there. On most records
this works.

On a sparse pressing it cannot. A real inter-track gap and a quiet passage inside
a song both sit near -43 dB full-band, and no threshold separates them. Tuning it
trades one failure for the other. On one album this produced two tracks 29
seconds too long and 22 seconds too short.

Measured in 1-3 kHz, the same two things separate by about 20 dB. Vinyl's noise -
plinth rumble, arm handling, warp - is bass-heavy, and a quiet pressing has
nothing at all above 8 kHz. The 1-3 kHz band is where music is and noise is not.

| | full band | 1-3 kHz |
|---|---|---|
| music | -26 | -40 |
| arm up, being handled | -36 | -67 |
| silent groove | -64 | -85 |
| dead air, needle up | -89 | -93 |

Full band puts music and arm-rumble 10 dB apart; the band lane puts them 27 dB
apart. Every boundary decision keys off that.

These are measurements from one signal chain, not universal constants.
[docs/method.md](docs/method.md) explains each number and where it came from, and
`ripdoctor measure` compares your chain against them.

## Getting started

```
pip install ripdoctor
ripdoctor doctor      # what is missing, and what to install
```

`doctor` names every tool it needs - `ffmpeg`, `ffprobe`, `flac`, `metaflac`,
and `arecord` to record - with the install command for each. It also reports
whether beets is configured, which matters more than it looks: without a beets
configuration an import still appears to work while fetching no cover art and
computing no ReplayGain.

Then set the capture device and the two directories, and start the interface:

```
ripdoctor devices     # find your capture device
ripdoctor probe       # record 20 seconds and say what arrived
ripdoctor serve       # http://127.0.0.1:8080
```

On first run `serve` generates a login and prints the password once.

**[The wiki](https://github.com/bspeelm/RipDoctor/wiki) is the manual** -
[installing](https://github.com/bspeelm/RipDoctor/wiki/Installation),
[configuring](https://github.com/bspeelm/RipDoctor/wiki/Configuration),
[setting beets up](https://github.com/bspeelm/RipDoctor/wiki/Setting-up-beets),
and each step of a record from the needle drop to the archive.

| | |
|---|---|
| [Ripping a side](https://github.com/bspeelm/RipDoctor/wiki/Ripping-a-side) | recording, the meter, the auto-stop, salvaging an interrupted capture |
| [Uploading a file](https://github.com/bspeelm/RipDoctor/wiki/Uploading-a-file) | bringing in audio that was not recorded here |
| [First pass](https://github.com/bspeelm/RipDoctor/wiki/First-pass) | finding the release and fitting the boundaries |
| [Checking the boundaries](https://github.com/bspeelm/RipDoctor/wiki/Checking-the-boundaries) | the ear check, and moving a cut |
| [Cutting](https://github.com/bspeelm/RipDoctor/wiki/Cutting-tracks) and [importing](https://github.com/bspeelm/RipDoctor/wiki/Importing) | into `review/`, then into the library |
| [Archiving](https://github.com/bspeelm/RipDoctor/wiki/Archiving) | putting the raw sides away once the tracks are confirmed |
| [Re-ripping](https://github.com/bspeelm/RipDoctor/wiki/Re-ripping-a-side) and [punching](https://github.com/bspeelm/RipDoctor/wiki/Punching-a-track) | redoing one side, or one track |
| [Command line](https://github.com/bspeelm/RipDoctor/wiki/Command-line) | all 15 commands and their options |
| [Troubleshooting](https://github.com/bspeelm/RipDoctor/wiki/Troubleshooting) | errors, and what each one means |

## Two things it will not do

Nothing is cut until the plan validates. A track that would end before it
starts, overlap its neighbour, collide on a track number or run past the end of
the side is refused with the reason.

The raw sides are not cleared until the record is provably in the library.
`ripdoctor archive` asks the importer where it filed things and counts what
arrived.

## What it is not

- **Not a library manager.** It writes tagged files into a directory and stops.
  Whatever serves or syncs your music watches that directory; RipDoctor never
  calls it.
- **Not a restoration tool.** No click removal, no declicking, no noise
  reduction.
- **Not fully automatic.** The last judgement about where a cut goes is yours.

## Development

```
make check     # lint, types, tests, budgets
make test
```

The core layer computes over decibel envelopes and touches nothing - no
subprocess, no filesystem, no clock - so the whole algorithm is testable without
ffmpeg, a sound card or any audio files. `tests/test_architecture.py` enforces
that boundary.

[docs/decisions.md](docs/decisions.md) records what was decided and why. The
[wiki](https://github.com/bspeelm/RipDoctor/wiki) covers using it rather than
building it.

On Python 3.14 the suite is occasionally unreliable through no fault of its own
(see ADR-019). A failure whose error is impossible - an unknown opcode, a
NameError for something imported at the top of the file - is the interpreter.
Run it again. CI uses 3.11 to 3.13.

## Prior work

[VinylFlow](https://github.com/olimic1000/vinylflow) (MIT) covers adjacent
ground and is worth a look if RipDoctor is not what you want. It takes a
recording you already made and splits, tags and exports it, with an interactive
waveform editor.

The differences are scope, not quality. VinylFlow does not record, so capture
happens elsewhere and the file is uploaded to it. It previews the resulting
track; RipDoctor is built to judge the boundary itself. It splits on full-band
silence detection with a tunable threshold, which is the approach this project
exists because of.

## Licence

MIT. See [LICENSE](LICENSE).
