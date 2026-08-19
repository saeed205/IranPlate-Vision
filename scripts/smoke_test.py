"""
Hit every endpoint of a running server.

    python app.py &
    python scripts/smoke_test.py [base_url]

Exits non-zero on the first failure. Endpoints that need the ML models are
reported as "pending" rather than failed while the models are still loading.
"""
import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5000').rstrip('/')
TIMEOUT = 10

failures = []


def request(path, method='GET', payload=None):
    """Return (status, parsed_body). Never raises for an HTTP error status."""
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            body = res.read().decode('utf-8', 'replace')
            status = res.status
    except urllib.error.HTTPError as err:
        body = err.read().decode('utf-8', 'replace')
        status = err.code
    try:
        return status, json.loads(body)
    except ValueError:
        return status, body


def check(label, path, method='GET', payload=None, expect=200, note=None):
    try:
        status, body = request(path, method, payload)
    except Exception as exc:                     # noqa: BLE001 - connection refused etc.
        print('FAIL %-46s %s' % (label, exc))
        failures.append('%s: %s' % (label, exc))
        return None

    codes = expect if isinstance(expect, (list, tuple)) else [expect]
    ok = status in codes
    summary = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    print('%-4s %-46s %s  %s' % ('ok' if ok else 'FAIL', label, status, summary[:90]))
    if not ok:
        failures.append('%s: expected %s, got %s' % (label, codes, status))
    if note:
        print('     %s' % note)
    return body


print('Target: %s\n' % BASE)

print('-- pages --')
check('GET /', '/')
check('GET /scan', '/scan')
check('GET /cameras', '/cameras')
check('GET /favicon.ico', '/favicon.ico')
check('GET /static/i18n.js', '/static/i18n.js')

print('\n-- status --')
status_body = check('GET /status', '/status')
models_ready = bool(isinstance(status_body, dict) and status_body.get('ready'))
allow_local = bool(isinstance(status_body, dict) and status_body.get('allow_local_sources'))
if allow_local:
    print('     PLATE_ALLOW_LOCAL_SOURCES is on — source validation is relaxed')
check('GET /health', '/health', expect=[200, 503])
if not models_ready:
    print('     models still loading — /detect is expected to answer 503')

print('\n-- collections --')
cams = check('GET /api/cameras', '/api/cameras')
vehicles = check('GET /api/vehicles', '/api/vehicles')
logs = check('GET /api/log?limit=5', '/api/log?limit=5')
for label, value in (('cameras', cams), ('vehicles', vehicles), ('log rows', logs)):
    if isinstance(value, list):
        print('     %-10s %d' % (label, len(value)))

print('\n-- validation (these must NOT be 500) --')
check('bad log limit', '/api/log?limit=not-a-number')
check('unknown api path', '/api/definitely-not-here', expect=404)
check('unknown camera', '/api/cameras/424242', expect=404)
check('update unknown camera', '/api/cameras/424242', 'PUT', {}, expect=404)
check('delete unknown camera', '/api/cameras/424242', 'DELETE', expect=404)
check('snapshot of unknown camera', '/api/cameras/424242/snapshot', expect=404)
check('camera with no name', '/api/cameras', 'POST', {'url': 'rtsp://h/s'}, expect=400)
if allow_local:
    print('skip camera with bad scheme (local sources allowed)')
else:
    check('camera with bad scheme', '/api/cameras', 'POST',
          {'name': 'x', 'url': 'ftp://h/s'}, expect=400)
check('vehicle with invalid plate', '/api/vehicles', 'POST', {'plate': 'nope'}, expect=400)
check('vehicle with 8 digits', '/api/vehicles', 'POST', {'plate': '12345678'}, expect=400)
check('detect with no image', '/detect', 'POST', expect=[400, 415, 503])

print('\n-- round trip --')
created = check('create camera', '/api/cameras', 'POST',
                {'name': 'smoke-test', 'url': 'rtsp://user:secret@127.0.0.1:554/x',
                 'role': 'monitor'}, expect=201)
if isinstance(created, dict) and created.get('id'):
    cid = created['id']
    if '••' not in str(created.get('url', '')):
        failures.append('camera url was returned unmasked')
        print('FAIL password was not masked in the response')
    else:
        print('ok   password masked in response')
    check('toggle camera off', '/api/cameras/%d/toggle' % cid, 'POST', {'enabled': False})
    check('rename camera', '/api/cameras/%d' % cid, 'PUT',
          {'name': 'smoke-test-2', 'url': created['url'], 'role': 'monitor'})
    check('delete camera', '/api/cameras/%d' % cid, 'DELETE')

created = check('create vehicle', '/api/vehicles', 'POST',
                {'plate': '۲۴ن۱۴۴۶۶', 'label': 'smoke', 'list': 'white'}, expect=201)
if isinstance(created, dict) and created.get('plate'):
    if created['plate'] != '24ن144-66':
        failures.append('plate not canonicalised: %r' % created['plate'])
        print('FAIL plate stored as %r' % created['plate'])
    else:
        print('ok   plate canonicalised to 24ن144-66')
    check('delete vehicle', '/api/vehicles/' + urllib.request.quote(created['plate']),
          'DELETE')

print('\n' + '=' * 62)
if failures:
    print('%d FAILURE(S):' % len(failures))
    for f in failures:
        print('  - ' + f)
    sys.exit(1)
print('Smoke test passed.')
