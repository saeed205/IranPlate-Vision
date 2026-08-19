"""
Lazy, thread-safe loading and inference for the detector + OCR models.

Previously app.py owned the two models and *pushed* them into camera_manager
from a thread that busy-waited on a global flag, and nothing serialised access:
Flask request threads and every RTSP worker could call into YOLO / hezar
concurrently, which torch modules do not support and which shows up as garbled
OCR or hard crashes under load. Both callers now share this module and every
inference call is serialised by ``_infer_lock``.
"""
import logging
import os
import threading

import cv2
import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEIGHTS = os.environ.get('PLATE_WEIGHTS', os.path.join(BASE_DIR, 'best.pt'))
OCR_MODEL_ID = os.environ.get(
    'PLATE_OCR_MODEL', 'hezarai/crnn-fa-license-plate-recognition-v2')
DEFAULT_CONF = float(os.environ.get('PLATE_DET_CONF', '0.4'))
CROP_PAD = int(os.environ.get('PLATE_CROP_PAD', '10'))

_det = None
_ocr = None
_error = None
_finished = threading.Event()     # set once the load attempt ends (ok or not)
_load_started = threading.Lock()
_loading = False
_infer_lock = threading.RLock()


def status():
    """Return {'ready': bool, 'error': str|None}. Safe to call at any time."""
    return {'ready': is_ready(), 'error': _error}


def is_ready():
    return _det is not None and _ocr is not None


def wait_ready(timeout=None):
    """Block until the load attempt finished. True if the models are usable."""
    _finished.wait(timeout)
    return is_ready()


def load_async():
    """Kick off model loading exactly once, in the background."""
    global _loading
    with _load_started:
        if _loading:
            return
        _loading = True
    threading.Thread(target=_load, name='model-loader', daemon=True).start()


def _load():
    global _det, _ocr, _error
    try:
        from ultralytics import YOLO
        from hezar.models import Model

        if not os.path.exists(WEIGHTS):
            raise FileNotFoundError(
                'Detector weights not found: %s | فایل وزن‌های مدل یافت نشد' % WEIGHTS)

        log.info('Loading models | در حال بارگذاری مدل‌ها...')
        det = YOLO(WEIGHTS)
        ocr = Model.load(OCR_MODEL_ID)
        _det, _ocr = det, ocr
        log.info('Models are ready | مدل‌ها آماده‌اند.')
    except Exception as exc:            # noqa: BLE001 - surfaced through /status
        _error = '%s: %s' % (type(exc).__name__, exc)
        log.error('Model load failed | خطا در بارگذاری مدل‌ها: %s', _error)
    finally:
        _finished.set()


def _pad_box(box, width, height, pad=CROP_PAD):
    x1, y1, x2, y2 = box
    return (max(0, x1 - pad), max(0, y1 - pad),
            min(width, x2 + pad), min(height, y2 + pad))


def read_plate(crop_bgr):
    """OCR a single BGR crop. Returns the raw model string, or '' on failure."""
    if _ocr is None or crop_bgr is None or crop_bgr.size == 0:
        return ''
    try:
        pil = Image.fromarray(cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB))
        with _infer_lock:
            raw = _ocr.predict(pil)
        if raw:
            first = raw[0]
            text = first.get('text') if isinstance(first, dict) else getattr(first, 'text', '')
            return (text or '').strip()
    except Exception as exc:            # noqa: BLE001 - a bad crop must not kill the worker
        log.debug('OCR failed: %s', exc)
    return ''


def detect_plates(frame_bgr, conf=None, run_ocr=True):
    """
    Detect plates in a BGR frame.

    Returns a list of dicts sorted by descending confidence, each holding the
    keys ``conf``, ``box`` (x1, y1, x2, y2), ``crop`` and ``text``.
    """
    if _det is None or frame_bgr is None or getattr(frame_bgr, 'size', 0) == 0:
        return []

    threshold = DEFAULT_CONF if conf is None else conf
    try:
        with _infer_lock:
            results = _det.predict(source=frame_bgr, conf=threshold, verbose=False)
    except Exception as exc:            # noqa: BLE001
        log.warning('Detection failed: %s', exc)
        return []

    if not results:
        return []

    height, width = frame_bgr.shape[:2]
    out = []
    for box in results[0].boxes:
        try:
            score = float(box.conf[0])
            x1, y1, x2, y2 = _pad_box(map(int, box.xyxy[0]), width, height)
        except (IndexError, TypeError, ValueError):
            continue
        if x2 <= x1 or y2 <= y1:
            continue
        crop = frame_bgr[y1:y2, x1:x2]
        if crop.size == 0:
            continue
        out.append({'conf': score, 'box': (x1, y1, x2, y2), 'crop': crop, 'text': ''})

    out.sort(key=lambda d: d['conf'], reverse=True)

    if run_ocr:
        for det in out:
            det['text'] = read_plate(det['crop'])
    return out


def annotate(frame_bgr, detections):
    """
    Draw the detection boxes on a copy of *frame_bgr*.

    Deliberately hand-drawn instead of ultralytics' ``results.plot()``: that
    helper lazily downloads a TTF font on first use, which fails (or stalls for
    the socket timeout) in an offline container.
    """
    canvas = frame_bgr.copy()
    thickness = max(2, round(min(canvas.shape[:2]) / 320))
    for det in detections:
        x1, y1, x2, y2 = det['box']
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (126, 201, 232), thickness)
        label = '%.0f%%' % (det['conf'] * 100)
        scale = max(0.45, thickness * 0.28)
        (tw, th), base = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
        ty = max(th + base, y1)
        cv2.rectangle(canvas, (x1, ty - th - base), (x1 + tw + 4, ty), (126, 201, 232), -1)
        cv2.putText(canvas, label, (x1 + 2, ty - base),
                    cv2.FONT_HERSHEY_SIMPLEX, scale, (20, 20, 20), 1, cv2.LINE_AA)
    return canvas


def encode_jpeg(image_bgr, quality=92, max_width=None):
    """JPEG-encode a BGR image to raw bytes; b'' when the image is unusable."""
    if image_bgr is None or getattr(image_bgr, 'size', 0) == 0:
        return b''
    if max_width:
        h, w = image_bgr.shape[:2]
        if w > max_width:
            image_bgr = cv2.resize(
                image_bgr, (max_width, max(1, round(h * max_width / w))),
                interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode('.jpg', image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    return buf.tobytes() if ok else b''


def decode_image(raw_bytes):
    """Decode uploaded bytes to a BGR ndarray, or None if empty/unsupported."""
    if not raw_bytes:
        return None
    try:
        arr = np.frombuffer(raw_bytes, np.uint8)
        if arr.size == 0:
            return None
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:                   # noqa: BLE001
        return None
