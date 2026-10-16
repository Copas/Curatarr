"""Library superlatives from what's said: who swears most, panics most, says
"I love you" most, and each show's catchphrase.

Each saved transcript is counted once (and again only if it's replaced) into a
small set of numbers stored with its file; the Fun facts page ranks those.
Counting reads transcripts already on disk and needs no GPU.
"""

import re
import time
from collections import Counter, defaultdict
from types import SimpleNamespace

from sqlalchemy import or_

from . import catchphrases, db, services
from .models import ChapterState, MediaIdentity, MediaPart

SWEEP = 300  # transcripts counted per worker cycle
CACHE_SECONDS = 600
SHOWN = 5
MIN_EPISODES = 3  # for a show's per-hour figures
MIN_FILM_MINUTES = 60

F_WORD = re.compile(
    r"^(fuck|fucks|fucked|fucker|fuckers|fucking|fuckin|motherfuck\w*|fuckhead|fuckface)$"
)
SWEARS = re.compile(
    r"^(fuck\w*|motherfuck\w*|shit\w*|bullshit|horseshit|bitch\w*|asshole\w*|bastard\w*|"
    r"goddamn\w*|damn\w*|dick|dicks|dickhead|piss\w*|cunt\w*|cock|cocksucker\w*|crap\w*|"
    r"prick\w*|twat\w*|wanker\w*|bollocks|bloody)$"
)
VIOLENT = re.compile(
    r"^(kill|kills|killed|killing|killer|killers|murder\w*|blood|bloody|bleed\w*|dead|die|dies|"
    r"died|dying|gun|guns|shoot|shooting|shot|shots|stab\w*|knife|knives|corpse\w*|"
    r"strangle\w*|slaughter\w*|massacre\w*|torture\w*|bury|buried)$"
)
PANIC = ("help", "run", "oh my god", "get out", "look out", "run run")
FILLER = frozenset(
    [
        "ha",
        "haha",
        "ah",
        "oh",
        "la",
        "na",
        "da",
        "hey",
        "whoa",
        "yeah",
        "no",
        "uh",
        "um",
        "hmm",
        "mm",
        "wow",
    ]
)
STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "it",
        "its",
        "this",
        "that",
        "i",
        "you",
        "he",
        "she",
        "we",
        "they",
        "me",
        "my",
        "your",
        "his",
        "her",
        "our",
        "their",
        "not",
        "no",
        "so",
        "do",
        "did",
        "don't",
        "i'm",
        "it's",
        "you're",
        "what",
        "yeah",
        "oh",
        "okay",
        "ok",
        "uh",
        "um",
        "hey",
        "right",
        "just",
        "know",
        "like",
        "get",
        "got",
        "go",
        "going",
        "gonna",
        "well",
        "all",
        "can",
        "there",
        "here",
        "have",
        "has",
        "had",
        "will",
        "would",
        "there's",
        "that's",
        "about",
        "want",
        "come",
        "let",
        "think",
        "really",
        "one",
        "good",
        "see",
        "say",
        "said",
        "look",
        "need",
        "make",
        "take",
        "tell",
        "mean",
        "thing",
        "something",
        "time",
        "back",
        "up",
        "out",
        "down",
        "now",
        "then",
        "how",
        "why",
        "who",
        "where",
        "when",
        "if",
        "because",
        "could",
        "should",
        "very",
        "too",
        "much",
        "some",
        "any",
        "more",
        "talking",
        "these",
        "those",
        "them",
        "him",
        "us",
    ]
)


def words(text: str) -> list[str]:
    return re.findall(r"[a-z']+", (text or "").lower().replace("’", "'"))


def count(segments) -> dict:
    """The numbers one transcript contributes."""
    total = Counter()
    for segment in segments:
        text = segment.text or ""
        said = words(text)
        total["words"] += len(said)
        total["speech"] += max(segment.end - segment.start, 0)
        total["questions"] += text.count("?")
        joined = " ".join(said)
        total["love"] += joined.count("i love you")
        total["polite"] += joined.count("please") + joined.count("thank you")
        padded = f" {joined} "
        total["panic"] += sum(padded.count(f" {phrase} ") for phrase in PANIC)
        for word in said:
            if F_WORD.match(word):
                total["f_words"] += 1
            if SWEARS.match(word):
                total["swears"] += 1
            if VIOLENT.match(word):
                total["violent"] += 1
    return {key: round(value, 1) for key, value in total.items()}


