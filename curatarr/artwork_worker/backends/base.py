from dataclasses import dataclass, field
from pathlib import Path


class Cancelled(Exception):
    """Curatarr said to stop (the picture was cancelled or reassigned)."""


class Failed(Exception):
    """The picture couldn't be made. ``code`` tells Curatarr why: usage_limit
    and busy wait without using up a try; refused gives up at once."""

    def __init__(self, message, code=None, retry_after=None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


@dataclass
class Reference:
    number: int  # as the prompt numbers it ("image 2")
    path: Path
    role: str  # person, set, original, lettering
    person: str | None = None


@dataclass
class Made:
    data: bytes
    params: dict = field(default_factory=dict)
    seed: int | None = None


class Backend:
    name = ""

    def __init__(self, options: dict, work_dir: Path):
        self.options = options
        self.work_dir = Path(work_dir)

    def engines(self) -> list[dict]:
        """What it can make, as Curatarr lists it: [{engine, label, kinds,
        references, external, ready, why}]."""
        raise NotImplementedError

    def make(self, task: dict, references: list[Reference], progress) -> Made:
        """One picture for ``task``. ``progress(percent, message)`` reports
        progress and raises Cancelled when Curatarr says to stop."""
        raise NotImplementedError

    def idle(self) -> None:
        """Called when there's been no work for a while."""

    def close(self) -> None:
        """Called when the worker stops."""
