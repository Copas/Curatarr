"""Pictures made by AI, from Curatarr itself (Library → Posters → Generate).

A job asks for one title's pictures (some or all of its types); each picture
asked for is an ArtworkImage, and each try at it an ArtworkCandidate. Workers
(artwork_worker/, on this computer or another) ask Curatarr for work over
HTTP with their own token, make the picture with one of their engines (a
local image model through ComfyUI, or Codex's image tool) and send it back.
Nothing a worker makes goes on in Jellyfin by itself: it waits in staging
until someone approves it, and approving puts it on the way an imported file
is (custom_posters.apply, or import_for_accounts for chosen accounts), so the
original is kept and can be restored.

A worker holds a picture on a lease, renewed as it reports progress. A lease
that runs out (the worker stopped, the network went) puts the picture back in
the queue for another try; Curatarr restarting loses nothing, as everything
is in the database. A picture is tried up to ``max_attempts`` times.
"""

import hashlib
import hmac
import secrets
from datetime import UTC, timedelta
from io import BytesIO
from pathlib import Path

from flask import current_app
from PIL import Image, ImageFilter, ImageStat, UnidentifiedImageError

from . import artwork_prompts, custom_posters, db
from .integrations import IntegrationError
from .models import (
    ArtworkBatch,
    ArtworkCandidate,
    ArtworkImage,
    ArtworkJob,
    ArtworkWorker,
    CustomPoster,
    MediaIdentity,
    utcnow,
)

KEY = "artwork_generation"
DEFAULTS = {
    "concurrency": 2,  # pictures being made at once, across all workers
    "lease_seconds": 300,  # how long a worker holds a picture between reports
    "max_attempts": 3,
    "retention_days": 30,  # rejected and replaced pictures are kept this long
    "batch_window": 4,  # a batch's jobs queued at once
    "defaults": {},  # {picture type: engine} preferred, from Benchmarks
}
# Engines tried first when a job leaves the choice to Curatarr and no default's
# set (docs/ARTWORK.md#results-on-a-12-gb-card): Qwen-Image-Edit keeps people
# recognisable best, then FLUX.2 Klein; Krea 2 letters best but takes no
# photos, so it leads for logos and banners (which have no people).
RECOMMENDED = {
    "Logo": ("comfyui/krea2-turbo",),
    "Banner": ("comfyui/krea2-turbo",),
    "*": (
        "comfyui/qwen-image-edit",
        "comfyui/flux2-klein-9b",
        "comfyui/flux2-klein-4b",
        "comfyui/krea2-turbo",
    ),
}
TOKEN_PREFIX = "cwk_"
ONLINE = timedelta(minutes=2)  # a worker heard from this recently is online
# Picture states, in order. A picture is "running" from assigned to validating.
RUNNING = ("assigned", "preparing", "generating", "validating")
ACTIVE = ("pending", *RUNNING)
DONE = ("completed", "failed", "cancelled")
STATE_NAMES = {
    "pending": "Waiting",
    "assigned": "Assigned",
    "preparing": "Preparing",
    "generating": "Generating",
    "validating": "Checking",
    "review": "Awaiting review",
    "approved": "Approved",
    "importing": "Putting on",
    "completed": "Done",
    "failed": "Failed",
    "cancelled": "Cancelled",
}
JOB_STATE_NAMES = {
    "pending": "Waiting",
    "generating": "Generating",
    "review": "Awaiting review",
    "completed": "Done",
    "failed": "Failed",
    "cancelled": "Cancelled",
}
MAX_RESULT = 40 * 1024 * 1024
MAX_PIXELS = custom_posters.MAX_PIXELS
# How far a picture's shape may be from its type's before it's refused.
ASPECT_TOLERANCE = 0.04


def settings() -> dict:
    from .services import setting

    return DEFAULTS | (setting(KEY) or {})


def save_settings(form) -> dict:
    from .services import set_setting

    current = settings()

    def number(name, low, high):
        try:
            return max(low, min(int(form.get(name)), high))
        except (TypeError, ValueError):
            return current[name]

    options = current | {
        "concurrency": number("concurrency", 1, 16),
        "max_attempts": number("max_attempts", 1, 10),
        "retention_days": number("retention_days", 1, 365),
        "batch_window": number("batch_window", 1, 50),
    }
    set_setting(KEY, options)
    db.session.commit()
    return options


def set_default_engine(kind: str, engine: str | None) -> None:
    """The engine used for ``kind`` when a job leaves it to Curatarr."""
    from .services import set_setting

    options = settings()
    defaults = dict(options.get("defaults") or {})
    if engine:
        defaults[kind] = engine
    else:
        defaults.pop(kind, None)
    set_setting(KEY, options | {"defaults": defaults})
    db.session.commit()


def staging() -> Path:
    folder = Path(current_app.instance_path) / "artwork_staging"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _aware(moment):
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


# --- Workers ----------------------------------------------------------------


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_worker(name: str, max_jobs: int = 1) -> tuple[ArtworkWorker, str]:
    """A new worker and its token, shown once: only its hash is kept."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    worker = ArtworkWorker(
        name=(name or "Worker").strip()[:100],
        token_hash=_hash(token),
        max_jobs=max(1, min(int(max_jobs or 1), 8)),
    )
    db.session.add(worker)
    db.session.commit()
    return worker, token


def new_token(worker: ArtworkWorker) -> str:
    """Replaces the worker's token (the old one stops working)."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    worker.token_hash = _hash(token)
    db.session.commit()
    return token


def authenticate(header: str | None) -> ArtworkWorker | None:
    """The enabled worker an ``Authorization: Bearer …`` header belongs to."""
    if not header or not header.startswith("Bearer "):
        return None
    token = header[7:].strip()
    if not token.startswith(TOKEN_PREFIX):
        return None
    digest = _hash(token)
    worker = db.session.query(ArtworkWorker).filter_by(token_hash=digest).first()
    if worker is None or not hmac.compare_digest(worker.token_hash, digest):
        return None
    return worker if worker.enabled else None


def _clean_capabilities(raw) -> list[dict]:
    """A worker's list of engines, kept to what Curatarr uses."""
    kept = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("engine"), str):
            continue
        kinds = [k for k in entry.get("kinds") or [] if k in custom_posters.KINDS]
        kept.append(
            {
                "engine": entry["engine"][:100],
                "label": str(entry.get("label") or entry["engine"])[:120],
                "kinds": kinds or list(custom_posters.KINDS),
                "references": bool(entry.get("references")),
                "external": bool(entry.get("external")),
                "ready": bool(entry.get("ready", True)),
                "why": str(entry.get("why") or "")[:300],
            }
        )
    return kept[:20]


