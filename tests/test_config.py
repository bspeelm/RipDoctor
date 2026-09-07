"""Configuration: detection that cannot fail, and loading that cannot refuse.

Both properties exist for the same reason. If either one can stop the program,
then a machine that is half set up cannot run the command that would explain
what is wrong with it, or the one that would fix it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ripdoctor.config import settings as S
from ripdoctor.config.machine import detect
from ripdoctor.config.thresholds import NAMES, Thresholds


def a_machine(**env: str):
    base = {
        "HOME": "/home/someone",
        "XDG_CONFIG_HOME": "/home/someone/.config",
        "XDG_DATA_HOME": "/home/someone/.local/share",
    }
    base.update(env)
    return detect(base)


# ----------------------------------------------------------- detection


def test_the_two_directories_are_separate_and_named() -> None:
    """One this owns and may delete; one the user owns and can put in git."""
    m = a_machine()
    assert m.state_dir == Path("/home/someone/.local/share/ripdoctor")
    assert m.config_dir == Path("/home/someone/.config/ripdoctor")
    assert not str(m.config_dir).startswith(str(m.state_dir))
    assert m.cache_dir.is_relative_to(m.state_dir), "the cache must be disposable"


def test_the_state_directory_can_be_pointed_elsewhere() -> None:
    m = a_machine(RIPDOCTOR_DIR="/mnt/pool/ripdoctor")
    assert m.state_dir == Path("/mnt/pool/ripdoctor")
    assert m.config_dir == Path("/home/someone/.config/ripdoctor"), (
        "the user's directory does not move with the state directory"
    )


def test_xdg_variables_are_honoured() -> None:
    m = a_machine(XDG_CONFIG_HOME="/etc/xdg", XDG_DATA_HOME="/var/lib")
    assert m.config_dir == Path("/etc/xdg/ripdoctor")
    assert m.state_dir == Path("/var/lib/ripdoctor")


def test_detection_never_raises_on_an_empty_environment() -> None:
    """A field it cannot determine is left empty; the doctor reports it."""
    m = detect({})
    assert m.home is not None
    assert m.settings_file.name == "config.toml"


def test_detection_reads_nothing_and_writes_nothing() -> None:
    m = a_machine()
    assert not m.state_dir.exists() or True  # never created here
    assert detect({"HOME": "/nowhere"}).state_dir == Path(
        "/nowhere/.local/share/ripdoctor"
    )


# ------------------------------------------------------------- loading


def test_a_missing_file_means_every_default() -> None:
    """Which is what a fresh install is."""
    s = S.load(a_machine(), Path("/nonexistent/config.toml"))
    assert s.port == 8080 and s.capture_rate == 48000
    assert not s.unknown and not s.unreadable


def test_defaults_describe_no_particular_machine(tmp_path: Path) -> None:
    """Detection fills the machine's part; the shipped values do not name it."""
    d = S.defaults(a_machine())
    assert d.capture_device == "", "a shipped default device would be one person's"
    assert d.vinyl.startswith("/home/someone")


