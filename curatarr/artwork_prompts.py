"""What a worker is asked to make for one picture (artwork_jobs.py): the prompt,
the reference pictures and the checks.

Two kinds of engine read prompts differently. An image model (ComfyUI's)
needs the picture described, with its reference images numbered in the order
they're given. An agent with an image tool (Codex) takes instructions, so its
prompt reuses the copy-and-paste prompts' wording (custom_posters.prompts):
the casting rules, the lettering and each picture type's spec.
"""

import re
from pathlib import Path

from . import custom_posters
from .custom_posters import KIND_NAMES, KIND_SPECS, LETTERING

# The size each picture type is made at by an image model: multiples of 16,
# about two megapixels at most, cropped to its exact shape afterwards.
MODEL_SIZES = {
    "Primary": (1024, 1536),
    "Thumb": (1920, 1088),
    "Backdrop": (1920, 1088),
    "Banner": (1728, 320),
    "Logo": (1536, 768),
}
# The shape each type is cropped to (width / height).
ASPECTS = {
    "Primary": 2 / 3,
    "Thumb": 16 / 9,
    "Backdrop": 16 / 9,
    "Banner": 1000 / 185,
    "Logo": None,  # whatever the lettering needs
}
# The picture types people appear in (a banner is too small for faces, a
# logo is only lettering).
CAST_KINDS = {"Primary", "Thumb", "Backdrop"}
# The types with the title lettered on them, checked after making.
LETTERED_KINDS = {"Primary", "Thumb", "Banner", "Logo"}
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
MAX_PEOPLE = 3
# An image model is sent up to this many photos of each person; the worker
# keeps the most typical (artwork_worker.backends.comfyui.fit_references).
PHOTOS_EACH = 5
AGENT_PHOTOS_EACH = 4
NAME_SPLIT = re.compile(r"[\s_.\-\d]+")


def people_in(folder: str) -> dict[str, list[str]]:
    """{person: [photo paths]} in a reference photos folder. A photo's file
    name starts with who's in it ("Sam 2.jpg", "sam_beach.png"), as the
    copy-and-paste prompts ask."""
    found: dict[str, list[str]] = {}
    try:
        files = sorted(Path(folder).iterdir()) if folder else []
    except OSError:
        return {}
    for path in files:
        if path.suffix.lower() not in PHOTO_SUFFIXES or not path.is_file():
            continue
        first = next((w for w in NAME_SPLIT.split(path.stem) if w), "")
        if first:
            found.setdefault(first.capitalize(), []).append(str(path))
    return found


def descriptions(folder: str) -> dict[str, str]:
    """{person: a few words on them} from ``<Name>.txt`` beside their photos
    ("a woman of about 30, platinum pixie cut"): an image model given only
    photos sometimes changes someone's sex or age in a busy picture."""
    found = {}
    try:
        files = sorted(Path(folder).glob("*.txt")) if folder else []
    except OSError:
        return {}
    for path in files:
        try:
            words = " ".join(path.read_text(errors="replace").split())[:160]
        except OSError:
            continue
        if words:
            found[path.stem.strip().capitalize()] = words
    return found


def choose_photos(photos: list[str], kind: str, each: int) -> list[str]:
    """``each`` of a person's photos for a picture: a different one leads for
    each type, so the set draws on all of them."""
    if len(photos) <= each:
        return list(photos)
    start = custom_posters.KINDS.index(kind) % len(photos)
    turned = photos[start:] + photos[:start]
    return turned[:each]


def cast(options: dict, kind: str, agent: bool) -> dict[str, list[str]]:
    """{person: photos} to put in this picture, from the job's options."""
    if kind not in CAST_KINDS or not options.get("photos"):
        return {}
    people = people_in(options["photos"])
    chosen = options.get("cast") or list(people)
    each = AGENT_PHOTOS_EACH if agent else PHOTOS_EACH
    return {
        who: choose_photos(people[who], kind, each)
        for who in chosen[:MAX_PEOPLE]
        if people.get(who)
    }


