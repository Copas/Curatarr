"""One picture, start to finish: made by a backend, finished (cropped to its
shape, a logo keyed out to transparency), checked, and made again with a new
seed when its title comes out misspelled, keeping the best try. Each step is
recorded with the picture, so its review shows how it was made."""

import time
from pathlib import Path

from . import checks, imaging
from .backends import Failed, Reference
from .gpu import Peak

LOGO_BACKGROUNDS = ("black", "white")


def make_picture(task, backend, references, progress, settings: dict) -> dict:
    """{data, meta} for ``task``, ready to upload."""
    check_python = settings.get("python") or ""
    lettered = bool(task.get("lettered") and task.get("title"))
    people = {}
    for ref in references:
        if ref.role == "person" and ref.person:
            people.setdefault(ref.person, []).append(str(ref.path))
    textless = task.get("kind") == "Backdrop"  # a background has no text at all
    checked = (lettered or textless or people) and check_python
    # Only a local model tries again by itself: an outside service's pictures
    # use up its allowance, and its lettering is good enough that a misread
    # (text recognition can't read thin, spaced-out lettering) costs more
    # than it saves. Its checks still show in review.
    local = getattr(backend, "name", "") != "codex"
    tries = 1 + (int(settings.get("title_retries") or 0) if checked and local else 0)
    if task.get("transparent") and local:
        tries = max(tries, 2)  # a second background if keying the first fails
    started = time.time()
    best = None
    steps: list[str] = []
    # Local models are shown head crops (they copy whatever else is in a
    # photo); an agent like Codex does better with the whole photos.
    cropped = 0
    if getattr(backend, "name", "") == "comfyui":
        references, cropped = _crop_people(references, check_python, settings)
    if cropped:
        steps.append(
            f"{cropped} reference photo{'' if cropped == 1 else 's'} cropped "
            "to the head"
        )
    with Peak() as peak:
        for attempt in range(tries):
            this = dict(task)
            this["seed"] = int(task.get("seed") or 0) + attempt * 7919
            if task.get("transparent") and attempt % 2 == 1:
                this["prompt"] = task["prompt"].replace(
                    "pure black background", "pure white background"
                )
            made = backend.make(this, references, progress)
            progress(92, "Checking")
            data, done, facts = imaging.finish(made.data, this)
            score, metrics, note = _check(
                data, this, people, lettered, check_python, settings
            )
            if (
                task.get("transparent")
                and facts
                and not 0.02 <= facts.get("opaque", 0) <= 0.85
            ):
                note = f"the keyed logo was {facts.get('opaque', 0):.0%} solid"
                score = -1
            label = f"try {attempt + 1} (seed {this['seed']})"
            steps.append(f"{label}: {note}" if note else label)
            steps += done
            if best is None or score > best["score"]:
                best = {
                    "score": score,
                    "data": data,
                    "metrics": metrics,
                    "made": made,
                    "seed": this["seed"],
                }
            if score >= 1:
                break
    if best is None:
        raise Failed("Nothing was made.")
    if best["score"] < 0 and task.get("transparent"):
        raise Failed("The logo's background couldn't be made transparent.")
    meta = {
        "seed": best["seed"],
        "params": best["made"].params,
        "steps": steps,
        "metrics": best["metrics"],
        "duration_s": round(time.time() - started, 1),
        "peak_vram_mb": peak.mb or None,
    }
    return {"data": best["data"], "meta": meta}


