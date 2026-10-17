"""Shows whose files are another show with the same name.

Sonarr matches releases by name, so a show that shares its name with another
can end up with that show's episodes: a 1976 Hong Kong "All in the Family"
under the 1971 American sitcom, a UK "Wheel of Fortune" under the American one.
Two clues, both judged across the whole show, since single episodes are noisy
(a scene in French, speech recognition hearing Scots as Welsh):

- language: most transcribed episodes (at least three) are in a language that
  is neither the show's own nor English;
- year: most release names (at least two) carry a year that is neither the
  show's year nor the episode's air year ("All.In.The.Family.S01E01.1976").

Shows are checked in the worker a few at a time, again only when their files
or transcripts change. Replace blocklists the releases, deletes the files and
searches again; when the year gave it away, Sonarr is also told to ignore
releases with that year for this show, since the same uploader usually has
the episodes in other releases too.
"""

import hashlib
import re
import time

from . import db
from .models import ChapterState, MediaIdentity, MediaPart

KEY = "wrong_versions"
MIN_EPISODES = 3
MIN_FILES = 2
RUN_SECONDS = 20
YEAR = re.compile(r"(?<![0-9])((?:19|20)\d\d)(?![0-9p])")
LANGUAGES = {
    "English": "en",
    "Japanese": "ja",
    "Chinese": "zh",
    "Cantonese": "zh",
    "Mandarin": "zh",
    "Korean": "ko",
    "French": "fr",
    "Spanish": "es",
    "German": "de",
    "Italian": "it",
    "Portuguese": "pt",
    "Russian": "ru",
    "Hindi": "hi",
    "Arabic": "ar",
    "Turkish": "tr",
    "Dutch": "nl",
    "Swedish": "sv",
    "Danish": "da",
    "Norwegian": "no",
    "Finnish": "fi",
    "Polish": "pl",
    "Thai": "th",
    "Hebrew": "he",
}
# Sonarr lists a file's soundtracks as three-letter codes ("eng/tur").
AUDIO = {
    "en": {"eng"},
    "ja": {"jpn"},
    "zh": {"chi", "zho"},
    "ko": {"kor"},
    "fr": {"fre", "fra"},
    "es": {"spa"},
    "de": {"ger", "deu"},
    "it": {"ita"},
    "pt": {"por"},
    "ru": {"rus"},
    "hi": {"hin"},
    "ar": {"ara"},
    "tr": {"tur"},
    "nl": {"dut", "nld"},
    "sv": {"swe"},
    "da": {"dan"},
    "no": {"nor"},
    "fi": {"fin"},
    "pl": {"pol"},
    "th": {"tha"},
    "he": {"heb"},
}
NAMES = {
    code: name
    for name, code in LANGUAGES.items()
    if name not in ("Cantonese", "Mandarin")
}


def language_name(code) -> str:
    return NAMES.get(code, code)


def _years(name):
    return {int(y) for y in YEAR.findall(name or "")}


