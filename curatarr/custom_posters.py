"""Custom posters: a picture someone chose for a title, with the original kept.

A poster can be uploaded on a title's page, or picked up from an import
folder (Library → Posters): every couple of minutes Curatarr looks there for
new pictures, matches each file's name to a title and puts it on in Jellyfin.
Names like ``The Big Lebowski (1998).jpg``, ``the-big-lebowski-1998.png`` or
``08_the-big-lebowski.jpg`` (a leading number is ignored) all work; the year
tells apart titles with the same name. A file written again later (a redo)
replaces the poster it put on.

Before the first custom poster goes on, the poster it replaces is saved, so
**Restore original** can put it back. A picture that is already the title's
poster when it's imported is recorded without uploading it again, and its
original isn't known: restoring then takes the metadata provider's poster.

During Leaving Soon the countdown banner is drawn on the custom poster (the
artwork pass notices the change), and the poster saved as the original is the
one from before the banner.
"""

import re
import time
import unicodedata
from datetime import timedelta
from io import BytesIO
from pathlib import Path

from flask import current_app
from PIL import Image, UnidentifiedImageError

from . import db
from .integrations import IntegrationError
from .models import (
    CustomPoster,
    Library,
    MediaIdentity,
    MediaPart,
    PosterSnapshot,
    utcnow,
    uuid,
)

KEY = "custom_posters"  # settings: {"folder": ..., "prompts": {...}}
STATE_KEY = "custom_poster_imports"  # files seen, files not matched
EVERY = timedelta(minutes=2)
SETTLE = 20  # seconds a file must be unchanged before it's taken
BATCH = 20  # files a pass, so one worker cycle stays short
SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
MAX_UPLOAD = 25 * 1024 * 1024
LARGEST = (2000, 3000)  # bigger pictures are scaled down to this
MAX_PIXELS = 60_000_000  # refuse decompression bombs before decoding
SAME = 2.0  # mean grey difference (24 × 36) below which two posters are one
YEAR = re.compile(r"[\s_.\-(]+((?:19|20)\d\d)\)?$")
LEADING_NUMBER = re.compile(r"^\d+[\s_.\-]+")


def settings() -> dict:
    from .services import setting

    return {"folder": None, "prompts": {}} | (setting(KEY) or {})


def save_settings(folder: str | None, accounts=None) -> None:
    """The import folder, and who imported posters are for (None or empty:
    everyone, as Jellyfin's own poster)."""
    from .services import set_setting

    folder = (folder or "").strip() or None
    if folder and not Path(folder).is_dir():
        raise ValueError(f"Curatarr can't find the folder {folder}")
    chosen = sorted({a for a in accounts or [] if a})
    previous = settings()
    if chosen != sorted(previous.get("accounts") or []):
        # Who they're for changed: every picture is looked at again.
        set_setting(STATE_KEY, import_state() | {"seen": {}, "at": None})
    set_setting(KEY, previous | {"folder": folder, "accounts": chosen})
    db.session.commit()


def _store() -> Path:
    path = Path(current_app.instance_path) / "custom_posters"
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalise(data: bytes) -> bytes:
    """The picture as a JPEG no bigger than LARGEST; ValueError if it isn't one."""
    try:
        with Image.open(BytesIO(data)) as probe:
            if probe.width * probe.height > MAX_PIXELS:
                raise ValueError("That picture is too large")
            probe.verify()
        image = Image.open(BytesIO(data)).convert("RGB")
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError("That file isn't a picture Curatarr can read") from exc
    image.thumbnail(LARGEST)
    output = BytesIO()
    image.save(output, format="JPEG", quality=90, optimize=True)
    return output.getvalue()


def active(media_id, user_id=None) -> CustomPoster | None:
    return (
        db.session.query(CustomPoster)
        .filter_by(media_identity_id=media_id, jellyfin_user_id=user_id, active=True)
        .order_by(CustomPoster.created_at.desc())
        .first()
    )


def _same_picture(a: bytes, b: bytes) -> bool:
    from .artwork import _difference, _grey

    size = (24, 36)
    return _difference(_grey(a, size), _grey(b, size), size) < SAME


def _original(media, jellyfin) -> bytes:
    """The poster from before any custom one: during Leaving Soon, the one
    saved before the banner went on."""
    from .artwork import _jpeg

    snapshot = (
        db.session.query(PosterSnapshot)
        .filter_by(media_identity_id=media.id, image_type="Primary", active=True)
        .first()
    )
    if snapshot and snapshot.original_image_bytes_path:
        saved = Path(snapshot.original_image_bytes_path)
        if saved.exists():
            return saved.read_bytes()
    return _jpeg(jellyfin.image(media.jellyfin_id))


