"""The nudity index: when each film first shows breasts, full-frontal nudity
and bare bottoms, and how much of each, for Fun facts and recommendations.

A sweep runs in its own background thread from the worker cycle, one film at
a time (Settings → Enhance → Nudity index, off by default). It samples one
frame a second, decoded on the graphics card where there is one, and runs
NudeNet, a small open detector that comes with the ``nudity`` extra and runs
on the processor; nothing leaves the server and no picture is kept, only
what was seen and when. It stands aside while Jellyfin is transcoding (they'd
share the card's video decoder) and pauses with chapter work. Animation is
left out: the detector reads cartoons poorly. A film is indexed again only
when its file is replaced.

What's stored is every detection over a low bar, so the thresholds can be
tuned later without reading the files again; ``summary`` turns them into
moments: a detection counts when it holds across two sampled seconds, or is
a confident one on its own.
"""

import importlib.util
import re
import threading
import time
from collections import Counter, defaultdict

from flask import current_app
from sqlalchemy import or_

from . import db, throttle
from .chapters import ChapterConfigError
from .models import ChapterState, Library, MediaIdentity, MediaPart, utcnow

VERSION = 2
# What's indexed, from NudeNet's classes.
KINDS = {
    "breasts": ("FEMALE_BREAST_EXPOSED",),
    "vulva": ("FEMALE_GENITALIA_EXPOSED",),
    "penis": ("MALE_GENITALIA_EXPOSED",),
    "bottoms": ("BUTTOCKS_EXPOSED", "ANUS_EXPOSED"),
}
CLASS_KIND = {name: kind for kind, names in KINDS.items() for name in names}
LABELS = {
    "breasts": "Breasts",
    "vulva": "Full frontal: women",
    "penis": "Full frontal: men",
    "bottoms": "Bare bottoms",
}
FACES = {"FACE_FEMALE": "f", "FACE_MALE": "m"}
KEEP = 0.4  # the small model's detections looked at again
CHECK = 0.5  # the large model's score a detection needs to count
GENITALS = 0.7  # and for genitals, which both models see in fingers and hands
SURE = 0.6  # one on its own counts from here (large model)
FACE = 0.5
# Male full frontal is noted but not shown: in testing, neither model told it
# apart from hands and fingers well enough (a woman's hand over her mouth
# scored higher than real scenes).
VISIBLE = ("breasts", "vulva", "bottoms")
# The large model, checking what the small one flags (one download, on first
# use, as the speech model is).
# Through GitHub's API: the release's own link asks for a sign-in.
LARGE_URL = "https://api.github.com/repos/notAI-tech/NudeNet/releases/assets/176832019"
LARGE_SHA256 = "04fe3d77980780c1f8297dc6d7f942fd5b3abe6942a188f742a85241e4f634eb"
GAP = 10  # seconds: detections closer than this are one moment
SAMPLE = 1  # frames a second
WIDTH, HEIGHT = 1280, 720
THREADS = 2  # the detector's processor threads
PER_RUN = 1
ANIMATION = re.compile(r"anim", re.IGNORECASE)
# Age ratings that rule nudity out, so those films aren't read at all: G and
# PG, and "Approved" (the old Production Code). PG-13 allows brief nudity.
CLEAN_RATINGS = {"G", "PG", "APPROVED", "TV-Y", "TV-Y7", "TV-G", "TV-PG", "U", "UC"}

_thread = None
_lock = threading.Lock()
_detector = None
_large = None


class ModelUnavailable(Exception):
    """The large model couldn't be downloaded: the sweep waits and tries
    again later, leaving films unread rather than failed."""


class NoFrames(RuntimeError):
    """FFmpeg read no frames (decoding on the card can fail for a format)."""


def _enrichment():
    from . import enrichment

    return enrichment


def available() -> bool:
    return importlib.util.find_spec("nudenet") is not None


def enabled() -> bool:
    try:
        return bool(_enrichment().options()["nudity_index"]) and available()
    except ChapterConfigError:
        return False


def _animated(media) -> bool:
    return any(ANIMATION.search(str(g)) for g in media.genres or [])


def _skip_reason(media) -> str | None:
    """Why a film needn't be read: animated, or rated for all ages."""
    from .integrations import IntegrationError
    from .services import client

    if _animated(media):
        return "animation"
    if not media.jellyfin_id:
        return None
    try:
        item = client("jellyfin").item(media.jellyfin_id) or {}
    except IntegrationError:
        return None
    rating = str(item.get("OfficialRating") or "").strip()
    if rating.upper() in CLEAN_RATINGS:
        return f"rated {rating}"
    return None


