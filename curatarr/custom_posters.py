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

A title's other pictures work the same way (``KINDS``, Jellyfin's image
types): its wide tile (Thumb), background (Backdrop), logo and banner. An
imported file says which with a word before its extension
(``08_the-big-lebowski.thumb.jpg``, ``….logo.png``); without one it's the
poster. A logo stays a PNG, keeping its see-through background.
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
# Jellyfin's image types Curatarr manages, and what people call them.
KINDS = ("Primary", "Thumb", "Backdrop", "Logo", "Banner")
KIND_NAMES = {
    "Primary": "poster",
    "Thumb": "wide tile",
    "Backdrop": "background",
    "Logo": "logo",
    "Banner": "banner",
}
# The word before a file's extension that says which picture it is.
FILE_KINDS = {
    "poster": "Primary",
    "thumb": "Thumb",
    "backdrop": "Backdrop",
    "background": "Backdrop",
    "logo": "Logo",
    "banner": "Banner",
}
FILE_WORDS = {
    "Thumb": "thumb",
    "Backdrop": "backdrop",
    "Logo": "logo",
    "Banner": "banner",
}
# Bigger pictures are scaled down to these.
LARGEST_OF = {
    "Primary": (2000, 3000),
    "Thumb": (3840, 2160),
    "Backdrop": (3840, 2160),
    "Logo": (2000, 1000),
    "Banner": (3000, 600),
}
LARGEST = LARGEST_OF["Primary"]
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


# Without a picture of its own, what an account with its own poster (or a
# ribbon) sees for each other type: one made from its poster, or Jellyfin's.
FALLBACK_CHOICES = {
    "Thumb": ("widened", "own"),
    "Backdrop": ("own", "blurred"),
    "Banner": ("own", "widened"),
}


def fallbacks() -> dict:
    chosen = settings().get("fallbacks") or {}
    return {
        kind: chosen.get(kind) if chosen.get(kind) in options else options[0]
        for kind, options in FALLBACK_CHOICES.items()
    }


def save_fallbacks(form) -> dict:
    from .services import set_setting

    chosen = {
        kind: form.get(kind) if form.get(kind) in options else options[0]
        for kind, options in FALLBACK_CHOICES.items()
    }
    set_setting(KEY, settings() | {"fallbacks": chosen})
    db.session.commit()
    return chosen


def _store() -> Path:
    path = Path(current_app.instance_path) / "custom_posters"
    path.mkdir(parents=True, exist_ok=True)
    return path


def mimetype(kind: str) -> str:
    return "image/png" if kind == "Logo" else "image/jpeg"


def _extension(kind: str) -> str:
    return ".png" if kind == "Logo" else ".jpg"


def normalise(data: bytes, kind: str = "Primary") -> bytes:
    """The picture as a JPEG (a logo: a PNG, see-through parts kept) no
    bigger than the type's largest size; ValueError if it isn't one."""
    try:
        with Image.open(BytesIO(data)) as probe:
            if probe.width * probe.height > MAX_PIXELS:
                raise ValueError("That picture is too large")
            probe.verify()
        image = Image.open(BytesIO(data))
        image = image.convert("RGBA" if kind == "Logo" else "RGB")
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError("That file isn't a picture Curatarr can read") from exc
    image.thumbnail(LARGEST_OF.get(kind, LARGEST))
    output = BytesIO()
    if kind == "Logo":
        image.save(output, format="PNG", optimize=True)
    else:
        image.save(output, format="JPEG", quality=90, optimize=True)
    return output.getvalue()


def active(media_id, user_id=None, kind="Primary") -> CustomPoster | None:
    return (
        db.session.query(CustomPoster)
        .filter_by(
            media_identity_id=media_id,
            jellyfin_user_id=user_id,
            image_type=kind,
            active=True,
        )
        .order_by(CustomPoster.created_at.desc())
        .first()
    )


def _noun(kind: str) -> str:
    return KIND_NAMES.get(kind, "picture")


def _current(jellyfin, item_id, kind) -> bytes | None:
    """The picture Jellyfin shows now, or None when the title has none of
    that type (most have no logo or banner)."""
    try:
        return jellyfin.image(item_id, kind) or None
    except IntegrationError as exc:
        if kind == "Primary" or exc.status_code != 404:
            raise
        return None


def _put_picture(jellyfin, item_id, data, kind, replacing=None):
    """Upload one of a title's pictures. A background is added to the
    title's backgrounds rather than replacing one, so copies of
    ``replacing`` are deleted afterwards and the new one put first."""
    from .artwork import _put

    if kind == "Logo":
        return jellyfin.put_image(item_id, data, kind, mimetype="image/png")
    if kind == "Backdrop" and replacing:
        return _put(jellyfin, item_id, data, kind, replacing)
    return jellyfin.put_image(item_id, data, kind)


def _same_picture(a: bytes, b: bytes) -> bool:
    from .artwork import _difference, _grey

    size = (24, 36)
    return _difference(_grey(a, size), _grey(b, size), size) < SAME


def _original(media, jellyfin, kind="Primary") -> bytes | None:
    """The picture from before any custom one: during Leaving Soon, the one
    saved before the banner went on. None when the title had none."""
    from .artwork import _jpeg

    snapshot = (
        db.session.query(PosterSnapshot)
        .filter_by(media_identity_id=media.id, image_type=kind, active=True)
        .first()
    )
    if snapshot and snapshot.original_image_bytes_path:
        saved = Path(snapshot.original_image_bytes_path)
        if saved.exists():
            return saved.read_bytes()
    current = _current(jellyfin, media.jellyfin_id, kind)
    if current is None:
        return None
    return current if kind == "Logo" else _jpeg(current)


