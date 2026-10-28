"""Checks of a picture once it's made, each marked with how far it can be
trusted: the title read back with text recognition, and the faces compared
with the reference photos by face recognition. They catch a misspelled
title or the wrong face; they can't say whether a picture is good."""

import json
import re
import subprocess
from difflib import SequenceMatcher
from pathlib import Path

HELPER = Path(__file__).with_name("checks_helper.py")
TITLE_OK = 0.95  # below this, the title's taken as misspelled
SAME_PERSON = 0.45  # face similarity above which it's taken as them
STRAY_LETTERS = 12  # more letters than this of other text (gibberish credits)
BACKGROUND_LETTERS = 3  # a background with more letters than this has text on it


def run(
    python: str, image: Path, *, ocr=False, people=None, root="", timeout=300
) -> dict:
    """The checks helper's answer, or {} when there's no checks environment."""
    if not python or not (ocr or people):
        return {}
    request = {
        "image": str(image),
        "ocr": ocr,
        "people": people or {},
        "insightface_root": root,
    }
    try:
        done = subprocess.run(
            [python, str(HELPER)],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        return json.loads(done.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def _letters(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def title_match(title: str, texts: list) -> dict:
    """How well what's written matches ``title`` (1.0: exactly), from the
    recognised text pieces, and what else is written (stray text)."""
    want = _letters(title)
    kept = [(t, score) for t, score in texts if _letters(t)]
    pieces = [(_letters(t), score) for t, score in kept]
    if not want:
        return {"value": None}
    best, span = 0.0, (0, 0)
    for start in range(len(pieces)):
        joined = ""
        for end in range(start, min(len(pieces), start + 8)):
            joined += pieces[end][0]
            ratio = SequenceMatcher(None, want, joined).ratio()
            if ratio > best:
                best, span = ratio, (start, end + 1)
    stray = sum(
        len(text)
        for i, (text, score) in enumerate(pieces)
        if not span[0] <= i < span[1] and score >= 0.6
    )
    read = " / ".join(t for t, _s in kept[span[0] : span[1]])
    return {"value": round(best, 3), "stray": stray, "read": read}


def likeness(answer: dict, people: list[str]) -> dict:
    """The worst-matched person's best face similarity, and whether two
    people's faces were mixed up (one face taken for both, or one person in
    two places)."""
    faces = answer.get("faces") or []
    best = {}
    for who in people:
        scores = [(f["sims"].get(who, 0), i) for i, f in enumerate(faces)]
        best[who] = max(scores) if scores else (0, None)
    value = round(min(s for s, _i in best.values()), 3) if best else None
    used = [i for s, i in best.values() if i is not None and s >= SAME_PERSON]
    mixed = len(used) != len(set(used))
    twice = sorted(
        who
        for who in people
        if sum(1 for f in faces if f["sims"].get(who, 0) >= SAME_PERSON) > 1
    )
    return {
        "value": value,
        "each": {who: s for who, (s, _i) in best.items()},
        "faces": len(faces),
        "mixed": mixed,
        "twice": twice,
    }
