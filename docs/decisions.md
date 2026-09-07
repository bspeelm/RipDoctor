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

**Status:** SUPERSEDED by ADR-038. beets is required.

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

> **Superseded by ADR-021.** There is no floor; the budgets apply from the first line.

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

> **Superseded by ADR-021.** The decision log is counted like any other live prose.

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

> **Superseded by ADR-021.** One comment ratio, not one per layer.

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
`PYTHON_JIT=0`, and this build has no JIT compiled in), pytest and its plugins,
the cycle collector (`gc.disable()`: four crashes in ten, against five with it
on), the allocator (`PYTHONMALLOC=malloc`: no change), and this project's code.

**It is not the hardware, and it is not this project.** The reproducer needs
neither pytest nor a line of RipDoctor:

    import random
    random.seed(1)
    for _ in range(3000):
        xs = [random.random() for _ in range(4096)]
        xs.sort()
        s = sum(x * x for x in xs)

That segfaults or raises an impossible error in roughly four runs in ten. A C
program compiled by the same GCC 16.1.1 doing the equivalent work - three
thousand rounds of four thousand small allocations each, every byte written and
verified, then two hundred repetitions of a floating-point sum compared against
the first - ran six times with no corruption and byte-identical results. So the
memory and the arithmetic are sound; what is unsound is this interpreter binary
running a hot loop over many small objects.

That last detail is why the rate went up when the live meter arrived: an FFT per
reading is exactly that shape, several hundred times per suite run.

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

**How it shows up, and this is the dangerous part.** It is not only crashes.
The same suite, unchanged, has in consecutive runs: passed completely, died with
a segmentation fault, and reported six failures with errors that cannot happen -

    SystemError: unknown opcode 220
    NameError: name 'exp' is not defined      (in a module that imports cmath)
    TypeError: cannot unpack non-iterable int object

A suite that invents failures is worse than one that crashes, because it sends
somebody chasing a defect that is not there.

**Telling one from the other.** A real failure is an assertion with a message
that makes sense for the thing being tested. Interpreter corruption produces
errors that are *impossible*: an unknown opcode, a NameError for a name that is
imported at the top of the file, a TypeError about a type that cannot be there.
Anything in the second category means run it again.

Observed in the reproducer above, none of which any correct interpreter can
produce:

    TypeError: unsupported operand type(s) for *: 'range_iterator' and 'complex'
    TypeError: 'complex' object does not support item assignment
    TypeError: 'list' object is not an iterator

**The rate is not stable.** Measured on one machine on one day: two runs in ten,
then five, then nine. It moves with nothing this project does - the C control
program stayed clean across the same span, including a run six times longer than
the first. So a local failure says nothing until it repeats, and a local green
run is a smoke test rather than the authority. CI on 3.11 to 3.13 is the
authority, and it has never seen any of this.

**What would reverse this.** The same crashes on a second 3.14 build, or on 3.13
or earlier. Either would mean the fault is in this code after all.

---

## ADR-020 — The comment ratio is a hard ceiling; every other budget is soft

**Status:** accepted. Set by the author, 2026-09-06.

**The comment ratio is never exceeded and never raised.** If prose has outgrown
it, the answer is to move findings into `docs/method.md`, which is where they
belong anyway. There is no version of this project where the right response is a
higher number.

**Every other ceiling is soft, and soft does not mean ignore.** It means the
choice between raising the ceiling and writing worse code is the author's, not
the implementer's. Reaching a soft ceiling is a signal to stop and say so.

**What must never happen** is the third option: quietly writing less to fit.
Trimming a function, dropping a guard, skipping a case or leaving an edge
unhandled because a number was in the way produces exactly the failure the
budgets exist to prevent - a codebase that looks disciplined and is incomplete.

`scripts/budgets.py` prints `HARD` or `OVER` accordingly, and says which
response each one calls for.

