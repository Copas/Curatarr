"""How many chapter jobs to run, adapted to the machine and to who's watching.

Every worker cycle (~15 s) Curatarr measures CPU and GPU use (including the
video decoder Jellyfin plays from) and who's playing what in Jellyfin. From
that it sets a target utilisation: high when nobody is watching, lower for each
viewer, lower still for each stream Jellyfin is transcoding. Below the target
it adds a job (one at a time, a minute apart, so each new job's load shows up
before the next); above it, it starts nothing new and running jobs finish in a
few minutes; far above it while someone watches, it suspends running jobs until
there's room again.
"""

import os
import subprocess
import time
from dataclasses import dataclass, field

IDLE_TARGET = 0.85  # utilisation jobs may push the machine to with nobody watching
VIEWER_COST = 0.15  # headroom kept back per person watching
TRANSCODE_COST = 0.15  # and again per stream Jellyfin is transcoding
FLOOR = 0.35  # never aim lower than this (direct play needs very little)
SLACK = 0.12  # add a job only when this far below the target
OVERLOAD = 0.20  # suspend running jobs this far above it while someone watches
STEP_SECONDS = 60  # between adding jobs
OVERLOAD_SECONDS = 30  # an overload must last this long before pausing
PAUSE_SECONDS = 120  # and a pause lasts at least this long, so it doesn't flap


@dataclass
class Reading:
    cpu: float | None  # 0-1 across all cores
    gpu: float | None  # 0-1, the GPU's compute (what transcription uses)
    viewers: int
    transcodes: int
    decoder: float | None = None  # 0-1, its video decoder: shown, not counted

    @property
    def load(self) -> float:
        """What playback competes for. The GPU only counts while Jellyfin is
        transcoding: direct play doesn't touch it, and otherwise its use is
        Curatarr's own transcription and language model."""
        gpu = self.gpu if self.transcodes else None
        return max(x for x in (self.cpu, gpu, 0.0) if x is not None)


@dataclass
class Decision:
    limit: int
    suspend: bool
    target: float
    reason: str


@dataclass
class Controller:
    """Keeps the job count near the target, one step at a time."""

    limit: int = 1
    last_change: float = field(default=0.0)
    over_since: float | None = None
    paused_until: float | None = None

    def decide(
        self,
        reading: Reading,
        *,
        most: int,
        running: int,
        waiting: bool,
        now: float | None = None,
    ) -> Decision:
        now = time.monotonic() if now is None else now
        target = max(
            FLOOR,
            IDLE_TARGET
            - VIEWER_COST * reading.viewers
            - TRANSCODE_COST * reading.transcodes,
        )
        load = reading.load
        watching = reading.viewers > 0
        what = _describe(reading, load, target)
        if self.paused_until is not None:
            # While paused, the load is everything but Curatarr: resume once
            # there's real room, or the moment nobody is watching.
            if watching and (now < self.paused_until or load > target - SLACK):
                return Decision(
                    0, True, target, f"{what}: paused to keep playback smooth"
                )
            self.paused_until, self.limit, self.last_change = None, 1, now
            return Decision(1, False, target, f"{what}: resuming")
        if watching and load > target + OVERLOAD and running:
            self.over_since = self.over_since if self.over_since is not None else now
            if now - self.over_since >= OVERLOAD_SECONDS:
                self.over_since, self.limit, self.last_change = None, 0, now
                self.paused_until = now + PAUSE_SECONDS
                return Decision(
                    0, True, target, f"{what}: paused to keep playback smooth"
                )
        else:
            self.over_since = None
        if load > target:
            # Over target: start nothing new; running jobs finish on their own.
            self.limit = min(self.limit, running)
            if self.limit < running:
                self.last_change = now
            return Decision(
                self.limit, False, target, f"{what}: holding at {self.limit}"
            )
        room = load < target - SLACK
        if (
            room
            and waiting
            and self.limit < most
            and now - self.last_change >= STEP_SECONDS
        ):
            self.limit += 1
            self.last_change = now
            return Decision(self.limit, False, target, f"{what}: room for {self.limit}")
        if not watching:
            self.limit = max(self.limit, 1)  # always make some progress when idle
        self.limit = min(self.limit, most)
        return Decision(self.limit, False, target, f"{what}: {self.limit} at a time")


def _describe(reading: Reading, load: float, target: float) -> str:
    parts = []
    if reading.cpu is not None:
        parts.append(f"CPU {reading.cpu:.0%}")
    if reading.gpu is not None:
        # Only Jellyfin's transcoding competes with Curatarr for the graphics
        # card, so without one the GPU isn't counted (it's mostly Curatarr's).
        counted = "" if reading.transcodes else " (not counted: nothing is transcoding)"
        parts.append(f"GPU {reading.gpu:.0%}{counted}")
    if reading.decoder is not None and reading.transcodes:
        parts.append(f"video decoder {reading.decoder:.0%} (Jellyfin's)")
    if reading.viewers:
        who = f"{reading.viewers} watching"
        if reading.transcodes:
            who += f" ({reading.transcodes} transcoding)"
        parts.append(who)
    else:
        parts.append("nobody watching")
    return ", ".join(parts) + f" (aiming under {target:.0%})"


# --- Sensors -------------------------------------------------------------------

_last_cpu: tuple[int, int] | None = None


def cpu_use() -> float | None:
    """Share of all CPU time busy since the last call (from /proc/stat)."""
    global _last_cpu
    try:
        with open("/proc/stat", encoding="ascii") as handle:
            fields = [int(x) for x in handle.readline().split()[1:]]
    except (OSError, ValueError):
        return _load_fallback()
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    total = sum(fields)
    previous, _last_cpu = _last_cpu, (idle, total)
    if previous is None or total <= previous[1]:
        return _load_fallback()
    return 1.0 - (idle - previous[0]) / (total - previous[1])


def _load_fallback() -> float | None:
    try:
        return min(os.getloadavg()[0] / (os.cpu_count() or 1), 1.0)
    except OSError:
        return None


# While Jellyfin transcodes, its video decoder is Jellyfin's: Curatarr's
# scene detection decodes on the processor instead (mediafile.detect_scenes),
# so the decoder's load isn't counted against Curatarr. Counting it kept all
# work paused for as long as anyone was transcoding: Jellyfin's own streams
# kept the decoder near 100%, which no pause of Curatarr's could change.
leave_decoder = False
last_decoder: float | None = None


def gpu_use() -> float | None:
    """The NVIDIA GPU's compute use, or None without one (its video decoder's
    is kept in last_decoder)."""
    global last_decoder
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,utilization.decoder",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        compute, decoder = (
            float(x) / 100 for x in result.stdout.splitlines()[0].split(",")
        )
    except (IndexError, ValueError):
        return None
    last_decoder = decoder
    return compute


def viewers(sessions: list[dict]) -> tuple[int, int]:
    """(streams playing, of which transcoding) from Jellyfin's sessions."""
    playing = [s for s in sessions if s.get("NowPlayingItem")]
    transcoding = [
        s
        for s in playing
        if (s.get("PlayState") or {}).get("PlayMethod") == "Transcode"
    ]
    return len(playing), len(transcoding)


controller = Controller()
