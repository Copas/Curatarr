"""Posters → Generate: jobs, workers, review and batches (artwork_jobs.py),
run end to end with the worker's own code and its mock engine, talking to
Curatarr through the test client."""

import json
import os
import stat
from datetime import timedelta
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from test_custom_posters import _fake, _picture
from test_enrichment import _movie, media_env  # noqa: F401

from curatarr import artwork_bench, artwork_jobs, artwork_prompts, custom_posters, db
from curatarr.artwork_worker import checks, imaging
from curatarr.artwork_worker.backends import Failed, Reference
from curatarr.artwork_worker.backends.codex import CodexBackend
from curatarr.artwork_worker.backends.workflows import WORKFLOWS, Flux2Klein
from curatarr.artwork_worker.client import Server, Unauthorised
from curatarr.artwork_worker.runner import Worker
from curatarr.models import (
    ArtworkCandidate,
    ArtworkImage,
    ArtworkJob,
    CustomPoster,
    MediaIdentity,
    utcnow,
)

MOCK = {
    "server": "http://curatarr.test",
    "slots": 2,
    "backends": {"mock": {"enabled": True}},
    "checks": {},
}


class Answer:
    """A Flask test response, looking like a requests one."""

    def __init__(self, response):
        self.status_code = response.status_code
        self.content = response.data
        self._response = response

    @property
    def text(self):
        return self._response.get_data(as_text=True)

    def json(self):
        return self._response.get_json()

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}")


class TestSession:
    """requests.Session over the Flask test client, so the worker's own
    client code talks to Curatarr's real routes."""

    __test__ = False

    def __init__(self, client):
        self.client = client
        self.headers = {}

    def request(self, method, url, timeout=None, json=None, files=None, data=None):
        path = url.split("curatarr.test", 1)[1]
        form = dict(data or {})
        for name, (filename, content, mimetype) in (files or {}).items():
            form[name] = (BytesIO(content), filename, mimetype)
        response = self.client.open(
            path,
            method=method,
            json=json,
            data=form if files else (form or None),
            headers=dict(self.headers),
        )
        return Answer(response)


def _art(size=(1024, 1536)):
    """A test picture with something in it (a plain one is refused as blank)."""
    picture = Image.new("RGB", size, (40, 60, 90))
    for x in range(0, size[0], 64):
        picture.paste((220, 200, 120), (x, 0, x + 16, size[1]))
    buffer = BytesIO()
    picture.save(buffer, "JPEG")
    return buffer.getvalue()


def _jellyfin(monkeypatch, current):
    """Test Jellyfin, telling titles' year, genres and plot too."""
    jellyfin = _fake(monkeypatch, current)
    put = jellyfin.put_image
    jellyfin.put_image = lambda item, data, kind="Primary", **_extra: put(
        item, data, kind
    )
    jellyfin.item_details = lambda ids: [
        {"Id": i, "ProductionYear": 2020, "Genres": ["Drama"], "Overview": "A story."}
        for i in ids
    ]
    return jellyfin


def _worker(app, client, tmp_path, config=None):
    worker_row, token = artwork_jobs.create_worker("Test worker", 2)
    config = dict(config or MOCK) | {"work_dir": str(tmp_path / "work")}
    server = Server(config["server"], token, session=TestSession(client))
    return worker_row, token, Worker(config, token, server=server)


def _second(library, title, jellyfin_id):
    media = MediaIdentity(
        library_id=library.id, media_type="movie", title=title, jellyfin_id=jellyfin_id
    )
    db.session.add(media)
    db.session.commit()
    return media


def _drain(worker, rounds=10):
    """Runs the worker until there's nothing left to claim."""
    for _ in range(rounds):
        worker.last_hello = 0
        started = worker.step()
        for thread in list(worker.running.values()):
            thread.join(timeout=30)
        if not started:
            break


# --- Workers and their tokens -----------------------------------------------


def test_a_worker_signs_in_with_its_own_token(app, client, tmp_path):
    worker, token = artwork_jobs.create_worker("Spare PC")
    assert token.startswith("cwk_") and token not in str(worker.token_hash)
    hello = "/api/v1/artwork/worker/hello"
    assert client.post(hello, json={}).status_code == 401
    assert (
        client.post(
            hello, json={}, headers={"Authorization": "Bearer cwk_x"}
        ).status_code
        == 401
    )
    good = {"Authorization": f"Bearer {token}"}
    report = {
        "capabilities": [
            {"engine": "mock/test", "label": "Test", "kinds": ["Primary", "Nope"]}
        ],
        "info": {"gpu": "Test GPU", "vram_mb": 12288, "secret": "dropped"},
    }
    assert client.post(hello, json=report, headers=good).status_code == 200
    db.session.refresh(worker)
    assert worker.capabilities[0]["kinds"] == ["Primary"]
    assert "secret" not in worker.info and artwork_jobs.online(worker)
    # Turned off, or given a new token: the old one stops working.
    fresh = artwork_jobs.new_token(worker)
    assert client.post(hello, json={}, headers=good).status_code == 401
    worker.enabled = False
    db.session.commit()
    assert (
        client.post(
            hello, json={}, headers={"Authorization": f"Bearer {fresh}"}
        ).status_code
        == 401
    )


