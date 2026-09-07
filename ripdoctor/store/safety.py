"""Proving a path is still inside where it should be.

`core.naming.token` validates one component against an allowlist. This is the
second half: after joining, the result is resolved and shown to still be under
its root. The allowlist alone does not catch a symlink planted inside the tree
and pointing out of it, because every component of that path is well-formed.
"""

from __future__ import annotations

from pathlib import Path

from ripdoctor.core.naming import Unsafe, token


def under(root: str | Path, *parts: str) -> Path:
    """Join validated components onto `root` and prove the result is inside it.

    Raises rather than sanitising. Rewriting a bad name hides an attempt; the
    caller is told instead.
    """
    base = Path(root).resolve()
    joined = base.joinpath(*(token(p) for p in parts))
    resolved = joined.resolve()
    if resolved != base and base not in resolved.parents:
        raise Unsafe(f"path escapes {base}: {resolved}")
    return resolved


def contains(root: str | Path, candidate: str | Path) -> bool:
    """Is `candidate` inside `root` once both are resolved?"""
    base = Path(root).resolve()
    target = Path(candidate).resolve()
    return target == base or base in target.parents
