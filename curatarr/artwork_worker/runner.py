"""The worker's loop: say hello, ask for work, make it, send it back.

Pictures are made in threads, up to the worker's slots. Each reports
progress (which also renews its lease with Curatarr) at most every few
seconds, and stops when Curatarr says it was cancelled. A picture that's
made but can't be sent (Curatarr unreachable) is kept in an outbox and
sent when Curatarr's back, even after the worker restarts.
"""

import json
import logging
import os
import platform
import shutil
import signal
import threading
import time
from pathlib import Path

import requests

from . import VERSION, gpu
from .backends import Cancelled, Failed, Reference
from .backends import load as load_backends
from .client import Server, Unauthorised
from .pipeline import make_picture

log = logging.getLogger("curatarr.artwork_worker")
HELLO_EVERY = 30
REPORT_EVERY = 5
KEEPALIVE = 60  # a quiet picture still renews its lease this often


def machine() -> dict:
    info = {
        "host": platform.node(),
        "system": f"{platform.system()} {platform.release()}",
        "processor": platform.processor() or platform.machine(),
        "cpus": os.cpu_count(),
        "version": VERSION,
    }
    try:
        pages = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        info["ram_mb"] = pages // (1024 * 1024)
    except (ValueError, OSError, AttributeError):
        pass
    return info | gpu.info()


class Worker:
    def __init__(self, config: dict, token: str, server=None):
        self.config = config
        self.work_dir = Path(config["work_dir"]).expanduser()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        (self.work_dir / "outbox").mkdir(exist_ok=True)
        self.server = server or Server(config["server"], token)
        self.backends = load_backends(config, self.work_dir)
        self.slots = max(1, int(config.get("slots") or 1))
        self.running: dict[str, threading.Thread] = {}
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.last_hello = 0.0
        self.last_work = time.time()
        self.checks = dict(config.get("checks") or {}) | {
            "_work_dir": str(self.work_dir)
        }

    # --- Talking to Curatarr ----------------------------------------------

    def engines(self) -> list[dict]:
        found = []
        for backend in self.backends:
            try:
                found += backend.engines()
            except Exception as exc:  # noqa: BLE001  one backend's trouble only
                log.warning("%s couldn't list its engines: %s", backend.name, exc)
        return found

    def hello(self, error=None) -> list[dict]:
        engines = self.engines()
        self.server.hello({"capabilities": engines, "info": machine(), "error": error})
        self.last_hello = time.time()
        return engines

    def backend_for(self, engine: str):
        for backend in self.backends:
            if engine.split("/", 1)[0] == backend.name:
                return backend
        return None

    # --- Making pictures ---------------------------------------------------

    def _references(self, task: dict, folder: Path) -> list[Reference]:
        found = []
        for ref in task.get("references") or []:
            data = self.server.reference(task["image"], ref["number"])
            path = folder / f"ref-{ref['number']}.img"
            path.write_bytes(data)
            found.append(Reference(ref["number"], path, ref["role"], ref.get("person")))
        return found

    def make(self, task: dict) -> None:
        image = task["image"]
        folder = self.work_dir / "tasks" / image
        folder.mkdir(parents=True, exist_ok=True)
        state = {"sent": 0.0, "cancel": False, "last": (None, None)}

        def progress(percent, message=None, stage="generating"):
            now = time.time()
            changed = (stage, message) != state["last"]
            if changed or now - state["sent"] >= REPORT_EVERY:
                state["cancel"] = self.server.progress(image, stage, percent, message)
                state["sent"], state["last"] = now, (stage, message)
            if state["cancel"] or self.stopping.is_set():
                raise Cancelled()

        keepalive = threading.Event()

        def keep_lease():
            while not keepalive.wait(KEEPALIVE):
                if time.time() - state["sent"] >= KEEPALIVE:
                    state["cancel"] = self.server.progress(image)
                    state["sent"] = time.time()

        threading.Thread(target=keep_lease, daemon=True).start()
        try:
            backend = self.backend_for(task["engine"])
            if backend is None:
                raise Failed(f"This worker has no {task['engine']}.", code="refused")
            progress(2, "Fetching references", "preparing")
            references = self._references(task, folder)
            result = make_picture(task, backend, references, progress, self.checks)
            progress(97, "Sending", "validating")
            self.send(image, result["data"], result["meta"])
        except Cancelled:
            if self.stopping.is_set() and not state["cancel"]:
                # The worker's stopping: back in the queue now, not a try used.
                self.server.fail(image, "Its worker stopped.", "unavailable", 60)
            log.info("Stopped %s", image)
        except Failed as exc:
            log.warning("Couldn't make %s: %s", image, exc)
            self.server.fail(image, str(exc), exc.code, exc.retry_after)
        except Unauthorised:
            self.stopping.set()
        except Exception as exc:
            log.exception("Making %s failed", image)
            self.server.fail(image, f"The worker hit an error: {exc}")
        finally:
            keepalive.set()
            shutil.rmtree(folder, ignore_errors=True)
            self.last_work = time.time()
            with self.lock:
                self.running.pop(image, None)

    def send(self, image: str, data: bytes, meta: dict) -> None:
        try:
            answer = self.server.result(image, data, meta)
        except requests.RequestException:
            outbox = self.work_dir / "outbox"
            (outbox / f"{image}.png").write_bytes(data)
            (outbox / f"{image}.json").write_text(json.dumps(meta))
            log.warning("Curatarr unreachable: %s kept to send later", image)
            return
        if not answer.get("ok"):
            log.warning("Curatarr refused %s: %s", image, answer.get("problems"))

    def flush_outbox(self) -> None:
        outbox = self.work_dir / "outbox"
        for picture in sorted(outbox.glob("*.png")):
            meta_path = picture.with_suffix(".json")
            try:
                meta = json.loads(meta_path.read_text())
                self.server.result(picture.stem, picture.read_bytes(), meta)
            except requests.RequestException:
                return  # still unreachable
            except (OSError, ValueError):
                pass
            picture.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)

    # --- The loop -------------------------------------------------------------

    def step(self) -> int:
        """One pass: hello when due, then claim work for free slots. Returns
        how many pictures were started."""
        engines = None
        if time.time() - self.last_hello >= HELLO_EVERY:
            engines = self.hello()
            self.flush_outbox()
        with self.lock:
            free = self.slots - len(self.running)
        if free <= 0 or self.stopping.is_set():
            return 0
        if engines is None:
            engines = self.engines()
        ready = [e["engine"] for e in engines if e.get("ready")]
        if not ready:
            return 0
        tasks = self.server.claim(free, ready)
        for task in tasks:
            thread = threading.Thread(target=self.make, args=(task,), daemon=True)
            with self.lock:
                self.running[task["image"]] = thread
            thread.start()
        if not tasks and not self.running and time.time() - self.last_work > 60:
            for backend in self.backends:
                backend.idle()
        return len(tasks)

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self.stopping.set())
        poll = max(2, int(self.config.get("poll_seconds") or 10))
        log.info(
            "Worker started with %s",
            ", ".join(b.name for b in self.backends) or "no backends",
        )
        while not self.stopping.is_set():
            try:
                started = self.step()
            except Unauthorised as exc:
                log.error("%s Stopping.", exc)
                break
            except requests.RequestException as exc:
                log.warning("Curatarr unreachable (%s); trying again", exc)
                started = 0
                self.stopping.wait(min(120, poll * 3))
                continue
            if not started:
                self.stopping.wait(poll)
        deadline = time.time() + 120
        for thread in list(self.running.values()):
            thread.join(timeout=max(0, deadline - time.time()))
        for backend in self.backends:
            backend.close()
