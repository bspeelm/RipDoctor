# RipDoctor

Record a vinyl side, split it into tracks, tag them and file them in your music
library.

![The cut panel. A side's waveform in blue, the same side measured in 1-3 kHz in
orange below it, the gap threshold as a dashed line, and the track boundaries as
vertical markers.](docs/images/cut_panel.png)

## What it does

- **Records** a side from a turntable attached to the machine, with a live meter
  and an auto-stop at the run-out. Or takes a WAV or FLAC you recorded elsewhere.
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

## Install

```
pip install ripdoctor
```

You also need `ffmpeg`, `ffprobe`, `flac` and `metaflac` on the system, plus
`arecord` to record. Run `ripdoctor doctor` and it will tell you which are
missing and what to install for each.

### Configuring beets

RipDoctor does not tag or file anything itself - it hands finished tracks to
beets, which matches them against MusicBrainz, writes the tags, fetches cover
art, computes ReplayGain and decides the path each file goes to.

**beets needs its own configuration, and a fresh install has none.** Without one
it runs with no plugins: no cover art, no ReplayGain, and files go to `~/Music`
rather than your library. The import still appears to work, which is what makes
this worth saying twice.

`beet config -p` prints where beets expects its file. Create it with at least:

```yaml
directory: /srv/music          # where finished albums go
plugins: fetchart embedart replaygain

import:
  move: yes                    # move out of review/ rather than copying

fetchart:
  auto: yes
  minwidth: 500
embedart:
  auto: yes
replaygain:
  backend: ffmpeg
  auto: yes
  albums: yes                  # album mode - vinyl is mastered as a side
```

`directory` must match the `library` setting in RipDoctor's own config; they are
two names for the same place and nothing else keeps them in step. `ripdoctor
doctor` reports when they disagree, which plugins beets will actually load, and
prints a starter config if there is none.

If you already have a beets setup, point RipDoctor at it with `BEETSDIR` rather
than writing a second one.

## Getting started

```
ripdoctor doctor      # what is missing, and what to install
ripdoctor devices     # find your capture device
ripdoctor config      # check where audio and the library live
```

Set the pool and library directories, the capture device, rate and format in
your config file - `ripdoctor config` prints the path.

Then, before you commit twenty minutes to a side:

```
ripdoctor probe       # record 20 seconds and say what arrived
```

This tells music from silence from an empty input, so a wrong input costs twenty
seconds rather than a whole side.

Then start the interface:

```
ripdoctor serve
```

On first run it generates a login and prints it:

```
  first run - a login was generated
    user:     ripdoctor
    password: <shown once>
  Store it now; it is not recoverable.
```

The password is hashed on the way to disk, so that is the only time you see it.
`--user` sets a different name on first run.

By default it listens on `127.0.0.1:8080` - that machine only. To reach it from
a laptop, set the address and port in RipDoctor's config file:

```toml
bind = "0.0.0.0"
port = 8084
```

or pass `--bind` and `--port` to try it once. Then open
`http://<the machine>:8084`.

It asks for a login whichever way it is bound: it can write to your pool and run
ffmpeg, so the password is the thing that makes it safe to be reachable at all.
There is no TLS - put it behind a reverse proxy if it leaves your own network.

From there, one record goes like this:

1. **Rip** side A, flip the record, rip side B.
2. **First pass** - search MusicBrainz, pick the release, and it lays the
   tracklist across the sides and fits the boundaries.
3. **Check by ear.** Look at the delta column for boundaries that disagree with
   the catalogue, and audition those.
4. **Cut tracks** into the review directory.
5. **Import** - beets tags them, fetches cover art and files them.
6. **Archive** - the raw sides are moved aside once the tracks are confirmed in
   the library.

## The web interface

![The rip panel while a side is recording. Elapsed time, the music level, and a
bar showing which zone the 1-3 kHz level is in.](docs/images/rip_panel.png)

The bar under the controls is what you watch while cueing the needle. Arm-up
handling rumble is loud full-band but dead in 1-3 kHz; a silent groove is quiet
in both; music is loud in both. Drop the needle when the band bar reads `groove`,
not below it.

Recording runs on the server rather than in the browser, so the meter and the
auto-stop survive a closed laptop. You can listen to the input while cueing.

In the cut panel, the 1-3 kHz gap curve is drawn under the waveform with the
detection threshold on it. Ghost markers show where the catalogue says each cut
should fall; the distance between a ghost and a real marker is how far the two
disagree.

To keep it running across reboots, install it as a service.
[`packaging/ripdoctor.service`](packaging/ripdoctor.service) is a systemd unit
to copy and edit - the paths in it are examples, and the comments say what each
one is for. Set `BEETSDIR` in it if beets keeps its configuration somewhere
other than the default; a variable exported in your shell is not inherited by a
service, which is an easy way to end up with no plugins.

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

## What it is not

- **Not a library manager.** It writes tagged files into a directory and stops.
  Whatever serves or syncs your music watches that directory; RipDoctor never
  calls it.
- **Not a restoration tool.** No click removal, no declicking, no noise
  reduction.
- **Not fully automatic.** The last judgement about where a cut goes is yours.

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
ripdoctor name          say what a record is, for one ripped before it was
ripdoctor lookup        find a release in the catalogue, usable entries first
ripdoctor fit           place track boundaries from a spec and measured levels
ripdoctor split         cut a plan into tracks
ripdoctor check         build tick clips, one per boundary, for listening to
ripdoctor import        tag the cut tracks and place them in the library
ripdoctor archive       confirm a record arrived before the raw sides are cleared
```

Two things it will not do:

Nothing is cut until the plan validates. A track that would end before it
starts, overlap its neighbour, collide on a track number or run past the end of
the side is refused with the reason.

The raw sides are not cleared until the record is provably in the library.
`ripdoctor archive` asks beets where it filed things and counts what arrived.

## Development

```
make check     # lint, types, tests, budgets
make test
```

The core layer computes over decibel envelopes and touches nothing - no
subprocess, no filesystem, no clock - so the whole algorithm is testable without
ffmpeg, a sound card or any audio files. `tests/test_architecture.py` enforces
that boundary.

[docs/decisions.md](docs/decisions.md) records what was decided and why.

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
