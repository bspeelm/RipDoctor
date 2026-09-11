# Working on RipDoctor

Record a vinyl side, find where the tracks begin and end, let a person check
those boundaries by ear, then cut, tag, file and archive.

## Read first, every session

`docs/north-star.md`, the ADR titles in `docs/decisions.md`, and the current
plan. The recorded refusals ride along for free — a decision already declined is
settled, and a re-proposal is answered with its ADR number rather than a fresh
argument.

The wiki is the manual for *using* it; `docs/` is why it is built this way. A
change that alters what a person does goes in both.

## Comments record intent, never history

A comment answers "why is this code shaped this way", so that changing it badly
is harder. It does not record what the code used to be. Git and
`docs/decisions.md` hold history; a source file that also holds it is a
changelog with a compiler.

The test: **would this comment still make sense to someone who had never seen
the previous version?** If it only lands as "we got this wrong once", it belongs
in a commit message.

**The same test governs user-facing prose.** A README sentence that only makes
sense to someone who saw the previous draft is history in the documentation, and
the documentation has one reader: someone arriving for the first time.

Corollaries:

- A bug fix ships with a test, and the test name carries the story. That is
  where "this broke once" belongs.
- A 30-line function does not need 40 lines of preamble. If the reasoning is
  that long, it is an ADR.
- No self-narration. Comments describe the code, not the process that produced
  it, and not the person producing it.
- Prefer deleting a comment to writing one that restates the line below it.

## Everything else

- Dependencies: `beets`, and nothing else. Adding one is a decision, not a
  convenience — `scripts/budgets.py` holds the list.
- `make check` before every commit: format, lint, types, tests, budgets. Do not
  argue with a budget; write an ADR to change it.
- Conventional commits. `main` requires a PR.
- Every user-facing claim names the test or CI job that proves it, or the claim
  goes.
- On Python 3.14 the suite is occasionally unreliable through no fault of its
  own (ADR-019). A failure whose error is impossible — an unknown opcode, a
  `NameError` for something imported at the top of the file — is the
  interpreter. Run it again. **CI on 3.11 to 3.13 is the authority.**

## The rules specific to this program

- **`core/` computes and touches nothing.** No subprocess, no filesystem, no
  clock, no network. That is what makes the whole algorithm testable without
  ffmpeg, a sound card or any audio files. `tests/test_architecture.py`
  enforces it.
- **One subprocess seam.** Every external binary goes through `Runner`
  (`audio/runner.py`), and `subprocess` is imported only under `audio/`. Tests
  drive it with `FakeRunner().expect(...)`. For an external process the argv
  *is* the behaviour — assert the flags, do not mock the program.
- **`os.environ` is read in one layer.** `config/`, and nowhere else.
- **The web layer is values.** `Request` and `Response` are frozen dataclasses,
  routes are a table, and one socket adapter is the only thing that knows about
  sockets. ADR-010.
- **The two documents are written together.** A spec and a plan are saved by one
  function so their names cannot disagree; every reader downstream trusts that.
- **A boundary a person set is never moved** by a later fit, refit or
  re-measure. The last judgement is theirs.
- **Nothing is cleared until the record is proved to be somewhere else**, and
  anything superseded is moved aside rather than deleted.
- **The comment ratio is the one hard budget.** Over it, retire prose into an
  ADR. It has never been raised and is not going to be.

## Instinct fences

Mine to self-check. Each is a failure that has actually shipped in agent-led
work, so the instinct is named before it fires.

- **Answer the ask.** Extra scope is proposed in prose, never shipped in the
  diff.
- **Fix in place.** No `_v2`/`_new`/`_old` parallel files; a replacement and the
  deletion it replaces travel in one commit.
- **Never discard an error** without a comment saying why ignoring it is
  correct.
- **Never weaken or delete a test to go green.** A failing test is information;
  report what it told you.
- **Never claim something ran** without the command and its output. `| head`
  hides an exit status — check it explicitly. Anything unverified is labelled
  unverified, including in reports about my own work.
- **Test the code, not the machine.** A test that asks whether something is
  installed passes where it was written and fails where the artifact is built.
- **Done is the consumed artefact.** A green tag is a step. The wheel installed,
  the record filed — that is finished.
- **No local detail leaves the machine.** Absolute paths, hostnames, account
  names, hardware model names, real records and real release ids never reach a
  commit message, an issue, a comment or a document. Use a generic cast, and
  prefer a project default to a value observed on a running deployment.
- **No placeholders** without a named tracking issue. Deferred work is a GitHub
  issue, never a TODO or a backlog file. The TODO count may not grow.
- **One diff, one intent.** Refactors and fixes ship separately; a move and a
  feature are two commits.
- **Give the strongest counter-argument first** on any proposal, and build only
  what survives it. Direction is settled by ADR, not by enthusiasm.
- **Polish is not proof.** The existence of docs, tests or ADRs is never offered
  as evidence of quality. Only executed checks count.
- **A standard without a gate is a wish.** Adding one means adding the command
  that disagrees with it, in the same commit.
