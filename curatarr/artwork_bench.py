"""Benchmarks: the same made-up films asked of each engine, to see which makes
the best pictures (Posters → Benchmarks).

A run is an ArtworkBatch with purpose "benchmark" and a job for each scenario
and engine. Its pictures are measured as they arrive (the worker's checks:
the title read back, faces compared with the reference photos) and rated by a
person: scores for each picture, and blind choices between two engines'
pictures of the same scenario. The films are invented, so nothing here
depends on a library; reference photos come from a folder chosen for the run
(made-up people, or the owner's own, which stay on their computer unless an
outside engine is chosen and allowed).
"""

import random
from statistics import mean

from . import artwork_jobs, db
from .models import (
    ArtworkBatch,
    ArtworkCandidate,
    ArtworkComparison,
    ArtworkImage,
    ArtworkJob,
    ArtworkRating,
)

FILMS = {
    "lighthouse": {
        "title": "The Last Lighthouse",
        "year": 2019,
        "kind": "film",
        "genres": ["Thriller", "Drama"],
        "overview": "A lighthouse keeper and a stranded marine biologist are cut off "
        "by a week-long storm, and begin to suspect the island isn't empty.",
    },
    "neon": {
        "title": "Neon Saints",
        "year": 1987,
        "kind": "film",
        "genres": ["Crime", "Comedy"],
        "overview": "Two hapless getaway drivers in 1980s Miami are mistaken for "
        "the city's most feared hitmen.",
    },
    "quixotic": {
        "title": "The Quixotic Adventures of Zephyr Blackwood",
        "year": 2023,
        "kind": "TV show",
        "genres": ["Fantasy", "Adventure"],
        "overview": "An eccentric cartographer maps a city that rearranges itself "
        "every night.",
    },
    "market": {
        "title": "Monsoon Market",
        "year": 2008,
        "kind": "film",
        "genres": ["Drama", "Romance"],
        "overview": "Over one rainy season, two stall owners in a crowded night "
        "market fall for each other while fighting over the same corner.",
    },
}
# Each scenario: what's asked, of which film, with how many people from the
# reference photos, and how many tries (different seeds) per engine.
SCENARIOS = [
    {
        "id": "portrait",
        "label": "One person, poster",
        "film": "lighthouse",
        "kinds": ["Primary"],
        "people": 1,
        "repeats": 1,
    },
    {
        "id": "duo",
        "label": "Two people together",
        "film": "neon",
        "kinds": ["Primary"],
        "people": 2,
        "repeats": 1,
    },
    {
        "id": "consistency",
        "label": "Same person, three tries",
        "film": "lighthouse",
        "kinds": ["Primary"],
        "people": 1,
        "repeats": 3,
    },
    {
        "id": "poses",
        "label": "Same person, wide pictures",
        "film": "lighthouse",
        "kinds": ["Thumb", "Backdrop"],
        "people": 1,
        "repeats": 1,
    },
    {
        "id": "noir",
        "label": "Low-key night lighting",
        "film": "neon",
        "kinds": ["Primary"],
        "people": 1,
        "repeats": 1,
        "style": "Film noir: hard low-key light, deep shadows, wet streets, neon.",
    },
    {
        "id": "painted",
        "label": "Painted illustration",
        "film": "quixotic",
        "kinds": ["Primary"],
        "people": 1,
        "repeats": 1,
        "style": "A hand-painted illustrated poster, gouache textures, storybook.",
    },
    {
        "id": "lettering",
        "label": "Long title lettering",
        "film": "quixotic",
        "kinds": ["Primary"],
        "people": 0,
        "repeats": 2,
    },
    {
        "id": "background",
        "label": "Busy background",
        "film": "market",
        "kinds": ["Backdrop"],
        "people": 0,
        "repeats": 1,
    },
    {
        "id": "logo",
        "label": "Transparent logo",
        "film": "neon",
        "kinds": ["Logo"],
        "people": 0,
        "repeats": 2,
    },
    {
        "id": "banner",
        "label": "Narrow banner",
        "film": "market",
        "kinds": ["Banner"],
        "people": 0,
        "repeats": 1,
    },
    {
        "id": "set",
        "label": "Matching five-picture set",
        "film": "lighthouse",
        "kinds": ["Primary", "Thumb", "Backdrop", "Logo", "Banner"],
        "people": 2,
        "repeats": 1,
    },
]
SMOKE = ["portrait", "logo"]
ASPECTS = {
    "overall": "Overall",
    "identity": "Likeness",
    "typography": "Lettering",
    "set": "Fits its set",
}


