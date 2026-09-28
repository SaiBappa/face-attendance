FROM python:3.11-slim

# System libs OpenCV needs. Retries + fix-missing make this survive a flaky Debian mirror.
RUN sed -i 's#http://deb.debian.org#https://deb.debian.org#g' /etc/apt/sources.list.d/debian.sources && \
    echo 'Acquire::Retries "10"; Acquire::http::Timeout "30";' > /etc/apt/apt.conf.d/80-retries && \
    for i in 1 2 3 4 5; do \
      apt-get update && apt-get install -y --no-install-recommends --fix-missing \
        libgl1 libglib2.0-0 ca-certificates && break || { echo "apt attempt $i failed, retrying…"; sleep 10; }; \
    done && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir numpy opencv-python-headless starlette "uvicorn[standard]" python-multipart

# ARM-native face models from OpenCV Zoo (git-LFS media endpoint). Downloaded with Python, no curl needed.
RUN mkdir -p /models && python - <<'PY'
import urllib.request, time
files = {
  "/models/yunet.onnx": "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
  "/models/sface.onnx": "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
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
print("models OK")
PY

WORKDIR /app
COPY app.py .
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