def apply(
    media, data: bytes, source: str, *, adopt=False, user_id=None
) -> CustomPoster | None:
    """Put ``data`` on as the title's poster in Jellyfin. With ``adopt`` (an
    import), a picture already showing is only recorded. Returns the record,
    or None when nothing changed (the same picture is on already).

    With ``user_id`` it's that account's poster only: Jellyfin's own poster
    stays, and the Curatarr plugin shows this one to that account."""
    from .services import audit, client

    if not media.jellyfin_id:
        raise ValueError(f"{media.title} isn't linked to Jellyfin yet")
    picture = normalise(data)
    if user_id:
        return _for_account(media, picture, source, user_id)
    jellyfin = client("jellyfin")
    current = jellyfin.image(media.jellyfin_id)
    previous = active(media.id)
    showing = bool(current) and _same_picture(picture, current)
    if previous and showing:
        return None  # this picture is already the custom poster
    store = _store()
    record = CustomPoster(id=uuid(), media_identity_id=media.id, source=source)
    if previous:
        record.original_path = previous.original_path
    elif not (adopt and showing):
        original = store / f"{record.id}-original.jpg"
        original.write_bytes(_original(media, jellyfin))
        record.original_path = str(original)
    image = store / f"{record.id}.jpg"
    image.write_bytes(picture)
    record.image_path = str(image)
    db.session.add(record)
    if not showing:
        jellyfin.put_image(media.jellyfin_id, picture, "Primary")
    if previous:
        previous.active = False
    record.applied_at = utcnow()
    audit(
        "custom_poster_applied",
        media.id,
        f"Custom poster for {media.title} ({source}).",
        f"custom-poster:{record.id}",
        payload={"source": source},
    )
    db.session.commit()
    return record


def _for_account(media, picture, source, user_id) -> CustomPoster:
    from .services import audit

    record = CustomPoster(
        id=uuid(), media_identity_id=media.id, source=source, jellyfin_user_id=user_id
    )
    image = _store() / f"{record.id}.jpg"
    image.write_bytes(picture)
    record.image_path = str(image)
    record.applied_at = utcnow()
    previous = active(media.id, user_id)
    if previous:
        previous.active = False
    db.session.add(record)
    audit(
        "custom_poster_applied",
        media.id,
        f"Custom poster for {media.title}, for {account_name(user_id)} only ({source}).",
        f"custom-poster:{record.id}",
        payload={"source": source, "user": user_id},
    )
    db.session.commit()
    return record


def import_for_accounts(media, data: bytes, source: str, accounts) -> bool:
    """An imported picture for some accounts only. Each gets it (unless it
    already has this picture), and if it's everyone's poster in Jellyfin now
    (put on for everyone before), everyone else gets Jellyfin's usual poster
    back. Returns whether anything changed."""
    from .services import client

    if not media.jellyfin_id:
        raise ValueError(f"{media.title} isn't linked to Jellyfin yet")
    picture = normalise(data)
    changed = False
    for user_id in accounts:
        mine = active(media.id, user_id)
        kept = Path(mine.image_path) if mine else None
        if kept and kept.exists() and _same_picture(picture, kept.read_bytes()):
            continue  # already theirs
        _for_account(media, picture, source, user_id)
        changed = True
    everyone = active(media.id)
    if everyone and _same_picture(picture, Path(everyone.image_path).read_bytes()):
        restore(everyone)
        return True
    if everyone is None:
        current = client("jellyfin").image(media.jellyfin_id)
        if current and _same_picture(picture, current):
            _give_back_usual(media)
            return True
    return changed


def _give_back_usual(media) -> None:
    """Jellyfin's poster is a custom picture Curatarr has no record of (put on
    for everyone before): the metadata provider's poster goes back on."""
    from .services import audit, client

    jellyfin = client("jellyfin")
    offered = jellyfin.remote_images(media.jellyfin_id, "Primary")
    if not offered:
        raise ValueError(
            f"Jellyfin's metadata providers offer no poster for {media.title}"
        )
    jellyfin.download_remote_image(media.jellyfin_id, offered[0], "Primary")
    audit(
        "custom_poster_restored",
        media.id,
        f"Everyone else sees {media.title}'s provider poster again; the custom one "
        "is for chosen accounts only.",
        f"custom-poster-usual:{media.id}:{utcnow().isoformat()}",
    )
    db.session.commit()


def account_name(user_id) -> str:
    from .services import setting

    return (setting("jellyfin_user_names", {}) or {}).get(user_id) or "one account"


def for_title(media_id) -> list[CustomPoster]:
    """A title's active custom posters: everyone's first, then each account's."""
    rows = (
        db.session.query(CustomPoster)
        .filter_by(media_identity_id=media_id, active=True)
        .all()
    )
    return sorted(rows, key=lambda p: (p.jellyfin_user_id is not None, p.created_at))


