"""A tape's audio, song list, boundaries and a person's judgement of them. ADR-063."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ripdoctor.audio import ffprobe, split
from ripdoctor.audio import ingest as G
from ripdoctor.core import filing as TF
from ripdoctor.core import match as TM
from ripdoctor.core import playlist as PL
from ripdoctor.core import tape as TP
from ripdoctor.integrations import importer as IMP
from ripdoctor.integrations import musicbrainz as MB
from ripdoctor.store import incoming as IN
from ripdoctor.store import playlists as PS
from ripdoctor.store import tapes as T
from ripdoctor.store.safety import contains
from ripdoctor.web import http as H
from ripdoctor.web.app import App
from ripdoctor.web.routes.records import slug_of
from ripdoctor.web.service import Service
from ripdoctor.work import tape_finish as TX
from ripdoctor.work import tape_jobs as TJ
from ripdoctor.work.jobs import Busy, Job

CHUNK = 8 * 1024 * 1024
MAX_UPLOAD = 16 * 1024 * 1024 * 1024


def _size(body: dict[str, Any]) -> int:
    try:
        size = int(body["size"])
    except (KeyError, TypeError, ValueError, OverflowError) as e:
        raise H.HttpError(400, "the file's size is needed") from e
    if size <= 0:
        raise H.HttpError(400, "that file is empty")
    if size > MAX_UPLOAD:
        raise H.HttpError(413, f"that file is larger than {MAX_UPLOAD // 2**30} GB")
    return size


def _answers(service: Service, songs: list[TP.Song]) -> dict[int, TM.Proposal]:
    """One album's songs from one set of requests. Failing to ask is not "none"."""
    first = TP.wanted(songs[0])
    try:
        found = MB.editions(
            MB.Paced(service.fetcher, service.pace),
            TM.search_artists(first.artist),
            TM.search_album(first.album),
            sleep=service.pace.sleep,
        )
    except MB.LookupFailed as e:
        failed = TM.Proposal(TM.FAILED, f"{e} - look again to retry")
        return {s.number: failed for s in songs}
    editions = [MB.edition_of(r) for r in found]
    return {s.number: TM.propose(TP.wanted(s), editions) for s in songs}


def _position(body: dict[str, Any]) -> int | None:
    value = body.get("track")
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as e:
        raise H.HttpError(400, f"track {value!r} is not a whole number") from e


def _seconds(body: dict[str, Any], edge: str) -> float:
    value = body.get(edge)
    try:
        if isinstance(value, bool):
            raise TypeError
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as e:
        raise H.HttpError(400, f"{edge} has to be a number of seconds") from e


def _place(service: Service) -> TX.Place:
    s = service.settings
    return TX.Place(service.runner, service.layout, s.library, s.file_mode, s.dir_mode)


def after_import(service: Service) -> list[str]:
    """Notes from rebuilding every tape's playlist, after a finished album import.

    Failure is caught because the album is already filed and its result is already
    true: nothing here may change that, and a playlist can be rebuilt from its page.
    """
    try:
        if not TX.any_known(service.layout):
            return []
        settings = service.settings
        importer = IMP.choose(service.runner, settings.importer, service.state_dir)
        if not isinstance(importer, IMP.Beets):
            return ["playlists were not rebuilt: that takes beets"]
        return TX.rebuild_all(_place(service), importer, service.jobs, service.now())
    except Exception as e:
        return [f"playlists were not rebuilt: {e}"]


