"""Talking to Curatarr: every request carries the worker's token, and a
dropped connection is retried rather than losing the picture."""

import json
import time

import requests

TIMEOUT = (10, 120)  # connect, read


class Unauthorised(Exception):
    """The token was refused: the worker was turned off or its token replaced."""


class Server:
    def __init__(self, url: str, token: str, session=None):
        self.url = url.rstrip("/") + "/api/v1/artwork/worker"
        self.session = session or requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def _call(self, method, path, tries=3, **kwargs):
        delay = 2
        for attempt in range(tries):
            try:
                response = self.session.request(
                    method, self.url + path, timeout=TIMEOUT, **kwargs
                )
            except requests.RequestException:
                if attempt == tries - 1:
                    raise
                time.sleep(delay)
                delay *= 3
                continue
            if response.status_code == 401:
                raise Unauthorised("Curatarr refused this worker's token.")
            if response.status_code >= 500 and attempt < tries - 1:
                time.sleep(delay)
                delay *= 3
                continue
            return response
        raise RuntimeError("unreachable")

    def hello(self, report: dict) -> dict:
        response = self._call("POST", "/hello", json=report)
        response.raise_for_status()
        return response.json()

    def claim(self, slots: int, ready: list[str]) -> list[dict]:
        response = self._call("POST", "/claim", json={"slots": slots, "ready": ready})
        response.raise_for_status()
        return response.json().get("tasks") or []

    def progress(self, image: str, state=None, progress=None, message=None) -> bool:
        """Reports progress; True when Curatarr says to stop."""
        body = {"state": state, "progress": progress, "message": message}
        try:
            response = self._call("POST", f"/tasks/{image}/progress", json=body)
        except requests.RequestException:
            return False  # keep going; the lease allows a missed report
        if response.status_code != 200:
            return False
        return bool(response.json().get("cancel"))

    def reference(self, image: str, number: int) -> bytes:
        response = self._call("GET", f"/tasks/{image}/references/{number}")
        response.raise_for_status()
        return response.content

    def result(self, image: str, data: bytes, meta: dict) -> dict:
        response = self._call(
            "POST",
            f"/tasks/{image}/result",
            tries=6,
            files={"picture": ("picture.png", data, "image/png")},
            data={"meta": json.dumps(meta)},
        )
        if response.status_code not in (200, 409):
            response.raise_for_status()
        return response.json()

    def fail(self, image: str, error: str, code=None, retry_after=None) -> None:
        body = {"error": error, "code": code, "retry_after": retry_after}
        try:
            self._call("POST", f"/tasks/{image}/fail", json=body)
        except requests.RequestException:
            pass  # the lease runs out and the picture is tried again
