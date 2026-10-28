"""A pretend engine for trying Curatarr's side without an image model: it
draws a plain picture with the title on it. Turned on for tests and demos."""

import time
from io import BytesIO

from PIL import Image, ImageDraw

from .base import Backend, Made

KINDS = ["Primary", "Thumb", "Backdrop", "Logo", "Banner"]


class MockBackend(Backend):
    name = "mock"

    def engines(self):
        return [
            {
                "engine": "mock/test",
                "label": "Test pictures (no AI)",
                "kinds": KINDS,
                "references": True,
                "external": False,
                "ready": True,
            }
        ]

    def make(self, task, references, progress):
        width, height = task["size"]
        progress(30, "Drawing")
        time.sleep(float(self.options.get("delay") or 0))
        seed = task.get("seed") or 0
        colour = (
            (seed * 53) % 200 + 30,
            (seed * 97) % 200 + 30,
            (seed * 31) % 200 + 30,
        )
        transparent = task.get("transparent")
        image = Image.new(
            "RGBA" if transparent else "RGB",
            (width, height),
            (0, 0, 0, 0) if transparent else colour,
        )
        draw = ImageDraw.Draw(image)
        if transparent:  # a block of "lettering" on see-through
            draw.rectangle(
                (width // 8, height // 4, width * 7 // 8, height * 3 // 4),
                fill=colour + (255,),
            )
        else:
            for i in range(0, width, 40):
                draw.line(
                    [(i, 0), (i + height // 3, height)], fill=(255, 255, 255), width=3
                )
        draw.text(
            (width // 10, height // 3),
            task.get("title") or "",
            fill=(255, 255, 255, 255),
            font_size=max(24, height // 8),
        )
        buffer = BytesIO()
        image.save(buffer, "PNG")
        progress(90, "Drawn")
        return Made(buffer.getvalue(), {"engine": "mock/test"}, seed)