def for_accounts() -> list[CustomPoster]:
    """Every account's own poster, for the Jellyfin plugin."""
    return (
        db.session.query(CustomPoster)
        .join(MediaIdentity, CustomPoster.media_identity_id == MediaIdentity.id)
        .filter(
            CustomPoster.active.is_(True),
            CustomPoster.jellyfin_user_id.isnot(None),
            MediaIdentity.jellyfin_id.isnot(None),
        )
        .all()
    )


def restore(poster: CustomPoster) -> str:
    """Put the original back; returns what was done, in words."""
    from .services import audit, client

    media = poster.media
    if poster.jellyfin_user_id:
        poster.active = False
        poster.restored_at = utcnow()
        who = account_name(poster.jellyfin_user_id)
        said = f"{who} sees {media.title}'s usual poster again."
        audit(
            "custom_poster_restored",
            media.id,
            said,
            f"custom-poster-restored:{poster.id}",
        )
        db.session.commit()
        return said
    jellyfin = client("jellyfin")
    original = Path(poster.original_path) if poster.original_path else None
    if original and original.exists():
        jellyfin.put_image(media.jellyfin_id, original.read_bytes(), "Primary")
        said = f"The original poster of {media.title} is back."
    else:
        offered = jellyfin.remote_images(media.jellyfin_id, "Primary")
        if not offered:
            raise ValueError(
                f"Curatarr has no copy of {media.title}'s original poster, "
                "and Jellyfin's metadata providers offer none"
            )
        jellyfin.download_remote_image(media.jellyfin_id, offered[0], "Primary")
        said = f"{media.title} has its provider's poster again."
    poster.active = False
    poster.restored_at = utcnow()
    audit(
        "custom_poster_restored",
        media.id,
        said,
        f"custom-poster-restored:{poster.id}",
    )
    db.session.commit()
    return said


# --- Import folder -------------------------------------------------------------


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.lower().replace("'", "").replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def parse_name(filename: str) -> tuple[str, int | None]:
    """A file's title words and year: ``08_the-big-lebowski-1998.jpg`` →
    ("the big lebowski", 1998)."""
    stem = LEADING_NUMBER.sub("", Path(filename).stem)
    year = None
    found = YEAR.search(stem)
    if found and found.start() > 0:
        year, stem = int(found.group(1)), stem[: found.start()]
    return _norm(stem.replace("_", " ").replace("-", " ")), year


def _titles():
    """Every linked film and show: (words, title, year from its folder)."""
    rows = (
        db.session.query(MediaIdentity)
        .join(Library, MediaIdentity.library_id == Library.id)
        .filter(
            Library.media_type.in_(("tv", "movies")),
            MediaIdentity.jellyfin_id.isnot(None),
            MediaIdentity.missing_since.is_(None),
        )
        .all()
    )
    years = {}
    for media_id, path in db.session.query(
        MediaPart.media_identity_id, MediaPart.file_path
    ).filter(MediaPart.file_path.isnot(None)):
        if media_id not in years:
            found = re.findall(r"\(((?:19|20)\d\d)\)", path or "")
            if found:
                years[media_id] = int(found[0])
    return [(_norm(m.title), m, years.get(m.id)) for m in rows]


def match(filename: str, titles) -> tuple[MediaIdentity | None, str]:
    """The one title a file's name means, or None and why not."""
    words, year = parse_name(filename)
    if not words:
        return None, "No title in the file name"
    same = [(m, y) for w, m, y in titles if w == words]
    if not same:  # "Birdman" for "Birdman or (The Unexpected Virtue …)"
        same = [(m, y) for w, m, y in titles if w.startswith(words + " ")]
    # Years differ by one between sources (a festival year, a release year:
    # Jellyfin's 2006 for a folder's 2005), so a year either side still counts.
    if year is not None and len(same) > 1:
        dated = [(m, y) for m, y in same if y == year]
        near = [(m, y) for m, y in same if y is not None and abs(y - year) <= 1]
        same = dated or near or [(m, y) for m, y in same if y is None]
    if year is not None and len(same) == 1:
        found = same[0][1]
        if found is not None and abs(found - year) > 1:
            return None, f"The only match is from {found}, not {year}"
    if len(same) == 1:
        return same[0][0], ""
    if not same:
        return None, "No title in the library has this name"
    return None, "Several titles have this name; add the year, e.g. Title (1998)"


def import_state() -> dict:
    from .services import setting

    return {"seen": {}, "unmatched": {}} | (setting(STATE_KEY) or {})