def test_the_worker_api_needs_no_form_token_but_a_worker_token(app, client):
    app.config["CSRF_ENABLED"] = True
    app.config["ALLOW_UNAUTHENTICATED"] = False
    _worker_row, token = artwork_jobs.create_worker("A")
    headers = {"Authorization": f"Bearer {token}"}
    assert (
        client.post(
            "/api/v1/artwork/worker/claim", json={"slots": 1}, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.post("/api/v1/artwork/worker/claim", json={"slots": 1}).status_code
        == 401
    )
    # The pages themselves still need a sign-in.
    assert client.get("/library/posters/generate").status_code == 302


# --- A job, start to finish -------------------------------------------------


def test_a_set_is_made_reviewed_and_put_on(app, client, tmp_path, monkeypatch):
    _library, media, _part = _movie()
    jellyfin = _jellyfin(monkeypatch, _picture("#223344"))
    _row, _token, worker = _worker(app, client, tmp_path)
    response = client.post(
        "/library/posters/generate",
        data={"media": [media.id], "kinds": ["Primary", "Thumb", "Logo"]},
    )
    assert response.status_code == 302
    job = db.session.query(ArtworkJob).one()
    assert [i.kind for i in job.images] == ["Primary", "Thumb", "Logo"]
    # The poster's made first; the rest wait for it, so they can match it.
    worker.last_hello = 0
    assert worker.step() == 1
    for thread in list(worker.running.values()):
        thread.join(timeout=30)
    db.session.expire_all()
    states = {i.kind: i.state for i in job.images}
    assert states == {"Primary": "review", "Thumb": "pending", "Logo": "pending"}
    _drain(worker)
    db.session.expire_all()
    assert {i.state for i in job.images} == {"review"}
    thumb = next(i for i in job.images if i.kind == "Thumb")
    assert [r["role"] for r in thumb.task["references"]] == ["set"]  # the poster
    logo = next(i for i in job.images if i.kind == "Logo").candidates[0]
    assert logo.has_alpha and logo.path.endswith(".png")
    poster = job.images[0].candidates[0]
    assert (poster.width, poster.height) == (1024, 1536)
    # Nothing's changed in Jellyfin until it's approved.
    assert jellyfin.uploads == 0
    page = client.get(f"/library/posters/jobs/{job.id}").text
    assert "Approve the whole set" in page and "Awaiting review" in page
    client.post(f"/library/posters/jobs/{job.id}/approve")
    db.session.expire_all()
    assert {i.state for i in job.images} == {"completed"} and job.state == "completed"
    assert jellyfin.uploads == 3
    on = custom_posters.active(media.id)
    assert on.source.startswith("film") and on.source.endswith(".jpg")
    assert custom_posters.active(media.id, kind="Logo").source.endswith(".logo.png")
    # Approving it again changes nothing more.
    with pytest.raises(ValueError):
        artwork_jobs.approve(ArtworkCandidate(image=job.images[0], path=None))


def test_some_of_a_set_approved_while_the_rest_is_made_again(
    app, client, tmp_path, monkeypatch
):
    _library, media, _part = _movie()
    _jellyfin(monkeypatch, _picture("#223344"))
    _row, _token, worker = _worker(app, client, tmp_path)
    job = artwork_jobs.create_job(media, ["Primary", "Backdrop", "Banner"])
    _drain(worker)
    db.session.expire_all()
    poster, backdrop, banner = job.images
    artwork_jobs.approve(poster.candidates[0])
    artwork_jobs.reject(backdrop.candidates[0], "more rain, please", again=True)
    artwork_jobs.reject(banner.candidates[0])
    db.session.expire_all()
    assert poster.state == "completed"
    assert backdrop.state == "pending" and backdrop.feedback == "more rain, please"
    assert banner.state == "cancelled"
    _drain(worker)
    db.session.expire_all()
    assert backdrop.state == "review" and len(backdrop.candidates) == 2
    assert "more rain, please" in backdrop.candidates[1].prompt
    assert backdrop.candidates[0].decision == "rejected"
    assert poster.state == "completed" and len(poster.candidates) == 1  # left alone


def test_a_job_for_chosen_accounts_goes_on_for_them_only(
    app, client, tmp_path, monkeypatch
):
    from curatarr.services import set_setting

    _library, media, _part = _movie()
    jellyfin = _jellyfin(monkeypatch, _picture("#223344"))
    set_setting("jellyfin_user_names", {"u1": "Sam", "u2": "Alex"})
    db.session.commit()
    _row, _token, worker = _worker(app, client, tmp_path)
    job = artwork_jobs.create_job(media, ["Primary"], accounts=["u1"])
    _drain(worker)
    db.session.expire_all()
    message = artwork_jobs.approve(job.images[0].candidates[0])
    assert "for Sam" in message
    assert custom_posters.active(media.id, "u1") is not None
    assert custom_posters.active(media.id) is None and jellyfin.uploads == 0


# --- Leases, limits and cancelling --------------------------------------------


def _claim(worker_row, ready=("mock/test",)):
    worker_row.capabilities = artwork_jobs._clean_capabilities(
        [{"engine": "mock/test", "label": "Mock", "references": True}]
    )
    db.session.commit()
    return artwork_jobs.claim(worker_row, 1, list(ready))


def test_a_picture_whose_worker_vanished_is_tried_again_then_fails(app, client):
    _library, media, _part = _movie()
    worker_row, _token = artwork_jobs.create_worker("Flaky", 1)
    job = artwork_jobs.create_job(media, ["Primary"], details={"title": "Film"})
    for attempt in range(1, 4):
        [task] = _claim(worker_row)
        assert task["attempt"] == attempt
        later = utcnow() + timedelta(
            seconds=artwork_jobs.settings()["lease_seconds"] + 5
        )
        assert artwork_jobs.recover(later) == 1
    db.session.expire_all()
    assert job.images[0].state == "failed" and job.state == "failed"
    assert "stopped reporting" in job.images[0].error
    assert artwork_jobs.retry_job(job) == 1
    assert job.images[0].state == "pending" and job.images[0].attempts == 0


def test_a_usage_limit_waits_without_using_up_a_try(app, client):
    _library, media, _part = _movie()
    worker_row, _token = artwork_jobs.create_worker("Codex box", 1)
    job = artwork_jobs.create_job(media, ["Primary"], details={"title": "Film"})
    [task] = _claim(worker_row)
    artwork_jobs.fail(
        worker_row,
        task["image"],
        {"error": "Used up", "code": "usage_limit", "retry_after": 3600},
    )
    image = job.images[0]
    assert image.state == "pending" and image.attempts == 0
    assert artwork_jobs._aware(image.not_before) > utcnow() + timedelta(minutes=55)
    assert _claim(worker_row) == []  # not before it resets


def test_cancelling_tells_the_worker_to_stop(app, client):
    _library, media, _part = _movie()
    worker_row, _token = artwork_jobs.create_worker("W", 1)
    job = artwork_jobs.create_job(media, ["Primary"], details={"title": "Film"})
    [task] = _claim(worker_row)
    assert artwork_jobs.report(worker_row, task["image"], {"progress": 40}) == {
        "cancel": False
    }
    artwork_jobs.cancel_job(job)
    assert artwork_jobs.report(worker_row, task["image"], {"progress": 50}) == {
        "cancel": True
    }
    assert artwork_jobs.submit(worker_row, task["image"], _art(), {})["ok"] is False
    assert job.state == "cancelled"


def test_work_waits_while_curatarr_is_paused(app, client):
    from curatarr import pause

    _library, media, _part = _movie()
    worker_row, _token = artwork_jobs.create_worker("W", 1)
    artwork_jobs.create_job(media, ["Primary"], details={"title": "Film"})
    pause.pause(1)
    assert _claim(worker_row) == []
    pause.resume()
    assert len(_claim(worker_row)) == 1


# --- What a worker sends back ------------------------------------------------


def test_curatarr_refuses_pictures_that_are_wrong(app, client):
    _library, media, _part = _movie()
    worker_row, _token = artwork_jobs.create_worker("W", 1)
    job = artwork_jobs.create_job(media, ["Logo"], details={"title": "Film"})
    [task] = _claim(worker_row)
    result = artwork_jobs.submit(
        worker_row, task["image"], _picture("#ffffff", (1200, 600)), {}
    )
    assert result["ok"] is False and "transparent" in result["problems"][0]
    assert job.images[0].state == "pending"  # a try used; tried again
    assert artwork_jobs.check_picture(b"not a picture", "Primary")["problems"]
    wide = artwork_jobs.check_picture(_art((1920, 1080)), "Primary")
    assert "not the shape" in wide["problems"][0]
    assert not artwork_jobs.check_picture(_art((1920, 1080)), "Backdrop")["problems"]
    assert (
        "blank"
        in artwork_jobs.check_picture(_picture("#336699", (1920, 1080)), "Backdrop")[
            "problems"
        ][0]
    )


def test_a_worker_only_sees_its_own_tasks_references(app, client, tmp_path):
    _library, media, _part = _movie()
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "Sam 1.jpg").write_bytes(_picture("#aa8866"))
    (photos / "notes.txt").write_text("not a photo")
    mine, my_token = artwork_jobs.create_worker("Mine", 1)
    _other, other_token = artwork_jobs.create_worker("Other", 1)
    artwork_jobs.create_job(
        media, ["Primary"], options={"photos": str(photos)}, details={"title": "Film"}
    )
    [task] = _claim(mine)
    assert task["references"] == [{"number": 1, "role": "person", "person": "Sam"}]
    assert str(photos) not in json.dumps(task)  # no paths are sent
    path = f"/api/v1/artwork/worker/tasks/{task['image']}/references/1"
    assert (
        client.get(path, headers={"Authorization": f"Bearer {my_token}"}).status_code
        == 200
    )
    assert (
        client.get(path, headers={"Authorization": f"Bearer {other_token}"}).status_code
        == 404
    )
    assert (
        client.get(
            path[:-1] + "2", headers={"Authorization": f"Bearer {my_token}"}
        ).status_code
        == 404
    )


def test_reference_photos_only_go_out_when_allowed(app, client, tmp_path):
    _library, media, _part = _movie()
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "Sam.jpg").write_bytes(_picture("#aa8866"))
    worker_row, _token = artwork_jobs.create_worker("Codex", 1)
    worker_row.capabilities = artwork_jobs._clean_capabilities(
        [
            {
                "engine": "codex/image",
                "label": "Codex",
                "references": True,
                "external": True,
            }
        ]
    )
    db.session.commit()
    with pytest.raises(ValueError, match="outside service"):
        artwork_jobs.create_job(
            media,
            ["Primary"],
            engine="codex/image",
            options={"photos": str(photos)},
            details={"title": "Film"},
        )
    # Left to Curatarr, a job with photos never picks an outside engine.
    artwork_jobs.create_job(
        media, ["Primary"], options={"photos": str(photos)}, details={"title": "Film"}
    )
    assert artwork_jobs.claim(worker_row, 1, ["codex/image"]) == []
    allowed = artwork_jobs.create_job(
        media,
        ["Primary"],
        engine="codex/image",
        options={"photos": str(photos), "external_ok": True},
        details={"title": "Film"},
    )
    [task] = artwork_jobs.claim(worker_row, 1, ["codex/image"])
    assert task["image"] == allowed.images[0].id
    assert "image generation tool" in task["prompt"]  # an agent's prompt