INFO_FIELDS = (
    "host",
    "system",
    "processor",
    "cpus",
    "ram_mb",
    "gpu",
    "vram_mb",
    "vram_free_mb",
    "version",
)


def heartbeat(worker: ArtworkWorker, report: dict) -> dict:
    """A worker saying it's there, what it can do and how it's doing."""
    worker.last_seen_at = utcnow()
    if "capabilities" in report:
        worker.capabilities = _clean_capabilities(report.get("capabilities"))
    if isinstance(report.get("info"), dict):
        worker.info = {k: report["info"][k] for k in INFO_FIELDS if k in report["info"]}
    if "error" in report:
        worker.last_error = str(report["error"])[:1000] if report["error"] else None
    db.session.commit()
    return {
        "worker": worker.id,
        "name": worker.name,
        "lease": settings()["lease_seconds"],
    }


def touch(worker: ArtworkWorker) -> None:
    """Marks the worker seen, at most once a minute: it asks every few
    seconds, and SQLite has one writer at a time."""
    seen = worker.last_seen_at
    if seen is None or utcnow() - _aware(seen) > timedelta(seconds=60):
        worker.last_seen_at = utcnow()


def online(worker: ArtworkWorker) -> bool:
    if worker.last_seen_at is None:
        return False
    return utcnow() - _aware(worker.last_seen_at) < ONLINE


def engines() -> list[dict]:
    """Every engine an enabled worker offers: {engine, label, kinds,
    references, external, workers: [(name, online, ready, why)]}."""
    found: dict[str, dict] = {}
    for worker in db.session.query(ArtworkWorker).filter_by(enabled=True):
        for entry in worker.capabilities or []:
            item = found.setdefault(
                entry["engine"],
                {k: entry[k] for k in ("engine", "label", "kinds", "references")}
                | {"external": entry["external"], "workers": []},
            )
            item["workers"].append(
                (worker.name, online(worker), entry.get("ready"), entry.get("why"))
            )
    return sorted(found.values(), key=lambda e: e["label"].lower())


def engine_info(engine: str) -> dict | None:
    return next((e for e in engines() if e["engine"] == engine), None)


# --- Jobs -------------------------------------------------------------------


def title_details(media: MediaIdentity) -> dict:
    """What prompts say about a title: its year, genres and a short plot."""
    entry = custom_posters.describe([media], with_originals=False)[0]
    return {
        "title": media.title,
        "year": entry["year"],
        "kind": entry["kind"],
        "genres": entry["genres"],
        "overview": entry["overview"],
        "base": custom_posters._slug(media.title)
        + (f"-{entry['year']}" if entry["year"] else ""),
    }


def create_job(
    media: MediaIdentity | None,
    kinds,
    *,
    accounts=(),
    engine="auto",
    worker_id=None,
    options=None,
    details=None,
    batch=None,
    purpose="library",
    priority=0,
    created_by=None,
) -> ArtworkJob:
    """A job for ``media``'s pictures of ``kinds``. ``details`` (title, year,
    genres, plot) are looked up when not given; a benchmark gives its own."""
    kinds = [k for k in custom_posters.KINDS if k in set(kinds)]
    if not kinds:
        raise ValueError("Choose at least one type of picture to make.")
    options = dict(options or {})
    if media is not None and details is None:
        details = title_details(media)
    options["details"] = details
    engine = engine or "auto"
    if engine != "auto":
        info = engine_info(engine)
        cast = options.get("photos") and set(kinds) & artwork_prompts.CAST_KINDS
        if info and cast and not info["references"]:
            raise ValueError(
                f"{info['label']} can't use reference photos: choose another engine, "
                "or leave the photos out."
            )
        if (
            info
            and info["external"]
            and options.get("photos")
            and not options.get("external_ok")
        ):
            raise ValueError(
                f"{info['label']} sends the reference photos to an outside "
                "service. Tick the box that allows it, or choose a local engine."
            )
    job = ArtworkJob(
        media_identity_id=media.id if media else None,
        batch_id=batch.id if batch else None,
        purpose=purpose,
        accounts=list(accounts or []),
        kinds=kinds,
        engine=engine,
        worker_id=worker_id or None,
        options=options,
        priority=priority,
        created_by=created_by,
    )
    db.session.add(job)
    db.session.flush()
    for position, kind in enumerate(kinds):
        db.session.add(ArtworkImage(job_id=job.id, kind=kind, position=position))
    db.session.flush()
    db.session.refresh(job)
    _refresh(job)
    db.session.commit()
    return job


def _refresh(job: ArtworkJob) -> None:
    """The job's state, from its pictures'."""
    states = [image.state for image in job.images]
    if not states:
        return
    if all(s == "cancelled" for s in states):
        state = "cancelled"
    elif any(s in RUNNING for s in states):
        state = "generating"
    elif any(s == "pending" for s in states):
        state = "pending"
    elif any(s in ("review", "approved", "importing") for s in states):
        state = "review"
    elif any(s == "failed" for s in states):
        state = "failed"
    else:
        state = "completed"
    if state != job.state:
        job.state = state
        job.finished_at = utcnow() if state in DONE else None


def cancel_job(job: ArtworkJob) -> int:
    """Cancels what's not been made yet (a worker making one hears on its
    next report). Pictures waiting for review stay."""
    count = 0
    for image in job.images:
        if image.state in ACTIVE:
            image.state = "cancelled"
            image.finished_at = utcnow()
            image.lease_expires_at = None
            count += 1
    _refresh(job)
    db.session.commit()
    return count


def cancel_waiting() -> tuple[int, int]:
    """Cancels every library job still waiting or being made: (jobs,
    pictures). Pictures waiting for review stay, as with one job."""
    jobs = (
        db.session.query(ArtworkJob)
        .filter(
            ArtworkJob.state.in_(("pending", "generating")),
            ArtworkJob.purpose == "library",
        )
        .all()
    )
    pictures = sum(cancel_job(job) for job in jobs)
    return len(jobs), pictures


def retry_job(job: ArtworkJob) -> int:
    """Tries again the pictures that failed."""
    count = 0
    for image in job.images:
        if image.state == "failed":
            _requeue(image, reset=True)
            count += 1
    _refresh(job)
    db.session.commit()
    return count