def sweep(now=None) -> int:
    """Worker cycle: put on new pictures from the import folder. Returns how
    many posters changed."""
    from .services import _as_datetime, set_setting

    folder = settings().get("folder")
    accounts = settings().get("accounts") or []
    if not folder:
        return 0
    now = now or utcnow()
    state = import_state()
    checked = _as_datetime(state.get("at"))
    if checked and now - checked < EVERY:
        return 0
    try:
        files = sorted(
            p for p in Path(folder).iterdir() if p.suffix.lower() in SUFFIXES
        )
    except OSError:
        return 0
    seen, unmatched = dict(state["seen"]), dict(state["unmatched"])
    titles, changed, taken = None, 0, 0
    for path in files:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if seen.get(path.name) == mtime or time.time() - mtime < SETTLE:
            continue
        if taken >= BATCH:
            break
        taken += 1
        if titles is None:
            titles = _titles()
        media, why = match(path.name, titles)
        if media is None:
            unmatched[path.name] = why
            seen[path.name] = mtime
            continue
        try:
            if accounts:
                if import_for_accounts(media, path.read_bytes(), path.name, accounts):
                    changed += 1
            elif apply(media, path.read_bytes(), path.name, adopt=True):
                changed += 1
        except IntegrationError:
            db.session.rollback()
            break  # Jellyfin unreachable: this file and the rest next pass
        except (ValueError, OSError) as exc:
            db.session.rollback()
            unmatched[path.name] = str(exc)
        else:
            unmatched.pop(path.name, None)
        seen[path.name] = mtime
    present = {p.name for p in files}
    set_setting(
        STATE_KEY,
        {
            "at": now.isoformat(),
            "seen": {n: m for n, m in seen.items() if n in present},
            "unmatched": {n: w for n, w in unmatched.items() if n in present},
        },
    )
    db.session.commit()
    return changed


def gallery() -> list[CustomPoster]:
    return (
        db.session.query(CustomPoster)
        .filter_by(active=True)
        .order_by(CustomPoster.applied_at.desc())
        .all()
    )


# --- Prompts for an image-making AI ------------------------------------------

PROMPT_DEFAULTS = {
    "titles": "favourites",  # favourites (anyone's), favourites:<user id>, movies, shows
    "skip_done": True,
    "batch": 20,
    "photos": "",
    "people": "",
}
OVERVIEW_CHARS = 280


def prompt_options() -> dict:
    return PROMPT_DEFAULTS | (settings().get("prompts") or {})


def save_prompt_options(form) -> dict:
    from .services import set_setting

    try:
        batch = max(1, min(int(form.get("batch") or 20), 100))
    except ValueError:
        batch = 20
    options = {
        "titles": form.get("titles") or "favourites",
        "skip_done": form.get("skip_done") in {"on", "true", "1"},
        "batch": batch,
        "photos": (form.get("photos") or "").strip(),
        "people": (form.get("people") or "").strip()[:200],
    }
    set_setting(KEY, settings() | {"prompts": options})
    db.session.commit()
    return options


def prompt_titles(which: str, skip_done=True) -> list[MediaIdentity]:
    """The titles to write prompts for, A to Z."""
    from .models import UserMediaState

    query = (
        db.session.query(MediaIdentity)
        .join(Library, MediaIdentity.library_id == Library.id)
        .filter(
            Library.media_type.in_(("tv", "movies")),
            MediaIdentity.jellyfin_id.isnot(None),
            MediaIdentity.missing_since.is_(None),
        )
    )
    if which == "movies":
        query = query.filter(MediaIdentity.media_type == "movie")
    elif which == "shows":
        query = query.filter(MediaIdentity.media_type == "series")
    else:
        favourites = db.session.query(UserMediaState.media_identity_id).filter(
            UserMediaState.favorite.is_(True)
        )
        if which.startswith("favourites:"):
            favourites = favourites.filter(
                UserMediaState.jellyfin_user_id == which.split(":", 1)[1]
            )
        query = query.filter(MediaIdentity.id.in_(favourites))
    titles = query.all()
    if skip_done:
        done = {
            row.media_identity_id
            for row in db.session.query(CustomPoster.media_identity_id).filter_by(
                active=True
            )
        }
        titles = [m for m in titles if m.id not in done]
    return sorted(titles, key=lambda m: _norm(m.sort_title or m.title))


def _slug(text: str) -> str:
    return _norm(text).replace(" ", "-")