def test_household_accounts_cant_reach_generation(app, client):
    app.config["ALLOW_UNAUTHENTICATED"] = False
    app.config["DEMO_MODE"] = True
    from curatarr.demo import seed_demo
    from curatarr.services import set_setting

    seed_demo()
    set_setting("household_user_ids", ["user-demo-viewer"])
    db.session.commit()
    client.post("/login", data={"username": "demo-viewer", "password": "demo"})
    for path in (
        "/library/posters/generate",
        "/library/posters/jobs",
        "/library/posters/review",
        "/library/posters/workers",
        "/library/posters/batches",
        "/library/posters/benchmarks",
    ):
        assert client.get(path).status_code == 403, path
    assert (
        client.post("/library/posters/workers", data={"name": "x"}).status_code == 403
    )


# --- Pages ---------------------------------------------------------------------


def test_the_pages_show(app, client, tmp_path, monkeypatch):
    _library, media, _part = _movie()
    _jellyfin(monkeypatch, _picture("#223344"))
    _row, _token, worker = _worker(app, client, tmp_path)
    worker.hello()
    for path in (
        "/library/posters/generate",
        "/library/posters/jobs",
        "/library/posters/review",
        "/library/posters/batches",
        "/library/posters/workers",
        "/library/posters/benchmarks",
        "/library/posters/benchmarks/compare",
        "/library/posters",
    ):
        page = client.get(path)
        assert page.status_code == 200, path
        assert (
            'style="' not in page.text.split("<main", 1)[-1]
            or path == "/library/posters"
        )
    assert "Test pictures (no AI)" in client.get("/library/posters/workers").text
    found = client.get("/library/posters/generate/find?q=fil").get_json()["titles"]
    assert found[0]["id"] == media.id
    artwork_jobs.create_job(media, ["Primary"])
    _drain(worker)
    assert "Approve and put on" in client.get("/library/posters/review").text
    listed = client.get("/library/posters/jobs?fragment=1").text
    assert "Film" in listed and 'class="artwork-made' in listed  # shown as made
    new = client.post("/library/posters/workers", data={"name": "Living room"}).text
    assert "curatarr-artwork-worker setup --server http://localhost --token cwk_" in new
    assert "--service" in new


# --- Batches -------------------------------------------------------------------


def test_a_batch_queues_a_few_titles_at_a_time_and_skips_done_ones(
    app, client, monkeypatch
):
    from curatarr.services import set_setting

    library, _first, _part = _movie()
    _jellyfin(monkeypatch, _picture("#223344"))
    second = _second(library, "Second", "jf-2")
    third = _second(library, "Third", "jf-3")
    for media in (second, third):
        from curatarr.models import MediaPart

        db.session.add(
            MediaPart(media_identity_id=media.id, kind="movie_file", has_file=True)
        )
    db.session.commit()
    custom_posters.apply(
        third, _picture("#cc2222", (1000, 1500)), "Uploaded"
    )  # has one
    set_setting(artwork_jobs.KEY, artwork_jobs.DEFAULTS | {"batch_window": 1})
    db.session.commit()
    spec = {"titles": "movies", "kinds": ["Primary"], "skip_done": True, "options": {}}
    preview = artwork_jobs.batch_titles(spec)
    assert [m.title for m in preview] == ["Film", "Second"]
    batch = artwork_jobs.create_batch(spec)
    assert len(batch.jobs) == 1  # one at a time
    artwork_jobs.set_batch_state(batch, "pause")
    worker_row, _token = artwork_jobs.create_worker("W", 1)
    assert _claim(worker_row) == []  # a paused batch's jobs wait
    artwork_jobs.set_batch_state(batch, "resume")
    [task] = _claim(worker_row)
    artwork_jobs.submit(worker_row, task["image"], _art(), {"duration_s": 5})
    artwork_jobs.fill_batches()
    db.session.expire_all()
    assert len(batch.jobs) == 2
    progress = artwork_jobs.batch_progress(batch)
    assert progress["queued"] == 2 and progress["counts"]["review"] == 1
    artwork_jobs.set_batch_state(batch, "cancel")
    assert batch.state == "cancelled"
    assert {j.state for j in batch.jobs} == {"review", "cancelled"}


# --- Prompts, pictures and checks ---------------------------------------------


def test_people_are_read_from_photo_names(tmp_path):
    for name in ("Sam 1.jpg", "sam_beach.png", "Alex-2.webp", "notes.txt", "Pat.jpeg"):
        (tmp_path / name).write_bytes(b"x")
    people = artwork_prompts.people_in(str(tmp_path))
    assert sorted(people) == ["Alex", "Pat", "Sam"] and len(people["Sam"]) == 2
    assert artwork_prompts.people_in("/nowhere") == {}
    photos = ["a", "b", "c"]
    assert artwork_prompts.choose_photos(photos, "Primary", 2) == ["a", "b"]
    assert artwork_prompts.choose_photos(photos, "Thumb", 2) == ["b", "c"]


def test_model_prompts_number_their_references():
    details = {
        "title": "Neon Saints",
        "year": 1987,
        "kind": "film",
        "genres": ["Crime"],
        "overview": "Two drivers.",
    }
    poster = artwork_prompts.model_prompt(
        "Primary", details, {"style": "Bold."}, {"Sam": [1, 2], "Alex": [3]}
    )
    assert '"NEON SAINTS"' in poster
    assert "Sam (images 1 and 2) and Alex (image 3)" in poster
    assert "No other text" in poster and "The look: Bold." in poster
    assert "1987" not in poster and "Crime," not in poster  # it would letter them
    assert "weathered silver serif capitals" in poster  # crime's lettering
    alone = artwork_prompts.model_prompt("Primary", details, {}, {"Sam": [1]})
    assert "Only one person appears" in alone
    backdrop = artwork_prompts.model_prompt("Backdrop", details, {}, {}, set_image=1)
    assert (
        "No text" in backdrop
        and "image 1" in backdrop
        and "original characters" in backdrop
    )
    logo = artwork_prompts.model_prompt("Logo", details, {}, {}, lettering_image=1)
    assert "pure black background" in logo and "image 1" in logo


def test_a_logo_is_keyed_to_real_transparency():
    picture = Image.new("RGB", (800, 400), (0, 0, 0))
    picture.paste((230, 200, 40), (200, 150, 600, 250))
    keyed, facts = imaging.key_out(picture)
    assert keyed.mode == "RGBA" and 0.5 < facts["opaque"] < 1
    assert keyed.getpixel((0, 0))[3] == 0 or keyed.size[0] < 800
    data, steps, _facts = imaging.finish(imaging.png(picture), {"transparent": True})
    assert "transparency" in steps[0]
    assert artwork_jobs.check_picture(data, "Logo")["has_alpha"]
    wide = Image.new("RGB", (1920, 1088), (40, 80, 120))
    assert imaging.crop_to(wide, 16 / 9).size == (1920, 1080)


def test_titles_are_read_back():
    good = checks.title_match(
        "The Last Lighthouse", [["THE LAST", 0.95], ["LIGHTHOUSE", 0.99]]
    )
    assert good["value"] == 1.0 and good["stray"] == 0
    bad = checks.title_match(
        "The Last Lighthouse",
        [["THE LAST", 0.9], ["LIGHHUUSE", 0.9], ["CDEREANBA SVONE", 0.7]],
    )
    assert bad["value"] < checks.TITLE_OK and bad["stray"] > 0
    like = checks.likeness(
        {"faces": [{"sims": {"A": 0.6, "B": 0.2}}, {"sims": {"A": 0.17, "B": 0.59}}]},
        ["A", "B"],
    )
    assert like["value"] == 0.59 and not like["mixed"]
    same = checks.likeness({"faces": [{"sims": {"A": 0.6, "B": 0.55}}]}, ["A", "B"])
    assert same["mixed"]
    twice = checks.likeness(
        {"faces": [{"sims": {"A": 0.69}}, {"sims": {"A": 0.62}}]}, ["A"]
    )
    assert twice["twice"] == ["A"] and not twice["mixed"]