def _crop_people(references, python, settings):
    """Person references cropped to the head (an image model copies the rest
    of a photo: clothes, glasses, writing), each marked with its face's size
    and how typical of the person it is (its face compared with the person's
    others: a photo in sunglasses or heavy make-up is the least typical).
    Returns (references, how many were cropped)."""
    people = [r for r in references if r.role == "person"]
    if not python or not people:
        return references, 0
    found = (
        checks.run(
            python,
            None,
            boxes=[r.path for r in people],
            root=settings.get("insightface_root") or "",
        ).get("boxes")
        or {}
    )
    means = {}
    for who in {r.person for r in people}:
        vectors = [
            found[str(r.path)]["embedding"]
            for r in people
            if r.person == who and str(r.path) in found
        ]
        if vectors:
            mean = [sum(v) / len(vectors) for v in zip(*vectors, strict=True)]
            size = sum(v * v for v in mean) ** 0.5 or 1
            means[who] = [v / size for v in mean]
    folder = Path(settings.get("_work_dir") or ".") / "crops"
    cropped, out = 0, []
    for ref in references:
        face = found.get(str(ref.path)) if ref.role == "person" else None
        if not face:
            out.append(ref)
            continue
        box, embedding = face["box"], face["embedding"]
        mean = means.get(ref.person) or embedding
        typical = sum(a * b for a, b in zip(embedding, mean, strict=True))
        # Into the worker's own folder: never beside the photo itself.
        folder.mkdir(parents=True, exist_ok=True)
        crop = folder / f"{ref.number}-{ref.path.stem}-head.png"
        imaging.head_and_shoulders(ref.path, box, crop)
        out.append(
            Reference(
                ref.number,
                crop,
                ref.role,
                ref.person,
                round(box[2] - box[0]),
                round(typical, 3),
            )
        )
        cropped += 1
    return out, cropped


def _check(data, task, people, lettered, python, settings):
    """(score, metrics, note): score 1 when nothing's wrong that a new try
    could fix, lower the worse the title reads."""
    textless = task.get("kind") == "Backdrop"
    if not python or not (lettered or textless or people):
        return 1, {}, ""
    work = Path(settings.get("_work_dir") or ".") / f"check-{task['image']}.png"
    work.write_bytes(data)
    try:
        answer = checks.run(
            python,
            work,
            ocr=lettered or textless,
            people=people,
            root=settings.get("insightface_root") or "",
        )
    finally:
        work.unlink(missing_ok=True)
    metrics, score, note = {}, 1.0, ""
    ocr = answer.get("ocr") or {}
    if lettered and ocr.get("available"):
        match = checks.title_match(task["title"], ocr.get("texts") or [])
        if match["value"] is not None:
            metrics["title_match"] = {
                "value": match["value"],
                "kind": "measured",
                "note": "text recognition; 1.0 is the title exactly",
                "detail": f"read “{match['read']}”"
                if match["read"]
                else "no title found",
            }
            metrics["stray_text"] = {
                "value": match["stray"],
                "kind": "measured",
                "note": "letters of other text (credits, gibberish)",
            }
            if match["value"] < checks.TITLE_OK:
                score = match["value"]
                note = (
                    f"title read as “{match['read'] or 'nothing'}” ({match['value']})"
                )
            elif match["stray"] > checks.STRAY_LETTERS:
                score = 0.95  # the title's right: better than a misspelled one
                note = f"{match['stray']} letters of other text"
    if textless and ocr.get("available"):
        found = [t for t, confidence in ocr.get("texts") or [] if confidence >= 0.6]
        letters = sum(len(checks._letters(t)) for t in found)
        metrics["stray_text"] = {
            "value": letters,
            "kind": "measured",
            "note": "letters of text on a background, which should have none",
            "detail": " / ".join(found)[:200] if found else "none found",
        }
        if letters > checks.BACKGROUND_LETTERS:
            score = 0.9
            note = f"text on the background: “{' / '.join(found)[:80]}”"
    faces = answer.get("faces") or {}
    if people and faces.get("available"):
        like = checks.likeness(faces, sorted(people))
        if like["value"] is not None:
            metrics["likeness"] = {
                "value": like["value"],
                "kind": "estimate",
                "note": "face recognition similarity of the least-like person; "
                "above about 0.45 is usually the same person",
                "detail": ", ".join(f"{w} {s}" for w, s in like["each"].items())
                + f"; {like['faces']} face{'' if like['faces'] == 1 else 's'} found",
            }
            metrics["identity_mix"] = {
                "value": "yes" if like["mixed"] else "no",
                "kind": "estimate",
                "note": "one face taken for two people",
            }
            metrics["shown_twice"] = {
                "value": ", ".join(like["twice"]) or "no one",
                "kind": "estimate",
                "note": "a person in two places in the picture",
            }
            if like["twice"] and not task.get("transparent"):
                score = min(score, 0.5)
                twice = " and ".join(like["twice"])
                note = (note + "; " if note else "") + f"{twice} appeared twice"
    return score, metrics, note