def describe(titles) -> list[dict]:
    """Each title with its year, genres, a short plot and the file name to save."""
    from .services import client

    details = {}
    try:
        for item in client("jellyfin").item_details([m.jellyfin_id for m in titles]):
            details[str(item.get("Id") or "").replace("-", "")] = item
    except IntegrationError:
        pass  # titles and years still make a prompt
    folder_years = {m.id: y for _w, m, y in _titles()}
    described = []
    for number, media in enumerate(titles, 1):
        item = details.get(str(media.jellyfin_id).replace("-", ""), {})
        year = item.get("ProductionYear") or folder_years.get(media.id)
        overview = " ".join((item.get("Overview") or "").split())
        if len(overview) > OVERVIEW_CHARS:
            overview = overview[:OVERVIEW_CHARS].rsplit(" ", 1)[0] + "…"
        name = f"{number:02d}_{_slug(media.title)}" + (f"-{year}" if year else "")
        described.append(
            {
                "media": media,
                "number": number,
                "title": media.title,
                "year": year,
                "kind": "TV show" if media.media_type == "series" else "film",
                "genres": (item.get("Genres") or media.genres or [])[:3],
                "overview": overview,
                "file": name + ".jpg",
            }
        )
    return described


def prompts(described, *, batch, photos, people, output) -> list[str]:
    """One prompt per batch, for Codex or another image-making AI agent."""
    chunks = [described[i : i + batch] for i in range(0, len(described), batch)]
    who = people or "the people in them"
    texts = []
    for index, chunk in enumerate(chunks, 1):
        lines = [
            (
                f"You're making custom posters for a home media library. "
                f"Batch {index} of {len(chunks)}: {len(chunk)} title"
                f"{'' if len(chunk) == 1 else 's'}."
            ),
            "",
        ]
        if photos:
            lines += [
                (
                    f"Reference photos are in {photos}. Use them for the likeness of "
                    f"{who}, with their consent. Where a title's story suits it, cast "
                    "them as its lead characters; where it doesn't, make the poster "
                    "without them. Don't use any other real person's likeness, actors "
                    "included."
                ),
                "",
            ]
        else:
            lines += ["Don't use any real person's likeness, actors included.", ""]
        lines += [
            "For each title below, make one poster:",
            "- Portrait 2:3, at least 1024 × 1536, saved as JPEG.",
            (
                "- Its tone, setting and key imagery, with the title on it spelled "
                "exactly as given. Not a copy of the official poster."
            ),
            (
                f"- Save it in {output} under the file name given. Write each file in "
                "one go (or write it elsewhere and move it in): it's picked up as soon "
                "as it stops changing."
            ),
            (
                "- If one comes out wrong (a misspelled title, an unconvincing face), "
                "make it again under the same name."
            ),
            "- When the batch is done, list each file with a line on its concept.",
            "",
            "Titles:",
        ]
        for entry in chunk:
            year = f" ({entry['year']})" if entry["year"] else ""
            about = ", ".join(entry["genres"])
            lines.append(
                f"{entry['number']}. {entry['title']}{year}, {entry['kind']}"
                + (f" ({about})" if about else "")
                + f" → {entry['file']}"
            )
            if entry["overview"]:
                lines.append(f"    {entry['overview']}")
        texts.append("\n".join(lines))
    return texts


def gallery_groups() -> list[dict]:
    """The gallery, one tile per picture: a title's posters that are the same
    picture (one per account, from one import) are shown together. Copies
    are saved identically, so the file's size tells them apart cheaply."""
    groups: dict[tuple, dict] = {}
    for poster in gallery():
        try:
            size = Path(poster.image_path).stat().st_size
        except OSError:
            size = None
        key = (poster.media_identity_id, size if size is not None else poster.id)
        group = groups.setdefault(
            key, {"media": poster.media, "image": poster, "posters": []}
        )
        group["posters"].append(poster)
    for group in groups.values():
        # Everyone's first, then accounts by name.
        group["posters"].sort(
            key=lambda p: (
                p.jellyfin_user_id is not None,
                account_name(p.jellyfin_user_id) if p.jellyfin_user_id else "",
            )
        )
        group["applied_at"] = max(
            (p.applied_at for p in group["posters"] if p.applied_at), default=None
        )
    return sorted(
        groups.values(),
        key=lambda g: g["applied_at"].isoformat() if g["applied_at"] else "",
        reverse=True,
    )


def leaving_label(media_id) -> str | None:
    """The Leaving Soon banner's words for a title ("7 DAYS LEFT"), or None
    when it isn't Leaving Soon: Jellyfin's own poster carries the banner, so
    an account's own poster must too."""
    from .artwork import badge_label
    from .models import PurgeCandidate

    snapshot = (
        db.session.query(PosterSnapshot)
        .filter_by(media_identity_id=media_id, image_type="Primary", active=True)
        .first()
    )
    if not snapshot or not snapshot.candidate_id:
        return None
    candidate = db.session.get(PurgeCandidate, snapshot.candidate_id)
    if not candidate or candidate.state not in {"REVIEW", "LEAVING_SOON"}:
        return None
    return badge_label(candidate)


