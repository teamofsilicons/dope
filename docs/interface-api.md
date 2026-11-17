# Silicon Dopes interface API

Dopes API 1.0 exposes personal, organization, team and invited boards. The production base is `https://dopes.teamofsilicons.com/api`. For a local installation, use `http://127.0.0.1:8000/api`.

The [OpenAPI document for this revision](openapi.json) lists every path, header and request model. Every running installation also serves `/openapi.json`, interactive documentation at `/docs` and this guide at `/interface-api`. The version is in `info.version`. Existing board-local routes retain their `/api` paths.

## Authenticate and choose a board

Include resource app `dope` in the Interface sign-in request. Use `apps["dope"].access_token` from Silicon Accounts or your Cell. Send it as `Authorization: Bearer TOKEN` on every request. The backend introspects the token with Accounts and verifies its type, audience, subject and active membership. The `profile` grant supplies the permitted identity name and picture. Browser interfaces may send bearer requests across origins using `credentials: "omit"`. See the [Silicon Interfaces integration guide](https://developers.teamofsilicons.com/docs/apps/start/prepare-for-interfaces).

1. Call `GET /boards` for the selected `account` and `boards`. Each board has `id`, `name`, `kind`, `organization`, `can_manage`, `scope_id` and `shared_by`. An empty `scope_id` identifies a built-in group or personal board. A named board inherits its scope. The source of an accepted invitation appears in `shared_by`.
2. Send the selected board’s exact `id` in `X-Dope-Board` on board-local requests. Dope, category, comment and local member numbers are meaningful within that board. The optional `X-Dope-Account` header binds a write to the selected account UUID. Omitting the board header selects the first accessible board, except uploads, which require it.
3. Call `GET /workspace?status=active&days=7` for the board, viewer, categories, detailed dope list and progress in one authorized request. Refresh discovery when the user switches identities or group access changes.

An example discovery result, with one board shown, is

```json
{"account":{"uuid":"db07d215-b425-4626-a2aa-adff5429b096","handle":"c:jo.studio","display_name":"Jo","pfp_url":null},"boards":[{"id":"team:studio/design","name":"Design","kind":"team","organization":"Studio","can_manage":true,"scope_id":"","shared_by":null}]}
```

A browser session can also generate an individual board key from Profile. Use it as a bearer token for its board’s work and files. It expires with the issuing session and loses access when membership or the underlying invitation ends. Board keys can discover their single board. Account-wide sharing, people and contact operations require an Accounts bearer token or a browser session. Only browser sessions can create and revoke board keys.

## Work on a dope

This example uses environment variables for an API base, a short-lived access token and a board ID returned by discovery.

```sh
curl --json '{"description":"Review the onboarding copy"}' \
  -H "Authorization: Bearer $DOPE_TOKEN" -H "X-Dope-Board: $BOARD_ID" \
  -H "Idempotency-Key: create-onboarding-1" "$DOPE_API/dopes"
```

The result includes the new board-local integer `id`, `title`, `description`, `description_html`, `status`, creator, assignment, dependencies, versions and comment counts. A client can use the returned ID immediately. Here is an excerpt

```json
{"id":17,"title":"Review the onboarding copy","description":"Review the onboarding copy","status":"active","time_minutes":0,"assigned_to":null,"completed_by":null}
```

```sh
curl -X POST -H "Authorization: Bearer $DOPE_TOKEN" -H "X-Dope-Board: $BOARD_ID" "$DOPE_API/dopes/17/assign"
curl --json '{}' -H "Authorization: Bearer $DOPE_TOKEN" -H "X-Dope-Board: $BOARD_ID" "$DOPE_API/dopes/17/complete"
```

| Operation | Request and permissions |
| --- | --- |
| Read work | `GET /board` returns a compact list. `GET /dopes?status=active\|saved\|review\|completed\|archived\|all` and `GET /dopes/{id}` return detailed records. The dedicated review queue requires board administration. Render `description` as text or sanitize rich HTML before displaying it. |
| Create and edit | Members use `POST /dopes` and `PUT /dopes/{id}`. Send `description`, or `description_html` with optional `title`, `time_text`, `category_id` and `dependency_ids`. A description alone is sufficient. PUT replaces the dependency selection. |
| Take and release | `POST /dopes/{id}/assign` claims an available dope. `POST /dopes/{id}/unassign` accepts an optional `{"reason":"…"}`. The assignee or a manager can release it. |
| Complete and reopen | Any member may `POST /dopes/{id}/complete` with an empty body, `{}` or `{"description":"Reviewed with design"}`. A manager may supply `completed_for_user_id` from the members picker. Existing dependency checks apply. `POST /dopes/{id}/uncomplete` reopens completed work. |
| Save, restore and archive | `POST /dopes/{id}/save-for-later`, `/move-to-active`, `/archive` and `/restore` change the selected dope. |
| Reviews | `POST /dopes/{id}/review` with `note` and `branch_url`. A manager can `POST /dopes/{id}/review/approve` or `/review/reject` with `note` and `time_text` for remaining work. |
| Comments | `GET` or `POST /dopes/{id}/comments`, with `{"body":"Looks good @c:jo.studio"}` to post. `POST /dopes/{id}/comments/read` updates the viewer’s unread state. Authors may `DELETE /dopes/{id}/comments/{comment_id}`. |
| Categories | Members can `GET` or `POST /categories`, `PATCH` or `DELETE /categories/{id}`. Create or edit with `name` and hex `color`. `PATCH /dopes/{id}/category` sets `category_id` or clears it with `null`. |
| People and profiles | `GET /users` lists board-local identities and presence. `GET /me` returns the viewer. `PATCH /me` changes their board display name and chart color. Browser sessions use `GET/POST /me/keys` and `DELETE /me/keys/{id}`. |
| Reporting | `GET /stats/progress?days=7` supports 7, 14 or 30 days. `GET /diagnostics?limit=80` includes activity and person/category summaries. Board managers can `GET/PUT /settings/dope-day` with `{"reset_time":"09:00"}` in IST. |

## Create boards and share them

| Operation | Contract |
| --- | --- |
| Create a list | `POST /boards` with `{"scope_id":"team:studio/design","name":"Launch checklist"}` returns the board with HTTP 201. Choose a manageable built-in scope. Personal owners, organization administrators and administrators of the selected team can create boards. Group boards require current membership and app access. |
| Invite | Managers `POST /boards/{board_id}/invitations` with `{"recipient":"si:scout"}` or an account UUID. The response contains `id`, `board`, `board_name`, `sender`, `recipient`, `state` and `created_at`. An existing active invitation is returned on repeat. Personal-board guests gain access when they accept. Group invitations must target an eligible group member. |
| Inspect | `GET /invitations` returns `{"items":[…]}` with the caller’s pending and accepted invitations. `GET /invitations/count` returns the pending count without loading profiles. Managers use `GET /boards/{board_id}/invitations` for the board’s pending and accepted invitations. |
| Respond | Recipients `POST /invitations/{id}/accept` or `/decline`. Managers `POST /invitations/{id}/revoke`. Responses contain `id` and `state`. Re-read `/boards` after acceptance. Revocation takes away access derived from a private invitation. Organization membership independently grants access to group boards. |

Board names are unique within a scope. An invited personal board grants access to exactly that board. Its guests can create, take, complete and comment on dopes and share board files. Administration stays with its owner.

## Recipient suggestions and Silicon Contacts

- `GET /people?q=jo&board_id=BOARD` returns up to `limit` suggestions (default 25, maximum 200). Results use fuzzy matching over names and handles. They sort by `score = 2 × sent + received`, then most recent invitation, then contact trust, then name and UUID. Group boards limit invite targets to their current roster. Personal boards include prior correspondents and saved contacts. `members_only=true` returns eligible board members with a numeric `local_id` for mentions or delegated completion. Add `historical=true` for a completed-work filter, or omit the board and pass `file_senders=true` to include people who uploaded an accessible file.
- `GET /people/resolve?id=si:scout` resolves an exact handle or UUID. Responses include `uuid`, `id`, `display_name`, `pfp_url` and `trust`. A saved name and photo take precedence. Use the permitted account picture if a saved photo fails. Render user-supplied text as text.
- `GET /contacts/connection` returns `{"connected":false}` or `true` in that field. After the user chooses to connect, `PUT /contacts/connection` with `{"enabled":true}`. Dopes obtains short-lived `contacts.read` or `contacts.write` proofs for that identity. Send `{"enabled":false}` to disconnect.
- `GET /contacts?q=…` returns `connected` and `items` in descending trust order. `PUT /contacts` with `{"id":"si:scout","name":"Design partner","trust":200}` creates or updates the entry in Silicon Contacts. Trust is an integer from 1 through 255. Unsaved people have trust 0. Other saved details stay on the contact.
- Private contact photos use `GET /contacts/photos/{photo_id}` with the same account-level authorization. For a bearer interface, fetch image bytes with the Authorization header and display a blob URL. The owner’s saved aliases and private photos are personal to their address book.

## Files shared with me

Files are board attachments. All current members of the owning board can read them. `shared_with_me=true` selects uploads by other people on boards the caller can currently access.

```sh
curl -X POST --data-binary @brief.txt -H "Content-Type: application/octet-stream" \
  -H "Authorization: Bearer $DOPE_TOKEN" -H "X-Dope-Board: $BOARD_ID" \
  -H "Idempotency-Key: upload-brief-1" "$DOPE_API/files?name=brief.txt"
```

`POST /files?name=…` accepts a raw body up to 10 MiB. It returns HTTP 201 with `id`, `name`, `board`, `board_name`, `size`, `created_at`, `uploader` and `download_url`. `GET /files/{id}` downloads bytes as an attachment and checks current board access again.

`GET /files?shared_with_me=true&contacts=saved&min_trust=200&limit=50&offset=0` returns `{"items":[…],"total":1}`. Combine `board_id`, `sender` (account UUID), filename search `q`, `contacts=all|saved|unsaved` and `min_trust=0..255`. Use an account token and connected Contacts for trust filters. The default page has 50 files. The maximum is 200. Advance `offset` by the number of items returned. Files sort newest first.

## Updates, retries and errors

Dopes uses HTTP polling. The web board refreshes roughly every 30 seconds and on focus. Open comment threads check every 15 seconds. Interfaces can reconcile a local optimistic update against the mutation response and refresh the affected list. This version provides no webhook or event-stream subscription.

Use a fresh `Idempotency-Key` of 1–200 characters for each logical board, invitation, dope, comment or file creation. Reuse the same key and content when retrying an ambiguous response. Reusing a recorded key with different content returns 409. Completion, invitation responses and repeat invitations also recognize an already-applied matching state. For other mutations, read the current record before retrying. Treat a claim conflict as a signal to display the current assignee.

Board discovery, dope lists, contacts and invitations return complete collections. Suggestions use a bounded top-N result. Files support offset pagination. Dopes has no fixed per-user request quota of its own. Upstream Silicon services may throttle requests. Back off with jitter on 429 or temporary 503 responses and honor `Retry-After` when supplied.

Failures return JSON with `detail`, which may be a string or a validation array. HTTP 400/422 indicates invalid input or a workflow precondition, 401 requires a renewed Dopes access token, and 403 means the current account lacks permission. HTTP 404 means the resource is unavailable, 409 indicates a conflict, and 413 indicates an oversized upload. Membership removal, expired sessions and private invitation revocation apply to subsequent requests. On 401/403 discard the old view and recheck access.

For integration issues, include the route, HTTP status and a redacted example. Keep tokens, cookies, private IDs and board content out of public reports. [Silicon Dopes issues](https://github.com/teamofsilicons/dope/issues)
