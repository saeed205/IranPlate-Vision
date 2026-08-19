"""
IranPlate Vision — Flask application.

Fixes over the first version:
  * uploads are size-capped and every error path returns JSON (an exception used
    to hand an HTML 500 page to fetch() callers, which then failed on .json());
  * ``/detect`` reported the confidence of one box and the OCR text of another;
    both now come from the same detection;
  * the province lookup was a full scan of plate_data.json per request *and*
    compared ``ه`` against the data's ``ھ``, so a third of the letters never
    resolved. It is now an index built once, over canonical letters;
  * camera PUT trusted ``d['name']`` / ``d['url']`` / ``d['role']`` and crashed
    with a KeyError, and dereferenced a possibly-missing row;
  * RTSP URLs (which usually embed a password) are masked in API responses;
  * the stored detection crops are reachable instead of being dead weight.
"""
import atexit
import base64
import json
import logging
import os
import queue
import re

import cv2
from flask import (Flask, Response, jsonify, make_response, render_template,
                   request, send_from_directory, stream_with_context)
from werkzeug.exceptions import HTTPException

import camera_manager as cm
import db
import models
import plates

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=os.environ.get('PLATE_LOG_LEVEL', 'INFO').upper(),
    format='%(asctime)s %(levelname)-7s %(name)s: %(message)s')
log = logging.getLogger('app')

MAX_UPLOAD_MB = int(os.environ.get('PLATE_MAX_UPLOAD_MB', '16'))
MAX_IMAGE_SIDE = int(os.environ.get('PLATE_MAX_IMAGE_SIDE', '1920'))

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, 'templates'))
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024
app.config['JSON_AS_ASCII'] = False

PLATE_DATA_PATH = os.path.join(BASE_DIR, 'plate_data.json')
try:
    with open(PLATE_DATA_PATH, encoding='utf-8') as f:
        PLATE_DATA = json.load(f)
except (OSError, ValueError) as exc:
    raise SystemExit('Cannot read %s: %s | خواندن داده‌های پلاک ممکن نشد'
                     % (PLATE_DATA_PATH, exc))


# ── plate_data.json indexes (built once, canonical letters) ───────────────────
def _build_indexes():
    type_by_letter = {}
    for t in PLATE_DATA.get('carplate_types', []):
        for raw in t.get('letters', []):
            type_by_letter[plates.canonical_letter(raw)] = t

    by_code_letter = {}
    by_code = {}
    for province, cities in PLATE_DATA.get('carplates', {}).items():
        for city, codes in cities.items():
            for code, letters in codes.items():
                place = (province, city)
                by_code.setdefault(code, []).append(place)
                if letters:
                    for raw in letters:
                        key = (code, plates.canonical_letter(raw))
                        by_code_letter.setdefault(key, []).append(place)
                else:
                    # No letter restriction: valid for every letter.
                    by_code_letter.setdefault((code, None), []).append(place)
    return type_by_letter, by_code_letter, by_code


TYPE_BY_LETTER, PLACES_BY_CODE_LETTER, PLACES_BY_CODE = _build_indexes()
MAX_LOCATIONS = 3


def lookup_plate(letter, suffix):
    """Vehicle type + up to MAX_LOCATIONS matching cities for a plate."""
    letter = plates.canonical_letter(letter)
    suffix = plates.to_en_digits(suffix)

    matches = []
    seen = set()
    for key in ((suffix, letter), (suffix, None)):
        for place in PLACES_BY_CODE_LETTER.get(key, ()):
            if place not in seen:
                seen.add(place)
                matches.append(place)

    if not matches:
        # Fall back to the code alone, as the original did.
        for place in PLACES_BY_CODE.get(suffix, ()):
            if place not in seen:
                seen.add(place)
                matches.append(place)

    return {
        'vehicle_type': TYPE_BY_LETTER.get(letter),
        'locations': [{'province': p, 'city': c} for p, c in matches[:MAX_LOCATIONS]],
    }


