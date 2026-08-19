"""
RTSP camera manager.

Each camera runs in a dedicated thread that captures frames, detects plates and
pushes events to clients over SSE.

Fixed here:
  * The status dot used ``Thread.is_alive()``, which stays true while a worker
    sits in its reconnect loop — so an unreachable camera reported "running".
    Workers now publish a real ``connected`` / ``last_error`` state.
  * Every decoded frame was JPEG-encoded *and* base64-encoded, at full stream
    framerate, whether or not anyone was watching. Snapshots are now produced on
    demand and rate-limited.
  * ``cv2.VideoCapture`` could block for minutes on a dead host; RTSP now uses
    TCP with an explicit timeout.
  * Plates are stored in the canonical form so the white/black list matches.
  * A slow SSE client used to be silently dropped forever; the bus now discards
    the oldest queued event instead of the subscriber.
"""
import base64
import logging
import os
import queue
import threading
import time

# Must be set before the first VideoCapture is constructed, otherwise a dead
# RTSP host can hang the worker for the OS-level connect timeout.
os.environ.setdefault(
    'OPENCV_FFMPEG_CAPTURE_OPTIONS',
    'rtsp_transport;tcp|timeout;5000000|stimeout;5000000|max_delay;500000')

import cv2

import db
import models
import plates

log = logging.getLogger(__name__)

DETECT_INTERVAL = float(os.environ.get('PLATE_DETECT_INTERVAL', '2.0'))
RECONNECT_WAIT = float(os.environ.get('PLATE_RECONNECT_WAIT', '5.0'))
ABSENT_FRAMES = int(os.environ.get('PLATE_ABSENT_FRAMES', '5'))
SNAPSHOT_FPS = float(os.environ.get('PLATE_SNAPSHOT_FPS', '4'))
SNAPSHOT_WIDTH = int(os.environ.get('PLATE_SNAPSHOT_WIDTH', '640'))
# Stop encoding snapshots when nobody has asked for one for this long.
SNAPSHOT_IDLE_TIMEOUT = float(os.environ.get('PLATE_SNAPSHOT_IDLE', '10'))
OPEN_TIMEOUT_MS = int(os.environ.get('PLATE_OPEN_TIMEOUT_MS', '6000'))
READ_TIMEOUT_MS = int(os.environ.get('PLATE_READ_TIMEOUT_MS', '10000'))
MAX_TRACKED_PLATES = int(os.environ.get('PLATE_MAX_TRACKED', '256'))
SUBSCRIBER_QUEUE_SIZE = 64

# ── SSE event bus ─────────────────────────────────────────────────────────────
_subscribers = []
_sub_lock = threading.Lock()


def subscribe():
    """Return a queue that receives SSE event dicts."""
    q = queue.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
    with _sub_lock:
        _subscribers.append(q)
    return q


def unsubscribe(q):
    with _sub_lock:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


def subscriber_count():
    with _sub_lock:
        return len(_subscribers)


def _broadcast(event):
    """
    Fan an event out to every subscriber.

    A backed-up client loses its oldest event rather than its subscription: the
    old behaviour removed the queue on the first ``Full``, so a browser that
    paused for a moment stopped receiving detections until it reloaded.
    """
    with _sub_lock:
        targets = list(_subscribers)
    for q in targets:
        while True:
            try:
                q.put_nowait(event)
                break
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break


# ── Plate state machine ───────────────────────────────────────────────────────
# A plate is registered once when it first appears, and again only after it has
# been missing for ABSENT_FRAMES consecutive checks (≈10s at the defaults).
class _PlateState:
    __slots__ = ('absent_count', 'conf', 'last_seen', 'status')

    def __init__(self, conf):
        self.status = 'present'
        self.absent_count = 0
        self.conf = conf
        self.last_seen = time.time()


