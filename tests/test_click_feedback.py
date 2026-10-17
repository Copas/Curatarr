"""Buttons that say what they started, instead of looking unclicked."""

from test_acquisition_reconcile import _show

from curatarr import db, enrichment
from curatarr.models import LifecycleAction, MediaPart
from curatarr.services import set_setting


def _searched(media, episode_ids, state="SUCCEEDED"):
    db.session.add(
        LifecycleAction(
            idempotency_key=f"search:{media.id}:{episode_ids}",
            action_type="sonarr_season_search",
            state=state,
            media_identity_id=media.id,
            reason_text="Requested",
            payload_json={"episode_ids": episode_ids, "search_now": True},
        )
    )
    db.session.commit()


def test_request_now_shows_it_was_requested_until_the_episodes_arrive(app, client):
    media, _ = _show("Waiting", [(1, 1, True), (1, 2, False), (1, 3, False)])
    page = client.get(f"/titles/{media.id}").text
    assert "Request now</button>" in page
    _searched(media, [1002, 1003])
    page = client.get(f"/titles/{media.id}").text
    assert "Request now</button>" not in page
    assert "waiting for 2 episodes" in page
    for part in db.session.query(MediaPart).filter_by(media_identity_id=media.id):
        part.has_file = True
    db.session.commit()
    assert "Request now</button>" in client.get(f"/titles/{media.id}").text


def test_a_failed_request_offers_the_button_again(app, client):
    media, _ = _show("Failed", [(1, 1, False)])
    _searched(media, [1001], state="FAILED_FINAL")
    assert "Request now</button>" in client.get(f"/titles/{media.id}").text


def test_enhance_buttons_show_work_asked_for_but_not_started(app, client):
    page = client.get("/chapters").text
    assert "Queuing skip-marker checks" not in page
    set_setting("skip_markers_requested", {"library_id": None})
    set_setting("chapter_scan_requested", {"library_id": None})
    db.session.commit()
    page = client.get("/chapters").text
    assert "Queuing skip-marker checks" in page
    assert "Finding your files; the scan starts after that" in page
    assert "Scan every library</button>" not in page


def test_wrong_episodes_reach_overview_and_get_a_tab(app, client, monkeypatch):
    monkeypatch.setattr(enrichment, "identity_check_enabled", lambda: True)
    _, parts = _show("Swapped", [(1, 1, True), (1, 2, True)])
    part = parts[(1, 1)]
    part.file_path = "/tv/Swapped/S01E01.mkv"
    state = enrichment.state_for(part)
    state.identity_issue = {
        "kind": "episode",
        "file": part.file_path,
        "season": 1,
        "filed_as": 1,
        "fits": 2,
        "margin": 4.0,
    }
    db.session.commit()
    page = client.get("/").text
    assert "Filed as E01, but sounds like E02" in page
    assert f"/chapters/identity/{state.id}/dismiss" in page
    assert ">Wrong episodes" in client.get("/chapters").text
    response = client.post(
        f"/chapters/identity/{state.id}/dismiss", data={"back": "overview"}
    )
    assert response.headers["Location"].endswith("/")
    assert "Filed as E01" not in client.get("/").text


def test_token_buttons_ask_before_breaking_what_uses_them(app, client):
    client.post("/settings", data={"kind": "api_key"})
    page = client.get("/settings").text
    assert "Revoke…</span>" in page and "Revoke the key</button>" in page
    assert "Make a new key</button>" in page
