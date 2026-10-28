"""The ways a worker can make pictures. Each backend offers one or more
engines ("comfyui/flux2-klein-4b", "codex/image"); a new one only needs
a Backend subclass registered here."""

from .base import Backend, Cancelled, Failed, Made, Reference
from .codex import CodexBackend
from .comfyui import ComfyBackend
from .mock import MockBackend

BACKENDS = {"comfyui": ComfyBackend, "codex": CodexBackend, "mock": MockBackend}

__all__ = ["BACKENDS", "Backend", "Cancelled", "Failed", "Made", "Reference", "load"]


def load(config: dict, work_dir) -> list[Backend]:
    """The backends turned on in the worker's settings."""
    found = []
    for name, options in (config.get("backends") or {}).items():
        if options.get("enabled") and name in BACKENDS:
            found.append(BACKENDS[name](options, work_dir))
    return found
