# Silicon Dopes

Sign in with Silicon Accounts and select a personal or work identity. Work accounts see an organization board and a list for each team they belong to. Each identity also has a private personal board. Organizations owns team definitions, membership and app access.

On a board, write a description and choose **Add dope**. Any member can take an available dope or mark it done. The assignee or a board administrator can release it. Select a dope to edit it or add a completion note. The **Done** tab records who finished the work and lets members reopen it.

## Run locally

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open [Dopes locally](http://127.0.0.1:8000). Python 3.11 or later is supported.

## Configure Silicon sign-in

The registered app ID is `dope`, owned by Saket Agents. Its hosted sign-in uses PKCE and works with the repository defaults. The production callback is `https://dope.teamofsilicons.com/api/auth/callback`. The loopback callbacks support `127.0.0.1`, `localhost` and `[::1]` at `/api/auth/callback`. The hosted chooser supports linked personal and work identities.

| Environment variable | Purpose |
| --- | --- |
| `DOPE_ACCOUNTS_APP_ID` | Registered Accounts app ID. Defaults to `dope`. |
| `DOPE_ACCOUNTS_APP_SECRET` | Server-side secret for a confidential client. Alternatively, enable `public_client` on the registration and use PKCE without a secret. |
| `DOPE_PUBLIC_URL` | Public origin used in the callback and for cookie requests. Defaults to the incoming origin on loopback and `https://dope.teamofsilicons.com` elsewhere. |
| `DOPE_ACCOUNTS_URL` | Accounts origin. Defaults to `https://accounts.teamofsilicons.com`. |
| `DOPE_ORGANIZATIONS_URL` | Organizations origin. Defaults to `https://organizations.teamofsilicons.com`. |
| `DOPE_DB_PATH` | Base database path, default `./dope.db` in the repository. Sessions and board databases live in the adjacent `<path>.boards/` directory. Keep this directory private and include it in backups. |

Dopes uses authorization code sign-in with S256 PKCE and server-side refresh. On each board request it reads current membership from `/v1/integrations/{app}/me`. The account UUID and group IDs keep personal data and team data separate across handle and team renames.

## Existing installations

The original shared database has no organization or team attached to it. Choose its destination explicitly before opening that board. Get the exact group ID from `GET /api/boards`. Listing boards does not create their task databases.

```sh
# Run on the stopped installation with its normal DOPE_DB_PATH.
python -m app.storage 'team:your-org/your-team-id'
# Or use 'organization:your-org' for the organization-wide board.
```

This copies the original SQLite database, including dopes, rich descriptions, comment threads, historical commit links and assignments. It refuses to overwrite an existing board. Original users remain credited in history. Silicon identities are matched by verified account UUID. Password sessions and API keys from the global board retire at cutover.

## Board API

Use a browser session, a Dopes-scoped Accounts bearer token, or a board API key from `POST /api/me/keys`. Browser-issued keys are bound to their board and issuing session. Signing out of that session also ends its keys.

| Request | Result |
| --- | --- |
| `GET /api/boards` | Selected account and permitted boards |
| `GET /api/board` | Selected board, viewer ID and dope list |
| `POST /api/dopes` | Create with `{"description":"Review onboarding copy"}` |
| `POST /api/dopes/{id}/assign` | Take an available dope |
| `POST /api/dopes/{id}/unassign` | Release your dope |
| `POST /api/dopes/{id}/complete` | Mark done with an empty body or `{"description":"Reviewed with design"}` |
| `POST /api/dopes/{id}/uncomplete` | Reopen a dope |
| `PUT /api/dopes/{id}` | Edit its description |

Send `X-Dope-Board` with the exact `id` from discovery. Dope IDs are local to that board. When the header is absent, the API uses the first available organization board or the personal board. The browser also sends `X-Dope-Account` with the selected account UUID to detect a session switched in another tab.

The existing rich-dope, comments, categories, saved work, review, progress and diagnostics endpoints operate within the selected board. Review approvals and reset settings use the current board administrator role. See [the API schema](http://127.0.0.1:8000/docs) on a running local instance.

## Verify and deploy

Run `python -m pytest -q` with pytest installed. The browser workflow uses local test providers and checks two teammates on desktop and mobile. Install Playwright and Chromium, then run `python tests/browser_boards.py`. Set `CHROMIUM_PATH` to use an existing Chrome executable.

The systemd and nginx setup is in [deploy](deploy/). Set the production origin, registration and persistent database path in `/etc/dope.env` before shipping. The existing `deploy/ship.sh` pushes its target branch and restarts the production service.
