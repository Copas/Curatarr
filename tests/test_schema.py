from curatarr import create_app


def test_unmigrated_app_returns_clear_error(tmp_path):
    app = create_app(
        {
            "TESTING": False,
            "ALLOW_UNAUTHENTICATED": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'fresh.db'}",
            "SECRET_KEY": "test-only",
        }
    )
    client = app.test_client()
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json["error"] == "Database migration required"
    response = client.post("/api/v1/webhook/jellyfin", json={})
    assert response.status_code == 503


def test_settings_from_a_newer_version_are_refused(tmp_path):
    from sqlalchemy import text

    from curatarr import db
    from curatarr.schema import POLICY_SCHEMA_VERSION

    app = create_app(
        {
            "TESTING": False,
            "ALLOW_UNAUTHENTICATED": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'newer.db'}",
            "SECRET_KEY": "test-only",
        }
    )
    with app.app_context():
        from flask_migrate import upgrade

        upgrade()
        assert app.test_client().get("/health").status_code == 200
        db.session.execute(
            text(
                "UPDATE app_settings SET value_json = :v "
                "WHERE key = 'policy_schema_version'"
            ),
            {"v": str(POLICY_SCHEMA_VERSION + 1)},
        )
        db.session.commit()
    response = app.test_client().get("/health")
    assert response.status_code == 503
    assert response.json["error"] == "Settings were saved by a newer Curatarr version"


def _app(tmp_path, name):
    return create_app(
        {
            "TESTING": False,
            "ALLOW_UNAUTHENTICATED": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / name}",
            "SECRET_KEY": "test-only",
        }
    )


def test_a_new_installation_starts_with_automatic_fixes_off(tmp_path):
    from flask_migrate import upgrade

    from curatarr import autofix
    from curatarr.enrichment import options

    with _app(tmp_path, "new.db").app_context():
        upgrade()
        assert not any(autofix.enabled().values())
        assert not options()["file_replace_automatically"]
        assert options()["file_check"]  # the checks still run and list what they find


def test_an_existing_installation_keeps_its_automatic_fixes(tmp_path):
    from flask_migrate import upgrade

    from curatarr import autofix, db
    from curatarr.enrichment import options
    from curatarr.models import Library
    from curatarr.services import set_setting

    with _app(tmp_path, "old.db").app_context():
        upgrade(revision="f3a9d2c7b815")
        db.session.add(
            Library(jellyfin_library_id="lib", name="Films", media_type="movies")
        )
        set_setting(
            "chapter_options", {"concurrency": 2, "dub_replace_automatically": False}
        )
        set_setting("auto_fixes", {"missing": False})
        db.session.commit()
        upgrade()
        db.session.expire_all()
        enabled = autofix.enabled()
        assert enabled["files"] and enabled["wrong_show"] and enabled["seek"]
        assert enabled["default_audio"] and enabled["misfiled"]
        assert not enabled["dubs"] and not enabled["missing"]  # chosen before: kept
        assert options()["concurrency"] == 2


def test_find_favorites_switched_on_where_it_was_keeps_on_under_automatic(tmp_path):
    from flask_migrate import upgrade

    from curatarr import autofix, db
    from curatarr.models import Library
    from curatarr.services import set_setting, setting

    with _app(tmp_path, "rc29.db").app_context():
        upgrade(revision="a6c4e2f8b931")
        db.session.add(
            Library(jellyfin_library_id="lib", name="Films", media_type="movies")
        )
        set_setting("curation", {"find_favourites": True, "seasonal": False})
        db.session.commit()
        upgrade()
        db.session.expire_all()
        assert autofix.automatic("favourites")
        assert setting("curation") == {"seasonal": False}


def test_the_old_default_whisper_model_gives_way_to_the_recommended_one(tmp_path):
    from flask_migrate import upgrade

    from curatarr import db
    from curatarr.chapters import DEFAULTS
    from curatarr.enrichment import config_for
    from curatarr.services import set_setting, setting

    for saved, device, kept in (
        ("medium", "auto", None),  # the old default, saved with the form
        ("medium", "cpu", "medium"),  # on the processor medium is quicker
        ("small", "auto", "small"),  # chosen
    ):
        with _app(tmp_path, f"{saved}-{device}.db").app_context():
            upgrade(revision="c4e7a9d2f618")
            set_setting("chapter_settings", {"semantic": {"model": saved}})
            set_setting("chapter_options", {"whisper_device": device})
            db.session.commit()
            upgrade()
            db.session.expire_all()
            assert setting("chapter_settings")["semantic"].get("model") == kept
            assert config_for("lib")["semantic"]["model"] == (
                kept or DEFAULTS["semantic"]["model"]
            )


