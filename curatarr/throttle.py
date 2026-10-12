"""Pause chapter work while someone is watching.

Chapter jobs read whole files from the media share and use the GPU, which can
starve a stream. While paused, the external tools they run (FFmpeg, ffprobe,
the transcription child process) are suspended with SIGSTOP and resumed with
SIGCONT, so no work is lost, and time spent suspended does not count toward
their time limits. Language-model requests wait before they are sent.
"""

import os
import signal
import subprocess
import threading
import time

_paused = threading.Event()
_children: set[subprocess.Popen] = set()
_lock = threading.Lock()
POLL_SECONDS = 2.0
_paused_total = 0.0  # seconds spent paused, completed pauses
_paused_at: float | None = None


def paused() -> bool:
    return _paused.is_set()


def set_paused(value: bool) -> bool:
    """Pause or resume; returns True when the state changed."""
    global _paused_total, _paused_at
    with _lock:
        if value == _paused.is_set():
            return False
        if value:
            _paused.set()
            _paused_at = time.monotonic()
            _signal_children(signal.SIGSTOP)
        else:
            _paused.clear()
            _paused_total += time.monotonic() - (_paused_at or time.monotonic())
            _paused_at = None
            _signal_children(signal.SIGCONT)
    return True


def paused_seconds() -> float:
    """Total time spent paused so far, including a pause in progress."""
    with _lock:
        current = time.monotonic() - _paused_at if _paused_at is not None else 0.0
        return _paused_total + current


def _signal_children(sig):
    """Signal each tool's whole process group (it may run helpers). Hold _lock."""
    for process in list(_children):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass


def wait_while_paused():
    while _paused.is_set():
        time.sleep(POLL_SECONDS)


def run(args, timeout, env=None) -> subprocess.CompletedProcess:
    """subprocess.run(capture_output=True, text=True) that honours pausing.

    ``timeout`` counts only time spent running, not time suspended. Raises
    subprocess.TimeoutExpired like subprocess.run.
    """
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        env=env,
        start_new_session=True,  # its own process group, signalled as one
    )
    with _lock:
        _children.add(process)
        if _paused.is_set():
            os.killpg(process.pid, signal.SIGSTOP)
    started, paused_before = time.monotonic(), paused_seconds()
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                active = time.monotonic() - started - (paused_seconds() - paused_before)
                if active > timeout:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
                    raise
    finally:
        with _lock:
            _children.discard(process)
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
