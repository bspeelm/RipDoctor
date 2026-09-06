"""The layering rules, enforced.

These are the only things standing between the design in docs/decisions.md and
the design the code actually has. They are cheap, they run everywhere, and they
fail the moment someone reaches through a boundary.

Each test that walks a discovered set asserts how many things it looked at. A
test that silently examines nothing passes for the wrong reason, which is worse
than failing.
"""

from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "ripdoctor"

# core/ is the published algorithm. It computes over values handed to it and
# reaches nothing: no process, no disk, no network, no clock. ADR-005.
FORBIDDEN_IN_CORE = {
    "subprocess",
    "os",
    "pathlib",
    "shutil",
    "socket",
    "time",
    "threading",
    "urllib",
    "tempfile",
    "random",
}


def modules_under(where: Path) -> list[Path]:
    return sorted(p for p in where.rglob("*.py") if "__pycache__" not in p.parts)


def imported_names(tree: ast.AST) -> set[str]:
    """Top-level package names imported anywhere in a module."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_core_reaches_nothing_impure() -> None:
    """core/ computes; it does not touch the world.

    This is the test that makes the whole suite possible. If core stays pure,
    the algorithm can be exercised with no ffmpeg, no sound card and no files,
    which is what lets it be tested at all.
    """
    checked = 0
    offences: list[str] = []
    for mod in modules_under(PKG / "core"):
        tree = ast.parse(mod.read_text(encoding="utf-8"))
        bad = imported_names(tree) & FORBIDDEN_IN_CORE
        if bad:
            offences.append(f"{mod.relative_to(PKG)} imports {sorted(bad)}")
        checked += 1

    assert checked >= 1, "no core modules were examined; the pattern has drifted"
    assert not offences, "core must stay pure:\n  " + "\n  ".join(offences)


def test_only_config_reads_the_environment() -> None:
    """os.environ is read in one layer, not eight.

    The predecessor read eight environment variables at import time across four
    modules, which is why its data root could not actually be relocated. Reading
    configuration in exactly one place is the fix, and this is what holds it.
    """
    checked = 0
    offences: list[str] = []
    for mod in modules_under(PKG):
        if mod.parts[-2] == "config":
            continue
        src = mod.read_text(encoding="utf-8")
        checked += 1
        for node in ast.walk(ast.parse(src)):
            # os.environ / os.environ.get / os.getenv
            if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}:
                offences.append(f"{mod.relative_to(PKG)}:{node.lineno} reads the env")

    assert checked >= 1, "no modules were examined; the pattern has drifted"
    assert not offences, "only ripdoctor/config may read the environment:\n  " + (
        "\n  ".join(offences)
    )


def test_subprocess_lives_only_in_the_audio_layer() -> None:
    """Every external binary call happens behind one seam.

    Not a security rule on its own - it is what makes the fake runner possible,
    and therefore what makes everything above audio/ testable without ffmpeg.
    """
    allowed = {"audio", "integrations", "doctor"}
    checked = 0
    offences: list[str] = []
    for mod in modules_under(PKG):
        layer = mod.parts[-2]
        checked += 1
        if layer in allowed:
            continue
        if "subprocess" in imported_names(ast.parse(mod.read_text(encoding="utf-8"))):
            offences.append(str(mod.relative_to(PKG)))

    assert checked >= 1, "no modules were examined; the pattern has drifted"
    assert not offences, (
        "subprocess belongs in audio/, integrations/ or doctor/:\n  "
        + "\n  ".join(offences)
    )


def test_every_layer_named_in_the_docstring_exists() -> None:
    """The package docstring describes the layout. Keep it true."""
    doc = (PKG / "__init__.py").read_text(encoding="utf-8")
    named = [ln.split("/")[0].strip() for ln in doc.splitlines() if "/ " in ln]
    layers = [n for n in named if n and n.isidentifier()]

    assert len(layers) >= 6, "the layer list in the docstring has drifted"
    for layer in layers:
        assert (PKG / layer).is_dir(), f"docstring names {layer}/, which does not exist"