class CameraWorker(threading.Thread):
    def __init__(self, cam):
        super().__init__(daemon=True, name='cam-%s' % cam['id'])
        self.cam = dict(cam)
        self._stop_evt = threading.Event()
        self._frame_lock = threading.Lock()
        self._latest_frame = None          # base64 JPEG
        self._latest_frame_at = 0.0
        self._snapshot_wanted_at = 0.0
        self._last_encode_at = 0.0
        self._connected = False
        self._connecting = False
        self._last_error = ''
        self._last_frame_at = 0.0
        self._states = {}

    # ── public state ──
    def stop(self):
        self._stop_evt.set()

    def get_snapshot(self):
        """Return ``(base64_jpeg | None, age_seconds)`` and arm on-demand encoding."""
        self._snapshot_wanted_at = time.time()
        with self._frame_lock:
            frame, at = self._latest_frame, self._latest_frame_at
        age = (time.time() - at) if at else None
        return frame, age

    def state(self):
        return {
            'running': self.is_alive() and not self._stop_evt.is_set(),
            'connected': self._connected,
            'connecting': self._connecting,
            'last_error': self._last_error,
            'last_frame_age': round(time.time() - self._last_frame_at, 1) if self._last_frame_at else None,
        }

    # ── capture loop ──
    def run(self):
        while not self._stop_evt.is_set():
            try:
                self._run_capture()
            except Exception as exc:                     # noqa: BLE001
                self._last_error = '%s: %s' % (type(exc).__name__, exc)
                log.warning('Camera %s error: %s - reconnecting in %ss',
                            self.cam['id'], exc, RECONNECT_WAIT)
            finally:
                self._connected = False
            if not self._stop_evt.is_set():
                self._emit_status()
                # Interruptible sleep so a delete/disable takes effect at once.
                self._stop_evt.wait(RECONNECT_WAIT)

    def _open(self):
        """
        Open the stream with a bounded connect time.

        The timeout has to be supplied as a *construction* parameter: the
        connect happens inside the VideoCapture constructor, so a later
        ``cap.set(CAP_PROP_OPEN_TIMEOUT_MSEC, ...)`` arrives far too late and the
        worker blocks on OpenCV's 30s default (measured) for every dead host.
        """
        source = self.cam['url']
        if isinstance(source, str) and source.isdigit():
            source = int(source)          # local device index

        params = []
        for name, value in (('CAP_PROP_OPEN_TIMEOUT_MSEC', OPEN_TIMEOUT_MS),
                            ('CAP_PROP_READ_TIMEOUT_MSEC', READ_TIMEOUT_MS)):
            prop = getattr(cv2, name, None)
            if prop is not None:
                params += [int(prop), int(value)]

        try:
            cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG, params) if params                 else cv2.VideoCapture(source, cv2.CAP_FFMPEG)
        except (cv2.error, TypeError):
            # Older OpenCV without the params overload.
            cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)

        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except cv2.error:
            pass
        return cap

    def _run_capture(self):
        log.info('Connecting to camera %s', self.cam['id'])
        self._connecting = True
        self._emit_status()
        try:
            cap = self._open()
        finally:
            self._connecting = False
        if not cap.isOpened():
            cap.release()
            self._last_error = 'Cannot open stream | اتصال به استریم برقرار نشد'
            log.warning('Camera %s: cannot open stream', self.cam['id'])
            return

        self._connected = True
        self._last_error = ''
        self._emit_status()
        last_detect = 0.0
        try:
            while not self._stop_evt.is_set():
                ok, frame = cap.read()
                if not ok or frame is None:
                    self._last_error = 'Stream interrupted | استریم قطع شد'
                    log.warning('Camera %s: lost frame', self.cam['id'])
                    break

                now = time.time()
                self._last_frame_at = now
                self._maybe_encode_snapshot(frame, now)

                if now - last_detect >= DETECT_INTERVAL:
                    last_detect = now
                    self._process_frame(frame)
        finally:
            cap.release()

    def _maybe_encode_snapshot(self, frame, now):
        """Encode a preview frame only while a client is actually watching."""
        if now - self._snapshot_wanted_at > SNAPSHOT_IDLE_TIMEOUT:
            return
        if SNAPSHOT_FPS > 0 and now - self._last_encode_at < 1.0 / SNAPSHOT_FPS:
            return
        raw = models.encode_jpeg(frame, quality=70, max_width=SNAPSHOT_WIDTH)
        if not raw:
            return
        self._last_encode_at = now
        encoded = base64.b64encode(raw).decode('ascii')
        with self._frame_lock:
            self._latest_frame = encoded
            self._latest_frame_at = now

    # ── detection ──
    def _process_frame(self, frame):
        seen_now = {}
        for det in models.detect_plates(frame):
            canonical = plates.normalize(det['text'])
            if not canonical:
                continue
            prev = seen_now.get(canonical)
            if prev is None or det['conf'] > prev[0]:
                seen_now[canonical] = (det['conf'], det['crop'])

        for canonical, (conf, crop) in seen_now.items():
            state = self._states.get(canonical)
            if state is None:
                self._states[canonical] = _PlateState(conf)
                self._register(canonical, conf, crop)
                continue
            state.absent_count = 0
            state.conf = conf
            state.last_seen = time.time()
            if state.status == 'gone':
                state.status = 'present'
                self._register(canonical, conf, crop)

        for canonical, state in list(self._states.items()):
            if canonical in seen_now:
                continue
            state.absent_count += 1
            if state.status == 'present':
                if state.absent_count >= ABSENT_FRAMES:
                    state.status = 'gone'
                    log.debug('[cam %s] plate %s left scene', self.cam['id'], canonical)
            elif state.absent_count > ABSENT_FRAMES * 6:
                del self._states[canonical]

        # Hard cap: a busy street would otherwise grow this dict without bound.
        if len(self._states) > MAX_TRACKED_PLATES:
            for canonical, _ in sorted(
                    self._states.items(), key=lambda kv: kv[1].last_seen
            )[:len(self._states) - MAX_TRACKED_PLATES]:
                self._states.pop(canonical, None)

    def _register(self, canonical, conf, crop):
        """Persist a detection and broadcast it."""
        vlist, label = 'none', ''
        try:
            veh = db.vehicle_get(canonical)
            if veh:
                vlist = veh.get('list') or 'none'
                label = veh.get('label') or ''
        except Exception as exc:                          # noqa: BLE001
            log.warning('Vehicle lookup failed for %s: %s', canonical, exc)

        crop_b64 = ''
        raw = models.encode_jpeg(crop, quality=85, max_width=480)
        if raw:
            crop_b64 = base64.b64encode(raw).decode('ascii')

        row_id = None
        try:
            row_id = db.log_add(
                plate=canonical,
                camera_id=self.cam['id'],
                camera_name=self.cam['name'],
                role=self.cam['role'],
                confidence=conf,
                crop_b64=crop_b64,
            )
        except Exception as exc:                          # noqa: BLE001
            log.error('Could not write access log for %s: %s', canonical, exc)

        _broadcast({
            'type': 'detection',
            'id': row_id,
            'plate': canonical,
            'label': label,
            'list': vlist,
            'camera_id': self.cam['id'],
            'camera_name': self.cam['name'],
            'role': self.cam['role'],
            'conf': round(conf, 3),
            'has_crop': bool(crop_b64),
            'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
        })
        log.info('[cam %s] NEW plate=%s conf=%.2f list=%s',
                 self.cam['id'], canonical, conf, vlist)

    def _emit_status(self):
        _broadcast({
            'type': 'camera_status',
            'camera_id': self.cam['id'],
            'camera_name': self.cam['name'],
            **self.state(),
        })


