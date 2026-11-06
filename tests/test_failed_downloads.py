import pytest

from curatarr import failed_downloads
from curatarr.integrations import RadarrClient, SonarrClient


class FakeArr:
    def __init__(self, service, rows):
        self.service = service
        self.rows = list(rows)
        self.calls = []
        self.has_file = False

    def queue_records(self, quick=False):
        return list(self.rows)

    def episode(self, episode_id):
        self.calls.append(("episode", episode_id))
        return {"hasFile": self.has_file}

    def movie(self, movie_id):
        self.calls.append(("movie", movie_id))
        return {"hasFile": self.has_file}

    def remove_failed_download(self, queue_id):
        self.calls.append(("remove", queue_id))
        download_id = next(
            row["downloadId"] for row in self.rows if row["id"] == queue_id
        )
        self.rows = [row for row in self.rows if row["downloadId"] != download_id]

    def episode_search(self, episode_ids):
        self.calls.append(("search_episodes", episode_ids))

    def search_movie(self, movie_id):
        self.calls.append(("search_movie", movie_id))


def row(queue_id, target_id, *, service="sonarr", status="completed"):
    result = {
        "id": queue_id,
        "downloadId": "same-download",
        "title": "Example.Show.S01",
        "status": status,
        "trackedDownloadStatus": "warning",
        "statusMessages": [{"messages": ["Could not import"]}],
        "episodeId" if service == "sonarr" else "movieId": target_id,
    }
    if service == "sonarr":
        result["seriesId"] = 1
    return result


def test_season_pack_is_removed_once_then_missing_episodes_searched(app, monkeypatch):
    arr = FakeArr("sonarr", [row(10, 101), row(11, 102)])
    monkeypatch.setattr(failed_downloads, "client", lambda _service: arr)
    monkeypatch.setattr(failed_downloads, "_dry_run_for", lambda *_args: False)

    found, errors = failed_downloads.review()
    assert not errors
    assert found[0]["count"] == 2
    assert found[0]["messages"] == ["Could not import"]

    message = failed_downloads.retry("sonarr", 10)
    assert "may find none" in message
    assert arr.calls == [
        ("episode", 101),
        ("episode", 102),
        ("remove", 10),
        ("search_episodes", [101, 102]),
    ]


def test_changed_or_existing_download_is_not_removed(monkeypatch):
    arr = FakeArr("sonarr", [row(10, 101)])
    monkeypatch.setattr(failed_downloads, "client", lambda _service: arr)
    monkeypatch.setattr(failed_downloads, "_dry_run_for", lambda *_args: False)
    arr.has_file = True
    try:
        failed_downloads.retry("sonarr", 10)
    except ValueError as exc:
        assert "library file" in str(exc)
    else:
        assert False, "Expected an existing-file check"
    assert ("remove", 10) not in arr.calls

    arr.has_file = False
    arr.rows[0]["status"] = "downloading"
    try:
        failed_downloads.retry("sonarr", 10)
    except ValueError as exc:
        assert "no longer waiting" in str(exc)
    else:
        assert False, "Expected a changed-queue check"
    assert ("remove", 10) not in arr.calls


def test_radarr_searches_movie_after_removal(app, monkeypatch):
    arr = FakeArr("radarr", [row(20, 201, service="radarr")])
    monkeypatch.setattr(failed_downloads, "client", lambda _service: arr)
    monkeypatch.setattr(failed_downloads, "_dry_run_for", lambda *_args: False)

    failed_downloads.retry("radarr", 20)
    assert arr.calls == [("movie", 201), ("remove", 20), ("search_movie", 201)]


def test_problems_page_offers_manual_retry(app, client, monkeypatch):
    arr = FakeArr("sonarr", [row(10, 101)])
    monkeypatch.setattr(failed_downloads, "_dry_run_for", lambda *_args: False)
    monkeypatch.setattr(
        failed_downloads,
        "client",
        lambda service: arr if service == "sonarr" else FakeArr(service, []),
    )
    page = client.get("/chapters/problems").text
    assert "Downloads that did not import" in page
    assert "/chapters/problems/download/sonarr/10/retry" in page
    response = client.post(
        "/chapters/problems/download/sonarr/10/retry", follow_redirects=True
    )
    assert "Asked Sonarr to search" in response.text


def test_dry_run_keeps_download_files(monkeypatch):
    arr = FakeArr("sonarr", [row(10, 101)])
    monkeypatch.setattr(failed_downloads, "client", lambda _service: arr)
    monkeypatch.setattr(failed_downloads, "_dry_run_for", lambda *_args: True)
    try:
        failed_downloads.retry("sonarr", 10)
    except ValueError as exc:
        assert "Dry run" in str(exc)
    else:
        assert False, "Expected the dry-run check"
    assert ("remove", 10) not in arr.calls


def test_unknown_title_uses_global_dry_run(app):
    from curatarr.services import set_setting

    group = [row(10, 101)]
    assert failed_downloads._dry_run_for("sonarr", group)
    set_setting("global_policy", {"dry_run": False})
    assert not failed_downloads._dry_run_for("sonarr", group)


