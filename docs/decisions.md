# Decisions

Architecture decision records. One entry per decision that would otherwise be
re-argued. Add one whenever a stated principle is bent, and say why.

Superseded entries stay in place with a note. They are not deleted.

---

## ADR-001 — The project is called RipDoctor

**Status:** accepted.

The tool was called cutAssist while it ran on one machine. The published name is
RipDoctor, used everywhere: repository, package, command, and UI. `ripdoctor`
was unregistered on PyPI, GitHub search returned no repositories, and no
existing product uses the name.

**What it costs.** A rename pass across roughly 11,000 lines, and the loss of
the connection to the older documents, which say cutAssist throughout. Those
documents are being rewritten anyway.

**Note.** An older, superseded script named `ripdoctor.py` exists in the
predecessor's toolchain. It is not this, and it is not ported. Do not let the
names collide when reading history.

---

## ADR-002 — MIT

**Status:** accepted.

Maximum adoption, minimum friction, and conventional for a tool of this kind.
The predecessor project in this space is also MIT.

---

## ADR-003 — The assistant is not part of 1.0

**Status:** accepted.

cutAssist included a chat assistant backed by a hosted model, with the method
handbook injected as its system prompt and fifteen tools that acted on the plan.
It is not in 1.0.

**Why.** It required every user to supply and pay for an API key, which puts a
third-party billing relationship in front of a tool that otherwise runs entirely
offline. It was also the only non-stdlib dependency in the entire application.
Removing it deletes about 617 lines and takes the dependency count to zero.

**What is lost.** The assistant was genuinely useful on difficult sides, and it
is the most distinctive thing the predecessor did. This defers it, it does not
reject it.

**What would reverse this.** A 1.x release, once the deterministic pipeline is
covered by tests and the handbook has been separated from one person's records.

---

## ADR-004 — Python 3.11, and zero runtime dependencies

**Status:** accepted.

`requires-python = ">=3.11"`, and `dependencies = []` for the base install,
enforced by `scripts/budgets.py`.

**Why.** `tomllib` entered the standard library in 3.11, so TOML configuration
costs nothing. The algorithm needs no numerical library: the predecessor's
toolchain was written against `array`, `math` and `cmath` alone, and the heavy
work is done by ffmpeg, which is a system dependency rather than a Python one.
An audio tool that installs with no transitive dependencies is worth more than
the convenience of numpy.

**What it costs.** Debian 11 ships Python 3.9 and is excluded. Some pure-Python
paths are slower than a numpy equivalent would be; where speed matters the work
is already delegated to ffmpeg.

---

## ADR-005 — The core operates on envelopes, not sample buffers

**Status:** accepted.

`ripdoctor/core` takes a dB envelope on a uniform grid. It does not take PCM,
except in the live meter, which is a separate entry point.

**Why.** Decoding is the expensive part and ffmpeg already does it: an `astats`
pass over a 22-minute side takes about 5.2 seconds, where the equivalent
pure-Python loop is not usable interactively. A core defined over sample buffers
would have pulled that loop back into the hot path. It is also the natural unit
of the algorithm — the predecessor's two gap-detection functions both took
levels rather than samples, and had already drifted into two signatures for one
behaviour.

**What it costs.** The core cannot be handed a file. Callers decode first, which
means the seam has to be explicit — which is the point.

**Consequence worth stating.** The pure-Python decoder is kept, in `audio/`, as
a test oracle. It proves the fast path agrees with a slow, obvious
implementation. It is never a production path.

---

## ADR-006 — Live capture and file import are equal inputs

**Status:** accepted.

A side may be recorded by RipDoctor or handed to it as an existing WAV or FLAC.
Neither is the primary path.

**Why.** The turntable may not be attached to the machine that holds the
library, and some people already record in an editor they trust. Treating import
as a second-class path would exclude them for no benefit. Equal support also
means the whole cutting pipeline is exercisable without hardware, which is what
makes it testable.

**What it costs.** Two ingest paths to maintain and document rather than one.

---

## ADR-007 — beets is one importer, not the importer

**Status:** accepted.

The base install tags with `metaflac` and places files under a configured
library root. `ripdoctor[beets]` adds beets as an alternative importer behind
the same interface.

**Why.** beets accounts for roughly 1,280 lines of host coupling in the
predecessor and is the second-largest source of machine-specific assumptions
after paths. More importantly, the archive step is gated on the album being
present in the beets library, so an install without beets could never complete
the workflow — raw sides would accumulate with no way to clear them. The gate is
correct and stays; what changes is that "the destination holds N verified
tracks" can be established without beets.

