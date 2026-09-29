"""
ARM-native face recognizer — a drop-in replacement for the CompreFace endpoints the
attendance app uses. Uses OpenCV's YuNet (detection) + SFace (128-d embedding), which run
natively on Apple Silicon (no AVX / Rosetta needed, unlike CompreFace's x86 ML core).

Implements the subset of CompreFace's /api/v1/recognition/* API that attendance calls:
  GET    /subjects
  GET    /faces
  POST   /faces?subject=NAME            (multipart 'file')   -> enrol one photo
  POST   /recognize                     (multipart 'file')   -> best match per face
  DELETE /subjects/NAME
  PUT    /subjects/NAME  {"subject": new}

Similarity is SFace cosine (0..1); same-person pairs score ~0.4–0.8, different people <~0.3.
Employee face vectors are stored under /data; raw photos are not kept.

Each recognised face also carries two on-device signals, computed from the same frame and
then discarded with it:
  * emotion  — FER+ (ONNX model zoo, 8 classes) on the face crop
  * attire   — dominant clothing colour from the region below the chin, plus `hivis`: the share
               of fluorescent yellow/lime/orange pixels (a high-visibility vest) in the torso area
"""
import os, uuid, threading, json, time
import numpy as np
import cv2
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.requests import Request
from starlette.routing import Route

DATA = os.environ.get("DATA_DIR", "/data")
FACES = os.path.join(DATA, "faces")
os.makedirs(FACES, exist_ok=True)

DET_MODEL = os.environ.get("DET_MODEL", "/models/yunet.onnx")
REC_MODEL = os.environ.get("REC_MODEL", "/models/sface.onnx")
EMO_MODEL = os.environ.get("EMO_MODEL", "/models/emotion.onnx")

# several uvicorn workers share the CPU: keep each one's OpenCV thread pool small
cv2.setNumThreads(int(os.environ.get("CV_THREADS", "2")))

_lock = threading.Lock()
_det = cv2.FaceDetectorYN.create(DET_MODEL, "", (320, 320), 0.6, 0.3, 5000)
_rec = cv2.FaceRecognizerSF.create(REC_MODEL, "")
_emo = cv2.dnn.readNetFromONNX(EMO_MODEL) if os.path.exists(EMO_MODEL) else None
EMOTIONS = ("neutral", "happiness", "surprise", "sadness", "anger", "disgust", "fear", "contempt")

# in-memory index: {subject: [(image_id, np.float32[128]), ...]}
_index: dict = {}
# Each uvicorn worker keeps its own copy of the index. Any enrol/delete/rename touches this stamp
# file, and every worker reloads when the stamp is newer than its copy.
STAMP = os.path.join(FACES, ".changed")
_loaded_at = None  # stamp mtime this worker last loaded (None = never)


def _stamp_mtime() -> float:
    try:
        return os.stat(STAMP).st_mtime
    except OSError:
        return 0.0


def _touch_stamp():
    with open(STAMP, "a"):
        os.utime(STAMP, None)
    _sync_index()


def _sync_index():
    global _loaded_at
    m = _stamp_mtime()
    if m != _loaded_at:
        _load_index()
        _loaded_at = m


def _load_index():
    _index.clear()
    for subj in sorted(os.listdir(FACES)):
        d = os.path.join(FACES, subj)
        if not os.path.isdir(d):
            continue
        vecs = []
        for fn in os.listdir(d):
            if fn.endswith(".npy"):
                try:
                    vecs.append((fn[:-4], np.load(os.path.join(d, fn))))
                except Exception:
                    pass
        _index[_unslug(subj)] = vecs


def _slug(name: str) -> str:
    return name.replace("/", "_")


def _unslug(s: str) -> str:
    return s