def _requeue(image: ArtworkImage, *, reset=False, not_before=None) -> None:
    image.state = "pending"
    image.worker_id = None
    image.lease_expires_at = None
    image.progress = 0
    image.not_before = not_before
    image.finished_at = None
    if reset:
        image.attempts = 0
        image.error = None


# --- Claiming work ----------------------------------------------------------


def _running_count() -> int:
    query = db.session.query(ArtworkImage).filter(ArtworkImage.state.in_(RUNNING))
    return query.count()


def _waiting_on_poster(image: ArtworkImage) -> bool:
    """A set's other pictures follow its poster: they wait until it's made,
    so theirs can match it."""
    if image.kind == "Primary":
        return False
    poster = next((i for i in image.job.images if i.kind == "Primary"), None)
    if poster is None or poster.state in ("cancelled", "failed"):
        return False
    return not poster.candidates


def _pick_engine(image: ArtworkImage, worker: ArtworkWorker, ready: set) -> str | None:
    """The engine this worker would make ``image`` with, or None."""
    job = image.job
    options = job.options or {}
    offered = {
        e["engine"]: e
        for e in worker.capabilities or []
        if e["engine"] in ready and image.kind in e["kinds"]
    }
    needs_people = bool(
        options.get("photos") and image.kind in artwork_prompts.CAST_KINDS
    )

    def usable(engine):
        entry = offered.get(engine)
        if entry is None:
            return False
        if needs_people and not entry["references"]:
            return False
        if entry["external"] and needs_people:
            return bool(options.get("external_ok"))
        return True

    if job.engine != "auto":
        return job.engine if usable(job.engine) else None
    preferred = (settings().get("defaults") or {}).get(image.kind)
    if preferred and usable(preferred):
        return preferred
    # Otherwise the local engine that did best in the benchmarks for this
    # type, then any local one; an outside service only when the job allows
    # it and nothing local can.
    for engine in RECOMMENDED.get(image.kind, ()) + RECOMMENDED["*"]:
        if engine in offered and not offered[engine]["external"] and usable(engine):
            return engine
    for entry in offered.values():
        if not entry["external"] and usable(entry["engine"]):
            return entry["engine"]
    # Nothing local can: an outside service, where the job allows one.
    if options.get("external_ok"):
        for entry in offered.values():
            if entry["external"] and usable(entry["engine"]):
                return entry["engine"]
    return None


def waiting_reason(image: ArtworkImage, now=None) -> str | None:
    """Why a picture that's waiting hasn't started, in words, with what to
    do about it; None when it's simply next in line."""
    from .pause import state as paused

    if image.state != "pending":
        return None
    now = now or utcnow()
    job = image.job
    if image.not_before and _aware(image.not_before) > now:
        when = _aware(image.not_before).astimezone().strftime("%H:%M")
        return (
            image.message or "It's waiting before trying again."
        ) + f" (until {when})"
    if paused():
        return "Curatarr's work is paused; it starts when work resumes."
    if job.batch is not None and job.batch.state == "paused":
        return "Its batch is paused (Posters → Batches)."
    if _waiting_on_poster(image):
        return (
            "Waiting for the new poster this job is making: it's sent with it, so "
            "the set matches it."
        )
    workers = [
        w
        for w in db.session.query(ArtworkWorker).filter_by(enabled=True)
        if online(w) and (job.worker_id is None or w.id == job.worker_id)
    ]
    if not workers:
        if job.worker_id and job.worker is not None:
            return f"It's for {job.worker.name}, which isn't online."
        return "No worker is online. Posters → Workers shows how to start one."
    for worker in workers:
        ready = {e["engine"] for e in worker.capabilities or [] if e.get("ready")}
        if _pick_engine(image, worker, ready):
            if _running_count() >= settings()["concurrency"]:
                return "Waiting for a free place: other pictures are being made."
            return None  # its worker picks it up on its next look (seconds)
    offered = [e for w in workers for e in w.capabilities or []]
    not_ready = [e for e in offered if not e.get("ready")]
    external = [e for e in offered if e.get("external") and e.get("ready")]
    options = job.options or {}
    if (
        job.engine == "auto"
        and external
        and not options.get("external_ok")
        and not any(not e.get("external") and e.get("ready") for e in offered)
    ):
        return (
            f"Its engine is Best available, which only uses engines on your own "
            f"computers unless the job allows an outside service, and your "
            f"workers only offer {external[0]['label']}, an outside one. Allow it "
            "(or choose it) on this job's page."
        )
    if job.engine != "auto":
        entry = next((e for e in offered if e["engine"] == job.engine), None)
        if entry is None:
            return f"No online worker offers {job.engine}."
        if not entry.get("ready"):
            return f"{entry['label']} isn't ready: {entry.get('why') or 'see Workers'}."
        if (
            entry.get("external")
            and options.get("photos")
            and not options.get("external_ok")
        ):
            return (
                f"{entry['label']} sends the reference photos to an outside "
                "service, and this job doesn't allow that. Allow it on this job's page."
            )
    if not_ready:
        return f"{not_ready[0]['label']} isn't ready: {not_ready[0].get('why') or ''}"
    return "No online worker has an engine that can make it."


def default_audience(image: ArtworkImage) -> tuple[bool, list[str]]:
    """Who a new picture starts out ticked for: the people who have a custom
    picture of its type now (replacing it keeps the same people), else whom
    the job was made for. Returns (everyone, accounts)."""
    rows = (
        db.session.query(CustomPoster.jellyfin_user_id)
        .filter_by(
            media_identity_id=image.job.media_identity_id,
            image_type=image.kind,
            active=True,
        )
        .all()
    )
    if rows:
        users = [u for (u,) in rows]
        return None in users, sorted(u for u in users if u)
    accounts = list(image.job.accounts or [])
    return not accounts, accounts


def set_audience(job: ArtworkJob) -> tuple[bool, list[str]]:
    """The same for a whole set: anyone who'd be ticked for any of it."""
    everyone, accounts = False, set()
    for image in job.images:
        some, who = default_audience(image)
        everyone = everyone or some
        accounts.update(who)
    return everyone, sorted(accounts)


def live_key(image: ArtworkImage) -> str:
    """What a picture's panel shows, in short: it changes when the panel
    needs redrawing (its state, a new version, why it's waiting), not with
    its progress, which pages update in place."""
    kept = [c.id for c in image.candidates if c.path and c.decision != "rejected"]
    why = waiting_reason(image) if image.state == "pending" else None
    if image.state == "failed":
        why = image.error
    return (
        f"{image.state}|{kept[-1] if kept else ''}|{len(image.candidates)}|{why or ''}"
    )


