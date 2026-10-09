from datetime import timedelta

import pytest

from curatarr import db
from curatarr.models import (
    AppSetting,
    Integration,
    LifecycleAction,
    PurgeCandidate,
    utcnow,
)


@pytest.fixture
def secured(app):
    app.config["ALLOW_UNAUTHENTICATED"] = False
    app.config["DEMO_MODE"] = True
    return app


def _sign_in(client, username="demo-admin", password="demo", **extra):
    return client.post(
        "/login" + extra.pop("query", ""),
        data={"username": username, "password": password, **extra},
    )


def test_signed_out_requests_are_redirected_or_rejected(secured, client):
    response = client.get("/review?x=1")
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login?next=")
    assert client.get("/api/v1/history").status_code == 401
    assert client.get("/api/v1/metrics").get_json() == {"error": "Sign-in required"}
    assert (
        client.post("/rules/acquisition", data={"grace_days": "1"}).status_code == 302
    )
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/status").status_code == 200
    assert client.get("/login").status_code == 200
    # The webhook keeps its own token check rather than a session.
    assert client.post("/api/v1/webhook/jellyfin", json={}).status_code == 403


def test_administrator_signs_in_and_out(secured, client):
    response = _sign_in(client, query="?next=/history")
    assert response.status_code == 303
    assert response.headers["Location"] == "/history"
    page = client.get("/")
    assert page.status_code == 200
    assert b"demo-admin" in page.data and b"Sign out" in page.data
    assert (
        db.session.query(LifecycleAction)
        .filter_by(action_type="admin_signed_in")
        .count()
        == 1
    )
    client.post("/logout")
    assert client.get("/").status_code == 302


@pytest.mark.parametrize(
    ("username", "password", "message"),
    [
        ("demo-viewer", "demo", b"Ask a Curatarr administrator"),
        ("demo-admin", "wrong", b"did not accept that username and password"),
    ],
)
def test_non_administrators_and_bad_passwords_are_refused(
    secured, client, username, password, message
):
    response = _sign_in(client, username, password)
    assert response.headers["Location"].startswith("/login")
    assert message in client.get("/login").data
    assert client.get("/").status_code == 302


def test_repeated_failures_are_rate_limited(secured, client):
    for _ in range(5):
        _sign_in(client, password="wrong")
    _sign_in(client)
    assert b"Too many failed sign-in attempts" in client.get("/login").data
    assert client.get("/").status_code == 302


def test_next_cannot_redirect_off_site(secured, client):
    for target in ("//evil.example/", "https://evil.example/", "/\\evil.example"):
        response = _sign_in(client, query=f"?next={target}")
        assert response.headers["Location"] == "/"


def test_first_run_binds_the_jellyfin_server(secured, client, monkeypatch):
    secured.config["DEMO_MODE"] = False
    calls = []

    class FakeJellyfin:
        def __init__(self, base_url, api_key):
            calls.append(("init", base_url, api_key))

        def authenticate(self, username, password, device_id):
            calls.append(("authenticate", username, device_id))
            return {
                "User": {
                    "Id": "u1",
                    "Name": "Owner",
                    "Policy": {"IsAdministrator": True},
                },
                "AccessToken": "user-token",
            }

        def end_session(self, token, device_id):
            calls.append(("end_session", token))

    monkeypatch.setattr("curatarr.auth.JellyfinClient", FakeJellyfin)
    assert b"Jellyfin server URL" in client.get("/login").data
    _sign_in(client, "owner", "pw")
    assert b"Enter your Jellyfin server URL" in client.get("/login").data
    _sign_in(client, "owner", "secret-pw", server_url="http://jellyfin.local:8096/")
    assert ("init", "http://jellyfin.local:8096", "") in calls
    assert ("end_session", "user-token") in calls
    row = db.session.query(Integration).filter_by(kind="jellyfin").one()
    assert row.base_url == "http://jellyfin.local:8096" and row.secret_ref is None
    assert b"Jellyfin server URL" not in client.get("/login").data
    stored = " ".join(str(r.value_json) for r in db.session.query(AppSetting))
    assert "secret-pw" not in stored and "user-token" not in stored
    with client.session_transaction() as session:
        assert session["user"]["id"] == "u1"
        assert "secret-pw" not in str(session) and "user-token" not in str(session)