def apply(
    media, data: bytes, source: str, *, adopt=False, user_id=None, kind="Primary"
) -> CustomPoster | None:
    """Put ``data`` on as the title's poster (or its ``kind`` of picture) in
    Jellyfin. With ``adopt`` (an import), a picture already showing is only
    recorded. Returns the record, or None when nothing changed (the same
    picture is on already).

    With ``user_id`` it's that account's only: Jellyfin's own picture stays,
    and the Curatarr plugin shows this one to that account."""
    from .services import audit, client

    if kind not in KINDS:
        raise ValueError(f"Curatarr doesn't manage {kind} pictures")
    if not media.jellyfin_id:
        raise ValueError(f"{media.title} isn't linked to Jellyfin yet")
    picture = normalise(data, kind)
    if user_id:
        return _for_account(media, picture, source, user_id, kind)
    jellyfin = client("jellyfin")
    current = _current(jellyfin, media.jellyfin_id, kind)
    previous = active(media.id, kind=kind)
    showing = bool(current) and _same_picture(picture, current)
    if previous and showing:
        return None  # this picture is already the custom one
    store = _store()
    record = CustomPoster(
        id=uuid(), media_identity_id=media.id, source=source, image_type=kind
    )
    if previous:
        record.original_path = previous.original_path
    elif not (adopt and showing):
        before = _original(media, jellyfin, kind)
        if before is None:
            record.original_path = ""  # it had none: restoring removes this one
        else:
            original = store / f"{record.id}-original{_extension(kind)}"
            original.write_bytes(before)
            record.original_path = str(original)
    image = store / f"{record.id}{_extension(kind)}"
    image.write_bytes(picture)
    record.image_path = str(image)
    db.session.add(record)
    if not showing:
        _put_picture(jellyfin, media.jellyfin_id, picture, kind, current)
    if previous:
        previous.active = False
    record.applied_at = utcnow()
    audit(
        "custom_poster_applied",
        media.id,
        f"Custom {_noun(kind)} for {media.title} ({source}).",
        f"custom-poster:{record.id}",
        payload={"source": source, "kind": kind},
    )
    db.session.commit()
    return record


def _for_account(media, picture, source, user_id, kind="Primary") -> CustomPoster:
    from .services import audit

    record = CustomPoster(
        id=uuid(),
        media_identity_id=media.id,
        source=source,
        jellyfin_user_id=user_id,
        image_type=kind,
    )
    image = _store() / f"{record.id}{_extension(kind)}"
    image.write_bytes(picture)
    record.image_path = str(image)
    record.applied_at = utcnow()
    previous = active(media.id, user_id, kind)
    if previous:
        previous.active = False
    db.session.add(record)
    audit(
        "custom_poster_applied",
        media.id,
        f"Custom {_noun(kind)} for {media.title}, for {account_name(user_id)} "
        f"only ({source}).",
        f"custom-poster:{record.id}",
        payload={"source": source, "user": user_id, "kind": kind},
    )
    db.session.commit()
    return record


def import_for_accounts(
    media, data: bytes, source: str, accounts, kind="Primary"
) -> bool:
    """An imported picture for some accounts only. Each gets it (unless it
    already has this picture), and if it's everyone's in Jellyfin now (put on
    for everyone before), everyone else gets Jellyfin's usual one back.
    Returns whether anything changed."""
    from .services import client

    if not media.jellyfin_id:
        raise ValueError(f"{media.title} isn't linked to Jellyfin yet")
    picture = normalise(data, kind)
    changed = False
    for user_id in accounts:
        mine = active(media.id, user_id, kind)
        kept = Path(mine.image_path) if mine else None
        if kept and kept.exists() and _same_picture(picture, kept.read_bytes()):
            continue  # already theirs
        _for_account(media, picture, source, user_id, kind)
        changed = True
    everyone = active(media.id, kind=kind)
    if everyone and _same_picture(picture, Path(everyone.image_path).read_bytes()):
        restore(everyone)
        return True
    if everyone is None:
        current = _current(client("jellyfin"), media.jellyfin_id, kind)
        if current and _same_picture(picture, current):
            _give_back_usual(media, kind, current)
            return True
    return changed


def _give_back_usual(media, kind="Primary", ours=None) -> None:
    """Jellyfin's picture is a custom one Curatarr has no record of (put on
    for everyone before): the metadata provider's goes back on, or, when
    they offer none, the title goes without (a logo or banner)."""
    from .services import audit, client

    jellyfin = client("jellyfin")
    noun = _noun(kind)
    if not _provider_picture(jellyfin, media, kind, ours):
        if kind == "Primary":
            raise ValueError(
                f"Jellyfin's metadata providers offer no poster for {media.title}"
            )
        jellyfin.delete_image(media.jellyfin_id, kind, 0)
    audit(
        "custom_poster_restored",
        media.id,
        f"Everyone else sees {media.title}'s usual {noun} again; the custom one "
        "is for chosen accounts only.",
        f"custom-poster-usual:{media.id}:{kind}:{utcnow().isoformat()}",
    )
    db.session.commit()


def _provider_picture(jellyfin, media, kind, ours=None) -> bool:
    """Put the metadata provider's first picture of ``kind`` on; False when
    they offer none. A background is added beside the others, so the copy of
    ``ours`` is deleted afterwards."""
    from .artwork import _backdrops, _tidy_backdrops

    offered = jellyfin.remote_images(media.jellyfin_id, kind)
    if not offered:
        return False
    before = None
    if kind == "Backdrop" and ours:
        before = {i.get("ImageTag") for i in _backdrops(jellyfin, media.jellyfin_id)}
    jellyfin.download_remote_image(media.jellyfin_id, offered[0], kind)
    if before is not None:
        _tidy_backdrops(jellyfin, media.jellyfin_id, ours, ours, before)
    return True


