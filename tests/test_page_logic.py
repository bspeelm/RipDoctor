"""What the page's own numbers do, run for real rather than read as text.

These need node, which is not this project's dependency. The with-tools job runs
them and requires that they ran; anywhere without node they stand aside.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = [
    pytest.mark.needs_node,
    pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed"),
]


def check_js(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv list, never shell=True
        [sys.executable, str(ROOT / "scripts" / "check_js.py"), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_real_scripts_parse() -> None:
    done = check_js("--require")
    assert done.returncode == 0, done.stdout
    assert " scripts parse as modules" in done.stdout
    assert not done.stdout.startswith("js: 0 ")


def test_a_duplicate_top_level_name_stops_the_check(tmp_path: Path) -> None:
    (tmp_path / "broken.js").write_text("const twice = 1;\nfunction twice() {}\n")
    done = check_js("--require", "--dir", tmp_path)
    assert done.returncode == 1
    assert "broken.js does not parse" in done.stdout
    assert "already been declared" in done.stdout


def test_any_other_syntax_error_stops_the_check(tmp_path: Path) -> None:
    (tmp_path / "fine.js").write_text("export const a = 1;\n")
    (tmp_path / "broken.js").write_text("export function f( {\n")
    done = check_js("--dir", tmp_path)
    assert done.returncode == 1 and "1 of 2 scripts" in done.stdout


def test_a_module_is_read_as_a_module(tmp_path: Path) -> None:
    """As CommonJS, `import` is the error, and the check would fail on every real
    file or be quietly switched off. This is what says the flag is right."""
    (tmp_path / "module.js").write_text(
        'import { x } from "/other.js";\nexport const y = await x;\n'
    )
    done = check_js("--dir", tmp_path)
    assert done.returncode == 0, done.stdout


def test_a_directory_with_no_scripts_is_not_a_pass(tmp_path: Path) -> None:
    assert check_js("--dir", tmp_path).returncode == 1