def test_a_misspelled_title_is_made_again(monkeypatch, tmp_path):
    from curatarr.artwork_worker import pipeline
    from curatarr.artwork_worker.backends.mock import MockBackend

    reads = iter([[["FILN", 0.9]], [["FILM", 0.99]]])
    monkeypatch.setattr(
        checks,
        "run",
        lambda *a, **k: {"ocr": {"available": True, "texts": next(reads)}},
    )
    backend = MockBackend({}, tmp_path)
    task = {
        "image": "x",
        "engine": "mock/test",
        "kind": "Primary",
        "title": "Film",
        "lettered": True,
        "size": [1024, 1536],
        "aspect": 2 / 3,
        "seed": 5,
        "prompt": "p",
    }
    result = pipeline.make_picture(
        task,
        backend,
        [],
        lambda *a: None,
        {"python": "/fake", "title_retries": 2, "_work_dir": str(tmp_path)},
    )
    meta = result["meta"]
    assert meta["metrics"]["title_match"]["value"] == 1.0 and meta["seed"] == 5 + 7919
    assert "title read as" in meta["steps"][0]


# --- Codex, locked down ----------------------------------------------------------


def _fake_codex(tmp_path, monkeypatch, body):
    script = tmp_path / "codex"
    script.write_text("#!/usr/bin/env python3\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    return CodexBackend({"executable": str(script), "timeout": 60}, tmp_path / "work")


CODEX_OK = """import json, os, sys
from pathlib import Path
if sys.argv[1:3] == ["login", "status"]:
    print("Logged in using ChatGPT"); sys.exit()
if sys.argv[1:3] == ["features", "list"]:
    print("image_generation   stable   true"); sys.exit()
prompt = sys.stdin.read()
Path(os.environ["CODEX_HOME"], "args.json").parent.mkdir(parents=True, exist_ok=True)
Path(os.environ["CODEX_HOME"], "args.json").write_text(json.dumps({"argv": sys.argv, "prompt": prompt}))
print(json.dumps({"type": "thread.started", "thread_id": "t1"}))
out = Path(os.environ["CODEX_HOME"], "generated_images", "t1")
out.mkdir(parents=True, exist_ok=True)
from PIL import Image
Image.new("RGB", (1024, 1536), (200, 50, 50)).save(out / "exec-1.png")
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "DONE"}}))
"""

CODEX_LIMITED = """import json, sys
if sys.argv[1:3] == ["login", "status"]:
    print("Logged in using ChatGPT"); sys.exit()
if sys.argv[1:3] == ["features", "list"]:
    print("image_generation   stable   true"); sys.exit()
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "t2"}))
sys.stderr.write('ERROR image generation failed: http 429: {"error":{"type":"usage_limit_reached","resets_in_seconds":5000}}')
"""


def test_codex_runs_locked_down_with_the_prompt_on_stdin(tmp_path, monkeypatch):
    backend = _fake_codex(tmp_path, monkeypatch, CODEX_OK)
    assert backend.engines()[0]["ready"] and backend.engines()[0]["external"]
    reference = tmp_path / "sam.jpg"
    reference.write_bytes(_picture("#aa8866"))
    task = {"image": "img1", "engine": "codex/image", "prompt": "; rm -rf / $(whoami)"}
    made = backend.make(
        task, [Reference(1, reference, "person", "Sam")], lambda *a: None
    )
    assert Image.open(BytesIO(made.data)).size == (1024, 1536)
    seen = json.loads((tmp_path / "codex-home" / "args.json").read_text())
    argv = seen["argv"]
    assert seen["prompt"] == task["prompt"] and task["prompt"] not in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    for feature in ("shell_tool", "unified_exec", "apps", "plugins"):
        assert feature in argv
    assert "--ignore-user-config" in argv and "-i" in argv
    assert not (tmp_path / "work" / "codex" / "img1").exists()  # its folder's gone


def test_codex_says_when_its_allowance_is_used_up(tmp_path, monkeypatch):
    backend = _fake_codex(tmp_path, monkeypatch, CODEX_LIMITED)
    with pytest.raises(Failed) as caught:
        backend.make(
            {"image": "i", "engine": "codex/image", "prompt": "p"}, [], lambda *a: None
        )
    assert caught.value.code == "usage_limit" and caught.value.retry_after >= 5000
    ready, why = backend.status()
    assert not ready and "resets in about" in why


def test_codex_isnt_offered_when_missing(tmp_path):
    backend = CodexBackend({"executable": "no-such-codex"}, tmp_path)
    [engine] = backend.engines()
    assert not engine["ready"] and "isn't installed" in engine["why"]


# --- ComfyUI's workflows -------------------------------------------------------


def test_workflows_find_their_models_and_build_graphs():
    available = {
        "UNETLoader": [
            "flux-2-klein-base-4b-fp8.safetensors",
            "flux-2-klein-4b-fp8.safetensors",
        ],
        "CLIPLoader": ["qwen3vl_4b_fp8_scaled.safetensors", "qwen_3_4b.safetensors"],
        "VAELoader": ["flux2-vae.safetensors"],
    }
    flux = Flux2Klein()
    models, _ = flux.resolve(available)
    assert models["UNETLoader"] == "flux-2-klein-4b-fp8.safetensors"
    assert models["CLIPLoader"] == "qwen_3_4b.safetensors"
    graph = flux.graph("A poster", (1024, 1536), 7, ["curatarr-x/ref.png"], models)
    assert graph["ref0"]["inputs"]["image"] == "curatarr-x/ref.png"
    assert graph["guider"]["inputs"]["positive"] == ["pos0", 0]
    assert graph["noise"]["inputs"]["noise_seed"] == 7
    krea = next(w for w in WORKFLOWS if w.key == "comfyui/krea2-turbo")
    missing, why = krea.resolve(available)  # Krea 2 isn't there
    assert missing is None and "needs" in why


# --- Benchmarks ------------------------------------------------------------------


def test_a_benchmark_compares_engines_blind(app, client, tmp_path):
    photos = tmp_path / "people"
    photos.mkdir()
    (photos / "Alex 1.png").write_bytes(_picture("#aa8866"))
    (photos / "Sam 1.png").write_bytes(_picture("#8866aa"))
    worker_row, _token = artwork_jobs.create_worker("Bench", 4)
    worker_row.capabilities = artwork_jobs._clean_capabilities(
        [{"engine": e, "label": e, "references": True} for e in ("mock/a", "mock/b")]
    )
    db.session.commit()
    run = artwork_bench.start(["mock/a", "mock/b"], ["duo", "logo"], photos=str(photos))
    assert len(run.jobs) == 2 * (1 + 2)  # duo once, logo twice, per engine
    duo = next(j for j in run.jobs if j.options["scenario"] == "duo")
    assert duo.options["cast"] == ["Alex", "Sam"] and duo.media is None
    for _ in range(3):
        for task in artwork_jobs.claim(worker_row, 4, ["mock/a", "mock/b"]):
            kind = db.session.get(ArtworkImage, task["image"]).kind
            if kind == "Logo":
                picture = Image.new("RGBA", (1200, 600), (0, 0, 0, 0))
                picture.paste((255, 255, 255, 255), (100, 100, 1100, 500))
                buffer = BytesIO()
                picture.save(buffer, "PNG")
                data = buffer.getvalue()
            else:
                data = _art()
            meta = {
                "duration_s": 3,
                "metrics": {"title_match": {"value": 1.0, "kind": "measured"}},
            }
            assert artwork_jobs.submit(worker_row, task["image"], data, meta)["ok"]
    summary = artwork_bench.results(run)["engines"]
    assert summary["mock/a"]["made"] == 3 and summary["mock/a"]["title"] == 1.0
    left, right = artwork_bench.pair(run)
    assert left.engine != right.engine
    page = client.get(f"/library/posters/benchmarks/compare?run={run.id}").text
    assert "mock/a" not in page and "mock/b" not in page  # blind
    client.post(
        "/library/posters/benchmarks/compare",
        data={"left": left.id, "right": right.id, "winner": "left", "run": run.id},
    )
    assert artwork_bench.wins(left.engine)["won"] == 1
    artwork_bench.rate(left, {"overall": "5", "identity": "9", "bogus": "1"})
    assert artwork_bench.ratings_of(left) == {"overall": 5, "identity": 5}
    client.post(
        "/library/posters/benchmarks/default", data={"kind": "Logo", "engine": "mock/b"}
    )
    assert artwork_jobs.settings()["defaults"] == {"Logo": "mock/b"}
    with pytest.raises(ValueError, match="rated, not put on"):
        artwork_jobs.approve(left)
    assert client.get(f"/library/posters/benchmarks?run={run.id}").status_code == 200


def test_old_turned_down_pictures_are_tidied_away(app, client, tmp_path):
    _library, media, _part = _movie()
    worker_row, _token = artwork_jobs.create_worker("W", 1)
    job = artwork_jobs.create_job(media, ["Primary"], details={"title": "Film"})
    [task] = _claim(worker_row)
    artwork_jobs.submit(worker_row, task["image"], _art(), {})
    candidate = job.images[0].candidates[0]
    artwork_jobs.reject(candidate)
    path = Path(candidate.path)
    assert path.exists()
    assert artwork_jobs.prune(utcnow() + timedelta(days=31)) == 1
    assert not path.exists() and candidate.path is None
    assert db.session.query(CustomPoster).count() == 0


def test_the_worker_keeps_a_picture_it_couldnt_send(app, client, tmp_path, monkeypatch):
    import requests

    _library, media, _part = _movie()
    _row, _token, worker = _worker(app, client, tmp_path)
    artwork_jobs.create_job(media, ["Primary"], details={"title": "Film"})
    real = worker.server.result
    monkeypatch.setattr(
        worker.server,
        "result",
        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError()),
    )
    _drain(worker, rounds=1)
    assert len(list((tmp_path / "work" / "outbox").glob("*.png"))) == 1
    monkeypatch.setattr(worker.server, "result", real)
    worker.flush_outbox()
    assert not list((tmp_path / "work" / "outbox").glob("*.png"))
    assert db.session.query(ArtworkCandidate).count() == 1


