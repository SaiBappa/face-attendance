FROM python:3.11-slim

RUN pip install --no-cache-dir faster-whisper starlette "uvicorn[standard]" python-multipart

# Bake the multilingual Whisper model into the image so the kiosk works without internet.
# "base" is a good speed/accuracy balance on a Mac CPU (~1 s for a short sentence).
ARG WHISPER_MODEL=base
ENV WHISPER_MODEL=${WHISPER_MODEL} HF_HOME=/models
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8'); print('whisper OK')"

WORKDIR /app
COPY app.py .
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
