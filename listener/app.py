"""
Speech listener — turns a short voice clip from the kiosk into text + the language spoken.

  POST /transcribe  (multipart 'file': webm / mp4 / wav from the browser's MediaRecorder)
       -> {"text": "...", "language": "ru", "probability": 0.97, "seconds": 2.4}

Runs faster-whisper locally on the CPU; audio is decoded in memory and never written to disk
beyond the request's temp buffer.
"""
import io
import os
import time

from faster_whisper import WhisperModel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

MODEL = WhisperModel(os.environ.get("WHISPER_MODEL", "base"), device="cpu", compute_type="int8")


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
    segments, info = MODEL.transcribe(
        io.BytesIO(data), beam_size=1, vad_filter=True, condition_on_previous_text=False
    )
    text = " ".join(s.text.strip() for s in segments).strip()
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