def scenario(scenario_id: str) -> dict | None:
    return next((s for s in SCENARIOS if s["id"] == scenario_id), None)


def start(engines, scenario_ids, *, photos="", cast=(), external_ok=False, by=None):
    """A benchmark run: each chosen scenario asked of each chosen engine."""
    from . import artwork_prompts

    chosen = [s for s in SCENARIOS if s["id"] in set(scenario_ids)]
    engines = [e for e in engines if e]
    if not chosen or not engines:
        raise ValueError("Choose at least one engine and one scenario.")
    people = list(cast) or list(artwork_prompts.people_in(photos))
    if any(s["people"] for s in chosen) and not people:
        raise ValueError(
            "Some scenarios need reference photos: give a folder with photos of "
            "one or two people (made-up people are fine, and better for sharing "
            "results)."
        )
    run = ArtworkBatch(
        name=f"Benchmark: {len(chosen)} scenario{'s' if len(chosen) > 1 else ''} × "
        f"{len(engines)} engine{'s' if len(engines) > 1 else ''}",
        purpose="benchmark",
        spec={
            "engines": engines,
            "scenarios": [s["id"] for s in chosen],
            "photos": photos,
            "cast": people,
        },
        media_ids=[],
        created_by=by,
    )
    db.session.add(run)
    db.session.flush()
    skipped = []
    for item in chosen:
        film = FILMS[item["film"]] | {"base": item["film"]}
        for engine in engines:
            for repeat in range(item["repeats"]):
                options = {
                    "scenario": item["id"],
                    "repeat": repeat + 1,
                    "style": item.get("style", ""),
                    "external_ok": external_ok,
                }
                if item["people"]:
                    options["photos"] = photos
                    options["cast"] = people[: item["people"]]
                try:
                    artwork_jobs.create_job(
                        None,
                        item["kinds"],
                        engine=engine,
                        options=options,
                        details=film,
                        batch=run,
                        purpose="benchmark",
                        created_by=by,
                    )
                except ValueError as exc:  # this engine can't do it
                    skipped.append(
                        {"scenario": item["id"], "engine": engine, "why": str(exc)}
                    )
                    break
    run.spec = dict(run.spec) | {"skipped": skipped}
    db.session.commit()
    return run


def _value(candidate: ArtworkCandidate, name: str):
    metric = (candidate.metrics or {}).get(name) or {}
    value = metric.get("value")
    return value if isinstance(value, (int, float)) else None


def results(run: ArtworkBatch) -> dict:
    """The run's pictures by scenario and engine, and a summary per engine:
    {"rows": [{scenario, engine, images: [ArtworkImage]}], "engines": {engine:
    summary}}. Measured numbers and people's ratings are kept apart."""
    rows: dict[tuple, dict] = {}
    for job in run.jobs:
        key = ((job.options or {}).get("scenario"), job.engine)
        row = rows.setdefault(
            key,
            {
                "scenario": scenario(key[0]) or {"label": key[0]},
                "engine": key[1],
                "images": [],
            },
        )
        row["images"].extend(job.images)
    summaries = {}
    for engine in (run.spec or {}).get("engines") or []:
        images = [i for (s, e), r in rows.items() if e == engine for i in r["images"]]
        made = [c for i in images for c in i.candidates]
        ratings = [
            r.score
            for c in made
            for r in db.session.query(ArtworkRating).filter_by(candidate_id=c.id)
        ]
        summaries[engine] = {
            "asked": len(images),
            "made": len({c.image_id for c in made}),
            "failed": sum(1 for i in images if i.state == "failed"),
            "seconds": _average(c.duration_s for c in made),
            "vram_gb": _peak(c.peak_vram_mb for c in made),
            "title": _average(_value(c, "title_match") for c in made),
            "likeness": _average(_value(c, "likeness") for c in made),
            "rating": _average(ratings),
            "ratings": len(ratings),
            "wins": wins(engine),
        }
    return {"rows": list(rows.values()), "engines": summaries}


