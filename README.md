# Silicon Dopes

Sign in with Silicon Accounts and select a personal or work identity. Work accounts see an organization board and a list for each team they belong to. Each identity also has a private personal board. Organizations owns team definitions, membership and app access.

Use **New Dope** to write a description. Anyone on the board can take or complete work. **Doped** finishes it immediately. **Completion note…** adds an optional note or, for an administrator, credits another member. The original progress chart, rich cards, comments and mentions, dependencies, categories, saved work, review queue, history and diagnostics are available in each board.

The **+** in the boards sidebar creates lists in a personal, organization or team scope the signed-in identity manages. Owners can invite guests to a private board. Recipients accept in **Invitations** and open the board under **Shared with me**. Organization and team boards follow live group membership. Review and board settings use the same administrator roles.

**Contacts** connects the signed-in identity’s Silicon Contacts address book, including private names, photos and trust from 1 to 255. Recipient suggestions rank invitations sent twice as heavily as invitations received, then compare recency and trust. **Files** accepts attachments up to 10 MiB and filters files shared with the viewer by sender, saved contact and minimum trust.

## Run locally

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open [Dopes locally](http://127.0.0.1:8000). Python 3.11 or later is supported.

## Configure Silicon sign-in

The registered app ID is `dope`, owned by Saket Agents. Its hosted sign-in uses PKCE and works with the repository defaults. The production callback is `https://dopes.teamofsilicons.com/api/auth/callback`. The loopback callbacks support `127.0.0.1`, `localhost` and `[::1]` at `/api/auth/callback`. The hosted chooser supports linked personal and work identities.

| Environment variable | Purpose |
| --- | --- |
| `DOPE_ACCOUNTS_APP_ID` | Registered Accounts app ID. Defaults to `dope`. |
| `DOPE_ACCOUNTS_APP_SECRET` | Server-only app secret for Interface token introspection, recipient lookup and Contacts proofs. Hosted PKCE alone can run as a public client. |
| `DOPE_REGISTRATION_FILE` | Alternative to the secret environment variable. Read `app_secret` from a private registration JSON file at this path. |
| `DOPE_CONTACTS_URL` | Contacts origin. Defaults to `https://contacts.teamofsilicons.com`. |
| `DOPE_PUBLIC_URL` | Public origin used in the callback and for cookie requests. Defaults to the incoming origin on loopback and `https://dopes.teamofsilicons.com` elsewhere. |
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

## Interface API

The [API guide](docs/interface-api.md) covers sign-in, board permissions, requests and responses, file filters, recipient lookup, pagination and retries. The [OpenAPI document](docs/openapi.json) can be imported into interface tooling. A running server also hosts `/docs`, `/openapi.json` and `/interface-api`.

## Verify and deploy

Run `python -m pytest -q` with pytest installed. The browser workflow uses local test providers and checks two teammates on desktop and mobile. Install Playwright and Chromium, then run `python tests/browser_boards.py`. Set `CHROMIUM_PATH` to use an existing Chrome executable.

Production runs at [dopes.teamofsilicons.com](https://dopes.teamofsilicons.com) on the `silicon-dopes-prod` Lightsail instance in `us-east-1`. The Linux Nano plan has 512 MB of RAM, a 20 GB SSD and one static IPv4 address. Nginx terminates HTTPS and Certbot renews the certificate using the webroot at `/var/www/letsencrypt`. Systemd runs a single application worker as `dope`.

The host keeps `DOPE_PUBLIC_URL`, `DOPE_REGISTRATION_FILE` and `DOPE_DB_PATH` in `/etc/dope.env`. The app registration is readable by the service group at `/etc/dope/registration.json`, and board data is in `/var/lib/dope/dope.db.boards/`.

Run `./deploy/ship.sh` from the clean release branch. It uses the `silicon-deployment` AWS profile for temporary SSH credentials and verifies the host keys supplied by Lightsail. The instance firewall allows SSH from the deployment network and the Lightsail browser console. The server checks the requested Git revision and tests the API after restarting.