def test_a_refused_token_stops_the_worker(app, client, tmp_path):
    _row, _token, worker = _worker(app, client, tmp_path)
    _row.enabled = False
    db.session.commit()
    with pytest.raises(Unauthorised):
        worker.hello()
    os.environ.pop("CURATARR_WORKER_TOKEN", None)


def test_text_on_a_background_is_made_again(monkeypatch, tmp_path):
    from curatarr.artwork_worker import pipeline
    from curatarr.artwork_worker.backends.mock import MockBackend

    reads = iter([[["CITONT IONE GLUO", 0.8]], []])
    monkeypatch.setattr(
        checks,
        "run",
        lambda *a, **k: {"ocr": {"available": True, "texts": next(reads)}},
    )
    task = {
        "image": "y",
        "engine": "mock/test",
        "kind": "Backdrop",
        "title": "Film",
        "lettered": False,
        "size": [1920, 1088],
        "aspect": 16 / 9,
        "seed": 1,
        "prompt": "p",
    }
    result = pipeline.make_picture(
        task,
        MockBackend({}, tmp_path),
        [],
        lambda *a: None,
        {"python": "/fake", "title_retries": 2, "_work_dir": str(tmp_path)},
    )
    assert "text on the background" in result["meta"]["steps"][0]
    assert result["meta"]["metrics"]["stray_text"]["value"] == 0


def test_a_persons_description_goes_beside_their_photos(tmp_path):
    (tmp_path / "Sam 1.jpg").write_bytes(b"x")
    (tmp_path / "Sam.txt").write_text("a woman of about 30,\n platinum pixie cut")
    assert artwork_prompts.descriptions(str(tmp_path)) == {
        "Sam": "a woman of about 30, platinum pixie cut"
    }
    details = {"title": "Film", "genres": ["Drama"], "overview": ""}
    prompt = artwork_prompts.model_prompt(
        "Primary", details, {"photos": str(tmp_path)}, {"Sam": [1]}
    )
    assert "Sam (image 1; a woman of about 30, platinum pixie cut)" in prompt


def test_curatarr_picks_the_recommended_engine_for_each_type(app, client):
    _library, media, _part = _movie()
    worker_row, _token = artwork_jobs.create_worker("GPU", 4)
    worker_row.capabilities = artwork_jobs._clean_capabilities(
        [
            {"engine": e, "label": e, "references": e != "comfyui/krea2-turbo"}
            for e in (
                "comfyui/sdxl-faceid",
                "comfyui/krea2-turbo",
                "comfyui/flux2-klein-4b",
            )
        ]
    )
    db.session.commit()
    artwork_jobs.create_job(media, ["Primary", "Logo"], details={"title": "Film"})
    ready = [e["engine"] for e in worker_row.capabilities]
    [poster] = artwork_jobs.claim(worker_row, 4, ready)
    assert poster["engine"] == "comfyui/flux2-klein-4b"
    artwork_jobs.submit(worker_row, poster["image"], _art(), {})
    [logo] = artwork_jobs.claim(worker_row, 4, ready)
    assert logo["engine"] == "comfyui/krea2-turbo"
    artwork_jobs.set_default_engine(
        "Logo", "comfyui/sdxl-faceid"
    )  # a person's choice wins
    job = artwork_jobs.create_job(media, ["Logo"], details={"title": "Film"})
    [again] = artwork_jobs.claim(worker_row, 4, ready)
    assert again["engine"] == "comfyui/sdxl-faceid" and again["job"] == job.id


def test_alternatives_are_made_and_compared_with_the_current_pictures(
    app, client, tmp_path, monkeypatch
):
    from curatarr import artwork_compare
    from curatarr.models import ArtworkPreference

    library, media, _part = _movie()
    jellyfin = _jellyfin(monkeypatch, _picture("#223344"))
    current = custom_posters.apply(media, _art(), "01_film.jpg")
    other = _second(library, "Other", "jf-other")
    from curatarr.models import MediaPart

    db.session.add(
        MediaPart(media_identity_id=other.id, kind="movie_file", has_file=True)
    )
    db.session.commit()
    spec = {
        "titles": "movies",
        "kinds": ["Primary", "Logo"],
        "alternatives": True,
        "options": {},
    }
    assert [m.title for m in artwork_jobs.batch_titles(spec)] == ["Film"]
    batch = artwork_jobs.create_batch(spec)
    [job] = batch.jobs
    assert job.kinds == ["Primary"]  # only what it has
    _row, _token, worker = _worker(app, client, tmp_path)
    _drain(worker)
    page = client.get("/library/posters/compare").text
    assert "Which do you like better?" in page and "1 left to compare" in page
    candidate, shown, _new_left = artwork_compare.next_matchup()
    assert shown.id == current.id and candidate.image.job_id == job.id
    client.post(
        "/library/posters/compare",
        data={
            "candidate": candidate.id,
            "current": shown.id,
            "winner": "new",
            "new_left": "1",
        },
    )
    assert artwork_compare.next_matchup() is None
    results = artwork_compare.results()
    assert results["overall"]["new"] == 1 and results["kinds"]["Primary"]["new"] == 1
    assert [c.candidate_id for c in results["preferred"]] == [candidate.id]
    assert jellyfin.uploads == 1  # only the first one: comparing changes nothing
    assert "New ones you preferred" in client.get("/library/posters/compare").text
    client.post("/library/posters/compare/undo")
    assert db.session.query(ArtworkPreference).count() == 0


def test_the_prompt_options_can_be_made_locally(app, client, monkeypatch):
    from curatarr.services import set_setting

    _library, media, _part = _movie()
    _jellyfin(monkeypatch, _picture("#223344"))
    set_setting(
        custom_posters.KEY,
        {"prompts": {"titles": "movies", "kinds": ["Primary"], "skip_done": True}},
    )
    db.session.commit()
    response = client.post("/library/posters/prompts/generate")
    assert response.status_code == 302
    [job] = db.session.query(ArtworkJob).all()
    assert job.media_identity_id == media.id and job.engine == "auto"
    assert job.options["external_ok"] is False
    assert "Make local alternatives to compare" in client.get("/library/posters").text


