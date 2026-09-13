# North star

*Put the cut where it belongs, and be answerable for where that is.*

## The invariant

Five verbs, and the whole project is in service of them:

```
get a side  →  find the boundaries  →  check them by ear  →  cut, tag, file  →  archive
```

Get one side of audio. Find where the tracks begin and end. Listen to the ones
that are in doubt. Cut, tag and file them. Put the raw audio away once the
tracks are provably in the library. Everything else is a comfort, and comforts
are argued for in `docs/decisions.md` before they are built.

**The middle verb is the one that matters.** Recording, cutting, tagging and
filing are all solved problems with several good tools each. Deciding where a
track ends on a sparse pressing, and being able to *hear* whether that decision
was right, is the part this project exists for. Anything that makes that
judgement easier is in scope; anything that only moves audio around is in scope
only insofar as it feeds that judgement.

## Who it is for

Someone with a turntable, a library they care about, and enough records that
doing this by hand in an audio editor has stopped being enjoyable. They are
willing to listen to a boundary to settle it. They are not looking for something
that files an album without being watched.

That is a narrow audience on purpose, and the narrowness is what makes the
refusals cheap to keep.

## What "good" means here

Every boundary is either measured, checked by ear, or visibly flagged as
neither.

Concretely, and each of these is a test rather than an aspiration:

- **The last judgement is the person's.** The tool measures, proposes and
  explains; it never decides a boundary is right because a number was close
  enough. A boundary set by hand is never moved by anything afterwards.
- **Honest when broken.** Every failure line carries the command, the setting or
  the next step that fixes it. "Something went wrong" is a bug.
- **Nothing is destroyed on a guess.** The raw sides outlive the tracks cut from
  them. Nothing is cleared until the record is proved to be somewhere else, and
  anything superseded is moved aside rather than deleted.
- **A claim has a check.** Every promise in the README or the wiki names the
  test or command that proves it, or the promise goes.
- **The measurements are this chain's, not universal truth.** The shipped
  thresholds came from one signal chain and say so, and `ripdoctor measure`
  compares yours against them.

## What would mean this failed

Not "it has few users" — the audience is small by construction. These:

- A cut lands on the music and nothing in the interface suggested it might.
- Raw audio is cleared and the tracks turn out not to have arrived.
- Someone reads the code and cannot tell why a decision was made, because it
  was made silently.
- The tool becomes something you set running and trust, and the ear check
  becomes a thing nobody does.

## Where audio comes from

The first verb is *get a side*, not *record a side*. Recording is the path this
was built for and the one with the most care in it — the live meter, the
band-limited cueing zones, the run-out auto-stop — and it stays the primary
path.

But the invariant does not depend on it. A side that arrives already digital
needs its boundaries found and checked exactly as a recorded one does, and every
verb after the first is identical. So bringing in a file is in scope, and it is
in scope precisely because it changes nothing downstream: it produces the same
side the recorder produces, and stops.

What that does **not** license is the tool becoming a general audio importer.
The test is the invariant: if a proposed intake produces a side that the middle
three verbs treat like any other, it belongs; if it needs its own notion of what
a side is, or its own cutting path, it does not.

Live monitoring stays a thing this does that a tool which only accepts uploads
cannot. That was true when it was written and it is still true; it was never an
argument against also accepting one.

## The scope fence

`docs/decisions.md` holds the refusals, and they are as binding as the features.
Things enter this program through a decision record, never through a pull
request that quietly grows what it is.

Standing refusals worth naming here, each with its record: no restoration or
declicking, no library management beyond writing files into a directory, no
assistant, and no fully automatic mode.
