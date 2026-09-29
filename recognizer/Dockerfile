# ---- build stage: passive liveness (anti-spoofing) models ----------------------------------------
# MiniFASNet from the Silent-Face-Anti-Spoofing authors (minivision-ai, Apache-2.0), exported from their
# original PyTorch weights to ONNX here so the runtime image needs no PyTorch. Two models look at the face
# at different zooms (2.7x and 4x the face box: skin texture, and the edges of a phone or print held up
# to the camera). ~2 MB together, a few ms, and only run on frames that already match a staff member.
FROM python:3.11-slim AS fas
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/minivision-ai/Silent-Face-Anti-Spoofing /fas
RUN pip install --no-cache-dir torch onnx --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple
RUN mkdir -p /out && cd /fas && python - <<'PY'
import inspect, torch
from src.model_lib.MiniFASNet import MiniFASNetV2, MiniFASNetV1SE
kw = {"dynamo": False} if "dynamo" in inspect.signature(torch.onnx.export).parameters else {}
for fn, cls, out in (("2.7_80x80_MiniFASNetV2.pth", MiniFASNetV2, "/out/liveness_2.7.onnx"),
                     ("4_0_0_80x80_MiniFASNetV1SE.pth", MiniFASNetV1SE, "/out/liveness_4.0.onnx")):
    sd = torch.load("resources/anti_spoof_models/" + fn, map_location="cpu")
    sd = {k[7:] if k.startswith("module.") else k: v for k, v in sd.items()}
    m = cls(conv6_kernel=(5, 5))  # 80x80 input
    m.load_state_dict(sd)
    m.eval()
    torch.onnx.export(m, torch.zeros(1, 3, 80, 80), out, input_names=["input"], output_names=["logits"], opset_version=11, **kw)
    print("exported", out)
PY
# the authors' sample photos (real faces and spoofs), for checking the export: see README "Liveness"
RUN cp -r /fas/images/sample /out/samples

FROM python:3.11-slim

# System libs OpenCV needs. Retries + fix-missing make this survive a flaky Debian mirror.
RUN sed -i 's#http://deb.debian.org#https://deb.debian.org#g' /etc/apt/sources.list.d/debian.sources && \
    echo 'Acquire::Retries "10"; Acquire::http::Timeout "30";' > /etc/apt/apt.conf.d/80-retries && \
    for i in 1 2 3 4 5; do \
      apt-get update && apt-get install -y --no-install-recommends --fix-missing \
        libgl1 libglib2.0-0 ca-certificates && break || { echo "apt attempt $i failed, retrying…"; sleep 10; }; \
    done && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir numpy opencv-python-headless starlette "uvicorn[standard]" python-multipart

# ARM-native face + emotion models from OpenCV Zoo / ONNX model zoo (git-LFS media endpoint). Downloaded with Python, no curl needed.
RUN mkdir -p /models && python - <<'PY'
import urllib.request, time
files = {
  "/models/yunet.onnx": "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
  "/models/sface.onnx": "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
  # FER+ facial-expression model (8 emotions), ONNX model zoo
  "/models/emotion.onnx": "https://media.githubusercontent.com/media/onnx/models/main/validated/vision/body_analysis/emotion_ferplus/model/emotion-ferplus-8.onnx",
}
for path, url in files.items():
    for attempt in range(5):
        try:
            urllib.request.urlretrieve(url, path); print("downloaded", path); break
        except Exception as e:
            print("retry", attempt, path, e); time.sleep(5)
    else:
        raise SystemExit(f"could not download {url}")
import cv2
cv2.FaceDetectorYN.create("/models/yunet.onnx", "", (320, 320))
cv2.FaceRecognizerSF.create("/models/sface.onnx", "")
cv2.dnn.readNetFromONNX("/models/emotion.onnx")
print("models OK")
PY
COPY --from=fas /out/liveness_2.7.onnx /out/liveness_4.0.onnx /models/
COPY --from=fas /out/samples /models/liveness_samples
RUN python - <<'PY'
import cv2, numpy as np
for p in ("/models/liveness_2.7.onnx", "/models/liveness_4.0.onnx"):
    n = cv2.dnn.readNetFromONNX(p)
    n.setInput(np.zeros((1, 3, 80, 80), np.float32))
    assert n.forward().size == 3, p
print("liveness models OK")
PY

WORKDIR /app
COPY app.py .
EXPOSE 8000
# Several workers so frames from different kiosks are processed in parallel instead of queueing
# (each worker loads its own models, ~70 MB). Enrolment changes reach every worker via /data/faces/.changed.
ENV WORKERS=3 CV_THREADS=2
# uvicorn kills a worker that misses a 5 s health ping. When the host is swapping, the whole Docker VM can
# stall that long, and a worker killed mid-frame (then reloading its models) turns a hiccup into
# "Server unreachable" on the kiosks, so give it 30 s.
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port 8000 --workers ${WORKERS} --timeout-worker-healthcheck 30"]