def account_name(user_id) -> str:
    from .services import setting

    return (setting("jellyfin_user_names", {}) or {}).get(user_id) or "one account"


def for_title(media_id, kind="Primary") -> list[CustomPoster]:
    """A title's active custom pictures of one type (every type with None):
    by type, everyone's first, then each account's."""
    query = db.session.query(CustomPoster).filter_by(
        media_identity_id=media_id, active=True
    )
    if kind:
        query = query.filter_by(image_type=kind)
    return sorted(
        query.all(),
        key=lambda p: (
            KINDS.index(p.image_type) if p.image_type in KINDS else len(KINDS),
            p.jellyfin_user_id is not None,
            p.created_at,
        ),
    )


def for_accounts(kind="Primary") -> list[CustomPoster]:
    """Every account's own pictures of one type (every type with None), for
    the Jellyfin plugin."""
    query = (
        db.session.query(CustomPoster)
        .join(MediaIdentity, CustomPoster.media_identity_id == MediaIdentity.id)
        .filter(
            CustomPoster.active.is_(True),
            CustomPoster.jellyfin_user_id.isnot(None),
            MediaIdentity.jellyfin_id.isnot(None),
        )
    )
    if kind:
        query = query.filter(CustomPoster.image_type == kind)
    return query.all()


def restore(poster: CustomPoster) -> str:
    """Put the original back; returns what was done, in words."""
    from .services import audit, client

    media = poster.media
    kind = poster.image_type or "Primary"
    noun = _noun(kind)
    if poster.jellyfin_user_id:
        poster.active = False
        poster.restored_at = utcnow()
        who = account_name(poster.jellyfin_user_id)
        said = f"{who} sees {media.title}'s usual {noun} again."
        audit(
            "custom_poster_restored",
            media.id,
            said,
            f"custom-poster-restored:{poster.id}",
        )
        db.session.commit()
        return said
    jellyfin = client("jellyfin")
    try:
        ours = Path(poster.image_path).read_bytes()
    except OSError:
        ours = None
    original = Path(poster.original_path) if poster.original_path else None
    if original and original.exists():
        _put_picture(jellyfin, media.jellyfin_id, original.read_bytes(), kind, ours)
        said = f"The original {noun} of {media.title} is back."
    elif poster.original_path == "":
        jellyfin.delete_image(media.jellyfin_id, kind, 0)
        said = f"{media.title} has no {noun} again, as before."
    elif _provider_picture(jellyfin, media, kind, ours):
        said = f"{media.title} has its provider's {noun} again."
    elif kind != "Primary":
        jellyfin.delete_image(media.jellyfin_id, kind, 0)
        said = f"{media.title}'s custom {noun} is gone; its providers offer none."
    else:
        raise ValueError(
            f"Curatarr has no copy of {media.title}'s original poster, "
            "and Jellyfin's metadata providers offer none"
        )
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


def split_kind(filename: str) -> tuple[str, str]:
    """Which picture a file is, and its name without the word saying so:
    ``08_film.thumb.jpg`` → ("Thumb", "08_film.jpg"); no word: the poster."""
    path = Path(filename)
    stem, _dot, word = path.stem.rpartition(".")
    if stem and word.lower() in FILE_KINDS:
        return FILE_KINDS[word.lower()], stem + path.suffix
    return "Primary", filename


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
        kind, name = split_kind(path.name)
        media, why = match(name, titles)
        if media is None:
            unmatched[path.name] = why
            seen[path.name] = mtime
            continue
        try:
            if accounts:
                if import_for_accounts(
                    media, path.read_bytes(), path.name, accounts, kind
                ):
                    changed += 1
            elif apply(media, path.read_bytes(), path.name, adopt=True, kind=kind):
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