def _average(values):
    found = [v for v in values if v is not None]
    return round(mean(found), 2) if found else None


def _peak(values):
    found = [v for v in values if v]
    return round(max(found) / 1024, 1) if found else None


def wins(engine: str) -> dict:
    """Blind choices involving ``engine``'s pictures: {"won", "lost", "tie"}."""
    tally = {"won": 0, "lost": 0, "tie": 0}
    for comparison in db.session.query(ArtworkComparison):
        left = db.session.get(ArtworkCandidate, comparison.left_id)
        right = db.session.get(ArtworkCandidate, comparison.right_id)
        if not left or not right or engine not in (left.engine, right.engine):
            continue
        if left.engine == right.engine:
            continue
        if comparison.winner == "tie":
            tally["tie"] += 1
        elif (comparison.winner == "left") == (left.engine == engine):
            tally["won"] += 1
        else:
            tally["lost"] += 1
    return tally


def _key(candidate: ArtworkCandidate) -> tuple:
    job = candidate.image.job
    return ((job.options or {}).get("scenario"), candidate.image.kind)


def pair(run: ArtworkBatch | None = None, rng=random):
    """Two pictures of the same scenario and type by different engines, not
    compared yet, in random order (so the page can't give away which is
    which). None when there's no such pair."""
    query = (
        db.session.query(ArtworkCandidate)
        .join(ArtworkImage)
        .join(ArtworkJob)
        .filter(ArtworkJob.purpose == "benchmark", ArtworkCandidate.path.isnot(None))
    )
    if run is not None:
        query = query.filter(ArtworkJob.batch_id == run.id)
    groups: dict[tuple, list] = {}
    for candidate in query:
        groups.setdefault(_key(candidate), []).append(candidate)
    done = {
        frozenset((c.left_id, c.right_id)) for c in db.session.query(ArtworkComparison)
    }
    options = []
    for found in groups.values():
        for i, a in enumerate(found):
            for b in found[i + 1 :]:
                if a.engine != b.engine and frozenset((a.id, b.id)) not in done:
                    options.append((a, b))
    if not options:
        return None
    a, b = rng.choice(options)
    return (a, b) if rng.random() < 0.5 else (b, a)


def compare(left_id, right_id, winner, notes=None, by=None) -> ArtworkComparison:
    if winner not in ("left", "right", "tie"):
        raise ValueError("Choose one of them, or a tie.")
    if not db.session.get(ArtworkCandidate, left_id) or not db.session.get(
        ArtworkCandidate, right_id
    ):
        raise ValueError("Those pictures are gone.")
    comparison = ArtworkComparison(
        left_id=left_id,
        right_id=right_id,
        winner=winner,
        notes=(notes or "").strip()[:1000] or None,
        created_by=by,
    )
    db.session.add(comparison)
    db.session.commit()
    return comparison


def rate(candidate: ArtworkCandidate, scores: dict, notes=None, by=None) -> int:
    """Scores (1–5) for a picture's aspects; a new score replaces the old."""
    saved = 0
    for aspect, raw in scores.items():
        if aspect not in ASPECTS or raw in (None, ""):
            continue
        try:
            score = max(1, min(int(raw), 5))
        except (TypeError, ValueError):
            continue
        db.session.query(ArtworkRating).filter_by(
            candidate_id=candidate.id, aspect=aspect
        ).delete()
        db.session.add(
            ArtworkRating(
                candidate_id=candidate.id,
                aspect=aspect,
                score=score,
                notes=(notes or "").strip()[:1000] or None,
                created_by=by,
            )
        )
        saved += 1
    db.session.commit()
    return saved


def ratings_of(candidate: ArtworkCandidate) -> dict:
    return {
        r.aspect: r.score
        for r in db.session.query(ArtworkRating).filter_by(candidate_id=candidate.id)
    }