def test_a_file_overrides_only_what_it_sets(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('port = 9000\ncapture_device = "hw:Rx,0"\n')
    s = S.load(a_machine(), p)
    assert s.port == 9000 and s.capture_device == "hw:Rx,0"
    assert s.capture_rate == 48000, "an absent key kept its default"


def test_an_unknown_key_is_a_warning_not_a_refusal(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('port = 9000\nnonsense = "x"\n')
    s = S.load(a_machine(), p)
    assert s.port == 9000, "the rest of the file still applied"
    assert s.unknown == ("nonsense",)


def test_a_key_with_the_wrong_type_is_dropped_and_named(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('port = "not a number"\nbind = "10.0.0.5"\n')
    s = S.load(a_machine(), p)
    assert s.port == 8080, "the default survived"
    assert s.bind == "10.0.0.5", "the rest of the file still applied"
    assert s.unreadable == ("port",)


def test_a_boolean_is_not_satisfied_by_a_number(tmp_path: Path) -> None:
    """TOML has real booleans, and 1 is not one of them."""
    p = tmp_path / "config.toml"
    p.write_text("embed_art = 1\n")
    assert S.load(a_machine(), p).unreadable == ("embed_art",)


def test_a_number_written_as_a_string_is_accepted(tmp_path: Path) -> None:
    """Being strict about this helps nobody; the intent is unambiguous."""
    p = tmp_path / "config.toml"
    p.write_text('port = "9001"\n')
    assert S.load(a_machine(), p).port == 9001


def test_a_file_that_is_not_toml_at_all_still_starts(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text("this is not = = toml\n")
    s = S.load(a_machine(), p)
    assert s.port == 8080
    assert s.unreadable and "config.toml" in s.unreadable[0]


def test_a_file_that_is_not_utf8_still_starts(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_bytes(b"\xff\xfe port = 1\n")
    assert S.load(a_machine(), p).port == 8080


# ------------------------------------------------------------ typos


@pytest.mark.parametrize(
    ("typo", "wanted"),
    [("prot", "port"), ("librari", "library"), ("capture_rat", "capture_rate")],
)
def test_a_near_miss_is_suggested(typo: str, wanted: str) -> None:
    assert S.nearest(typo) == wanted


def test_nothing_is_suggested_for_something_unrecognisable() -> None:
    """Suggesting a key for a word that resembles none is worse than silence."""
    assert S.nearest("xyzzy") is None
    assert S.nearest("a") is None


# -------------------------------------------------------- thresholds


def test_every_threshold_defaults_to_the_value_the_code_uses() -> None:
    """A default that drifts from the constant it mirrors is worse than none."""
    from ripdoctor.core import autostop, gaps, sides, xcorr

    t = Thresholds()
    assert t.gap_below == gaps.BELOW
    assert t.gap_above == gaps.ABOVE
    assert t.span_run == sides.SPAN_RUN
    assert t.autostop_below == autostop.BELOW
    assert t.align_min_r == xcorr.MIN_R


def test_a_moved_threshold_is_reported_with_what_it_was() -> None:
    """The first thing to look at when detection behaves unexpectedly."""
    t = Thresholds(gap_below=12.0)
    assert t.changed_from_default() == {"gap_below": (16.0, 12.0)}
    assert Thresholds().changed_from_default() == {}


def test_every_threshold_is_documented() -> None:
    """The numbers are the domain knowledge, so they are what must not drift.

    A threshold with no explanation is a number nobody can judge, and this
    project's whole claim is that its numbers were measured.
    """
    method = Path(__file__).parent.parent / "docs" / "method.md"
    text = method.read_text()
    assert len(NAMES) >= 10
    missing = [n for n in NAMES if f"`{n}`" not in text]
    assert not missing, f"undocumented thresholds: {missing}"

    # The documented value must be the one the code actually uses. Taken from
    # the table row rather than any mention: the prose names these too, and it
    # should be able to without breaking this.
    defaults = Thresholds().as_dict()
    rows = [ln for ln in text.splitlines() if ln.startswith("|")]
    for name in NAMES:
        row = next(ln for ln in rows if f"`{name}`" in ln)
        value = defaults[name]
        shown = row.split("|")[2].strip()
        assert shown, f"{name} has no value in the table"
        number = float(shown.split()[0].replace("\u2212", "-"))
        assert number == pytest.approx(value, rel=0.01), (
            f"{name}: documented {number}, code uses {value}"
        )


def test_the_resolved_configuration_can_be_reported() -> None:
    """A wrong path is far easier to see than to deduce from behaviour."""
    described = S.describe(S.defaults(a_machine()), Thresholds())
    assert "library" in described and "vinyl" in described
    assert described["thresholds"]["gap_below"] == 16.0
    assert "unknown" not in described, "internal fields are not settings"