def describe_plate(raw_text):
    """Full plate_info payload for an OCR string, or None when unparseable."""
    parsed = plates.parse(raw_text)
    if not parsed:
        return None
    info = lookup_plate(parsed['letter'], parsed['suffix'])
    info.update({
        'prefix': parsed['prefix'],
        'letter': parsed['letter'],
        'middle': parsed['middle'],
        'suffix': parsed['suffix'],
        'canonical': parsed['canonical'],
    })
    vt = info.get('vehicle_type')
    if vt:
        info['vehicle_type'] = {
            'type': vt.get('type', ''),
            'id': vt.get('id', ''),
            'bg': vt.get('bg', '#ddd'),
            'color': vt.get('color', '#1d1d1b'),
        }
    return info


# ── URL handling for cameras ─────────────────────────────────────────────────
ALLOWED_SCHEMES = ('rtsp://', 'rtsps://', 'http://', 'https://')
_CRED_RE = re.compile(r'^(?P<scheme>\w+://)(?P<user>[^/@:]+)(?::(?P<pw>[^/@]*))?@(?P<rest>.+)$')


# Local file sources are refused by default: the app ships without any
# authentication, so an arbitrary path would let any visitor stream the contents
# of a file back through the snapshot endpoint. Set PLATE_ALLOW_LOCAL_SOURCES=1
# to allow them when testing against a recorded clip.
ALLOW_LOCAL_SOURCES = os.environ.get('PLATE_ALLOW_LOCAL_SOURCES', '').strip().lower()     in ('1', 'true', 'yes')


def validate_camera_url(url):
    """Return (ok, error_message). Accepts RTSP/HTTP streams or a device index."""
    url = (url or '').strip()
    if not url:
        return False, 'url is required | آدرس الزامی است'
    if len(url) > 2048:
        return False, 'url is too long | آدرس بیش از حد طولانی است'
    if url.isdigit():
        return True, ''
    if url.lower().startswith(ALLOWED_SCHEMES):
        return True, ''
    if ALLOW_LOCAL_SOURCES:
        return True, ''
    return False, ('url must start with rtsp://, rtsps://, http:// or https:// '
                   '| آدرس باید با rtsp:// یا http:// شروع شود')


def mask_url(url):
    """Hide the password in a stream URL so it is not echoed to every client."""
    m = _CRED_RE.match(url or '')
    if not m or m.group('pw') is None:
        return url
    return '%s%s:%s@%s' % (m.group('scheme'), m.group('user'), '•' * 6, m.group('rest'))


def has_credentials(url):
    m = _CRED_RE.match(url or '')
    return bool(m and m.group('pw'))


def public_camera(cam, state=None):
    """DB row → API shape, with the password masked."""
    state = state or {}
    return {
        'id': cam['id'],
        'name': cam['name'],
        'url': mask_url(cam['url']),
        'has_credentials': has_credentials(cam['url']),
        'role': cam['role'] if cam['role'] in db.VALID_ROLES else 'entry',
        'enabled': bool(cam['enabled']),
        'created': cam.get('created'),
        'running': bool(state.get('running')),
        'connected': bool(state.get('connected')),
        'connecting': bool(state.get('connecting')),
        'last_error': state.get('last_error') or '',
        'last_frame_age': state.get('last_frame_age'),
    }


def body():
    """Parsed JSON body, always a dict (never raises on a malformed payload)."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


# ── error handlers: always JSON for /api and /detect ─────────────────────────
def _wants_json():
    return (request.path.startswith('/api/') or request.path == '/detect'
            or request.accept_mimetypes.best == 'application/json')


ERROR_MESSAGES = {
    400: 'Bad request | درخواست نامعتبر',
    403: 'Forbidden | دسترسی مجاز نیست',
    404: 'Not found | یافت نشد',
    405: 'Method not allowed | متد مجاز نیست',
    413: 'File too large (max %d MB) | فایل بیش از حد بزرگ است' % MAX_UPLOAD_MB,
    415: 'Unsupported media type | نوع فایل پشتیبانی نمی‌شود',
    500: 'Internal server error | خطای داخلی سرور',
}


@app.errorhandler(HTTPException)
def _http_error(err):
    """Keep Flask's own status/HTML for pages, but answer the API in JSON."""
    if not _wants_json():
        return err
    code = err.code or 500
    return jsonify({'error': ERROR_MESSAGES.get(code, err.description or str(err))}), code


