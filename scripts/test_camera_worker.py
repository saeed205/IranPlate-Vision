"""
Offline checks for the RTSP worker's detection bookkeeping.

The detector and OCR are stubbed, so this runs without models and without a
camera:  python scripts/test_camera_worker.py
"""
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('PLATE_DB', os.path.join(tempfile.mkdtemp(), 'worker.db'))

import camera_manager as cm                                # noqa: E402
import db                                                  # noqa: E402
import models                                              # noqa: E402

failures = []


def check(label, actual, expected):
    ok = actual == expected
    print('%-4s %-54s %r' % ('ok' if ok else 'FAIL', label, actual))
    if not ok:
        failures.append('%s: expected %r, got %r' % (label, expected, actual))


# ── stub inference ───────────────────────────────────────────────────────────
FRAME = np.zeros((480, 640, 3), np.uint8)
FRAME[:] = 40
CROP = np.full((60, 200, 3), 200, np.uint8)

fake_text = ['']


def fake_detect_plates(frame, conf=None, run_ocr=True):
    if not fake_text[0]:
        return []
    return [{'conf': 0.91, 'box': (10, 10, 210, 70), 'crop': CROP, 'text': fake_text[0]}]


models.detect_plates = fake_detect_plates

REAL_BROADCAST = cm._broadcast
captured = []
cm._broadcast = lambda event: captured.append(event)

worker = cm.CameraWorker({'id': 7, 'name': 'Gate A', 'url': 'rtsp://x/y', 'role': 'entry'})

print('== registration ==')
# The OCR emits Persian digits with no separator; the DB must hold the canonical form.
fake_text[0] = '۲۴ن۱۴۴۶۶'
worker._process_frame(FRAME)
check('one detection broadcast', len([e for e in captured if e['type'] == 'detection']), 1)
check('plate broadcast canonically', captured[-1]['plate'], '24ن144-66')
check('log row written', [r['plate'] for r in db.log_recent(10)], ['24ن144-66'])
check('crop stored', bool(db.log_recent(1)[0]['has_crop']), True)

# Still the same car in frame: must not be logged again.
for _ in range(3):
    worker._process_frame(FRAME)
check('no duplicate while present', len(db.log_recent(10)), 1)

print('\n== leaving and returning ==')
fake_text[0] = ''
for _ in range(cm.ABSENT_FRAMES):
    worker._process_frame(FRAME)
check('marked gone after ABSENT_FRAMES',
      worker._states['24ن144-66'].status, 'gone')

fake_text[0] = '24ن144-66'
worker._process_frame(FRAME)
check('re-registered on return', len(db.log_recent(10)), 2)

print('\n== list matching ==')
# This is the bug the canonical form fixes: the worker used to store 24ن14466
# while the UI submitted 24ن144-66, so the blacklist never matched.
db.vehicle_upsert('24ن144-66', 'Boss', 'black', '')
fake_text[0] = ''
for _ in range(cm.ABSENT_FRAMES * 7):
    worker._process_frame(FRAME)
check('state pruned after long absence', '24ن144-66' in worker._states, False)

captured.clear()
fake_text[0] = '۲۴ن۱۴۴۶۶'
worker._process_frame(FRAME)
check('blacklist hit reported', captured[-1]['list'], 'black')
check('label reported', captured[-1]['label'], 'Boss')
check('log join reflects the list', db.log_recent(1)[0]['list'], 'black')

print('\n== invalid OCR output is dropped ==')
before = len(db.log_recent(50))
for junk in ['12345678', 'abcd', '', '1ن123-45', '۲۴ن۱۴۴']:
    fake_text[0] = junk
    worker._process_frame(FRAME)
check('nothing logged for junk OCR', len(db.log_recent(50)), before)

print('\n== event bus backpressure ==')
# Restore the real implementation for this section.
cm._broadcast = REAL_BROADCAST

slow = cm.subscribe()
fast = cm.subscribe()
check('two subscribers registered', cm.subscriber_count(), 2)

# Nobody drains `slow`, so its queue overflows. The old code removed the
# subscriber on the first queue.Full, which silently killed a browser that had
# merely paused; it must drop the oldest event and stay subscribed.
overflow = cm.SUBSCRIBER_QUEUE_SIZE + 25
for i in range(overflow):
    cm._broadcast({'type': 'detection', 'n': i})

check('slow subscriber survives overflow', cm.subscriber_count(), 2)
check('slow queue capped at maxsize', slow.qsize(), cm.SUBSCRIBER_QUEUE_SIZE)

# The oldest events were discarded, so the newest one is still queued.
newest = None
while not slow.empty():
    newest = slow.get_nowait()
check('newest event retained', newest['n'], overflow - 1)

cm._broadcast({'type': 'detection', 'n': 'after-overflow'})
check('slow subscriber still receiving', slow.get_nowait()['n'], 'after-overflow')
check('fast subscriber got everything too', fast.qsize(), cm.SUBSCRIBER_QUEUE_SIZE)

cm.unsubscribe(slow)
cm.unsubscribe(fast)
check('unsubscribe works', cm.subscriber_count(), 0)
check('unsubscribe twice is safe', cm.unsubscribe(fast), None)

print('\n' + '=' * 62)
if failures:
    print('%d FAILURE(S):' % len(failures))
    for f in failures:
        print('  - ' + f)
    sys.exit(1)
print('All checks passed.')
