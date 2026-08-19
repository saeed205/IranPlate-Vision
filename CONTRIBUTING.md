# Contributing to IranPlate Vision

Thanks for your interest. Issues and pull requests are both welcome, and you do
not need to be an expert in computer vision to help — a good bug report with a
sample image is genuinely useful.

## Development setup

```bash
git clone https://github.com/saeed205/IranPlate-Vision.git
cd IranPlate-Vision
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate

pip install -r requirements-dev.txt   # tests + linter, installs in seconds
pip install -r requirements.txt       # add this only when you need real inference
python app.py
```

`requirements-dev.txt` is deliberately small: the detector and OCR models load
lazily, so the test suites run without ultralytics, torch or hezar. Install the
full `requirements.txt` when you are changing detection or OCR behaviour.

## Before opening a pull request

```bash
make test      # 69 offline checks — no server, no models needed
make smoke     # every endpoint against a running server (start it first)
ruff check .   # lint; must be clean
```

CI runs exactly these on Python 3.10–3.12, so a green local run usually means a
green PR.

### Checklist

- Keep the scope focused. One concern per pull request.
- Add or extend a check in `scripts/test_plates.py` or
  `scripts/test_camera_worker.py` for any behaviour you fix or add.
- Update the docs if setup, configuration or behaviour changed — including
  [README.fa.md](README.fa.md), which is the Persian counterpart of the README.
- Include screenshots for UI changes (`/scan`, `/cameras`) in **both** English
  and Persian, since the layout is direction-sensitive.
- Confirm no local artifacts are staged: `cert.pem`, `key.pem`, `traffic.db`,
  `*.db-shm`, `*.db-wal`. These are git-ignored, but check anyway.

## Commit messages

Conventional prefixes, imperative mood:

```
feat: support motorcycle plate format
fix: reject plates with a non-Persian letter
docs: document PLATE_SNAPSHOT_FPS
refactor: extract the snapshot encoder
test: cover the gone-then-returning plate path
chore: bump ruff
```

Explain **why** in the body when the reason is not obvious from the diff. If you
are fixing a bug, say what the wrong behaviour was.

## Things worth knowing about this codebase

**Plates have one canonical form.** Everything is normalised to `24ن144-66` by
`plates.py`. Never compare or store a raw OCR string — call `plates.normalize()`
first. Both the HTTP API and the RTSP workers go through this module so their
values always match; skipping it is what previously broke the allow/block list.

**Inference is serialised.** `models.py` holds a lock around every model call
because torch modules are not safe to invoke from several threads at once, and
Flask request threads run alongside one worker thread per camera. If you add a
code path that touches the models, go through `models.py`.

**The app has no authentication.** That constraint shapes several decisions —
camera sources are restricted to network schemes, RTSP passwords are masked in
responses, and untrusted text is escaped before rendering. Please read
[SECURITY.md](SECURITY.md) before adding an endpoint that accepts a path, a URL,
or anything that gets rendered into a page.

**The UI is bilingual at runtime.** Strings are written as
`"English | فارسی"` and split by `static/i18n.js`. If you build DOM from
JavaScript, wrap user-visible text in `__tPipe()`, escape data with `__esc()`,
and register a re-render callback via `I18N.onChange()` so switching language
does not leave stale text behind.

## Reporting a bug

Include:

- Steps to reproduce, and expected versus actual behaviour
- Relevant log output (`PLATE_LOG_LEVEL=DEBUG` gives more)
- Environment: OS, Python version, browser
- For detection problems: a sample image, if you can share one
- For RTSP problems: the **shape** of your URL, with credentials removed —
  `rtsp://<user>:<pass>@192.168.1.x:554/stream1`

Never attach real credentials, or footage of identifiable people or plates.

## Security issues

Do not open a public issue. Use
[private vulnerability reporting](https://github.com/saeed205/IranPlate-Vision/security/advisories/new)
instead. See [SECURITY.md](SECURITY.md).

## Code of Conduct

Participation is covered by the [Code of Conduct](CODE_OF_CONDUCT.md).
