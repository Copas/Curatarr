"""What's done to a picture after it's made: cropped to its type's shape,
and a logo's plain background keyed out to real transparency."""

from io import BytesIO

from PIL import Image, ImageChops, ImageFilter

# A logo is made on a plain background, then the background is taken out:
# the image models can't make transparent pictures themselves.
EDGE = 8  # the border, in pixels, the background colour is read from


def load(data: bytes) -> Image.Image:
    image = Image.open(BytesIO(data))
    image.load()
    return image


def png(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def crop_to(image: Image.Image, aspect: float | None) -> Image.Image:
    """The middle of ``image`` at ``aspect`` (width / height)."""
    if not aspect:
        return image
    width, height = image.size
    if abs(width / height - aspect) / aspect < 0.005:
        return image
    if width / height > aspect:
        new = round(height * aspect)
        left = (width - new) // 2
        return image.crop((left, 0, left + new, height))
    new = round(width / aspect)
    top = (height - new) // 2
    return image.crop((0, top, width, top + new))


def background_colour(image: Image.Image) -> tuple[tuple[int, int, int], float]:
    """The colour around the edge and how much it varies (0 is perfectly
    plain)."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    strips = [
        rgb.crop((0, 0, width, EDGE)),
        rgb.crop((0, height - EDGE, width, height)),
        rgb.crop((0, 0, EDGE, height)),
        rgb.crop((width - EDGE, 0, width, height)),
    ]
    raw = b"".join(s.tobytes() for s in strips)
    pixels = [tuple(raw[i : i + 3]) for i in range(0, len(raw), 3)]
    pixels.sort(key=sum)
    middle = pixels[len(pixels) // 2]
    spread = sorted(
        max(abs(a - b) for a, b in zip(p, middle, strict=True)) for p in pixels
    )
    return middle, spread[int(len(spread) * 0.95)]


def key_out(image: Image.Image) -> tuple[Image.Image, dict]:
    """``image`` with its plain background made transparent, cropped to what's
    left, and how it went: {"background", "spread", "opaque"}. The edges are
    soft, and the background's colour is taken out of half-transparent
    pixels so the lettering doesn't keep a dark or light fringe."""
    rgb = image.convert("RGB")
    colour, spread = background_colour(rgb)
    plain = Image.new("RGB", rgb.size, colour)
    difference = ImageChops.difference(rgb, plain)
    channels = difference.split()
    distance = ImageChops.lighter(
        ImageChops.lighter(channels[0], channels[1]), channels[2]
    )
    low, high = spread + 6, spread + 60
    alpha = distance.point(
        lambda d: (
            0
            if d <= low
            else 255
            if d >= high
            else round((d - low) * 255 / (high - low))
        )
    )
    alpha = alpha.filter(ImageFilter.GaussianBlur(0.6))
    try:
        import numpy

        a = numpy.asarray(alpha, dtype=numpy.float32)[..., None] / 255
        pixels = numpy.asarray(rgb, dtype=numpy.float32)
        bg = numpy.array(colour, dtype=numpy.float32)
        clean = (pixels - (1 - a) * bg) / numpy.maximum(a, 1 / 255)
        rgb = Image.fromarray(numpy.clip(clean, 0, 255).astype(numpy.uint8), "RGB")
    except ImportError:
        pass  # a faint fringe; still transparent
    keyed = rgb.convert("RGBA")
    keyed.putalpha(alpha)
    box = alpha.point(lambda v: 255 if v > 24 else 0).getbbox()
    if box:
        pad = 12
        box = (
            max(0, box[0] - pad),
            max(0, box[1] - pad),
            min(keyed.width, box[2] + pad),
            min(keyed.height, box[3] + pad),
        )
        keyed = keyed.crop(box)
    histogram = keyed.getchannel("A").histogram()
    opaque = sum(histogram[128:]) / max(1, keyed.width * keyed.height)
    return keyed, {"background": colour, "spread": spread, "opaque": round(opaque, 3)}


def finish(data: bytes, task: dict) -> tuple[bytes, list[str], dict]:
    """The picture as it's sent: its type's shape, a logo transparent.
    Returns (PNG bytes, the steps done, facts)."""
    image = load(data)
    steps, facts = [], {}
    if task.get("transparent"):
        has_alpha = (
            image.mode in ("RGBA", "LA") and image.getchannel("A").getextrema()[0] < 250
        )
        if not has_alpha:
            image, facts = key_out(image)
            steps.append("plain background keyed out to transparency")
    else:
        image = image.convert("RGB")
        cropped = crop_to(image, task.get("aspect"))
        if cropped.size != image.size:
            steps.append(
                f"cropped {image.width}×{image.height} to {cropped.width}×{cropped.height}"
            )
        image = cropped
    return png(image), steps, facts
