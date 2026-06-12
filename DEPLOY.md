# Deploying ZenReach on Railway

This app is a single always-on Flask process (gunicorn, **1 worker**) with
background threads (campaign queue, scheduler, token-health, trial auto-suspend)
and a **SQLite** database. It must run on an always-on instance with a
**persistent volume** — not on serverless (Vercel/Lambda).

> Keep it at `--workers 1`. Multiple workers would each spawn the background
> threads and fight over the SQLite file. Concurrency comes from `--threads`.

## 1. Create the service
1. Railway → **New Project → Deploy from GitHub repo** → pick `ZenReach`.
2. Railway auto-detects Python (Nixpacks) and uses `Procfile` / `railway.json`
   to start gunicorn. No build config needed.

## 2. Add a volume (so the database survives redeploys)
1. Service → **Settings → Volumes → Add Volume**.
2. Mount path: `/data`.
3. This is where the SQLite DB + generated exports live.

## 3. Set environment variables
Service → **Variables**:

| Variable | Value | Why |
|---|---|---|
| `NEXZEN_DB` | `/data/nexzen.db` | DB on the persistent volume |
| `NEXZEN_SECRET_KEY` | a long random string | encrypts WhatsApp tokens at rest (must stay constant or saved tokens become unreadable) |
| `NEXZEN_FLASK_SECRET` | a long random string | signs login sessions (constant so logins survive restarts) |
| `NEXZEN_SUPERADMIN_EMAIL` | `nexzenai1@gmail.com` | who becomes super-admin |
| `LLM_PROVIDER` | e.g. `groq` | AI assist provider (optional) |
| `LLM_API_KEY` | your key | AI assist (optional) |
| `SMTP_USER` / `SMTP_PASS` / `SMTP_SERVER` / `SMTP_PORT` | your mailbox | real invite/reset emails (optional) |
| `NEXZEN_VERIFY_TOKEN` / `NEXZEN_META_APP_SECRET` | from Meta | webhook verification (optional, for live delivered/read) |

Generate the two secrets locally:
```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## 4. Deploy
Railway builds and starts automatically. Open the generated URL:
- `/` landing · `/console` app · `/whatsapp` demo · `/mail` mailer
- Meta webhook URL (set in Meta dashboard): `https://<your-app>/webhooks/meta`

## 5. First login
Sign up at `/console` with `NEXZEN_SUPERADMIN_EMAIL` → the 🛡️ Admin panel unlocks.

## Notes
- `exports/` is written under the app dir by default; for durability set the
  working files onto the volume or treat exports as transient (regenerate on demand).
- To scale beyond one instance later, move SQLite → Postgres and the in-process
  queue → Redis/RQ. The data layer in `nexzen/db.py` is the single seam to change.