def _numbers(indexes: list[int]) -> str:
    words = [str(i) for i in indexes]
    if len(words) == 1:
        return f"image {words[0]}"
    return "images " + ", ".join(words[:-1]) + " and " + words[-1]


def _story(details: dict) -> str:
    """The title, year, genres and plot, for an agent's prompt."""
    year = f" ({details['year']})" if details.get("year") else ""
    genres = ", ".join(details.get("genres") or [])
    words = f'the {details.get("kind") or "film"} "{details["title"]}"{year}'
    if genres:
        words += f", {genres}"
    if details.get("overview"):
        words += f". The story: {details['overview']}"
    return words


# Lettering an image model draws well, by genre (first match wins). Spelled
# out, it letters far more reliably than "lettering that suits the story".
GENRE_LETTERING = (
    ("horror", "jagged, distressed blood-red serif capitals"),
    ("science fiction", "sleek chrome sans-serif capitals with a cold glow"),
    ("sci-fi", "sleek chrome sans-serif capitals with a cold glow"),
    ("fantasy", "ornate engraved gold serif capitals"),
    ("animation", "bold rounded colourful capitals"),
    ("family", "bold rounded colourful capitals"),
    ("comedy", "bold, chunky, playful capitals"),
    ("romance", "elegant thin serif capitals"),
    ("western", "weathered wood-type slab-serif capitals"),
    ("war", "stencilled military capitals"),
    ("crime", "weathered silver serif capitals"),
    ("thriller", "weathered silver serif capitals"),
    ("mystery", "weathered silver serif capitals"),
    ("action", "heavy brushed-steel capitals"),
    ("documentary", "clean bold sans-serif capitals"),
)
DEFAULT_LETTERING = "bold cinematic serif capitals"


def lettering_style(details: dict) -> str:
    genres = " ".join(details.get("genres") or []).lower()
    return next(
        (style for word, style in GENRE_LETTERING if word in genres), DEFAULT_LETTERING
    )


def _scene(details: dict) -> str:
    """What the picture's about, for an image model: no year or genre names,
    which it letters onto the picture as text."""
    genre = (details.get("genres") or ["drama"])[0].lower()
    noun = "TV series" if details.get("kind") == "TV show" else "film"
    words = f"a {genre} {noun}"
    if details.get("overview"):
        words += f". Story: {details['overview'].rstrip('.')}."
    return words


def _cast(people: dict[str, list[int]], about: dict[str, str] | None = None) -> str:
    """Who plays whom, each person once."""
    about = about or {}
    named = [
        f"{name} ({_numbers(numbers)}"
        + (f"; {about[name]}" if about.get(name) else "")
        + ")"
        for name, numbers in people.items()
    ]
    if len(named) == 1:
        lead = (
            f"Only one person appears in the picture: the lead, played by "
            f"{named[0]}, shown once."
        )
    else:
        lead = (
            "The main characters are played by "
            + ", ".join(named[:-1])
            + f" and {named[-1]}, each a different person shown once."
        )
    return (
        lead + " They play the story's own characters: in those characters' "
        "costumes, make-up and props, in the places that make it recognisable at "
        "a glance. Keep each face exactly as in their photos (face shape, eyes, "
        "nose, mouth, skin tone, age, hair and facial hair)."
    )


