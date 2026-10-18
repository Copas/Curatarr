from hashlib import sha256
from io import BytesIO
from pathlib import Path

from PIL import Image

from curatarr import db
from curatarr.artwork import _jpeg, apply_badge, badge_poster, restore_badge
from curatarr.integrations import IntegrationError
from curatarr.models import Library, MediaIdentity, PosterSnapshot, PurgeCandidate


def _poster():
    output = BytesIO()
    Image.new("RGB", (300, 450), "#334455").save(output, format="JPEG")
    return output.getvalue()


def test_badge_and_restore(app, monkeypatch):
    source = _poster()
    assert badge_poster(source) != source

    class FakeJellyfin:
        current = source

        def item(self, _item_id):
            return {"ImageTags": {"Primary": sha256(self.current).hexdigest()}}

        def image(self, _item_id):
            return self.current

        def put_image(self, _item_id, data):
            self.current = data

    fake = FakeJellyfin()
    monkeypatch.setattr("curatarr.artwork.client", lambda _kind: fake)
    with app.app_context():
        library = Library(jellyfin_library_id="art-tv", name="Demo TV", media_type="tv")
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id,
            media_type="series",
            title="Sample Series",
            jellyfin_id="series-a",
        )
        db.session.add(media)
        db.session.flush()
        candidate = PurgeCandidate(
            media_identity_id=media.id,
            state="REVIEW",
            reason_code="inactivity",
            reason_text="Old",
            reclaimable_bytes=100,
        )
        db.session.add(candidate)
        db.session.commit()
        assert apply_badge(candidate)
        badged = fake.current
        assert apply_badge(candidate)
        assert fake.current == badged
        snapshot = db.session.query(PosterSnapshot).one()
        assert snapshot.original_image_tag
        assert snapshot.badged_image_tag
        assert restore_badge(snapshot)
        assert fake.current == _jpeg(source)


def test_restore_uses_image_tag_when_jellyfin_reencodes_badge(app, monkeypatch):
    source = _poster()

    class ReencodingJellyfin:
        current = source
        tag = "original-tag"
        uploads = 0

        def item(self, _item_id):
            return {"ImageTags": {"Primary": self.tag}}

        def image(self, _item_id):
            return self.current

        def put_image(self, _item_id, data):
            self.uploads += 1
            self.tag = f"upload-{self.uploads}"
            output = BytesIO()
            Image.open(BytesIO(data)).save(output, format="JPEG", quality=70)
            self.current = output.getvalue()

    fake = ReencodingJellyfin()
    monkeypatch.setattr("curatarr.artwork.client", lambda _kind: fake)
    with app.app_context():
        library = Library(jellyfin_library_id="tag-tv", name="TV", media_type="tv")
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id,
            media_type="series",
            title="Series",
            jellyfin_id="series-tag",
        )
        db.session.add(media)
        db.session.flush()
        candidate = PurgeCandidate(
            media_identity_id=media.id,
            state="LEAVING_SOON",
            reason_code="inactivity",
            reason_text="Old",
            reclaimable_bytes=100,
        )
        db.session.add(candidate)
        db.session.commit()
        assert apply_badge(candidate)
        snapshot = db.session.query(PosterSnapshot).one()
        assert snapshot.badged_image_tag == "upload-1"
        assert fake.current != Path(snapshot.badged_image_bytes_path).read_bytes()
        assert restore_badge(snapshot)
        assert fake.uploads == 2
        assert not snapshot.active


def test_restore_preserves_artwork_changed_by_someone_else(app, monkeypatch):
    source = _poster()

    class FakeJellyfin:
        current = source
        tag = "original"
        uploads = 0

        def item(self, _item_id):
            return {"ImageTags": {"Primary": self.tag}}

        def image(self, _item_id):
            return self.current

        def put_image(self, _item_id, data):
            self.uploads += 1
            self.current = data
            self.tag = f"upload-{self.uploads}"

    fake = FakeJellyfin()
    monkeypatch.setattr("curatarr.artwork.client", lambda _kind: fake)
    with app.app_context():
        library = Library(jellyfin_library_id="edited-tv", name="TV", media_type="tv")
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id,
            media_type="series",
            title="Series",
            jellyfin_id="edited-series",
        )
        db.session.add(media)
        db.session.flush()
        candidate = PurgeCandidate(
            media_identity_id=media.id,
            state="REVIEW",
            reason_code="inactivity",
            reason_text="Old",
            reclaimable_bytes=100,
        )
        db.session.add(candidate)
        db.session.commit()
        assert apply_badge(candidate)
        snapshot = db.session.query(PosterSnapshot).one()
        fake.current = Image.new("RGB", (300, 450), "#aabbcc")
        output = BytesIO()
        fake.current.save(output, format="JPEG")
        fake.current = output.getvalue()
        fake.tag = "external-edit"
        assert not restore_badge(snapshot)
        assert fake.uploads == 1
        assert snapshot.active