def judge(series, files, languages) -> dict | None:
    """The evidence against a show, or None.

    series: Sonarr's series (year, originalLanguage); files: [(Sonarr episode
    file, air year)]; languages: {episode file id: its transcript's language}.
    """
    evidence = []
    own = LANGUAGES.get((series.get("originalLanguage") or {}).get("name"))
    expected = AUDIO.get(own, set()) | AUDIO["en"]
    known, foreign = 0, []
    for file, _ in files:
        lang = languages.get(file["id"])
        if not lang:
            continue
        known += 1
        audio = set(
            re.split(
                r"[/,\s]+",
                ((file.get("mediaInfo") or {}).get("audioLanguages") or "").lower(),
            )
        )
        # A file that also has the show's own (or an English) soundtrack is the
        # right show playing another track (a dual-audio release).
        if lang not in (own, "en") and not audio & expected:
            foreign.append(lang)
    language = None
    if known >= MIN_EPISODES and len(foreign) * 2 > known:
        language = max(set(foreign), key=foreign.count)
        evidence.append(
            f"{len(foreign)} of {known} transcribed episodes are in "
            f"{language_name(language)}, with no "
            f"{(series.get('originalLanguage') or {}).get('name') or 'English'} soundtrack; "
            f"the show is in "
            f"{(series.get('originalLanguage') or {}).get('name') or 'another language'}"
        )
    show_year = series.get("year") or 0
    odd = {}
    for file, air_year in files:
        name = file.get("sceneName") or file.get("relativePath") or ""
        allowed = {show_year - 1, show_year, show_year + 1}
        if air_year:
            allowed |= {air_year - 1, air_year, air_year + 1}
        # A year in the show's own title ("1923") is its name, not a clue.
        wrong = _years(name) - allowed - _years(series.get("title"))
        if wrong:
            odd[file["id"]] = min(wrong)
    year = None
    if len(odd) >= MIN_FILES and len(odd) * 2 > len(files):
        year = max(set(odd.values()), key=list(odd.values()).count)
        example = next(
            f.get("sceneName") or f.get("relativePath")
            for f, _ in files
            if odd.get(f["id"]) == year
        )
        evidence.append(
            f"{len(odd)} of {len(files)} release names say {year}, but the show "
            f"is from {show_year} (e.g. {example})"
        )
    if not evidence:
        return None
    flagged = [f for f, _ in files if f["id"] in odd] if year else [f for f, _ in files]
    return {
        "evidence": evidence,
        "language": language,
        "year": year,
        "files": [
            {"id": f["id"], "name": f.get("sceneName") or f.get("relativePath")}
            for f in flagged
        ],
    }


def _shows():
    """{media id: (media, [(part, transcript language)])} for shows Sonarr has."""
    rows = (
        db.session.query(MediaIdentity, MediaPart, ChapterState)
        .join(MediaPart, MediaPart.media_identity_id == MediaIdentity.id)
        .outerjoin(ChapterState, ChapterState.media_part_id == MediaPart.id)
        .filter(
            MediaIdentity.media_type == "series",
            MediaIdentity.sonarr_id.isnot(None),
            MediaIdentity.missing_since.is_(None),
            MediaPart.kind == "episode",
            MediaPart.has_file.is_(True),
        )
        .all()
    )
    shows = {}
    for media, part, state in rows:
        language = ((state.talk_stats or {}) if state else {}).get("language")
        shows.setdefault(media.id, (media, []))[1].append((part, language))
    return shows


def _signature(parts) -> str:
    text = ",".join(sorted(f"{p.arr_file_id}:{lang}" for p, lang in parts))
    return hashlib.blake2b(text.encode(), digest_size=8).hexdigest()


def sweep() -> int:
    """Check shows whose files or transcripts changed (worker cycle)."""
    from .services import client, set_setting, setting

    stored = dict(setting(KEY, {}) or {})
    shows = _shows()
    todo = [
        (media_id, media, parts)
        for media_id, (media, parts) in sorted(
            shows.items(), key=lambda kv: kv[1][0].title
        )
        if (stored.get(media_id) or {}).get("sig") != _signature(parts)
    ]
    gone = set(stored) - set(shows)
    if not todo and not gone:
        return 0
    sonarr = client("sonarr")
    series = {s["id"]: s for s in sonarr.series() or []}
    db.session.rollback()  # ask Sonarr outside any transaction
    started, done = time.monotonic(), 0
    for media_id, media, parts in todo:
        show = series.get(media.sonarr_id)
        if show is None:
            continue
        air = {
            p.arr_file_id: (p.air_date.year if p.air_date else None) for p, _ in parts
        }
        files = [
            (f, air.get(f["id"])) for f in sonarr.episode_files(media.sonarr_id) or []
        ]
        verdict = judge(show, files, {p.arr_file_id: lang for p, lang in parts})
        previous = stored.get(media_id) or {}
        same = previous.get("verdict") == verdict
        stored[media_id] = {
            "sig": _signature(parts),
            "verdict": verdict,
            # "It's the right show", a Replace being looked into, and "no better
            # copy" all hold until the evidence changes.
            "dismissed": previous.get("dismissed") and same,
            "replace_requested": previous.get("replace_requested") if same else None,
            "no_better": previous.get("no_better") if same else None,
            "replace_auto": previous.get("replace_auto") if same else None,
        }
        done += 1
        if time.monotonic() - started > RUN_SECONDS:
            break
    for media_id in gone:
        del stored[media_id]
        done += 1
    set_setting(KEY, stored)
    db.session.commit()
    return done


