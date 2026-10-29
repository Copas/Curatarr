"""Local image models through ComfyUI, on this computer: nothing leaves it.

The worker talks to ComfyUI's HTTP API on 127.0.0.1. Given ComfyUI's folder
and Python, it starts ComfyUI itself when there's work (listening on
127.0.0.1 only) and stops it after a while with none, so the graphics card
is free for everything else between jobs. It won't start a picture while
something else holds most of the card's memory (Jellyfin transcoding,
another model): the picture waits and is tried again later.
"""

import json
import re
import subprocess
import time
import uuid
from pathlib import Path

import requests

from .. import gpu
from .base import Backend, Cancelled, Failed, Made, Reference
from .workflows import KINDS, WORKFLOWS

LOADERS = ("UNETLoader", "CLIPLoader", "VAELoader", "CheckpointLoaderSimple")
NODES = ("IPAdapterFaceID", "EmptyFlux2LatentImage", "ReferenceLatent")


class ComfyBackend(Backend):
    name = "comfyui"

    def __init__(self, options, work_dir):
        super().__init__(options, work_dir)
        self.url = (options.get("url") or "http://127.0.0.1:8188").rstrip("/")
        self.process = None
        self.last_used = 0.0
        self.freed = True
        self._available = None
        self._available_at = 0.0

    # --- ComfyUI itself ---------------------------------------------------

    def _get(self, path, **kwargs):
        return requests.get(
            self.url + path, timeout=kwargs.pop("timeout", 10), **kwargs
        )

    def running(self) -> bool:
        try:
            return self._get("/system_stats", timeout=3).ok
        except requests.RequestException:
            return False

    def can_start(self) -> bool:
        folder = self.options.get("folder")
        return bool(folder and (Path(folder) / "main.py").is_file())

    def start(self) -> None:
        """Starts ComfyUI (on 127.0.0.1) when it isn't running and the worker
        knows where it is."""
        if self.running():
            return
        if not self.can_start():
            raise Failed(
                f"ComfyUI isn't running at {self.url}, and the worker doesn't know "
                "where it's installed to start it.",
                code="unavailable",
                retry_after=600,
            )
        folder = Path(self.options["folder"])
        python = self.options.get("python") or str(folder / ".venv" / "bin" / "python")
        port = self.url.rsplit(":", 1)[-1].split("/")[0]
        log = open(self.work_dir / "comfyui.log", "ab")  # noqa: SIM115 (kept open)
        self.process = subprocess.Popen(
            [
                python,
                "main.py",
                "--listen",
                "127.0.0.1",
                "--port",
                port,
                "--disable-auto-launch",
            ],
            cwd=folder,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        deadline = time.time() + 240
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise Failed(
                    f"ComfyUI stopped while starting; see {self.work_dir / 'comfyui.log'}.",
                    code="unavailable",
                    retry_after=900,
                )
            if self.running():
                return
            time.sleep(2)
        raise Failed(
            "ComfyUI took too long to start.", code="unavailable", retry_after=600
        )

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def free(self) -> None:
        """Unloads ComfyUI's models from the graphics card."""
        try:
            requests.post(
                self.url + "/free",
                json={"unload_models": True, "free_memory": True},
                timeout=10,
            )
        except requests.RequestException:
            pass
        self.freed = True

    def idle(self) -> None:
        if not self.last_used:
            return
        quiet = time.time() - self.last_used
        if quiet > 60 and not self.freed and self.running():
            self.free()
        if self.process and quiet > float(self.options.get("stop_after_idle") or 300):
            self.stop()

    def close(self) -> None:
        if self.process:
            self.stop()

    # --- What it can make ---------------------------------------------------

    def available(self) -> dict[str, list[str]] | None:
        """{loader: model files} and the custom nodes ComfyUI has, or None when
        it isn't running (then the last answer, if there was one)."""
        now = time.time()
        if self._available is not None and now - self._available_at < 600:
            return self._available
        if not self.running():
            return self._available
        found: dict[str, list[str]] = {}
        for loader in LOADERS + NODES:
            try:
                info = self._get(f"/object_info/{loader}").json().get(loader)
            except (requests.RequestException, ValueError):
                info = None
            if not info:
                continue
            if loader in NODES:
                found[loader] = ["installed"]
                continue
            for name, spec in (info["input"].get("required") or {}).items():
                if name.endswith("_name") and isinstance(spec[0], list):
                    found[loader] = list(spec[0])
        self._available, self._available_at = found, now
        return found

    def engines(self):
        available = self.available()
        offered = []
        for workflow in WORKFLOWS:
            entry = {
                "engine": workflow.key,
                "label": workflow.label,
                "kinds": KINDS,
                "references": workflow.references,
                "external": False,
            }
            if available is None:
                if not self.can_start():
                    entry |= {
                        "ready": False,
                        "why": f"ComfyUI isn't running at {self.url}.",
                    }
                    offered.append(entry)
                    continue
                # Not started yet: check the model files on disk instead.
                if not self._on_disk(workflow):
                    continue
                offered.append(entry | {"ready": True})
                continue
            models, _missing = workflow.resolve(available)
            if models is None:
                continue  # not installed here: not offered
            offered.append(entry | {"ready": True})
        return offered

    def _on_disk(self, workflow) -> bool:
        folder = Path(self.options["folder"]) / "models"
        listing = {
            "UNETLoader": self._files(folder, "diffusion_models", "unet"),
            "CLIPLoader": self._files(folder, "text_encoders", "clip"),
            "VAELoader": self._files(folder, "vae"),
            "CheckpointLoaderSimple": self._files(folder, "checkpoints"),
            "IPAdapterFaceID": ["installed"]
            if (
                Path(self.options["folder"]) / "custom_nodes" / "ComfyUI_IPAdapter_plus"
            ).is_dir()
            else [],
        }
        models, _missing = workflow.resolve(listing)
        return models is not None

    @staticmethod
    def _files(models: Path, *subfolders) -> list[str]:
        found = []
        for sub in subfolders:
            try:
                found += [p.name for p in (models / sub).rglob("*.safetensors")]
            except OSError:
                pass
        return found

    # --- Making a picture ---------------------------------------------------

    def _workflow(self, engine: str):
        workflow = next((w for w in WORKFLOWS if w.key == engine), None)
        if workflow is None:
            raise Failed(f"This worker has no {engine} workflow.", code="refused")
        return workflow

    def _is_comfy(self, pid: int) -> bool:
        """Whether ``pid`` is this ComfyUI (its memory doesn't count as busy)."""
        if self.process and pid == self.process.pid:
            return True
        folder = self.options.get("folder")
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
            here = Path(f"/proc/{pid}/cwd").resolve()
        except OSError:
            return False
        if b"main.py" not in command:
            return False
        return bool(folder) and here == Path(folder).resolve()

    def _check_memory(self) -> None:
        """Refuses to start while something else holds the graphics card."""
        need = int(self.options.get("min_free_vram_mb") or 0)
        free = gpu.free_mb()
        if not need or free is None:
            return
        held = gpu.used_by(self._is_comfy)
        if free + held < need:
            raise Failed(
                f"The graphics card is busy: {free / 1024:.1f} GB free, and this "
                f"needs about {need / 1024:.0f} GB. It'll be tried again shortly.",
                code="busy",
                retry_after=300,
            )

    def _upload(self, path: Path, folder: str) -> str:
        with open(path, "rb") as handle:
            response = requests.post(
                self.url + "/upload/image",
                files={"image": (path.name, handle, "application/octet-stream")},
                data={"subfolder": folder, "type": "input", "overwrite": "true"},
                timeout=60,
            )
        response.raise_for_status()
        answer = response.json()
        return (
            f"{answer.get('subfolder')}/{answer['name']}"
            if answer.get("subfolder")
            else answer["name"]
        )

    def _clean_inputs(self, folder: str) -> None:
        if not self.options.get("folder"):
            return
        place = Path(self.options["folder"]) / "input" / folder
        if place.is_dir():
            for file in place.iterdir():
                file.unlink(missing_ok=True)
            place.rmdir()

    def make(self, task, references, progress):
        workflow = self._workflow(task["engine"])
        progress(5, "Starting ComfyUI" if not self.running() else "Preparing")
        self.start()
        self._check_memory()
        models, missing = workflow.resolve(self.available() or {})
        if models is None:
            raise Failed(f"{workflow.label} {missing}.", code="refused")
        refs = references
        if workflow.one_person:  # only the first person's photos
            people = [r for r in references if r.role == "person"]
            first = people[0].person if people else None
            refs = [r for r in people if r.person == first]
        elif not workflow.references:
            refs = []
        prompt = task["prompt"]
        if workflow.max_images:  # one photo of each person, at most
            refs, prompt = fit_references(refs, prompt, workflow.max_images, 1)
        elif not workflow.one_person:  # more than two each made likenesses worse
            refs, prompt = fit_references(refs, prompt, 8, 2)
        folder = f"curatarr-{task['image']}"
        try:
            names = [self._upload(r.path, folder) for r in refs]
            width, height = workflow.size(task["kind"])
            seed = int(task.get("seed") or 0)
            graph = workflow.graph(prompt, (width, height), seed, names, models)
            data = self._run(graph, workflow, progress)
        finally:
            self._clean_inputs(folder)
        self.last_used = time.time()
        self.freed = False
        return Made(
            data,
            {
                "engine": workflow.key,
                "models": models,
                "size": [width, height],
                "references": len(refs),
                "steps": getattr(workflow, "steps", None),
                "cfg": getattr(workflow, "cfg", None),
            },
            seed,
        )

    def _run(self, graph: dict, workflow, progress) -> bytes:
        client = str(uuid.uuid4())
        response = requests.post(
            self.url + "/prompt",
            json={"prompt": graph, "client_id": client},
            timeout=30,
        )
        if response.status_code != 200:
            raise Failed(f"ComfyUI refused the workflow: {response.text[:400]}")
        prompt_id = response.json()["prompt_id"]
        started = time.time()
        try:
            while True:
                elapsed = time.time() - started
                progress(
                    min(90, 10 + int(80 * elapsed / max(workflow.seconds, 1))),
                    "Generating",
                )
                history = self._get(f"/history/{prompt_id}").json()
                if prompt_id in history:
                    break
                if elapsed > max(900, workflow.seconds * 20):
                    raise Failed("ComfyUI took far longer than expected.")
                time.sleep(1)
        except Cancelled:
            self._interrupt(prompt_id)
            raise
        entry = history[prompt_id]
        status = entry.get("status") or {}
        if status.get("status_str") != "success":
            messages = [
                m for m in status.get("messages") or [] if m[0] == "execution_error"
            ]
            detail = messages[0][1].get("exception_message") if messages else ""
            if "out of memory" in (detail or "").lower():
                raise Failed(
                    "The graphics card ran out of memory.", code="busy", retry_after=600
                )
            raise Failed(
                f"ComfyUI couldn't make it: {(detail or json.dumps(status))[:400]}"
            )
        images = (entry.get("outputs") or {}).get("save", {}).get("images") or []
        if not images:
            raise Failed("ComfyUI finished without a picture.")
        picture = self._get("/view", params=images[0], timeout=60)
        picture.raise_for_status()
        return picture.content

    def _interrupt(self, prompt_id: str) -> None:
        try:
            requests.post(self.url + "/queue", json={"delete": [prompt_id]}, timeout=10)
            requests.post(
                self.url + "/interrupt", json={"prompt_id": prompt_id}, timeout=10
            )
        except requests.RequestException:
            pass


def _numbers(indexes) -> str:
    """As the prompts write them (artwork_prompts._numbers)."""
    words = [str(i) for i in indexes]
    if len(words) == 1:
        return f"image {words[0]}"
    return "images " + ", ".join(words[:-1]) + " and " + words[-1]


def fit_references(refs, prompt, limit, each):
    """At most ``limit`` references: each person's ``each`` most typical
    photos (the sharpest among equals: a photo in sunglasses is the least
    typical), then the other references, with the prompt's image numbers
    changed to match. Given more, the models did worse: several photos side
    by side lost likenesses, and more than two each confused FLUX.2. Returns
    (references, prompt)."""
    people: dict[str, list] = {}
    others = []
    for ref in refs:
        if ref.role == "person" and ref.person:
            people.setdefault(ref.person, []).append(ref)
        else:
            others.append(ref)
    fitted, renumber = [], []
    for who, photos in people.items():
        room = min(each, limit - len(fitted))
        if room <= 0:
            break
        best = sorted(photos, key=lambda r: (-round(r.typical, 2), -r.face_px))[:room]
        numbers = []
        for ref in sorted(best, key=lambda r: r.number):
            numbers.append(len(fitted) + 1)
            fitted.append(
                Reference(
                    numbers[-1], ref.path, "person", who, ref.face_px, ref.typical
                )
            )
        renumber.append(([r.number for r in photos], numbers))
    for ref in others:
        if len(fitted) >= limit:
            break
        fitted.append(Reference(len(fitted) + 1, ref.path, ref.role, ref.person))
        renumber.append(([ref.number], [len(fitted)]))
    # Longest first, so "images 1 and 2" isn't half-replaced as "image 1".
    for index, (old, _new) in sorted(
        enumerate(renumber), key=lambda r: -len(_numbers(r[1][0]))
    ):
        prompt = prompt.replace(_numbers(old), f"@@{index}@@")
    return fitted, re.sub(
        r"@@(\d+)@@", lambda m: _numbers(renumber[int(m.group(1))][1]), prompt
    )