def test_ambiguous_badge_upload_keeps_recoverable_snapshot(app, monkeypatch):
    source = _poster()

    class FakeJellyfin:
        current = source
        fail_once = True

        def item(self, _item_id):
            return {"ImageTags": {"Primary": sha256(self.current).hexdigest()}}

        def image(self, _item_id):
            return self.current

        def put_image(self, _item_id, data):
            self.current = data
            if self.fail_once:
                self.fail_once = False
                raise IntegrationError("response lost")

    fake = FakeJellyfin()
    monkeypatch.setattr("curatarr.artwork.client", lambda _kind: fake)
    with app.app_context():
        library = Library(jellyfin_library_id="ambiguous", name="TV", media_type="tv")
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id,
            media_type="series",
            title="Series",
            jellyfin_id="ambiguous-series",
        )
        db.session.add(media)
        db.session.flush()
        candidate = PurgeCandidate(
            media_identity_id=media.id,
            state="REVIEW",
            reason_code="inactivity",
            reason_text="Old",
            reclaimable_bytes=100,
        )
        db.session.add(candidate)
        db.session.commit()
        assert not apply_badge(candidate)
        snapshot = db.session.query(PosterSnapshot).one()
        assert snapshot.active
        assert Path(snapshot.original_image_bytes_path).is_file()
        assert apply_badge(candidate)
        assert snapshot.badged_image_tag
        assert restore_badge(snapshot)
        assert not snapshot.active


def test_dry_run_libraries_still_get_leaving_soon_badges(app, monkeypatch):
    """Owner's decision: dry run rehearses everything visible, badges included."""
    from curatarr.artwork import reconcile_artwork
    from curatarr.models import LibraryPolicy

    source = _poster()
    uploads = []

    class FakeJellyfin:
        current = source

        def item(self, _item_id):
            return {"ImageTags": {"Primary": sha256(self.current).hexdigest()}}

        def image(self, _item_id):
            return self.current

        def put_image(self, _item_id, data):
            uploads.append(data)
            self.current = data

    monkeypatch.setattr("curatarr.artwork.client", lambda _kind: FakeJellyfin())
    with app.app_context():
        library = Library(jellyfin_library_id="dry-tv", name="TV", media_type="tv")
        db.session.add(library)
        db.session.flush()
        db.session.add(
            LibraryPolicy(library_id=library.id, policy_json={"dry_run": True})
        )
        media = MediaIdentity(
            library_id=library.id,
            media_type="series",
            title="Rehearsed",
            jellyfin_id="dry-series",
        )
        db.session.add(media)
        db.session.flush()
        db.session.add(
            PurgeCandidate(
                media_identity_id=media.id,
                state="LEAVING_SOON",
                reason_code="disk_pressure",
                reason_text="Low space",
                reclaimable_bytes=100,
            )
        )
        db.session.commit()
        reconcile_artwork()
        assert len(uploads) == 1
        assert db.session.query(PosterSnapshot).one().active


def test_a_badge_being_applied_elsewhere_isnt_applied_twice(app, monkeypatch):
    """The web app and the worker reconcile artwork at the same moment."""
    from curatarr import leases

    source = _poster()

    class FakeJellyfin:
        current = source
        puts = 0

        def item(self, _item_id):
            return {"ImageTags": {"Primary": sha256(self.current).hexdigest()}}

        def image(self, _item_id):
            return self.current

        def put_image(self, _item_id, data):
            self.puts += 1
            self.current = data

    fake = FakeJellyfin()
    monkeypatch.setattr("curatarr.artwork.client", lambda _kind: fake)
    with app.app_context():
        library = Library(
            jellyfin_library_id="art-m", name="Films", media_type="movies"
        )
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id, media_type="movie", title="Film", jellyfin_id="f"
        )
        db.session.add(media)
        db.session.flush()
        candidate = PurgeCandidate(
            media_identity_id=media.id,
            state="LEAVING_SOON",
            reason_code="requested",
            reason_text="Asked",
            reclaimable_bytes=100,
        )
        db.session.add(candidate)
        db.session.commit()
        other = leases.acquire(f"artwork:{candidate.id}", 120)  # the other process
        assert not apply_badge(candidate)
        assert db.session.query(PosterSnapshot).count() == 0 and fake.puts == 0
        leases.release(f"artwork:{candidate.id}", other)
        assert apply_badge(candidate) and apply_badge(candidate)
        assert db.session.query(PosterSnapshot).count() == 1 and fake.puts == 1


