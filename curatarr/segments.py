"""Skip segments (intros, credits, recaps, previews) kept by Curatarr.

Curatarr finds and corrects segments; Intro Skipper has stored them and given
them to Jellyfin. To retire Intro Skipper, Curatarr keeps every episode's
segments itself (MediaSegment), and its own Jellyfin plugin gives them to
Jellyfin as a segment provider.

``SegmentStore`` has Intro Skipper's four operations (an item's segments, add,
hide, restore). While Intro Skipper is in use each goes to it as before and is
recorded here too: reading an item's segments takes in whatever the plugin's
own analysis changed. ``import_sweep`` reads every episode once, so the store
starts complete, including the intros only Intro Skipper found.

Every change takes the next ``version``; ``changed_since`` lists what the
plugin hasn't fetched yet.
"""

import time

from sqlalchemy import func

from . import db, integrations
from .integrations import IntegrationError
from .models import MediaSegment, utcnow, uuid

IMPORT_KEY = "segments_import"  # {"after": last part id read, "finished_at", "rule"}
# Raised when what's read changes, so every item is read again:
#   2: Intro Skipper's suppressed segments are kept hidden
IMPORT_RULE = 2
IMPORT_BATCH = 100
IMPORT_SECONDS = 20
# Jellyfin's names for the segment types (MediaSegmentType).
JELLYFIN_TYPES = {
    "Introduction": "Intro",
    "Credits": "Outro",
    "Recap": "Recap",
    "Preview": "Preview",
    "Commercial": "Commercial",
}


def bare(item_id) -> str:
    return str(item_id or "").replace("-", "").lower()


def _next_version() -> int:
    return (db.session.query(func.max(MediaSegment.version)).scalar() or 0) + 1


def mirror(item_id, live: list[dict]) -> bool:
    """Make the store hold exactly ``live`` (Intro Skipper's segments) for an
    item; True when anything changed."""
    item = bare(item_id)
    kept = {s.id: s for s in db.session.query(MediaSegment).filter_by(item_id=item)}
    version, changed = None, False
    seen = set()
    for segment in live:
        sid = str(segment.get("Id") or "")
        if not sid:
            continue
        seen.add(sid)
        start, end = float(segment.get("Start") or 0), float(segment.get("End") or 0)
        kind = segment.get("Type") or "Unknown"
        # Intro Skipper lists the segments it hides too, flagged Suppressed
        # (those Curatarr replaced or hid): kept, hidden. Taken as shown,
        # they'd have come back beside their replacements.
        hidden = bool(segment.get("Suppressed"))
        row = kept.get(sid)
        if row and (row.kind, row.start, row.end, row.hidden) == (
            kind,
            start,
            end,
            hidden,
        ):
            continue
        version = version or _next_version()
        if row is None:
            row = MediaSegment(id=sid, item_id=item)
            db.session.add(row)
        row.kind, row.start, row.end, row.hidden, row.version = (
            kind,
            start,
            end,
            hidden,
            version,
        )
        changed = True
    for sid, row in kept.items():
        if sid not in seen and not row.hidden:
            version = version or _next_version()
            row.hidden, row.version = True, version
            changed = True
    return changed


