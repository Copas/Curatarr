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


def _per_hour(action_type, now) -> float | None:
    """How many of an Activity entry were logged an hour over the last day,
    or the last three when the last day has too few to tell."""
    for window in (PACE_WINDOW, 3 * PACE_WINDOW):
        count = (
            db.session.query(func.count(LifecycleAction.id))
            .filter(
                LifecycleAction.action_type == action_type,
                LifecycleAction.created_at >= now - window,
            )
            .scalar()
        )
        if count >= 3:
            return count / (window.total_seconds() / 3600)
    return None


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
    audio = compat_audio.progress()
    if audio and audio["on"]:
        left = audio["films"]["left"] + audio["episodes"]["left"]
        if left:
            rate = _per_hour("compat_audio_added", now)
            parts.append(
                {
                    "name": "TV-friendly audio",
                    "done": audio["done"],
                    "left": left,
                    "finishes_at": now + timedelta(hours=left / rate) if rate else None,
                    "approximate": True,  # paced by Jellyfin's thumbnails
                }
            )
    seek = seek_repair.progress()
    if seek and seek["on"] and seek["left"]:
        # Checking a file takes a moment; the time is in rebuilding the share
        # of them that need it, at the pace rebuilds have been going.
        share = seek["rebuilt"] / seek["checked"] if seek["checked"] else 0.25
        rate = _per_hour("seek_index_repaired", now)
        rebuilds = seek["left"] * share
        parts.append(
            {
                "name": "Seeking on Roku",
                "done": seek["checked"],
                "left": seek["left"],
                "finishes_at": now + timedelta(hours=rebuilds / rate) if rate else None,
                "approximate": True,
            }
        )
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