def add(app: App, service: Service) -> None:
    layout = service.layout

    def held(r: H.Request) -> TP.Tape:
        try:
            return T.read(layout, slug_of(r))
        except FileNotFoundError as e:
            raise H.HttpError(404, "no such tape") from e

    def listed(r: H.Request) -> TP.Tape:
        tape = held(r)
        if not tape.songs:
            raise H.HttpError(409, "this tape has no song list yet - add one first")
        return tape

    def settled(
        change: Callable[[H.Request, TP.Tape], H.Response],
    ) -> Callable[[H.Request], H.Response]:
        """Hold the tape through a change, so no job starts halfway through it."""

        def holding(r: H.Request) -> H.Response:
            try:
                with service.jobs.claim(T.key_of(slug_of(r)), "a change"):
                    return change(r, held(r))
            except Busy as e:
                raise H.HttpError(409, "this tape is busy - wait for it") from e

        return holding

    def started(tape: str, op: str, work: Callable[[Job], Any]) -> H.Response:
        try:
            return H.ok(service.jobs.start(T.key_of(tape), op, work).as_dict(), 202)
        except Busy as e:
            raise H.HttpError(409, str(e)) from e

    def ear_rows(tape: TP.Tape) -> list[dict[str, Any]]:
        """The songs with what is waiting on a person first."""
        cuts = T.cuts_on_disk(layout, tape.id)
        return [
            {
                "number": s.number,
                "doubtful": TP.doubtful(tape, s),
                "cut": TP.cut_status(s, len(tape.songs), cuts),
            }
            for s in TP.ear_order(tape)
        ]

    def view(tape: TP.Tape) -> dict[str, Any]:
        key = T.key_of(tape.id)
        upload = IN.state(layout, key)
        job = service.jobs.status(key)
        cuts = T.cuts_on_disk(layout, tape.id)
        return {
            **tape.to_dict(),
            "audio": T.audio_file(layout, tape.id).is_file(),
            "waveform": T.envelope_file(layout, tape.id).is_file(),
            "upload": None if upload is None else upload.as_dict(),
            "key": key,
            "job": None if job is None else job.as_dict(),
            "chunk": CHUNK,
            "all_settled": TP.all_settled(tape),
            "ear": ear_rows(tape),
            "cut_refusal": TP.cut_refusal(tape),
            "all_cut": TP.all_cut(tape, cuts),
            "file_refusal": TF.file_refusal(tape, cuts),
            "all_filed": TF.all_filed(tape),
            "filing": TF.outcomes(tape),
            "playlist": playlist_view(tape),
        }

    def archived(tape: TP.Tape) -> bool:
        audio = T.audio_file(layout, tape.id).is_file()
        return PL.archived(PS.read(layout, tape.id), audio)

    def not_archived(tape: TP.Tape) -> None:
        if archived(tape):
            raise H.HttpError(409, f"{tape.name} is archived, and stays as it is")

    def playlist_view(tape: TP.Tape) -> dict[str, Any]:
        state = PS.read(layout, tape.id)
        library = service.settings.library
        return {
            **state.to_dict(),
            "path": str(PS.path_of(library, state.file)) if state.file else "",
            "done": archived(tape),
            "archive_refusal": TX.refusal(_place(service), tape, state),
        }

    def beets_for(what: str) -> IMP.Beets:
        settings = service.settings
        if not settings.library:
            raise H.HttpError(409, "no library directory is configured")
        importer = IMP.choose(service.runner, settings.importer, service.state_dir)
        if not isinstance(importer, IMP.Beets):
            raise H.HttpError(409, what)
        return importer

    def kept(changed: TP.Tape) -> H.Response:
        T.save(layout, changed)
        return H.ok(view(changed))

    def with_songs(tape: TP.Tape, songs: tuple[TP.Song, ...]) -> H.Response:
        not_archived(tape)
        try:
            return kept(TP.relist(tape, songs))
        except TP.EarSet as e:
            raise H.HttpError(409, str(e)) from e

    @app.route("GET", "/api/tapes")
    def tapes(_r: H.Request) -> H.Response:
        found = T.listed(layout)
        return H.ok(
            {
                "tapes": [
                    {
                        "id": t.id,
                        "name": t.name,
                        "songs": len(t.songs),
                        "mismatch": bool(t.mismatch),
                        **({"done": True} if archived(t) else {}),
                    }
                    for t in found.tapes
                ],
                "unreadable": list(found.unreadable),
            }
        )

    @app.route("POST", "/api/tapes")
    def create(r: H.Request) -> H.Response:
        try:
            return H.ok(
                view(T.create(layout, str(r.json().get("name", "")), service.now())),
                201,
            )
        except TP.BadList as e:
            raise H.HttpError(400, str(e)) from e

    @app.route("GET", "/api/tapes/([^/]+)")
    def one(r: H.Request) -> H.Response:
        return H.ok(view(held(r)))

    @app.route("POST", "/api/tapes/([^/]+)/songs")
    @settled
    def songs(r: H.Request, tape: TP.Tape) -> H.Response:
        """The whole list by hand; adding, editing and reordering all send this."""
        try:
            return with_songs(tape, TP.songs_from(r.json().get("songs")))
        except TP.BadList as e:
            raise H.HttpError(400, str(e)) from e

    @app.route("POST", "/api/tapes/([^/]+)/list")
    @settled
    def from_csv(r: H.Request, tape: TP.Tape) -> H.Response:
        """The body is the file. It pairs with this tape because it was sent here."""
        try:
            return with_songs(tape, TP.parse_list(r.body.decode("utf-8")))
        except UnicodeDecodeError as e:
            raise H.HttpError(400, "that list is not UTF-8 - save it as UTF-8") from e
        except TP.BadList as e:
            raise H.HttpError(400, str(e)) from e

    @app.route("POST", "/api/tapes/([^/]+)/upload/begin")
    @settled
    def begin(r: H.Request, tape: TP.Tape) -> H.Response:
        not_archived(tape)
        if T.audio_file(layout, tape.id).is_file():
            raise H.HttpError(
                409,
                f"{tape.name} already has its audio - remove the tape to replace it",
            )
        body = r.json()
        size = _size(body)
        name = str(body.get("name", ""))
        key = T.key_of(tape.id)
        here = IN.state(layout, key)
        if here is not None and here.name == name and here.total == size:
            return H.ok({"have": here.have, "total": size, "chunk": CHUNK})
        if here is not None and not body.get("replace"):
            raise H.HttpError(
                409,
                f"{here.name} is already part-way here ({here.have} of "
                f"{here.total} bytes) - resume it, or say replace",
            )
        if not IN.room_for(layout, size):
            raise H.HttpError(507, "not enough room in the pool for that file")
        T.sweep(layout)
        IN.begin(layout, key, name=name, size=size, now=service.now())
        return H.ok({"have": 0, "total": size, "chunk": CHUNK})

    @app.route("POST", "/api/tapes/([^/]+)/upload/chunk")
    @settled
    def chunk(r: H.Request, tape: TP.Tape) -> H.Response:
        key = T.key_of(tape.id)
        here = IN.state(layout, key)
        if here is None:
            raise H.HttpError(409, "no upload is in progress for this tape")
        if not r.body:
            raise H.HttpError(400, "no data was sent")
        if len(r.body) > CHUNK:
            raise H.HttpError(413, f"a piece may be at most {CHUNK} bytes")
        try:
            at = int(r.query.get("at", ""))
        except ValueError as e:
            raise H.HttpError(400, "an offset is needed") from e
        if at + len(r.body) > here.total:
            raise H.HttpError(409, "that piece runs past the end of the file")
        try:
            have = IN.append(layout, key, at, r.body)
        except IN.Mismatch as e:
            raise H.HttpError(409, f"the upload is at {e.have}, not {at}") from e
        return H.ok({"have": have, "total": here.total})

    @app.route("POST", "/api/tapes/([^/]+)/upload/cancel")
    @settled
    def cancel(r: H.Request, tape: TP.Tape) -> H.Response:
        return H.ok({"freed_bytes": IN.clear(layout, T.key_of(tape.id))})

    def ready(r: H.Request) -> tuple[TP.Tape, IN.Incoming | None]:
        """The tape and what has arrived for it, or why it cannot be processed."""
        tape = listed(r)
        arrived = IN.state(layout, T.key_of(tape.id))
        if not T.audio_file(layout, tape.id).is_file():
            if arrived is None:
                raise H.HttpError(409, "this tape has no audio yet - upload it first")
            if arrived.have != arrived.total:
                raise H.HttpError(
                    409,
                    f"only {arrived.have} of {arrived.total} bytes arrived "
                    "- send the rest",
                )
        return tape, arrived

    @app.route("POST", "/api/tapes/([^/]+)/process")
    def process(r: H.Request) -> H.Response:
        """Make the audio a tape's, measure it, and lay the list over it."""
        tape, _ = ready(r)
        key = T.key_of(tape.id)

        def take_in(job: Job, source: str) -> None:
            part, done = IN.part_file(layout, key), IN.done_file(layout, key)
            job.step(source, "reading what arrived")
            try:
                G.probe(service.runner, str(part))
            except G.NotAudio as e:
                raise ValueError(str(e)) from e
            job.finished = 1
            job.step(source, "normalising")
            G.normalise(service.runner, str(part), str(done), timeout=T.LONG_RUN)
            job.finished = 2
            job.step(source, "checking it decodes")
            if not split.verify(service.runner, str(done), timeout=T.LONG_RUN):
                done.unlink(missing_ok=True)
                raise ValueError("the encoded tape would not decode - nothing placed")
            T.take_audio(layout, tape.id)

        def work(job: Job) -> dict[str, Any]:
            job.total = 4
            # Read again: only now does the job hold the tape against a change.
            current, arrived = ready(r)
            if not T.audio_file(layout, tape.id).is_file() and arrived is not None:
                take_in(job, arrived.name)
                current = replace(current, source=arrived.name)
                T.save(layout, current)
            if arrived is not None:
                IN.clear(layout, key)
            job.finished = 3

            audio = str(T.audio_file(layout, tape.id))
            job.step(tape.name, "measuring")
            seconds = ffprobe.true_duration(service.runner, audio, timeout=T.LONG_RUN)
            if seconds <= 0:
                raise ValueError("the tape has no playable audio in it")
            lane = T.measure(service.runner, layout, tape.id, seconds)

            job.step(tape.name, "placing boundaries")
            current = TP.place(replace(current, duration=seconds), TP.gaps_of(lane))
            T.save(layout, current)
            job.finished = 4
            return current.to_dict()

        return started(tape.id, "tape", work)

    @app.route("POST", "/api/tapes/([^/]+)/match")
    def match(r: H.Request) -> H.Response:
        """Ask about every song with no answer, or with `again` every one unsettled."""
        tape = listed(r)
        not_archived(tape)
        again = bool(r.json().get("again"))

        def work(job: Job) -> dict[str, Any]:
            current = T.read(layout, tape.id)
            waiting = TP.to_match(current, again=again)
            albums: dict[tuple[str, str], list[TP.Song]] = {}
            for song in waiting:
                albums.setdefault(TM.lookup_key(TP.wanted(song)), []).append(song)
            job.total = len(waiting)
            for songs in albums.values():
                job.step(tape.name, f"asking about {songs[0].album or songs[0].title}")
                current = TP.with_proposals(current, _answers(service, songs))
                T.save(layout, current)
                job.finished += len(songs)
            return current.to_dict()

        return started(tape.id, "match", work)

    def song_named(tape: TP.Tape, number: object) -> TP.Song:
        for song in tape.songs:
            if str(song.number) == str(number):
                return song
        raise H.HttpError(404, f"{tape.name} has no such song")

    def song_of(tape: TP.Tape, r: H.Request) -> TP.Song:
        return song_named(tape, r.params[1])

    def choosable(tape: TP.Tape, r: H.Request) -> TP.Song:
        not_archived(tape)
        song = song_of(tape, r)
        refused = TF.choice_refusal(song)
        if refused:
            raise H.HttpError(409, refused)
        return song

    @app.route("POST", r"/api/tapes/([^/]+)/songs/(\d+)/release")
    @settled
    def release(r: H.Request, tape: TP.Tape) -> H.Response:
        """One of the candidates needs no request; anything else is looked up."""
        song = choosable(tape, r)
        body = r.json()
        given = str(body.get("release", "")).strip()
        position = _position(body)
        offered = song.proposal.candidates if song.proposal else ()
        for candidate in offered:
            if candidate.release == given and candidate.number and position is None:
                return kept(TP.settle(tape, song.number, candidate))
        try:
            found = MB.release_by_id(
                MB.Paced(service.fetcher, service.pace),
                TM.release_id(given),
                sleep=service.pace.sleep,
            )
            choice = TM.track_on(TP.wanted(song), MB.edition_of(found), position)
        except TM.NoTrack as e:
            raise H.HttpError(409, str(e)) from e
        except ValueError as e:
            raise H.HttpError(400, str(e)) from e
        except MB.LookupFailed as e:
            raise H.HttpError(503, str(e)) from e
        return kept(TP.settle(tape, song.number, choice))

    @app.route("POST", r"/api/tapes/([^/]+)/songs/(\d+)/from-list")
    @settled
    def from_list(r: H.Request, tape: TP.Tape) -> H.Response:
        """No release: filed from the list's own values, the ADR-049 route."""
        return kept(TP.settle(tape, choosable(tape, r).number, None))

    @app.route("POST", r"/api/tapes/([^/]+)/songs/(\d+)/use-copy")
    @settled
    def use_copy(r: H.Request, tape: TP.Tape) -> H.Response:
        """A person says the row the pre-check could not vouch for is this song. The
        path is sent back so a page left open can only speak for the row it showed."""
        not_archived(tape)
        song, given = song_of(tape, r), str(r.json().get("path", ""))
        if not song.unconfirmed or given != song.unconfirmed:
            raise H.HttpError(
                409, f"song {song.number} is not waiting on that copy - file again"
            )
        library = service.settings.library
        if not (contains(library, given) and Path(given).is_file()):
            raise H.HttpError(
                409, f"{given} is not a file in the library any more - file again"
            )
        return kept(TF.person_owns(tape, song.number))

    def served(where: Path | None, what: str) -> H.Response:
        if where is None or not where.is_file():
            raise H.HttpError(404, f"{what} has not been built")
        return H.file_at(str(where))

    def with_audio(r: H.Request) -> TP.Tape:
        tape = held(r)
        if not T.audio_file(layout, tape.id).is_file():
            raise H.HttpError(409, "this tape has no audio yet - upload it first")
        return tape

    def judged(tape: TP.Tape, change: Callable[[], TP.Tape]) -> H.Response:
        not_archived(tape)
        try:
            return kept(change())
        except TP.BadEdge as e:
            raise H.HttpError(409, str(e)) from e

    @app.route("POST", r"/api/tapes/([^/]+)/songs/(\d+)/edges")
    @settled
    def edges(r: H.Request, tape: TP.Tape) -> H.Response:
        song, body = song_of(tape, r), r.json()
        start, end = _seconds(body, TP.START), _seconds(body, TP.END)
        return judged(tape, lambda: TP.set_edges(tape, song.number, start, end))

    @app.route("POST", r"/api/tapes/([^/]+)/songs/(\d+)/heard")
    @settled
    def heard(r: H.Request, tape: TP.Tape) -> H.Response:
        song, now_heard = song_of(tape, r), bool(r.json().get("heard", True))
        return judged(tape, lambda: TP.hear(tape, song.number, now_heard))

    @app.route("POST", r"/api/tapes/([^/]+)/songs/(\d+)/skip")
    @settled
    def skip(r: H.Request, tape: TP.Tape) -> H.Response:
        song, skipped = song_of(tape, r), bool(r.json().get("skip", True))
        return judged(tape, lambda: TP.skip(tape, song.number, skipped))

    @app.route("GET", "/api/tapes/([^/]+)/audio")
    def audio(r: H.Request) -> H.Response:
        return H.file_at(str(T.audio_file(layout, with_audio(r).id)))

    @app.route("GET", "/api/tapes/([^/]+)/envelope")
    def envelope(r: H.Request) -> H.Response:
        """Kept from processing, because measuring a long tape takes minutes."""
        return served(T.envelope_file(layout, held(r).id), "the waveform")

    @app.route("POST", "/api/tapes/([^/]+)/cut")
    def cut(r: H.Request) -> H.Response:
        """Refused with every reason while anything is still in doubt."""
        tape = with_audio(r)
        refusal = TP.cut_refusal(tape)
        if refusal:
            raise H.HttpError(409, refusal)

        def work(job: Job) -> dict[str, Any]:
            return TJ.cut(service.runner, layout, tape.id, job, service.now())

        return started(tape.id, "cut", work)

    @app.route("POST", "/api/tapes/([^/]+)/file")
    def file_songs(r: H.Request) -> H.Response:
        """Refused with every reason until the tape is cut and every song settled."""
        tape = held(r)
        refusal = TF.file_refusal(tape, T.cuts_on_disk(layout, tape.id))
        if refusal:
            raise H.HttpError(409, refusal)
        importer = beets_for("filing a tape needs beets, to join albums")
        settings = service.settings

        def work(job: Job) -> dict[str, Any]:
            try:
                done = TJ.file_songs(
                    service.runner,
                    importer,
                    layout,
                    tape.id,
                    job,
                    service.now(),
                    library=settings.library,
                    file_mode=settings.file_mode,
                    dir_mode=settings.dir_mode,
                )
            finally:
                notes = TX.after_filing(
                    _place(service), importer, service.jobs, tape.id, service.now()
                )
                # Set here because a raise leaves no return value for the job to keep.
                job.result = {"notes": notes}
            return {**done, "notes": notes}

        return started(tape.id, "file", work)

    @app.route("POST", "/api/tapes/([^/]+)/playlist")
    def playlist(r: H.Request) -> H.Response:
        """Ask the library about every song and write what resolves."""
        tape = listed(r)
        importer = beets_for("a playlist is asked of beets")

        def work(job: Job) -> dict[str, Any]:
            job.step(tape.name, "asking the library about every song")
            state = TX.rebuild(_place(service), importer, tape.id, service.now())
            return state.to_dict()

        return started(tape.id, "playlist", work)

    @app.route("POST", "/api/tapes/([^/]+)/archive")
    def archive(r: H.Request) -> H.Response:
        """Refused with every reason, and asked again of the library once running."""
        tape = held(r)
        refusal = TX.refusal(_place(service), tape, PS.read(layout, tape.id))
        if refusal:
            raise H.HttpError(409, refusal)
        importer = beets_for("archiving a tape asks beets where its songs are")

        def work(job: Job) -> dict[str, Any]:
            return TX.archive(_place(service), importer, tape.id, job, service.now())

        return started(tape.id, "archive", work)

    @app.route("POST", "/api/tapes/([^/]+)/remove")
    @settled
    def remove(r: H.Request, tape: TP.Tape) -> H.Response:
        moved = T.remove(layout, tape.id, service.now())
        where = moved.relative_to(layout.root.resolve())
        return H.ok({"removed": tape.id, "moved_to": str(where)})