def account_tag(poster: CustomPoster, label: str | None) -> str:
    """The image tag the plugin uses: new whenever the banner's words change,
    so apps fetch the poster again as the days count down."""
    import hashlib

    from .artwork import BADGE_STYLE

    if not label:
        return poster.id.replace("-", "")
    return hashlib.sha1(f"{poster.id}|{BADGE_STYLE}|{label}".encode()).hexdigest()[:32]


def account_picture(poster: CustomPoster, label: str | None) -> bytes:
    """An account's poster as served to it: with the Leaving Soon banner
    while the title is Leaving Soon."""
    from .artwork import badge_poster

    picture = Path(poster.image_path).read_bytes()
    return badge_poster(picture, label, "Primary") if label else picture


# --- Favourite ribbons -----------------------------------------------------

RIBBON_KEY = "favourite_ribbons"
RIBBON_DEFAULTS = {
    "enabled": False,
    "position": "bottom",  # or "top_right"
    "list_style": "faces",  # posters in lists: ★ and faces, "names" or "star"
    "page_style": "names",  # a title's own page: names listed, or "list" (the same)
    "colour": "gold",  # or silver, teal, purple
    "own": False,  # whether an account sees its own favourites marked
    "sees": {},  # account → the accounts whose favourites it sees
    "new_days": 7,  # a favourite this recent says NEW (0: never)
    "household_at": 3,  # this many people's favourite: a household one (0: off)
    "episodes": True,  # favourite episodes' thumbnails too
}
RIBBON_STYLE = "fav-3"  # part of each tag: a new look fetches every ribbon again
_jellyfin_tags: dict[str, str] = {}  # last known image tags, while Jellyfin is away
_avatar_tags: dict[str, str] = {}  # Jellyfin profile pictures' tags


def ribbon_settings() -> dict:
    from .services import setting

    saved = dict(setting(RIBBON_KEY) or {})
    if "label" in saved and "list_style" not in saved:  # settings from 0.2.0rc6
        star = saved["label"] == "star"
        saved["list_style"] = (
            "star" if star else "faces" if saved.get("avatars", True) else "names"
        )
        saved["page_style"] = "list" if star else "names"
    return RIBBON_DEFAULTS | saved


def _number(value, default, low, high) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def _choice(value, allowed):
    return value if value in allowed else allowed[0]


def save_ribbon_settings(form, accounts) -> dict:
    from .services import set_setting

    known = [a for a, _name in accounts]
    ticked = lambda name: form.get(name) in {"on", "true", "1"}
    options = {
        "enabled": ticked("enabled"),
        "position": "top_right" if form.get("position") == "top_right" else "bottom",
        "list_style": _choice(form.get("list_style"), ("faces", "names", "star")),
        "page_style": _choice(form.get("page_style"), ("names", "list")),
        "colour": _choice(form.get("colour"), ("gold", "silver", "teal", "purple")),
        "own": ticked("own"),
        # "Everyone sees Sam's favourites", or account by account.
        "sees": {
            viewer: [
                who
                for who in known
                if who != viewer
                and (form.get(f"see-{viewer}-{who}") or form.get(f"all-{who}"))
            ]
            for viewer in known
        },
        "new_days": _number(form.get("new_days"), 7, 0, 60) if ticked("new") else 0,
        "household_at": (
            _number(form.get("household_at"), 3, 2, 20) if ticked("household") else 0
        ),
        "episodes": ticked("episodes"),
    }
    set_setting(RIBBON_KEY, options)
    db.session.commit()
    return options


def ribbon_text(names: list[str], style: str, household: bool = False) -> str:
    if household:
        return "♥ Household favourite"
    if style == "star" or not names:
        return "★"
    return f"★ {names[0]}" + (f" + {len(names) - 1}" if len(names) > 1 else "")


def has_large(options, household: bool = False, kind: str = "t") -> bool:
    """Whether a title's ribbon looks different on its own page (names
    listed). Episodes' thumbnails show in one size only."""
    return kind == "t" and (options["page_style"] == "names" or household)


def draw_ribbon(base, users, names, options, *, new, household, large=False):
    """A poster or thumbnail with the ribbon these options give it."""
    from .artwork import favourite_ribbon

    faces = (
        [(avatar_picture(u), name) for u, name in zip(users, names, strict=True)]
        if options["list_style"] == "faces"
        else ()
    )
    return favourite_ribbon(
        base,
        ribbon_text(names, options["list_style"], household),
        options["position"],
        avatars=faces,
        new=new,
        household=household,
        names=names if large and has_large(options, household) else (),
        large=large,
        colour=options["colour"],
    )


def _since(value):
    from .services import _as_datetime

    return _as_datetime(value)


