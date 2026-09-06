"""RipDoctor - capture a vinyl side, find the track boundaries, cut them.

The package is layered, and the layering is enforced by tests rather than
convention:

  core/   pure computation over dB envelopes. No subprocess, no filesystem,
          no clock. This is the part worth publishing.
  audio/  the only subprocess users. ffmpeg, ffprobe, flac, metaflac, arecord.
  store/  path safety, on-disk layout, plan and spec files, the analysis cache.
  config/ machine detection, settings, and the measured thresholds.
  doctor/ turns a missing tool or permission into a visible, actionable problem.
  web/    routes as pure request-to-response functions; the server is a shim.

See docs/decisions.md for why each boundary is where it is.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ripdoctor")
except PackageNotFoundError:  # running from a source tree with no install
    __version__ = "0+unknown"

__all__ = ["__version__"]
