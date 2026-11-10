"""Dress the demo library for the README's screenshots.

Run on a demo database only (it refuses anything else), after `demo-seed`
and `worker --once`:

    CURATARR_DEMO_MODE=true DATABASE_URL=sqlite:////tmp/demo.db \\
        .venv/bin/python scripts/stage_screenshots.py

It gives some demo titles made-up names, keeps a short review queue (one
Leaving Soon, one to review, one only recommended), and adds one example of
each kind of problem: subtitles burned into the picture, a file cut short
being replaced, a split film with no better copy, a film with no English
audio, and a namesake show filed under a series. Then capture the pages at
1280 px wide (docs/DEVELOPMENT.md, Screenshots).
"""

import base64
import colorsys
import zlib
from datetime import timedelta
from io import BytesIO

from curatarr import create_app, db
from curatarr.models import (
    AppSetting,
    ChapterState,
    Library,
    MediaIdentity,
    MediaPart,
    PurgeCandidate,
    utcnow,
)
from curatarr.services import set_setting

SHOWS = {
    "Demo Series 01": "Harbor Lights",  # the demo's missing always-keep episode
    "Demo Series 05": "Kitchen Table Club",
    "Demo Series 07": "Dry Creek Mesa",
}
LIBRARIES = {
    "Demo Movies": "Movies",
    "Demo TV A": "Shows",
    "Demo TV B": "Kids & Family",
}
FILMS = {
    "Demo Movie 08": "The Quiet Year",
    "Demo Movie 03": "Thunder Road Trip",
    "Demo Movie 11": "The Lighthouse Watch",
    "Demo Movie 14": "Overdue",
    "Demo Movie 17": "The Body Double",
    "Demo Movie 21": "The Ninth Life",
}


def _poster(title: str) -> bytes:
    """A simple made-up poster: a sky, a moon, hills and the title."""
    from PIL import Image, ImageDraw, ImageFont

    hue = (zlib.crc32(title.encode()) % 360) / 360
    sky = tuple(round(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.55, 0.45))
    hills = tuple(round(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.6, 0.18))
    image = Image.new("RGB", (300, 450), sky)
    draw = ImageDraw.Draw(image)
    draw.ellipse((190, 60, 250, 120), fill="#E8F1F2")
    draw.polygon(
        [(0, 330), (90, 270), (170, 310), (240, 260), (300, 300), (300, 450), (0, 450)],
        fill=hills,
    )
    font = ImageFont.truetype("DejaVuSerif-Bold.ttf", 22)
    words, lines = title.upper().split(), [""]
    for word in words:
        trial = (lines[-1] + " " + word).strip()
        if draw.textlength(trial, font=font) > 260 and lines[-1]:
            lines.append(word)
        else:
            lines[-1] = trial
    y = 450 - 30 - 28 * len(lines)
    for line in lines:
        draw.text(
            ((300 - draw.textlength(line, font=font)) / 2, y),
            line,
            font=font,
            fill="#F4F4F4",
        )
        y += 28
    out = BytesIO()
    image.save(out, format="JPEG", quality=90)
    return out.getvalue()


def _media(old, new):
    media = db.session.query(MediaIdentity).filter_by(title=old).one()
    media.title = media.sort_title = new
    if media.jellyfin_id:
        key = f"demo_image:{media.jellyfin_id}"
        row = db.session.query(AppSetting).filter_by(key=key).first()
        if row is None:
            row = AppSetting(key=key)
            db.session.add(row)
        row.value_json = base64.b64encode(_poster(new)).decode()
    if media.media_type == "movie":
        for part in media.parts:
            part.file_path = f"/movies/{new}/{new}.mkv"
    return media


def _state(media):
    part = db.session.query(MediaPart).filter_by(media_identity_id=media.id).first()
    state = (
        db.session.query(ChapterState).filter_by(media_part_id=part.id).one_or_none()
    )
    if state is None:
        state = ChapterState(media_part_id=part.id, media_identity_id=media.id)
        db.session.add(state)
    return state


def stage():
    if not db.session.query(MediaIdentity).filter_by(title="Demo Series 01").first():
        raise SystemExit("Not a freshly seeded demo database; nothing changed.")
    now = utcnow()
    for old, new in LIBRARIES.items():
        db.session.query(Library).filter_by(name=old).one().name = new
    named = {new: _media(old, new) for old, new in {**SHOWS, **FILMS}.items()}

    # The review queue: three titles, one in each state.
    db.session.query(PurgeCandidate).delete()
    for title, state, reason, days in (
        ("Kitchen Table Club", "LEAVING_SOON", "no playback for 120 days.", 21),
        (
            "Thunder Road Trip",
            "REVIEW",
            "No playback observed for more than 90 days.",
            None,
        ),
        (
            "The Quiet Year",
            "ELIGIBLE",
            "No playback observed for more than 90 days.",
            None,
        ),
    ):
        media = named[title]
        db.session.add(
            PurgeCandidate(
                media_identity_id=media.id,
                state=state,
                reason_code="inactivity",
                reason_text=reason,
                reclaimable_bytes=sum(p.size_bytes or 0 for p in media.parts)
                or 1_900_000_000,
                scheduled_delete_at=now + timedelta(days=days) if days else None,
            )
        )

    # Problems: one of each kind.
    checked = now.isoformat()
    lantern = _state(named["The Lighthouse Watch"])
    lantern.file_checked_at = now
    lantern.file_problem = True
    lantern.file_check = {
        "issues": [],
        "transcript": True,
        "subtitles": {"verdict": "burned", "hits": 21, "samples": 24},
    }
    overdue = _state(named["Overdue"])
    overdue.file_checked_at = now
    overdue.file_problem = True
    overdue.file_check = {
        "issues": [
            {
                "kind": "truncated",
                "text": "The picture stops at 1:12:40 of 1:51:05: probably an "
                "incomplete download",
            }
        ],
        "replace_requested": True,
        "replace_auto": True,
    }
    double = _state(named["The Body Double"])
    double.file_checked_at = now
    double.file_problem = True
    double.file_check = {
        "issues": [
            {
                "kind": "short",
                "text": "Only 41 of 98 minutes: one part of a split release, so "
                "the rest of the film is missing",
            }
        ],
        "no_better": {"checked": 4, "at": checked},
    }
    ninth = _state(named["The Ninth Life"])
    ninth.dub_checked_at = now
    ninth.dub_result = {"original": "fr", "verdict": "no_english"}
    mesa = named["Dry Creek Mesa"]
    set_setting(
        "wrong_versions",
        {
            mesa.id: {
                "verdict": {
                    "files": [{"id": n} for n in range(9)],
                    "evidence": [
                        (
                            "9 of 10 file names say 1987, but Dry Creek Mesa "
                            "started in 2019"
                        )
                    ],
                }
            }
        },
    )
    db.session.commit()
    from curatarr.artwork import reconcile_artwork

    reconcile_artwork()  # Leaving Soon's banner, on the new poster
    print("Staged:", ", ".join(sorted(named)))


if __name__ == "__main__":
    app = create_app()
    if not app.config["DEMO_MODE"]:
        raise SystemExit("Demo mode only: set CURATARR_DEMO_MODE=true.")
    with app.app_context():
        stage()