def retire_gone() -> int:
    """Worker cycle: a title that's gone (deleted, by Curatarr or outside it,
    or no longer in Radarr or Sonarr) shows no pictures any more, so its
    custom ones are retired and Curatarr's copies deleted; the plugin drops
    its own at its next sync. The import folder's file stays: it's yours.
    Returns how many titles were tidied."""
    from .models import PurgeCandidate
    from .services import audit

    with_pictures = {
        media_id
        for (media_id,) in db.session.query(CustomPoster.media_identity_id).filter_by(
            active=True
        )
    }
    if not with_pictures:
        return 0
    deleted = {
        media_id
        for (media_id,) in db.session.query(PurgeCandidate.media_identity_id).filter(
            PurgeCandidate.media_identity_id.in_(with_pictures),
            PurgeCandidate.state == "COMPLETED",
            PurgeCandidate.target_part_ids.is_(None),
        )
    }
    on_disk = {
        media_id
        for (media_id,) in db.session.query(MediaPart.media_identity_id).filter(
            MediaPart.media_identity_id.in_(with_pictures),
            MediaPart.has_file.is_(True),
        )
    }
    missing = {
        media.id
        for media in db.session.query(MediaIdentity).filter(
            MediaIdentity.id.in_(with_pictures),
            MediaIdentity.missing_since.isnot(None),
        )
    }
    gone = (deleted - on_disk) | missing
    for media_id in sorted(gone):
        media = db.session.get(MediaIdentity, media_id)
        rows = db.session.query(CustomPoster).filter_by(media_identity_id=media_id)
        retired = 0
        for poster in rows:
            if poster.active:
                poster.active = False
                poster.restored_at = utcnow()
                retired += 1
            for path in (poster.image_path, poster.original_path):
                if path:
                    Path(path).unlink(missing_ok=True)
        audit(
            "custom_pictures_retired",
            media_id,
            f"{media.title if media else 'A title'} is gone, so its {retired} custom "
            f"picture{' was' if retired == 1 else 's were'} removed.",
            f"custom-pictures-retired:{media_id}:{utcnow().date().isoformat()}",
        )
        db.session.commit()
    return len(gone)


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
    "kinds": ["Primary"],  # which pictures to ask for, as a matching set
    "style": None,  # the look asked for; None: DEFAULT_STYLE, "": none
}
DEFAULT_STYLE = (
    "An extreme, heightened take on the story, like an over-the-top movie "
    "poster: big, intense facial expressions (terror, joy, rage, grief, "
    "romantic or sexual tension, whatever the story runs on), pushed further "
    "than the film itself, with bold, dramatic composition and lighting."
)
OVERVIEW_CHARS = 280
# Left to "lettering in the film's style", logos came out as plain fonts; and
# with the pictures made without text, an agent typed every title on in the
# same serif font with code.
LETTERING = (
    "- The title is in the title's own logo lettering, as on its official "
    "poster and title card: the same typeface (or the closest match), weight, "
    "letter shapes, spacing, colours and effects (chrome, cracks, glow, drips, "
    "whatever it has). Look it up if you can. Never a plain or generic font. "
    "Make the lettering with the image tool, as part of each picture (the logo "
    "with a transparent background), not typed on afterwards with code or a "
    "font file."
)
# What each picture should be, for the prompts.
KIND_SPECS = {
    "Primary": (
        "Poster: portrait 2:3, at least 1024 × 1536, JPEG, with the title on it."
    ),
    "Thumb": (
        "Wide tile: landscape 16:9, at least 1920 × 1080, JPEG, with the title on "
        "it, large enough to read when the picture is shown small."
    ),
    "Backdrop": (
        "Background: landscape 16:9, at least 1920 × 1080, JPEG, with no text at "
        "all; keep the lower left fairly calm, as apps lay the title and plot "
        "over it. A place or moment from the story; people only if their faces "
        "are large and clear enough to recognise."
    ),
    "Logo": (
        "Logo: only the title, in the title's own lettering (below), on a "
        "transparent background, PNG, at least 1000 pixels wide; nothing else "
        "in it."
    ),
    "Banner": (
        "Banner: a wide strip, 1000 × 185 (about 5.4:1), JPEG, with the title on "
        "it; too small for faces, so a setting or object from the story."
    ),
}


def prompt_options() -> dict:
    options = PROMPT_DEFAULTS | (settings().get("prompts") or {})
    if options["style"] is None:
        options["style"] = DEFAULT_STYLE
    return options


def save_prompt_options(form) -> dict:
    """Saves the prompt options; ValueError (after saving) when the reference
    photos folder isn't there, as the prompts would send the AI to nothing."""
    from .services import set_setting

    try:
        batch = max(1, min(int(form.get("batch") or 20), 100))
    except ValueError:
        batch = 20
    kinds = [k for k in KINDS if k in form.getlist("kinds")] or ["Primary"]
    options = {
        "titles": form.get("titles") or "favourites",
        "skip_done": form.get("skip_done") in {"on", "true", "1"},
        "batch": batch,
        "photos": (form.get("photos") or "").strip(),
        "people": (form.get("people") or "").strip()[:200],
        "kinds": kinds,
        "style": " ".join((form.get("style") or "").split())[:600],
    }
    set_setting(KEY, settings() | {"prompts": options})
    db.session.commit()
    if options["photos"] and not Path(options["photos"]).is_dir():
        raise ValueError(
            f"Curatarr can't find the reference photos folder {options['photos']}. "
            "The prompts send the AI agent there, so check the path (the agent "
            "reads it, so it must be a folder the agent can open)."
        )
    return options


def prompt_titles(
    which: str, skip_done=True, kinds=("Primary",)
) -> list[MediaIdentity]:
    """The titles to write prompts for, A to Z: ones with a file on disk (a
    deleted title needs no pictures). ``skip_done`` leaves out those that
    already have a custom picture of every type asked for."""
    from .models import UserMediaState

    on_disk = db.session.query(MediaPart.media_identity_id).filter(
        MediaPart.has_file.is_(True)
    )
    query = (
        db.session.query(MediaIdentity)
        .join(Library, MediaIdentity.library_id == Library.id)
        .filter(
            Library.media_type.in_(("tv", "movies")),
            MediaIdentity.jellyfin_id.isnot(None),
            MediaIdentity.missing_since.is_(None),
            MediaIdentity.id.in_(on_disk),
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
        have: dict[str, set] = {}
        for media_id, kind in db.session.query(
            CustomPoster.media_identity_id, CustomPoster.image_type
        ).filter_by(active=True):
            have.setdefault(media_id, set()).add(kind)
        titles = [m for m in titles if not set(kinds) <= have.get(m.id, set())]
    return sorted(titles, key=lambda m: _norm(m.sort_title or m.title))


def _slug(text: str) -> str:
    return _norm(text).replace(" ", "-")


def file_name(base: str, kind: str) -> str:
    """The file name to save a title's picture of ``kind`` under."""
    if kind == "Primary":
        return base + ".jpg"
    return f"{base}.{FILE_WORDS[kind]}{_extension(kind)}"


def _made(titles) -> dict[str, dict[str, CustomPoster]]:
    """Each title's active custom pictures by type (anyone's)."""
    found: dict[str, dict[str, CustomPoster]] = {}
    rows = db.session.query(CustomPoster).filter(
        CustomPoster.active.is_(True),
        CustomPoster.media_identity_id.in_([m.id for m in titles] or [""]),
    )
    for poster in rows.order_by(CustomPoster.created_at):
        found.setdefault(poster.media_identity_id, {})[poster.image_type] = poster
    return found


def describe(titles, kinds=("Primary",), skip_done=False) -> list[dict]:
    """Each title with its year, genres, a short plot and the file names to
    save (``file``: the poster's; ``files``: each type asked for). With
    ``skip_done``, only the types it has no custom picture of yet; ``made``
    is then the import folder's file of the poster it has, for the rest to
    match, and its name is the one the new files are given."""
    from .services import client

    details = {}
    try:
        for item in client("jellyfin").item_details([m.jellyfin_id for m in titles]):
            details[str(item.get("Id") or "").replace("-", "")] = item
    except IntegrationError:
        pass  # titles and years still make a prompt
    folder_years = {m.id: y for _w, m, y in _titles()}
    made = _made(titles) if skip_done else {}
    folder = settings().get("folder")
    described = []
    for number, media in enumerate(titles, 1):
        item = details.get(str(media.jellyfin_id).replace("-", ""), {})
        year = item.get("ProductionYear") or folder_years.get(media.id)
        overview = " ".join((item.get("Overview") or "").split())
        if len(overview) > OVERVIEW_CHARS:
            overview = overview[:OVERVIEW_CHARS].rsplit(" ", 1)[0] + "…"
        name = f"{number:02d}_{_slug(media.title)}" + (f"-{year}" if year else "")
        have = made.get(media.id, {}) if skip_done else {}
        needed = [kind for kind in kinds if kind not in have] or list(kinds)
        poster = have.get("Primary")
        source = poster.source if poster else None
        if source and split_kind(source)[0] == "Primary" and source != "Uploaded":
            name = Path(source).stem  # beside its poster in the import folder
        else:
            source = None
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
                "files": {kind: file_name(name, kind) for kind in needed},
                "made": source
                if source
                and "Primary" not in needed
                and folder
                and (Path(folder) / source).is_file()
                else None,
            }
        )
    return described