def _due():
    """Films not indexed yet, or replaced since they were."""
    return (
        db.session.query(MediaPart)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .join(Library, MediaIdentity.library_id == Library.id)
        .outerjoin(ChapterState, ChapterState.media_part_id == MediaPart.id)
        .filter(
            MediaPart.kind == "movie_file",
            MediaPart.has_file.is_(True),
            MediaPart.file_path.isnot(None),
            MediaIdentity.missing_since.is_(None),
            Library.media_type == "movies",
            or_(
                ChapterState.id.is_(None),
                ChapterState.nudity_size.is_(None),
                ChapterState.nudity_size != MediaPart.size_bytes,
            ),
        )
        .order_by(MediaPart.id)
    )


def _transcoding() -> bool:
    """Whether Jellyfin is transcoding: then its streams need the video
    decoder this would share."""
    from . import adaptive

    try:
        return adaptive.viewers(_enrichment().playback_sessions())[1] > 0
    except Exception:  # noqa: BLE001  unreachable Jellyfin: don't hold up
        return False


def start(app=None):
    """Start a sweep in the background unless one is running (worker cycle)."""
    global _thread
    from .services import setting

    if not enabled() or setting("chapters_paused_since"):
        return False
    with _lock:
        if _thread and _thread.is_alive():
            return False
        if not _due().first():
            return False
        app = app or current_app._get_current_object()
        _thread = threading.Thread(
            target=_run, args=(app,), name="curatarr-nudity", daemon=True
        )
        _thread.start()
    return True


def _run(app):
    with app.app_context():
        try:
            sweep()
        except throttle.Interrupted:
            db.session.rollback()  # all work paused; it carries on after resuming
        except Exception:
            db.session.rollback()
            app.logger.exception("Nudity index failed")
        finally:
            db.session.remove()


def sweep(limit=PER_RUN) -> int:
    from .services import setting

    e = _enrichment()
    done = 0
    for part in _due().limit(limit * 10).all():
        if done >= limit:
            break
        if setting("chapters_paused_since") or throttle.stopping() or _transcoding():
            break  # all work paused, or Jellyfin needs the decoder
        state = e.state_for(part)
        reason = _skip_reason(part.media)
        if reason:
            _record(state, part, {"skipped": reason})
            continue
        path = e.local_path(part.file_path)
        if not path or not e.file_exists(path):
            continue  # unreachable for now: later
        db.session.commit()  # never hold the write lock while a film is read
        state_id, part_id = state.id, part.id
        try:
            result = scan(path)
        except ModelUnavailable as exc:
            current_app.logger.warning("Nudity index waiting: %s", exc)
            break  # left unread; tried again next time
        except (OSError, ValueError, RuntimeError) as exc:
            result = {"error": str(exc)[:300]}
        _record(
            db.session.get(ChapterState, state_id),
            db.session.get(MediaPart, part_id),
            result,
        )
        done += 1
    return done


def _record(state, part, result):
    state.nudity = {"v": VERSION, "at": utcnow().isoformat(), **result}
    state.nudity_size = part.size_bytes
    db.session.commit()


# --- Reading a film -----------------------------------------------------------


