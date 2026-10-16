"""Library superlatives from what's said: who swears most, panics most, says
"I love you" most, and each show's catchphrase.

Each saved transcript is counted once (and again only if it's replaced) into a
small set of numbers stored with its file; the Fun facts page ranks those.
Counting reads transcripts already on disk and needs no GPU.
"""

import re
import time
from collections import Counter, defaultdict
from itertools import pairwise
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


# Words that start with a capital mid-sentence but aren't anyone's name.
NOT_NAMES = frozenset(
    [
        "I",
        "I'm",
        "I'll",
        "I've",
        "I'd",
        "Mr",
        # What people are called by their job or family, not their name.
        "Mister",
        "Miss",
        "Sister",
        "Brother",
        "Father",
        "Mother",
        "Aunt",
        "Uncle",
        "Grandma",
        "Grandpa",
        "Mommy",
        "Daddy",
        "Captain",
        "Doctor",
        "Officer",
        "Detective",
        "Agent",
        "Judge",
        "Honor",
        "Professor",
        "Sergeant",
        "Lieutenant",
        "Chief",
        "Coach",
        "Boss",
        "Reverend",
        "Pastor",
        "Nurse",
        # The first word of a place said all the time ("New York").
        "New",
        "Los",
        "San",
        "Las",
        "United",
        "North",
        "South",
        "East",
        "West",
        "Mrs",
        "Ms",
        "Dr",
        "Sir",
        "Madam",
        "God",
        "Jesus",
        "Christ",
        "Lord",
        "Mom",
        "Mum",
        "Dad",
        "Okay",
        "OK",
        "Oh",
        "Hey",
        "Yes",
        "No",
        "Yeah",
        "Uh",
        "Um",
        "Hmm",
        "TV",
        "Internet",
        "English",
        "Christmas",
        "Halloween",
        "Thanksgiving",
        "Easter",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ]
)
PLACE_STARTS = frozenset(
    ["New", "Los", "San", "Las", "United", "Santa", "El", "Le", "La"]
)
CAPITALISED = re.compile(r"^[A-Z][a-z]+(?:'s)?$")
WINDOW = 1000  # words per vocabulary sample, so long and short files compare
SECONDS_PER_WORD = 0.6  # generous: people say two or three words a second
PAUSE = 1.5  # seconds of quiet that end a stretch of non-stop talking
EDGE = 180  # seconds at each end left out of the longest silence (logos, credits)
# What speech recognition writes over music and silence.
NOT_SPEECH = {"thank you.", "thanks for watching!", "you", "you.", "♪", "♪♪"}


def _spoken_until(segment) -> float:
    """When the talking in a line really ends. Speech recognition sometimes
    stretches one line across a long quiet passage (nine words over 18 minutes),
    so a line counts only as long as its words take to say."""
    said = len(words(segment.text))
    return min(segment.end, segment.start + said * SECONDS_PER_WORD + 1)


def _real(segment) -> bool:
    text = (segment.text or "").strip()
    return len(words(text)) >= 2 and text.lower() not in NOT_SPEECH


def _names(text) -> Counter:
    """Capitalised words that aren't the first word of a sentence."""
    found = Counter()
    for sentence in re.split(r"[.?!]+", text or ""):
        said = [w.strip(',;:"()-—') for w in sentence.split()]
        for before, word in pairwise(said):
            if before in PLACE_STARTS:
                continue  # "New York", "Los Angeles"
            if CAPITALISED.match(word):
                name = word.removesuffix("'s")
                if name not in NOT_NAMES and name.lower() not in STOP:
                    found[name] += 1
    return found


def count(segments) -> dict:
    """The numbers one transcript contributes."""
    total = Counter()
    names = Counter()
    vocabulary = []
    segments = list(segments)
    for segment in segments:
        text = segment.text or ""
        said = words(text)
        vocabulary.extend(said)
        total["words"] += len(said)
        total["speech"] += max(segment.end - segment.start, 0)
        total["questions"] += text.count("?")
        total["exclaims"] += text.count("!")
        names.update(_names(text))
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
    # Different words in each block of 1,000 said: a big vocabulary, not a long file.
    for start in range(0, len(vocabulary) - WINDOW + 1, WINDOW):
        total["vocab_windows"] += 1
        total["vocab_sum"] += len(set(vocabulary[start : start + WINDOW]))
    result = {key: round(value, 1) for key, value in total.items()}
    speech = [s for s in segments if _real(s)]
    if speech:
        ends = max(s.end for s in segments)
        result["first_word"] = round(speech[0].start)
        quiet, talk = 0.0, 0.0
        run_start, last_end = speech[0].start, _spoken_until(speech[0])
        for segment in speech[1:]:
            gap = segment.start - last_end
            if last_end > EDGE and segment.start < ends - EDGE:
                quiet = max(quiet, gap)
            if gap > PAUSE:
                talk = max(talk, last_end - run_start)
                run_start = segment.start
            last_end = max(last_end, _spoken_until(segment))
        result["longest_silence"] = round(quiet)
        result["longest_talk"] = round(max(talk, last_end - run_start))
    result["names"] = dict(names.most_common(15))
    return result


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


