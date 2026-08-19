"""
SQLite storage for cameras, vehicles (white/black list) and the access log.

Notable fixes over the first version:
  * connections are opened with a real ``timeout`` plus ``busy_timeout`` so a
    camera worker writing while a request reads no longer raises
    "database is locked";
  * ``log_recent`` joins ``vehicles``, so the Status column in the UI keeps
    showing Allowed/Blocked after a refresh instead of always "—";
  * plates are stored in the canonical form produced by ``plates.normalize``,
    with a one-off migration for rows written by the old code (which stored
    ``24ن14466`` while the UI submitted ``24ن144-66``, so the whitelist could
    never match);
  * the access log is capped, because every row carries a base64 JPEG crop.
"""
import logging
import os
import sqlite3
import threading

import plates

log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('PLATE_DB', os.path.join(BASE_DIR, 'traffic.db'))
BUSY_TIMEOUT_MS = int(os.environ.get('PLATE_DB_TIMEOUT_MS', '15000'))
LOG_MAX_ROWS = int(os.environ.get('PLATE_LOG_MAX_ROWS', '5000'))

VALID_ROLES = ('entry', 'exit', 'monitor')
VALID_LISTS = ('none', 'white', 'black')

_local = threading.local()
_write_lock = threading.Lock()


def _configure(conn):
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA busy_timeout=%d' % BUSY_TIMEOUT_MS)
    return conn


def get_conn():
    conn = getattr(_local, 'conn', None)
    if conn is None:
        conn = _configure(sqlite3.connect(
            DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000.0, check_same_thread=False))
        _local.conn = conn
    return conn


def close_conn():
    """Release this thread's connection (used on shutdown)."""
    conn = getattr(_local, 'conn', None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _local.conn = None


SCHEMA = '''
    CREATE TABLE IF NOT EXISTS cameras (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        name     TEXT NOT NULL,
        url      TEXT NOT NULL,
        role     TEXT NOT NULL DEFAULT 'entry',  -- entry | exit | monitor
        enabled  INTEGER NOT NULL DEFAULT 1,
        created  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS vehicles (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        plate    TEXT NOT NULL UNIQUE,
        label    TEXT,
        list     TEXT NOT NULL DEFAULT 'none',   -- none | white | black
        note     TEXT,
        added    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS access_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        plate       TEXT NOT NULL,
        camera_id   INTEGER,
        camera_name TEXT,
        role        TEXT,
        confidence  REAL,
        crop_b64    TEXT,
        ts          TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    );
    CREATE INDEX IF NOT EXISTS idx_log_plate ON access_log(plate);
    CREATE INDEX IF NOT EXISTS idx_log_ts    ON access_log(ts);
    CREATE INDEX IF NOT EXISTS idx_veh_list  ON vehicles(list);
'''


def init_db():
    conn = _configure(sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000.0))
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate_plate_format(conn)
    finally:
        conn.close()


def _migrate_plate_format(conn):
    """
    Rewrite plates written by the pre-canonical code (``24ن14466``) so the
    whitelist lookup can find them. Runs once; afterwards every row already
    equals its canonical form and the UPDATE loop is a no-op.
    """
    try:
        rows = conn.execute('SELECT id, plate FROM vehicles').fetchall()
        for row in rows:
            canon = plates.normalize(row['plate'])
            if canon and canon != row['plate']:
                try:
                    conn.execute('UPDATE vehicles SET plate=? WHERE id=?', (canon, row['id']))
                except sqlite3.IntegrityError:
                    # A canonical duplicate already exists — drop the stale row.
                    conn.execute('DELETE FROM vehicles WHERE id=?', (row['id'],))

        rows = conn.execute(
            'SELECT DISTINCT plate FROM access_log').fetchall()
        for row in rows:
            canon = plates.normalize(row['plate'])
            if canon and canon != row['plate']:
                conn.execute('UPDATE access_log SET plate=? WHERE plate=?', (canon, row['plate']))
        conn.commit()
    except sqlite3.Error as exc:
        log.warning('Plate format migration skipped: %s', exc)


def _fetchall(sql, params=()):
    return [dict(r) for r in get_conn().execute(sql, params).fetchall()]


def _fetchone(sql, params=()):
    row = get_conn().execute(sql, params).fetchone()
    return dict(row) if row else None