**Why the asymmetry.** A line budget bounds how much the project does. That is a
product decision, and product decisions belong to whoever owns the product. A
comment budget bounds how much is *said about* what it does, and past a point
that is displacement rather than documentation - the writing becomes the work.
Nobody needs to be consulted about moving a paragraph into the file written to
hold paragraphs.

---

## ADR-021 — The budgets match the scheme they were taken from

**Status:** accepted. Supersedes ADR-012, ADR-017 and ADR-018.

Four numbers, one of each kind, exactly as in the project this process came
from: a code-line cap, one comment ratio at 25 per cent, one prose ratio at 75
per cent, and a build-artifact size. Plus a zero on runtime dependencies, which
is this project's own.

**What this reverses.** Three accommodations had accumulated, each reasonable on
its own and wrong together:

* a floor below which the ratios did not apply (ADR-012)
* an exemption for the decision log (ADR-017)
* separate comment ratios for `core` and everything else, at 60 and 35 (ADR-018)

All three are gone. The budgets apply from the first line, every live document
counts, and there is one comment ratio. Only genuinely append-only directories
are exempt, because the remedy a budget asks for cannot be applied to them.

**Why the accommodations were wrong.** Each was introduced when a budget fired,
and each made the next firing easier to absorb. A cap on one directory is one
the prose walks out of; a cap that does not apply yet is one that never starts;
a cap split by layer is two caps, each easier to argue about than the one it
replaced. The comment ratio had reached 59 per cent under those rules and read
as compliant.

**What it cost to comply.** 286 lines of comment came out of the package, taking
the ratio from 59 to 18. Nothing was deleted that is not written down elsewhere:
the findings were already in `docs/method.md` and the decisions here, and what
came out of the modules was the second telling. Module docstrings are now one
line and point at the file that holds the reasoning.

**What is still over.** The prose ratio, at 109 per cent against 75. That is a
soft ceiling and therefore the author's call - see ADR-020.

---

## ADR-022 — The prose ratio is knowingly over, and frozen until it is not

**Status:** accepted by the author, 2026-09-06.

> **Superseded by ADR-023 the same day.** The freeze was the wrong instrument:
> prose written ahead of the code is a draft, and rationing a draft loses material
> that is cheaper to cut later than to recover.

The prose budget is breached: 898 lines of live documentation against 779 lines
of code, or 115 per cent where the ceiling is 75. It is accepted as it stands
rather than resolved.

**Why.** It is arithmetic rather than indiscipline. The project is early - one
layer of seven - and the decisions were written before the code they govern,
which is the point of writing them first. The same 898 lines is 45 per cent at
2,000 lines of code, which is where the next two phases land. The reference
project sits at 69 per cent with 6,991 lines of code carrying 4,879 of prose;
the difference is denominator, not restraint.

Retiring something was considered and rejected. The decision log is the record
and cannot be retired without destroying it; `docs/method.md` is the published
asset; the README is the front door. There is no third candidate.

**The condition.** Prose is frozen at its current length while the ratio is over
budget. `budgets.py` fails if live documentation grows past the accepted line
count, so while over the ceiling a new document costs an old one - which is what
"over budget means retiring" means in practice. The freeze lifts on its own as
soon as the ratio comes back under 75, and the ordinary rule resumes with no
further decision.

**What this is not.** Not a floor, and not an exemption. The budget applies, is
measured every run, and is reported every run. What changed is that a breach the
author has looked at and accepted does not also block the build, while still
preventing the thing the budget exists to prevent.

**What would reverse this.** Prose continuing to grow once the freeze lifts, or
the ratio failing to fall as the code does.

---

## ADR-023 — Documentation is a draft until the code is complete

**Status:** accepted by the author, 2026-09-06. Supersedes ADR-022.

The prose ratio stays over budget, and prose may grow. There is no freeze and no
rationing. The documentation written so far is a draft: it was written ahead of
most of the code it describes, and some of it will turn out to be wrong or
redundant once that code exists.

**Why not ration it now.** Cutting prose while the code it describes is still
being written throws away material at the moment it is least possible to judge.
A finding that reads as excessive today may be the one that explains a defect in
Phase 3. Recovering a deleted paragraph costs far more than deleting it later
will.