@app.errorhandler(Exception)
def _unhandled(err):
    log.exception('Unhandled error on %s %s', request.method, request.path)
    if not _wants_json():
        # Let Flask render its standard 500 page rather than re-raising here.
        return make_response(ERROR_MESSAGES[500], 500)
    return jsonify({'error': ERROR_MESSAGES[500]}), 500


# ── static / pages ───────────────────────────────────────────────────────────
def _no_store(resp):
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@app.route('/fonts/<path:filename>')
def serve_font(filename):
    resp = send_from_directory(os.path.join(BASE_DIR, 'fonts'), filename)
    resp.headers['Cache-Control'] = 'public, max-age=604800'
    return resp


@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory(os.path.join(BASE_DIR, 'static'), filename)


@app.route('/favicon.ico')
def favicon():
    """Answer the browser's automatic request instead of logging a 404 per page."""
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
           "<rect width='64' height='64' rx='14' fill='#6c63ff'/>"
           "<text x='32' y='44' font-size='34' text-anchor='middle'>🚗</text></svg>")
    resp = make_response(svg)
    resp.headers['Content-Type'] = 'image/svg+xml'
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@app.route('/')
def menu():
    return _no_store(make_response(render_template('menu.html')))


@app.route('/scan')
def index():
    return _no_store(make_response(render_template('index.html')))


@app.route('/cameras')
def cameras_page():
    return _no_store(make_response(render_template('cameras.html')))


@app.route('/status')
def status():
    st = models.status()
    st['cameras'] = len(cm.worker_status())
    st['clients'] = cm.subscriber_count()
    # Surfaced because it relaxes camera-source validation.
    st['allow_local_sources'] = ALLOW_LOCAL_SOURCES
    return jsonify(st)


@app.route('/health')
def health():
    st = models.status()
    payload = {'ok': bool(st['ready']), 'models': st, 'db': db.stats()}
    return jsonify(payload), 200 if st['ready'] else 503


# ── detection ────────────────────────────────────────────────────────────────
@app.route('/detect', methods=['POST'])
def detect():
    if not models.is_ready():
        err = models.status()['error']
        msg = err or 'Models are still loading | مدل‌ها هنوز در حال بارگذاری هستند'
        return jsonify({'error': msg, 'loading': not bool(err)}), 503

    upload = request.files.get('image')
    if upload is None:
        return jsonify({'error': 'No image uploaded | تصویری ارسال نشده'}), 400

    img = models.decode_image(upload.read())
    if img is None:
        return jsonify({'error': 'Unsupported image format | فرمت تصویر پشتیبانی نمی‌شود'}), 400

    # Very large uploads cost a lot of memory for no accuracy gain.
    h, w = img.shape[:2]
    if max(h, w) > MAX_IMAGE_SIDE:
        scale = MAX_IMAGE_SIDE / float(max(h, w))
        img = cv2.resize(img, (max(1, round(w * scale)), max(1, round(h * scale))),
                         interpolation=cv2.INTER_AREA)

    detections = models.detect_plates(img)

    # Pair the reported text and confidence: prefer the highest-confidence box
    # whose OCR output is a valid plate, then any box with text at all.
    chosen = next((d for d in detections if plates.is_valid(d['text'])), None)
    if chosen is None:
        chosen = next((d for d in detections if d['text']), None)

    ocr_text = chosen['text'] if chosen else ''
    plate_info = describe_plate(ocr_text) if ocr_text else None

    annotated = models.annotate(img, detections) if detections else img
    img_b64 = base64.b64encode(models.encode_jpeg(annotated, quality=88)).decode('ascii')

    crop_b64 = ''
    if chosen is not None:
        raw = models.encode_jpeg(chosen['crop'], quality=92, max_width=640)
        if raw:
            crop_b64 = base64.b64encode(raw).decode('ascii')

    return jsonify({
        'image': img_b64,
        'crop': crop_b64,
        'plates_found': len(detections),
        'best_conf': round(chosen['conf'], 4) if chosen else (
            round(detections[0]['conf'], 4) if detections else 0.0),
        'ocr_text': ocr_text,
        'plate': plate_info['canonical'] if plate_info else '',
        'plate_info': plate_info,
        'all_plates': [
            {'conf': round(d['conf'], 4), 'text': d['text'],
             'plate': plates.normalize(d['text'])}
            for d in detections
        ],
    })


