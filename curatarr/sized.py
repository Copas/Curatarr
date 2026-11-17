"""Smaller copies of Curatarr's own pictures, for pages that show them small.

A custom poster is kept at up to 2000 × 3000, a background or wide tile at
up to 3840 × 2160, a generated picture at the image tool's size: around a
megabyte or more each, while a gallery tile shows one a few hundred pixels
wide. A page asks for ``?w=`` one of WIDTHS (with srcset, so the browser
picks the one that fits the tile and the screen), and gets a WebP copy that
size, made once and kept on disk next to the database (keyed by the
picture's id and its file's modification time, so a changed file gets a new
copy). Transparency is kept (logos). Without ``?w=`` the picture is sent as
it is: the full-screen viewer, downloads and Jellyfin use that.
"""

import os
import tempfile
from pathlib import Path

from flask import abort, current_app, request, send_file
from markupsafe import Markup, escape

WIDTHS = (240, 480, 960, 1440)
QUALITY = 80  # WebP: no visible loss at these sizes, about 1/40 of the original
KEPT_FOR = 3600  # s, as the originals


def folder() -> Path:
    path = Path(current_app.instance_path) / "sized"
    path.mkdir(parents=True, exist_ok=True)
    return path


def copy(source: str, key: str, width: int) -> Path:
    """The WebP copy of ``source`` at most ``width`` wide, made if needed."""
    from PIL import Image, ImageOps

    stamp = os.stat(source).st_mtime_ns
    target = folder() / f"{key}-{width}-{stamp}.webp"
    if target.exists():
        return target
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image)
        clear = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
        image = image.convert("RGBA" if clear else "RGB")
        image.thumbnail((width, width * 4), Image.LANCZOS)  # never larger
        with tempfile.NamedTemporaryFile(
            dir=target.parent, suffix=".part", delete=False
        ) as out:
            image.save(out, "WEBP", quality=QUALITY, method=4)
    os.replace(out.name, target)
    # The copy of an older version of the file, if there was one, goes.
    for old in target.parent.glob(f"{key}-{width}-*.webp"):
        if old != target:
            old.unlink(missing_ok=True)
    return target


def send(path: str, key: str, mimetype: str | None = None):
    """The picture at ``path``: as it is, or the ``?w=`` copy."""
    width = request.args.get("w", type=int)
    if width is None:
        return send_file(path, mimetype=mimetype, max_age=KEPT_FOR)
    if width not in WIDTHS:
        abort(404)
    try:
        return send_file(
            copy(path, key, width), mimetype="image/webp", max_age=KEPT_FOR
        )
    except OSError:  # not a picture Pillow reads: the original will do
        return send_file(path, mimetype=mimetype, max_age=KEPT_FOR)


def attrs(url: str, sizes: str, width: int = 480) -> Markup:
    """src, srcset and sizes for an <img> of the picture at ``url``: the
    browser picks the smallest copy that fills ``sizes`` (a CSS width, or
    media conditions with widths) at the screen's pixel density."""
    joiner = "&" if "?" in url else "?"
    srcset = ", ".join(f"{url}{joiner}w={w} {w}w" for w in WIDTHS)
    return Markup(
        f'src="{escape(url)}{joiner}w={width}" srcset="{escape(srcset)}" sizes="{escape(sizes)}"'
    )
