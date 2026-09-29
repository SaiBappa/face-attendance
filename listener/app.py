"""
Speech listener — turns a short voice clip from the kiosk into text + the language spoken.

  POST /transcribe  (multipart 'file': webm / mp4 / wav from the browser's MediaRecorder)
       -> {"text": "...", "language": "ru", "probability": 0.97, "seconds": 2.4}

Runs faster-whisper locally on the CPU; audio is decoded in memory and never written to disk
beyond the request's temp buffer.
"""
import io
import os
import threading
import time

import numpy as np
from faster_whisper import WhisperModel
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

MODEL = WhisperModel(os.environ.get("WHISPER_MODEL", "base"), device="cpu", compute_type="int8")


def _warm_up():
    """The first real transcription otherwise pays ~1 s for lazy initialisation. Exercise the same
    path a request takes: container decoding + the VAD filter, then the decoder itself."""
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes((np.sin(np.arange(16000) * 2 * np.pi * 220 / 16000) * 8000).astype(np.int16).tobytes())
    list(MODEL.transcribe(io.BytesIO(buf.getvalue()), beam_size=1, vad_filter=True)[0])
    noise = (np.random.default_rng(0).standard_normal(16000) * 0.05).astype(np.float32)
    list(MODEL.transcribe(noise, beam_size=1)[0])


_warm_up()


# One transcription at a time: two in parallel oversubscribe the CPU threads and each took ~14 s
# instead of ~1 s. The thread pool still keeps the event loop (healthchecks, uploads) responsive.
_busy = threading.Lock()


def _run(data: bytes):
    with _busy:
        segments, info = MODEL.transcribe(
            io.BytesIO(data), beam_size=1, vad_filter=True, condition_on_previous_text=False
        )
        # segments is a lazy generator: decoding happens while joining, so keep it under the lock
        return " ".join(s.text.strip() for s in segments).strip(), info


async def healthcheck(request):
    return JSONResponse({"status": "OK", "model": os.environ.get("WHISPER_MODEL", "base")})


async def transcribe(request: Request):
    form = await request.form()
    up = form.get("file")
    if up is None:
        return JSONResponse({"message": "file is required"}, status_code=400)
    data = await up.read()
    if len(data) < 800:
        return JSONResponse({"text": "", "language": None, "probability": 0.0, "seconds": 0})
    t0 = time.time()
    # off the event loop, so a second kiosk's clip (or a healthcheck) isn't stuck behind this one
    text, info = await run_in_threadpool(_run, data)
    return JSONResponse({
        "text": text,
        "language": info.language,
        "probability": round(float(info.language_probability), 3),
        "seconds": round(float(info.duration), 2),
        "took": round(time.time() - t0, 2),
    })


app = Starlette(routes=[
    Route("/healthcheck", healthcheck),
    Route("/transcribe", transcribe, methods=["POST"]),
])
