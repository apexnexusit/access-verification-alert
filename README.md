# Consent-based access selfie alerts

A minimal HTTPS web page that explicitly asks a visitor to press a button and grant camera access. It captures one JPEG selfie only after that consent and forwards it to:

1. the configured Ghozo.JS Gmail account, and
2. the configured Hermes Telegram home chat (`Mohamad Kassem`).

Images are streamed to those destinations then deleted from the server. They are never publicly accessible.

## Run

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
set -a; source .env; set +a  # optional allowed-IP settings
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8787
```

## Allowed IPs

Create a local `.env` (never commit it):

```bash
ALLOWED_IPS=203.0.113.41,198.51.100.0/24
```

Allowed addresses get `{ "status": "allowed" }` from the API and do not initiate delivery. Behind a reverse proxy, configure trusted proxy handling before relying on IP addresses; the application deliberately does not trust arbitrary `X-Forwarded-For` headers.

## Security notes

- HTTPS is required by modern browsers for camera access. The public Cloudflare URL supplies HTTPS.
- `POST /api/selfie` accepts JPEG only, up to 5 MB, and rate-limits each source IP to one capture per 90 seconds.
- No camera capture occurs without the page copy, user button press, and browser permission.
- The recipient email is read from the existing Hermes email configuration at delivery time; no credentials are duplicated in this project.
