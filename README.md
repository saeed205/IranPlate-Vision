# IranPlate Vision | سامانه IranPlate Vision

<div align="center">

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-API-000000?style=for-the-badge&logo=flask&logoColor=white)
![YOLO](https://img.shields.io/badge/YOLO-Detection-00FFFF?style=for-the-badge)
![OpenCV](https://img.shields.io/badge/OpenCV-Computer%20Vision-5C3EE8?style=for-the-badge&logo=opencv&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-Local%20DB-003B57?style=for-the-badge&logo=sqlite&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)

</div>

![IranPlate Vision Hero](docs/hero.png)

> Real-time Persian plate detection + RTSP monitoring + bilingual dashboard, runnable in minutes.
>
> Note: This repository is maintained as a fork of [12345zahraa/Persian-Plates-Detection](https://github.com/12345zahraa/Persian-Plates-Detection).

## Demo

## Screenshots

### Home
| EN | FA |
|---|---|
| ![Home EN](docs/screenshots/home-en.png) | ![Home FA](docs/screenshots/home-fa.png) |

### Scan & Result
| EN | FA |
|---|---|
| ![Scan EN](docs/screenshots/scan-en.png) | ![Scan FA](docs/screenshots/scan-fa.png) |
| ![Result EN](docs/screenshots/result-en.png) | ![Result FA](docs/screenshots/result-fa.png) |

### RTSP Cameras
| EN | FA |
|---|---|
| ![Cameras EN](docs/screenshots/cameras-en.png) | ![Cameras FA](docs/screenshots/cameras-fa.png) |

## Why This Project Is Different | تفاوت این پروژه
- End-to-end workflow: detection + OCR + metadata + camera events in one app.
- Built-in multi-camera RTSP workers with reconnect and SSE live feed.
- Bilingual docs/messages (English + Persian) for broader adoption.

## One Command Run
```bash
make up
```
Then open `http://localhost:5000`.

`python app.py` starts Flask's development server, which is fine for local use.
For anything shared, run the bundled WSGI server instead — this is what the
Docker image does:
```bash
make serve         # waitress-serve --host=0.0.0.0 --port=5000 --threads=8 app:app
```
The first start downloads the OCR model (~50 MB) and `/detect` answers `503`
until it finishes; poll `/health` to wait for readiness.

## راهنمای فارسی سریع
این پروژه یک سامانه تشخیص پلاک ایرانی است که شامل:
- تشخیص پلاک با مدل YOLO
- OCR پلاک فارسی
- مدیریت دوربین‌های RTSP
- ثبت لاگ تردد و مدیریت لیست مجاز/غیرمجاز

### اجرای سریع
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

آدرس‌ها:
- `http://localhost:5000`
- `http://localhost:5000/scan`
- `http://localhost:5000/cameras`

### اجرای Docker
```bash
docker compose up --build
```

### تست سلامت
ابتدا سرور را بالا بیاورید، سپس:
```bash
python scripts/smoke_test.py
```

## Quick Local Run
```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
python app.py
```

## Local HTTPS (Test Only)
If you want to run local HTTPS for camera/mobile testing, create a self-signed certificate:

### Windows (PowerShell + OpenSSL)
```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout key.pem -out cert.pem \
  -subj "/C=IR/ST=Tehran/L=Tehran/O=IranPlateVision/OU=Dev/CN=localhost"
```

Then run:
```bash
python run_https.py
```

Notes:
- `cert.pem` and `key.pem` are for local development/testing only.
- For production/public deployment, use a valid certificate from a trusted CA (or your organization PKI).
- Do not commit real private keys to Git.

## Tests
No server or models needed — parsing, lookup and the whole JSON API:
```bash
make test          # python scripts/test_plates.py
```

Against a running server:
```bash
make smoke         # python scripts/smoke_test.py [base_url]
```

## Docker
```bash
docker compose up --build
```

## Project Structure
```text
.
├── app.py               # Flask routes, plate_data indexes, JSON error handling
├── models.py            # lazy model loading + serialised inference
├── plates.py            # canonical plate parsing / normalisation
├── camera_manager.py    # RTSP workers, SSE event bus
├── db.py                # SQLite schema, queries, migrations
├── run_https.py         # self-signed TLS for phone camera access
├── best.pt
├── plate_data.json
├── templates/           # menu.html · index.html (scan) · cameras.html
├── static/i18n.js
├── fonts/
├── scripts/
│   ├── test_plates.py      # offline checks, no server or models needed
│   ├── smoke_test.py        # hits a running server
│   └── test_rtsp_sources.py
├── Dockerfile
├── docker-compose.yml
├── Makefile
└── .github/
```

## API Endpoints
| Method | Path | Notes |
|---|---|---|
| `GET` | `/status` | `{ready, error, cameras, clients}` |
| `GET` | `/health` | 200 when models are loaded, 503 otherwise |
| `POST` | `/detect` | multipart `image`; max 16 MB |
| `GET` `POST` | `/api/cameras` | RTSP passwords are masked in responses |
| `GET` `PUT` `DELETE` | `/api/cameras/<id>` | |
| `POST` | `/api/cameras/<id>/toggle` | body `{enabled}` |
| `GET` | `/api/cameras/<id>/snapshot` | `{image, age}` |
| `GET` | `/api/events` | SSE: `detection`, `camera_status` |
| `GET` `DELETE` | `/api/log` | `?limit=` 1..1000 |
| `GET` | `/api/log/<id>/crop` | stored plate crop |
| `GET` `POST` | `/api/vehicles` | |
| `DELETE` | `/api/vehicles/<plate>` | |

Every error response is JSON: `{"error": "English | فارسی"}`.

### Plate format | قالب پلاک
Plates are stored and compared in one canonical form: **`24ن144-66`** —
two digits, the letter, three digits, a dash, the two-digit province code.
Input is accepted with Persian or Arabic-Indic digits, with or without
separators; `ا`/`الف`, `ه`/`ھ`, `ي`/`ی` and `ك`/`ک` are folded together.

## Configuration | تنظیمات
All optional; defaults in parentheses.

| Variable | Default | Purpose |
|---|---|---|
| `PLATE_HOST` / `PLATE_PORT` | `0.0.0.0` / `5000` | bind address |
| `PLATE_DB` | `./traffic.db` | SQLite path |
| `PLATE_LOG_LEVEL` | `INFO` | Python logging level |
| `PLATE_MAX_UPLOAD_MB` | `16` | upload cap for `/detect` |
| `PLATE_MAX_IMAGE_SIDE` | `1920` | uploads are downscaled to this |
| `PLATE_DET_CONF` | `0.4` | detector confidence threshold |
| `PLATE_WEIGHTS` | `./best.pt` | detector weights |
| `PLATE_OCR_MODEL` | `hezarai/crnn-fa-...-v2` | OCR model id |
| `PLATE_DETECT_INTERVAL` | `2.0` | seconds between RTSP detections |
| `PLATE_SNAPSHOT_FPS` | `4` | live-preview encode rate |
| `PLATE_RECONNECT_WAIT` | `5.0` | seconds before an RTSP retry |
| `PLATE_LOG_MAX_ROWS` | `5000` | access-log cap (rows store JPEG crops) |
| `PLATE_OPEN_TIMEOUT_MS` | `6000` | RTSP connect timeout |
| `PLATE_ALLOW_LOCAL_SOURCES` | unset | allow file paths as camera sources (testing only — the app has no auth) |

## Launch Checklist (Trending Pack)
- Add `docs/demo.gif` and 3 screenshots.
- Set GitHub topics: `license-plate-recognition`, `persian-ocr`, `yolo`, `flask`, `rtsp`, `computer-vision`.
- Publish release `v1.0.0` with concise release notes.
- Share launch post on X/LinkedIn/Reddit with demo.
- Keep issue/PR response fast in first 24 hours.

## Public Release Checklist
- Remove local HTTPS secrets and DB artifacts before first public push:
  `cert.pem`, `key.pem`, `traffic.db`, `traffic.db-shm`, `traffic.db-wal`.
- Keep large model files out of Git when possible (`best.pt`), or use release assets / model download step.
- Verify `.gitignore` is active in the actual Git repo root.
- Run smoke test before tagging:
  `python scripts/smoke_test.py`
- Confirm both `/scan` and `/cameras` views work in `EN` and `FA`.

## Contributing
See:
- `.github/ISSUE_TEMPLATE/bug_report.yml`
- `.github/ISSUE_TEMPLATE/feature_request.yml`
- `.github/pull_request_template.md`

## License
MIT - see `LICENSE`.