# ── Camera CRUD ──────────────────────────────────────────────────────────────
@app.route('/api/cameras', methods=['GET'])
def api_cameras_list():
    states = cm.worker_status()
    return jsonify([public_camera(c, states.get(c['id'])) for c in db.cameras_all()])


@app.route('/api/cameras', methods=['POST'])
def api_camera_add():
    d = body()
    name = str(d.get('name', '')).strip()
    url = str(d.get('url', '')).strip()
    role = str(d.get('role', 'entry')).strip()

    if not name:
        return jsonify({'error': 'name is required | نام الزامی است'}), 400
    if len(name) > 120:
        return jsonify({'error': 'name is too long | نام بیش از حد طولانی است'}), 400
    ok, err = validate_camera_url(url)
    if not ok:
        return jsonify({'error': err}), 400
    if role not in db.VALID_ROLES:
        return jsonify({'error': 'role must be entry, exit or monitor | نقش نامعتبر است'}), 400

    cid = cm.add_camera(name, url, role)
    cam = db.camera_get(cid)
    return jsonify(public_camera(cam, cm.worker_status().get(cid))), 201


@app.route('/api/cameras/<int:cid>', methods=['GET'])
def api_camera_get(cid):
    cam = db.camera_get(cid)
    if not cam:
        return jsonify({'error': 'Camera not found | دوربین یافت نشد'}), 404
    return jsonify(public_camera(cam, cm.worker_status().get(cid)))


@app.route('/api/cameras/<int:cid>', methods=['PUT'])
def api_camera_update(cid):
    existing = db.camera_get(cid)
    if not existing:
        return jsonify({'error': 'Camera not found | دوربین یافت نشد'}), 404

    d = body()
    name = str(d.get('name', existing['name'])).strip()
    url = str(d.get('url', existing['url'])).strip()
    role = str(d.get('role', existing['role'])).strip()
    enabled = bool(d.get('enabled', existing['enabled']))

    # The client only ever sees the masked URL; sending it back means "unchanged".
    if url == mask_url(existing['url']):
        url = existing['url']

    if not name:
        return jsonify({'error': 'name is required | نام الزامی است'}), 400
    ok, err = validate_camera_url(url)
    if not ok:
        return jsonify({'error': err}), 400
    if role not in db.VALID_ROLES:
        return jsonify({'error': 'role must be entry, exit or monitor | نقش نامعتبر است'}), 400

    db.camera_update(cid, name, url, role, enabled)
    cm.restart_worker(cid)
    return jsonify(public_camera(db.camera_get(cid), cm.worker_status().get(cid)))


@app.route('/api/cameras/<int:cid>', methods=['DELETE'])
def api_camera_delete(cid):
    if not db.camera_get(cid):
        return jsonify({'error': 'Camera not found | دوربین یافت نشد'}), 404
    cm.remove_camera(cid)
    return jsonify({'ok': True})


@app.route('/api/cameras/<int:cid>/toggle', methods=['POST'])
def api_camera_toggle(cid):
    if not db.camera_get(cid):
        return jsonify({'error': 'Camera not found | دوربین یافت نشد'}), 404
    enabled = bool(body().get('enabled', True))
    cm.set_enabled(cid, enabled)
    return jsonify(public_camera(db.camera_get(cid), cm.worker_status().get(cid)))