def job_key(job: ArtworkJob) -> str:
    """The same for a job's row on Jobs: its state and its pictures'."""
    return job.state + "|" + "||".join(live_key(i) for i in job.images)


def head_key(job: ArtworkJob) -> str:
    """And for the top of a job's page (its state and the buttons it offers)."""
    states = [i.state for i in job.images]
    return "|".join(
        [job.state, job.engine]
        + [str(states.count(s)) for s in ("review", "failed", "pending")]
    )


def set_engine(job: ArtworkJob, engine: str, external_ok: bool) -> int:
    """Changes the engine of a job's pictures that haven't started."""
    options = dict(job.options or {})
    info = engine_info(engine) if engine != "auto" else None
    if engine != "auto" and info is None:
        raise ValueError("No worker offers that engine.")
    if info and info["external"] and options.get("photos") and not external_ok:
        raise ValueError(
            f"{info['label']} sends the reference photos to an outside service: "
            "tick the box that allows it."
        )
    options["external_ok"] = bool(external_ok)
    job.options = options
    job.engine = engine
    waiting = [i for i in job.images if i.state == "pending"]
    for image in waiting:
        image.not_before = None
        image.message = None
    db.session.commit()
    return len(waiting)


def claim(worker: ArtworkWorker, slots: int, ready) -> list[dict]:
    """Up to ``slots`` pictures for ``worker`` to make with its ``ready``
    engines, each with its task."""
    from .pause import state as paused

    touch(worker)
    ready = set(ready or [])
    options = settings()
    mine = (
        db.session.query(ArtworkImage)
        .filter(ArtworkImage.state.in_(RUNNING), ArtworkImage.worker_id == worker.id)
        .count()
    )
    room = min(
        max(0, slots),
        max(0, worker.max_jobs - mine),
        max(0, options["concurrency"] - _running_count()),
    )
    if not room or not ready or paused():
        if db.session.dirty:
            db.session.commit()
        return []
    now = utcnow()
    waiting = (
        db.session.query(ArtworkImage)
        .join(ArtworkJob, ArtworkImage.job_id == ArtworkJob.id)
        .outerjoin(ArtworkBatch, ArtworkJob.batch_id == ArtworkBatch.id)
        .filter(
            ArtworkImage.state == "pending",
            (ArtworkImage.not_before.is_(None)) | (ArtworkImage.not_before <= now),
            (ArtworkJob.worker_id.is_(None)) | (ArtworkJob.worker_id == worker.id),
            (ArtworkBatch.id.is_(None)) | (ArtworkBatch.state == "active"),
        )
        .order_by(
            ArtworkJob.priority.desc(),
            ArtworkJob.created_at,
            ArtworkImage.position,
        )
        .limit(200)
        .all()
    )
    tasks = []
    for image in waiting:
        if len(tasks) >= room:
            break
        if _waiting_on_poster(image):
            continue
        engine = _pick_engine(image, worker, ready)
        if engine is None:
            continue
        entry = next(e for e in worker.capabilities if e["engine"] == engine)
        image.state = "assigned"
        image.worker_id = worker.id
        image.engine = engine
        image.attempts += 1
        image.started_at = now
        image.progress = 0
        image.message = None
        image.lease_expires_at = now + timedelta(seconds=options["lease_seconds"])
        image.seed = secrets.randbelow(2**31)  # a new try is a new picture
        tasks.append(build_task(image, engine, agent=entry["external"]))
        _refresh(image.job)
    if tasks or db.session.dirty:
        db.session.commit()
    return tasks


def _set_picture(image: ArtworkImage) -> Path | None:
    """The poster a set's other pictures match: the one approved, else the
    latest made in this job, else the title's custom poster."""
    poster = next((i for i in image.job.images if i.kind == "Primary"), None)
    if poster is not None and poster.candidates:
        chosen = next(
            (c for c in poster.candidates if c.id == poster.candidate_id), None
        )
        kept = [c for c in poster.candidates if c.path and c.decision != "rejected"]
        pick = chosen or (kept[-1] if kept else None)
        if pick and pick.path and Path(pick.path).is_file():
            return Path(pick.path)
    media_id = image.job.media_identity_id
    if media_id:
        for user_id in image.job.accounts or [None]:
            existing = custom_posters.active(media_id, user_id, "Primary")
            if existing and Path(existing.image_path).is_file():
                return Path(existing.image_path)
    return None


ORIGINALS_FOR_A_DAY = 86400  # seconds an original fetched from Jellyfin is reused


def original_art(media: MediaIdentity, kind: str) -> Path | None:
    """The title's own picture of ``kind`` from before any custom one, as a
    file: beside its media when Jellyfin saves art there, else from Jellyfin
    (the one saved before a custom picture for everyone, or Jellyfin's own
    when only some accounts have their own). None when it has none."""
    import time

    on_disk = custom_posters.originals(media).get(kind)
    if on_disk:
        return Path(on_disk)
    everyone = custom_posters.active(media.id, kind=kind)
    if everyone is not None:
        saved = Path(everyone.original_path) if everyone.original_path else None
        return saved if saved and saved.is_file() else None
    folder = staging().parent / "artwork_originals"
    folder.mkdir(parents=True, exist_ok=True)
    cached = folder / f"{media.id}-{kind}{custom_posters._extension(kind)}"
    try:
        if time.time() - cached.stat().st_mtime < ORIGINALS_FOR_A_DAY:
            return cached
    except OSError:
        pass
    from .services import client

    try:
        data = custom_posters._original(media, client("jellyfin"), kind)
    except IntegrationError:
        return cached if cached.is_file() else None
    if not data:
        return None
    cached.write_bytes(data)
    return cached


