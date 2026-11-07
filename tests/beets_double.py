"""A beets that keeps rows, for what a scripted reply cannot show: a replace is judged
on how the library changes across its commands. It answers by the format asked for."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ripdoctor.audio.runner import FakeRunner, Result

SEP = "\x1f"


@dataclass
class Rec:
    id: str
    album_id: str
    path: Path
    albumartist: str = "A Band"
    album: str = "A Record"
    mb_albumid: str = ""
    artpath: str = ""
    title: str = "T"

    def value(self, key: str) -> str:
        known = {
            "artist": self.albumartist,
            "length": "3:00",
            "mb_trackid": "",
            "track": "1",
        }
        return known[key] if key in known else str(getattr(self, key))


class Library(FakeRunner):
    """Rows as a list. `takes` is how many of the waiting files an import files, and
    `fails` makes it file none and exit badly."""

    def __init__(
        self,
        root: Path,
        waiting: Path,
        *,
        who: str = "A Band",
        what: str = "A Record",
        takes: int | None = None,
        lands: str = "{what} [2]",
        art: bytes = b"",
        **kw: object,
    ) -> None:
        kw.setdefault("installed", {"beet", "metaflac"})
        super().__init__(**kw)  # type: ignore[arg-type]
        self.root, self.waiting = root, waiting
        self.who, self.what, self.takes, self.lands = who, what, takes, lands
        self.art = art
        self.recs: list[Rec] = []
        self.next = 1

    def add(self, path: Path, *, album_id: str = "1", **kw: str) -> Rec:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(b"fLaC old")
        rec = Rec(str(self.next), album_id, path, **kw)  # type: ignore[arg-type]
        self.next += 1
        self.recs.append(rec)
        return rec

    def run(self, argv, *, stdin=None, timeout=None):  # type: ignore[no-untyped-def]
        reply = super().run(argv, stdin=stdin, timeout=timeout)
        args = [str(a) for a in argv]
        if args[0] != "beet":
            return reply
        if "--config" in args:
            del args[args.index("--config") : args.index("--config") + 2]
        verb = args[1]
        if verb == "ls":
            return self._ls(args, reply)
        if verb == "remove":
            ids = {t[3:] for t in args if t.startswith("id:")}
            paths = {t[5:] for t in args if t.startswith("path:")}
            self.recs = [
                r for r in self.recs if r.id not in ids and str(r.path) not in paths
            ]
        looking = any("preview" in a for a in argv)
        if verb == "import" and reply.returncode == 0 and not looking:
            self._import(args)
        if verb == "move":
            self._move(args)
        return reply

    def _ls(self, args: list[str], reply: Result) -> Result:
        if reply.returncode or reply.stdout:
            return reply
        fmt = args[args.index("-f") + 1]
        keys = re.findall(r"\$(\w+)", fmt)
        rows = self.recs
        for term in args[args.index("-f") + 2 :]:
            if term.startswith("path:"):
                rows = [r for r in rows if str(r.path) == term[5:]]
            if term.startswith("id:"):
                rows = [r for r in rows if r.id == term[3:]]
        if "-a" in args:
            seen = {r.album_id: r for r in rows}
            lines = [
                SEP.join(a if key == "id" else r.value(key) for key in keys)
                for a, r in seen.items()
            ]
        else:
            lines = [SEP.join(r.value(key) for key in keys) for r in rows]
        return Result(reply.argv, 0, "\n".join(lines).encode(), b"")

    def _import(self, args: list[str]) -> None:
        release = args[args.index("--search-id") + 1] if "--search-id" in args else ""
        taken = sorted(self.waiting.glob("*.flac"))
        if self.takes is not None:
            taken = taken[: self.takes]
        folder = self.root / self.who / self.lands.format(what=self.what)
        album = str(max((int(r.album_id) for r in self.recs), default=0) + 1)
        for f in taken:
            folder.mkdir(parents=True, exist_ok=True)
            f.rename(folder / f.name)
            self.add(
                folder / f.name,
                album_id=album,
                albumartist=self.who,
                album=self.what,
                mb_albumid=release,
            )
        if self.art and taken:
            cover = _free(folder / "cover.jpg")
            cover.write_bytes(self.art)
            for r in self.recs:
                if r.album_id == album:
                    r.artpath = str(cover)

    def _move(self, args: list[str]) -> None:
        album = next(t[3:] for t in args if t.startswith("id:"))
        for r in self.recs:
            if r.album_id != album:
                continue
            target = self.root / r.albumartist / r.album / r.path.name
            if target != r.path and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                was = r.path.parent
                r.path.rename(target)
                r.path = target
                if not any(was.iterdir()):
                    was.rmdir()


def _free(path: Path) -> Path:
    n = 0
    target = path
    while target.exists():
        n += 1
        target = path.with_name(f"{path.stem}.{n}{path.suffix}")
    return target