@app.route('/api/cameras/<int:cid>/snapshot')
def api_camera_snapshot(cid):
    snap, age = cm.get_snapshot(cid)
    if not snap:
        return jsonify({'error': 'No snapshot available | تصویری موجود نیست'}), 404
    return jsonify({'image': snap, 'age': round(age, 2) if age is not None else None})


# ── SSE event stream ─────────────────────────────────────────────────────────
HEARTBEAT_SECONDS = 20


@app.route('/api/events')
def api_events():
    q = cm.subscribe()

    def generate():
        try:
            # retry: tells the browser how long to wait before reconnecting.
            yield 'retry: 3000\n\n'
            yield 'data: %s\n\n' % json.dumps({'type': 'hello'}, ensure_ascii=False)
            while True:
                try:
                    evt = q.get(timeout=HEARTBEAT_SECONDS)
                    yield 'data: %s\n\n' % json.dumps(evt, ensure_ascii=False)
                except Exception:                        # queue.Empty
                    yield ': heartbeat\n\n'
        finally:
            cm.unsubscribe(q)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache, no-transform',
                 'Connection': 'keep-alive',
                 'X-Accel-Buffering': 'no'},
    )


# ── Access log ───────────────────────────────────────────────────────────────
def _int_arg(name, default, lo, hi):
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(lo, min(value, hi))


@app.route('/api/log')
def api_log():
    return jsonify(db.log_recent(_int_arg('limit', 200, 1, 1000)))


@app.route('/api/log/<int:row_id>/crop')
def api_log_crop(row_id):
    crop = db.log_crop(row_id)
    if not crop:
        return jsonify({'error': 'No crop stored | تصویری ذخیره نشده'}), 404
    return jsonify({'image': crop})


@app.route('/api/log', methods=['DELETE'])
def api_log_clear():
    db.log_clear()
    return jsonify({'ok': True})


# ── Vehicles (whitelist / blacklist) ─────────────────────────────────────────
@app.route('/api/vehicles', methods=['GET'])
def api_vehicles_list():
    return jsonify(db.vehicles_all())


@app.route('/api/vehicles', methods=['POST'])
def api_vehicle_add():
    d = body()
    raw = str(d.get('plate', '')).strip()
    if not raw:
        return jsonify({'error': 'plate is required | پلاک الزامی است'}), 400

    canonical = plates.normalize(raw)
    if not canonical:
        return jsonify({
            'error': 'Invalid plate. Expected e.g. 24ن144-66 '
                     '| پلاک نامعتبر است. نمونه: 24ن144-66'
        }), 400

    list_type = str(d.get('list', 'white')).strip()
    if list_type not in db.VALID_LISTS:
        return jsonify({'error': 'list must be none, white or black | لیست نامعتبر است'}), 400

    db.vehicle_upsert(canonical, str(d.get('label', ''))[:120],
                      list_type, str(d.get('note', ''))[:500])
    return jsonify(db.vehicle_get(canonical)), 201


@app.route('/api/vehicles/<path:plate>', methods=['DELETE'])
def api_vehicle_delete(plate):
    canonical = plates.normalize(plate) or plate
    if not db.vehicle_get(canonical):
        return jsonify({'error': 'Vehicle not found | خودرو یافت نشد'}), 404
    db.vehicle_delete(canonical)
    return jsonify({'ok': True})


# ── lifecycle ────────────────────────────────────────────────────────────────
def bootstrap():
    models.load_async()
    cm.bootstrap()


@atexit.register
def _shutdown():
    cm.stop_all()
    db.close_conn()


bootstrap()

if __name__ == '__main__':
    host = os.environ.get('PLATE_HOST', '0.0.0.0')
    port = int(os.environ.get('PLATE_PORT', '5000'))
    log.info('Server is ready: http://localhost:%s | سرور آماده است', port)
    app.run(debug=False, host=host, port=port, threaded=True)