def _age_session(client, minutes):
    with client.session_transaction() as session:
        session["user"]["verified_at"] = (
            utcnow() - timedelta(minutes=minutes)
        ).isoformat()


def test_demoted_administrator_loses_access(secured, client, monkeypatch):
    _sign_in(client)
    monkeypatch.setitem(
        __import__("curatarr.demo", fromlist=["DEMO_USERS"]).DEMO_USERS,
        "demo-admin",
        False,
    )
    assert client.get("/").status_code == 200  # Within the re-check interval.
    _age_session(client, 6)
    assert client.get("/").status_code == 302


def test_jellyfin_outage_allows_a_grace_period(secured, client):
    _sign_in(client)
    db.session.add(AppSetting(key="demo_outage", value_json="jellyfin"))
    db.session.commit()
    _age_session(client, 30)
    assert client.get("/").status_code == 200
    _age_session(client, 61)
    assert client.get("/").status_code == 302


def test_allowlisted_household_can_review_but_not_administer(secured, client):
    from curatarr.demo import seed_demo
    from curatarr.services import setting

    seed_demo()
    _sign_in(client)
    response = client.post(
        "/settings", data={"kind": "household", "user_id": "user-demo-viewer"}
    )
    assert response.status_code == 302
    assert setting("household_user_ids") == ["user-demo-viewer"]
    client.post("/logout")
    assert _sign_in(client, "demo-viewer").status_code == 303
    for path in ("/", "/review", "/titles", "/shows"):
        assert client.get(path).status_code == 200, path
    review = client.get("/review").text
    assert "Never purge" in review
    assert "Approve deletion" not in review
    assert "by Avery" not in review and "by Blake" not in review
    shows = client.get("/shows").text
    assert "Reset…" not in shows and "by Avery" not in shows
    assert "Reset all unwatched shows" not in shows
    assert client.get("/shows/reset-unwatched").status_code == 403
    for path in (
        "/settings",
        "/setup",
        "/rules/retention",
        "/history",
        "/operations",
        "/cleanup-preview",
        "/api/v1/history",
        "/api/v1/metrics",
    ):
        assert client.get(path).status_code == 403, path
    candidate = db.session.query(PurgeCandidate).filter_by(state="REVIEW").first()
    assert candidate is not None
    media_id = candidate.media_identity_id
    assert client.get(f"/titles/{media_id}").status_code == 200
    assert "Save overrides" not in client.get(f"/titles/{media_id}").text
    assert client.post(f"/titles/{media_id}", data={}).status_code == 403
    assert client.get(f"/titles/{media_id}/reset").status_code == 403
    assert client.post(f"/titles/{media_id}/request").status_code == 403
    assert (
        client.post(
            f"/review/{candidate.id}/delete", data={"confirm": "delete"}
        ).status_code
        == 403
    )
    assert client.post(f"/review/{candidate.id}/keep").status_code == 302


def test_household_allowlist_revocation_ends_session(secured, client):
    from curatarr.services import set_setting

    set_setting("household_user_ids", ["user-demo-viewer"])
    _sign_in(client, "demo-viewer")
    assert client.get("/").status_code == 200
    set_setting("household_user_ids", [])
    assert client.get("/").status_code == 302


def test_delete_requires_confirmation(secured, client):
    from curatarr.demo import seed_demo

    seed_demo()
    _sign_in(client)
    candidate = db.session.query(PurgeCandidate).filter_by(state="REVIEW").first()
    assert client.get(f"/review/{candidate.id}/delete").status_code == 200
    assert client.post(f"/review/{candidate.id}/delete").status_code == 400


def test_household_allowlist_rejects_unknown_ids(secured, client):
    from curatarr.services import setting

    _sign_in(client)
    client.post("/settings", data={"kind": "household", "user_id": "unknown"})
    assert setting("household_user_ids", []) == []


def test_theme_preference_persists_in_cookie(secured, client):
    _sign_in(client)
    response = client.post(
        "/preferences/theme", data={"theme": "light", "back": "/review"}
    )
    assert response.status_code == 303
    assert response.headers["Location"] == "/review"
    assert b'data-bs-theme="light"' in client.get("/login").data
    assert (
        client.post("/preferences/theme", data={"theme": "unknown"}).status_code == 400
    )