def _decode(data: bytes):
    arr = np.frombuffer(data, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _detect_all(img, timing: dict = None, limit: int = 0):
    """Return list of (box[x,y,w,h], score, aligned_feature) sorted by score desc.
    Only the first `limit` faces (0 = all) get an embedding; `timing` collects stage times in ms."""
    h, w = img.shape[:2]
    with _lock:
        t0 = time.perf_counter()
        _det.setInputSize((w, h))
        _, faces = _det.detect(img)
        t1 = time.perf_counter()
        results = []
        if faces is not None:
            ranked = sorted(faces, key=lambda r: -r[-1])
            for f in ranked[:limit] if limit > 0 else ranked:
                aligned = _rec.alignCrop(img, f)
                feat = _rec.feature(aligned).flatten().astype(np.float32).copy()
                results.append((f[:4].tolist(), float(f[-1]), feat))
        t2 = time.perf_counter()
    if timing is not None:
        timing["detect"] = (t1 - t0) * 1000
        timing["embed"] = (t2 - t1) * 1000
    return results


def _emotion(img, box):
    """FER+ on a slightly padded grayscale face crop -> {label, confidence, scores}."""
    if _emo is None:
        return None
    x, y, w, h = [int(v) for v in box]
    pad = int(0.1 * max(w, h))
    H, W = img.shape[:2]
    x0, y0, x1, y1 = max(0, x - pad), max(0, y - pad), min(W, x + w + pad), min(H, y + h + pad)
    if x1 - x0 < 24 or y1 - y0 < 24:
        return None
    gray = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    blob = cv2.resize(gray, (64, 64)).astype(np.float32).reshape(1, 1, 64, 64)
    with _lock:
        _emo.setInput(blob)
        logits = _emo.forward().flatten()
    p = np.exp(logits - logits.max())
    p /= p.sum()
    i = int(p.argmax())
    return {"label": EMOTIONS[i], "confidence": round(float(p[i]), 3),
            "scores": {e: round(float(v), 3) for e, v in zip(EMOTIONS, p)}}


def _colour_name(h, s, v):
    """OpenCV HSV (h 0..180, s/v 0..255) -> a friendly clothing colour name."""
    if v < 50:
        return "black"
    if s < 40:
        return "white" if v > 190 else ("light grey" if v > 140 else ("grey" if v > 90 else "charcoal"))
    if h < 8 or h >= 170:
        return "maroon" if v < 110 else "red"
    if h < 20:
        return "brown" if v < 150 else "orange"
    if h < 34:
        return "mustard" if v < 170 else "yellow"
    if h < 78:
        return "olive" if v < 110 else "green"
    if h < 96:
        return "teal"
    if h < 130:
        return "navy" if v < 120 else ("sky blue" if s < 110 else "blue")
    if h < 150:
        return "purple"
    return "pink"


def _attire(img, box):
    """Dominant colour of the chest area just below the face (k-means, k=3)."""
    x, y, w, h = [int(v) for v in box]
    H, W = img.shape[:2]
    x0, x1 = max(0, x - w // 2), min(W, x + w + w // 2)
    y0, y1 = min(H, y + int(1.35 * h)), min(H, y + int(2.6 * h))
    if y1 - y0 < 16 or x1 - x0 < 16:
        return None
    region = cv2.resize(img[y0:y1, x0:x1], (48, 32), interpolation=cv2.INTER_AREA)
    px = region.reshape(-1, 3).astype(np.float32)
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 10, 1.0)
    _, labels, centers = cv2.kmeans(px, 3, None, crit, 2, cv2.KMEANS_PP_CENTERS)
    counts = np.bincount(labels.flatten(), minlength=3)
    k = int(counts.argmax())
    b, g, r = [int(c) for c in centers[k]]
    hh, ss, vv = cv2.cvtColor(np.uint8([[[b, g, r]]]), cv2.COLOR_BGR2HSV)[0, 0]
    return {"name": _colour_name(int(hh), int(ss), int(vv)), "hex": f"#{r:02x}{g:02x}{b:02x}",
            "share": round(float(counts[k] / counts.sum()), 2), "hivis": _hivis(img, box)}


def _hivis(img, box) -> float:
    """Share of fluorescent (hi-vis) pixels on the torso: a wider/taller region than attire,
    because vests are often open at the front. Fluorescent yellow-lime is very saturated and
    bright at hue ~25-45 (OpenCV scale); fluorescent orange at ~5-18."""
    x, y, w, h = [int(v) for v in box]
    H, W = img.shape[:2]
    x0, x1 = max(0, x - w), min(W, x + 2 * w)
    y0, y1 = min(H, y + int(1.2 * h)), min(H, y + int(3.2 * h))
    if y1 - y0 < 16 or x1 - x0 < 16:
        return 0.0
    hsv = cv2.cvtColor(cv2.resize(img[y0:y1, x0:x1], (64, 48), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2HSV)
    lime = cv2.inRange(hsv, (24, 110, 150), (45, 255, 255))
    orange = cv2.inRange(hsv, (4, 150, 170), (18, 255, 255))
    return round(float(((lime > 0) | (orange > 0)).mean()), 3)


def _cos(a, b) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _best_matches(feat, top=1):
    scores = []
    for subj, vecs in _index.items():
        if not vecs:
            continue
        s = max(_cos(feat, v) for _, v in vecs)
        scores.append((subj, s))
    scores.sort(key=lambda x: -x[1])
    return scores[:top]


# --------------------------------------------------------------------------- routes
async def healthcheck(request):
    return JSONResponse({"status": "OK", "emotion": _emo is not None})


async def get_subjects(request):
    _sync_index()
    return JSONResponse({"subjects": sorted(_index.keys(), key=str.lower)})


async def get_faces(request):
    _sync_index()
    faces = []
    for subj, vecs in _index.items():
        for image_id, _ in vecs:
            faces.append({"image_id": image_id, "subject": subj})
    return JSONResponse({"faces": faces})


async def add_face(request: Request):
    subject = request.query_params.get("subject", "").strip()
    if not subject:
        return JSONResponse({"message": "subject is required"}, status_code=400)
    form = await request.form()
    up = form.get("file")
    if up is None:
        return JSONResponse({"message": "file is required"}, status_code=400)
    img = _decode(await up.read())
    if img is None:
        return JSONResponse({"message": "Invalid image"}, status_code=400)
    faces = _detect_all(img)
    if not faces:
        return JSONResponse({"message": "No face is found in the given image"}, status_code=400)
    _, _, feat = faces[0]
    image_id = uuid.uuid4().hex
    d = os.path.join(FACES, _slug(subject))
    os.makedirs(d, exist_ok=True)
    np.save(os.path.join(d, image_id + ".npy"), feat)
    _index.setdefault(subject, []).append((image_id, feat))
    _touch_stamp()
    return JSONResponse({"image_id": image_id, "subject": subject})


async def recognize(request: Request):
    """Query params (CompreFace-compatible plus two extras):
      extras=0|1               compute emotion + clothing for the top face (default 1)
      extras_min_similarity=x  ...or compute them only when the top match reaches x
    The response carries "timing" (ms per stage) for latency monitoring."""
    t_start = time.perf_counter()
    limit = int(request.query_params.get("limit", 0) or 0)
    pred = int(request.query_params.get("prediction_count", 1) or 1)
    want_extras = request.query_params.get("extras", "1") not in ("0", "false")
    extras_min = request.query_params.get("extras_min_similarity")
    extras_min = float(extras_min) if extras_min else None
    form = await request.form()
    up = form.get("file")
    if up is None:
        return JSONResponse({"message": "file is required"}, status_code=400)
    data = await up.read()
    t_read = time.perf_counter()
    img = _decode(data)
    if img is None:
        return JSONResponse({"message": "Invalid image"}, status_code=400)
    t_decode = time.perf_counter()
    _sync_index()
    timing = {"read": (t_read - t_start) * 1000, "decode": (t_decode - t_read) * 1000}
    faces = _detect_all(img, timing, limit)
    t0 = time.perf_counter()
    result = []
    H, W = img.shape[:2]
    extras_ms = 0.0
    for i, (box, score, feat) in enumerate(faces):
        x, y, w, h = box
        subs = [{"subject": s, "similarity": round(max(0.0, min(1.0, sim)), 5)}
                for s, sim in _best_matches(feat, top=max(1, pred))]
        # extra signals for the largest face only (the person standing at the kiosk)
        top = subs[0]["similarity"] if subs else 0.0
        extras = i == 0 and (want_extras or (extras_min is not None and top >= extras_min))
        te = time.perf_counter()
        emotion = _emotion(img, box) if extras else None
        attire = _attire(img, box) if extras else None
        extras_ms += (time.perf_counter() - te) * 1000
        result.append({
            "box": {"probability": round(score, 4),
                    "x_min": int(x), "y_min": int(y),
                    "x_max": int(x + w), "y_max": int(y + h)},
            "subjects": subs,
            "emotion": emotion,
            "attire": attire,
            "size": round(float(w) / W, 3),
        })
    timing["match"] = (time.perf_counter() - t0) * 1000 - extras_ms
    timing["extras"] = extras_ms
    timing["total"] = (time.perf_counter() - t_start) * 1000
    return JSONResponse({"result": result, "timing": {k: round(v, 2) for k, v in timing.items()}})


async def delete_subject(request: Request):
    name = request.path_params["name"]
    _index.pop(name, None)
    d = os.path.join(FACES, _slug(name))
    if os.path.isdir(d):
        for fn in os.listdir(d):
            os.remove(os.path.join(d, fn))
        os.rmdir(d)
    _touch_stamp()
    return JSONResponse({"deleted": 1})


async def rename_subject(request: Request):
    old = request.path_params["name"]
    new = ((await request.json()).get("subject") or "").strip()
    if not new:
        return JSONResponse({"message": "subject is required"}, status_code=400)
    od, nd = os.path.join(FACES, _slug(old)), os.path.join(FACES, _slug(new))
    if os.path.isdir(od):
        os.rename(od, nd)
    _index[new] = _index.pop(old, [])
    _touch_stamp()
    return JSONResponse({"updated": True})


routes = [
    Route("/healthcheck", healthcheck),
    Route("/api/v1/recognition/subjects", get_subjects, methods=["GET"]),
    Route("/api/v1/recognition/subjects/", get_subjects, methods=["GET"]),
    Route("/api/v1/recognition/faces", get_faces, methods=["GET"]),
    Route("/api/v1/recognition/faces", add_face, methods=["POST"]),
    Route("/api/v1/recognition/recognize", recognize, methods=["POST"]),
    Route("/api/v1/recognition/subjects/{name}", delete_subject, methods=["DELETE"]),
    Route("/api/v1/recognition/subjects/{name}", rename_subject, methods=["PUT"]),
]

app = Starlette(routes=routes)
_sync_index()