def test_a_secret_key_that_doesnt_match_says_so(tmp_path):
    from flask_migrate import upgrade

    from curatarr import db
    from curatarr.services import set_setting

    path = tmp_path / "keys.db"
    first = create_app(
        {
            "TESTING": False,
            "ALLOW_UNAUTHENTICATED": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{path}",
            "SECRET_KEY": "the-first-key",
        }
    )
    with first.app_context():
        upgrade()
        set_setting("webhook_token", "a-token")
        db.session.commit()
    second = create_app(
        {
            "TESTING": False,
            "ALLOW_UNAUTHENTICATED": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{path}",
            "SECRET_KEY": "another-key",
        }
    )
    response = second.test_client().get("/")
    assert response.status_code == 503
    assert "secret key doesn't match" in response.json["error"]
    assert "CURATARR_SECRET_KEY" in response.json["action"]


def test_only_episodes_with_an_added_intro_are_checked_again_for_rule_7(tmp_path):
    from flask_migrate import upgrade

    from curatarr import db
    from curatarr.models import ChapterState, Library, MediaIdentity, MediaPart

    with _app(tmp_path, "old.db").app_context():
        upgrade(revision="a8c4e2f7b519")
        library = Library(jellyfin_library_id="tv", name="TV", media_type="tv")
        db.session.add(library)
        db.session.flush()
        show = MediaIdentity(library_id=library.id, media_type="series", title="Show")
        db.session.add(show)
        db.session.flush()
        hidden = (
            [{"id": "a", "type": "Introduction", "added": True, "start": 1, "end": 30}],
            [
                {
                    "id": "b",
                    "type": "Introduction",
                    "replaced_by": "c",
                    "start": 1,
                    "end": 9,
                }
            ],
            [{"id": "d", "type": "Credits", "added": True, "start": 1, "end": 9}],
            None,
        )
        ids = []
        for number, markers in enumerate(hidden, 1):
            part = MediaPart(
                media_identity_id=show.id,
                kind="episode",
                season_number=1,
                episode_number=number,
                file_path=f"/mnt/media/Show/S01E0{number}.mkv",
            )
            db.session.add(part)
            db.session.flush()
            state = ChapterState(
                media_part_id=part.id,
                media_identity_id=show.id,
                intro_rule=6,
                markers_hidden=markers,
            )
            db.session.add(state)
            db.session.flush()
            ids.append(state.id)
        db.session.commit()
        upgrade(revision="b6d3f8a1c427")  # rule 7's own
        db.session.expire_all()
        rules = [db.session.get(ChapterState, i).intro_rule for i in ids]
        assert rules == [6, 7, 7, 7]  # only the one with an added intro waits


def test_only_episodes_with_an_intro_are_checked_again_for_rule_8(tmp_path):
    from flask_migrate import upgrade

    from curatarr import db
    from curatarr.models import ChapterState, Library, MediaIdentity, MediaPart

    with _app(tmp_path, "old.db").app_context():
        upgrade(revision="b6d3f8a1c427")
        library = Library(jellyfin_library_id="tv", name="TV", media_type="tv")
        db.session.add(library)
        db.session.flush()
        show = MediaIdentity(library_id=library.id, media_type="series", title="Show")
        db.session.add(show)
        db.session.flush()
        cases = (
            ({"ours": [0, 25], "theirs": [[0, 24]]}, None),  # Intro Skipper's
            (
                {"ours": [0, 25], "theirs": []},
                [{"type": "Introduction", "added": True}],
            ),
            ({"ours": None, "theirs": []}, None),  # none: can't run on
            (None, [{"type": "Credits", "added": True}]),  # credits only
        )
        ids = []
        for number, (found, hidden) in enumerate(cases, 1):
            part = MediaPart(
                media_identity_id=show.id,
                kind="episode",
                season_number=1,
                episode_number=number,
                file_path=f"/mnt/media/Show/S01E0{number}.mkv",
            )
            db.session.add(part)
            db.session.flush()
            state = ChapterState(
                media_part_id=part.id,
                media_identity_id=show.id,
                intro_rule=7,
                intro_found=found,
                markers_hidden=hidden,
            )
            db.session.add(state)
            db.session.flush()
            ids.append(state.id)
        db.session.commit()
        upgrade(revision="c8e2a6d4f913")  # rule 8's own
        db.session.expire_all()
        rules = [db.session.get(ChapterState, i).intro_rule for i in ids]
        assert rules == [7, 7, 8, 8]  # only those with an intro wait


