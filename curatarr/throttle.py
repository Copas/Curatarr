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
_stopping = threading.Event()  # all work paused on request: tools are ended


class Interrupted(BaseException):
    """A tool was ended because all work was paused; the job goes back in the
    queue. A BaseException, so error handling that catches tool failures
    doesn't record it as one."""


_children: set[subprocess.Popen] = set()
_suspendable: set[subprocess.Popen] = set()  # not the worker's own thread's
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
    for process in list(_suspendable):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass


def _pausable() -> bool:
    """Tools on the worker's own thread are never suspended: that thread is
    the one that lifts a pause, so suspending its tool (a reconcile probing a
    new import while someone watched) left the worker stuck for good."""
    return threading.current_thread() is not threading.main_thread()


def wait_while_paused():
    while _pausable() and _paused.is_set():
        if _stopping.is_set():
            raise Interrupted
        time.sleep(POLL_SECONDS)
    if _stopping.is_set():
        raise Interrupted


def stop_all() -> None:
    """End every running tool and refuse to start more, until allow()."""
    with _lock:
        _stopping.set()
        for process in list(_children):
            for sig in (signal.SIGCONT, signal.SIGKILL):  # a suspended one too
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    pass


def allow() -> None:
    _stopping.clear()


def stopping() -> bool:
    return _stopping.is_set()


def run(args, timeout, env=None) -> subprocess.CompletedProcess:
    """subprocess.run(capture_output=True, text=True) that honours pausing.

    ``timeout`` counts only time spent running, not time suspended. Raises
    subprocess.TimeoutExpired like subprocess.run.
    """
    if _stopping.is_set():
        raise Interrupted
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        env=env,
        start_new_session=True,  # its own process group, signalled as one
    )
    pausable = _pausable()
    with _lock:
        _children.add(process)
        if pausable:
            _suspendable.add(process)
            if _paused.is_set():
                os.killpg(process.pid, signal.SIGSTOP)
    started, paused_before = time.monotonic(), paused_seconds()
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                active = time.monotonic() - started
                if pausable:
                    active -= paused_seconds() - paused_before
                if active > timeout:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
                    raise
    finally:
        with _lock:
            _children.discard(process)
            _suspendable.discard(process)
    if _stopping.is_set() and process.returncode != 0:
        raise Interrupted
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