class SegmentStore:
    """Intro Skipper's segment operations, recorded in Curatarr's store too.
    With ``forward`` False (Intro Skipper retired) only the store is used."""

    def __init__(self, jellyfin=None, forward=True):
        # Looked up each time, so a stand-in (tests) or a later change applies.
        self.plugin = integrations.IntroSkipper(jellyfin) if forward else None

    def segments(self, item_id) -> list[dict] | None:
        """The item's segments as Intro Skipper gives them ({"Id", "Type",
        "Start", "End"}); None when there's no source to ask."""
        if self.plugin is not None:
            live = self.plugin.segments(item_id)
            if live is not None:
                mirror(item_id, live)
            return live
        return [
            {"Id": s.id, "Type": s.kind, "Start": s.start, "End": s.end}
            for s in db.session.query(MediaSegment)
            .filter_by(item_id=bare(item_id), hidden=False)
            .order_by(MediaSegment.start)
        ]

    def add(self, item_id, kind: str, start: float, end: float) -> str:
        sid = self.plugin.add(item_id, kind, start, end) if self.plugin else uuid()
        row = db.session.get(MediaSegment, sid) or MediaSegment(
            id=sid, item_id=bare(item_id)
        )
        db.session.add(row)
        row.kind, row.start, row.end = kind, round(start, 3), round(end, 3)
        row.hidden, row.version = False, _next_version()
        return sid

    def hide(self, item_id, segment_id):
        if self.plugin:
            self.plugin.hide(item_id, segment_id)
        row = db.session.get(MediaSegment, str(segment_id))
        if row and not row.hidden:
            row.hidden, row.version = True, _next_version()

    def restore(self, item_id, segment_id):
        if self.plugin:
            self.plugin.restore(item_id, segment_id)
        row = db.session.get(MediaSegment, str(segment_id))
        if row and row.hidden:
            row.hidden, row.version = False, _next_version()
        elif row is None and self.plugin:
            self.segments(item_id)  # not recorded yet: take the plugin's list


def store():
    """The segment store as Curatarr uses it now: through Intro Skipper."""
    from .services import client

    return SegmentStore(client("jellyfin"))


def changed_since(after: int, limit: int = 500) -> dict:
    """{"version", "items": [{"item", "segments": [{"type", "start", "end"}]}],
    "more"}: every item with a change after ``after``, with all its visible
    segments (an item whose segments are all hidden has none)."""
    rows = (
        db.session.query(MediaSegment.item_id, func.max(MediaSegment.version))
        .filter(MediaSegment.version > after)
        .group_by(MediaSegment.item_id)
        .order_by(func.max(MediaSegment.version))
        .limit(limit + 1)
        .all()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for item_id, _version in rows:
        visible = (
            db.session.query(MediaSegment)
            .filter_by(item_id=item_id, hidden=False)
            .order_by(MediaSegment.start)
        )
        items.append(
            {
                "item": item_id,
                "segments": [
                    {
                        "type": JELLYFIN_TYPES.get(s.kind, "Unknown"),
                        "start": s.start,
                        "end": s.end,
                    }
                    for s in visible
                ],
            }
        )
    version = rows[-1][1] if rows else after
    return {"version": version, "items": items, "more": more}


def import_sweep(now=None) -> int:
    """Read every episode's (and film's) segments from Intro Skipper once,
    a batch each worker cycle; returns how many items were read."""
    from .enrichment import jellyfin_item_id
    from .models import MediaIdentity, MediaPart
    from .services import set_setting, setting

    state = dict(setting(IMPORT_KEY, {}) or {})
    if state.get("rule") != IMPORT_RULE:
        state = {"rule": IMPORT_RULE}  # read everything again
    if state.get("finished_at"):
        return 0
    started, read = time.monotonic(), 0
    try:
        segments = store()
    except IntegrationError:
        return 0
    rows = (
        db.session.query(MediaPart, MediaIdentity)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(MediaPart.has_file.is_(True), MediaPart.id > (state.get("after") or ""))
        .order_by(MediaPart.id)
        .limit(IMPORT_BATCH)
        .all()
    )
    for part, media in rows:
        if time.monotonic() - started > IMPORT_SECONDS:
            break
        item_id = jellyfin_item_id(part, media)
        if item_id:
            try:
                # None: Intro Skipper has nothing for this item (it answers "not
                # found" for one it never analysed). Treating that as "not
                # installed" stopped the import at the first such episode.
                if segments.segments(item_id) is not None:
                    read += 1
            except IntegrationError:
                break  # Jellyfin unreachable: carry on next cycle
        state["after"] = part.id
        db.session.commit()
    else:
        if len(rows) < IMPORT_BATCH:
            state["finished_at"] = (now or utcnow()).isoformat()
    set_setting(IMPORT_KEY, state)
    db.session.commit()
    return read