def _casting(photos: str, who: str, single: bool) -> list[str]:
    """How to cast the people in the reference photos, keeping them
    recognisable: an image model left to a description makes generic faces
    with their general look, or puts them small and turned away."""
    pictures = "poster" if single else "pictures"
    return [
        (
            f"Reference photos of {who}, who've agreed to this, are in {photos}. "
            "Each photo's file name says who's in it."
        ),
        "",
        (
            "Casting: they play the story's main characters, in place of the "
            "actors. Decide from the story who plays whom, and keep them in the "
            f"same roles in every picture of a title. Where a story has no fitting "
            f"roles, make the {pictures} without them."
        ),
        "",
        "Likeness matters most: they must still be themselves.",
        (
            "- Use all of their photos across the pictures: they're different on "
            "purpose. For each picture, choose the photos whose angle, expression "
            "and look suit it, so a different photo leads each time rather than "
            "the same one. Photos where the face is hidden (sunglasses) or small "
            "still show hair, build and style; pair them with one where the face "
            "is clear."
        ),
        (
            "- Make every picture with them in it from image inputs, never from a "
            "description of them: the photos chosen for that picture"
            + (
                "."
                if single
                else ", plus the title's poster, so the faces, costumes and look "
                "stay the same across its set. Where a title's poster still has "
                "to be made, make and check it first, then the rest from it."
            )
        ),
        (
            "- Keep what makes them recognisable, as in the photos: face shape, "
            "eyes, brows, nose, mouth, jaw, skin tone, real age and build, and "
            "their own hair and facial hair (length, colour, texture). Don't make "
            "them younger, slimmer or more glamorous, and don't blend in an "
            "actor's features."
        ),
        (
            "- Costume, setting, lighting and expression change with the story; "
            "their faces don't. Keep hair and beards as they are unless a role "
            "can't work without a change."
        ),
        (
            "- Show their faces clearly: front or three-quarter view, well lit, "
            "large in the frame. No tiny figures in the distance, backs turned or "
            "faces in shadow; a picture is better without them than with people "
            "who aren't recognisably them."
        ),
        (
            "- Before saving, compare each face with the photos side by side. If "
            "someone who knows them wouldn't recognise them at a glance, make it "
            "again."
        ),
        "",
        "Don't use any other real person's likeness, actors included.",
        "",
    ]


def prompts(described, *, batch, photos, people, output, style="") -> list[str]:
    """One prompt per batch, for Codex or another image-making AI agent."""
    chunks = [described[i : i + batch] for i in range(0, len(described), batch)]
    kinds = [k for k in KINDS if any(k in entry["files"] for entry in described)]
    if not kinds:
        kinds = ["Primary"]
    single = kinds == ["Primary"]
    who = people or "the people in them"
    texts = []
    for index, chunk in enumerate(chunks, 1):
        lines = [
            (
                f"You're making custom {'posters' if single else 'artwork'} for a "
                "home media library. "
                f"Batch {index} of {len(chunks)}: {len(chunk)} title"
                f"{'' if len(chunk) == 1 else 's'}."
            ),
            "",
        ]
        if photos:
            lines += _casting(photos, who, single)
        else:
            lines += ["Don't use any real person's likeness, actors included.", ""]
        if style:
            lines += [f"The look: {style}", ""]
        if single:
            lines += [
                "For each title below, make one poster:",
                "- Portrait 2:3, at least 1024 × 1536, saved as JPEG.",
                (
                    "- Its tone, setting and key imagery, with the title on it "
                    "spelled exactly as given."
                ),
                LETTERING,
            ]
        else:
            lines += [
                (
                    "For each title below, make a matching set of pictures: the "
                    "same cast, look and style in each, but each a different scene "
                    "or framing (none a crop of another):"
                ),
                *(f"- {KIND_SPECS[kind]}" for kind in kinds),
                (
                    "- Its tone, setting and key imagery, with the title spelled "
                    "exactly as given wherever it appears."
                ),
                LETTERING,
            ]
        lines += [
            (
                f"- Save it in {output} under the file name given. Write each file in "
                "one go (or write it elsewhere and move it in): it's picked up as soon "
                "as it stops changing."
            ),
            (
                "- Make only the files listed: no drafts, test images, extra "
                "versions or working references. Image generation is slow and "
                "costly, and Jellyfin uses nothing else."
            ),
            (
                "- If one comes out wrong (a misspelled title, an unconvincing face), "
                "make it again under the same name."
            ),
            (
                "- When the batch is done, list each file with a line on its concept."
                if single
                else "- When the batch is done, list each title with a line on its "
                "set's concept."
            ),
            "",
            "Titles:",
        ]
        for entry in chunk:
            year = f" ({entry['year']})" if entry["year"] else ""
            about = ", ".join(entry["genres"])
            lines.append(
                f"{entry['number']}. {entry['title']}{year}, {entry['kind']}"
                + (f" ({about})" if about else "")
                + (f" → {entry['file']}" if single else "")
            )
            if entry["overview"]:
                lines.append(f"    {entry['overview']}")
            if not single:
                if entry.get("made"):
                    lines.append(
                        f"    Its poster is already made: {output}/{entry['made']}. "
                        "Don't make it again; match the others to its cast and style."
                    )
                lines += [
                    f"    {KIND_NAMES[kind].capitalize()} → {entry['files'][kind]}"
                    for kind in kinds
                    if kind in entry["files"]
                ]
        texts.append("\n".join(lines))
    return texts