def ribbons(options=None) -> dict[tuple[str, str, str], dict]:
    """(kind, account, id) → that account's ribbon on a title ("t", a
    MediaIdentity id) or an episode ("e", a MediaPart id): whose favourite
    it is among the accounts it sees, whether it's NEW, and whether it's a
    household favourite."""
    from .models import EpisodeUserState, UserMediaState

    options = options or ribbon_settings()
    if not options["enabled"]:
        return {}
    liked: dict[tuple[str, str], dict[str, object]] = {}  # (kind, id) → user → since
    for media_id, user_id, since in (
        db.session.query(
            UserMediaState.media_identity_id,
            UserMediaState.jellyfin_user_id,
            UserMediaState.favorite_since,
        )
        .join(MediaIdentity, UserMediaState.media_identity_id == MediaIdentity.id)
        .filter(
            UserMediaState.favorite.is_(True), MediaIdentity.jellyfin_id.isnot(None)
        )
    ):
        liked.setdefault(("t", media_id), {})[user_id] = since
    if options["episodes"]:
        for part_id, user_id, since in (
            db.session.query(
                EpisodeUserState.media_part_id,
                EpisodeUserState.jellyfin_user_id,
                EpisodeUserState.favorite_since,
            )
            .join(MediaPart, EpisodeUserState.media_part_id == MediaPart.id)
            .filter(
                EpisodeUserState.favorite.is_(True), MediaPart.jellyfin_id.isnot(None)
            )
        ):
            liked.setdefault(("e", part_id), {})[user_id] = since
    recent = utcnow() - timedelta(days=options["new_days"] or 0)
    household_at = options["household_at"] or 0
    found = {}
    for viewer, seen in (options["sees"] or {}).items():
        shown = set(seen) | ({viewer} if options["own"] else set())
        if not seen and not options["own"]:
            continue  # ribbons aren't set up for this account
        for (kind, item), users in liked.items():
            household = bool(household_at) and len(users) >= household_at
            who = sorted(users if household else set(users) & shown, key=account_name)
            if not who:
                continue
            new = bool(options["new_days"]) and any(
                (_since(users[u]) or recent) > recent for u in who
            )
            found[(kind, viewer, item)] = {
                "users": who,
                "names": [account_name(u) for u in who],
                "new": new,
                "household": household,
            }
    return found


def _image_tags(jellyfin_ids) -> dict[str, str]:
    """Jellyfin's current Primary image tag of each item (it changes when the
    picture does, Leaving Soon's banner included)."""
    from .services import client

    try:
        ids = list(jellyfin_ids)
        for start in range(0, len(ids), 100):
            for item in client("jellyfin").item_details(ids[start : start + 100]):
                tag = (item.get("ImageTags") or {}).get("Primary")
                if tag:
                    _jellyfin_tags[str(item.get("Id") or "").replace("-", "")] = tag
    except IntegrationError:
        pass  # the last known tags stand in
    return _jellyfin_tags


def _avatar_dir() -> Path:
    path = Path(current_app.instance_path) / "avatars"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_avatar(user_id: str, data: bytes | None) -> None:
    """An account's picture for ribbons (None: back to Jellyfin's, or initials)."""
    target = _avatar_dir() / f"{user_id}.jpg"
    if data is None:
        target.unlink(missing_ok=True)
        return
    picture = Image.open(BytesIO(normalise(data))).convert("RGB")
    side = min(picture.size)
    left, top = (picture.width - side) // 2, (picture.height - side) // 3
    picture = picture.crop((left, top, left + side, top + side)).resize((256, 256))
    output = BytesIO()
    picture.save(output, format="JPEG", quality=90)
    target.write_bytes(output.getvalue())


def has_uploaded_avatar(user_id: str) -> bool:
    return (_avatar_dir() / f"{user_id}.jpg").exists()


def avatar_picture(user_id: str) -> bytes | None:
    """The picture on a person's ribbon: one uploaded in Curatarr, else their
    Jellyfin profile picture, else None (their initials are drawn)."""
    from .services import client

    uploaded = _avatar_dir() / f"{user_id}.jpg"
    if uploaded.exists():
        return uploaded.read_bytes()
    if not _avatar_tags.get(user_id):
        return None
    try:
        return client("jellyfin").user_image(user_id)
    except IntegrationError:
        return None


def _avatar_version(user_id: str) -> str:
    uploaded = _avatar_dir() / f"{user_id}.jpg"
    if uploaded.exists():
        return f"up{int(uploaded.stat().st_mtime)}"
    return _avatar_tags.get(user_id) or "initials"


def _refresh_avatar_tags() -> None:
    from .services import client

    try:
        for user in client("jellyfin").users() or []:
            _avatar_tags[str(user["Id"])] = user.get("PrimaryImageTag") or ""
    except IntegrationError:
        pass


