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
Employee face vectors are stored under /data, each with a small face-crop JPEG (the face only, no
background) so admins can see what was enrolled. Visitor frames are never kept.

Daily learning (POST /recognize?learn=1): when a staff member is matched confidently, the
recognizer keeps the DAILY_BEST (2) best-quality face crops + vectors of that person per day and
drops them after DAILY_KEEP_DAYS (7). They are matched alongside the enrolled photos, so gradual
changes (beard, glasses, haircut, weight) keep being recognised. To stop drift or poisoning, a frame
is only learned when it matches the person clearly (LEARN_MIN_SIMILARITY), beats every other person by
LEARN_MARGIN, and still resembles their *enrolled* photos (LEARN_ANCHOR_SIMILARITY).

Extra routes:  GET /faces?subject=NAME or ?details=1 (kind/day/quality per photo),
               GET /faces/{image_id}/img?subject=NAME (face crop JPEG),
               DELETE /faces/{image_id}?subject=NAME

Each recognised face also carries two on-device signals, computed from the same frame and
then discarded with it:
  * emotion  — FER+ (ONNX model zoo, 8 classes) on the face crop
  * attire   — dominant clothing colour from the region below the chin, plus `hivis`: the share
               of fluorescent yellow/lime/orange pixels (a high-visibility vest) in the torso area

Liveness (anti-spoofing): MiniFASNet (Silent-Face-Anti-Spoofing, two models at 2.7x and 4x the face box)
scores how likely the top face is a real person rather than a photo, print or screen held up to the
camera: "liveness" 0..1 (probability of "real", averaged over both models). It only runs when the top
match reaches ?liveness_min_similarity= (the attendance app passes its match threshold), so frames of
visitors and empty scenes cost nothing extra. With ?learn_min_liveness=x a frame scoring below x is
never learned as a daily best, so a photo can't poison someone's reference photos.
"""
import os, re, uuid, threading, json, time, fcntl
from datetime import date, datetime, timedelta
import numpy as np
import cv2
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
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
# liveness: [(face-box zoom, net)]; empty if the models are missing (liveness is then reported as None)
LIVENESS_MODELS = os.environ.get("LIVENESS_MODELS", "2.7:/models/liveness_2.7.onnx,4.0:/models/liveness_4.0.onnx")
_live = [(float(z), cv2.dnn.readNetFromONNX(p)) for z, p in
         (item.split(":", 1) for item in LIVENESS_MODELS.split(",") if ":" in item) if os.path.exists(p)]
# daily learning (see module docstring)
DAILY_BEST = int(os.environ.get("DAILY_BEST", "2"))
DAILY_KEEP_DAYS = int(os.environ.get("DAILY_KEEP_DAYS", "7"))
LEARN_MIN = float(os.environ.get("LEARN_MIN_SIMILARITY", "0.45"))
LEARN_ANCHOR = float(os.environ.get("LEARN_ANCHOR_SIMILARITY", "0.28"))
LEARN_MARGIN = float(os.environ.get("LEARN_MARGIN", "0.08"))
LEARN_COOLDOWN = float(os.environ.get("LEARN_COOLDOWN_SECONDS", "15"))
_ID_RE = re.compile(r"^(d\d{8}-)?[0-9a-f]{32}$")  # enrolled: <hex>; learned: d<YYYYMMDD>-<hex>
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


_purged_day = None


def _sync_index():
    global _loaded_at, _purged_day
    if _purged_day != date.today():
        _purged_day = date.today()
        _purge_daily()
    m = _stamp_mtime()
    if m != _loaded_at:
        _load_index()
        _loaded_at = m


_dir_mtime: dict = {}  # subject dir -> mtime when this worker last read it


def _load_index():
    """Re-read only the subject folders that changed (learning adds files several times a day)."""
    seen = set()
    for subj in os.listdir(FACES):
        d = os.path.join(FACES, subj)
        if not os.path.isdir(d):
            continue
        name = _unslug(subj)
        seen.add(name)
        m = os.stat(d).st_mtime
        if _dir_mtime.get(name) == m and name in _index:
            continue
        vecs = []
        for fn in os.listdir(d):
            if fn.endswith(".npy"):
                try:
                    vecs.append((fn[:-4], np.load(os.path.join(d, fn))))
                except Exception:
                    pass
        _index[name] = vecs
        _dir_mtime[name] = m
    for name in list(_index):
        if name not in seen:
            _index.pop(name, None)
            _dir_mtime.pop(name, None)


def _is_learned(image_id: str) -> bool:
    return image_id[:1] == "d" and image_id[9:10] == "-"


def _day_of(image_id: str):
    """Learned photos carry their day in the id; enrolled photos return None."""
    if _is_learned(image_id):
        try:
            return datetime.strptime(image_id[1:9], "%Y%m%d").date()
        except ValueError:
            return None
    return None


def _purge_daily():
    """Drop learned photos older than DAILY_KEEP_DAYS (today counts as day 1)."""
    cut = date.today() - timedelta(days=DAILY_KEEP_DAYS - 1)
    removed = False
    for subj in os.listdir(FACES):
        d = os.path.join(FACES, subj)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            day = _day_of(fn.split(".")[0])
            if day is not None and day < cut:
                with _quiet():
                    os.remove(os.path.join(d, fn))
                    removed = True
    if removed:
        _touch_stamp()


class _quiet:
    """Ignore missing files: several workers may purge/replace the same file at once."""
    def __enter__(self):
        return self

    def __exit__(self, et, e, tb):
        return et is not None and issubclass(et, FileNotFoundError)


def _slug(name: str) -> str:
    return name.replace("/", "_")


def _subject_dir(name: str) -> str:
    d = os.path.realpath(os.path.join(FACES, _slug(name)))
    if os.path.dirname(d) != os.path.realpath(FACES):
        raise ValueError("bad subject")
    return d


def _unslug(s: str) -> str:
    return s


def _decode(data: bytes):
    arr = np.frombuffer(data, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _detect_all(img, timing: dict = None, limit: int = 0):
    """Return list of (box[x,y,w,h], score, aligned_feature, raw_detection) sorted by score desc.
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
                results.append((f[:4].tolist(), float(f[-1]), feat, f))
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


