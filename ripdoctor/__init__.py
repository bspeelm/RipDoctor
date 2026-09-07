"""RipDoctor - capture a vinyl side, find the track boundaries, cut them.

Layered, and the layering is enforced by tests rather than convention:

    core/   pure computation over dB envelopes; no subprocess, disk or clock
    audio/  the only subprocess users
    store/  path safety, on-disk layout, plan files, the analysis cache
    config/ machine detection, settings, measured thresholds
    doctor/ turns a missing tool or permission into an actionable problem
    web/    routes as pure request-to-response functions
    work/   the job runner and per-slug locks

See docs/decisions.md for why each boundary is where it is.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ripdoctor")
except PackageNotFoundError:  # running from a source tree with no install
    __version__ = "0+unknown"

__all__ = ["__version__"]
