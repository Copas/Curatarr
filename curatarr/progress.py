"""One overall progress bar for everything Curatarr is working through.

The separate kinds of work run side by side on different resources
(transcription, file rebuilds paced to Jellyfin's thumbnails), so the time
left overall is when the slowest finishes, not the sum. Each kind's pace is
its own recent one; the bar counts items done and left across all of them.
"""

from datetime import timedelta

from sqlalchemy import func

from . import db
from .models import LifecycleAction, utcnow

PACE_WINDOW = timedelta(hours=24)


def _per_hour(action_types, now) -> float | None:
    """How many of these Activity entries were logged an hour over the last
    day. None when there were too few: an older pace would describe work that
    has since sped up or stopped."""
    count = (
        db.session.query(func.count(LifecycleAction.id))
        .filter(
            LifecycleAction.action_type.in_(action_types),
            LifecycleAction.created_at >= now - PACE_WINDOW,
        )
        .scalar()
    )
    return count / (PACE_WINDOW.total_seconds() / 3600) if count >= 3 else None


def overall(now=None) -> dict | None:
    from . import compat_audio, seek_repair
    from .enrichment import _duration_words, job_forecast, marker_forecast

    now = now or utcnow()
    parts = []
    for name, forecast in (
        ("Chapter jobs", job_forecast(now)),
        ("Skip-marker checks", marker_forecast(now)),
    ):
        if forecast:
            parts.append(
                {
                    "name": name,
                    "done": forecast["done"],
                    "left": forecast["remaining"],
                    "finishes_at": forecast["finishes_at"],
                    "approximate": False,
                }
            )
    # TV-friendly audio and the seek repair share one limit (rebuilds wait for
    # Jellyfin's thumbnails), so they get one estimate from their joint pace.
    rebuilds_left, rebuild_parts = 0.0, []
    audio = compat_audio.progress()
    if audio and audio["on"]:
        left = audio["films"]["left"] + audio["episodes"]["left"]
        if left:
            rebuilds_left += left
            rebuild_parts.append(
                {"name": "TV-friendly audio", "done": audio["done"], "left": left}
            )
    seek = seek_repair.progress()
    if seek and seek["on"] and seek["left"]:
        # Checking a file takes a moment; the time is in rebuilding the share
        # of them that need it.
        share = seek["rebuilt"] / seek["checked"] if seek["checked"] else 0.25
        rebuilds_left += seek["left"] * share
        rebuild_parts.append(
            {"name": "Seeking on Roku", "done": seek["checked"], "left": seek["left"]}
        )
    if rebuild_parts:
        rate = _per_hour(("compat_audio_added", "seek_index_repaired"), now)
        finishes = now + timedelta(hours=rebuilds_left / rate) if rate else None
        for part in rebuild_parts:
            parts.append(part | {"finishes_at": finishes, "approximate": True})
    if not parts:
        return None
    done = sum(p["done"] for p in parts)
    left = sum(p["left"] for p in parts)
    known = [p for p in parts if p["finishes_at"]]
    slowest = max(known, key=lambda p: p["finishes_at"]) if known else None
    return {
        "done": done,
        "left": left,
        "percent": round(100 * done / (done + left), 1) if done + left else 100.0,
        "finishes_at": slowest["finishes_at"] if slowest else None,
        "time_left": _duration_words(slowest["finishes_at"] - now) if slowest else None,
        "slowest": slowest["name"] if slowest else None,
        "approximate": bool(slowest and slowest["approximate"]),
        "unknown": [p["name"] for p in parts if not p["finishes_at"]],
        "parts": parts,
    }