**What beets still earns.** Acoustic fingerprint matching, path formatting,
album-mode ReplayGain, duplicate resolution, and a real library database. Those
are reasons to install the extra. They are not reasons to require it.

---

## ADR-008 — Thresholds are configuration with measured defaults; automatic calibration is deferred

**Status:** accepted.

Every detection constant becomes a documented field on a `Thresholds` value,
defaulting to the number in use today. `ripdoctor measure` records reference
levels on the user's own chain and prints them beside the defaults, emitting a
configuration snippet. It does not rewrite configuration itself.

**Why.** The shipped numbers were derived by replaying fifteen archived sides on
one signal chain, calibrated at a peak of −12.3 dBFS. They are real
measurements, not preferences, but they are one chain's measurements. Deriving
new thresholds automatically requires knowing how they generalise, and that is
not yet known — there is one turntable to learn it from. A command that computed
new values would be shipping a guess with the authority of a measurement.

**What it costs.** A user on a different chain has manual work to do. The
documentation has to explain what each number means rather than hiding it.

**What would reverse this.** Measurements from several different signal chains
showing how the numbers move.

---

## ADR-009 — The device-name heuristic is deleted; the signal guard is kept

**Status:** accepted.

The predecessor decided whether an input was the turntable by testing whether
the card name contained `USB` or was called `CODEC`, and refused to record
otherwise. The heuristic is removed. The refusal is not.

**Why.** The heuristic makes the application unusable on any machine that is not
the one it was written on. The guard behind it is sound and was added for a
specific reason: an interface lost its USB connection and 162 seconds of
mic-jack bleed were captured instead of a record. The general form of that check
already exists in the code — no music-like signal after 30 seconds of recording,
with the band level reported — and it catches the same failure on any machine
with no knowledge of device names.

**What it costs.** Nothing on the original machine. Elsewhere it converts a
refusal into a warning plus an early abort, which is the correct trade.

---

## ADR-010 — Routes are values

**Status:** accepted.

HTTP handlers are pure functions from a request value to a response value. The
server is a thin adapter over them. The standard library's `http.server` is
kept.

**Why.** The predecessor's 1,051-line handler could not be tested without
opening a socket, which would have left roughly a fifth of the codebase outside
the test suite. Separating the routing from the transport is about sixty lines
of work. A framework would solve the same problem by adding three dependencies
and an async model, for a single-user application on a local network.

**What it costs.** Some hand-written plumbing that a framework would supply.

---

## ADR-011 — The integration contract is a directory

**Status:** accepted.

RipDoctor writes tagged files into a library layout and stops. It does not
serve, index, stream, or synchronise them.

**Why.** The predecessor ran alongside a music server, a file browser and a
synchronisation daemon, and called none of them. That was not an accident of
implementation; it is what makes the tool portable. Anything that watches a
directory can consume its output, and nothing has to be installed to try it.

**What it costs.** No integration conveniences. A user wanting their player to
notice a new album waits for that player's own scan.

---

## ADR-012 — Ratio budgets are not enforced below 500 lines of code

**Status:** accepted.

`scripts/budgets.py` enforces absolute line counts from the first commit, but
the comment-ratio and documentation-ratio budgets are printed and not enforced
until the package reaches 500 lines of code.

**Why.** A ratio needs a denominator worth dividing by. At the end of Phase 0
the package was 23 lines of code against a full set of decision records, giving
a documentation ratio of 1535 per cent. Enforcing the cap there would have
meant either raising it to a number that means nothing later, or deleting
documentation written deliberately and in advance. Neither is the behaviour the
budget exists to produce.

**What it costs.** There is a window early in the project where prose can grow
unchecked. It is bounded: the floor is a stated number, the ratios are printed
at every run so the trend is visible, and they begin to bite on their own.

**What this is not.** It is not permission to raise a ceiling that has started
to bite. Once a ratio is enforced, exceeding it means retiring something or
writing a record explaining why the number moved.

---

## ADR-013 — Python, not Rust or Go

**Status:** accepted.

The implementation language is Python. This was reconsidered deliberately at the
end of Phase 0, when the switching cost was still close to zero.

**Why.** The numeric work is not where the time goes. Measured at production
sizes on Python 3.14:

| Operation | Size | Time |
|---|---|---|
| `gaps()` over a 22-minute side | 26,400 windows | 2.5 ms |
| Alignment, coarse pass | needle 150, hay 600 | 1.7 ms |
| Alignment, fine pass | needle 100, hay 1,250 | 3.0 ms |
| **All production numeric work, per side** | | **≈ 7 ms** |
| ffmpeg `astats` over the same side | | 5,200 ms |