def _session():
    """NudeNet's detector, with a session limited to a few processor threads
    (left to itself it takes every core)."""
    global _detector
    if _detector is None:
        import os

        import nudenet
        import onnxruntime

        detector = nudenet.NudeDetector()
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = THREADS
        options.inter_op_num_threads = 1
        detector.onnx_session = onnxruntime.InferenceSession(
            os.path.join(os.path.dirname(nudenet.__file__), "320n.onnx"),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        _detector = detector
    return _detector


def _large_model_path():
    """The large model's file, downloaded once and checked."""
    import hashlib
    import tempfile
    from pathlib import Path

    import requests

    folder = Path(current_app.instance_path) / "models"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "nudenet-640m.onnx"
    if path.exists():
        return path
    with tempfile.NamedTemporaryFile(dir=folder, delete=False) as handle:
        partial = Path(handle.name)
        digest = hashlib.sha256()
        try:
            with requests.get(
                LARGE_URL,
                headers={"Accept": "application/octet-stream"},
                stream=True,
                timeout=60,
            ) as response:
                response.raise_for_status()
                for chunk in response.iter_content(1 << 20):
                    handle.write(chunk)
                    digest.update(chunk)
        except requests.RequestException as exc:
            partial.unlink(missing_ok=True)
            raise ModelUnavailable(
                f"Couldn't download the nudity model: {exc}"
            ) from exc
    if digest.hexdigest() != LARGE_SHA256:
        partial.unlink(missing_ok=True)
        raise ModelUnavailable("The downloaded nudity model didn't match its checksum")
    partial.replace(path)
    return path


def _large_session():
    global _large
    if _large is None:
        import nudenet
        import onnxruntime

        path = str(_large_model_path())
        detector = nudenet.NudeDetector(model_path=path, inference_resolution=640)
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = THREADS * 2
        options.inter_op_num_threads = 1
        detector.onnx_session = onnxruntime.InferenceSession(
            path, sess_options=options, providers=["CPUExecutionProvider"]
        )
        _large = detector
    return _large


def _command(path, gpu: bool) -> list[str]:
    from .mediafile import required_tool

    fit = "force_original_aspect_ratio=decrease"
    pad = f"pad={WIDTH}:{HEIGHT}:-1:-1,showinfo"
    if gpu:
        # Decoded and shrunk on the card; only the small frames come back.
        return [
            required_tool("ffmpeg"),
            "-v",
            "info",
            "-nostats",
            "-hwaccel",
            "cuda",
            "-hwaccel_output_format",
            "cuda",
            "-i",
            path,
            "-map",
            "0:v:0",
            "-vf",
            (
                f"fps={SAMPLE},scale_cuda={WIDTH}:{HEIGHT}:{fit}:format=nv12,"
                f"hwdownload,format=nv12,{pad}"
            ),
            "-fps_mode",
            "passthrough",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-",
        ]
    return [
        required_tool("ffmpeg"),
        "-v",
        "info",
        "-nostats",
        "-threads",
        "2",
        "-i",
        path,
        "-map",
        "0:v:0",
        "-vf",
        f"fps={SAMPLE},scale={WIDTH}:{HEIGHT}:{fit},{pad}",
        "-fps_mode",
        "passthrough",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-",
    ]


PTS = re.compile(rb"pts_time:\s*(-?[0-9.]+)")


def frames(path, gpu: bool):
    """(seconds, frame) pairs, one a second, as BGR arrays."""
    import numpy as np

    size = WIDTH * HEIGHT * 3
    times: list[float] = []
    errors: list[bytes] = []

    def read_errors(stream):
        for line in stream:
            found = PTS.search(line)
            if found:
                times.append(float(found.group(1)))
            elif b"rror" in line:
                errors.append(line.strip()[:200])

    with throttle.stream(_command(path, gpu)) as process:
        reader = threading.Thread(target=read_errors, args=(process.stderr,))
        reader.start()
        index = 0
        while True:
            data = process.stdout.read(size)
            if len(data) < size:
                break
            for _ in range(50):  # showinfo's line may come just after its frame
                if len(times) > index:
                    break
                time.sleep(0.01)
            at = times[index] if len(times) > index else index / SAMPLE
            yield at, np.frombuffer(data, np.uint8).reshape(HEIGHT, WIDTH, 3)
            index += 1
        process.wait()
        reader.join(timeout=5)
        if index == 0:
            raise NoFrames(
                (errors[-1].decode(errors="replace") if errors else "")
                or "FFmpeg read no frames"
            )


def detect(frame) -> list[dict]:
    return _session().detect(frame)


def check(frame) -> list[dict]:
    """The large model, on a frame the small one flagged."""
    return _large_session().detect(frame)


def scan(path) -> dict:
    """Every detection the small model makes over KEEP in one film, checked
    by the large model: [seconds, kind, small model's score, who, large
    model's score, a man's bare chest's score]. ``who`` is "f" or "m" when
    the large model saw only women's or men's faces in the frame."""
    from .transcribe import nvidia_gpu

    gpu = bool(nvidia_gpu())
    try:
        seen = _scan(path, gpu)
    except NoFrames:
        if not gpu:
            raise
        seen = _scan(path, False)  # a card that can't decode this format
    return seen


def _best(found, names) -> float:
    return max((d["score"] for d in found if d["class"] in names), default=0.0)


def _scan(path, gpu: bool) -> dict:
    hits, count, last, checked = [], 0, 0.0, 0
    for at, frame in frames(path, gpu):
        count += 1
        last = at
        flagged = {}
        for d in detect(frame):
            kind = CLASS_KIND.get(d["class"])
            if kind and d["score"] >= KEEP:
                flagged[kind] = max(flagged.get(kind, 0.0), d["score"])
        if not flagged:
            continue
        large = check(frame)
        checked += 1
        faces = {
            FACES[d["class"]]
            for d in large
            if d["class"] in FACES and d["score"] >= FACE
        }
        who = faces.pop() if len(faces) == 1 else None
        chest = round(_best(large, {"MALE_BREAST_EXPOSED"}), 2)
        for kind, small in flagged.items():
            hits.append(
                [
                    round(at, 1),
                    kind,
                    round(small, 2),
                    who,
                    round(_best(large, set(KINDS[kind])), 2),
                    chest,
                ]
            )
    return {
        "frames": count,
        "checked": checked,
        "seconds": round(last, 1),
        "hits": hits,
    }


def counts(hit) -> bool:
    """Whether a detection counts: the large model agrees, with more
    certainty for genitals, and a breast isn't a man's bare chest."""
    if len(hit) < 6:
        return False  # read before the large model checked
    _at, kind, _small, who, large, chest = hit[:6]
    if large < (GENITALS if kind in ("vulva", "penis") else CHECK):
        return False
    return not (kind == "breasts" and (chest >= large * 0.8 or who == "m"))


# --- What it adds up to -------------------------------------------------------


def _moments(result) -> dict[str, list[list[tuple]]]:
    """{kind: [moment, ...]}, each moment its (seconds, score, who)
    detections, only the ones that count: two sampled seconds or more, or a
    confident one. Bottoms are also kept as "bottoms_f"/"bottoms_m" when the
    faces in frame said whose."""
    by_kind = defaultdict(list)
    for hit in (result or {}).get("hits") or []:
        if not counts(hit):
            continue
        at, kind, _small, who, score = hit[:5]
        by_kind[kind].append((at, score, who))
        if kind == "bottoms" and who:
            by_kind[f"bottoms_{who}"].append((at, score, who))
    found = {}
    for kind, items in by_kind.items():
        items.sort()
        moments, current = [], []
        for item in items:
            if current and item[0] - current[-1][0] > GAP:
                moments.append(current)
                current = []
            current.append(item)
        if current:
            moments.append(current)
        sure = [
            m
            for m in moments
            if len({round(i[0]) for i in m}) >= 2 or max(i[1] for i in m) >= SURE
        ]
        if sure:
            found[kind] = sure
    return found


def summary(result) -> dict:
    """{kind: {"first": seconds, "scenes": n, "seconds": screen time}} for
    the kinds a film shows; also "bottoms_f"/"bottoms_m" when the faces in
    frame said whose."""
    return {
        kind: {
            "first": sure[0][0][0],
            "scenes": len(sure),
            "seconds": sum(len({round(i[0]) for i in m}) / SAMPLE for m in sure),
        }
        for kind, sure in _moments(result).items()
    }


def timeline(result) -> list[dict]:
    """Every moment, in order: {"kind", "label", "start", "end"}."""
    found = [
        {
            "kind": kind,
            "label": LABELS[kind],
            "start": moment[0][0],
            "end": moment[-1][0] + 1 / SAMPLE,
        }
        for kind, sure in _moments(result).items()
        if kind in VISIBLE
        for moment in sure
    ]
    return sorted(found, key=lambda m: (m["start"], m["kind"]))


def clock(seconds) -> str:
    seconds = int(seconds or 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def for_title(media_id) -> dict | None:
    """A film's summary for its page, or None when it isn't indexed."""
    state = (
        db.session.query(ChapterState)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .filter(
            MediaPart.media_identity_id == media_id, ChapterState.nudity.isnot(None)
        )
        .first()
    )
    if state is None:
        return None
    result = state.nudity or {}
    return {
        "skipped": result.get("skipped"),
        "error": result.get("error"),
        "found": summary(result),
        "timeline": timeline(result),
        "length": result.get("seconds") or state.duration_seconds or 0,
        "at": result.get("at"),
    }


CACHE_SECONDS = 600
SHOWN = 5
_cache: dict = {}


def facts() -> dict | None:
    """The Fun facts rankings, or None before anything's indexed."""
    hit = _cache.get("facts")
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = _facts()
    if value:  # not before the first film's read: it'd hide it for a while
        _cache["facts"] = (time.monotonic(), value)
    return value


def _facts() -> dict | None:
    rows = (
        db.session.query(ChapterState, MediaPart, MediaIdentity)
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .join(MediaIdentity, MediaPart.media_identity_id == MediaIdentity.id)
        .filter(
            ChapterState.nudity.isnot(None),
            MediaPart.kind == "movie_file",
            MediaIdentity.missing_since.is_(None),
        )
        .all()
    )
    films, skipped = [], Counter()
    for state, part, media in rows:
        result = state.nudity or {}
        if result.get("skipped") or result.get("error"):
            skipped[result.get("skipped") or "error"] += 1
            continue
        minutes = (result.get("seconds") or state.duration_seconds or 0) / 60
        films.append(
            {
                "media": media,
                "part": part,
                "found": summary(result),
                "minutes": minutes,
            }
        )
    if not films:
        return None

    def soonest(kind):
        have = [f for f in films if kind in f["found"]]
        have.sort(key=lambda f: f["found"][kind]["first"])
        return [f | {"value": clock(f["found"][kind]["first"])} for f in have[:SHOWN]]

    def most(kind):
        have = [f for f in films if kind in f["found"]]
        have.sort(
            key=lambda f: (f["found"][kind]["scenes"], f["found"][kind]["seconds"]),
            reverse=True,
        )
        return [
            f
            | {
                "value": f["found"][kind]["scenes"],
                "unit": "scene" if f["found"][kind]["scenes"] == 1 else "scenes",
            }
            for f in have[:SHOWN]
        ]

    def per_hour(f):
        seconds = sum(v["seconds"] for k, v in f["found"].items() if k in VISIBLE)
        return seconds / 60 / (f["minutes"] / 60) if f["minutes"] else 0

    overall = sorted(
        (f for f in films if any(k in VISIBLE for k in f["found"])),
        key=per_hour,
        reverse=True,
    )[:SHOWN]
    categories = [
        {
            "id": "nudity-soonest-breasts",
            "title": "Soonest breasts",
            "blurb": "How far in the first bare breasts appear.",
            "films": soonest("breasts"),
        },
        {
            "id": "nudity-most-breasts",
            "title": "Most breasts",
            "blurb": "Scenes with bare breasts.",
            "films": most("breasts"),
        },
        {
            "id": "nudity-frontal-women",
            "title": LABELS["vulva"],
            "blurb": "Scenes with full-frontal female nudity.",
            "films": most("vulva"),
        },
        {
            "id": "nudity-bottoms",
            "title": "Most bare bottoms",
            "blurb": "Scenes with a bare bottom, anyone's.",
            "films": most("bottoms"),
        },
        {
            "id": "nudity-soonest-any",
            "title": "Soonest nudity of any kind",
            "blurb": "How far in anything's on show.",
            "films": sorted(
                (
                    f
                    | {
                        "value": clock(
                            min(
                                v["first"]
                                for k, v in f["found"].items()
                                if k in VISIBLE
                            )
                        )
                    }
                    for f in films
                    if any(k in VISIBLE for k in f["found"])
                ),
                key=lambda f: min(
                    v["first"] for k, v in f["found"].items() if k in VISIBLE
                ),
            )[:SHOWN],
        },
        {
            "id": "nudity-most-overall",
            "title": "Most nudity overall",
            "blurb": "Minutes of nudity an hour, every kind together.",
            "films": [
                f | {"value": round(per_hour(f), 1), "unit": "min an hour"}
                for f in overall
            ],
        },
    ]
    clean = sum(1 for f in films if not any(k in VISIBLE for k in f["found"]))
    return {
        "films": len(films),
        "clean": clean,
        "skipped": dict(skipped),
        "categories": [c for c in categories if c["films"]],
    }


def progress() -> dict:
    """How many films are indexed, and how many there are to index."""
    from sqlalchemy import func

    done = (
        db.session.query(func.count(ChapterState.id))
        .join(MediaPart, ChapterState.media_part_id == MediaPart.id)
        .filter(MediaPart.kind == "movie_file", ChapterState.nudity.isnot(None))
        .scalar()
    )
    return {"indexed": done, "waiting": _due().order_by(None).count()}