def model_prompt(
    kind: str,
    details: dict,
    options: dict,
    people: dict[str, list[int]],
    set_image: int | None = None,
    lettering_image: int | None = None,
    feedback: str | None = None,
    background: str = "black",
) -> str:
    """The prompt for an image model. ``people`` gives each person's
    reference images by number; ``set_image`` the picture whose look this one
    matches; ``lettering_image`` the title's original logo. The title is
    given in capitals and its lettering spelled out: that's what these models
    letter most reliably."""
    title = f'"{details["title"].upper()}"'
    lettering = (
        f"in the lettering of {_numbers([lettering_image])}: the same typeface, "
        "letter shapes, colours and effects"
        if lettering_image
        else f"in large {lettering_style(details)}"
    )
    if kind == "Logo":
        return (
            f"Title logo for {_scene(details)} Only the words {title}, {lettering}, "
            f"centred, on a plain flat pure {background} background, spelled "
            "exactly. Nothing else in the image: no scenery, objects, people, "
            "borders or other text."
            + (f" Changes asked for: {feedback}" if feedback else "")
        )
    lines = []
    if kind == "Primary":
        lines.append(f"Cinematic movie poster key art for {_scene(details)}")
    elif kind == "Thumb":
        lines.append(
            f"Wide 16:9 key art tile for {_scene(details)} Composed to read "
            "clearly when shown small: a few large, bold shapes."
        )
    elif kind == "Backdrop":
        lines.append(
            f"Wide cinematic film still, a place or moment from {_scene(details)} "
            "Keep the lower left calm and uncluttered (sky, water, shadow), as "
            "apps lay text over it."
        )
    else:  # Banner
        lines.append(
            f"A very wide, narrow banner strip for {_scene(details)} A setting or "
            "object from the story as a panorama, no people."
        )
    if people:
        lines.append(_cast(people, descriptions(options.get("photos") or "")))
        if kind == "Backdrop":
            lines.append("They're on the right-hand side, faces large, clear and lit.")
        else:
            lines.append(
                "Faces large, clear and well lit, front or three-quarter view."
            )
    elif kind in CAST_KINDS:
        lines.append("Any people are original characters, not real actors.")
    if set_image:
        lines.append(
            f"Part of a matching set: the same cast, costumes, colours and style "
            f"as {_numbers([set_image])}, but a different scene and framing."
        )
    if options.get("style"):
        lines.append(f"The look: {options['style']}")
    if kind == "Backdrop":
        lines.append("No text, letters, logos or watermarks anywhere.")
    else:
        where = {
            "Primary": "At the top",
            "Thumb": "Large across the lower third",
            "Banner": "On the left third, filling the strip's height",
        }[kind]
        lines.append(
            f"{where}, the title {title} {lettering}, spelled exactly. No other "
            "text anywhere: no credits, names, taglines, dates or genre words."
        )
    if feedback:
        lines.append(f"Changes asked for: {feedback}")
    return " ".join(lines)


def agent_prompt(
    kind: str,
    details: dict,
    options: dict,
    people: dict[str, list[int]],
    set_image: int | None = None,
    originals: dict[str, int] | None = None,
    feedback: str | None = None,
) -> str:
    """The prompt for an agent with an image tool (Codex): one picture, made
    with the tool once, nothing else done."""
    noun = KIND_NAMES[kind]
    lines = [
        (
            f"Make one {noun} for a home media library with your image "
            "generation tool, then stop. Don't run commands, read or write "
            "files, or browse: the picture the tool makes is collected "
            "automatically."
        ),
        "",
        f"- {KIND_SPECS[kind]}",
        f"- Its tone, setting and key imagery: {_story(details)}",
    ]
    if kind != "Backdrop":
        lines += [
            "- The title, spelled exactly as given: " + details["title"],
            LETTERING,
        ]
    if people:
        who = ", ".join(
            f"{name} ({_numbers(numbers)})" for name, numbers in people.items()
        )
        rules = custom_posters._casting("", "them", True)
        lines += [
            "",
            f"Reference photos of {who}, who've agreed to this, are attached.",
            *[line for line in rules[2:] if "photos side by side" not in line],
        ]
    else:
        lines += ["", "Don't use any real person's likeness, actors included."]
    if set_image:
        lines.append(
            f"It's one of a matching set: keep the cast, costumes, look and style "
            f"of {_numbers([set_image])}, in a different scene or framing."
        )
    if originals:
        named = ", ".join(
            f"its original {custom_posters.ORIGINAL_NAMES[k]} ({_numbers([n])})"
            for k, n in originals.items()
        )
        lines.append(
            f"Original art, as a tonal reference only: {named}. Carry over its "
            "mood, palette, era and lettering, but none of the actors' faces."
        )
    if options.get("style"):
        lines.append(f"The look: {options['style']}")
    if feedback:
        lines.append(f"Changes asked for: {feedback}")
    lines.append("Use the image tool once, for this one picture.")
    return "\n".join(lines)