def _run(sql, params=()):
    conn = get_conn()
    with _write_lock:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid


# ── Cameras ──────────────────────────────────────────────────────────────────
def cameras_all():
    return _fetchall('SELECT * FROM cameras ORDER BY id')


def camera_get(cid):
    return _fetchone('SELECT * FROM cameras WHERE id=?', (cid,))


def camera_add(name, url, role='entry', enabled=1):
    role = role if role in VALID_ROLES else 'entry'
    return _run('INSERT INTO cameras(name,url,role,enabled) VALUES(?,?,?,?)',
                (name, url, role, int(bool(enabled))))


def camera_update(cid, name, url, role, enabled):
    role = role if role in VALID_ROLES else 'entry'
    _run('UPDATE cameras SET name=?,url=?,role=?,enabled=? WHERE id=?',
         (name, url, role, int(bool(enabled)), cid))


def camera_delete(cid):
    _run('DELETE FROM cameras WHERE id=?', (cid,))


def camera_set_enabled(cid, enabled):
    _run('UPDATE cameras SET enabled=? WHERE id=?', (int(bool(enabled)), cid))


# ── Vehicles (whitelist / blacklist) ─────────────────────────────────────────
def vehicles_all():
    return _fetchall('SELECT * FROM vehicles ORDER BY added DESC, id DESC')


def vehicle_get(plate):
    return _fetchone('SELECT * FROM vehicles WHERE plate=?', (plate,))


def vehicle_upsert(plate, label, list_type, note=''):
    """Insert or update in one statement — the read-then-write version raced."""
    list_type = list_type if list_type in VALID_LISTS else 'none'
    _run(
        'INSERT INTO vehicles(plate,label,list,note) VALUES(?,?,?,?) '
        'ON CONFLICT(plate) DO UPDATE SET label=excluded.label, '
        'list=excluded.list, note=excluded.note',
        (plate, label, list_type, note))


def vehicle_delete(plate):
    _run('DELETE FROM vehicles WHERE plate=?', (plate,))


# ── Access log ────────────────────────────────────────────────────────────────
def log_add(plate, camera_id, camera_name, role, confidence, crop_b64=''):
    row_id = _run(
        'INSERT INTO access_log(plate,camera_id,camera_name,role,confidence,crop_b64) '
        'VALUES(?,?,?,?,?,?)',
        (plate, camera_id, camera_name, role, confidence, crop_b64))
    log_prune()
    return row_id


def log_recent(limit=200):
    """
    Recent detections joined with the vehicle lists.

    ``crop_b64`` is intentionally *not* selected: those blobs are hundreds of KB
    each and the client only needs to know whether a crop exists.
    """
    limit = max(1, min(int(limit), 1000))
    return _fetchall(
        'SELECT l.id, l.plate, l.camera_id, l.camera_name, l.role, l.confidence, l.ts, '
        "       COALESCE(v.list, 'none') AS list, COALESCE(v.label, '') AS label, "
        "       (l.crop_b64 IS NOT NULL AND l.crop_b64 != '') AS has_crop "
        'FROM access_log l LEFT JOIN vehicles v ON v.plate = l.plate '
        'ORDER BY l.id DESC LIMIT ?',
        (limit,))


def log_crop(row_id):
    row = _fetchone('SELECT crop_b64 FROM access_log WHERE id=?', (row_id,))
    return (row or {}).get('crop_b64') or ''


def log_clear():
    _run('DELETE FROM access_log')


def log_prune(max_rows=None):
    """Keep the log bounded; each row stores a base64 JPEG crop."""
    cap = LOG_MAX_ROWS if max_rows is None else max_rows
    if cap <= 0:
        return
    _run('DELETE FROM access_log WHERE id <= '
         '(SELECT id FROM access_log ORDER BY id DESC LIMIT 1 OFFSET ?)', (cap,))


def stats():
    row = _fetchone(
        'SELECT (SELECT COUNT(*) FROM cameras)  AS cameras, '
        '       (SELECT COUNT(*) FROM vehicles) AS vehicles, '
        '       (SELECT COUNT(*) FROM access_log) AS log_rows')
    return row or {'cameras': 0, 'vehicles': 0, 'log_rows': 0}


init_db()