def test_a_replaced_poster_is_badged_again_and_restores_to_the_new_art(
    app, monkeypatch
):
    """A Jellyfin metadata refresh that replaces images drops the banner."""
    first, fresh = _poster(), None
    output = BytesIO()
    Image.new("RGB", (300, 450), "#aa2233").save(output, format="JPEG")
    fresh = output.getvalue()

    class FakeJellyfin:
        current = first

        def item(self, _item_id):
            return {"ImageTags": {"Primary": sha256(self.current).hexdigest()}}

        def image(self, _item_id):
            return self.current

        def put_image(self, _item_id, data):
            self.current = data

    fake = FakeJellyfin()
    monkeypatch.setattr("curatarr.artwork.client", lambda _kind: fake)
    with app.app_context():
        library = Library(
            jellyfin_library_id="art-r", name="Films", media_type="movies"
        )
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id, media_type="movie", title="Film", jellyfin_id="f"
        )
        db.session.add(media)
        db.session.flush()
        candidate = PurgeCandidate(
            media_identity_id=media.id,
            state="LEAVING_SOON",
            reason_code="requested",
            reason_text="Asked",
            reclaimable_bytes=100,
        )
        db.session.add(candidate)
        db.session.commit()
        assert apply_badge(candidate)
        badged_first = fake.current
        assert apply_badge(candidate) and fake.current == badged_first  # unchanged
        fake.current = fresh  # Jellyfin downloaded new artwork
        assert apply_badge(candidate)
        assert fake.current == badge_poster(_jpeg(fresh))
        snapshot = db.session.query(PosterSnapshot).one()
        assert snapshot.badged_image_tag == sha256(fake.current).hexdigest()
        assert restore_badge(snapshot)
        assert fake.current == _jpeg(fresh)


def test_the_banner_counts_down_the_days(app, monkeypatch):
    from datetime import date, datetime, timedelta

    from curatarr import artwork
    from curatarr.artwork import badge_label

    due = datetime(2026, 11, 1, 20, 0).astimezone()
    candidate = PurgeCandidate(scheduled_delete_at=due)
    assert badge_label(candidate, date(2026, 10, 18)) == "14 DAYS LEFT"
    assert badge_label(candidate, date(2026, 10, 31)) == "1 DAY LEFT"
    assert badge_label(candidate, date(2026, 11, 1)) == "LAST DAY"
    assert badge_label(PurgeCandidate()) == "LEAVING SOON"

    source = _poster()

    class FakeJellyfin:
        current = source
        puts = 0

        def item(self, _item_id):
            return {"ImageTags": {"Primary": sha256(self.current).hexdigest()}}

        def image(self, _item_id):
            return self.current

        def put_image(self, _item_id, data):
            self.puts += 1
            self.current = data

    fake = FakeJellyfin()
    monkeypatch.setattr("curatarr.artwork.client", lambda _kind: fake)
    today = {"day": date(2026, 10, 18)}
    real = artwork.badge_label
    monkeypatch.setattr(
        artwork, "badge_label", lambda c, t=None: real(c, t or today["day"])
    )
    with app.app_context():
        library = Library(
            jellyfin_library_id="art-d", name="Films", media_type="movies"
        )
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id, media_type="movie", title="Film", jellyfin_id="f"
        )
        db.session.add(media)
        db.session.flush()
        candidate = PurgeCandidate(
            media_identity_id=media.id,
            state="LEAVING_SOON",
            reason_code="requested",
            reason_text="Asked",
            reclaimable_bytes=100,
            scheduled_delete_at=due,
        )
        db.session.add(candidate)
        db.session.commit()
        assert apply_badge(candidate)
        assert fake.current == badge_poster(_jpeg(source), "14 DAYS LEFT")
        assert apply_badge(candidate) and fake.puts == 1  # same day: left alone
        today["day"] += timedelta(days=1)
        assert apply_badge(candidate) and fake.puts == 2
        assert fake.current == badge_poster(_jpeg(source), "13 DAYS LEFT")
        snapshot = db.session.query(PosterSnapshot).one()
        assert snapshot.badge_label == "red-tag:13 DAYS LEFT"
        assert restore_badge(snapshot)
        assert fake.current == _jpeg(source)
