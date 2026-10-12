"""Transcription and the language model take turns on the GPU.

faster-whisper and Ollama's model do not fit on one consumer card together
alongside Jellyfin's transcoding. On the owner's 12 GB card, qwen3:14b needs
about 10 GB; while a transcription held 2 GB, Ollama could not load and 151
of 165 files overnight fell back to Navigation chapters, and half the
transcriptions that started while Ollama was loaded fell back to the CPU.

Language-model requests share the GPU with each other. A transcription waits
for the requests in flight, holds the GPU alone, and asks Ollama to unload
its model first. A waiting transcription goes ahead of new requests, so it
waits for at most one request each. This coordinates the threads of one
worker process, which is where chapter jobs run.
"""

import threading
from contextlib import contextmanager

_condition = threading.Condition()
_requests = 0
_transcribing = False
_waiting = 0


@contextmanager
def shared():
    """Held around each language-model request."""
    global _requests
    with _condition:
        while _transcribing or _waiting:
            _condition.wait()
        _requests += 1
    try:
        yield
    finally:
        with _condition:
            _requests -= 1
            _condition.notify_all()


@contextmanager
def exclusive():
    """Held around a transcription."""
    global _transcribing, _waiting
    with _condition:
        _waiting += 1
        while _transcribing or _requests:
            _condition.wait()
        _waiting -= 1
        _transcribing = True
    try:
        yield
    finally:
        with _condition:
            _transcribing = False
            _condition.notify_all()
