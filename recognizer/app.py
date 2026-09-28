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
"""
import os, uuid, threading, json
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

_lock = threading.Lock()
_det = cv2.FaceDetectorYN.create(DET_MODEL, "", (320, 320), 0.6, 0.3, 5000)
_rec = cv2.FaceRecognizerSF.create(REC_MODEL, "")

# in-memory index: {subject: [(image_id, np.float32[128]), ...]}
_index: dict = {}


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


def _detect_all(img):
    """Return list of (box[x,y,w,h], score, aligned_feature) sorted by score desc."""
    h, w = img.shape[:2]
    with _lock:
        _det.setInputSize((w, h))
        _, faces = _det.detect(img)
        results = []
        if faces is not None:
            for f in sorted(faces, key=lambda r: -r[-1]):
                aligned = _rec.alignCrop(img, f)
                feat = _rec.feature(aligned).flatten().astype(np.float32).copy()
                results.append((f[:4].tolist(), float(f[-1]), feat))
    return results


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
    return JSONResponse({"status": "OK"})


async def get_subjects(request):
    return JSONResponse({"subjects": sorted(_index.keys(), key=str.lower)})


async def get_faces(request):
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
    return JSONResponse({"image_id": image_id, "subject": subject})


async def recognize(request: Request):
    limit = int(request.query_params.get("limit", 0) or 0)
    pred = int(request.query_params.get("prediction_count", 1) or 1)
    form = await request.form()
    up = form.get("file")
    if up is None:
        return JSONResponse({"message": "file is required"}, status_code=400)
    img = _decode(await up.read())
    if img is None:
        return JSONResponse({"message": "Invalid image"}, status_code=400)
    faces = _detect_all(img)
    if limit and limit > 0:
        faces = faces[:limit]
    result = []
    for box, score, feat in faces:
        x, y, w, h = box
        subs = [{"subject": s, "similarity": round(max(0.0, min(1.0, sim)), 5)}
                for s, sim in _best_matches(feat, top=max(1, pred))]
        result.append({
            "box": {"probability": round(score, 4),
                    "x_min": int(x), "y_min": int(y),
                    "x_max": int(x + w), "y_max": int(y + h)},
            "subjects": subs,
        })
    return JSONResponse({"result": result})


async def delete_subject(request: Request):
    name = request.path_params["name"]
    _index.pop(name, None)
    d = os.path.join(FACES, _slug(name))
    if os.path.isdir(d):
        for fn in os.listdir(d):
            os.remove(os.path.join(d, fn))
        os.rmdir(d)
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
_load_index()