def _names_said(shows) -> list[dict]:
    """The name each show says most: in several of its episodes, not in its title."""
    watched = _episodes_watched()
    found = []
    for media_id, show in shows.items():
        if show["episodes"] < MIN_EPISODES:
            continue
        hours = (show["stats"].get("minutes") or 0) / 60
        # "Gilligan's Island" names Gilligan.
        own = {w.removesuffix("'s") for w in words(show["media"].title)}
        need = min(3, show["episodes"])
        for name, times in show["names"].most_common(10):
            if name.lower() in own or show["name_episodes"][name] < need:
                continue
            found.append(
                {
                    "media": show["media"],
                    "name": name,
                    "per_hour": times / hours if hours else 0,
                    "episodes": show["name_episodes"][name],
                    "of": show["episodes"],
                    "watched": watched.get(media_id, 0),
                }
            )
            break
    found.sort(key=lambda f: (-f["watched"], -f["per_hour"], f["media"].title))
    return found


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
    shows = defaultdict(
        lambda: {
            "stats": Counter(),
            "episodes": 0,
            "names": Counter(),
            "name_episodes": Counter(),
            "first": [],
            "silence": 0,
            "talk": 0,
        }
    )
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
                {
                    k: v
                    for k, v in stats.items()
                    if isinstance(v, int | float)
                    and k not in ("first_word", "longest_silence", "longest_talk")
                }
            )
            show["names"].update(stats.get("names") or {})
            show["name_episodes"].update(set(stats.get("names") or {}))
            if "first_word" in stats:
                show["first"].append(stats["first_word"])
            show["silence"] = max(show["silence"], stats.get("longest_silence", 0))
            show["talk"] = max(show["talk"], stats.get("longest_talk", 0))
    show_list = [
        {
            "media": s["media"],
            "stats": {
                **s["stats"],
                # A show waits, on average, this long; its longest silence and
                # talking stretch are its record across episodes.
                "first_word": sum(s["first"]) / len(s["first"]) if s["first"] else 0,
                "longest_silence": s["silence"],
                "longest_talk": s["talk"],
            },
            "episodes": s["episodes"],
        }
        for s in shows.values()
        if s["episodes"] >= MIN_EPISODES
    ]
    long_films = [f for f in films if f["stats"]["minutes"] >= MIN_FILM_MINUTES]
    rate = lambda key: lambda item: _per_hour(item["stats"], key)
    minutes = lambda key: lambda item: item["stats"].get(key, 0) / 60
    vocabulary = lambda item: (
        item["stats"].get("vocab_sum", 0)
        / max(item["stats"].get("vocab_windows", 0), 1)
    )
    sampled = lambda least: lambda item: item["stats"].get("vocab_windows", 0) >= least
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
        {
            "id": "shouty",
            "title": "Shouty",
            "blurb": "Most exclamations per hour",
            "films": _top(long_films, rate("exclaims")),
            "shows": _top(show_list, rate("exclaims")),
            "value": rate("exclaims"),
            "unit": "an hour",
        },
        {
            "id": "vocabulary",
            "title": "Biggest vocabularies",
            "blurb": "Most different words in every 1,000 spoken",
            "films": _top(long_films, vocabulary, keep=sampled(3)),
            "shows": _top(show_list, vocabulary, keep=sampled(5)),
            "value": vocabulary,
            "unit": "different words per 1,000",
        },
        {
            "id": "slow",
            "title": "Slow burners",
            "blurb": "Longest wait before anyone says a word (for shows, the average episode)",
            "films": _top(long_films, minutes("first_word")),
            "shows": _top(show_list, minutes("first_word")),
            "value": minutes("first_word"),
            "unit": "minutes in",
        },
        {
            "id": "silence",
            "title": "Sounds of silence",
            "blurb": "The longest quiet stretch, away from the opening and the credits",
            "films": _top(long_films, minutes("longest_silence")),
            "shows": _top(show_list, minutes("longest_silence")),
            "value": minutes("longest_silence"),
            "unit": "minutes",
        },
        {
            "id": "talkers",
            "title": "Non-stop talkers",
            "blurb": "The longest stretch of talking without a pause of more than a "
            "second and a half (one person or several; transcripts don't say who)",
            "films": _top(long_films, minutes("longest_talk")),
            "shows": _top(show_list, minutes("longest_talk")),
            "value": minutes("longest_talk"),
            "unit": "minutes",
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
        "names": _names_said(shows),
        "totals": dict(totals),
        "films": len(films),
        "episodes": len(episodes),
    }
