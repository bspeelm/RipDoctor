"""Listing capture devices, from arecord's own output.

The point of this module is that nothing here decides which device is the
turntable. Deciding that from a card name is what made the predecessor refuse
to record on any machine but the one it was written on.
"""

from __future__ import annotations

from pathlib import Path

from ripdoctor.audio import devices as V
from ripdoctor.audio.runner import FakeRunner

# Real output, including the parts that break a naive parser: names with
# spaces, several devices on one card, and a card whose id is not its number.
ARECORD_L = """**** List of CAPTURE Hardware Devices ****
card 0: PCH [HDA Intel PCH], device 0: ALC1150 Analog [ALC1150 Analog]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 0: PCH [HDA Intel PCH], device 2: ALC1150 Alt Analog [ALC1150 Alt Analog]
  Subdevices: 1/1
card 3: Digital [USB Digital Audio Rx], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
"""

HW_PARAMS = """Warning: format is changed to S24_3LE
ACCESS:  MMAP_INTERLEAVED RW_INTERLEAVED
FORMAT:  S16_LE S24_3LE
SUBFORMAT:  STD
SAMPLE_BITS: [16 32]
RATE: [32000 96000]
CHANNELS: 2
"""


def a_runner() -> FakeRunner:
    return (
        FakeRunner(installed={"arecord"})
        .expect("--dump-hw-params", stderr=HW_PARAMS.encode())
        .expect("-l", stdout=ARECORD_L.encode())
    )


def test_every_device_is_listed_including_names_with_spaces() -> None:
    found = V.parse_list(ARECORD_L)
    assert len(found) == 3
    assert "ALC1150 Alt Analog" in found[1].name


def test_devices_are_addressed_by_card_name_not_number() -> None:
    """Card numbers shuffle between boots, exactly as disk names do."""
    found = V.parse_list(ARECORD_L)
    assert found[2].id == "hw:Digital,0"
    assert all(not d.id.startswith("hw:0,") or d.id == "hw:PCH,0" for d in found)


def test_several_devices_on_one_card_are_distinguished() -> None:
    found = V.parse_list(ARECORD_L)
    assert {d.id for d in found[:2]} == {"hw:PCH,0", "hw:PCH,2"}


def test_no_device_is_declared_to_be_the_turntable() -> None:
    """ADR-009. A name-based guess is what broke portability."""
    text = Path(V.__file__).read_text()
    assert "CODEC" not in text
    assert "likely" not in text.lower()
    assert not hasattr(V.Device("hw:x,0", 0, 0, "n"), "likely_turntable")


def test_a_range_of_rates_expands_to_the_ones_worth_offering() -> None:
    rates, _ = V.parse_hw_params(HW_PARAMS)
    assert 48000 in rates and 96000 in rates
    assert 176400 not in rates, "a rate outside the device's range was offered"


def test_formats_come_back_best_first() -> None:
    _, formats = V.parse_hw_params(HW_PARAMS)
    assert formats == ("S24_3LE", "S16_LE")


def test_a_device_offering_nothing_recognisable_reports_nothing() -> None:
    rates, formats = V.parse_hw_params("no parameters here")
    assert rates == () and formats == ()
    assert V.Device("hw:x,0", 0, 0, "n").best_format == "S16_LE"


def test_hardware_parameters_are_read_from_stderr() -> None:
    """arecord reports them there and exits non-zero. That is how it works."""
    rates, formats = V.probe(a_runner(), "hw:Digital,0")
    assert 96000 in rates and "S24_3LE" in formats


def test_enumeration_probes_every_device_it_finds() -> None:
    found = V.enumerate_devices(a_runner())
    assert len(found) == 3
    assert all(d.rates and d.formats for d in found)


def test_the_report_names_what_to_do_with_the_answer() -> None:
    text = V.report(V.enumerate_devices(a_runner()))
    assert "hw:Digital,0" in text and "capture_device" in text
    assert "96000" in text and "S24_3LE" in text


def test_the_configured_device_is_marked() -> None:
    text = V.report(V.enumerate_devices(a_runner()), configured="hw:Digital,0")
    assert "* hw:Digital,0" in text
    assert "configured: hw:Digital,0" in text


def test_no_devices_is_a_sentence_not_an_empty_list() -> None:
    assert "no capture devices" in V.report([])