def _liveness_crop(img, box, zoom, size=80):
    """The face box scaled by `zoom` around its centre, shifted (not clipped) to stay inside the frame,
    resized to size x size: the crop MiniFASNet was trained on."""
    H, W = img.shape[:2]
    x, y, w, h = [float(v) for v in box]
    if w <= 0 or h <= 0:
        return None
    zoom = min((H - 1) / h, (W - 1) / w, zoom)
    nw, nh = w * zoom, h * zoom
    cx, cy = x + w / 2, y + h / 2
    x0, y0, x1, y1 = cx - nw / 2, cy - nh / 2, cx + nw / 2, cy + nh / 2
    if x0 < 0:
        x1 -= x0; x0 = 0
    if y0 < 0:
        y1 -= y0; y0 = 0
    if x1 > W - 1:
        x0 -= x1 - W + 1; x1 = W - 1
    if y1 > H - 1:
        y0 -= y1 - H + 1; y1 = H - 1
    crop = img[int(y0):int(y1) + 1, int(x0):int(x1) + 1]
    if crop.size == 0:
        return None
    return cv2.resize(crop, (size, size))


def _liveness(img, box):
    """Probability (0..1) that the face is a live person, not a photo/screen; None if unavailable."""
    if not _live:
        return None
    total, n = 0.0, 0
    for zoom, net in _live:
        crop = _liveness_crop(img, box, zoom)
        if crop is None:
            continue
        blob = crop.astype(np.float32).transpose(2, 0, 1)[None]  # BGR, 0..255, NCHW (as trained)
        with _lock:
            net.setInput(blob)
            logits = net.forward().flatten()
        p = np.exp(logits - logits.max())
        total += float(p[1] / p.sum())  # classes: 0 spoof (print), 1 real, 2 spoof (screen)
        n += 1
    return round(total / n, 4) if n else None


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
    """[(subject, best similarity over all photos, best over enrolled photos only)], best first."""
    scores = []
    for subj, vecs in _index.items():
        if not vecs:
            continue
        best, base = 0.0, 0.0
        for image_id, v in vecs:
            s = _cos(feat, v)
            best = max(best, s)
            if not _is_learned(image_id):
                base = max(base, s)
        scores.append((subj, best, base))
    scores.sort(key=lambda x: -x[1])
    return scores[:top]