def test_reference_photos_are_cropped_to_the_head(monkeypatch, tmp_path):
    from curatarr.artwork_worker import pipeline

    photo = tmp_path / "sam.png"
    Image.new("RGB", (1500, 2000), (90, 90, 90)).save(photo)
    other = tmp_path / "sam2.png"
    Image.new("RGB", (1000, 1300), (90, 90, 90)).save(other)
    odd = tmp_path / "sam3.png"  # sunglasses: least like the other two
    Image.new("RGB", (1000, 1300), (90, 90, 90)).save(odd)
    monkeypatch.setattr(
        checks,
        "run",
        lambda *a, **k: {
            "boxes": {
                str(photo): {"box": [700, 400, 860, 600], "embedding": [1.0, 0.0]},
                str(other): {"box": [300, 200, 700, 700], "embedding": [0.9, 0.436]},
                str(odd): {"box": [300, 200, 700, 700], "embedding": [0.0, 1.0]},
            }
        },
    )
    refs = [
        Reference(1, photo, "person", "Sam"),
        Reference(2, other, "person", "Sam"),
        Reference(3, odd, "person", "Sam"),
    ]
    work = tmp_path / "work"
    out, cropped = pipeline._crop_people(refs, "/fake", {"_work_dir": str(work)})
    assert cropped == 3 and out[0].path == work / "crops" / "1-sam-head.png"
    assert not list(tmp_path.glob("*-head.png"))  # never beside the photos
    assert Image.open(out[0].path).size == (320, 320)  # 1.6 face heights
    assert out[2].typical < out[1].typical and out[1].face_px == 400
    from curatarr.artwork_worker.backends.comfyui import fit_references

    [chosen], prompt = fit_references(out, "Sam (images 1, 2 and 3)", 1, 1)
    assert chosen.path.name == "2-sam2-head.png" and prompt == "Sam (image 1)"
    two, prompt = fit_references(out, "Sam (images 1, 2 and 3), then image 9", 8, 2)
    assert [r.path.name for r in two] == ["1-sam-head.png", "2-sam2-head.png"]
    assert prompt == "Sam (images 1 and 2), then image 9"


def test_the_checks_helper_answers_each_request(tmp_path):
    """The worker's call and the helper's answer, without the models: a
    fake checks environment that echoes what it was asked."""
    import sys

    fake = tmp_path / "python"
    fake.write_text(
        "#!/bin/sh\n"
        f'exec {sys.executable} -c "import json,sys; r=json.load(sys.stdin); '
        "print(json.dumps({'asked': sorted(k for k, v in r.items() if v)}))\"\n"
    )
    fake.chmod(0o755)
    photo = tmp_path / "a.png"
    asked = checks.run(str(fake), None, boxes=[photo], root="r")["asked"]
    assert "boxes" in asked and "image" not in asked
    asked = checks.run(str(fake), photo, ocr=True)["asked"]
    assert {"image", "ocr"} <= set(asked)
    assert checks.run("", photo, ocr=True) == {}


def test_a_three_image_model_gets_each_persons_sharpest_photo(tmp_path):
    from curatarr.artwork_worker.backends.comfyui import fit_references

    paths = []
    for i in range(5):
        path = tmp_path / f"{i}.png"
        Image.new("RGB", (300, 400), (40 * i, 80, 120)).save(path)
        paths.append(path)
    refs = [
        Reference(1, paths[0], "person", "Sam", 120),
        Reference(2, paths[1], "person", "Sam", 480),
        Reference(3, paths[2], "person", "Alex"),
        Reference(4, paths[3], "person", "Alex"),
        Reference(5, paths[4], "set"),
    ]
    prompt = (
        "Played by Sam (images 1 and 2) and Alex (images 3 and 4). "
        "The same look as image 5."
    )
    fitted, renumbered = fit_references(refs, prompt, 3, 1)
    assert [(r.number, r.role, r.person) for r in fitted] == [
        (1, "person", "Sam"),
        (2, "person", "Alex"),
        (3, "set", None),
    ]
    assert renumbered == (
        "Played by Sam (image 1) and Alex (image 2). The same look as image 3."
    )
    assert fitted[0].path == paths[1]  # Sam's sharper photo


def test_the_prompt_options_can_go_to_chatgpt(app, client, monkeypatch):
    from curatarr.services import set_setting

    _library, media, _part = _movie()
    _jellyfin(monkeypatch, _picture("#223344"))
    set_setting(
        custom_posters.KEY,
        {"prompts": {"titles": "movies", "kinds": ["Primary"], "photos": "/p"}},
    )
    db.session.commit()
    client.post("/library/posters/prompts/generate", data={"engine": "codex/image"})
    [job] = db.session.query(ArtworkJob).all()
    assert job.engine == "codex/image" and job.options["external_ok"] is True
    assert job.media_identity_id == media.id
    assert "Make them with ChatGPT" in client.get("/library/posters").text


def test_an_outside_service_isnt_asked_again_by_itself(monkeypatch, tmp_path):
    from curatarr.artwork_worker import pipeline
    from curatarr.artwork_worker.backends.mock import MockBackend

    class Codex(MockBackend):
        name = "codex"

    made = []
    monkeypatch.setattr(
        checks, "run", lambda *a, **k: {"ocr": {"available": True, "texts": []}}
    )
    backend = Codex({}, tmp_path)
    original = backend.make
    backend.make = lambda *a: made.append(1) or original(*a)
    task = {
        "image": "z",
        "engine": "codex/image",
        "kind": "Primary",
        "title": "Hush",
        "lettered": True,
        "size": [1024, 1536],
        "aspect": 2 / 3,
        "seed": 1,
        "prompt": "p",
    }
    result = pipeline.make_picture(
        task,
        backend,
        [],
        lambda *a: None,
        {"python": "/fake", "title_retries": 2, "_work_dir": str(tmp_path)},
    )
    assert len(made) == 1  # a misread title doesn't spend more of the allowance
    assert result["meta"]["metrics"]["title_match"]["value"] == 0.0


def test_setup_saves_where_codex_is(monkeypatch, tmp_path):
    import io

    from curatarr.artwork_worker import cli

    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.write_text("#!/bin/sh\n")
    codex.chmod(0o755)
    monkeypatch.setenv("PATH", str(codex.parent))
    monkeypatch.setenv("CURATARR_WORKER_TOKEN", "cwk_test")
    monkeypatch.setattr(cli, "check", lambda args: 0)
    monkeypatch.setattr(cli, "CHECKS_ENV", tmp_path / "none")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"token_file": str(tmp_path / "token")}))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["--config", str(config), "setup", "--server", "http://x"]) == 0
    saved = json.loads(config.read_text())
    assert saved["backends"]["codex"] == saved["backends"]["codex"] | {
        "enabled": True,
        "executable": str(codex),
    }
    assert (tmp_path / "token").read_text().strip() == "cwk_test"


def test_setup_can_take_the_token_and_start_the_service(monkeypatch, tmp_path):
    from curatarr.artwork_worker import cli

    monkeypatch.setenv("PATH", str(tmp_path))  # no Codex here
    monkeypatch.setattr(cli, "check", lambda args: 0)
    monkeypatch.setattr(cli, "CHECKS_ENV", tmp_path / "none")
    monkeypatch.setattr(cli, "SERVICE", tmp_path / "unit" / "w.service")
    ran = []
    monkeypatch.setattr(
        cli.subprocess, "run", lambda command, check: ran.append(command[2:])
    )
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"token_file": str(tmp_path / "token")}))
    argv = [
        "--config",
        str(config),
        "setup",
        "--server",
        "http://x",
        "--token",
        "cwk_abc",
        "--service",
    ]
    assert cli.main(argv) == 0
    assert (tmp_path / "token").read_text().strip() == "cwk_abc"
    unit = (tmp_path / "unit" / "w.service").read_text()
    assert f"--config {config} run" in unit and "Restart=on-failure" in unit
    assert ran == [
        ["daemon-reload"],
        ["enable", "curatarr-artwork-worker"],
        ["restart", "curatarr-artwork-worker"],
    ]


