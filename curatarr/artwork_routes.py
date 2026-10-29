"""Posters → Generate, Jobs, Review, Batches, Workers and Benchmarks
(artwork_jobs.py, artwork_bench.py), and the API artwork workers use.

The worker API's requests carry the worker's own token (``Authorization:
Bearer …``), checked here; routes.guard_request lets them past the form token
and sign-in. Everything else is for administrators only, as every page
outside routes.HOUSEHOLD_PAGES is.
"""

import json
from pathlib import Path

from flask import (
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from . import artwork_jobs, artwork_prompts, custom_posters, db
from .integrations import IntegrationError
from .models import (
    ArtworkBatch,
    ArtworkCandidate,
    ArtworkImage,
    ArtworkJob,
    ArtworkWorker,
    MediaIdentity,
)
from .routes import NAV_SECTIONS, PUBLIC_ENDPOINTS, bp

WORKER_ENDPOINTS = {
    "main.artwork_worker_hello",
    "main.artwork_worker_claim",
    "main.artwork_worker_progress",
    "main.artwork_worker_reference",
    "main.artwork_worker_result",
    "main.artwork_worker_fail",
}
PUBLIC_ENDPOINTS.update(WORKER_ENDPOINTS)
for _endpoint in (
    "main.artwork_generate",
    "main.artwork_jobs",
    "main.artwork_job",
    "main.artwork_review",
    "main.artwork_batches",
    "main.artwork_workers",
    "main.artwork_benchmarks",
    "main.artwork_compare",
):
    NAV_SECTIONS[_endpoint] = "library"
PER_PAGE = 20


# --- The worker API ---------------------------------------------------------


def _worker() -> ArtworkWorker:
    worker = artwork_jobs.authenticate(request.headers.get("Authorization"))
    if worker is None:
        abort(401)
    return worker


def _json() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@bp.post("/api/v1/artwork/worker/hello")
def artwork_worker_hello():
    """A worker's heartbeat: what it can do, and how it's doing."""
    return jsonify(artwork_jobs.heartbeat(_worker(), _json()))


@bp.post("/api/v1/artwork/worker/claim")
def artwork_worker_claim():
    data = _json()
    try:
        slots = int(data.get("slots") or 1)
    except (TypeError, ValueError):
        slots = 1
    ready = [e for e in data.get("ready") or [] if isinstance(e, str)]
    return jsonify({"tasks": artwork_jobs.claim(_worker(), slots, ready)})


@bp.post("/api/v1/artwork/worker/tasks/<image_id>/progress")
def artwork_worker_progress(image_id):
    return jsonify(artwork_jobs.report(_worker(), image_id, _json()))


@bp.get("/api/v1/artwork/worker/tasks/<image_id>/references/<int:number>")
def artwork_worker_reference(image_id, number):
    path = artwork_jobs.reference_path(_worker(), image_id, number)
    if path is None:
        abort(404)
    return send_file(path, max_age=0)


@bp.post("/api/v1/artwork/worker/tasks/<image_id>/result")
def artwork_worker_result(image_id):
    worker = _worker()
    upload = request.files.get("picture")
    if upload is None:
        abort(400)
    try:
        meta = json.loads(request.form.get("meta") or "{}")
    except ValueError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    data = upload.read(artwork_jobs.MAX_RESULT + 1)
    result = artwork_jobs.submit(worker, image_id, data, meta)
    return jsonify(result), (200 if result["ok"] else 409)


@bp.post("/api/v1/artwork/worker/tasks/<image_id>/fail")
def artwork_worker_fail(image_id):
    return jsonify(artwork_jobs.fail(_worker(), image_id, _json()))


# --- Pages ------------------------------------------------------------------


def _accounts():
    from .poster_routes import _accounts as accounts

    return accounts()


def _who() -> str | None:
    from .auth import current_user

    return (current_user() or {}).get("name")


def _page() -> int:
    try:
        return max(1, int(request.args.get("page") or 1))
    except ValueError:
        return 1


def _common() -> dict:
    return {
        "overview": artwork_jobs.overview(),
        "kinds": custom_posters.KINDS,
        "kind_names": custom_posters.KIND_NAMES,
        "state_names": artwork_jobs.STATE_NAMES,
        "job_state_names": artwork_jobs.JOB_STATE_NAMES,
        "engines": artwork_jobs.engines(),
    }


def _engine_label(engine: str) -> str:
    if engine == "auto":
        return "Best available"
    info = artwork_jobs.engine_info(engine)
    return info["label"] if info else engine


@bp.get("/library/posters/generate")
def artwork_generate():
    options = custom_posters.prompt_options()
    chosen = [
        m
        for m in (
            db.session.get(MediaIdentity, media_id)
            for media_id in request.args.getlist("media")
        )
        if m is not None
    ]
    photos = request.args.get("photos", options["photos"])
    return render_template(
        "artwork_generate.html",
        options=options,
        chosen=chosen,
        photos=photos,
        people=artwork_prompts.people_in(photos),
        accounts=_accounts(),
        workers=db.session.query(ArtworkWorker).order_by(ArtworkWorker.name).all(),
        engine_label=_engine_label,
        **_common(),
    )


@bp.get("/library/posters/generate/find")
def artwork_find_titles():
    """Titles matching what's typed, for the Generate page's title picker."""
    text = (request.args.get("q") or "").strip()
    if len(text) < 2:
        return jsonify({"titles": []})
    rows = (
        db.session.query(MediaIdentity)
        .filter(
            MediaIdentity.title.ilike(f"%{text}%"),
            MediaIdentity.jellyfin_id.isnot(None),
            MediaIdentity.missing_since.is_(None),
        )
        .order_by(MediaIdentity.title)
        .limit(15)
        .all()
    )
    return jsonify(
        {
            "titles": [
                {
                    "id": m.id,
                    "title": m.title,
                    "kind": "TV show" if m.media_type == "series" else "film",
                }
                for m in rows
            ]
        }
    )


def _job_options(form) -> dict:
    cast = [n.strip() for n in (form.get("cast") or "").split(",") if n.strip()]
    return {
        "photos": (form.get("photos") or "").strip(),
        "cast": cast[: artwork_prompts.MAX_PEOPLE],
        "style": " ".join((form.get("style") or "").split())[:600],
        "originals": form.get("originals") in {"on", "1"},
        "external_ok": form.get("external_ok") in {"on", "1"},
    }


def _scope(form) -> list[str]:
    known = dict(_accounts())
    return [a for a in form.getlist("accounts") if a in known]


@bp.post("/library/posters/generate")
def artwork_generate_submit():
    titles = [
        m
        for m in (
            db.session.get(MediaIdentity, i) for i in request.form.getlist("media")
        )
        if m is not None
    ]
    if not titles:
        flash("Choose at least one title.", "warning")
        return redirect(url_for("main.artwork_generate"))
    kinds = request.form.getlist("kinds")
    options = _job_options(request.form)
    if options["photos"] and not Path(options["photos"]).is_dir():
        flash(f"Curatarr can't find the folder {options['photos']}.", "danger")
        return redirect(url_for("main.artwork_generate", media=[m.id for m in titles]))
    worker = request.form.get("worker") or None
    if worker and db.session.get(ArtworkWorker, worker) is None:
        abort(400)
    made = 0
    try:
        for media in titles:
            artwork_jobs.create_job(
                media,
                kinds,
                accounts=_scope(request.form),
                engine=request.form.get("engine") or "auto",
                worker_id=worker,
                options=options,
                created_by=_who(),
            )
            made += 1
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("main.artwork_generate", media=[m.id for m in titles]))
    flash(
        f"Queued pictures for {made} title{'' if made == 1 else 's'}. They're "
        "listed below as they're made; nothing changes in Jellyfin until you "
        "approve them.",
        "success",
    )
    return redirect(url_for("main.artwork_jobs"))