**The obligation this creates.** A revision pass, once the code is complete and
before 1.0 is published - not an intention but a step in the plan, alongside the
phases. It covers all live documentation:

* findings in `docs/method.md` that the code no longer does, or never did
* decisions superseded so thoroughly that the entry is only history
* README sections describing behaviour that changed
* anything said twice

The ratio is the measure of whether that pass was real. If it is still over 75
per cent when the code is complete, the pass did not happen.

**What is not deferred.** The comment ratio remains hard and is enforced every
run. This concerns `docs/` and the README, not the package.

**Closed by ADR-037**, which records what the pass found and why the ratio
turned out not to measure it.

---

## ADR-024 — The meter's band level was 1.76 dB hot

**Status:** accepted. Fixes a defect inherited from the predecessor.

The live meter's 1-3 kHz reading is computed by summing FFT bin power across the
band and correcting for the analysis window. The predecessor divided by the Hann
window's **coherent gain squared**, 0.25. Summing power requires the window's
**mean square**, 0.375. The ratio is 1.5, so every band reading was 1.76 dB
louder than the signal actually was.

Measured on a half-scale 2 kHz tone, whose true RMS is -9.03 dBFS: the shipped
correction returns -7.27, the correct one returns -9.03 exactly.

**What it affected.** The comment above that line said the correction existed
"so the band figure is comparable with the dB numbers the analysis tools
report", which is the one thing it did not do - the envelope path measures the
band with a filter and an RMS, and sat 1.76 dB below the meter for the same
audio. Anyone comparing the live meter against the waveform's band curve was
comparing two different scales.

The auto-stop gate is unharmed: it is relative, and both its terms come from
this same meter, so a constant offset cancels. The arming floor is absolute and
was therefore effectively -61.76 rather than -60.

**Why it was not noticed.** 1.76 dB is small, systematic, and in the direction
that makes a capture look healthier rather than worse. Nothing in the workflow
compares the two paths numerically; a person looking at both sees a meter and a
curve that broadly agree.

**How it is held.** A test asserts the band reading of a pure in-band tone
matches the full-band reading of the same signal, which is only true when the
normalisation is right. The window's mean square is now computed from the window
rather than written as a constant, so it stays correct if the window changes.

---

## ADR-025 — The port is checked against the reference on real records

**Status:** accepted.

Six records, 73 tracks, 146 boundaries. For each, the reference implementation
was run on the same envelope with the same spec, and its answer committed. The
specs have their ear-set edges removed, so the detector decides every boundary -
re-fitting a spec whose edges are all human-set exercises nothing.

`refit2` was verified deterministic first: run against all 22 specs on the
server, it reproduced every boundary in every plan already on disk. The only
differences were an `mbid` the application adds on save and a `cat: null` where
the catalogue had no duration.

**What the comparison found.**

*Agreement, to the window.* 132 of 146 boundaries are identical. One
disagreement was a genuine port error - `refine` used an exclusive end and a
floored index where the reference is inclusive and rounds, moving an edge by one
window, 0.05 s. Small enough to look like agreement and it is not.

*The overlap defect, on real audio.* The remaining 14 differences are all
ADR-016. Ten are pairs where the reference has a track ending after its
neighbour starts - by up to **0.90 s** - because it clamps each padding against
the gap and not against the other. The same audio is written into both files.
Four more are the same arithmetic landing just short of an overlap, leaving a
0.05 to 0.40 s sliver where this implementation makes the cuts meet.

*A first pass that runs out of side.* On one record the detector gives a track
an end 52 s past its catalogue duration and consumes the gap the last track
needed. That track then starts after the side's music ends - 1268.55 to 1266.57
- and cutting it runs ffmpeg from a later time to an earlier one, writing an
empty file. This implementation produces the same numbers, reports the
catalogue disagreement that preceded it, and then refuses the plan. The
reference has no validation and writes it.

