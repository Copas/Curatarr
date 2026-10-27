"""Automatic fixes: try once without asking, drop it quietly if it doesn't work."""

from datetime import timedelta

from test_acquisition_reconcile import _show
from test_enrichment import _movie
from test_filecheck import PATH, check_env  # noqa: F401  (fixture)
from test_missing_keep import _Sonarr

from curatarr import autofix, better_copy, db, filecheck_jobs, missing_keep
from curatarr.models import ChapterState, LifecycleAction, utcnow
from curatarr.services import setting


def test_everything_is_on_by_default(app, monkeypatch):
    from curatarr import chapters

    monkeypatch.setattr(autofix, "ON_BY_DEFAULT", True)
    for option in autofix.ENHANCE_OPTION.values():
        monkeypatch.setitem(chapters.GLOBAL_DEFAULTS, option, True)
    assert all(autofix.enabled().values())


def test_each_can_be_switched_off(app, client):
    assert not any(autofix.enabled().values())
    page = client.get("/rules/automatic").text
    assert "Automatic fixes" in page and "checked" not in page
    client.post("/rules/automatic", data={"files": "on", "missing": "on"})
    assert autofix.enabled() == {
        "files": True,
        "dubs": False,
        "wrong_show": False,
        "missing": True,
        "misfiled": False,
        "default_audio": False,
        "seek": False,
        "favourites": False,
    }
    assert setting("chapter_options")["file_replace_automatically"] is True


def test_a_problem_file_is_replaced_without_asking_and_dropped_if_nothing_better(
    client,
    check_env,  # noqa: F811
):
    arr, _found = check_env
    _movie(PATH)
    filecheck_jobs.sweep()  # found while automatic replacement was off
    state = db.session.query(ChapterState).one()
    replace_url = f"/chapters/problems/{state.id}/replace"
    assert replace_url in client.get("/").text  # off: it asks
    client.post("/rules/automatic", data={"files": "on"})
    assert replace_url not in client.get("/").text  # on: not your job any more
    assert autofix.sweep() == 1
    assert better_copy.status(state, "file") == "pending"
    arr.available = [{"title": "Film.2020.Swedish", "rejections": ["Not English"]}]
    better_copy.sweep()
    assert filecheck_jobs.review()["to_act"] == []  # tried once, then dropped
    assert (
        db.session.query(LifecycleAction)
        .filter_by(action_type="auto_fix_gave_up")
        .count()
        == 1
    )
    assert autofix.sweep() == 0  # never tried again
    page = client.get("/").text
    assert "Handled in the last 7 days" in page and "1 couldn&#39;t be fixed" in page


def test_a_manual_replace_that_finds_nothing_still_asks(client, check_env):  # noqa: F811
    arr, _found = check_env
    _movie(PATH)
    filecheck_jobs.sweep()
    state = db.session.query(ChapterState).one()
    client.post(f"/chapters/problems/{state.id}/replace")
    arr.available = [{"title": "Film.2020.Swedish", "rejections": ["Not English"]}]
    better_copy.sweep()
    assert "No better copy was available" in client.get("/").text


def test_missing_episodes_are_searched_once_then_left(app, client, monkeypatch):
    sonarr = _Sonarr()
    monkeypatch.setattr("curatarr.lifecycle.client", lambda _kind: sonarr)
    media, _ = _show("Gappy", [(1, 1, False), (1, 2, True)], monitored=False)
    row_url = f"/chapters/problems/show/{media.id}/"
    assert row_url in client.get("/").text  # off: it asks
    client.post("/rules/automatic", data={"missing": "on"})
    assert row_url not in client.get("/").text
    assert autofix.sweep() == 1
    assert missing_keep.rows()[0]["state"] == "waiting"
    assert autofix.sweep() == 0  # not searched twice
    search = (
        db.session.query(LifecycleAction)
        .filter_by(media_identity_id=media.id, action_type="sonarr_season_search")
        .one()
    )
    search.created_at = utcnow() - timedelta(days=3)
    db.session.commit()
    autofix.sweep()
    assert missing_keep.rows() == []  # still missing: stopped trying
    assert row_url not in client.get("/").text