def _face_crop(img, box, size=224):
    """Square face crop with a little margin, for the admin gallery (never the whole frame)."""
    x, y, w, h = [int(v) for v in box]
    H, W = img.shape[:2]
    side = int(max(w, h) * 1.4)
    cx, cy = x + w // 2, y + h // 2
    x0, y0 = max(0, cx - side // 2), max(0, cy - side // 2)
    x1, y1 = min(W, x0 + side), min(H, y0 + side)
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    s = size / max(crop.shape[:2])
    if s < 1:
        crop = cv2.resize(crop, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else None


def _quality(img, f) -> float:
    """0..1 score for how useful a frame is as a reference photo: sharp, large, frontal,
    well lit and confidently detected. Deliberately ignores similarity so that a good photo of a
    changed look is not penalised."""
    x, y, w, h = [float(v) for v in f[:4]]
    size = min(1.0, w / 160.0)
    xi, yi = max(0, int(x)), max(0, int(y))
    face = img[yi:yi + int(h), xi:xi + int(w)]
    if face.size == 0:
        return 0.0
    gray = cv2.cvtColor(cv2.resize(face, (112, 112)), cv2.COLOR_BGR2GRAY)
    sharp = min(1.0, cv2.Laplacian(gray, cv2.CV_64F).var() / 250.0)
    mean = float(gray.mean())
    light = max(0.0, 1.0 - abs(mean - 125) / 90.0)
    # yaw proxy from YuNet landmarks: nose tip should sit midway between the eyes
    rex, lex, nx = float(f[4]), float(f[6]), float(f[8])
    eye = abs(lex - rex) or 1.0
    frontal = max(0.0, 1.0 - abs(nx - (rex + lex) / 2) / (eye / 2))
    det = float(f[-1])
    return round(det * (0.35 + 0.65 * size) * (0.3 + 0.7 * sharp) * (0.4 + 0.6 * light) * (0.3 + 0.7 * frontal), 4)


_last_learn: dict = {}  # subject -> time this worker last considered a frame


def _maybe_learn(img, box, f, feat, subs):
    """Keep this frame as one of today's DAILY_BEST reference photos for the matched person,
    if it is a confident, unambiguous match and better than what is already kept for today."""
    if not subs:
        return None
    subj, sim, base = subs[0]
    second = subs[1][1] if len(subs) > 1 else 0.0
    if sim < LEARN_MIN or sim - second < LEARN_MARGIN or base < LEARN_ANCHOR:
        return None
    now = time.time()
    if now - _last_learn.get(subj, 0) < LEARN_COOLDOWN:
        return None
    q = _quality(img, f)
    if q < 0.25:
        return None
    _last_learn[subj] = now
    try:
        d = _subject_dir(subj)
    except ValueError:
        return None
    if not os.path.isdir(d):
        return None
    tag = "d" + date.today().strftime("%Y%m%d")
    with open(os.path.join(d, ".lock"), "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        kept = []  # (quality, image_id, vector)
        for fn in os.listdir(d):
            if fn.startswith(tag) and fn.endswith(".json"):
                iid = fn[:-5]
                try:
                    with open(os.path.join(d, fn)) as fh:
                        meta = json.load(fh)
                    kept.append((meta.get("quality", 0), iid, np.load(os.path.join(d, iid + ".npy"))))
                except Exception:
                    pass
        # a near-identical frame replaces its twin rather than taking a second slot (keeps variety)
        twin = next((k for k in kept if _cos(feat, k[2]) > 0.93), None)
        if twin is not None:
            drop = twin if q > twin[0] else None
        elif len(kept) >= DAILY_BEST:
            worst = min(kept, key=lambda k: k[0])
            drop = worst if q > worst[0] else None
        else:
            drop = ()
        if drop is None:
            return None
        iid = f"{tag}-{uuid.uuid4().hex}"
        jpg = _face_crop(img, box)
        if jpg:
            with open(os.path.join(d, iid + ".jpg"), "wb") as fh:
                fh.write(jpg)
        with open(os.path.join(d, iid + ".json"), "w") as fh:
            json.dump({"quality": q, "similarity": round(sim, 4), "ts": datetime.now().isoformat(timespec="seconds")}, fh)
        np.save(os.path.join(d, iid + ".npy"), feat)
        if drop:
            for ext in (".npy", ".jpg", ".json"):
                with _quiet():
                    os.remove(os.path.join(d, drop[1] + ext))
    _touch_stamp()
    return iid


# --------------------------------------------------------------------------- routes
async def healthcheck(request):
    return JSONResponse({"status": "OK", "emotion": _emo is not None, "liveness": bool(_live)})


async def get_subjects(request):
    _sync_index()
    return JSONResponse({"subjects": sorted(_index.keys(), key=str.lower)})


async def get_faces(request):
    """All photos, or one person's (?subject=) with details: kind is "enrolled" or "learned"
    (a daily best from the kiosk, with its day, quality and time). ?details=1 adds the same details
    to the full listing; enrolled photos then also carry "ts" (when the crop was saved)."""
    _sync_index()
    only = request.query_params.get("subject")
    details = only is not None or request.query_params.get("details") in ("1", "true")
    faces = []
    for subj, vecs in _index.items():
        if only is not None and subj != only:
            continue
        for image_id, _ in vecs:
            day = _day_of(image_id)
            item = {"image_id": image_id, "subject": subj, "kind": "learned" if day else "enrolled"}
            if details:
                d = _subject_dir(subj)
                jpg = os.path.join(d, image_id + ".jpg")
                item["has_image"] = os.path.exists(jpg)
                if item["has_image"] and not day:
                    item["ts"] = datetime.fromtimestamp(os.path.getmtime(jpg)).isoformat(timespec="seconds")
                if day:
                    item["day"] = day.isoformat()
                    try:
                        with open(os.path.join(d, image_id + ".json")) as fh:
                            item.update(json.load(fh))
                    except Exception:
                        pass
            faces.append(item)
    return JSONResponse({"faces": faces})


def _photo_path(request: Request, ext: str):
    image_id = request.path_params["image_id"]
    if not _ID_RE.match(image_id):
        return None
    try:
        return os.path.join(_subject_dir(request.query_params.get("subject", "")), image_id + ext)
    except ValueError:
        return None


async def face_image(request: Request):
    p = _photo_path(request, ".jpg")
    if not p or not os.path.exists(p):
        return JSONResponse({"message": "No image for this photo"}, status_code=404)
    with open(p, "rb") as fh:
        return Response(fh.read(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


async def delete_face(request: Request):
    p = _photo_path(request, ".npy")
    if not p or not os.path.exists(p):
        return JSONResponse({"message": "Photo not found"}, status_code=404)
    for ext in (".npy", ".jpg", ".json"):
        with _quiet():
            os.remove(p[:-4] + ext)
    _touch_stamp()
    return JSONResponse({"deleted": 1})


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
    box, _, feat, _ = faces[0]
    image_id = uuid.uuid4().hex
    try:
        d = _subject_dir(subject)
    except ValueError:
        return JSONResponse({"message": "Invalid subject name"}, status_code=400)
    os.makedirs(d, exist_ok=True)
    jpg = _face_crop(img, box)
    if jpg:
        with open(os.path.join(d, image_id + ".jpg"), "wb") as fh:
            fh.write(jpg)
    np.save(os.path.join(d, image_id + ".npy"), feat)
    _index.setdefault(subject, []).append((image_id, feat))
    _touch_stamp()
    return JSONResponse({"image_id": image_id, "subject": subject})


async def recognize(request: Request):
    """Query params (CompreFace-compatible plus two extras):
      extras=0|1               compute emotion + clothing for the top face (default 1)
      extras_min_similarity=x  ...or compute them only when the top match reaches x
      learn=0|1                keep the frame as a daily best for the matched person (see top)
      liveness_min_similarity=x  score liveness for the top face when its top match reaches x
      learn_min_liveness=x     never learn a frame whose liveness is below x (or unknown)
    The response carries "timing" (ms per stage) for latency monitoring."""
    t_start = time.perf_counter()
    limit = int(request.query_params.get("limit", 0) or 0)
    pred = int(request.query_params.get("prediction_count", 1) or 1)
    want_extras = request.query_params.get("extras", "1") not in ("0", "false")
    extras_min = request.query_params.get("extras_min_similarity")
    extras_min = float(extras_min) if extras_min else None
    learn = request.query_params.get("learn", "0") in ("1", "true")
    live_min_sim = request.query_params.get("liveness_min_similarity")
    live_min_sim = float(live_min_sim) if live_min_sim else None
    learn_min_live = request.query_params.get("learn_min_liveness")
    learn_min_live = float(learn_min_live) if learn_min_live else None
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
    learned = None
    for i, (box, score, feat, raw) in enumerate(faces):
        x, y, w, h = box
        matches = _best_matches(feat, top=max(2, pred))
        subs = [{"subject": s, "similarity": round(max(0.0, min(1.0, sim)), 5)}
                for s, sim, _ in matches[:max(1, pred)]]
        # extra signals for the largest face only (the person standing at the kiosk)
        top = subs[0]["similarity"] if subs else 0.0
        extras = i == 0 and (want_extras or (extras_min is not None and top >= extras_min))
        te = time.perf_counter()
        emotion = _emotion(img, box) if extras else None
        attire = _attire(img, box) if extras else None
        extras_ms += (time.perf_counter() - te) * 1000
        live = None
        if i == 0 and live_min_sim is not None and top >= live_min_sim:
            tv = time.perf_counter()
            live = _liveness(img, box)
            timing["liveness"] = (time.perf_counter() - tv) * 1000
        if learn and i == 0 and (learn_min_live is None or (live is not None and live >= learn_min_live)):
            tl = time.perf_counter()
            try:
                learned = _maybe_learn(img, box, raw, feat, matches)
            except OSError:
                learned = None
            timing["learn"] = (time.perf_counter() - tl) * 1000
        result.append({
            "box": {"probability": round(score, 4),
                    "x_min": int(x), "y_min": int(y),
                    "x_max": int(x + w), "y_max": int(y + h)},
            "subjects": subs,
            "emotion": emotion,
            "attire": attire,
            "liveness": live,
            "size": round(float(w) / W, 3),
        })
    timing["match"] = (time.perf_counter() - t0) * 1000 - extras_ms - timing.get("liveness", 0)
    timing["extras"] = extras_ms
    timing["total"] = (time.perf_counter() - t_start) * 1000
    return JSONResponse({"result": result, "learned": learned, "timing": {k: round(v, 2) for k, v in timing.items()}})


async def delete_subject(request: Request):
    name = request.path_params["name"]
    _index.pop(name, None)
    try:
        d = _subject_dir(name)
    except ValueError:
        return JSONResponse({"message": "Invalid subject name"}, status_code=400)
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
    try:
        od, nd = _subject_dir(old), _subject_dir(new)
    except ValueError:
        return JSONResponse({"message": "Invalid subject name"}, status_code=400)
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
    Route("/api/v1/recognition/faces/{image_id}/img", face_image, methods=["GET"]),
    Route("/api/v1/recognition/faces/{image_id}", delete_face, methods=["DELETE"]),
    Route("/api/v1/recognition/recognize", recognize, methods=["POST"]),
    Route("/api/v1/recognition/subjects/{name}", delete_subject, methods=["DELETE"]),
    Route("/api/v1/recognition/subjects/{name}", rename_subject, methods=["PUT"]),
]

app = Starlette(routes=routes)
_sync_index()