def _jobs_query(state=None, purpose="library"):
    query = db.session.query(ArtworkJob).filter(ArtworkJob.purpose == purpose)
    if state:
        query = query.filter(ArtworkJob.state == state)
    return query.order_by(ArtworkJob.created_at.desc())


@bp.get("/library/posters/jobs", endpoint="artwork_jobs")
def artwork_jobs_page():
    state = request.args.get("state") or None
    if state and state not in artwork_jobs.JOB_STATE_NAMES:
        state = None
    query = _jobs_query(state)
    total = query.count()
    page = _page()
    jobs = query.offset((page - 1) * PER_PAGE).limit(PER_PAGE).all()
    context = {
        "jobs": jobs,
        "state": state,
        "page": page,
        "pages": max(1, -(-total // PER_PAGE)),
        "total": total,
        "engine_label": _engine_label,
        "live": any(j.state in ("pending", "generating") for j in jobs),
        **_common(),
    }
    if request.args.get("fragment"):
        return render_template("_artwork_jobs.html", **context)
    return render_template("artwork_jobs.html", **context)


def _existing(job: ArtworkJob, kind: str):
    """What the title shows now for ``kind``, to compare with: its custom
    picture (the first account's, or everyone's)."""
    if job.media is None:
        return None
    for user_id in job.accounts or [None]:
        poster = custom_posters.active(job.media_identity_id, user_id, kind)
        if poster:
            return poster
    return None


@bp.get("/library/posters/jobs/<job_id>")
def artwork_job(job_id):
    job = db.session.get(ArtworkJob, job_id)
    if job is None:
        abort(404)
    context = {
        "job": job,
        "existing_by_kind": {i.kind: _existing(job, i.kind) for i in job.images},
        "engine_label": _engine_label,
        "accounts": dict(_accounts()),
        "live": job.state in ("pending", "generating"),
        **_common(),
    }
    if request.args.get("fragment"):
        return render_template("_artwork_job.html", **context)
    return render_template("artwork_job.html", **context)


@bp.get("/library/posters/review")
def artwork_review():
    query = (
        db.session.query(ArtworkImage)
        .join(ArtworkJob)
        .filter(ArtworkImage.state == "review", ArtworkJob.purpose == "library")
        .order_by(ArtworkImage.finished_at)
    )
    total = query.count()
    page = _page()
    images = query.offset((page - 1) * PER_PAGE).limit(PER_PAGE).all()
    return render_template(
        "artwork_review.html",
        images=images,
        existing_by_image={i.id: _existing(i.job, i.kind) for i in images},
        page=page,
        pages=max(1, -(-total // PER_PAGE)),
        total=total,
        engine_label=_engine_label,
        **_common(),
    )


@bp.get("/library/posters/candidates/<candidate_id>.<ext>")
def artwork_candidate_image(candidate_id, ext):
    candidate = db.session.get(ArtworkCandidate, candidate_id)
    if candidate is None or not candidate.path or not Path(candidate.path).is_file():
        abort(404)
    return send_file(candidate.path, max_age=3600)


@bp.get("/library/posters/images/<image_id>/references/<int:number>")
def artwork_image_reference(image_id, number):
    """A reference picture a task was given, to compare a likeness with."""
    image = db.session.get(ArtworkImage, image_id)
    references = ((image.task or {}).get("references") or []) if image else []
    if not 1 <= number <= len(references):
        abort(404)
    path = Path(references[number - 1]["path"])
    if not path.is_file():
        abort(404)
    return send_file(path, max_age=300)


def _back(default):
    target = request.form.get("back") or ""
    if target.startswith("/library/posters/") and "//" not in target:
        return redirect(target)
    return redirect(default)


@bp.post("/library/posters/candidates/<candidate_id>/<action>")
def artwork_candidate_action(candidate_id, action):
    candidate = db.session.get(ArtworkCandidate, candidate_id)
    if candidate is None:
        abort(404)
    job = candidate.image.job
    back = url_for("main.artwork_job", job_id=job.id)
    feedback = request.form.get("feedback")
    try:
        if action == "approve":
            flash(artwork_jobs.approve(candidate, _who()), "success")
        elif action == "reject":
            artwork_jobs.reject(candidate, feedback, again=False)
            flash("Turned down.", "success")
        elif action == "again":
            artwork_jobs.reject(candidate, feedback, again=True)
            flash("Turned down; it's being made again.", "success")
        else:
            abort(404)
    except (ValueError, IntegrationError, OSError) as exc:
        db.session.rollback()
        flash(f"Couldn't do that: {exc}", "danger")
    return _back(back)


@bp.post("/library/posters/images/<image_id>/again")
def artwork_image_again(image_id):
    image = db.session.get(ArtworkImage, image_id)
    if image is None:
        abort(404)
    try:
        artwork_jobs.regenerate(image, request.form.get("feedback"))
        flash(f"Making its {custom_posters._noun(image.kind)} again.", "success")
    except ValueError as exc:
        flash(str(exc), "warning")
    return _back(url_for("main.artwork_job", job_id=image.job_id))


@bp.post("/library/posters/jobs/<job_id>/<action>")
def artwork_job_action(job_id, action):
    job = db.session.get(ArtworkJob, job_id)
    if job is None:
        abort(404)
    try:
        if action == "approve":
            done = artwork_jobs.approve_set(job, _who())
            flash(" ".join(done) or "Nothing was waiting for review.", "success")
        elif action == "cancel":
            count = artwork_jobs.cancel_job(job)
            flash(f"Cancelled {count} picture{'' if count == 1 else 's'}.", "success")
        elif action == "retry":
            count = artwork_jobs.retry_job(job)
            flash(f"Trying {count} again.", "success")
        elif action == "first":
            artwork_jobs.prioritise(job)
            flash("It's next in the queue.", "success")
        else:
            abort(404)
    except (ValueError, IntegrationError, OSError) as exc:
        db.session.rollback()
        flash(f"Couldn't do that: {exc}", "danger")
    return _back(url_for("main.artwork_job", job_id=job.id))


@bp.get("/library/posters/batches")
def artwork_batches():
    options = custom_posters.prompt_options()
    preview = None
    spec = None
    if request.args.get("preview"):
        spec = _batch_spec(request.args)
        titles = artwork_jobs.batch_titles(spec)
        preview = {"count": len(titles), "titles": titles[:50]}
    batches = (
        db.session.query(ArtworkBatch)
        .filter_by(purpose="library")
        .order_by(ArtworkBatch.created_at.desc())
        .limit(30)
        .all()
    )
    return render_template(
        "artwork_batches.html",
        options=options,
        batches=batches,
        progress={b.id: artwork_jobs.batch_progress(b) for b in batches},
        preview=preview,
        spec=spec,
        accounts=_accounts(),
        workers=db.session.query(ArtworkWorker).order_by(ArtworkWorker.name).all(),
        engine_label=_engine_label,
        **_common(),
    )


def _batch_spec(form) -> dict:
    which = form.get("titles") or "favourites"
    if not (
        which in ("favourites", "movies", "shows") or which.startswith("favourites:")
    ):
        which = "favourites"
    worker = form.get("worker") or None
    if worker and db.session.get(ArtworkWorker, worker) is None:
        worker = None
    return {
        "titles": which,
        "kinds": [k for k in custom_posters.KINDS if k in form.getlist("kinds")]
        or ["Primary"],
        "accounts": _scope(form),
        "skip_done": form.get("skip_done") in {"on", "1"},
        "alternatives": form.get("alternatives") in {"on", "1"},
        "engine": form.get("engine") or "auto",
        "worker": worker,
        "options": _job_options(form),
    }


@bp.post("/library/posters/batches")
def artwork_batch_create():
    spec = _batch_spec(request.form)
    if spec["options"]["photos"] and not Path(spec["options"]["photos"]).is_dir():
        flash(f"Curatarr can't find the folder {spec['options']['photos']}.", "danger")
        return redirect(url_for("main.artwork_batches"))
    engine = artwork_jobs.engine_info(spec["engine"]) if spec["engine"] else None
    if (
        engine
        and engine["external"]
        and spec["options"]["photos"]
        and not spec["options"]["external_ok"]
    ):
        flash(
            f"{engine['label']} sends the reference photos to an outside service. "
            "Tick the box that allows it, or choose a local engine.",
            "danger",
        )
        return redirect(url_for("main.artwork_batches"))
    try:
        batch = artwork_jobs.create_batch(
            spec, name=request.form.get("name"), created_by=_who()
        )
    except ValueError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("main.artwork_batches"))
    flash(
        f"Started: {len(batch.media_ids)} title"
        f"{'' if len(batch.media_ids) == 1 else 's'}, a few at a time.",
        "success",
    )
    return redirect(url_for("main.artwork_batches"))


@bp.post("/library/posters/batches/<batch_id>/<action>")
def artwork_batch_action(batch_id, action):
    batch = db.session.get(ArtworkBatch, batch_id)
    if batch is None or action not in ("pause", "resume", "cancel", "retry"):
        abort(404)
    artwork_jobs.set_batch_state(batch, action)
    flash(
        {
            "pause": "Paused.",
            "resume": "Resumed.",
            "cancel": "Cancelled.",
            "retry": "Trying the failed ones again.",
        }[action],
        "success",
    )
    return redirect(url_for("main.artwork_batches"))


@bp.get("/library/posters/workers")
def artwork_workers():
    return render_template(
        "artwork_workers.html",
        workers=db.session.query(ArtworkWorker).order_by(ArtworkWorker.name).all(),
        settings=artwork_jobs.settings(),
        online=artwork_jobs.online,
        token=None,
        new_worker=None,
        server=request.host_url.rstrip("/"),
        engine_label=_engine_label,
        **_common(),
    )


@bp.post("/library/posters/workers")
def artwork_worker_create():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Give the worker a name.", "warning")
        return redirect(url_for("main.artwork_workers"))
    try:
        jobs = int(request.form.get("max_jobs") or 1)
    except ValueError:
        jobs = 1
    worker, token = artwork_jobs.create_worker(name, jobs)
    return _show_token(worker, token)


def _setup_command(token: str) -> str:
    """The one command that sets a worker up on a computer and starts it:
    this installation's own program when it's run here, else the one on the
    PATH (where the worker's installed on another computer)."""
    import shlex
    import sys

    from .artwork_worker.cli import stable

    here = Path(sys.executable).parent / "curatarr-artwork-worker"
    program = str(stable(here)) if here.is_file() else "curatarr-artwork-worker"
    server = request.host_url.rstrip("/")
    return (
        f"{shlex.quote(program)} setup --server {shlex.quote(server)} "
        f"--token {shlex.quote(token)} --service"
    )


def _show_token(worker, token):
    return render_template(
        "artwork_workers.html",
        workers=db.session.query(ArtworkWorker).order_by(ArtworkWorker.name).all(),
        settings=artwork_jobs.settings(),
        online=artwork_jobs.online,
        token=token,
        new_worker=worker,
        command=_setup_command(token),
        server=request.host_url.rstrip("/"),
        engine_label=_engine_label,
        **_common(),
    )


@bp.post("/library/posters/workers/<worker_id>/<action>")
def artwork_worker_action(worker_id, action):
    worker = db.session.get(ArtworkWorker, worker_id)
    if worker is None:
        abort(404)
    if action == "token":
        return _show_token(worker, artwork_jobs.new_token(worker))
    if action == "enable":
        worker.enabled = True
    elif action == "disable":
        worker.enabled = False
    elif action == "jobs":
        try:
            worker.max_jobs = max(1, min(int(request.form.get("max_jobs") or 1), 8))
        except ValueError:
            pass
    else:
        abort(404)
    db.session.commit()
    flash("Saved.", "success")
    return redirect(url_for("main.artwork_workers"))


@bp.post("/library/posters/workers/settings")
def artwork_settings_save():
    artwork_jobs.save_settings(request.form)
    for kind in custom_posters.KINDS:
        if f"default-{kind}" in request.form:
            artwork_jobs.set_default_engine(
                kind, request.form.get(f"default-{kind}") or None
            )
    flash("Saved.", "success")
    return redirect(url_for("main.artwork_workers"))


# --- Benchmarks -------------------------------------------------------------


@bp.get("/library/posters/benchmarks")
def artwork_benchmarks():
    from . import artwork_bench

    runs = (
        db.session.query(ArtworkBatch)
        .filter_by(purpose="benchmark")
        .order_by(ArtworkBatch.created_at.desc())
        .limit(20)
        .all()
    )
    run = None
    if request.args.get("run"):
        run = db.session.get(ArtworkBatch, request.args["run"])
    elif runs:
        run = runs[0]
    return render_template(
        "artwork_benchmarks.html",
        runs=runs,
        run=run,
        results=artwork_bench.results(run) if run else None,
        scenarios=artwork_bench.SCENARIOS,
        smoke=artwork_bench.SMOKE,
        aspects=artwork_bench.ASPECTS,
        ratings_of=artwork_bench.ratings_of,
        options=custom_posters.prompt_options(),
        defaults=artwork_jobs.settings().get("defaults") or {},
        engine_label=_engine_label,
        **_common(),
    )


@bp.post("/library/posters/benchmarks")
def artwork_benchmark_start():
    from . import artwork_bench

    photos = (request.form.get("photos") or "").strip()
    if photos and not Path(photos).is_dir():
        flash(f"Curatarr can't find the folder {photos}.", "danger")
        return redirect(url_for("main.artwork_benchmarks"))
    cast = [n.strip() for n in (request.form.get("cast") or "").split(",") if n.strip()]
    try:
        run = artwork_bench.start(
            request.form.getlist("engines"),
            request.form.getlist("scenarios"),
            photos=photos,
            cast=cast,
            external_ok=request.form.get("external_ok") in {"on", "1"},
            by=_who(),
        )
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
        return redirect(url_for("main.artwork_benchmarks"))
    flash(f"Started: {len(run.jobs)} jobs.", "success")
    return redirect(url_for("main.artwork_benchmarks", run=run.id))


@bp.post("/library/posters/benchmarks/<run_id>/cancel")
def artwork_benchmark_cancel(run_id):
    run = db.session.get(ArtworkBatch, run_id)
    if run is None or run.purpose != "benchmark":
        abort(404)
    for job in run.jobs:
        artwork_jobs.cancel_job(job)
    run.state = "cancelled"
    db.session.commit()
    flash("Cancelled what hadn't been made.", "success")
    return redirect(url_for("main.artwork_benchmarks", run=run.id))


@bp.post("/library/posters/benchmarks/rate/<candidate_id>")
def artwork_benchmark_rate(candidate_id):
    from . import artwork_bench

    candidate = db.session.get(ArtworkCandidate, candidate_id)
    if candidate is None:
        abort(404)
    artwork_bench.rate(
        candidate,
        {a: request.form.get(a) for a in artwork_bench.ASPECTS},
        request.form.get("notes"),
        _who(),
    )
    flash("Rated.", "success")
    return _back(url_for("main.artwork_benchmarks"))


@bp.get("/library/posters/benchmarks/compare")
def artwork_benchmark_compare():
    from . import artwork_bench

    run = (
        db.session.get(ArtworkBatch, request.args["run"])
        if request.args.get("run")
        else None
    )
    found = artwork_bench.pair(run)
    return render_template(
        "artwork_compare.html",
        pair=found,
        run=run,
        scenario=(
            artwork_bench.scenario((found[0].image.job.options or {}).get("scenario"))
            if found
            else None
        ),
        **_common(),
    )


@bp.post("/library/posters/benchmarks/compare")
def artwork_benchmark_choose():
    from . import artwork_bench

    try:
        comparison = artwork_bench.compare(
            request.form.get("left"),
            request.form.get("right"),
            request.form.get("winner"),
            request.form.get("notes"),
            _who(),
        )
    except ValueError as exc:
        flash(str(exc), "warning")
    else:
        left = db.session.get(ArtworkCandidate, comparison.left_id)
        right = db.session.get(ArtworkCandidate, comparison.right_id)
        flash(
            f"Saved. A was {_engine_label(left.engine)}; B was "
            f"{_engine_label(right.engine)}.",
            "success",
        )
    return redirect(
        url_for("main.artwork_benchmark_compare", run=request.form.get("run") or None)
    )


@bp.post("/library/posters/benchmarks/default")
def artwork_benchmark_default():
    kind = request.form.get("kind")
    if kind not in custom_posters.KINDS:
        abort(400)
    engine = request.form.get("engine") or None
    artwork_jobs.set_default_engine(kind, engine)
    flash(
        f"{_engine_label(engine) if engine else 'Automatic'} now makes "
        f"{custom_posters.KIND_NAMES[kind]}s when a job leaves it to Curatarr.",
        "success",
    )
    return _back(url_for("main.artwork_benchmarks"))


# --- Compare with the current pictures ---------------------------------------


@bp.get("/library/posters/compare")
def artwork_compare():
    from . import artwork_compare as compare

    return render_template(
        "artwork_compare_current.html",
        matchup=compare.next_matchup(),
        remaining=compare.remaining(),
        results=compare.results(),
        engine_label=_engine_label,
        **_common(),
    )


@bp.post("/library/posters/compare")
def artwork_compare_choose():
    from . import artwork_compare as compare

    try:
        choice = compare.record(
            request.form.get("candidate"),
            request.form.get("current"),
            request.form.get("winner"),
            request.form.get("notes"),
            _who(),
        )
    except ValueError as exc:
        flash(str(exc), "warning")
    else:
        new_side = "A" if request.form.get("new_left") == "1" else "B"
        old_side = "B" if new_side == "A" else "A"
        engine = _engine_label(choice.candidate.engine or "")
        flash(
            f"Saved. {new_side} was the new one ({engine}); {old_side} the one it "
            "has now.",
            "success",
        )
    return redirect(url_for("main.artwork_compare"))


@bp.get("/library/posters/compare/current/<poster_id>")
def artwork_compare_current_image(poster_id):
    from . import artwork_compare as compare
    from .models import CustomPoster

    poster = db.session.get(CustomPoster, poster_id)
    path = compare.current_file(poster) if poster else None
    if path is None:
        abort(404)
    return send_file(path, max_age=3600)


@bp.post("/library/posters/compare/undo")
def artwork_compare_undo():
    from . import artwork_compare as compare

    flash(
        "Took back your last choice."
        if compare.undo_last()
        else "Nothing to take back.",
        "success",
    )
    return redirect(url_for("main.artwork_compare"))


@bp.post("/library/posters/prompts/generate")
def artwork_generate_from_prompts():
    """The Posters page's prompt options, made by a worker as a batch
    instead of copied into an AI by hand."""
    options = custom_posters.prompt_options()
    spec = {
        "titles": options["titles"],
        "kinds": options["kinds"],
        "accounts": custom_posters.settings().get("accounts") or [],
        "skip_done": options["skip_done"],
        "alternatives": request.form.get("alternatives") in {"on", "1"},
        "engine": "auto",
        "worker": None,
        "options": {
            "photos": options["photos"],
            "cast": [],
            "style": options["style"],
            "originals": options["originals"],
            "external_ok": False,
        },
    }
    if request.form.get("engine") == "codex/image":
        # The button says the photos go to OpenAI: that's the permission.
        spec["engine"] = "codex/image"
        spec["options"]["external_ok"] = True
    try:
        batch = artwork_jobs.create_batch(
            spec,
            name="Alternatives to compare"
            if spec["alternatives"]
            else "From the prompts",
            created_by=_who(),
        )
    except ValueError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("main.custom_posters_page") + "#prompts")
    flash(
        f"Started: {len(batch.media_ids)} title"
        f"{'' if len(batch.media_ids) == 1 else 's'}, made by your workers a few at "
        "a time. Each waits for your review.",
        "success",
    )
    return redirect(url_for("main.artwork_batches"))