def test_a_service_follows_the_installers_current_release(tmp_path):
    from curatarr.artwork_worker.cli import stable

    release = tmp_path / "opt" / "releases" / "20261029-1" / "bin"
    release.mkdir(parents=True)
    (tmp_path / "opt" / "current").symlink_to(release.parent)
    assert stable(release / "python") == tmp_path / "opt" / "current" / "bin" / "python"
    assert (
        stable(tmp_path / "venv" / "bin" / "python")
        == tmp_path / "venv" / "bin" / "python"
    )


def test_a_waiting_picture_says_why_and_its_engine_can_change(app, client, tmp_path):
    _library, media, _part = _movie()
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "Sam.jpg").write_bytes(_picture("#aa8866"))
    worker_row, _token = artwork_jobs.create_worker("Codex PC", 1)
    worker_row.capabilities = artwork_jobs._clean_capabilities(
        [
            {
                "engine": "codex/image",
                "label": "Codex image tool (ChatGPT)",
                "references": True,
                "external": True,
            }
        ]
    )
    worker_row.last_seen_at = utcnow()
    db.session.commit()
    job = artwork_jobs.create_job(
        media, ["Primary"], options={"photos": str(photos)}, details={"title": "Film"}
    )
    reason = artwork_jobs.waiting_reason(job.images[0])
    assert "Best available" in reason and "Codex image tool" in reason
    assert (
        "only uses engines on your own computers"
        in client.get("/library/posters/jobs").text
    )
    page = client.get(f"/library/posters/jobs/{job.id}").text
    assert "Engine for what's waiting" in page
    # Without allowing the photos out, it's refused; with, it's made.
    client.post(
        f"/library/posters/jobs/{job.id}/engine", data={"engine": "codex/image"}
    )
    assert job.engine == "auto"
    client.post(
        f"/library/posters/jobs/{job.id}/engine",
        data={"engine": "codex/image", "external_ok": "on"},
    )
    db.session.expire_all()
    assert job.engine == "codex/image" and job.options["external_ok"] is True
    assert artwork_jobs.waiting_reason(job.images[0]) is None
    [task] = artwork_jobs.claim(worker_row, 1, ["codex/image"])
    assert task["image"] == job.images[0].id
    # With only Codex on offer, Generate chooses it.
    assert (
        'value="codex/image" selected' in client.get("/library/posters/generate").text
    )


def test_no_worker_online_is_said(app, client):
    _library, media, _part = _movie()
    job = artwork_jobs.create_job(media, ["Primary"], details={"title": "Film"})
    assert "No worker is online" in artwork_jobs.waiting_reason(job.images[0])


def test_best_available_uses_an_allowed_outside_service_when_nothing_local_can(
    app, client
):
    _library, media, _part = _movie()
    worker_row, _token = artwork_jobs.create_worker("Codex PC", 1)
    worker_row.capabilities = artwork_jobs._clean_capabilities(
        [
            {
                "engine": "codex/image",
                "label": "Codex",
                "references": True,
                "external": True,
            }
        ]
    )
    db.session.commit()
    artwork_jobs.create_job(media, ["Primary"], details={"title": "Film"})
    assert artwork_jobs.claim(worker_row, 1, ["codex/image"]) == []  # not allowed
    allowed = artwork_jobs.create_job(
        media, ["Primary"], options={"external_ok": True}, details={"title": "Film"}
    )
    [task] = artwork_jobs.claim(worker_row, 1, ["codex/image"])
    assert task["image"] == allowed.images[0].id and task["engine"] == "codex/image"


