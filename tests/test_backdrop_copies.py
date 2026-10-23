"""Jellyfin adds an uploaded backdrop to a title's backdrops instead of
replacing the first one: every redraw of the Leaving Soon tag used to leave
the old one behind."""

from datetime import timedelta
from hashlib import sha256
from io import BytesIO

from PIL import Image, ImageDraw

from curatarr import db
from curatarr.artwork import (
    _delete_tagged,
    _jpeg,
    _tidy_existing_backdrops,
    apply_badge,
    badge_poster,
    restore_badge,
)
from curatarr.models import (
    Library,
    MediaIdentity,
    PosterSnapshot,
    PurgeCandidate,
    utcnow,
)


def _picture(colour, stripes):
    image = Image.new("RGB", (1280, 720), colour)
    drawing = ImageDraw.Draw(image)
    for x in range(0, 1280, 1280 // stripes):
        drawing.rectangle((x, 0, x + 40, 720), fill="#f0f0f0")
    output = BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


class AppendingJellyfin:
    """Backdrops as Jellyfin keeps them: an upload is added to the end."""

    def __init__(self, backdrops):
        self.backdrops = list(backdrops)

    def item(self, _item_id):
        return {"ImageTags": {}, "BackdropImageTags": [self._tag(0)]}

    def _tag(self, index):
        return sha256(self.backdrops[index]).hexdigest()

    def image_list(self, _item_id):
        return [
            {"ImageType": "Backdrop", "ImageIndex": i, "ImageTag": self._tag(i)}
            for i in range(len(self.backdrops))
        ]

    def image(self, _item_id, _kind="Backdrop"):
        return self.backdrops[0]

    def image_at(self, _item_id, _kind, index, max_width=None):
        image = Image.open(BytesIO(self.backdrops[index]))
        if max_width:
            image = image.resize(
                (max_width, round(image.height * max_width / image.width))
            )
        output = BytesIO()
        image.save(output, format="JPEG")
        return output.getvalue()

    def put_image(self, _item_id, data, _kind="Backdrop"):
        self.backdrops.append(data)

    def delete_image(self, _item_id, _kind, index):
        del self.backdrops[index]

    def move_image(self, _item_id, _kind, index, new_index):
        self.backdrops.insert(new_index, self.backdrops.pop(index))


def test_one_tagged_backdrop_however_often_it_is_redrawn(app, monkeypatch):
    original = _jpeg(_picture("#335544", 8))
    other = _picture("#aa2200", 3)  # another of the title's backdrops
    fake = AppendingJellyfin([original, other])
    monkeypatch.setattr("curatarr.artwork.client", lambda _kind: fake)
    with app.app_context():
        library = Library(jellyfin_library_id="bd", name="Films", media_type="movies")
        db.session.add(library)
        db.session.flush()
        media = MediaIdentity(
            library_id=library.id, media_type="movie", title="Film", jellyfin_id="b"
        )
        db.session.add(media)
        db.session.flush()
        candidate = PurgeCandidate(
            media_identity_id=media.id,
            state="LEAVING_SOON",
            reason_code="requested",
            reason_text="Asked",
            reclaimable_bytes=100,
            scheduled_delete_at=utcnow() + timedelta(days=9, hours=1),
        )
        db.session.add(candidate)
        db.session.commit()
        assert apply_badge(candidate)
        saved = _jpeg(original)  # the original as Curatarr keeps it
        assert len(fake.backdrops) == 2
        nine = fake.backdrops[0]
        assert nine == badge_poster(saved, "9 DAYS LEFT", "Backdrop")
        # A day later: redrawn, and still one tagged copy, the new one, first.
        candidate.scheduled_delete_at -= timedelta(days=1)
        db.session.commit()
        assert apply_badge(candidate)
        assert fake.backdrops == [
            badge_poster(saved, "8 DAYS LEFT", "Backdrop"),
            other,
        ]
        # Restored: the original is back, with no tagged copy left.
        snapshot = db.session.query(PosterSnapshot).one()
        assert restore_badge(snapshot)
        assert fake.backdrops == [saved, other]

        # Copies left by earlier versions, on a title restored since.
        fake.backdrops = [original, nine, other, nine, nine]
        assert _delete_tagged(fake, "b", original) == 3
        assert fake.backdrops == [original, other]

        # The one-off pass over titles badged before this was fixed.
        fake.backdrops = [saved, nine, other, nine]
        _tidy_existing_backdrops()
        assert fake.backdrops == [saved, other]
        fake.backdrops = [saved, nine]
        _tidy_existing_backdrops()  # only once
        assert fake.backdrops == [saved, nine]
