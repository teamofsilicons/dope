# Dope

A simple FastAPI-backed task tracker for product teams. A feature/task is a "Dope": title, rich description with images, and an estimated time to complete.

## Local

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

## Environment

- `DOPE_DB_PATH`: optional SQLite path. Defaults to `./dope.db`.
- `DOPE_SECRET_KEY`: optional cookie signing secret. Set this in production.

## Deploy

The app runs as a systemd service behind nginx (see `deploy/`). To ship the
latest `main` onto the server, run on the box:

```bash
sudo /srv/dope/deploy/deploy.sh
```

It fetches `main`, syncs dependencies, restarts the `dope` service, and runs a
smoke check. DB migrations run automatically on startup.
