"""A tape's audio, song list, boundaries and a person's judgement of them. ADR-063."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

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
                "doubtful": TP.doubtful(s),
                "cut": TP.cut_status(s, len(tape.songs), cuts),
            }
            for s in TP.ear_order(tape)
        ]

    def asking() -> TJ.Asking:
        settings = service.settings
        beets = None
        if settings.library:
            found = IMP.choose(service.runner, settings.importer, service.state_dir)
            beets = found if isinstance(found, IMP.Beets) else None
        return TJ.Asking(
            MB.Paced(service.fetcher, service.pace),
            service.pace.sleep,
            service.runner,
            layout,
            settings.library,
            beets,
        )

    def wanted_ready(tape: TP.Tape) -> bool:
        if archived(tape):
            return False
        try:
            TJ.arrived(layout, tape)
        except ValueError:  # nothing to prepare yet, which is all this asks
            return False
        return TP.wants_preparing(tape, True)

    def view(tape: TP.Tape) -> dict[str, Any]:
        key = T.key_of(tape.id)
        upload = IN.state(layout, key)
        job = service.jobs.status(key)
        cuts = T.cuts_on_disk(layout, tape.id)
        return {
            **tape.to_dict(),
            "releases": [
                {"number": s.number, **TF.release_view(s)} for s in tape.songs
            ],
            "audio": T.audio_file(layout, tape.id).is_file(),
            "waveform": T.envelope_file(layout, tape.id).is_file(),
            "upload": None if upload is None else upload.as_dict(),
            "key": key,
            "job": None if job is None else job.as_dict(),
            "chunk": CHUNK,
            "all_settled": TP.all_settled(tape),
            "prepare_needed": wanted_ready(tape),
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

    def with_list(
        tape: TP.Tape, songs: tuple[TP.Song, ...], confirmed: bool
    ) -> H.Response:
        """A list that loses boundaries is said twice, because finding them again can
        take minutes, and any that a person set are refused however often it is said."""
        not_archived(tape)
        try:
            changed = TP.relist(tape, songs)
        except TP.EarSet as e:
            raise H.HttpError(409, str(e)) from e
        if any(s.start is not None for s in tape.songs) and not confirmed:
            return H.ok(
                {
                    "error": "this clears the boundaries found for this list, and what "
                    "you marked heard - they are found again from the audio",
                    "needs_confirm": True,
                },
                409,
            )
        return kept(changed)

    def with_edits(tape: TP.Tape, songs: tuple[TP.Song, ...]) -> H.Response:
        not_archived(tape)
        try:
            return kept(TP.edited(tape, songs))
        except TP.Locked as e:
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
        """The whole list by hand; adding, editing and reordering all send this. A row
        changed where it stands keeps its boundaries; anything else is a new list."""
        body = r.json()
        try:
            songs = TP.songs_from(body.get("songs"))
        except TP.BadList as e:
            raise H.HttpError(400, str(e)) from e
        if TP.only_edits(tape, songs):
            return with_edits(tape, songs)
        return with_list(tape, songs, body.get("confirm") is True)

    @app.route("POST", "/api/tapes/([^/]+)/list")
    @settled
    def from_csv(r: H.Request, tape: TP.Tape) -> H.Response:
        """The body is the file. It pairs with this tape because it was sent here."""
        try:
            songs = TP.parse_list(r.body.decode("utf-8"))
            return with_list(tape, songs, r.query.get("confirm") == "1")
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

    def ready(r: H.Request) -> TP.Tape:
        """The tape, or why it cannot be processed."""
        tape = listed(r)
        try:
            TJ.arrived(layout, tape)
        except ValueError as e:
            raise H.HttpError(409, str(e)) from e
        return tape

    @app.route("POST", "/api/tapes/([^/]+)/process")
    def process(r: H.Request) -> H.Response:
        """Make the audio a tape's, measure it, and lay the list over it."""
        tape = ready(r)

        def work(job: Job) -> dict[str, Any]:
            found = TJ.find_boundaries(service.runner, layout, tape.id, job)
            return found.to_dict()

        return started(tape.id, "tape", work)

    @app.route("POST", "/api/tapes/([^/]+)/prepare")
    def prepare(r: H.Request) -> H.Response:
        """All a tape needs once its audio and list are both in: one job, by phases."""
        tape = ready(r)
        not_archived(tape)
        return started(
            tape.id, "prepare", lambda job: TJ.prepare(asking(), tape.id, job)
        )

    @app.route("POST", "/api/tapes/([^/]+)/match")
    def match(r: H.Request) -> H.Response:
        """Ask about every song with no answer, or with `again` every one unsettled."""
        tape = listed(r)
        not_archived(tape)
        again = bool(r.json().get("again"))

        def work(job: Job) -> dict[str, Any]:
            return TJ.look_up(asking(), tape.id, job, again=again).to_dict()

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

    def chosen(
        tape: TP.Tape, song: TP.Song, choice: TM.Choice, confirmed: bool
    ) -> H.Response:
        """A person's choice, said twice if it is far from what the list says."""
        far = TM.off_the_list(TP.wanted(song), choice)
        if far and not confirmed:
            return H.ok({"error": far, "needs_confirm": True}, 409)
        return kept(TP.settle(tape, song.number, choice))

    @app.route("POST", r"/api/tapes/([^/]+)/songs/(\d+)/release")
    @settled
    def release(r: H.Request, tape: TP.Tape) -> H.Response:
        """One of the candidates needs no request and no track, since it carries its
        own; anything else is looked up."""
        song = choosable(tape, r)
        body = r.json()
        given = str(body.get("release", "")).strip()
        confirmed = body.get("confirm") is True
        position = _position(body)
        offered = song.proposal.candidates if song.proposal else ()
        for candidate in offered:
            if candidate.release == given and candidate.number and position is None:
                return chosen(tape, song, candidate, confirmed)
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
        return chosen(tape, song, choice, confirmed)

    @app.route("POST", r"/api/tapes/([^/]+)/songs/(\d+)/search")
    @settled
    def search(r: H.Request, tape: TP.Tape) -> H.Response:
        """A person's own words for a song. What it finds is offered and never settled,
        and is kept with the song so that choosing one takes no typing."""
        song = choosable(tape, r)
        body = r.json()
        title = str(body.get("title") or song.title).strip()
        artist = str(body.get("artist") or song.artist).strip()
        if not title or not artist:
            raise H.HttpError(400, "a search needs a title and an artist")
        asked = TM.Wanted(title, artist, song.album, song.length)
        made = song.proposal or TM.Proposal(TM.NONE)
        answer = asking().catalogue(asked, made.held)
        if answer.status == TM.FAILED:
            raise H.HttpError(503, answer.reason)
        changed = TP.offered(tape, song.number, answer.candidates)
        T.save(layout, changed)
        said = "" if answer.candidates else answer.reason
        return H.ok(
            {"options": TM.options(asked, answer), "reason": said, "song": song.number}
        )

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
        """`seen_start` and `seen_end` are the edges the sender last saw, so that a page
        left open cannot put back an edge somebody has moved since."""
        song, body = song_of(tape, r), r.json()
        start, end = _seconds(body, TP.START), _seconds(body, TP.END)
        seen = _seconds(body, "seen_start"), _seconds(body, "seen_end")

        def moved() -> TP.Tape:
            TP.still_as_seen(tape, song, seen)
            return TP.set_edges(tape, song.number, start, end)

        return judged(tape, moved)

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
