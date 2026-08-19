# Changelog

All notable changes to this project are documented here. Format based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [1.1.0] - 2026-08-19

### ⚠️ Upgrade notes

The public HTTP API stays backward compatible, so this is a minor release. One
thing does change on disk:

- Plates are now stored and compared in a single canonical form, `24ن144-66`.
  Existing `vehicles` and `access_log` rows are migrated automatically on first
  start. Anything reading `traffic.db` directly will see the new format.
- Local file paths are no longer accepted as camera sources unless
  `PLATE_ALLOW_LOCAL_SOURCES=1` is set.

### Fixed

- **Allow/block list never matched.** The RTSP workers wrote `24ن14466` while the
  dashboard submitted `24ن144-66`, so no vehicle lookup ever succeeded.
- **`ه` (U+0647) and `ھ` (U+06BE) were treated as different letters.**
  `plate_data.json` uses one spelling and the OCR emits the other, leaving 31
  city entries unreachable.
- **Any eight digits validated as a plate.** The letter class `[؀-ۿ]` also
  matches Persian digits.
- **Government `الف` plates never parsed** — the pattern accepted a single
  character only.
- **Access log Status column always showed `—`** after a refresh; the query did
  not join the vehicle lists.
- `/detect` reported the confidence of one detection alongside the OCR text of a
  different one.
- An unreachable camera showed a green "running" indicator: status came from
  `Thread.is_alive()`, which stays true throughout the reconnect loop.
- Unstyled *Menu* button on `/scan` (it referenced CSS variables that page does
  not define).
- Language switching left JS-rendered lists in the previous language until a
  reload.
- Copying a plate silently failed over `http://<lan-ip>`, where
  `navigator.clipboard` is undefined.
- The live preview reset every ten seconds because the grid was rebuilt wholesale.
- Reconnecting SSE stacked streams, because the previous `EventSource` was not
  closed and it already retries on its own.
- Server-Sent Events heartbeat swallowed genuine errors instead of only
  `queue.Empty`.
- Several API paths returned an HTML 500 page to `fetch()` callers, or raised:
  a `PUT` to a missing camera, and a non-numeric `?limit`.
- `run_https.py` used the deprecated `datetime.utcnow()`, and the generated
  certificate's SAN omitted the LAN address it told you to open on a phone.

### Security

- Escaped all server-provided text rendered into the cameras page; camera names,
  URLs, plates, labels and notes were injectable, and inline `onclick` handlers
  interpolated the same values into attributes.
- RTSP passwords are masked in API responses.
- Added an upload size cap and camera URL scheme validation.
- Documented the threat model in [SECURITY.md](SECURITY.md).

### Added

- `plates.py` — canonical plate parsing shared by the API and the workers.
- `models.py` — lazy model loading with serialised inference, so Flask threads
  and camera workers no longer call into YOLO/hezar concurrently.
- `GET /health`, `GET /api/log/<id>/crop`, and `GET /api/cameras/<id>` endpoints.
- Real camera state (`connected` / `connecting` / `last_error`) pushed over SSE.
- `scripts/test_plates.py` and `scripts/test_camera_worker.py` — 69 offline
  checks that need neither a server nor the models.
- Continuous integration, Dependabot, and a lint configuration.

### Changed

- Live snapshots are encoded on demand and rate-limited, replacing a JPEG plus
  base64 encode of every decoded frame at full stream framerate.
- RTSP connect time is bounded by passing the timeout as a `VideoCapture`
  construction parameter — measured 30 s to 4 s against an unreachable host.
- Docker serves through waitress as a non-root user, no longer mounts the
  project over `/app`, persists the database and model cache, and has a
  healthcheck.
- SQLite gains lock timeouts, indexes, an atomic upsert, and a capped log.

## [1.0.0] - 2026-05-29

### Added

- Initial public release: YOLO plate detection, Persian OCR, plate metadata
  lookup, RTSP camera management, access log, allow/block lists, and a bilingual
  (EN/FA) dashboard.

[Unreleased]: https://github.com/saeed205/IranPlate-Vision/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/saeed205/IranPlate-Vision/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/saeed205/IranPlate-Vision/releases/tag/v1.0.0