def gallery_groups() -> list[dict]:
    """The gallery, one tile per picture: a title's pictures that are the same
    (one per account, from one import) are shown together. Copies are saved
    identically, so the file's size tells them apart cheaply. Each title's
    other pictures (wide tile, background, logo, banner) go with its tile as
    ``extras``, grouped the same way."""
    groups: dict[tuple, dict] = {}
    for poster in gallery():
        try:
            size = Path(poster.image_path).stat().st_size
        except OSError:
            size = None
        key = (
            poster.media_identity_id,
            poster.image_type,
            size if size is not None else poster.id,
        )
        group = groups.setdefault(
            key,
            {
                "media": poster.media,
                "kind": poster.image_type,
                "noun": _noun(poster.image_type),
                "image": poster,
                "posters": [],
            },
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
    tiles: dict[str, list[dict]] = {}
    for group in groups.values():
        tiles.setdefault(group["media"].id, []).append(group)
    shown = []
    for parts in tiles.values():
        posters = [g for g in parts if g["kind"] == "Primary"]
        extras = sorted(
            (g for g in parts if g["kind"] != "Primary"),
            key=lambda g: KINDS.index(g["kind"]) if g["kind"] in KINDS else 99,
        )
        # A title with only other pictures still gets a tile, led by the first.
        leads = posters or extras[:1]
        rest = extras if posters else extras[1:]
        for index, lead in enumerate(leads):
            tile = lead | {"extras": rest if index == 0 else []}
            tile["latest"] = max(
                (
                    g["applied_at"].isoformat()
                    for g in [tile, *tile["extras"]]
                    if g["applied_at"]
                ),
                default="",
            )
            shown.append(tile)
    return sorted(shown, key=lambda g: g["latest"], reverse=True)


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
    "position": "bottom",  # or "top", "top_right"
    "list_style": "faces",  # posters in lists: ★ and faces, "names" or "star"
    "page_style": "names",  # a title's own page: names listed, or "list" (the same)
    "colour": "gold",  # or silver, teal, purple
    "own": False,  # whether an account sees its own favourites marked
    "sees": {},  # account → the accounts whose favourites it sees
    "new_days": 7,  # a favourite this recent says NEW (0: never)
    "household_at": 3,  # this many people's favourite: a household one (0: off)
    "episodes": True,  # favourite episodes' thumbnails too
}
# An account's own wide tile and background show Leaving Soon's countdown, as
# Jellyfin's own do; ribbons go on its wide tile and banner too.
BADGED_KINDS = {"Thumb", "Backdrop"}
RIBBON_KINDS = {"Thumb", "Banner"}
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
        "position": _choice(form.get("position"), ("bottom", "top", "top_right")),
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


def draw_ribbon(
    base, users, names, options, *, new, household, large=False, leaving=None
):
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
        leaving=leaving,
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
    from .services import left_out_user_ids

    left_out = left_out_user_ids()  # never named on a ribbon
    liked = {
        key: {u: since for u, since in users.items() if u not in left_out}
        for key, users in liked.items()
    }
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


_preview: dict[str, bytes] = {}  # Jellyfin id → its poster, for the previews


def preview_poster() -> tuple[bytes | None, str | None]:
    """A poster to show the ribbon previews on, and its Leaving Soon words:
    the latest favourite's (anything with a poster when there's none yet)."""
    from .artwork import _jpeg
    from .models import UserMediaState
    from .services import client

    media = (
        db.session.query(MediaIdentity)
        .join(UserMediaState, UserMediaState.media_identity_id == MediaIdentity.id)
        .filter(
            UserMediaState.favorite.is_(True), MediaIdentity.jellyfin_id.isnot(None)
        )
        .order_by(UserMediaState.favorite_since.desc().nullslast())
        .first()
    ) or (
        db.session.query(MediaIdentity)
        .filter(MediaIdentity.jellyfin_id.isnot(None))
        .order_by(MediaIdentity.id)
        .first()
    )
    if media is None:
        return None, None
    if media.jellyfin_id not in _preview:
        try:
            picture = client("jellyfin").image(media.jellyfin_id)
            _preview.clear()
            _preview[media.jellyfin_id] = _jpeg(picture)
        except (IntegrationError, OSError, ValueError):
            return None, None
    return _preview[media.jellyfin_id], leaving_label(media.id)


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