def test_a_busy_database_asks_the_worker_to_come_back(app, client, monkeypatch):
    from sqlalchemy.exc import OperationalError

    _row, token = artwork_jobs.create_worker("W", 1)

    def locked(*_a, **_k):
        raise OperationalError("SELECT", {}, Exception("database is locked"))

    monkeypatch.setattr(artwork_jobs, "claim", locked)
    response = client.post(
        "/api/v1/artwork/worker/claim",
        json={"slots": 1, "ready": ["x"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 503 and "busy" in response.get_json()["error"]


def test_the_forms_remember_their_last_choices(app, client, monkeypatch, tmp_path):
    _library, media, _part = _movie()
    _jellyfin(monkeypatch, _picture("#223344"))
    photos = tmp_path / "photos"
    photos.mkdir()
    worker_row, _token = artwork_jobs.create_worker("Codex PC", 1)
    worker_row.capabilities = artwork_jobs._clean_capabilities(
        [
            {
                "engine": "codex/image",
                "label": "Codex",
                "references": True,
                "external": True,
            }
        ]
    )
    db.session.commit()
    client.post(
        "/library/posters/generate",
        data={
            "media": [media.id],
            "kinds": ["Primary", "Logo"],
            "engine": "codex/image",
            "photos": str(photos),
            "cast": "Sam",
            "style": "Moody.",
            "external_ok": "on",
            "worker": worker_row.id,
        },
    )
    page = client.get("/library/posters/generate").text
    assert 'name="external_ok" id="g-external" checked' in page
    assert (
        'id="g-kind-Logo" checked' in page and 'id="g-kind-Thumb" checked' not in page
    )
    assert 'value="Sam"' in page and "Moody.</textarea>" in page
    assert f'value="{worker_row.id}" selected' in page
    assert 'value="codex/image" selected' in page
    client.get(
        "/library/posters/batches",
        query_string={
            "preview": "1",
            "titles": "movies",
            "kinds": ["Banner"],
            "external_ok": "on",
        },
    )
    page = client.get("/library/posters/batches").text
    assert 'id="b-kind-Banner" checked' in page and 'id="f-external" checked' in page
    assert 'value="movies" selected' in page
    client.post(
        "/library/posters/benchmarks",
        data={"engines": ["codex/image"], "scenarios": ["logo"], "external_ok": "on"},
    )
    page = client.get("/library/posters/benchmarks").text
    assert 'name="external_ok" id="b-ext" checked' in page
    assert 'id="bs-logo" checked' in page and 'id="bs-portrait" checked' not in page


def test_review_shows_every_version_and_puts_it_on_for_whom_you_choose(
    app, client, tmp_path, monkeypatch
):
    from curatarr.services import set_setting

    _library, media, _part = _movie()
    jellyfin = _jellyfin(monkeypatch, _picture("#223344"))
    set_setting("jellyfin_user_names", {"u1": "Sam", "u2": "Alex", "u3": "Pat"})
    db.session.commit()
    ours = _art()
    custom_posters.apply(media, ours, "01_film.jpg", user_id="u1")
    custom_posters.apply(media, ours, "01_film.jpg", user_id="u2")
    accounts = [("u1", "Sam"), ("u2", "Alex"), ("u3", "Pat")]
    versions = artwork_jobs.in_use(media.id, "Primary", accounts)
    assert [v["who"] for v in versions] == [["Everyone else"], ["Sam", "Alex"]]
    assert versions[0]["poster"] is None  # Jellyfin's own
    _row, _token, worker = _worker(app, client, tmp_path)
    job = artwork_jobs.create_job(media, ["Primary"])
    _drain(worker)
    page = client.get(f"/library/posters/jobs/{job.id}").text
    assert "Now: Alex, Sam" in page and "Now: Everyone else" in page
    assert "Put on for" in page and 'name="who" value="u3"' in page
    first = job.images[0].candidates[0]
    client.post(
        f"/library/posters/candidates/{first.id}/approve",
        data={"audience": "1", "everyone": "on", "who": ["u3"]},
    )
    db.session.expire_all()
    assert jellyfin.uploads == 1  # everyone's, in Jellyfin
    assert custom_posters.active(media.id, "u3") is not None  # Pat's own too
    assert custom_posters.active(media.id, "u1").source == "01_film.jpg"  # kept
    # Another version, for Sam only: the first stays on for the others.
    artwork_jobs.regenerate(job.images[0])
    _drain(worker)
    db.session.expire_all()
    second = job.images[0].candidates[-1]
    message = artwork_jobs.approve(second, everyone=False, accounts=["u1"])
    assert "for Sam" in message
    db.session.expire_all()
    assert first.decision == "approved" and second.decision == "approved"
    assert custom_posters.active(media.id, "u1").source != "01_film.jpg"
    with pytest.raises(ValueError, match="Choose who sees it"):
        artwork_jobs.approve(second, everyone=False, accounts=[])


def test_original_art_comes_from_jellyfin_when_it_isnt_beside_the_file(
    app, client, tmp_path, monkeypatch
):
    _library, media, _part = _movie()
    jellyfin = _jellyfin(monkeypatch, _picture("#445566"))
    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "Sam.jpg").write_bytes(_picture("#aa8866"))
    worker_row, _token = artwork_jobs.create_worker("Codex PC", 1)
    worker_row.capabilities = artwork_jobs._clean_capabilities(
        [
            {
                "engine": "codex/image",
                "label": "Codex",
                "references": True,
                "external": True,
            }
        ]
    )
    db.session.commit()
    job = artwork_jobs.create_job(
        media,
        ["Primary"],
        engine="codex/image",
        options={"photos": str(photos), "originals": True, "external_ok": True},
    )
    [task] = artwork_jobs.claim(worker_row, 1, ["codex/image"])
    roles = [r["role"] for r in task["references"]]
    assert roles.count("original") == 2  # its poster and its logo
    assert "original poster" in task["prompt"]
    page = client.get(f"/library/posters/jobs/{job.id}").text
    assert "References it was given: 1 photo of Sam, 2 pieces of original art" in page
    assert '<details class="mt-3 artwork-fold" id="refs-' in page  # folded away
    assert 'data-alt="Reference 1: Sam"' in page  # opens full screen
    # A custom poster for everyone: its saved original, never the custom one.
    jellyfin.current = _picture("#112233")
    custom_posters.apply(media, _art(), "Uploaded")
    saved = custom_posters.active(media.id).original_path
    assert artwork_jobs.original_art(media, "Primary") == Path(saved)
    # Jellyfin's own picture, to compare with.
    response = client.get(f"/library/posters/now/{media.id}/Primary")
    assert response.status_code == 200 and response.data
    assert client.get(f"/library/posters/now/{media.id}/Nope").status_code == 404


def test_pages_update_only_what_changed(app, client, tmp_path, monkeypatch):
    _library, media, _part = _movie()
    _jellyfin(monkeypatch, _picture("#223344"))
    _row, _token, worker = _worker(app, client, tmp_path)
    job = artwork_jobs.create_job(media, ["Primary", "Logo"])
    image = job.images[0]
    page = client.get(f"/library/posters/jobs/{job.id}").text
    assert 'data-active="1"' in page and 'data-live="image"' in page
    assert f'data-live-key="{artwork_jobs.live_key(image)}"' in page
    listing = client.get("/library/posters/jobs").text
    assert 'data-live="row"' in listing and "data-progress-for" not in listing
    status = client.get(
        f"/library/posters/live?images={image.id}&jobs={job.id}"
    ).get_json()
    before = status["images"][image.id]
    assert status["active"] is True and status["progress"][image.id] == 0
    _drain(worker)
    status = client.get(
        f"/library/posters/live?images={image.id}&jobs={job.id}"
    ).get_json()
    assert status["images"][image.id] != before  # made: its panel is redrawn
    assert status["active"] is False
    panel = client.get(f"/library/posters/images/{image.id}/panel").text
    assert panel.startswith("{#") is False and 'id="image-' in panel
    assert "Approve and put on" in panel
    row = client.get(f"/library/posters/jobs/{job.id}/row").text
    assert row.lstrip().startswith("<tr") and 'class="artwork-made' in row
    top = client.get(f"/library/posters/jobs/{job.id}/top").text
    assert "Approve the whole set" in top
    assert client.get("/library/posters/images/x/panel").status_code == 404


def test_the_ticks_start_from_who_has_a_custom_picture_now(
    app, client, tmp_path, monkeypatch
):
    from curatarr.services import set_setting

    library, media, _part = _movie()
    _jellyfin(monkeypatch, _picture("#223344"))
    set_setting("jellyfin_user_names", {"u1": "Sam", "u2": "Alex", "u3": "Pat"})
    db.session.commit()
    ours = _art()
    custom_posters.apply(media, ours, "01_film.jpg", user_id="u1")
    custom_posters.apply(media, ours, "01_film.jpg", user_id="u2")
    other = _second(library, "Other", "jf-other")
    _row, _token, worker = _worker(app, client, tmp_path)
    job = artwork_jobs.create_job(media, ["Primary", "Logo"])  # for everyone
    fresh = artwork_jobs.create_job(other, ["Primary"])
    _drain(worker)
    poster, logo = job.images
    assert artwork_jobs.default_audience(poster) == (False, ["u1", "u2"])
    assert artwork_jobs.default_audience(logo) == (True, [])  # it has no logo
    assert artwork_jobs.default_audience(fresh.images[0]) == (True, [])
    assert artwork_jobs.set_audience(job) == (True, ["u1", "u2"])
    panel = client.get(f"/library/posters/images/{poster.id}/panel").text
    assert f'id="who-{poster.id}-u1" checked' in panel
    assert f'id="who-{poster.id}-u3" checked' not in panel
    assert f'id="ev-{poster.id}" checked' not in panel


def test_review_actions_answer_without_a_page_reload(
    app, client, tmp_path, monkeypatch
):
    _library, media, _part = _movie()
    _jellyfin(monkeypatch, _picture("#223344"))
    _row, _token, worker = _worker(app, client, tmp_path)
    job = artwork_jobs.create_job(media, ["Primary", "Logo"])
    _drain(worker)
    page = client.get("/library/posters/review").text
    assert "artwork-actions.js" in page and "data-review-item" in page
    assert 'class="mt-3 artwork-decide" data-async' in page
    poster = job.images[0].candidates[0]
    answer = client.post(
        f"/library/posters/candidates/{poster.id}/approve",
        data={"audience": "1", "everyone": "on"},
        headers={"X-Curatarr-Async": "1"},
    )
    assert answer.status_code == 200
    body = answer.get_json()
    assert body["ok"] and "is on for everyone" in body["messages"][0]["text"]
    assert body["review"] == 1  # the logo still waits
    refused = client.post(
        f"/library/posters/candidates/{job.images[1].candidates[0].id}/approve",
        data={"audience": "1"},
        headers={"X-Curatarr-Async": "1"},
    ).get_json()
    assert (
        refused["ok"] is False
        and "Choose who sees it" in refused["messages"][0]["text"]
    )
    # Sent the usual way, it still redirects back.
    plain = client.post(f"/library/posters/jobs/{job.id}/first")
    assert plain.status_code == 302


def test_chatgpt_works_from_the_films_own_art_not_its_plot():
    details = {
        "title": "John Wick",
        "year": 2014,
        "kind": "film",
        "genres": ["Action"],
        "overview": "A retired hitman avenges his dog.",
    }
    people = {"Sam": [1, 2], "Alex": [3]}
    poster = artwork_prompts.agent_prompt(
        "Primary", details, {}, people, originals={"Primary": 4, "Logo": 5}
    )
    assert "Base it on the title's own artwork, its original poster (image 4)" in poster
    assert "dog" not in poster and "hitman" not in poster  # no plot to illustrate
    assert "They replace the people in the original artwork" in poster
    assert "Don't add story elements, characters, animals" in poster
    assert "own logo (image 5) shows the lettering" in poster
    backdrop = artwork_prompts.agent_prompt(
        "Backdrop", details, {}, people, originals={"Primary": 4, "Backdrop": 6}
    )
    assert (
        "its original background (image 6)" in backdrop and "lettering)" not in backdrop
    )
    banner = artwork_prompts.agent_prompt(
        "Banner", details, {}, {}, set_image=1, originals={"Primary": 2}
    )
    assert "from the story" not in banner and "new faces" not in banner
    logo = artwork_prompts.agent_prompt("Logo", details, {}, {}, originals={"Logo": 1})
    assert "Recreate the title's own logo (image 1)" in logo
    # Asked for a new scene, or with no original art: the story, held back.
    for prompt in (
        artwork_prompts.agent_prompt(
            "Primary", details, {"approach": "story"}, people, originals={"Primary": 4}
        ),
        artwork_prompts.agent_prompt("Primary", details, {}, people),
    ):
        assert "don't illustrate its plot details" in prompt
        assert "Base it on the title's own artwork" not in prompt
