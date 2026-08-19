# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

Use [GitHub's private vulnerability reporting](https://github.com/saeed205/IranPlate-Vision/security/advisories/new)
for this repository, or contact the maintainer directly. Include what you found,
how to reproduce it, and the impact you think it has. Expect a first reply within
a few days.

## Supported versions

Only `main` receives fixes. There is no long-term support branch.

## Threat model — read this before deploying

**IranPlate Vision ships with no authentication, no authorisation, and no
transport encryption by default.** Anyone who can reach the port can view every
camera, read the full access log, and add or delete cameras and plates. It is
built for a trusted LAN or a single workstation, not for the public internet.

If you expose it beyond a trusted network, put it behind a reverse proxy that
terminates TLS and enforces authentication, and restrict access by IP.

### What the application does defend against

| Concern | Handling |
|---|---|
| Camera source paths | Restricted to `rtsp://`, `rtsps://`, `http://`, `https://` or a device index. Local file paths need the explicit `PLATE_ALLOW_LOCAL_SOURCES=1`, because the snapshot endpoint would otherwise read arbitrary files back to any caller. |
| RTSP credentials | Masked in every API response. Editing a camera and sending the masked URL back preserves the stored password rather than overwriting it. |
| Untrusted text in the UI | Camera names, plates, labels and notes are HTML-escaped before rendering; event handlers are delegated rather than built into markup. |
| Upload size | Capped by `PLATE_MAX_UPLOAD_MB` (16 MB default); oversized requests get a JSON 413. |
| Access log growth | Capped by `PLATE_LOG_MAX_ROWS`, since each row stores a JPEG crop. |

### What it deliberately does not do

- No login, sessions, API keys, rate limiting, or audit of who changed what.
- No CSRF protection — a browser on the same network can be made to POST to the
  API. Do not run this alongside untrusted web content on the same machine.
- The bundled `run_https.py` generates a **self-signed** certificate. It exists
  so phone browsers will grant camera access (`getUserMedia` needs a secure
  origin), not to authenticate the server.

## Handling secrets

`cert.pem`, `key.pem` and `traffic.db` are git-ignored. If you add configuration,
pass it through environment variables rather than committing it. Detection crops
stored in the access log may be personal data under your local law — mind the
retention cap and who can reach the dashboard.
