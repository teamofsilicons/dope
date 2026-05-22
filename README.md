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
