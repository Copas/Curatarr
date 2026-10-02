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