def test_rule_9_checks_again_what_rule_8_ran_on(tmp_path):
    from flask_migrate import upgrade

    from curatarr import db
    from curatarr.models import ChapterState, Library, MediaIdentity, MediaPart

    with _app(tmp_path, "old.db").app_context():
        upgrade(revision="c8e2a6d4f913")
        library = Library(jellyfin_library_id="tv", name="TV", media_type="tv")
        db.session.add(library)
        db.session.flush()
        show = MediaIdentity(library_id=library.id, media_type="series", title="Show")
        db.session.add(show)
        db.session.flush()
        cases = (
            (8, {"theirs": [[0, 40]]}),  # run on under rule 8: check again
            (7, {"theirs": [[0, 24]]}),  # not reached yet: check
            (8, {"ours": None, "theirs": []}),  # no intro: nothing to redo
        )
        ids = []
        for number, (rule, found) in enumerate(cases, 1):
            part = MediaPart(
                media_identity_id=show.id,
                kind="episode",
                season_number=1,
                episode_number=number,
                file_path=f"/mnt/media/Show/S01E0{number}.mkv",
            )
            db.session.add(part)
            db.session.flush()
            state = ChapterState(
                media_part_id=part.id,
                media_identity_id=show.id,
                intro_rule=rule,
                intro_found=found,
            )
            db.session.add(state)
            db.session.flush()
            ids.append(state.id)
        db.session.commit()
        upgrade(revision="d4a9e1c7b235")  # rule 9's own
        db.session.expire_all()
        rules = [db.session.get(ChapterState, i).intro_rule for i in ids]
        assert rules == [8, 7, 9]


def test_rule_10_checks_again_episodes_with_an_intro(tmp_path):
    from flask_migrate import upgrade

    from curatarr import db
    from curatarr.models import ChapterState, Library, MediaIdentity, MediaPart

    with _app(tmp_path, "old.db").app_context():
        upgrade(revision="d4a9e1c7b235")
        library = Library(jellyfin_library_id="tv", name="TV", media_type="tv")
        db.session.add(library)
        db.session.flush()
        show = MediaIdentity(library_id=library.id, media_type="series", title="Show")
        db.session.add(show)
        db.session.flush()
        ids = []
        for number, found in enumerate(
            ({"theirs": [[0, 40]]}, {"ours": None, "theirs": []}), 1
        ):
            part = MediaPart(
                media_identity_id=show.id,
                kind="episode",
                season_number=1,
                episode_number=number,
                file_path=f"/mnt/media/Show/S01E0{number}.mkv",
            )
            db.session.add(part)
            db.session.flush()
            state = ChapterState(
                media_part_id=part.id,
                media_identity_id=show.id,
                intro_rule=9,
                intro_found=found,
            )
            db.session.add(state)
            db.session.flush()
            ids.append(state.id)
        db.session.commit()
        upgrade(revision="e7b2c4f9a618")  # rule 10's own
        db.session.expire_all()
        assert [db.session.get(ChapterState, i).intro_rule for i in ids] == [9, 10]


def test_rule_11_checks_again_episodes_with_an_intro(tmp_path):
    from flask_migrate import upgrade

    from curatarr import db
    from curatarr.models import ChapterState, Library, MediaIdentity, MediaPart

    with _app(tmp_path, "old.db").app_context():
        upgrade(revision="a1c5e9f2b704")
        library = Library(jellyfin_library_id="tv", name="TV", media_type="tv")
        db.session.add(library)
        db.session.flush()
        show = MediaIdentity(library_id=library.id, media_type="series", title="Show")
        db.session.add(show)
        db.session.flush()
        ids = []
        for number, found in enumerate(
            ({"theirs": [[0, 40]]}, {"ours": None, "theirs": []}), 1
        ):
            part = MediaPart(
                media_identity_id=show.id,
                kind="episode",
                season_number=1,
                episode_number=number,
                file_path=f"/mnt/media/Show/S01E0{number}.mkv",
            )
            db.session.add(part)
            db.session.flush()
            state = ChapterState(
                media_part_id=part.id,
                media_identity_id=show.id,
                intro_rule=10,
                intro_found=found,
            )
            db.session.add(state)
            db.session.flush()
            ids.append(state.id)
        db.session.commit()
        upgrade()
        db.session.expire_all()
        assert [db.session.get(ChapterState, i).intro_rule for i in ids] == [10, 11]
