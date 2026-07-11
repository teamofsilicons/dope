---
name: verify
description: Build/launch/drive recipe for verifying Dope (FastAPI + vanilla JS) changes end-to-end in a real browser.
---

# Verifying Dope changes

## Launch (isolated DB — never run against ./dope.db)

```bash
cd <repo>
DOPE_DB_PATH=/path/to/scratch/verify.db .venv/bin/uvicorn app.main:app --port 8787 &
```

Migrations run on startup; a fresh DB path gives a clean instance. The venv at
`.venv/` already has uvicorn and all deps.

## Seed via API (curl + cookie jar)

- Signup requires `display_name`: `POST /api/auth/signup {"username","password","display_name"}`
- Login sets the `dope_session` cookie: `POST /api/auth/login` with `-c jar.txt`
- Create dope: `POST /api/dopes {"title","description_html","time_text":"2h"}` (`time_text`, not `time_minutes`)
- Assign to self: `POST /api/dopes/{id}/assign`
- Send for review: `POST /api/dopes/{id}/review {"note","branch_url"}` (both required)
- Complete: `POST /api/dopes/{id}/complete {"completion_text":"... https://a-commit-link"}` (needs ≥1 http link)
- Reviewers are hardcoded usernames in `app/main.py` (`REVIEWER_USERNAMES = {"saket", "brainspoof"}`) — sign up as `saket` to see review-queue UI.

## Drive the UI

Playwright (installed for system `python3`, chromium cached). Inject the
`dope_session` cookie from the curl jar into the browser context, then
`page.goto("http://127.0.0.1:8787")`. Routes are hash-based: `#active`,
`#completed`, `#review`, `#archived`, `#diagnostics`. Open a dope's modal by
clicking its title text. Listen for `pageerror`/console errors — the whole UI
is one file, `app/static/app.js`, so a render bug usually throws.

## Gotchas

- The dope detail modal re-renders on every open; button handlers are wired
  by id (`#complete`, `#assign`, `#send-review`, …) right after render.
- Mobile breakpoint media query is at the bottom of `app/static/app.css`.