def _due():
    return (
        db.session.query(ChapterState)
        .filter(
            ChapterState.transcript_key.isnot(None),
            or_(
                ChapterState.talk_stats_key.is_(None),
                ChapterState.talk_stats_key != ChapterState.transcript_key,
            ),
        )
        .limit(SWEEP)
    )


def sweep() -> int:
    """Count transcripts not counted yet (worker cycle)."""
    from .enrichment import cache_dir
    from .transcribe import load

    rows = [(state.id, state.transcript_key) for state in _due()]
    db.session.rollback()  # read the transcripts outside any transaction
    counted = {}
    for state_id, key in rows:
        hit = load(cache_dir() / "transcripts" / f"{key}.json")
        stats = count(hit[0]) if hit else None
        if stats is not None:
            stats["language"] = hit[1].get("language")
        counted[state_id] = (stats, key)
    for start in range(0, len(rows), 50):  # short writes, so nothing waits long
        for state_id, (stats, key) in list(counted.items())[start : start + 50]:
            state = db.session.get(ChapterState, state_id)
            if state is not None:
                state.talk_stats, state.talk_stats_key = stats, key
        db.session.commit()
    if rows:
        _cache.clear()
    return len(rows)


# --- Rankings -------------------------------------------------------------------

_cache: dict = {}


def facts() -> dict:
    hit = _cache.get("facts")
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = _facts()
    _cache["facts"] = (time.monotonic(), value)
    return value


def _rows():
    return (
        db.session.query(ChapterState, MediaPart, MediaIdentity)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(
            ChapterState.talk_stats.isnot(None), MediaIdentity.missing_since.is_(None)
        )
        .all()
    )


def _episodes_watched() -> dict:
    """{show id: episodes played to the end, counting each viewer}."""
    from sqlalchemy import func

    from .models import EpisodeUserState

    return dict(
        db.session.query(MediaPart.media_identity_id, func.count(EpisodeUserState.id))
        .join(MediaPart, EpisodeUserState.media_part_id == MediaPart.id)
        .filter(EpisodeUserState.played.is_(True))
        .group_by(MediaPart.media_identity_id)
        .all()
    )


def _per_hour(stats, key):
    hours = (stats.get("minutes") or 0) / 60
    return stats.get(key, 0) / hours if hours else 0


def _top(items, key, *, reverse=True, limit=SHOWN, keep=lambda item: True):
    chosen = [item for item in items if keep(item)]
    chosen.sort(key=key, reverse=reverse)
    return chosen[:limit]