def build_task(image: ArtworkImage, engine: str, agent: bool) -> dict:
    """What the worker is sent: the prompt, its references (as addresses it
    fetches with its token) and the checks to run. The references' paths are
    kept with the picture, never sent."""
    job = image.job
    options = job.options or {}
    details = options.get("details") or {"title": "Untitled"}
    references: list[dict] = []

    def add(path, role, person=None):
        references.append({"path": str(path), "role": role, "person": person})
        return len(references)

    people_refs: dict[str, list[int]] = {}
    for who, photos in artwork_prompts.cast(options, image.kind, agent).items():
        people_refs[who] = [add(p, "person", who) for p in photos]
    set_number = None
    # A banner from an image model isn't given the poster: it has no people,
    # and given one with people, the models put them in.
    skip_set = ("Primary", "Logo") if agent else ("Primary", "Logo", "Banner")
    if image.kind not in skip_set:
        poster = _set_picture(image)
        if poster is not None:
            set_number = add(poster, "set")
    originals = {}
    lettering = None
    if options.get("originals") and job.media is not None:
        if agent:
            # Its poster for the feel and its logo for the lettering, and its
            # own picture of this type, as tonal references.
            kinds = ["Primary", "Logo"]
            if image.kind not in kinds:
                kinds.append(image.kind)
            for kind in kinds:
                path = original_art(job.media, kind)
                if path is not None:
                    originals[kind] = add(path, "original")
        elif image.kind in artwork_prompts.LETTERED_KINDS:
            path = original_art(job.media, "Logo")
            if path is not None:
                lettering = add(path, "lettering")
    if agent:
        prompt = artwork_prompts.agent_prompt(
            image.kind,
            details,
            options,
            people_refs,
            set_image=set_number,
            originals=originals,
            feedback=image.feedback,
        )
    else:
        prompt = artwork_prompts.model_prompt(
            image.kind,
            details,
            options,
            people_refs,
            set_image=set_number,
            lettering_image=lettering,
            feedback=image.feedback,
        )
    image.prompt = prompt
    image.task = {"references": references}
    width, height = artwork_prompts.MODEL_SIZES[image.kind]
    return {
        "image": image.id,
        "job": job.id,
        "kind": image.kind,
        "engine": engine,
        "seed": image.seed,
        "prompt": prompt,
        "title": details.get("title"),
        "lettered": image.kind in artwork_prompts.LETTERED_KINDS,
        "size": [width, height],
        "aspect": artwork_prompts.ASPECTS[image.kind],
        "transparent": image.kind == "Logo",
        "references": [
            {"number": n, "role": r["role"], "person": r["person"]}
            for n, r in enumerate(references, 1)
        ],
        "people": people_refs,
        "attempt": image.attempts,
        "lease": settings()["lease_seconds"],
        "feedback": image.feedback,
    }


def reference_path(worker: ArtworkWorker, image_id: str, number: int) -> Path | None:
    """A reference picture of a task the worker holds."""
    image = db.session.get(ArtworkImage, image_id)
    if image is None or image.worker_id != worker.id or image.state not in RUNNING:
        return None
    references = (image.task or {}).get("references") or []
    if not 1 <= number <= len(references):
        return None
    path = Path(references[number - 1]["path"])
    return path if path.is_file() else None


def _held(worker: ArtworkWorker, image_id: str) -> ArtworkImage | None:
    image = db.session.get(ArtworkImage, image_id)
    if image is None or image.worker_id != worker.id:
        return None
    return image


def report(worker: ArtworkWorker, image_id: str, update: dict) -> dict:
    """A worker's progress on a picture; renews its lease. Says whether to
    stop (the picture was cancelled, or given to another worker)."""
    touch(worker)
    image = _held(worker, image_id)
    if image is None or image.state not in RUNNING:
        db.session.commit()
        return {"cancel": True}
    if update.get("state") in ("preparing", "generating", "validating"):
        image.state = update["state"]
    try:
        image.progress = max(0, min(int(update.get("progress") or 0), 100))
    except (TypeError, ValueError):
        pass
    if update.get("message"):
        image.message = str(update["message"])[:500]
    image.lease_expires_at = utcnow() + timedelta(seconds=settings()["lease_seconds"])
    _refresh(image.job)
    db.session.commit()
    return {"cancel": False}


def fail(worker: ArtworkWorker, image_id: str, report_: dict) -> dict:
    """A worker couldn't make a picture. A usage limit or a busy machine
    isn't the picture's fault: it waits, without using up a try."""
    image = _held(worker, image_id)
    if image is None or image.state not in RUNNING:
        return {"ok": False}
    error = str(report_.get("error") or "The worker couldn't make it.")[:2000]
    code = report_.get("code")
    image.error = error
    if code in ("usage_limit", "busy", "unavailable"):
        image.attempts = max(0, image.attempts - 1)
        try:
            wait = max(60, min(int(report_.get("retry_after") or 600), 7 * 86400))
        except (TypeError, ValueError):
            wait = 600
        _requeue(image, not_before=utcnow() + timedelta(seconds=wait))
        image.message = error[:500]
    elif code == "refused" or image.attempts >= settings()["max_attempts"]:
        image.state = "failed"
        image.message = error[:500]
        image.finished_at = utcnow()
        image.lease_expires_at = None
    else:
        _requeue(image, not_before=utcnow() + timedelta(seconds=30))
        image.message = f"Trying again: {error}"[:500]
    _refresh(image.job)
    db.session.commit()
    return {"ok": True}


def recover(now=None) -> int:
    """Worker cycle: pictures whose worker stopped reporting go back in the
    queue (or fail, after their last try). Returns how many."""
    now = now or utcnow()
    expired = (
        db.session.query(ArtworkImage)
        .filter(
            ArtworkImage.state.in_(RUNNING),
            ArtworkImage.lease_expires_at.isnot(None),
            ArtworkImage.lease_expires_at < now,
        )
        .all()
    )
    for image in expired:
        image.error = "Its worker stopped reporting progress."
        if image.attempts >= settings()["max_attempts"]:
            image.state = "failed"
            image.message = image.error
            image.finished_at = now
            image.lease_expires_at = None
        else:
            _requeue(image)
            image.message = "Its worker stopped reporting; waiting for another try."
        _refresh(image.job)
    stale = now - timedelta(minutes=10)
    for image in db.session.query(ArtworkImage).filter_by(state="importing"):
        # Curatarr stopped while putting it on: back to review.
        if image.updated_at and _aware(image.updated_at) < stale:
            image.state = "review"
            _refresh(image.job)
    db.session.commit()
    return len(expired)


# --- Results ----------------------------------------------------------------