def rows() -> list[dict]:
    from .services import setting

    stored = setting(KEY, {}) or {}
    flagged = [
        media_id
        for media_id, entry in stored.items()
        if entry.get("verdict") and not entry.get("dismissed")
    ]
    if not flagged:
        return []
    media = {
        m.id: m
        for m in db.session.query(MediaIdentity).filter(MediaIdentity.id.in_(flagged))
    }
    return sorted(
        (
            {
                "media": media[media_id],
                **stored[media_id]["verdict"],
                "status": "pending"
                if stored[media_id].get("replace_requested")
                else "no_better"
                if stored[media_id].get("no_better")
                else None,
                "checked_at": (stored[media_id].get("no_better") or {}).get("at"),
            }
            for media_id in flagged
            if media_id in media
        ),
        key=lambda row: row["media"].title.lower(),
    )


def count() -> int:
    return len(rows())


def dismiss(media) -> None:
    from .services import set_setting, setting

    stored = dict(setting(KEY, {}) or {})
    if media.id in stored:
        stored[media.id] = {**stored[media.id], "dismissed": True}
        set_setting(KEY, stored)
    db.session.commit()


def replace(media) -> str:
    """Blocklist the wrong releases, delete their files and search again."""
    from .filecheck_jobs import _dry_run
    from .services import audit, client, set_setting, setting, utcnow

    if _dry_run(media):
        raise ValueError(
            "dry run is on for this library, so Curatarr won't delete files. "
            "Turn dry run off, or replace them yourself"
        )
    stored = dict(setting(KEY, {}) or {})
    verdict = (stored.get(media.id) or {}).get("verdict")
    if not verdict:
        raise ValueError("Nothing is flagged for this show any more")
    file_ids = {f["id"] for f in verdict["files"]}
    parts = [
        p for p in media.parts if p.kind == "episode" and p.arr_file_id in file_ids
    ]
    episode_ids = sorted({p.sonarr_episode_id for p in parts if p.sonarr_episode_id})
    sonarr = client("sonarr")
    if verdict.get("year"):
        sonarr.ignore_for_series(
            media.sonarr_id,
            f"curatarr-{media.sonarr_id}",
            rf"/\b{verdict['year']}\b/",
            f"Curatarr: not the {verdict['year']} {media.title}",
        )
    blocklisted = set()
    for episode_id in episode_ids:
        grabs = sonarr.episode_grabs(episode_id)
        if grabs and grabs[0].get("downloadId") not in blocklisted:
            sonarr.mark_failed(grabs[0]["id"])
            blocklisted.add(grabs[0].get("downloadId"))
    for file_id in sorted(file_ids):
        sonarr.delete_episode_file(file_id)
    for part in parts:
        part.has_file = False
    if episode_ids:
        sonarr.episode_search(episode_ids)
    at = utcnow().isoformat()
    stored[media.id] = {**stored[media.id], "verdict": None, "replaced": at}
    set_setting(KEY, stored)
    audit(
        "wrong_version_replaced",
        media.id,
        f"Replaced {len(file_ids)} file{'s' if len(file_ids) != 1 else ''} of "
        f"{media.title} from another show: " + "; ".join(verdict["evidence"]) + ".",
        f"wrong-version:{media.id}:{at}",
        payload={"files": verdict["files"], "year": verdict.get("year")},
    )
    db.session.commit()
    message = (
        f"Deleted {len(file_ids)} file{'s' if len(file_ids) != 1 else ''}, blocklisted "
        f"the release{'s' if len(blocklisted) != 1 else ''} and asked Sonarr to search again."
    )
    if verdict.get("year"):
        message += (
            f" Sonarr now ignores releases mentioning {verdict['year']} for this show."
        )
    return message