def _facts() -> dict:
    films, episodes = [], []
    shows = defaultdict(lambda: {"stats": Counter(), "episodes": 0})
    totals = Counter()
    for state, part, media in _rows():
        stats = dict(state.talk_stats)
        stats["minutes"] = (state.duration_seconds or 0) / 60
        if not stats["minutes"]:
            continue
        if stats.get("language") not in (None, "en"):
            # Every count looks for English words; a transcript in another
            # language would rank as the quietest and cleanest thing there is.
            totals["other_language"] += 1
            continue
        for key in ("words", "swears", "f_words", "panic", "love", "violent"):
            totals[key] += stats.get(key, 0)
        totals["minutes"] += stats["minutes"]
        totals["files"] += 1
        # Plain values: the result is cached past this request's session.
        media = SimpleNamespace(
            id=media.id, title=media.title, jellyfin_id=media.jellyfin_id
        )
        part = SimpleNamespace(
            kind=part.kind,
            season_number=part.season_number,
            episode_number=part.episode_number,
            jellyfin_id=part.jellyfin_id,
        )
        item = {"media": media, "part": part, "stats": stats}
        if part.kind == "movie_file":
            films.append(item)
        else:
            episodes.append(item)
            show = shows[media.id]
            show["media"] = media
            show["episodes"] += 1
            show["stats"].update(
                {k: v for k, v in stats.items() if isinstance(v, int | float)}
            )
    show_list = [
        {"media": s["media"], "stats": dict(s["stats"]), "episodes": s["episodes"]}
        for s in shows.values()
        if s["episodes"] >= MIN_EPISODES
    ]
    long_films = [f for f in films if f["stats"]["minutes"] >= MIN_FILM_MINUTES]
    rate = lambda key: lambda item: _per_hour(item["stats"], key)
    wpm = lambda item: item["stats"].get("words", 0) / max(item["stats"]["minutes"], 1)
    categories = [
        {
            "id": "swearing",
            "title": "Potty mouths",
            "blurb": "Most swearing per hour",
            "films": _top(long_films, rate("swears")),
            "shows": _top(show_list, rate("swears")),
            "value": rate("swears"),
            "unit": "an hour",
        },
        {
            "id": "f-words",
            "title": "F-bomb champions",
            "blurb": "Most f-words per hour",
            "films": _top(long_films, rate("f_words")),
            "shows": _top(show_list, rate("f_words")),
            "value": rate("f_words"),
            "unit": "an hour",
        },
        {
            "id": "clean",
            "title": "Squeaky clean",
            "blurb": "Talkative films without a single swear word, and the shows that swear least",
            "films": _top(
                long_films, wpm, keep=lambda f: f["stats"].get("swears", 0) == 0
            ),
            # Fewest swears, then the most talkative (many shows never swear).
            "shows": _top(show_list, lambda s: (-rate("swears")(s), wpm(s))),
            "value": rate("swears"),
            "unit": "swears an hour",
        },
        {
            "id": "violent",
            "title": "Bloodthirsty talk",
            "blurb": "Most kill, blood, dead, gun and knife talk per hour",
            "films": _top(long_films, rate("violent")),
            "shows": _top(show_list, rate("violent")),
            "value": rate("violent"),
            "unit": "an hour",
        },
        {
            "id": "panic",
            "title": "Panic stations",
            "blurb": 'Most "help!", "run!", "get out!" and "oh my god" per hour',
            "films": _top(long_films, rate("panic")),
            "shows": _top(show_list, rate("panic")),
            "value": rate("panic"),
            "unit": "an hour",
        },
        {
            "id": "chatty",
            "title": "Chatterboxes",
            "blurb": "Most words a minute",
            "films": _top(long_films, wpm),
            "shows": _top(show_list, wpm),
            "value": wpm,
            "unit": "words a minute",
        },
        {
            "id": "quiet",
            "title": "Strong, silent types",
            "blurb": "Fewest words a minute",
            "films": _top(long_films, wpm, reverse=False),
            "shows": _top(show_list, wpm, reverse=False),
            "value": wpm,
            "unit": "words a minute",
        },
        {
            "id": "love",
            "title": "Hopeless romantics",
            "blurb": 'Most times "I love you" is said, per hour',
            "films": _top(long_films, rate("love")),
            # Per hour, or the longest-running show wins by sheer length.
            "shows": _top(show_list, rate("love")),
            "value": rate("love"),
            "unit": "an hour",
        },
        {
            "id": "polite",
            "title": "Mind your manners",
            "blurb": "Most pleases and thank-yous per hour",
            "films": _top(long_films, rate("polite")),
            "shows": _top(show_list, rate("polite")),
            "value": rate("polite"),
            "unit": "an hour",
        },
        {
            "id": "questions",
            "title": "Inquiring minds",
            "blurb": "Most questions asked per hour",
            "films": _top(long_films, rate("questions")),
            "shows": _top(show_list, rate("questions")),
            "value": rate("questions"),
            "unit": "an hour",
        },
    ]
    return {
        "categories": [c for c in categories if c["films"] or c["shows"]],
        "catchphrases": catchphrases.pick(
            services.setting(catchphrases.KEY, {}) or {},
            {media_id: show["media"] for media_id, show in shows.items()},
            services.setting(catchphrases.SPREAD_KEY),
            _episodes_watched(),
        ),
        "totals": dict(totals),
        "films": len(films),
        "episodes": len(episodes),
    }
