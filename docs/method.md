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
