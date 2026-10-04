from curatarr import db
from curatarr.models import Library, LibraryPolicy
from curatarr.services import resolved_policy, setting


def _library(policy=None):
    library = Library(jellyfin_library_id="rules", name="Rules TV", media_type="tv")
    db.session.add(library)
    db.session.flush()
    if policy is not None:
        db.session.add(LibraryPolicy(library_id=library.id, policy_json=policy))
    db.session.commit()
    return library


def test_rule_pages_render_for_global_and_library(app, client):
    library = _library()
    for section in ("acquisition", "retention"):
        for scope in ("global", library.id):
            page = client.get(f"/rules/{section}?scope={scope}")
            assert page.status_code == 200
    assert b"Keep one season ahead" in client.get("/rules/acquisition").data
    assert client.get("/rules/unknown").status_code == 404
    assert client.get("/rules/retention?scope=missing").status_code == 404


def test_global_rules_are_inherited_by_libraries(app, client):
    library = _library()
    client.post(
        "/rules/acquisition", data={"grace_days": "7", "manage_specials": "true"}
    )
    assert setting("global_policy") == {"grace_days": 7, "manage_specials": True}
    page = client.get(f"/rules/acquisition?scope={library.id}")
    assert b"Effective: 7 (global)" in page.data


def test_partial_page_keeps_other_library_fields(app, client):
    library = _library(
        {"quota_enabled": True, "high_water_bytes": 200, "low_water_bytes": 100}
    )
    client.post(
        f"/rules/acquisition?scope={library.id}",
        data={"minimum_mode": "season_1", "grace_days": ""},
    )
    db.session.refresh(library.policy)
    assert library.policy.policy_json == {
        "quota_enabled": True,
        "high_water_bytes": 200,
        "low_water_bytes": 100,
        "minimum_mode": "season_1",
    }
    client.post(f"/rules/acquisition?scope={library.id}", data={"minimum_mode": ""})
    db.session.refresh(library.policy)
    assert "minimum_mode" not in library.policy.policy_json


def test_global_scope_ignores_library_only_fields(app, client):
    _library()
    client.post(
        "/rules/retention",
        data={"dry_run": "false", "quota_enabled": "true", "notice_days": "3"},
    )
    assert setting("global_policy") == {"notice_days": 3}
    assert b'name="dry_run"' not in client.get("/rules/retention").data


def test_global_change_that_breaks_a_library_is_rejected(app, client):
    library = _library({"low_free_percent": 10})
    response = client.post(
        "/rules/retention", data={"critical_free_percent": "12"}, follow_redirects=True
    )
    assert b"Rules TV: Critical free-space threshold must be below low" in response.data
    assert setting("global_policy", {}) == {}
    db.session.refresh(library)
    from curatarr.models import MediaIdentity

    media = MediaIdentity(
        library_id=library.id, media_type="series", title="Show", jellyfin_id="s"
    )
    db.session.add(media)
    db.session.commit()
    assert resolved_policy(media)[0]["critical_free_percent"] == 8


def test_invalid_number_is_reported(app, client):
    response = client.post(
        "/rules/acquisition", data={"grace_days": "soon"}, follow_redirects=True
    )
    assert b"Invalid grace_days" in response.data
    assert setting("global_policy", {}) == {}
