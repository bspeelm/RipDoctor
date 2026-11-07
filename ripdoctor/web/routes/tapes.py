"""A tape's audio, song list, boundaries and a person's judgement of them. ADR-063."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ripdoctor.audio.runner import TimedOut, ToolFailed, ToolMissing
from ripdoctor.core import filing as TF
from ripdoctor.core import match as TM
from ripdoctor.core import playlist as PL
from ripdoctor.core import tape as TP
from ripdoctor.integrations import artwork as ART
from ripdoctor.integrations import importer as IMP
from ripdoctor.integrations import musicbrainz as MB
from ripdoctor.store import incoming as IN
from ripdoctor.store import playlists as PS
from ripdoctor.store import tapes as T
from ripdoctor.store.safety import contains
from ripdoctor.web import http as H
from ripdoctor.web.app import App
from ripdoctor.web.routes import art as RA
from ripdoctor.web.routes.records import slug_of
from ripdoctor.web.service import Service
from ripdoctor.work import tape_finish as TX
from ripdoctor.work import tape_jobs as TJ
from ripdoctor.work.jobs import Busy, Job

CHUNK = 8 * 1024 * 1024
MAX_UPLOAD = 16 * 1024 * 1024 * 1024
# Someone is waiting on the page, and a list can be sent again.
ASKING_BEETS = 20.0
ASKS_AS_IT_GOES = frozenset(("prepare", "match"))


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
            except Busy:
                return busy(slug_of(r))

        return holding

    def alongside(
        change: Callable[[H.Request, TP.Tape], H.Response],
    ) -> Callable[[H.Request], H.Response]:
        """What a person does to a row. A job a person did not press, or that only asks
        the catalogue, saves a song at a time and reads the tape again each time, so it
        is no reason to refuse anybody. Any other job still is."""

        def holding(r: H.Request) -> H.Response:
            tape = slug_of(r)
            try:
                with (
                    service.jobs.claim(T.key_of(tape), "a change", ASKS_AS_IT_GOES),
                    T.changing(tape),
                ):
                    return change(r, held(r))
            except Busy:
                return busy(tape)

        return holding

    def busy(tape: str) -> H.Response:
        """A real conflict, said as one. The page holds on to what was being saved."""
        job = service.jobs.status(T.key_of(tape))
        doing = job.op if job and not job.done else "a change"
        said = f"this tape is busy with {doing}, so nothing was changed - wait for it"
        return H.ok({"error": said, "busy": True}, 409)

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
        """Never after a job that ended in error, whatever the document managed to
        keep: it would only fail again, and until a person acts that is a loop."""
        job = service.jobs.status(T.key_of(tape.id))
        if archived(tape) or (job and job.done and job.error):
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
            "from_list": TF.from_list_view(tape),
            "list_seen": TP.list_digest(tape),
            "prepare_needed": wanted_ready(tape),
            "place_refusal": TP.place_refusal(tape),
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

    def gone_from_library(tape: TP.Tape) -> set[str]:
        """The filed paths beets has no row for now, asked in one query since this is
        the request's own thread. A failure to ask is taken as still there, which is the
        side that refuses, and running out of time is said at once. A person's boundary
        refuses the list whatever beets says, so beets is not asked."""
        library = service.settings.library
        filed = {s.path for s in tape.songs if s.filing in TF.FINISHED and s.path}
        found = IMP.choose(service.runner, service.settings.importer, service.state_dir)
        if tape.has_ear or not filed or not library or not isinstance(found, IMP.Beets):
            return set()
        try:
            return filed - found.rows_at(service.runner, library, filed, ASKING_BEETS)
        except TimedOut as e:
            raise H.HttpError(
                503,
                "the library did not answer in time, so the list was not changed - "
                "try again",
            ) from e
        except (IMP.ImportFailed, ToolMissing, ToolFailed):
            return set()

    def with_list(
        tape: TP.Tape, songs: tuple[TP.Song, ...], confirmed: bool
    ) -> H.Response:
        """A list that loses boundaries is said twice, because finding them again can
        take minutes, and any that a person set are refused however often it is said."""
        not_archived(tape)
        try:
            changed = TP.relist(tape, songs, gone_from_library(tape))
        except (TP.EarSet, TP.Locked) as e:
            raise H.HttpError(409, str(e)) from e
        service.jobs.forget(T.key_of(tape.id))
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
            changed = TP.edited(tape, songs)
            service.jobs.forget(T.key_of(tape.id))
            return kept(changed)
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
    @alongside
    def songs(r: H.Request, tape: TP.Tape) -> H.Response:
        """The whole list by hand; adding, editing and reordering all send this. A row
        changed where it stands keeps its boundaries; anything else is a new list.
        `seen` is the digest of the list the sender last had."""
        body = r.json()
        try:
            songs = TP.songs_from(body.get("songs"))
        except TP.BadList as e:
            raise H.HttpError(400, str(e)) from e
        if body.get("seen") != TP.list_digest(tape):
            said = (
                f"the list on {tape.name} has changed since this page loaded it, and "
                "nothing was saved - here it is as it stands now"
            )
            return H.ok({"error": said, "stale": True}, 409)
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
        service.jobs.forget(key)
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
        refused = TP.place_refusal(tape)
        if refused:
            raise H.HttpError(409, refused)

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
    @alongside
    def release(r: H.Request, tape: TP.Tape) -> H.Response:
        """One of the candidates needs no request and no track, since it carries its
        own, and an option names its recording, because one release can be offered with
        two. An id pasted by hand is looked up, or taken as the candidate it is."""
        song = choosable(tape, r)
        body = r.json()
        given = str(body.get("release", "")).strip()
        recording = str(body.get("recording") or "")
        confirmed = body.get("confirm") is True
        position = _position(body)
        offered = song.proposal.candidates if song.proposal else ()
        for candidate in offered:
            if (
                candidate.release == given
                and candidate.number
                and position is None
                and recording in ("", candidate.recording)
            ):
                return chosen(tape, song, candidate, confirmed)
        if recording:
            raise H.HttpError(
                409, f"{TP.label(song)} no longer offers that option - look again"
            )
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
    def search(r: H.Request) -> H.Response:
        """A person's own words for a song. What it finds is offered and never settled,
        and is kept with the song so that choosing one takes no typing. The tape is not
        held while the catalogue answers, so what comes back is for a list that may have
        changed underneath it."""
        tape = held(r)
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

        def kept_with_song(_r: H.Request, current: TP.Tape) -> H.Response:
            if TP.list_digest(current) != TP.list_digest(tape):
                said = "the list changed while that search ran, so look for it again"
                raise H.HttpError(409, said)
            T.save(layout, TP.offered(current, song.number, answer.candidates))
            said = "" if answer.candidates else answer.reason
            return H.ok(
                {
                    "options": TM.options(asked, answer),
                    "reason": said,
                    "song": song.number,
                }
            )

        return alongside(kept_with_song)(r)

    @app.route("POST", r"/api/tapes/([^/]+)/songs/(\d+)/from-list")
    @alongside
    def from_list(r: H.Request, tape: TP.Tape) -> H.Response:
        """No release: filed from the list's own values, the ADR-049 route."""
        return kept(TP.settle(tape, choosable(tape, r).number, None))

    @app.route("POST", "/api/tapes/([^/]+)/from-list")
    @alongside
    def all_from_list(r: H.Request, tape: TP.Tape) -> H.Response:
        """Every song with no match at all, in one save. `numbers` is what the page
        counted for the person, so nothing is settled that they were not told about."""
        not_archived(tape)
        asked = r.json().get("numbers")
        if not isinstance(asked, list):
            raise H.HttpError(400, "numbers must be a list of songs")
        return kept(TF.all_from_list(tape, set(asked)))

    @app.route("POST", r"/api/tapes/([^/]+)/songs/(\d+)/use-copy")
    @alongside
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

    @app.route("POST", r"/api/tapes/([^/]+)/songs/(\d+)/cover")
    @settled
    def cover(r: H.Request, tape: TP.Tape) -> H.Response:
        """The body is the image. It goes through the record flow's own install, for
        this song's album row, so one upload answers for all of that album's songs."""
        song = song_of(tape, r)
        if song.filing != TF.FILED or not song.path:
            raise H.HttpError(
                409,
                f"{TP.label(song)} was not filed by this tape, so its album's cover is "
                "not this tape's to change",
            )
        if not song.no_cover:
            raise H.HttpError(409, f"{TP.label(song)} is not missing a cover")
        if not r.body:
            raise H.HttpError(400, "no image was sent")
        importer = beets_for("a filed song's cover is set through beets")
        try:
            return given_cover(importer, tape, song, r.body)
        except TimedOut as e:
            raise H.HttpError(
                503, f"the cover was not set: {e.cmd[0]} did not answer in time"
            ) from e
        except IMP.ImportFailed as e:
            raise H.HttpError(503, str(e)) from e
        except (OSError, ToolFailed, RuntimeError) as e:
            # Some files may have taken it by now, so what they hold is asked again.
            T.save(layout, still_lacking(importer, tape, song))
            raise H.HttpError(409, f"the cover was not set: {e}") from e

    def given_cover(
        importer: IMP.Beets, tape: TP.Tape, song: TP.Song, image: bytes
    ) -> H.Response:
        library = service.settings.library
        album, files = importer.album_files(service.runner, library, song.path)
        outside = [f for f in files if not contains(library, f)]
        if outside:
            raise H.HttpError(
                409,
                f"beets lists {outside[0]} on this album, outside the library at "
                f"{library}, so no cover was set - move it in with beet move",
            )
        already = importer.art_of(service.runner, library, song.path)
        if already:
            return H.ok(
                {**view_saved(TF.cover_given(tape, files)), "cover": {"already": True}}
            )
        folder = Path(song.path).parent
        mine = set(files)
        shared = any(
            p.suffix.lower() == ".flac" and str(p) not in mine for p in folder.iterdir()
        )
        name = f"cover-{album}.jpg" if shared else ART.COVER
        done = RA.installed(service, folder, image, only=files, cover_name=name)
        if not done.embedded:
            raise H.HttpError(502, f"the cover did not embed: {'; '.join(done.failed)}")
        if done.embedded < len(files):
            kept_asking = view_saved(still_lacking(importer, tape, song))
            return H.ok({**kept_asking, "cover": done.as_dict()})
        why = importer.set_art(service.runner, album, str(done.cover))
        said = {**done.as_dict(), "note": why}
        return H.ok({**view_saved(TF.cover_given(tape, files)), "cover": said})

    def still_lacking(importer: IMP.Beets, tape: TP.Tape, song: TP.Song) -> TP.Tape:
        """Some files took a cover and some did not. The album row is given none, so the
        next upload is a whole install again, and the song it was given for keeps the
        offer, whichever of the album's files it was that failed."""
        library = service.settings.library
        TJ.covers_known(service.runner, importer, layout, tape.id, library)
        current = T.read(layout, tape.id)
        missing = {s.number for s in current.songs if s.no_cover} | {song.number}
        return TF.with_cover_known(current, missing)

    def view_saved(changed: TP.Tape) -> dict[str, Any]:
        T.save(layout, changed)
        return view(changed)

    def served(where: Path | None, what: str) -> H.Response:
        if where is None or not where.is_file():
            raise H.HttpError(404, f"{what} has not been built")
        return H.file_at(str(where))

    def with_audio(r: H.Request) -> TP.Tape:
        tape = held(r)
        if not T.audio_file(layout, tape.id).is_file():
            raise H.HttpError(409, "this tape has no audio yet - upload it first")
        return tape

    def as_seen(tape: TP.Tape, song: TP.Song, body: dict[str, Any]) -> None:
        """A verdict on a song is about the edges the sender was looking at. A caller
        sending none has none in mind, which is what a song not yet placed has."""
        if "seen_start" in body:
            seen = _seconds(body, "seen_start"), _seconds(body, "seen_end")
            TP.still_as_seen(tape, song, seen)

    def judged(tape: TP.Tape, change: Callable[[], TP.Tape]) -> H.Response:
        not_archived(tape)
        try:
            return kept(change())
        except TP.BadEdge as e:
            raise H.HttpError(409, str(e)) from e

    @app.route("POST", r"/api/tapes/([^/]+)/songs/(\d+)/edges")
    @alongside
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
    @alongside
    def heard(r: H.Request, tape: TP.Tape) -> H.Response:
        song, body = song_of(tape, r), r.json()
        now_heard = bool(body.get("heard", True))

        def accepted() -> TP.Tape:
            as_seen(tape, song, body)
            return TP.hear(tape, song.number, now_heard)

        return judged(tape, accepted)

    @app.route("POST", r"/api/tapes/([^/]+)/songs/(\d+)/skip")
    @alongside
    def skip(r: H.Request, tape: TP.Tape) -> H.Response:
        song, body = song_of(tape, r), r.json()
        skipped = bool(body.get("skip", True))

        def skipping() -> TP.Tape:
            as_seen(tape, song, body)
            return TP.skip(tape, song.number, skipped)

        return judged(tape, skipping)

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
            finished = TX.archive_if_resolved(
                _place(service), importer, tape.id, job, service.now()
            )
            return {**done, "notes": notes, "archive": finished}

        return started(tape.id, "file", work)

    @app.route("POST", "/api/tapes/([^/]+)/playlist")
    def playlist(r: H.Request) -> H.Response:
        """Ask the library about every song and write what resolves."""
        tape = listed(r)
        importer = beets_for("a playlist is asked of beets")

        def work(job: Job) -> dict[str, Any]:
            job.step(tape.name, "asking the library about every song")
            state = TX.rebuild(_place(service), importer, tape.id, service.now())
            job.step(tape.name, "asking which albums have a cover")
            library = service.settings.library
            TJ.covers_known(service.runner, importer, layout, tape.id, library)
            finished = TX.archive_if_resolved(
                _place(service), importer, tape.id, job, service.now()
            )
            return {**state.to_dict(), "archive": finished}

        return started(tape.id, "playlist", work)

    @app.route("POST", "/api/tapes/([^/]+)/archive")
    def archive(r: H.Request) -> H.Response:
        """Archive again, after a job that could not. Refused with every reason, and
        asked again of the library once running."""
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
