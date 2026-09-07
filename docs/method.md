# Method

What RipDoctor knows about finding track boundaries on vinyl, and how it was
learned. Each item below cost a wrong cut.

The numbers are measurements from one signal chain. Treat them as the shape of
the problem, not as constants: anchor to a known gap and a known music passage
on the side actually in front of you.

## The 1–3 kHz lane

On a sparse pressing, full-band level cannot separate an inter-track gap from a
quiet passage. Both sit near −43 dB, and no threshold exists between them.
Restricted to 1–3 kHz the same two things separate by about 20 dB.

| | full band | 1–3 kHz |
|---|---|---|
| music | −26 | −40 |
| arm up, being handled | −36 | −67 |
| silent groove | −64 | −85 |
| dead air, needle up | −89 | −93 |

Two reasons it works. Vinyl's noise — plinth rumble, arm handling, warp — is
bass-heavy, so excluding below 1 kHz removes the dominant contaminant. And on a
quiet pressing there is nothing above 8 kHz to measure: one region read −94.3 dB
with the stylus certainly down and −94.5 dB with it certainly up, so that band
carries no information about groove contact at all.

The cost of not knowing this: two tracks came out 29 seconds long and 22 seconds
short on the first automated pass of one record.

## The two lanes need opposite anchors

This is the part that inverts rather than degrades when it is wrong.

**The full lane thresholds down from the music** — the 85th percentile, minus a
margin. It cannot anchor on the floor, because the quietest reading on a side is
needle-up electrical noise, well below groove noise. Anchored there, no side
reports any gaps at all.

**The band lane thresholds up from its floor.** Its dynamic range is much wider
— music around −35 against a floor near −85, where the full lane runs −24 to
−50. Music-minus-16 in the band lane lands near −51, which is inside the quiet
music: on one measured side that produced 57 gaps where the record has a
handful.

## Three floors, not one

A side has at least three distinct noise floors, tens of decibels apart:

| floor | level | what it is |
|---|---|---|
| electrical | ≈ −70 | the converter's own noise, stylus not yet down |
| lead-in / run-out groove | −45 to −57 | stylus down, no signal cut |
| inter-track gap | −52 to −66 | varies by pressing |

Any detector that anchors on the minimum picks whichever of these happens to be
lowest on that side, and finds nothing.

## Peak finds the fade; RMS loses it

RMS averages a decaying tail into its own window, so the end it reports is where
the tail stops dominating rather than where the music stops — typically three to
four seconds early. On one record a listener flagged **six** track ends in a
single pass, every one of them early.

Use RMS to locate *where* the gaps are. Use peak to judge where each one begins
and ends.

## Refine against the gap's own floor

Once a gap is located, its edges are found by measuring the floor of that
particular gap — the middle half of it, so the fade at one end and the lead-in
at the other do not contaminate the measurement — and walking outward to about
8 dB above it.

Judging edges against the side-wide threshold instead truncates every fade, for
the same reason RMS does.

## Two constraints must agree

A boundary is trusted only where a measured gap and the catalogue duration point
at the same place. Gaps alone cannot tell an inter-track silence from a pause in
a song. Arithmetic alone drifts, because a pressing is never exactly the
catalogue.

Where they disagree, the disagreement is clamped and **reported**. A boundary
that had to be forced is exactly the one to listen to; absorbing it silently
produces a plan that looks clean and is wrong.

The approach this replaced walked forward from the side start, snapping to
whatever gap was within ±6 seconds. Its failure signature is a drift that
*decreases* along the side — measured at +16.15, +9.12, +2.75 s — because each
bad cut poisoned the next track's start. Gaps of 17–19 seconds exist, and a ±6 s
window cannot see them.

## Per-track start and end, not shared cut points

A boundary modelled as one point shared by two tracks forces the whole
inter-track groove onto one side of it. With gaps running 4.8 to 14.2 seconds
that means a track ending in thirteen seconds of blank groove, or starting with
it.

Each track gets its own start and end. Each keeps `lead` seconds before its
first note and `tail` after its last; whatever remains between them belongs to
neither and is not written.

## The ear is the final instrument

Level finds where music *dominates*. Only a listener finds where music *is*. No
amount of tuning closes that gap, so every fade gets a human pass, and an edge
set by ear is never overridden by a detector.

The check is made answerable rather than open-ended: a 1400 Hz tick, 60 ms, is
mixed at the exact cut instant, so the question is one thing — does the tick
land in the gap, or on the music? Handing someone a clip and asking them to find
the boundary by counting seconds does not work, and is not their job.

## Traps that are not about detection

**A capture device is chosen, never defaulted to.** The first device a machine
lists is whatever the motherboard calls its own audio, and its input is
whatever some other program last selected. Recording from it produces a file
that looks like a quiet record: signal present, nothing musical in it. Name the
device, check the name against what the machine can currently see, and refuse a
different one unless it is asked for twice.

**The side's loudest moment is often the needle drop**, not music — a brief
impulse in the lead-in that can reach full scale. Reading a peak measurement
without checking *where* it falls will convince you a clean record is clipped.

**ffmpeg's resampler runs about 0.25 per cent long**, so an envelope taken from
its output must be rescaled onto the audio's true decoded duration. Uncorrected,
that is roughly three seconds of drift across a twenty-minute side, with every
cut late by a growing amount.

**`arecord | flac` produces a truncated file.** Ending the pipe with Ctrl-C
means STREAMINFO is never backfilled: total samples reads zero, `ffprobe`
reports `duration=N/A`, and `flac -t` fails. Duration must come from decoding,
and such a file must be re-encoded rather than moved into an archive.

