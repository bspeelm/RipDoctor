"""What every route needs, built once and passed in.

Handlers close over this rather than reaching for module state, so a test builds
a service pointed at a temporary directory and the whole server runs inside it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ripdoctor.audio.runner import Runner
from ripdoctor.config.settings import Settings
from ripdoctor.config.thresholds import Thresholds
from ripdoctor.store.files import Layout
from ripdoctor.web.auth import Credentials, Sessions, Throttle
from ripdoctor.work.capture import Recorder
from ripdoctor.work.jobs import Jobs


@dataclass
class Service:
    layout: Layout
    settings: Settings
    thresholds: Thresholds
    runner: Runner
    credentials: Credentials
    sessions: Sessions
    jobs: Jobs = field(default_factory=Jobs)
    recorder: Recorder = field(default_factory=Recorder)
    throttle: Throttle = field(default_factory=Throttle)
    now: Callable[[], float] = time.time