def waiting_marks() -> dict[tuple[str, str, str], str]:
    """(kind, account, id) → the tag on a title or episode flagged Leave after
    watched, for the accounts whose watch counts (everyone's, when anyone's
    does). A season has no picture of its own here."""
    from .flags import LEAVE
    from .models import MediaFlag
    from .services import account_names

    everyone = list(account_names())
    found = {}
    for row in db.session.query(MediaFlag).filter_by(flag=LEAVE):
        if row.media_part_id:
            key = ("e", row.media_part_id)
        elif row.season_number is None:
            key = ("t", row.media_identity_id)
        else:
            continue
        each = row.mode == "each"
        for viewer in [row.jellyfin_user_id] if row.jellyfin_user_id else everyone:
            if row.jellyfin_user_id:
                text = "LEAVES AS YOU WATCH" if each else "LEAVING AFTER YOU WATCH"
            else:
                text = "LEAVES AS WATCHED" if each else "LEAVING AFTER WATCHED"
            found[(key[0], viewer, key[1])] = text
    return found


def plugin_list(version: int = 1) -> list[dict]:
    """What the Curatarr plugin shows each account instead of the usual
    picture: its own custom posters, favourite ribbons on titles and
    episodes, and Leave after watched's tag.

    Version 2 (plugin 0.8.0.0 and later) lists each type of picture
    (``kind``): the poster, and an account's own wide tile, background, logo
    and banner, or ones made from its poster (``fallbacks``). Version 1 has
    posters only, each with ``wide``: whether there's a landscape version."""
    import hashlib
    import json

    bare = lambda value: str(value or "").replace("-", "").lower()
    options = ribbon_settings()
    marked = ribbons(options)
    waiting = waiting_marks()
    keys = set(marked) | set(waiting)
    own = {(p.jellyfin_user_id, p.media_identity_id): p for p in for_accounts()}
    others = (
        {
            (p.jellyfin_user_id, p.media_identity_id, p.image_type): p
            for p in for_accounts(None)
            if p.image_type != "Primary"
        }
        if version >= 2
        else {}
    )
    made = fallbacks()
    widened = made["Thumb"] == "widened"
    title_ids = (
        {i for k, _v, i in keys if k == "t"}
        | {m for _v, m in own}
        | {m for _v, m, _k in others}
    )
    medias = {
        m.id: m
        for m in db.session.query(MediaIdentity).filter(
            MediaIdentity.id.in_(title_ids or {""})
        )
    }
    part_ids = {i for k, _v, i in keys if k == "e"}
    parts = {
        p.id: p
        for p in db.session.query(MediaPart).filter(MediaPart.id.in_(part_ids or {""}))
    }
    needs_tag = {
        medias[i].jellyfin_id
        for k, v, i in keys
        if k == "t" and (v, i) not in own and i in medias
    } | {parts[i].jellyfin_id for k, _v, i in keys if k == "e" and i in parts}
    tags = _image_tags(needs_tag) if needs_tag else _jellyfin_tags
    if marked and options["list_style"] == "faces":
        _refresh_avatar_tags()
    leaving: dict[str, str | None] = {}
    entries = []
    poster_of: dict[tuple[str, str], dict] = {}  # (viewer, title): its entry

    def add(entry, viewer=None, media_id=None, title=True):
        if not title:
            entry["wide"] = False
        if version >= 2:
            entry["kind"] = "Primary"
            # A wide tile made from the poster stays the poster's "wide"
            # version, under its tag, as plugin 0.7.0.0 had it: a separate
            # entry had a tag of its own, and plugin 0.8.0.0 fetched every one
            # again. An account's own wide tile takes its place.
            if (viewer, media_id, "Thumb") in others:
                entry["wide"] = False
        if viewer and media_id:
            poster_of[(viewer, media_id)] = entry
        entries.append(entry)

    def label_for(media_id):
        if media_id not in leaving:
            leaving[media_id] = leaving_label(media_id)
        return leaving[media_id]

    for (viewer, media_id), poster in own.items():
        media = medias.get(media_id)
        if media and media.jellyfin_id and ("t", viewer, media_id) not in keys:
            add(
                {
                    "item": bare(media.jellyfin_id),
                    "user": bare(viewer),
                    "id": poster.id,
                    "tag": account_tag(poster, label_for(media_id)),
                    "wide": widened,
                },
                viewer,
                media_id,
            )
    for kind, viewer, item_id in sorted(keys):
        ribbon = marked.get((kind, viewer, item_id))
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
                waiting.get((kind, viewer, item_id)),
                options["position"],
                options["list_style"],
                options["page_style"],
                options["colour"],
                [_avatar_version(u) for u in ribbon["users"]]
                if ribbon and options["list_style"] == "faces"
                else None,
                RIBBON_STYLE,
            ],
            sort_keys=True,
        )
        add(
            {
                "item": bare(jellyfin_id),
                "user": bare(viewer),
                "id": f"{'v' if kind == 't' else 'e'}.{viewer}.{item_id}",
                "tag": hashlib.sha1(spec.encode()).hexdigest()[:32],
                # A second picture, listing the names, for where it's shown big.
                "large": bool(ribbon) and has_large(options, ribbon["household"], kind),
                # A landscape version, for rows of wide tiles (a title's Thumb).
                "wide": kind == "t" and widened,
            },
            *((viewer, item_id) if kind == "t" else (None, None)),
            title=kind == "t",
        )
    if version < 2:
        return entries
    from .artwork import BADGE_STYLE

    pairs = set(poster_of) | {(v, m) for v, m, _k in others}
    for viewer, media_id in sorted(pairs):
        media = medias.get(media_id)
        if media is None or not media.jellyfin_id:
            continue
        poster = poster_of.get((viewer, media_id))
        ribbon = marked.get(("t", viewer, media_id))
        for kind in KINDS[1:]:
            custom = others.get((viewer, media_id, kind))
            if custom:
                base = custom.id
                label = label_for(media_id) if kind in BADGED_KINDS else None
                drawn = ribbon if kind in RIBBON_KINDS else None
            elif kind == "Thumb":
                continue  # made from the poster: its "wide" version
            elif poster and made.get(kind, "own") != "own":
                # Made from the poster as this account sees it, ribbon and all.
                base, label, drawn = f"{made[kind]}:{poster['tag']}", None, None
            else:
                continue
            spec = json.dumps(
                [
                    kind,
                    base,
                    label,
                    drawn,
                    options["position"],
                    options["list_style"],
                    options["colour"],
                    [_avatar_version(u) for u in drawn["users"]]
                    if drawn and options["list_style"] == "faces"
                    else None,
                    RIBBON_STYLE,
                    BADGE_STYLE,
                ],
                sort_keys=True,
            )
            entries.append(
                {
                    "item": bare(media.jellyfin_id),
                    "user": bare(viewer),
                    "kind": kind,
                    "id": f"{kind.lower()}.{viewer}.{media_id}",
                    "tag": hashlib.sha1(spec.encode()).hexdigest()[:32],
                    "large": False,
                }
            )
    return entries


