"""The job runner, with the worker and the clock passed in.

Nothing here starts a thread. The predecessor's two runners could only be
exercised by timing a real one, which is why neither was.
"""

from __future__ import annotations

import pytest

from ripdoctor.work.jobs import Busy, Job, Jobs


def now_at(t: float):  # type: ignore[no-untyped-def]
    return lambda: t


def immediate(work) -> None:  # type: ignore[no-untyped-def]
    """Run the worker where it was started, so the test can see the result."""
    work()


def a_runner(t: float = 1000.0) -> Jobs:
    return Jobs(now=now_at(t), spawn=immediate)


# ---------------------------------------------------------------- running


def test_a_job_runs_and_carries_its_result() -> None:
    jobs = a_runner()
    job = jobs.start("album", "split", lambda _j: {"tracks": 11})
    assert job.done and job.result == {"tracks": 11}


def test_progress_is_visible_while_it_runs() -> None:
    """A page with nothing on it for four minutes looks broken."""
    seen: list[tuple[str, str]] = []

    def work(job: Job) -> None:
        job.total = 2
        for i, side in enumerate(("a", "b")):
            job.step(side, "cutting")
            seen.append((job.current, job.detail))
            job.finished = i + 1

    jobs = a_runner()
    job = jobs.start("album", "split", work)
    assert seen == [("a", "cutting"), ("b", "cutting")]
    assert job.finished == 2 and job.total == 2


def test_a_finished_job_names_nothing_as_current() -> None:
    jobs = a_runner()
    job = jobs.start("album", "split", lambda j: j.step("a", "cutting"))
    assert job.current == ""


def test_the_status_of_a_record_is_pollable() -> None:
    jobs = a_runner()
    jobs.start("album", "split", lambda _j: 1)
    status = jobs.status("album")
    assert status is not None and status.op == "split"
    assert jobs.status("other") is None


# --------------------------------------------------------------- failures


def test_a_failure_is_carried_as_text_not_raised() -> None:
    """Once the work is off the request thread there is no response left to put
    a status code on. The message survives, which is the part anyone reads."""
    jobs = a_runner()
    job = jobs.start("album", "split", lambda _j: 1 / 0)
    assert job.done and "ZeroDivisionError" in job.error
    assert job.result is None


def test_a_failed_job_releases_the_record() -> None:
    jobs = a_runner()
    jobs.start("album", "split", lambda _j: 1 / 0)
    assert jobs.start("album", "import", lambda _j: "fine").result == "fine"


# ------------------------------------------------------------- the claim


def test_a_second_job_for_the_same_record_is_refused() -> None:
    """Refused while the caller is still there to be told, rather than accepted
    and silently stalled."""
    jobs = Jobs(now=now_at(1000.0), spawn=lambda _w: None)
    jobs.start("album", "split", lambda _j: None)
    with pytest.raises(Busy, match="split is already running"):
        jobs.start("album", "import", lambda _j: None)


def test_another_record_is_not_blocked() -> None:
    jobs = Jobs(now=now_at(1000.0), spawn=lambda _w: None)
    jobs.start("album", "split", lambda _j: None)
    assert jobs.start("other", "split", lambda _j: None).slug == "other"


def test_the_claim_is_dropped_before_the_job_reports_finished() -> None:
    """A client that polls, sees it done and starts the next operation must not
    arrive while this one still holds the record."""
    import inspect

    source = inspect.getsource(Jobs.start)
    ended = source.index("job.ended")
    marked = source.index("job.done = True")
    assert ended < marked, "done is set before the record is released"


# --------------------------------------------------------------- pruning


def test_a_finished_job_is_forgotten_eventually() -> None:
    """The predecessor's table held one entry per record for the life of the
    process, and went on answering with a finished job indefinitely."""
    jobs = Jobs(keep=60.0, now=now_at(1000.0), spawn=immediate)
    jobs.start("album", "split", lambda _j: 1)
    jobs.now = now_at(1000.0 + 61.0)
    assert jobs.prune() == 1
    assert jobs.status("album") is None


def test_a_recent_job_is_still_pollable() -> None:
    """Long enough that a browser which reloaded mid-operation can collect the
    result."""
    jobs = Jobs(keep=60.0, now=now_at(1000.0), spawn=immediate)
    jobs.start("album", "split", lambda _j: 1)
    jobs.now = now_at(1030.0)
    assert jobs.prune() == 0 and jobs.status("album") is not None


def test_a_running_job_is_never_pruned() -> None:
    jobs = Jobs(keep=1.0, now=now_at(1000.0), spawn=lambda _w: None)
    jobs.start("album", "split", lambda _j: None)
    jobs.now = now_at(9999.0)
    assert jobs.prune() == 0


# ---------------------------------------------------------------- output


def test_the_status_is_json_a_browser_can_poll() -> None:
    import json

    jobs = a_runner()
    job = jobs.start("album", "split", lambda _j: {"tracks": 11})
    data = json.loads(json.dumps(job.as_dict()))
    assert data["done"] and data["result"] == {"tracks": 11}
    assert data["op"] == "split" and data["error"] == ""


def test_a_skipped_item_is_reported_rather_than_failing_the_job() -> None:
    """A side still being recorded is not an error; it is a side to come back
    to, and the rest of the record still prepares."""
    jobs = a_runner()
    job = jobs.start("album", "prepare", lambda j: j.skipped.append("b"))
    assert job.as_dict()["skipped"] == ["b"] and not job.error