**A quiet intro is not groove noise.** Two captures of the same side correlate
at r > 0.99 wherever real groove content exists; handling noise, arm rumble and
dead air do not correlate at all, at r ≈ 0.2. That is how to settle whether a
suspicious quiet stretch is on the record or was introduced.

## When a side has ended

Detecting the run-out is worth doing but is not worth trusting alone. Measured
across fifteen archived sides:

* **An absolute threshold is useless.** Run-out read −41 to −85 dB in the band
  lane across those sides, and on one it was *louder* than the quietest music on
  the same record. The gate has to be relative to that side's own music level.
* **Music can be very quiet for a very long time.** One side sits 17.5 dB below
  its music level for two continuous minutes, a third of the way in, and is
  still the song.
* **20 dB below is the tightest gate that never trips inside any of the
  fifteen.** That is 2.5 dB of margin, which is why the dwell is two minutes and
  why a hard cap exists at all.
* What the gate reliably catches is a lifted or never-dropped needle — dead air
  sits about 50 dB down. Whether it catches a needle sitting *in* the run-out
  depends on the pressing: one measured 24 dB down and trips, a quieter groove
  might not. That case belongs to the hard cap.

Flatness was tried as a second discriminator and rejected: run-out measured a
standard deviation of 6.74 against quiet music's 8.51, too close to separate.

The detector must not arm before the needle is down, or the minutes spent cueing
trip it immediately. Once it has been quiet for thirty seconds with nothing
music-like, that is worth saying out loud: one capture ran its whole length at
−77 dB — a wrong input — and the meter reported it the entire time, to nobody.

## Carrying a cut across a re-rip

Re-ripping a record does not invalidate boundaries somebody already approved.
Needle-drop timing drifts by seconds between sessions; timing *within* a side
does not, because it is the same record on the same platter. The old cut is a
template needing `new = offset + scale × old`.

**Scale is not optional.** A belt running 0.1 per cent different between
sessions drifts about a second across a sixteen-minute side — audible at a
boundary. Converter crystal tolerance, around 50 ppm or 50 ms over the same
side, is noise beside it.

**Correlation locates; it does not judge.** Pearson r collapses on dense
material while the lag it picks stays correct. Measured on a wall-of-guitar side
against a known +7.30 s shift:

| window | envelope sd | best r | recovered |
|---|---|---|---|
| 0.20 s | 0.31 dB | 0.635 | +7.20 |
| 0.02 s | 0.92 dB | 1.000 | +7.30 |

The same trap sits one level down: a 0.1 per cent platter difference stretches
the envelope inside the probe itself, so r falls as the probe lengthens while
the answer stays right.

| fine probe | r | recovered a −0.162 s truth as |
|---|---|---|
| 2 s | 0.882 | −0.160 |
| 4 s | 0.840 | −0.160 |
| 10 s | 0.665 | −0.160 |

So probes are short, and r is asked one question only — is this the same music
at all — which it answers very well:

| | r |
|---|---|
| same side, self / shifted / 0.1% fast | 1.000 / 1.000 / 0.882 |
| a needle from one side against another side of the same record | 0.391 |
| the same needle against a different record | 0.379 |

`MIN_R` sits at 0.65, in the empty gap between those groups.

**The window is a count of samples, so it must come from the file.** Assuming
48 kHz halves every window on a 96 kHz capture, so a thirty-second needle is
compared against fifteen seconds of hay. It degrades rather than breaks, which
is worse: one side measured r = 0.60 that way, just under `align_min_r`, and was
refused when it should have fitted. `-ar` does not help - that is an output
option, applied after the filter graph, and the window is set inside it.

**Correctness comes from a prediction, not from r.** Any two points define a
line, including two wrong ones. Fit on two probes, then predict a third the fit
has never seen and measure where the music actually is. An internally consistent
but wrong fit fails that and is refused.

## The thresholds, and where each came from

Every number the detectors use, with its provenance. All were measured on one
signal chain; `ripdoctor measure` reports what yours does beside them.

| name | default | what it is |
|---|---:|---|
| `gap_below` | 16.0 dB | Full lane: below the music level (85th percentile) counts as quiet. Anchoring on the floor instead finds no gaps at all. |
| `gap_above` | 12.0 dB | Band lane: above that lane's own floor still counts as quiet. Using `gap_below` here found 57 gaps on a side with a handful. |
| `gap_minimum` | 1.2 s | Shorter runs are pauses inside a song, not inter-track gaps. |
| `refine_above` | 8.0 dB | Above a located gap's own floor when walking its edges out. Tighter than `gap_below` because the gap is already found; the question is only where the fade stops. |
| `span_below` | 20.0 dB | Below the music level when looking for a side's first and last sustained run. Wider than `gap_below`: this separates music from run-in groove, not from a silence between tracks. |
| `span_run` | 1.5 s | How long that run must last. A needle drop that skids into a groove makes about a second of real audio before the arm is lifted, and that accident is not the start of side one. |
| `autostop_below` | 20.0 dB | Below the side's own music level that counts as run-out. The tightest value that never trips inside any of fifteen archived sides; one of them sits 17.5 dB down for two continuous minutes and is still the song. |
| `autostop_dwell` | 120.0 s | How long that must hold. Long because the gate has only 2.5 dB of margin. |
| `autostop_max_seconds` | 2100 s | The hard cap, for a needle that never reaches the run-out. This is the guard that always works. |
| `arm_floor` | −60.0 dB | The detector does not arm below this. Cueing a needle would otherwise trip it immediately. |
| `align_min_r` | 0.65 | Correlation below this is not the same music. Sits in the empty gap between 0.88–1.00 for a side against itself and 0.38–0.39 against a different record. |
| `align_max_drift` | 0.02 | A platter differing by more than two per cent between sessions is not credible; the fit is likelier wrong than the turntable. |