def plugin_picture(
    entry_id: str, large: bool = False, wide: bool = False
) -> bytes | None:
    """The picture for one of plugin_list's entries, or None if it's gone.
    ``large``: a ribbon as shown big (on a title's own page), with names.
    ``wide``: the picture made landscape, for rows of wide tiles."""
    if wide:
        from .artwork import widen

        picture = plugin_picture(entry_id)
        return widen(picture) if picture else None
    from .artwork import _jpeg
    from .services import client

    prefix, _dot, rest = entry_id.partition(".")
    kind = {k.lower(): k for k in KINDS[1:]}.get(prefix)
    if kind:
        viewer, _dot, media_id = rest.partition(".")
        return _other_picture(kind, viewer, media_id)
    if not entry_id.startswith(("v.", "e.")):
        poster = db.session.get(CustomPoster, entry_id)
        if (
            not poster
            or not poster.active
            or poster.image_type != "Primary"
            or not Path(poster.image_path).exists()
        ):
            return None
        return account_picture(poster, leaving_label(poster.media_identity_id))
    kind, viewer, item_id = entry_id.split(".", 2)
    options = ribbon_settings()
    key = ("t" if kind == "v" else "e", viewer, item_id)
    ribbon = ribbons(options).get(key)
    wait = waiting_marks().get(key)
    if not ribbon and not wait:
        return None
    if kind == "v":
        media = db.session.get(MediaIdentity, item_id)
        if media is None or not media.jellyfin_id:
            return None
        poster = active(item_id, viewer)
        leaving = leaving_label(item_id)
        if poster and Path(poster.image_path).exists():
            base = account_picture(poster, leaving)
        else:
            base = _jpeg(client("jellyfin").image(media.jellyfin_id))
    else:
        part = db.session.get(MediaPart, item_id)
        if part is None or not part.jellyfin_id:
            return None
        base, leaving = _jpeg(client("jellyfin").image(part.jellyfin_id)), None
    if wait:
        from .artwork import INK, WAITING_AMBER, badge_poster

        base = badge_poster(base, wait, colour=WAITING_AMBER, ink=INK)
        leaving = wait  # a ribbon along the top goes below it
    if not ribbon:
        return base
    return draw_ribbon(
        base,
        ribbon["users"],
        ribbon["names"],
        options,
        new=ribbon["new"],
        household=ribbon["household"],
        large=large and kind == "v",
        leaving=leaving,
    )


def _poster_as_seen(viewer, media_id) -> bytes | None:
    """The poster the plugin shows this account: its own, a ribbon's or
    Leave after watched's tag drawn on; None when it sees Jellyfin's own."""
    key = ("t", viewer, media_id)
    if key in ribbons() or key in waiting_marks():
        return plugin_picture(f"v.{viewer}.{media_id}")
    poster = active(media_id, viewer)
    if poster and Path(poster.image_path).exists():
        return account_picture(poster, leaving_label(media_id))
    return None


def _other_picture(kind, viewer, media_id) -> bytes | None:
    """An account's wide tile, background, logo or banner as the plugin
    serves it: its own, with the countdown and ribbon where they go, or one
    made from its poster (``fallbacks``)."""
    from .artwork import BANNER_SIZE, badge_poster, blurred_background, widen

    custom = active(media_id, viewer, kind)
    if custom and Path(custom.image_path).exists():
        picture = Path(custom.image_path).read_bytes()
        label = leaving_label(media_id) if kind in BADGED_KINDS else None
        if label:
            picture = badge_poster(picture, label, kind)
        options = ribbon_settings()
        ribbon = ribbons(options).get(("t", viewer, media_id))
        if ribbon and kind in RIBBON_KINDS:
            if label:  # a wide tile's countdown runs along its bottom
                options = options | {"position": "top"}
            picture = draw_ribbon(
                picture,
                ribbon["users"],
                ribbon["names"],
                options,
                new=ribbon["new"],
                household=ribbon["household"],
            )
        return picture
    made = fallbacks().get(kind, "own")
    if made == "own":
        return None
    poster = _poster_as_seen(viewer, media_id)
    if poster is None:
        return None
    if kind == "Backdrop":
        return blurred_background(poster)
    return widen(poster, BANNER_SIZE) if kind == "Banner" else widen(poster)
