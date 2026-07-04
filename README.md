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

## API

Every endpoint works with the browser session cookie or an API key created in
Profile → API keys, sent as `Authorization: Bearer <key>`.

### Comments

Each dope has a comment thread. Mentions are extracted from the body by
matching `@username` or `@display name` against real users.

- `GET /api/users` — all members, each with `online` (active in the last 5
  minutes) and `last_seen_at`. Used for the `@` mention picker.
- `GET /api/dopes/{id}/comments` — the thread, oldest first. Each comment has
  `id`, `body`, `created_at`, `user`, and `mentions`.
- `POST /api/dopes/{id}/comments` — body `{"body": "text with @mentions"}`.
- `DELETE /api/dopes/{id}/comments/{comment_id}` — author only.
- `POST /api/dopes/{id}/comments/read` — mark the thread read for the caller.

Dope payloads from `GET /api/dopes` include `comment_count`,
`unread_comments`, `unread_mentions`, and `latest_comment_at` for the caller,
which power the new-message badges.

### Diagnostics

- `GET /api/diagnostics?limit=50` — team-wide overview (in the UI it's a
  dedicated page at `#diagnostics`, opened by the pulse icon, top right):
  - `totals` — active/completed/archived counts and minutes, plus how much
    is remaining split into `ready`, `in_progress`, and `blocked`.
  - `remaining_by_category` — open work grouped by category.
  - `per_person` — per member: completed (all-time and last 7 days),
    in-progress load, dopes created, comments posted, online status.
  - `activity` — newest-first feed of everything that happened: created,
    assigned, unassigned (with reason), completed, archived, edited,
    commented. `limit` caps the feed (max 200).

```bash
curl -s -H "Authorization: Bearer $DOPE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"body": "Deployed, please verify @saket"}' \
  https://example.com/api/dopes/42/comments
```

## Deploy

The app runs as a systemd service behind nginx (see `deploy/`). To ship from
your laptop in one command:

```bash
./deploy/ship.sh
```

It pushes `main`, runs the server-side deploy over SSH, and verifies the
deployed commit matches local. It expects the server key at `~/.ssh/dope.pem`
(or `~/Downloads/dope.pem`); override with `DOPE_SSH_KEY`, `DOPE_SSH_HOST`,
or `DOPE_SSH_USER` if your setup differs.

Alternatively, run the server-side script directly on the box:

```bash
sudo /srv/dope/deploy/deploy.sh
```

It fetches `main`, syncs dependencies, restarts the `dope` service, and runs a
smoke check. DB migrations run automatically on startup.