def check_picture(data: bytes, kind: str) -> dict:
    """Facts about a picture made for ``kind`` (Pillow only), and what's
    wrong with it: {"width", "height", "format", "has_alpha", "problems",
    "metrics"}. A problem means it can't be used."""
    if len(data) > MAX_RESULT:
        return {"problems": ["It's larger than 40 MB."]}
    try:
        with Image.open(BytesIO(data)) as probe:
            width, height = probe.size
            fmt = probe.format
            if width * height > MAX_PIXELS:
                return {"problems": ["It has too many pixels."]}
            image = probe.convert("RGBA")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        return {"problems": ["It isn't a picture Curatarr can read."]}
    if fmt not in ("PNG", "JPEG", "WEBP"):
        return {"problems": [f"{fmt} pictures aren't accepted."]}
    problems = []
    metrics = {}
    alpha = image.getchannel("A")
    clear = sum(alpha.histogram()[:16]) / (width * height)
    has_alpha = clear > 0.01
    metrics["transparent_share"] = {"value": round(clear, 3), "kind": "measured"}
    want = artwork_prompts.ASPECTS[kind]
    if want:
        off = abs(width / height - want) / want
        metrics["shape_error"] = {"value": round(off, 3), "kind": "measured"}
        if off > ASPECT_TOLERANCE:
            problems.append(
                f"It's {width} × {height}, not the shape of a "
                f"{custom_posters.KIND_NAMES[kind]}."
            )
    if kind == "Logo":
        if not has_alpha:
            problems.append("A logo must have a transparent background.")
        elif clear > 0.97:
            problems.append("The logo is almost all transparent: nothing's in it.")
    smallest = {"Banner": 600, "Logo": 400}.get(kind, 900)
    if max(width, height) < smallest:
        problems.append(f"At {width} × {height} it's too small.")
    grey = image.convert("L")
    small = grey.resize((max(1, width // 4), max(1, height // 4)))
    edges = small.filter(ImageFilter.FIND_EDGES)
    metrics["sharpness"] = {
        "value": round(ImageStat.Stat(edges).var[0], 1),
        "kind": "measured",
        "note": "edge variance; higher is crisper, but busy pictures score high too",
    }
    spread = ImageStat.Stat(grey).stddev[0]
    metrics["contrast"] = {"value": round(spread, 1), "kind": "measured"}
    if spread < 4 and kind != "Logo":
        problems.append("It's a blank or nearly blank picture.")
    return {
        "width": width,
        "height": height,
        "format": fmt,
        "has_alpha": has_alpha,
        "problems": problems,
        "metrics": metrics,
    }


def _number(meta: dict, name: str, kind=float):
    try:
        return kind(meta[name]) if meta.get(name) is not None else None
    except (TypeError, ValueError):
        return None


def submit(worker: ArtworkWorker, image_id: str, data: bytes, meta: dict) -> dict:
    """A picture a worker made: checked, kept in staging and put up for
    review. Returns {"ok", "problems"}; a refused picture counts as a try."""
    image = _held(worker, image_id)
    if image is None or image.state not in RUNNING:
        return {"ok": False, "problems": ["This picture isn't yours to send."]}
    facts = check_picture(data, image.kind)
    if facts["problems"]:
        fail(
            worker,
            image_id,
            {"error": "Curatarr refused it: " + " ".join(facts["problems"])},
        )
        return {"ok": False, "problems": facts["problems"]}
    version = len(image.candidates) + 1
    keep_png = facts["format"] == "PNG" and (facts["has_alpha"] or image.kind == "Logo")
    extension = ".png" if keep_png else ".jpg"
    if not keep_png and facts["format"] != "JPEG":
        with Image.open(BytesIO(data)) as picture:
            buffer = BytesIO()
            picture.convert("RGB").save(buffer, "JPEG", quality=93)
            data = buffer.getvalue()
    folder = staging() / image.job_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{image.id}-v{version}{extension}"
    path.write_bytes(data)
    metrics = dict(facts["metrics"])
    for name, value in (meta.get("metrics") or {}).items():
        if isinstance(name, str) and isinstance(value, dict):
            metrics[name[:40]] = {
                k: value[k] for k in ("value", "kind", "note", "detail") if k in value
            }
    candidate = ArtworkCandidate(
        image_id=image.id,
        version=version,
        path=str(path),
        width=facts["width"],
        height=facts["height"],
        has_alpha=facts["has_alpha"],
        size_bytes=len(data),
        engine=image.engine,
        worker_id=worker.id,
        seed=_number(meta, "seed", int) if meta.get("seed") is not None else image.seed,
        prompt=image.prompt,
        params=meta.get("params") if isinstance(meta.get("params"), dict) else None,
        steps=meta.get("steps") if isinstance(meta.get("steps"), list) else None,
        metrics=metrics,
        duration_s=_number(meta, "duration_s"),
        peak_vram_mb=_number(meta, "peak_vram_mb", int),
        peak_ram_mb=_number(meta, "peak_ram_mb", int),
    )
    db.session.add(candidate)
    image.state = "review"
    image.progress = 100
    image.message = None
    image.error = None
    image.lease_expires_at = None
    image.finished_at = utcnow()
    image.feedback = None
    _refresh(image.job)
    db.session.commit()
    return {"ok": True, "candidate": candidate.id}


# --- Review -----------------------------------------------------------------


def _source(image: ArtworkImage) -> str:
    """The name a generated picture is recorded under: the import folder's
    naming, so it reads like an imported file."""
    details = (image.job.options or {}).get("details") or {}
    return custom_posters.file_name(details.get("base") or "generated", image.kind)


def in_use(media_id, kind, accounts) -> list[dict]:
    """Every version of a title's picture someone sees now: {"poster"
    (CustomPoster, or None for Jellyfin's own), "who": [names]}, with
    accounts that see the same picture together. ``accounts`` is [(id,
    name)]; an account without its own picture sees everyone's."""
    versions: dict[object, dict] = {}

    def key(poster):
        if poster is None:
            return "jellyfin"
        try:
            return ("size", Path(poster.image_path).stat().st_size)
        except OSError:
            return ("id", poster.id)

    everyone = custom_posters.active(media_id, kind=kind)
    shared = {"poster": everyone, "who": ["Everyone else"]}
    versions[key(everyone)] = shared
    for user_id, name in accounts:
        own = custom_posters.active(media_id, user_id, kind)
        if own is None:
            continue
        found = versions.setdefault(key(own), {"poster": own, "who": []})
        if found is shared:
            found["who"].insert(0, name)
        else:
            found["who"].append(name)
    return list(versions.values())


def _put_on(media, data, source, kind, everyone, accounts) -> None:
    if everyone:
        custom_posters.apply(media, data, source, kind=kind)
        picture = custom_posters.normalise(data, kind)
        for user_id in accounts:  # their own picture, the same
            mine = custom_posters.active(media.id, user_id, kind)
            kept = Path(mine.image_path) if mine else None
            if (
                kept
                and kept.exists()
                and custom_posters._same_picture(picture, kept.read_bytes())
            ):
                continue
            custom_posters._for_account(media, picture, source, user_id, kind)
    else:
        custom_posters.import_for_accounts(media, data, source, accounts, kind)


def approve(candidate: ArtworkCandidate, by=None, everyone=None, accounts=None) -> str:
    """Puts ``candidate`` on in Jellyfin the way an imported file is: for
    everyone (Jellyfin's own picture) and/or for chosen ``accounts`` (their
    own, through the plugin). Left out, it's whom the job was made for.
    Returns what happened, for the page."""
    image = candidate.image
    job = image.job
    if job.purpose == "benchmark" or job.media is None:
        raise ValueError("Benchmark pictures are rated, not put on.")
    if image.state not in ("review", "completed", "failed", "cancelled"):
        raise ValueError("This picture is still being made.")
    if not candidate.path or not Path(candidate.path).is_file():
        raise ValueError("This picture's file is gone (removed after its retention).")
    if everyone is None and accounts is None:
        accounts = list(job.accounts or [])
        everyone = not accounts
    accounts = list(accounts or [])
    if not everyone and not accounts:
        raise ValueError("Choose who sees it: everyone, or some accounts.")
    before = image.state
    image.state = "importing"
    db.session.commit()
    data = Path(candidate.path).read_bytes()
    source = _source(image)
    media, kind = job.media, image.kind
    try:
        _put_on(media, data, source, kind, everyone, accounts)
    except (IntegrationError, ValueError, OSError):
        db.session.rollback()
        image = db.session.get(ArtworkImage, image.id)
        image.state = before
        _refresh(image.job)
        db.session.commit()
        raise
    candidate = db.session.get(ArtworkCandidate, candidate.id)
    image = candidate.image
    now = utcnow()
    for other in image.candidates:
        if other.id != candidate.id and other.decision is None:
            other.decision = "superseded"
            other.decided_at = now
    candidate.decision = "approved"
    candidate.decided_at = now
    image.candidate_id = candidate.id
    image.state = "completed"
    image.finished_at = now
    image.message = None
    _refresh(image.job)
    db.session.commit()
    names = [custom_posters.account_name(a) for a in accounts]
    who = ", ".join((["everyone"] if everyone else []) + names)
    return f"{media.title}: its {custom_posters._noun(kind)} is on for {who}."


def approve_set(job: ArtworkJob, by=None, everyone=None, accounts=None) -> list[str]:
    """Approves each picture of the job waiting for review (its latest), for
    the same people."""
    done = []
    for image in list(job.images):
        if image.state != "review":
            continue
        waiting = [c for c in image.candidates if c.decision is None and c.path]
        if waiting:
            done.append(approve(waiting[-1], by, everyone, accounts))
    return done


def reject(candidate: ArtworkCandidate, feedback=None, again=False) -> None:
    """Turns ``candidate`` down; with ``again``, the picture is made again
    (with ``feedback`` added to its prompt). The job's other pictures stay
    as they are."""
    image = candidate.image
    candidate.decision = "rejected"
    candidate.decided_at = utcnow()
    candidate.feedback = (feedback or "").strip()[:1000] or None
    if again:
        regenerate(image, feedback, commit=False)
    elif image.state == "review" and not any(
        c.decision is None for c in image.candidates
    ):
        if image.candidate_id is None:
            image.state = "cancelled"
            image.message = "Turned down."
        else:
            image.state = "completed"  # the one approved before stays
        image.finished_at = utcnow()
    _refresh(image.job)
    db.session.commit()


def regenerate(image: ArtworkImage, feedback=None, commit=True) -> None:
    """Makes ``image`` again, keeping its earlier versions."""
    if image.state in RUNNING or image.state == "importing":
        raise ValueError("It's being made right now.")
    for other in image.candidates:
        if other.decision is None:
            other.decision = "superseded"
            other.decided_at = utcnow()
    _requeue(image, reset=True)
    image.feedback = (feedback or "").strip()[:1000] or None
    image.message = None
    _refresh(image.job)
    if commit:
        db.session.commit()


def prune(now=None) -> int:
    """Worker cycle: deletes the files of pictures turned down or replaced
    longer ago than the retention. Their records stay."""
    now = now or utcnow()
    cutoff = now - timedelta(days=settings()["retention_days"])
    old = (
        db.session.query(ArtworkCandidate)
        .filter(
            ArtworkCandidate.decision.in_(("rejected", "superseded")),
            ArtworkCandidate.decided_at < cutoff,
            ArtworkCandidate.path.isnot(None),
        )
        .limit(200)
        .all()
    )
    for candidate in old:
        try:
            Path(candidate.path).unlink(missing_ok=True)
        except OSError:
            continue
        candidate.path = None
    db.session.commit()
    return len(old)


# --- Batches ----------------------------------------------------------------


def _have(accounts) -> dict[str, set]:
    """{title: picture types it has} for ``accounts`` (or everyone's)."""
    have: dict[str, set] = {}
    rows = db.session.query(
        CustomPoster.media_identity_id,
        CustomPoster.image_type,
        CustomPoster.jellyfin_user_id,
    ).filter_by(active=True)
    for media_id, kind, user_id in rows:
        if (user_id in accounts) if accounts else user_id is None:
            have.setdefault(media_id, set()).add(kind)
    return have


def batch_titles(spec: dict) -> list[MediaIdentity]:
    """The titles a batch would make pictures for: the prompt writer's
    choice (favourites, one account's, films or shows), less those that
    already have every type asked for (for the accounts it's for) when
    ``skip_done``, and those with a job under way."""
    kinds = spec.get("kinds") or ["Primary"]
    titles = custom_posters.prompt_titles(spec.get("titles") or "favourites", False)
    have = _have(spec.get("accounts") or [])
    if spec.get("alternatives"):
        # Alternatives to compare: the titles that have pictures of these
        # types, most recently made first.
        latest = _latest_applied(spec.get("accounts") or [])
        titles = [m for m in titles if set(kinds) & have.get(m.id, set())]
        titles.sort(key=lambda m: latest.get(m.id, ""), reverse=True)
    elif spec.get("skip_done", True):
        titles = [m for m in titles if not set(kinds) <= have.get(m.id, set())]
    busy = {
        media_id
        for (media_id,) in db.session.query(ArtworkJob.media_identity_id).filter(
            ArtworkJob.state.in_(("pending", "generating", "review")),
            ArtworkJob.purpose == "library",
        )
    }
    return [m for m in titles if m.id not in busy]


def _latest_applied(accounts) -> dict[str, str]:
    """{title: when its newest custom picture (for ``accounts``) went on}."""
    latest: dict[str, str] = {}
    rows = db.session.query(
        CustomPoster.media_identity_id,
        CustomPoster.jellyfin_user_id,
        CustomPoster.applied_at,
    ).filter_by(active=True)
    for media_id, user_id, applied in rows:
        if ((user_id in accounts) if accounts else user_id is None) and applied:
            when = _aware(applied).isoformat()
            latest[media_id] = max(latest.get(media_id, ""), when)
    return latest


def create_batch(spec: dict, name=None, created_by=None) -> ArtworkBatch:
    titles = batch_titles(spec)
    if not titles:
        raise ValueError("There's nothing to make: every title chosen has them all.")
    batch = ArtworkBatch(
        name=(name or "Missing artwork")[:200],
        purpose="library",
        spec=spec,
        media_ids=[m.id for m in titles],
        created_by=created_by,
    )
    db.session.add(batch)
    db.session.commit()
    fill_batches()
    return batch


def _missing_kinds(media_id, kinds, accounts) -> list[str]:
    have = _have(accounts or []).get(media_id, set())
    return [k for k in kinds if k not in have]


def fill_batches() -> int:
    """Worker cycle: keeps each active batch's queue topped up, a few jobs at
    a time, so a library-wide batch never puts thousands in the queue at
    once. A title that got its pictures meanwhile is skipped."""
    window = settings()["batch_window"]
    added = 0
    for batch in db.session.query(ArtworkBatch).filter_by(
        state="active", purpose="library"
    ):
        spec = batch.spec or {}
        open_jobs = (
            db.session.query(ArtworkJob)
            .filter(
                ArtworkJob.batch_id == batch.id,
                ArtworkJob.state.in_(("pending", "generating")),
            )
            .count()
        )
        ids = batch.media_ids or []
        while open_jobs < window and batch.queued < len(ids):
            media = db.session.get(MediaIdentity, ids[batch.queued])
            batch.queued += 1
            if media is None or media.missing_since is not None:
                continue
            kinds = spec.get("kinds") or ["Primary"]
            if spec.get("alternatives"):
                have = _have(spec.get("accounts") or []).get(media.id, set())
                kinds = [k for k in kinds if k in have]
                if not kinds:
                    continue
            elif spec.get("skip_done", True):
                kinds = _missing_kinds(media.id, kinds, spec.get("accounts"))
                if not kinds:
                    continue
            try:
                create_job(
                    media,
                    kinds,
                    accounts=spec.get("accounts") or [],
                    engine=spec.get("engine") or "auto",
                    worker_id=spec.get("worker") or None,
                    options=spec.get("options") or {},
                    batch=batch,
                    created_by=batch.created_by,
                )
            except ValueError:
                continue
            open_jobs += 1
            added += 1
        if batch.queued >= len(ids) and not open_jobs:
            batch.state = "done"
            batch.finished_at = utcnow()
    db.session.commit()
    return added


def batch_progress(batch: ArtworkBatch) -> dict:
    """{total, queued, counts (by state), remaining, estimate (seconds) or
    None}."""
    counts: dict[str, int] = {}
    for (state,) in (
        db.session.query(ArtworkImage.state)
        .join(ArtworkJob)
        .filter(ArtworkJob.batch_id == batch.id)
    ):
        counts[state] = counts.get(state, 0) + 1
    total = len(batch.media_ids or [])
    kinds = len((batch.spec or {}).get("kinds") or ["Primary"])
    remaining = sum(counts.get(s, 0) for s in ACTIVE)
    if batch.state in ("active", "paused"):
        remaining += max(0, total - batch.queued) * kinds
    return {
        "total": total,
        "queued": batch.queued,
        "counts": counts,
        "remaining": remaining,
        "estimate": estimate(remaining),
    }


def estimate(pictures: int) -> int | None:
    """Seconds to make ``pictures``, from how long the last ones took; None
    until there are enough to go on."""
    durations = [
        d
        for (d,) in db.session.query(ArtworkCandidate.duration_s)
        .filter(ArtworkCandidate.duration_s.isnot(None))
        .order_by(ArtworkCandidate.created_at.desc())
        .limit(40)
    ]
    if len(durations) < 3 or not pictures:
        return None
    lanes = sum(
        w.max_jobs
        for w in db.session.query(ArtworkWorker).filter_by(enabled=True)
        if online(w)
    )
    lanes = max(1, min(lanes or 1, settings()["concurrency"]))
    return round(sum(durations) / len(durations) * pictures / lanes)


def set_batch_state(batch: ArtworkBatch, action: str) -> None:
    if action == "pause" and batch.state == "active":
        batch.state = "paused"
    elif action == "resume" and batch.state == "paused":
        batch.state = "active"
    elif action == "cancel" and batch.state in ("active", "paused"):
        batch.state = "cancelled"
        batch.finished_at = utcnow()
        for job in batch.jobs:
            cancel_job(job)
    elif action == "retry":
        for job in batch.jobs:
            retry_job(job)
        if batch.state == "done":
            batch.state = "active"
            batch.finished_at = None
    db.session.commit()


def prioritise(job: ArtworkJob) -> None:
    """Puts ``job`` ahead of the rest of the queue."""
    top = db.session.query(db.func.max(ArtworkJob.priority)).scalar() or 0
    job.priority = top + 1
    db.session.commit()


# --- Overview ---------------------------------------------------------------


def overview() -> dict:
    """Counts for the Generate pages' dashboard."""
    counts: dict[str, int] = {}
    for state, number in (
        db.session.query(ArtworkImage.state, db.func.count(ArtworkImage.id))
        .join(ArtworkJob)
        .filter(ArtworkJob.purpose == "library")
        .group_by(ArtworkImage.state)
    ):
        counts[state] = number
    workers = db.session.query(ArtworkWorker).order_by(ArtworkWorker.name).all()
    return {
        "waiting": counts.get("pending", 0),
        "running": sum(counts.get(s, 0) for s in RUNNING),
        "review": counts.get("review", 0),
        "failed": counts.get("failed", 0),
        "workers": workers,
        "online": [w for w in workers if w.enabled and online(w)],
        "defaults": settings().get("defaults") or {},
    }


def tick() -> None:
    """Worker cycle: recover abandoned pictures, top up batches, tidy up."""
    recover()
    fill_batches()
    prune()