**What is held.** Boundaries must match exactly wherever the gap is roomy; the
exception is stated as a condition, not as a tolerance. The reference must still
overlap at least eight pairs, or the fixtures have changed or the defect was not
real. At least half of all boundaries must come from the detector, or the
comparison has quietly stopped exercising one.

---

## ADR-026 — External programs are run from exactly one module

**Status:** accepted.

`audio/runner.py` is the only module that imports `subprocess`. Everything else
is handed a `Runner` and calls through it. A test enforces the rule by name, so
a second caller cannot appear quietly.

**Why one module rather than one layer.** The earlier form of this rule allowed
`audio/`, `integrations/` and `doctor/` to run programs, which is three places
to keep honest and three places a fake has to be threaded past. One module means
one fake, and everything above it - including the parts that talk to beets and
the parts that check the machine - is exercised with no ffmpeg installed.

**What the seam is worth beyond testing.** A missing program is named before
anything starts, rather than surfacing as a `FileNotFoundError` partway through
a twenty-minute capture. Every call is an argv list and never a shell string, so
a record whose title contains a quote or a semicolon has no string to break out
of. And the fake records every call, which is what allows the argv itself to be
asserted - for cutting, the argv *is* the behaviour, and the return value says
nothing about whether the right seconds were taken.

**What it costs.** A `Runner` is threaded through constructors that would
otherwise reach for a process directly, and the real one is only exercised in
its own tests.

---

## ADR-027 — A reading that is not a finite number is the floor

**Status:** accepted. Fixes a latent defect inherited from the predecessor.

ffmpeg prints `-nan` for a window of digital silence. That is a real reading,
not a fault, and it becomes the floor.

**The defect.** The predecessor guarded with `except ValueError`, which does not
catch it: `float("-nan")` succeeds and returns a NaN. Every such window carried
a NaN out of the parser. It never caused visible harm because those values went
directly into a quantiser that happened to test for NaN, so the cache on disk
was always clean.

Here the parsed lane goes into an `Envelope` and is sorted for a percentile. A
NaN compares false against everything, so the sort order is undefined and the
threshold every detector anchors on becomes arbitrary. The guard is now on the
value being finite rather than on the parse succeeding.

**A second alignment fault, found while fixing it.** A frame whose value could
not be read produced no entry at all, so the lane came back one window short and
every reading after it shifted by 50 ms. Over a side that is a boundary in the
wrong place, from one bad line. Every frame now contributes exactly one reading
per lane, unreadable or not - and a corrupt line is not hypothetical here, since
that is precisely what the interleaving defect produced.

---

## ADR-028 — Detection cannot fail and configuration cannot refuse

**Status:** accepted.

`config.machine.detect()` never raises. A field it cannot determine is left
empty. `config.settings.load()` never refuses: a missing file means every
default, an unrecognised key is a warning, a key of the wrong type is dropped
and named, and a file that is not TOML at all still starts.

**Why both.** If either can stop the program, a half-configured machine cannot
run the command that would explain what is wrong with it, or the edit that would
fix it. Every failure a person can actually act on has to survive long enough to
be reported, and `doctor` is what turns an empty field into a visible problem.

**Three directories, and the split is the portability story.** The state
directory is this program's and is disposable - caches and generated files, and
removing it must never lose anything that cannot be recomputed. The config
directory is the user's and belongs in git. The library root is chosen and holds
the audio. `RIPDOCTOR_DIR` moves the first without moving the second.

**Defaults name no machine.** `capture_device` ships empty rather than holding
one person's card, which is the same decision as ADR-009 seen from the
configuration side.

**A typo gets a suggestion, within a limit that scales.** The distance counts an
adjacent swap as one change rather than two, because `prot` for `port` is a
single slip and the most common typo there is; plain Levenshtein scores it 2 and
a limit tight enough to be useful then rejects it. Nothing is suggested for a
word that resembles no key - suggesting `port` for `xyzzy` is worse than
silence.

