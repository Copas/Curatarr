from io import BytesIO

from PIL import Image

from curatarr import db
from curatarr.artwork import _jpeg, apply_badge, badge_poster, restore_badge
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
        assert restore_badge(snapshot)
        assert fake.current == _jpeg(source)