# ── Manager ───────────────────────────────────────────────────────────────────
_workers = {}
_mgr_lock = threading.Lock()


def start_all():
    """Start workers for every enabled camera in the DB."""
    for cam in db.cameras_all():
        if cam['enabled']:
            start_worker(cam)


def start_worker(cam):
    if not cam:
        return
    cid = cam['id']
    with _mgr_lock:
        existing = _workers.get(cid)
        if existing is not None and existing.is_alive():
            return
        worker = CameraWorker(cam)
        _workers[cid] = worker
    worker.start()
    log.info('Started worker for camera %s', cid)


def stop_worker(cid, join_timeout=2.0):
    with _mgr_lock:
        worker = _workers.pop(cid, None)
    if worker is None:
        return
    worker.stop()
    # Wait briefly so a restart does not run two capture loops on one URL.
    worker.join(timeout=join_timeout)
    log.info('Stopped worker for camera %s', cid)


def stop_all():
    for cid in list(_workers):
        stop_worker(cid, join_timeout=1.0)


def restart_worker(cid):
    stop_worker(cid)
    cam = db.camera_get(cid)
    if cam and cam['enabled'] and models.is_ready():
        start_worker(cam)


def add_camera(name, url, role='entry'):
    cid = db.camera_add(name, url, role)
    if models.is_ready():
        start_worker(db.camera_get(cid))
    return cid


def remove_camera(cid):
    stop_worker(cid)
    db.camera_delete(cid)


def set_enabled(cid, enabled):
    db.camera_set_enabled(cid, enabled)
    if enabled:
        cam = db.camera_get(cid)
        if cam and models.is_ready():
            start_worker(cam)
    else:
        stop_worker(cid)


def get_snapshot(cid):
    """``(base64_jpeg | None, age_seconds | None)``."""
    with _mgr_lock:
        worker = _workers.get(cid)
    return worker.get_snapshot() if worker else (None, None)


def worker_status():
    """``{camera_id: {'running','connected','last_error','last_frame_age'}}``."""
    with _mgr_lock:
        workers = dict(_workers)
    return {cid: w.state() for cid, w in workers.items()}


def bootstrap():
    """Wait for the models, then bring up the enabled cameras."""
    def _wait():
        if models.wait_ready():
            start_all()
        else:
            log.warning('Models unavailable — RTSP workers not started')
    threading.Thread(target=_wait, name='camera-bootstrap', daemon=True).start()