**Every threshold is documented and the documentation is tested.** A table in
`docs/method.md` carries each name, its value and where the number came from,
and a test asserts both that every threshold appears and that the documented
value is the one the code uses. The numbers are the domain knowledge, so they
are the thing that must not drift.

---

## ADR-029 — The fake runner distinguishes "everything" from "nothing"

**Status:** accepted. Fixes a defect in the test seam itself.

`FakeRunner(installed=None)` means every tool exists; `FakeRunner(installed=set())`
means none do.

**The defect.** The field defaulted to an empty set and was tested for
truthiness, so both spellings meant the same thing - everything exists. A test
that asked for a machine with nothing installed silently got a fully equipped
one and passed for the wrong reason. It was found by a doctor test that expected
a bare machine to fail and watched it succeed.

A fake that lies is worse than no fake, because everything above it is then
tested against a world that cannot occur. Neither value is now a default that
could be reached by accident.

---

## ADR-030 — The lane and its anchor travel together

**Status:** accepted.

`core.gaps.find` refuses to guess whether an envelope is the full lane or the
band lane, because the two need opposite anchors and getting it wrong inverts
detection rather than degrading it. The command line inherits that refusal:
`ripdoctor fit --lane` names which lane the supplied envelopes hold, and the
anchor follows from it.

**Why it needed saying.** The first version of the command hardcoded the band
anchor and was handed full-band envelopes, which produced a plan with tracks
running backwards. The refusal in the core is only worth having if every caller
above it is equally explicit; a caller that guesses on the core's behalf
reintroduces exactly what the core refused to do.

---

## ADR-031 — The meter reads the file the recorder is writing

**Status:** accepted.

ALSA gives one program the input. A live meter that opened its own stream would
be a meter that could not run during a capture, which is the only time it
matters. So the meter reads the tail of the WAV the recorder is already writing:
the last block of frames, seeked from the end, metered in memory.

Two things follow from it. The format is read out of the header on every reading
rather than assumed, because `wave.open` refuses a file that is still being
written and because a 96 kHz capture metered as 48 measures 2-6 kHz and reports
it as 1-3. And the capture is written as WAV rather than piped into an encoder:
a pipe ended with a signal never closes the stream, so the header is never
backfilled, the file reports no duration, and there is nothing to seek into
while it grows.

---

## ADR-032 — The recorder's errors go to a file, not a pipe

**Status:** accepted.

A capture runs for twenty minutes and a driver reporting overruns writes for all
of it. Nobody is draining a pipe during that, and a full pipe blocks the writer -
so the program recording the record would stall on a diagnostic about the record.
The predecessor solved this with a thread per capture whose only job was to read
a pipe nobody wanted. A file cannot block, it can be read while the capture runs,
and the overrun count comes out of it at the end. Each overrun is a slice of the
record that is not in the file, so it is reported rather than dropped.

---

## ADR-033 — A capture is interrupted, and only killed if it will not stop

**Status:** accepted.

`arecord` backfills the WAV header - which is where the length lives - when it is
interrupted, and does not when it is killed. So stopping a side sends SIGINT and
waits; a recorder that ignores it is killed after the grace period, because a
capture that will not stop is worse than a header that needs repairing.

The same reasoning covers Ctrl-C: it stops the record, not the program. The loop
catches it, stops the recorder properly, and encodes what was captured. A side is
twenty minutes of somebody's evening, and every path out of the capture loop
encodes what is on disk rather than discarding it.

---

## ADR-034 — The code ceiling moves from 4,000 to 5,000, once

**Status:** accepted. Set by the author, 2026-09-06.

4,000 was chosen in Phase 0, when the package was a scaffold and the web layer
did not exist. It was not a measured number and nothing about it was wrong; it
simply predates a quarter of the application.

At the point it bound, the code stood at 3,939 lines against roughly 770 still
to write: the socket adapter, `ripdoctor serve`, the remaining parity routes -
capture, split, review, import, archive, library, artwork, punch - and Phase 5's
artwork and beets importer. That lands near 4,700.