def plugin_list() -> list[dict]:
    """What the Curatarr plugin shows each account instead of the usual
    picture: its own custom posters, and favourite ribbons on titles and
    episodes."""
    import hashlib
    import json

    bare = lambda value: str(value or "").replace("-", "").lower()
    options = ribbon_settings()
    marked = ribbons(options)
    own = {(p.jellyfin_user_id, p.media_identity_id): p for p in for_accounts()}
    title_ids = {i for k, _v, i in marked if k == "t"} | {m for _v, m in own}
    medias = {
        m.id: m
        for m in db.session.query(MediaIdentity).filter(
            MediaIdentity.id.in_(title_ids or {""})
        )
    }
    part_ids = {i for k, _v, i in marked if k == "e"}
    parts = {
        p.id: p
        for p in db.session.query(MediaPart).filter(MediaPart.id.in_(part_ids or {""}))
    }
    needs_tag = {
        medias[i].jellyfin_id
        for k, v, i in marked
        if k == "t" and (v, i) not in own and i in medias
    } | {parts[i].jellyfin_id for k, _v, i in marked if k == "e" and i in parts}
    tags = _image_tags(needs_tag) if needs_tag else _jellyfin_tags
    if marked and options["list_style"] == "faces":
        _refresh_avatar_tags()
    leaving: dict[str, str | None] = {}
    entries = []

    def label_for(media_id):
        if media_id not in leaving:
            leaving[media_id] = leaving_label(media_id)
        return leaving[media_id]

    for (viewer, media_id), poster in own.items():
        media = medias.get(media_id)
        if media and media.jellyfin_id and ("t", viewer, media_id) not in marked:
            entries.append(
                {
                    "item": bare(media.jellyfin_id),
                    "user": bare(viewer),
                    "id": poster.id,
                    "tag": account_tag(poster, label_for(media_id)),
                }
            )
    for (kind, viewer, item_id), ribbon in sorted(marked.items()):
        if kind == "t":
            media = medias.get(item_id)
            if media is None or not media.jellyfin_id:
                continue
            jellyfin_id, poster = media.jellyfin_id, own.get((viewer, item_id))
            base = poster.id if poster else tags.get(bare(jellyfin_id))
            banner = label_for(item_id)
        else:
            part = parts.get(item_id)
            if part is None or not part.jellyfin_id:
                continue
            jellyfin_id, base, banner = (
                part.jellyfin_id,
                tags.get(bare(part.jellyfin_id)),
                None,
            )
        if base is None:
            continue  # Jellyfin's picture unknown for now: the usual one shows
        spec = json.dumps(
            [
                base,
                banner,
                ribbon,
                options["position"],
                options["list_style"],
                options["page_style"],
                options["colour"],
                [_avatar_version(u) for u in ribbon["users"]]
                if options["list_style"] == "faces"
                else None,
                RIBBON_STYLE,
            ],
            sort_keys=True,
        )
        entries.append(
            {
                "item": bare(jellyfin_id),
                "user": bare(viewer),
                "id": f"{'v' if kind == 't' else 'e'}.{viewer}.{item_id}",
                "tag": hashlib.sha1(spec.encode()).hexdigest()[:32],
                # A second picture, listing the names, for where it's shown big.
                "large": has_large(options, ribbon["household"], kind),
            }
        )
    return entries


def plugin_picture(entry_id: str, large: bool = False) -> bytes | None:
    """The picture for one of plugin_list's entries, or None if it's gone.
    ``large``: a ribbon as shown big (on a title's own page), with names."""
    from .artwork import _jpeg
    from .services import client

    if not entry_id.startswith(("v.", "e.")):
        poster = db.session.get(CustomPoster, entry_id)
        if not poster or not poster.active or not Path(poster.image_path).exists():
            return None
        return account_picture(poster, leaving_label(poster.media_identity_id))
    kind, viewer, item_id = entry_id.split(".", 2)
    options = ribbon_settings()
    ribbon = ribbons(options).get(("t" if kind == "v" else "e", viewer, item_id))
    if not ribbon:
        return None
    if kind == "v":
        media = db.session.get(MediaIdentity, item_id)
        if media is None or not media.jellyfin_id:
            return None
        poster = active(item_id, viewer)
        if poster and Path(poster.image_path).exists():
            base = account_picture(poster, leaving_label(item_id))
        else:
            base = _jpeg(client("jellyfin").image(media.jellyfin_id))
    else:
        part = db.session.get(MediaPart, item_id)
        if part is None or not part.jellyfin_id:
            return None
        base = _jpeg(client("jellyfin").image(part.jellyfin_id))
    return draw_ribbon(
        base,
        ribbon["users"],
        ribbon["names"],
        options,
        new=ribbon["new"],
        household=ribbon["household"],
        large=large and kind == "v",
    )