Python accounts for roughly 0.13 per cent of the analysis time. A compiled
language would take 7 milliseconds down to a fraction of one, on a workflow
whose next step is a person listening to a record. The design already delegates
every expensive operation — decode, windowed RMS, the tick filtergraph,
sample-accurate cutting, the Opus proxies — to ffmpeg, which is C.

**The strongest argument against this decision** is distribution rather than
speed: a single static binary needs no interpreter, no virtual environment, and
does not meet a PEP 668 externally-managed environment on Debian. That argument
does not survive the ffmpeg dependency. ffmpeg cannot be removed — replacing it
means writing a FLAC encoder, an Opus encoder, a resampler and a filter graph,
and the result would be worse than ffmpeg. Since the user installs system
packages either way, a static binary saves the interpreter and nothing else.
Zero runtime dependencies (ADR-004) removes the failure mode that makes Python
packaging unpleasant, and `pipx install` handles the rest.

**What it costs.** An interpreter on the target machine, and a slower path if
a genuinely hot loop ever appears. The likeliest candidate is the live meter's
FFT, which currently runs about once a second.

**What would reverse this.** A measured hot path that matters. The remedy then
is a compiled extension for that one function, not a rewrite.

**Not a factor either way.** beets is driven by subprocess, never as a library,
so it argues for neither language. The browser front-end is JavaScript
regardless.

---

## ADR-014 — The envelope window-count tolerance is two per cent, not ten

**Status:** accepted. Amends the behaviour ported from the predecessor.

An envelope is cached only if its window count matches the audio's duration.
The predecessor allowed a ratio between 0.9 and 1.1. RipDoctor allows 0.98 to
1.02.

**Why.** The wide tolerance had already let a real fault through: an envelope
measured at 44.1 kHz matched against audio at 48 kHz gives a ratio of 0.919,
which sits inside ten per cent and is wrong by an amount that grows along the
side. Every cut taken from such an envelope is confidently misplaced.

The replacement is measured rather than chosen. Across the 29 real sides in the
fixture set the worst honest deviation between declared windows and computed
duration is **0.072 per cent**. Two per cent is a 28-fold margin over anything
real, and rejects every sample-rate confusion: 44.1 against 48 kHz is 8.1 per
cent out, 48 against 96 is 50 per cent out.

**What it costs.** An envelope produced by some future measuring path with a
genuinely different windowing convention would be rejected rather than cached.
That is the intended behaviour; the alternative is silently misaligned cuts.

**How it is held.** `test_the_tolerance_clears_every_real_side` fails if real
captures ever drift close to the tolerance, before anyone's envelope is
rejected by it.

---

## ADR-015 — Isolated corrupt windows are tolerated and measured, not repaired in the core

**Status:** accepted.

Peak level cannot be lower than RMS level over the same window; it is
arithmetic. Four of the 29 real fixture sides violate it, in exactly one window
each, at magnitudes from 1 to 49 dB.

**Why it is not a reader bug.** A swapped lane order or a broken parser would
fail on every window of every side. One isolated window per side is the
signature of interleaved `astats` output, which the predecessor already handles
when parsing: two concurrent ffmpeg passes writing to one stream merge a line
and a value is misread.

**The decision.** `core` does not repair these. It computes over what it is
given, and the test suite asserts the *rate* of the anomaly rather than its
absence - currently under one in ten thousand readings. If the lanes were ever
swapped or the parser regressed, the proportion jumps and the test fails.
Detecting and re-measuring a corrupt window needs the audio, so it belongs to
the layer that has it.

**What it costs.** A single bad window can still perturb a threshold. At one
window in twenty-five thousand, against detection that requires a run of at
least twenty-four consecutive quiet windows to call a gap, the exposure is
negligible - but it is exposure, and it is written down rather than assumed
away.

---

## ADR-016 — A gap too small for both paddings is divided, not clamped twice

**Status:** accepted. Fixes a defect inherited from the predecessor.

Each track keeps `tail` seconds after its last note and the next keeps `lead`
before its first. When the gap between them is shorter than `lead + tail` there
is not enough room for both, and the two have to be reconciled against each
other rather than clamped independently.

**The defect.** The predecessor computed them separately:

```
end = min(music_end + tail, next_start - 0.2)
nxt = max(next_start - lead, music_end + 0.2)
```

Neither expression can see the other, so on any gap shorter than `lead + tail`
- 2.8 seconds with the shipped values - `end` lands after `nxt`. Measured on a
1.6 second gap: the first track ended at 71.35 and the second began at 70.25.
The tracks overlapped by 1.1 seconds and the same audio was written into both
files. Nothing downstream objected; the cutter wrote what it was told.