**Why 5,000 rather than 4,700.** A ceiling set exactly at the estimate is one
that has to move again the moment the estimate is out, and a ceiling that moves
often is not a ceiling. This moves once, with room, and any further move needs
its own record saying what changed.

**What did not move.** The comment ratio, which is hard and stays at 25%. No
code was trimmed, no guard dropped and no case skipped to fit either number -
that is the failure this budget exists to prevent, and it would have been the
wrong answer here. ADR-020.

**What would reverse this.** The web layer coming in far under estimate, or a
decision to ship the command line alone. Neither would justify moving the
ceiling back on its own; a smaller number is only worth setting if something is
retired to fit it.

---

## ADR-035 — The ceiling moves again, because the front end ports whole

**Status:** accepted. Set by the author, 2026-09-06. Amends ADR-034.

ADR-034 said a further move needs a record saying what changed. This is it.

**What changed.** The front end was to port as-is. That turned out not to be
possible: the page calls artwork and punch endpoints that do not exist here, and
chat endpoints that were deliberately cut. The choice was to write a smaller page
matched to what exists, or to build the two missing features so the page ports
whole. Building them is the decision, so the estimate ADR-034 was set against no
longer covers the work.

Artwork and punch are roughly 450 lines with their routes, and the beets importer
another 150. That lands near 5,400, so the ceiling is 6,000 for the same reason
5,000 was not 4,700: a ceiling set at the estimate moves again the moment the
estimate is out.

**What punch is, since it is now in scope.** A capture of one track, taken to
replace a track that came out dirty - a skip, a click, a passage the stylus
fought. It is not a side and must never be mistaken for one: it is written under
a stem no side scan matches, so the album picker and the archive gate cannot see
it. That invisibility is the whole reason for the separate name.

**What did not move.** The comment ratio, still hard, still 25%. Nothing has been
trimmed to fit any of these numbers.

---

## ADR-036 — The ceiling moves to 7,000, and this is the last move before the trim

**Status:** accepted. Set by the author, 2026-09-07. Amends ADR-035.

**What changed.** Porting the front end whole turned out to mean more than
artwork and punch. The page reaches for two things this project had simplified
away, and both were worth having rather than cutting:

- **Listening to the input, live.** The only way to hear what the stylus is
  doing while you cue it, and one of the things this does that a
  digitise-an-upload tool cannot. It needed a streaming response, which the
  adapter did not have.
- **A real archive step.** What existed moved `raw/` into `archive/` and left
  the cut tracks, the tick clips and the measurements behind - a second copy of
  every track plus an envelope and a proxy per side, all of it derived, all of
  it large. Archiving now reads every side back from where it will live,
  re-encodes the truncated ones so the archive copy has a length in its header,
  and only then clears what can be made again.

Re-labelling and the early duplicate warning came back for the same reason: the
page asks for them, and both are useful without beets.

**Why 7,000, and why this is the last one.** What remains is the beets importer
behind the interface the base tagger already implements - a couple of hundred
lines - and then Phase 6, which is a revision pass that removes rather than
adds. A ceiling that has moved three times is a ceiling that is being followed
rather than set, so the next number to change should be a smaller one, chosen
during the trim, with something retired to fit it.

**What has not moved, through all three.** The comment ratio. Nothing has been
trimmed, no guard dropped and no case skipped to fit any of these numbers.

---

## ADR-037 — The prose ratio stopped measuring the thing it was chosen for

**Status:** accepted. Closes ADR-023, 2026-09-07.

ADR-023 said the ratio was the measure of whether the revision pass was real:
still over 75 per cent when the code was complete, and the pass did not happen.

It is at 23 per cent, and the pass had not happened. Prose grew from about 1,250
lines to about 1,430 while the code went from 2,600 to 6,200 - so the number
fell by a factor of three without a word being retired. A ratio against a
denominator that trebles measures the denominator.

**What the pass actually found**, done properly rather than by the number:

- The README said capture and the web interface were not built. Both are.
- Two screenshots were carried over from the predecessor: its name in the
  header, a feature that has since been cut visible in the corner, and the
  author's own record collection in the album picker. Nothing referenced them.
  They are removed, and come back when there is something to photograph that
  has met a turntable.