def test_arr_queue_removal_blocklists_without_automatic_redownload(monkeypatch):
    calls = []

    def request(self, method, path, **kwargs):
        calls.append((method, path, kwargs))

    monkeypatch.setattr(SonarrClient, "request", request)
    monkeypatch.setattr(RadarrClient, "request", request)
    for kind in (SonarrClient, RadarrClient):
        kind("http://localhost:1", "test-only").remove_failed_download(42)
    assert (
        calls
        == [
            (
                "DELETE",
                "/api/v3/queue/42",
                {
                    "params": {
                        "removeFromClient": "true",
                        "blocklist": "true",
                        "skipRedownload": "true",
                    },
                    "destructive": True,
                },
            )
        ]
        * 2
    )


def test_removal_is_in_history_and_a_failed_search_says_so(app, monkeypatch):
    from curatarr import db
    from curatarr.integrations import IntegrationError
    from curatarr.models import LifecycleAction

    arr = FakeArr("sonarr", [row(10, 101)])

    def broken(_ids):
        raise IntegrationError("down")

    arr.episode_search = broken
    monkeypatch.setattr(failed_downloads, "client", lambda _service: arr)
    monkeypatch.setattr(failed_downloads, "_dry_run_for", lambda *_args: False)
    with pytest.raises(IntegrationError, match="could not start"):
        failed_downloads.retry("sonarr", 10)
    kinds = {a.action_type: a for a in db.session.query(LifecycleAction).all()}
    assert "Example.Show.S01" in kinds["failed_download_removed"].reason_text
    assert kinds["failed_download_search_failed"].state == "FAILED"


def test_a_download_with_a_library_file_offers_no_button(app, monkeypatch):
    stuck = row(10, 101) | {"episode": {"hasFile": True}}
    arr = FakeArr("sonarr", [stuck])
    monkeypatch.setattr(failed_downloads, "_dry_run_for", lambda *_args: False)
    monkeypatch.setattr(
        failed_downloads,
        "client",
        lambda service: arr if service == "sonarr" else FakeArr(service, []),
    )
    found, _errors = failed_downloads.review()
    assert "already here" in found[0]["blocked_reason"]


def test_the_page_asks_quickly_and_the_button_works_in_place(app, client, monkeypatch):
    asked = []

    class Quick(FakeArr):
        def queue_records(self, quick=False):
            asked.append(quick)
            return list(self.rows)

    arr = Quick("sonarr", [row(10, 101)])
    monkeypatch.setattr(failed_downloads, "_dry_run_for", lambda *_args: False)
    monkeypatch.setattr(failed_downloads, "client", lambda service: arr)
    page = client.get("/chapters/problems").text
    assert asked and all(asked)
    assert "data-download-retry" in page and "download-retry.js" in page
    assert "delete its download files" in page
    answer = client.post(
        "/chapters/problems/download/sonarr/10/retry",
        headers={"X-Requested-With": "fetch"},
    ).get_json()
    assert answer["ok"] and "Asked Sonarr to search" in answer["message"]
    answer = client.post(
        "/chapters/problems/download/sonarr/10/retry",
        headers={"X-Requested-With": "fetch"},
    ).get_json()
    assert not answer["ok"] and "no longer waiting" in answer["message"]


def test_the_worker_counts_them_and_retries_only_when_automatic(app, monkeypatch):
    from curatarr import db
    from curatarr.services import set_setting

    sonarr, radarr = FakeArr("sonarr", [row(10, 101)]), FakeArr("radarr", [])
    monkeypatch.setattr(
        failed_downloads, "client", lambda s: sonarr if s == "sonarr" else radarr
    )
    monkeypatch.setattr(failed_downloads, "_dry_run_for", lambda *_args: False)

    def again():  # the next look, past the ten minutes between looks
        set_setting(failed_downloads.WAITING_KEY, {"count": failed_downloads.count()})
        db.session.commit()
        return failed_downloads.sweep()

    assert failed_downloads.sweep() == 0  # off: counted for Problems, not touched
    assert failed_downloads.count() == 1 and not sonarr.calls
    assert failed_downloads.sweep() == 0  # not looked at again so soon

    set_setting("auto_fixes", {"downloads": True})
    db.session.commit()
    for attempt in (1, 2, 3):
        sonarr.rows = [row(10 + attempt, 101)]  # the next release fails too
        retried = again()
        assert retried == (1 if attempt < 3 else 0), attempt
    # Twice in a week for the same episode, then it's left for a person.
    assert [c for c in sonarr.calls if c[0] == "remove"] == [
        ("remove", 11),
        ("remove", 12),
    ]
    assert failed_downloads.count() == 1


def test_stuck_downloads_count_on_problems(app, monkeypatch):
    from curatarr import db
    from curatarr.routes import _nav_counts
    from curatarr.services import set_setting

    with app.test_request_context("/chapters/problems"):
        before = _nav_counts(True, True)["problems"]
        set_setting(failed_downloads.WAITING_KEY, {"count": 2, "at": "2026-01-01"})
        db.session.commit()
        assert _nav_counts(True, True)["problems"] == before + 2