**The fix.** When the gap cannot hold both paddings, divide it in proportion to
what each side asked for, so both give up the same fraction and the two cuts
meet at a single point. There is then no groove left over, which is correct - a
gap that short has none to discard.

**Why it was not noticed.** The record this logic was developed on has gaps of
4.8 to 14.2 seconds, comfortably above the threshold. It would have appeared on
the first tightly-cut side.

**How it is held.** A test reproduces the overlap directly, and another runs the
fitter across gap widths from 0.5 to 9 seconds and validates every resulting
plan. `validate()` would also have caught it, but only for someone who called it.

---

## ADR-017 — Append-only records are exempt from the prose budget

**Status:** accepted.

`docs/decisions.md` is not counted in the documentation-ratio budget.

**Why.** That budget's stated remedy is "retire something". It cannot be applied
to this file: its own header says superseded entries stay in place with a note,
and deleting a decision record to fit a ceiling destroys the record the ceiling
exists to keep honest. bothy exempts `docs/history/` for the same reason.

Fixed at the same time: the budget was counting `.pytest_cache/README.md` as
project prose. Build artifacts are not documentation, and any path with a
dot-prefixed component is now skipped.

**What it costs.** Nothing bounds the number of ADRs. The discipline has to come
from only writing one when a decision is actually made, which is what the file's
header asks for.

---

## ADR-018 — The comment budget is split between core and everything else

**Status:** accepted. Replaces the single 40 per cent ceiling.

`ripdoctor/core` may run to 60 per cent comment lines. Everything outside it is
capped at 35.

**Why.** The single ceiling was set at 40 without measuring anything, which the
budget script's own comment had explicitly warned against - it says to set the
number from a measured baseline rather than by taste. Measured with the same
counter:

| | code | comment | ratio |
|---|---:|---:|---:|
| the four predecessor modules core was ported from | 424 | 233 | **55.0%** |
| the three live predecessor tools | 179 | 92 | 51.4% |
| RipDoctor's core | 494 | 260 | **52.6%** |
| the predecessor's whole application | 3828 | 1257 | **32.8%** |

Two kinds of code are being measured with one number. In `core` the comments are
the experimental record - which threshold came from which measurement, which
approach was tried and abandoned - and that is the thing being published. Outside
core it is subprocess plumbing, routing and file handling, where the same density
would be noise. The predecessor shows both figures clearly, and RipDoctor's core
is already slightly leaner than the code it came from.

**What was done before changing the number**, because a budget that is raised
whenever it bites is not a budget: the duplicated 1-3 kHz explanation was
retired from `envelope.py` (it is stated once, in `core.gaps`), and the
long-form findings moved out of module docstrings into `docs/method.md`, where
they belong. That took the whole-project ratio from 64 to 56 per cent on its
own. 40 was still unreachable without deleting measurements.

**What it costs.** `core` can carry more prose than the rest of the project, and
the honest risk is that new code is put in `core` to get the looser ceiling. The
existing `core lines` cap of 2000 is what bounds that, and the layering test
stops anything impure being moved there to qualify.

**What would reverse this.** Core drifting above 60 would mean the findings have
outgrown the code that acts on them, and belong in `docs/method.md` instead.

---

## ADR-019 — Python 3.14.6 segfaults intermittently here; the suite is not at fault

**Status:** accepted as a known environment defect. Not worked around further.

On the development machine - Fedora's Python 3.14.6, built with GCC 16.1.1 - the
test suite segfaults on roughly one run in ten. The fault lands inside
`envelope.decode`, in code that multiplies integers by floats and builds a
tuple. Pure Python arithmetic cannot segfault in a correct interpreter.

**What was ruled out.** The experimental JIT (it still crashes with
`PYTHON_JIT=0`), pytest plugins (it crashes with hypothesis, cov and the cache
provider all disabled), the cycle collector, and any single test file - one file
alone reproduces it. It does not reproduce in a plain script looping over the
same decode sixty times, so it needs pytest's environment, not the workload.

No second interpreter is installed on this machine to compare against.

**What was done.** `decode` now maps bytes through a precomputed 256-entry table
instead of computing the same 256 answers 26,500 times per lane. That is the
right implementation on its own merits and it lowered the crash rate, but it did
not remove it - which is the evidence that the allocation pattern was not the
cause.

**Why nothing further.** CI runs 3.11, 3.12 and 3.13, none of which are
affected, so this does not reach anyone else. Contorting the code around an
interpreter bug that a point release will fix would cost more than re-running a
failed check.

**How it shows up.** `make check` exits 139 with no test failure reported. Run it
again. If it fails twice with an actual assertion, that is a real failure.

**What would reverse this.** The same crash on 3.13 or earlier, or on a second
3.14 build. Either would mean the fault is in this code after all.
