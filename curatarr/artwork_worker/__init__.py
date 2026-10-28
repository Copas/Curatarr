"""Curatarr's artwork worker: makes the pictures Curatarr's Posters →
Generate asks for, on this computer, and sends them back for review.

It asks Curatarr for work over HTTP(S) with its own token (so it needs no open
port and no shared folders), makes each picture with one of its backends
(ComfyUI running a local image model, or Codex's image tool), checks it (the
title read back, faces compared with the reference photos) and uploads it.
Run ``curatarr-artwork-worker setup`` once, then ``curatarr-artwork-worker
run``; docs/ARTWORK.md has the details.
"""

VERSION = "1"