- `docs/method.md` was accurate throughout and gained one finding it was
  missing - that a correlation window is a count of samples and has to come from
  the file's own rate, which is why one side that should have aligned was
  refused.
- The install section promised there was nothing to install yet.

**What holds it true from here**, in place of a ratio: every command the README
names is a real subparser and every subparser is named; every threshold appears
in `docs/method.md` with the number the code uses; every element the front end
reaches for exists in the markup, every module it imports is there, and every
endpoint it calls is a route. Those fail when prose and code disagree, which is
what the ratio was standing in for.

The ratio is still reported every run. It is a trend, not a gate, and this
record is here so nobody reads a low number as evidence of a tidy repository.

---

## ADR-038 — beets is required

**Status:** accepted. Set by the author, 2026-09-07. Supersedes ADR-007.

beets is a dependency, not an extra. It is part of the flow rather than an
alternative to part of it, and an install without it is not this application.

**What ADR-007 got wrong.** It reasoned from the coupling: beets was the
second-largest source of machine-specific assumptions in the predecessor, so it
should be optional. That is an argument about how to *contain* a dependency, and
it was answered - beets sits behind an interface now, with the whole application
above it testable without beets installed. Having contained it, ADR-007 went on
to make it optional as well, which was a decision about what the tool is, taken
on the strength of an argument about how it was built.

**What it costs.** The headline "installs with no Python dependencies" is gone.
It was worth something, and it is not worth this: the fingerprint matching, the
path formatting, the album-mode ReplayGain - which matters on vinyl, where one
twelve-second fade scored +20.1 dB track gain against +6.8 for the album - the
duplicate resolution and the library database are what the finished record is
tagged and filed by. A record imported without them is one somebody has to fix
later by hand.

**What is kept.** The `metaflac` importer stays as `importer = "tagger"`. It is
written, tested, and it satisfies the archive gate on its own, which makes it
the answer when beets is broken or unavailable at the moment somebody is halfway
through a record. Keeping working code that costs nothing is cheaper than
deleting it and wanting it back.

The dependency budget becomes "beets and nothing else" rather than zero. A
budget of zero was a good discipline for the fourteen weeks it lasted; it is
also how a project ends up with a worse importer to protect a number.

---

## ADR-039 — Versions come from tags, and 1.0 waits for a turntable

**Status:** accepted. Set by the author, 2026-09-07.

**The version is the tag.** `hatch-vcs` writes it from git, so there is no file
to bump and nothing that can disagree with the tag - which is the failure the
process this borrows from guards against with a whole step. `make release
VERSION=x.y.z` checks that the tree is clean, on main, level with the remote and
green, then tags and pushes. Actions runs the gates again at the tag rather than
trusting the branch, because a tag can be pushed at any commit, including one CI
never saw.

Publishing to PyPI is part of cutting a release rather than a separate act. It
runs last in the workflow, because it is the only step that cannot be undone: a
release page can be deleted and made again, and a version on PyPI can only be
yanked.

Trusted publishing rather than an API token, for the same reason the artifacts
are signed keylessly: there is no secret to store, rotate or leak, and the
upload is bound to this workflow at this tag.

**This is not 1.0, and here is what 1.0 needs.** Everything is built and tested,
and none of it has met a turntable. The tests that carry the hardware behaviours
- ALSA contention, the header a signal leaves behind, overrun counting, the
auto-stop firing on a real run-out - are named and skipped, which is honest
about their status and no substitute for running them.

1.0 is when:

- the whole chain has taken real records end to end, on the machine it was
  written for, without the predecessor running beside it;
- the hardware tier has actually run rather than been skipped;
- somebody other than the author has installed it from PyPI and reached the
  point of cutting a record.

Until then the numbers are 0.x, and the shape of the promise is that the
interfaces may still move. Calling it 1.0 before a needle has touched a record
would be claiming something nobody has checked.
