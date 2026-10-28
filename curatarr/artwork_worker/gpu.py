"""The graphics card, as nvidia-smi reports it: what it is, and memory use
sampled while a picture's made."""

import shutil
import subprocess
import threading


def query(*fields: str) -> list[str] | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                f"--query-gpu={','.join(fields)}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    return [v.strip() for v in out[0].split(",")] if out else None


def info() -> dict:
    found = query("name", "memory.total", "memory.used")
    if not found:
        return {}
    name, total, used = found
    try:
        return {
            "gpu": name,
            "vram_mb": int(total),
            "vram_free_mb": int(total) - int(used),
        }
    except ValueError:
        return {"gpu": name}


def free_mb() -> int | None:
    found = query("memory.total", "memory.used")
    try:
        return int(found[0]) - int(found[1]) if found else None
    except ValueError:
        return None


class Peak:
    """Samples graphics memory in use (whole card) while open."""

    def __init__(self, every=0.5):
        self.every = every
        self.mb = 0
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        if shutil.which("nvidia-smi"):
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            found = query("memory.used")
            if found:
                try:
                    self.mb = max(self.mb, int(found[0]))
                except ValueError:
                    pass
            self._stop.wait(self.every)

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        return False


def used_by(match) -> int:
    """Graphics memory (MiB) held by processes ``match(pid)`` picks out."""
    if not shutil.which("nvidia-smi"):
        return 0
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,used_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return 0
    total = 0
    for line in out:
        try:
            pid, used = (int(v) for v in line.split(","))
        except ValueError:
            continue
        if match(pid):
            total += used
    return total
