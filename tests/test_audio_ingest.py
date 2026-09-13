"""Bringing an outside file to the shape a side has.

For an external process the argv is the behaviour, so these assert the flags
rather than the return value.
"""

from __future__ import annotations

import json

import pytest

from ripdoctor.audio import ingest as I
from ripdoctor.audio.runner import FakeRunner, ToolFailed


def a_stream(**over: object) -> bytes:
    stream = {"codec_name": "flac", "channels": 2, "sample_rate": "48000"}
    return json.dumps({"streams": [{**stream, **over}]}).encode()


def probing(**over: object) -> FakeRunner:
    return FakeRunner().expect("ffprobe", stdout=a_stream(**over))


# ------------------------------------------------------------------ probing


def test_a_file_reports_what_it_is() -> None:
    found = I.probe(probing(), "/in/album.flac")
    assert found.codec == "flac" and found.channels == 2 and found.rate == 48000


def test_only_the_first_audio_stream_is_asked_about() -> None:
    """A download carries a cover, and often more than one audio stream."""
    argv = I.probe_argv("/in/album.m4a")
    assert "-select_streams" in argv and "a:0" in argv


def test_a_file_with_no_audio_is_refused() -> None:
    fake = FakeRunner().expect("ffprobe", stdout=b'{"streams": []}')
    with pytest.raises(I.NotAudio, match="no audio"):
        I.probe(fake, "/in/cover.jpg")


def test_output_that_is_not_json_is_refused_rather_than_raised_through() -> None:
    fake = FakeRunner().expect("ffprobe", stdout=b"not json at all")
    with pytest.raises(I.NotAudio):
        I.probe(fake, "/in/album.flac")


def test_more_than_two_channels_is_refused_with_the_count() -> None:
    """Six channels becomes a six-channel track in the library, and finding
    that out inside the importer is worse than finding it out here."""
    with pytest.raises(I.NotAudio, match="6 channels"):
        I.probe(probing(channels=6), "/in/album.ac3")


def test_a_file_reporting_no_channels_is_refused() -> None:
    with pytest.raises(I.NotAudio, match="no channels"):
        I.probe(probing(channels=0), "/in/album.flac")


def test_mono_is_allowed() -> None:
    """Narrower than a side is a real record, not a broken file."""
    assert I.probe(probing(channels=1), "/in/album.flac").channels == 1


# ------------------------------------------------------------- normalising


def test_the_cover_cannot_become_a_stream() -> None:
    """Without this the placed side's duration probes as 0.000, and the fault
    surfaces three steps from its cause."""
    argv = I.normalise_argv("/in/album.mp3", "/out/side-a.flac")
    assert "-map" in argv and "0:a:0" in argv


def test_the_tags_a_download_arrived_with_are_dropped() -> None:
    """They are what the first pass and the importer are about to decide, and
    a recorded side carries none - so dropping them makes the two the same."""
    argv = I.normalise_argv("/in/album.mp3", "/out/side-a.flac")
    assert "-map_metadata" in argv and "-1" in argv


def test_nothing_is_resampled_or_remixed() -> None:
    """The rate is read rather than assumed, and the envelope windows in
    samples, so a conversion here would be lossy for nothing."""
    argv = I.normalise_argv("/in/album.flac", "/out/side-a.flac")
    assert "-ar" not in argv and "-ac" not in argv


def test_it_encodes_flac_the_way_every_other_path_does() -> None:
    argv = I.normalise_argv("/in/album.wav", "/out/side-a.flac")
    assert "-c:a" in argv and "flac" in argv and "-compression_level" in argv


def test_an_encode_that_fails_carries_what_the_encoder_said() -> None:
    fake = FakeRunner().expect("ffmpeg", stderr=b"Invalid data found", returncode=1)
    with pytest.raises(ToolFailed, match="Invalid data"):
        I.normalise(fake, "/in/album.mp3", "/out/side-a.flac")


def test_a_flac_is_re_encoded_rather_than_passed_through() -> None:
    """A thing named .flac may be FLAC in another container, or carrying a
    picture. One pass makes it the same as everything else."""
    fake = FakeRunner().expect("ffmpeg")
    I.normalise(fake, "/in/album.flac", "/out/side-a.flac")
    assert fake.argv_for("compression_level")
