"""
Offline checks for plate parsing, the province lookup and the JSON API.

No server and no ML models needed:  python scripts/test_plates.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Keep the developer's real traffic.db out of the way.
os.environ.setdefault('PLATE_DB', os.path.join(tempfile.mkdtemp(), 'test.db'))

import app as flask_app
import db
import plates

failures = []


def check(label, actual, expected):
    ok = actual == expected
    print('%-4s %-52s %r' % ('ok' if ok else 'FAIL', label, actual))
    if not ok:
        failures.append('%s: expected %r, got %r' % (label, expected, actual))


def section(title):
    print('\n== %s ==' % title)


# ── normalisation ────────────────────────────────────────────────────────────
section('plate normalisation')
check('canonical form passes through', plates.normalize('24ن144-66'), '24ن144-66')
check('persian digits, no separator', plates.normalize('۲۴ن۱۴۴۶۶'), '24ن144-66')
check('spaces between groups', plates.normalize('24 ن 144 66'), '24ن144-66')
check('arabic-indic digits', plates.normalize('٢٤ن١٤٤٦٦'), '24ن144-66')
# The old regex accepted any Arabic-block char as the letter, so eight digits
# validated as a plate.
check('eight digits rejected', plates.normalize('12345678'), '')
check('too few digits rejected', plates.normalize('1ن123-45'), '')
check('non-plate text rejected', plates.normalize('hello'), '')
check('empty rejected', plates.normalize(''), '')
# The old regex matched a single character only, so government plates failed.
check('alef spelled out', plates.normalize('12الف34567'), '12الف345-67')
check('bare alef folds to الف', plates.normalize('12ا345 67'), '12الف345-67')
# heh U+06BE vs U+0647 were treated as different letters.
check('heh U+06BE folds', plates.normalize('12ھ12345'), '12ه123-45')
check('heh U+0647 folds', plates.normalize('12ه12345'), '12ه123-45')
check('arabic yeh folds', plates.normalize('12ي12345'), '12ی123-45')
check('arabic kaf folds', plates.normalize('12ك12345'), '12ک123-45')
check('embedded bidi marks stripped',
      plates.normalize('\u202a24ن144-66\u202c'), '24ن144-66')

# ── province / type lookup ───────────────────────────────────────────────────
section('province + vehicle type lookup')
info = flask_app.describe_plate('12ه12345')
# plate_data.json spells this letter ھ, so the pre-fix code found no city.
check('heh plate resolves to a city', bool(info and info['locations']), True)
check('heh plate is a private vehicle',
      info['vehicle_type']['id'] if info and info['vehicle_type'] else None, 'shakhsi')

info = flask_app.describe_plate('12الف34567')
check('alef plate is a government vehicle',
      info['vehicle_type']['id'] if info and info['vehicle_type'] else None, 'dolati')
check('alef plate resolves to a city', bool(info and info['locations']), True)

check('unparseable text yields no info', flask_app.describe_plate('12345678'), None)
check('locations are capped', len(flask_app.describe_plate('12ب12345')['locations']) <= 3, True)

# Every letter in plate_data.json must be reachable through the lookup.
unreachable = [
    letter for letter in sorted(plates.LETTERS)
    if flask_app.TYPE_BY_LETTER.get(letter) is None
]
check('every letter maps to a vehicle type', unreachable, [])

# ── URL handling ─────────────────────────────────────────────────────────────
section('camera url handling')
check('rtsp accepted', flask_app.validate_camera_url('rtsp://h/s')[0], True)
check('https accepted', flask_app.validate_camera_url('https://h/s.mjpg')[0], True)
check('device index accepted', flask_app.validate_camera_url('0')[0], True)
check('ftp rejected', flask_app.validate_camera_url('ftp://h/s')[0], False)
check('file path rejected', flask_app.validate_camera_url('/etc/passwd')[0], False)
check('empty rejected', flask_app.validate_camera_url('')[0], False)
check('password masked',
      flask_app.mask_url('rtsp://admin:hunter2@10.0.0.5:554/live'),
      'rtsp://admin:••••••@10.0.0.5:554/live')
check('url without password unchanged',
      flask_app.mask_url('rtsp://10.0.0.5/live'), 'rtsp://10.0.0.5/live')

# ── HTTP API ─────────────────────────────────────────────────────────────────
section('HTTP API')
client = flask_app.app.test_client()


def status_of(method, path, **kw):
    return getattr(client, method)(path, **kw).status_code


def json_of(method, path, **kw):
    return getattr(client, method)(path, **kw).get_json()


check('GET /status', status_of('get', '/status'), 200)
check('GET / renders', status_of('get', '/'), 200)
check('GET /scan renders', status_of('get', '/scan'), 200)
check('GET /cameras renders', status_of('get', '/cameras'), 200)
check('GET /favicon.ico', status_of('get', '/favicon.ico'), 200)

# These used to raise and return an HTML 500 page to a fetch() caller.
check('PUT unknown camera -> 404', status_of('put', '/api/cameras/424242', json={}), 404)
check('DELETE unknown camera -> 404', status_of('delete', '/api/cameras/424242'), 404)
check('non-numeric limit -> 200', status_of('get', '/api/log?limit=abc'), 200)
check('unknown /api path is JSON',
      'error' in (json_of('get', '/api/does-not-exist') or {}), True)
check('/detect with no file -> 503 while models load',
      status_of('post', '/detect'), 503)

check('camera with bad scheme -> 400',
      status_of('post', '/api/cameras', json={'name': 'x', 'url': 'ftp://a/b'}), 400)
check('camera with bad role -> 400',
      status_of('post', '/api/cameras',
                json={'name': 'x', 'url': 'rtsp://h/s', 'role': 'nope'}), 400)
check('camera with no name -> 400',
      status_of('post', '/api/cameras', json={'url': 'rtsp://h/s'}), 400)

created = json_of('post', '/api/cameras',
                  json={'name': 'Gate', 'url': 'rtsp://u:p@10.0.0.9/live', 'role': 'exit'})
check('camera created', created.get('name'), 'Gate')
check('response url is masked', '••' in created['url'], True)
check('has_credentials reported', created['has_credentials'], True)

cid = created['id']
# Echoing the masked URL back must not overwrite the stored password.
client.put('/api/cameras/%d' % cid, json={'name': 'Gate 2', 'url': created['url']})
check('password preserved on edit',
      db.camera_get(cid)['url'], 'rtsp://u:p@10.0.0.9/live')
check('camera deleted', status_of('delete', '/api/cameras/%d' % cid), 200)

check('invalid plate rejected',
      status_of('post', '/api/vehicles', json={'plate': 'nope'}), 400)
check('eight-digit plate rejected',
      status_of('post', '/api/vehicles', json={'plate': '12345678'}), 400)

client.post('/api/vehicles', json={'plate': '۲۴ن۱۴۴۶۶', 'label': 'CEO', 'list': 'white'})
stored = json_of('get', '/api/vehicles')
check('plate stored canonically', [v['plate'] for v in stored], ['24ن144-66'])
# Whitelist matching used to fail because the worker wrote 24ن14466 while the
# UI submitted 24ن144-66.
check('worker form finds the same row',
      (db.vehicle_get(plates.normalize('24ن14466')) or {}).get('label'), 'CEO')

# The log's Status column comes from a join, so it survives a page refresh.
db.log_add('24ن144-66', 1, 'Gate', 'entry', 0.93, 'Zm9v')
row = json_of('get', '/api/log?limit=1')[0]
check('log row carries list from join', row['list'], 'white')
check('log row reports a stored crop', bool(row['has_crop']), True)
check('crop is retrievable', json_of('get', '/api/log/%d/crop' % row['id'])['image'], 'Zm9v')
check('crop 404s when absent', status_of('get', '/api/log/999999/crop'), 404)
check('log_recent omits the blob', 'crop_b64' in row, False)

client.delete('/api/vehicles/' + '24ن144-66')
check('vehicle deleted', json_of('get', '/api/vehicles'), [])

# ── summary ──────────────────────────────────────────────────────────────────
print('\n' + '=' * 60)
if failures:
    print('%d FAILURE(S):' % len(failures))
    for f in failures:
        print('  - ' + f)
    sys.exit(1)
print('All checks passed.')
