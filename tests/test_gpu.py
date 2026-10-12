import threading
import time

import pytest
import requests

from curatarr import gpu, semantic
from curatarr.semantic import LanguageModelError, LanguageModelUnavailable, Ollama


def test_transcription_waits_for_requests_and_goes_before_new_ones():
    order = []
    request_started = threading.Event()
    release_request = threading.Event()

    def request(name, started=None, release=None):
        with gpu.shared():
            order.append(f"{name} start")
            if started:
                started.set()
            if release:
                release.wait(5)
            order.append(f"{name} end")

    def transcription():
        with gpu.exclusive():
            order.append("transcribe")

    first = threading.Thread(
        target=request, args=("r1", request_started, release_request)
    )
    first.start()
    request_started.wait(5)
    waiting = threading.Thread(target=transcription)
    waiting.start()
    time.sleep(0.1)
    later = threading.Thread(target=request, args=("r2",))
    later.start()
    time.sleep(0.1)
    assert order == ["r1 start"]  # neither may start while r1 runs
    release_request.set()
    for thread in (first, waiting, later):
        thread.join(5)
    assert order == ["r1 start", "r1 end", "transcribe", "r2 start", "r2 end"]


class _Response:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, json, timeout):
        self.posts.append((url, json))
        return self.responses.pop(0)


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(semantic.time, "sleep", lambda _seconds: None)


def test_ollama_retries_a_failed_request(no_wait):
    answer = _Response(200, {"message": {"content": '{"choice": "B"}'}})
    session = _Session([_Response(500), answer])
    llm = Ollama("http://ollama", "qwen3:14b", session=session)
    assert llm.ask("prompt", {"properties": {}}) == {"choice": "B"}
    assert len(session.posts) == 2


def test_ollama_gives_up_after_three_tries(no_wait):
    session = _Session([_Response(500)] * 3)
    llm = Ollama("http://ollama", "qwen3:14b", session=session)
    with pytest.raises(LanguageModelUnavailable):
        llm.ask("prompt", {"properties": {}})
    assert len(session.posts) == 3


def test_ollama_does_not_retry_a_refused_request(no_wait):
    session = _Session([_Response(404)])
    llm = Ollama("http://ollama", "missing-model", session=session)
    with pytest.raises(LanguageModelError) as raised:
        llm.ask("prompt", {"properties": {}})
    assert not isinstance(raised.value, LanguageModelUnavailable)
    assert len(session.posts) == 1


def test_unload_asks_ollama_to_free_the_model_now():
    session = _Session([_Response(200, {})])
    Ollama("http://ollama", "qwen3:14b", session=session).unload()
    assert session.posts == [
        ("http://ollama/api/generate", {"model": "qwen3:14b", "keep_alive": 0})
    ]
