---
name: verify
description: Verify Silicon Dopes with an isolated database and a real desktop and mobile browser.
---

# Verify Silicon Dopes

Install the service dependencies, pytest and Playwright in the project virtual environment. Install Chromium with `python -m playwright install chromium` or set `CHROMIUM_PATH` to an existing Chrome executable.

```sh
.venv/bin/python -m pytest -q
.venv/bin/python tests/browser_boards.py
```

The browser check starts Dopes and local Accounts and Organizations test endpoints on ephemeral loopback ports, uses a temporary database and closes the servers when it finishes. It drives the hosted chooser, two teammates sharing a board, creating, taking, completing with and without a note, reopening, switching to a personal account, and signing out. Desktop uses the board sidebar. Mobile uses the board selector.

For manual testing with a registered Silicon app, set `DOPE_DB_PATH` to a scratch path and configure its callback as described in the README. Open Dopes and use **Sign in with Silicon Accounts**. A work identity sees only its current Organizations groups. Use the provided account switcher to check a personal identity.
