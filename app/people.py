"""Recipient lookup, invitation signals, and the user's consented Silicon Contacts address book."""
from __future__ import annotations

import os
import re
import secrets
import time
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from . import accounts
from . import views
from .accounts import Access, account_access
from .catalog import catalog
from .storage import board_path, db

router = APIRouter(tags=["People and contacts"])
HANDLE = re.compile(r"(?:c|si):[a-z0-9][a-z0-9._-]*", re.I)


def remote(method, url, *, binary=False, **options):
    try:
        r = accounts.http.request(method, url, **options)
    except httpx.HTTPError:
        raise HTTPException(503, "Silicon could not be reached. Try again.") from None
    if not r.is_success:
        status = r.status_code if r.status_code in (400, 403, 404, 409, 422, 429) else 503
        try:
            error = r.json().get("error", {})
            message = error.get("message") if isinstance(error, dict) else None
        except (ValueError, AttributeError):
            message = None
        raise HTTPException(status, message or "Silicon could not complete this request.")
    if binary:
        return r.content
    if r.status_code == 204:
        return {}
    try:
        return r.json()
    except ValueError:
        raise HTTPException(503, "Silicon returned an incomplete response.") from None


def connection(owner):
    with catalog() as conn:
        row = conn.execute("SELECT generation FROM contacts_connections WHERE owner=?", (owner,)).fetchone()
    return row[0] if row else None


def proof(context, scope, generation):
    owner = context.account.uuid
    session = accounts.digest(context.token)
    now = int(time.time())
    with catalog() as conn:
        cached = conn.execute("SELECT proof FROM contacts_proofs JOIN contacts_connections USING(owner) WHERE owner=? AND session=? AND scope=? AND expires>? AND generation=?",
                              (owner, session, scope, now + 5, generation)).fetchone()
    if cached:
        return cached[0]
    key = accounts.digest(f"{generation}/{session}/{scope}/{now // 120}")
    result = remote("POST", accounts.accounts_url() + "/v1/proofs/user-verification",
                    auth=accounts.app_credentials(), headers={"Idempotency-Key": key},
                    json={"subject_token": context.token, "receiving_app": "silicon-contacts",
                          "scopes": [scope], "access_ttl_seconds": 300})
    token = result.get("proof_token")
    if not token or not result.get("proof_id"):
        raise HTTPException(503, "Contacts authorization is unavailable.")
    expiry = result.get("expires_at")
    try:
        until = min(now + 300, int(datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp())) if expiry else now + 120
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(503, "Contacts returned an invalid proof lifetime.") from None
    with catalog(write=True) as conn:
        row = conn.execute("SELECT generation FROM contacts_connections WHERE owner=?", (owner,)).fetchone()
        if not row or row[0] != generation:
            raise HTTPException(403, "Silicon Contacts has been disconnected.")
        conn.execute("DELETE FROM contacts_proofs WHERE expires<=?", (now,))
        conn.execute("INSERT OR REPLACE INTO contacts_proofs VALUES (?,?,?,?,?,?)",
                     (owner, session, scope, result["proof_id"], token, until))
    return token


def contacts_request(context, method, path, *, scope="contacts.read", **options):
    generation = connection(context.account.uuid)
    if not generation:
        raise HTTPException(403, "Connect Silicon Contacts to use your address book.")
    base = os.getenv("DOPE_CONTACTS_URL", "https://contacts.teamofsilicons.com").rstrip("/")
    return remote(method, base + "/v1/" + path, headers={"Authorization": "Proof " + proof(context, scope, generation)}, **options)


def public_person(record):
    uid = record["uuid"]
    handle = record.get("id") or record.get("handle") or uid
    kind = "silicon" if handle.startswith("si:") else "carbon" if handle.startswith("c:") else None
    default = f"https://iris.teamofsilicons.com/pfp/{kind}?id={quote(uid, safe='')}" if kind else None
    return {"uuid": uid, "id": handle, "display_name": record.get("display_name") or handle,
            "pfp_url": record.get("pfp_url") or record.get("picture") or default, "trust": 0}


def saved_person(row):
    person = public_person({**row.get("account", {}), "uuid": row["uuid"]})
    person.update(display_name=row.get("name") or person["display_name"], trust=row.get("trust", 1),
                  fallback_pfp_url=person["pfp_url"], blocked=bool(row.get("blocked")))
    if row.get("photo_id"):
        person["pfp_url"] = "/api/contacts/photos/" + quote(row["photo_id"], safe="")
    return person


def address_book(context):
    if not connection(context.account.uuid):
        return {}
    items = {}
    offset = 0
    while True:
        page = contacts_request(context, "GET", "contacts", params={"limit": 200, "offset": offset})
        rows = page["items"]
        items.update((r["uuid"], saved_person(r)) for r in rows)
        offset += len(rows)
        if not rows or offset >= page["total"]:
            return items


def directory(context, value):
    value = value.strip()
    try:
        target = str(UUID(value))
    except ValueError:
        if not HANDLE.fullmatch(value):
            raise HTTPException(422, "Enter a Carbon ID (c:…), Silicon ID (si:…) or account UUID.") from None
        target = "by-id/" + quote(value, safe="")
    if connection(context.account.uuid):
        data = contacts_request(context, "GET", "lookup", params={"q": value})
    else:
        base = accounts.accounts_url()
        credentials = accounts.app_credentials()
        data = remote("GET", f"{base}/v1/accounts/{target}", auth=credentials)
        if not data.get("pfp_url"):
            try:
                member = remote("GET", f"{base}/v1/apps/{accounts.app_id()}/users/{data['uuid']}", auth=credentials)
                if member.get("status") == "active" and "profile" in member.get("granted_scopes", []):
                    data.update(display_name=member.get("display_name"), pfp_url=member.get("pfp_url"))
            except HTTPException as error:
                if error.status_code != 404:
                    raise
    if data.get("status", "active") != "active":
        raise HTTPException(404, "Account is unavailable.")
    return public_person(data)


def selected(context, board_id):
    board = next((b for b in context.boards if b.id == board_id), None)
    if not board:
        raise HTTPException(403, "You do not have access to this board.")
    return board


def org_members(context, board):
    scope = board.scope.split(":", 1)[1]
    org, _, team = scope.partition("/")
    base = os.getenv("DOPE_ORGANIZATIONS_URL", "https://organizations.teamofsilicons.com").rstrip("/")
    record = accounts.upstream("GET", f"{base}/v1/integrations/{accounts.app_id()}/orgs/{quote(org, safe='')}",
                               headers={"Authorization": "Bearer " + context.token})
    eligible = None
    if team:
        eligible = set(next((t["members"] for t in record["teams"] if t["id"] == team), []))
    return {m["uuid"]: public_person(m) for m in record["members"]
            if not m.get("removed_at") and (eligible is None or m["uuid"] in eligible)}


def participants(context, board):
    if board.kind != "personal":
        return org_members(context, board)
    owner = board.scope.split(":", 1)[1]
    with catalog() as conn:
        rows = conn.execute("""SELECT p.* FROM profiles p WHERE p.uuid=? OR p.uuid IN (
            SELECT recipient FROM invitations WHERE board=? AND state='accepted')""", (owner, board.id)).fetchall()
    members = {row["uuid"]: public_person(dict(row)) for row in rows}
    actor = context.account
    members.setdefault(actor.uuid, public_person({"uuid": actor.uuid, "id": actor.handle,
                                                 "display_name": actor.display_name, "pfp_url": actor.pfp_url}))
    return members


def local_members(board_id, roster, people):
    # Assign board-local IDs lazily so someone can be mentioned before their first visit.
    from .main import init_db
    path = board_path(board_id)
    init_db(path)
    with db(path, write=True) as conn:
        for person in people:
            if person.get("local_id") or person["uuid"] not in roster:
                continue
            identity = roster[person["uuid"]]
            conn.execute("""INSERT INTO users (username,password_hash,display_name,created_at,account_uuid,account_handle,accounts_name)
                            VALUES (?,'',?,?,?,?,?) ON CONFLICT(account_uuid) DO NOTHING""",
                         ("accounts/" + secrets.token_hex(16), identity["display_name"], datetime.now(timezone.utc).isoformat(),
                          identity["uuid"], identity["id"], identity["display_name"]))
            person["local_id"] = conn.execute("SELECT id FROM users WHERE account_uuid=?", (identity["uuid"],)).fetchone()[0]


def normalize(text):
    return "".join(c for c in unicodedata.normalize("NFKD", str(text).casefold()) if c.isalnum())


def fuzzy(query, person):
    needle = normalize(query)
    if not needle:
        return True
    values = [person.get("id", ""), person.get("display_name", ""), *person.get("display_name", "").split()]
    for text in values:
        value = normalize(text)
        letters = iter(value)
        if needle in value or all(c in letters for c in needle):
            return True
        if len(needle) >= 3 and SequenceMatcher(None, needle, value).ratio() >= .72:
            return True
    return False


class ConnectionIn(BaseModel):
    enabled: bool


class ContactIn(BaseModel):
    id: str = Field(min_length=1, max_length=254)
    name: str = Field(min_length=1, max_length=200)
    trust: int = Field(ge=1, le=255)


@router.get("/api/contacts/connection", response_model=views.Connection, response_model_exclude_unset=True)
def connection_status(context: Access = Depends(account_access)):
    return {"connected": bool(connection(context.account.uuid))}


@router.put("/api/contacts/connection", response_model=views.Connection, response_model_exclude_unset=True)
def set_connection(data: ConnectionIn, context: Access = Depends(account_access)):
    owner = context.account.uuid
    generation = uuid4().hex
    with catalog(write=True) as conn:
        if data.enabled:
            conn.execute("INSERT OR IGNORE INTO contacts_connections VALUES (?,?)", (owner, generation))
        else:
            ids = [r[0] for r in conn.execute("SELECT proof_id FROM contacts_proofs WHERE owner=?", (owner,))]
            conn.execute("DELETE FROM contacts_connections WHERE owner=?", (owner,))
    if data.enabled:
        try:
            contacts_request(context, "GET", "me")
        except HTTPException:
            with catalog(write=True) as conn:
                conn.execute("DELETE FROM contacts_connections WHERE owner=? AND generation=?", (owner, generation))
            raise
    else:
        for proof_id in ids:
            try:
                remote("POST", accounts.accounts_url() + "/v1/proofs/revoke", auth=accounts.app_credentials(), json={"proof_id": proof_id})
            except HTTPException:
                pass  # Locally disconnected. Remaining short-lived proof tokens cannot be retrieved.
    return {"connected": data.enabled}


@router.get("/api/contacts", response_model=views.Contacts, response_model_exclude_unset=True)
def list_contacts(q: str = Query("", max_length=254), context: Access = Depends(account_access)):
    people = [p for p in address_book(context).values() if not p["blocked"] and fuzzy(q, p)]
    return {"connected": bool(connection(context.account.uuid)),
            "items": sorted(people, key=lambda p: (-p["trust"], p["display_name"].casefold(), p["uuid"]))}


@router.put("/api/contacts", response_model=views.Person, response_model_exclude_unset=True)
def save_contact(data: ContactIn, context: Access = Depends(account_access)):
    if not connection(context.account.uuid):
        raise HTTPException(403, "Connect Silicon Contacts first.")
    peer = directory(context, data.id)
    if peer["uuid"] == context.account.uuid or not data.name.strip():
        raise HTTPException(422, "Choose another account and enter a contact name.")
    path = "contacts/" + quote(peer["uuid"], safe="")
    try:
        original = contacts_request(context, "GET", path)
    except HTTPException as error:
        if error.status_code != 404:
            raise
        original = {}
    keep = {k: original[k] for k in ("note", "email", "phone", "photo_id", "birthday_day", "birthday_month", "birthday_year") if k in original}
    result = contacts_request(context, "PUT" if original else "POST", path if original else "contacts",
                              scope="contacts.write", json={**keep, "id": peer["uuid"], "name": data.name.strip(), "trust": data.trust})
    return saved_person(result)


@router.get("/api/contacts/photos/{photo_id}")
def contact_photo(photo_id: UUID, context: Access = Depends(account_access)):
    data = contacts_request(context, "GET", "photos/" + str(photo_id), binary=True)
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(503, "The contact picture is unavailable.")
    return Response(data, media_type="image/png", headers={"X-Content-Type-Options": "nosniff"})


@router.get("/api/people/resolve", response_model=views.Person, response_model_exclude_unset=True)
def resolve(id: str = Query(min_length=1, max_length=254), context: Access = Depends(account_access)):
    peer = directory(context, id)
    if connection(context.account.uuid):
        try:
            peer = saved_person(contacts_request(context, "GET", "contacts/" + quote(peer["uuid"], safe="")))
        except HTTPException as error:
            if error.status_code != 404:
                raise
    if peer.get("blocked"):
        raise HTTPException(403, "This contact is blocked.")
    return peer


@router.get("/api/people", response_model=views.People, response_model_exclude_unset=True)
def people(q: str = Query("", max_length=254), board_id: str | None = None, members_only: bool = False,
           limit: int = Query(25, ge=1, le=200), historical: bool = False, file_senders: bool = False, context: Access = Depends(account_access)):
    owner = context.account.uuid
    saved = address_book(context)
    with catalog() as conn:
        rows = conn.execute("""WITH peers AS (
             SELECT recipient AS peer,1 AS sent,0 AS received,created_at FROM invitations WHERE sender=?
             UNION ALL SELECT sender,0,1,created_at FROM invitations WHERE recipient=?
           ) SELECT p.*,sum(sent) AS sent,sum(received) AS received,max(created_at) AS last_invited_at
             FROM peers JOIN profiles p ON p.uuid=peers.peer GROUP BY peer""", (owner, owner)).fetchall()
    candidates = {r["uuid"]: public_person(dict(r)) for r in rows}
    stats = {r["uuid"]: dict(r) for r in rows}
    board = selected(context, board_id) if board_id else None
    if members_only and not board:
        raise HTTPException(422, "Choose a board to search its members.")
    roster = {}
    if members_only:
        roster = participants(context, board)
        local = {}
        path = board_path(board.id)
        if path.is_file():
            with db(path) as conn:
                for row in conn.execute("SELECT id,account_uuid,account_handle,username,display_name FROM users"):
                    key = row["account_uuid"] or f"legacy:{row['id']}"
                    if historical or key in roster:
                        local[key] = {"uuid": key, "id": row["account_handle"] or row["username"],
                                      "display_name": row["display_name"], "local_id": row["id"]}
        candidates = {k: {**stats_person, **local.get(k, {})} for k, stats_person in roster.items()}
        if historical:
            candidates.update((k, v) for k, v in local.items() if k not in candidates)
    elif board and board.kind != "personal":
        candidates = org_members(context, board)
    else:
        candidates.update((k, v) for k, v in saved.items() if k not in candidates)
        if file_senders:
            board_ids = [b.id for b in context.boards]
            with catalog() as conn:
                rows = conn.execute("SELECT * FROM profiles WHERE uuid IN (SELECT uploader FROM board_files WHERE board IN ("
                                    + ",".join("?" for _ in board_ids) + "))", board_ids)
                for row in rows:
                    candidates.setdefault(row["uuid"], public_person(dict(row)))
    result = []
    for key, value in candidates.items():
        person = {**value, **saved.get(key, {})}
        if person.get("blocked") or (key == owner and not members_only) or not fuzzy(q, person):
            continue
        hist = stats.get(key, {})
        sent, received = hist.get("sent", 0), hist.get("received", 0)
        result.append({**person, "sent": sent, "received": received, "score": 2 * sent + received,
                       "last_invited_at": hist.get("last_invited_at")})
    result.sort(key=lambda p: (p["display_name"].casefold(), p["uuid"]))
    result.sort(key=lambda p: (p["score"], p["last_invited_at"] or "", p.get("trust", 0)), reverse=True)
    matches = result[:limit]
    if members_only and any(not p.get("local_id") for p in matches):
        local_members(board.id, roster, matches)
    return {"items": matches}
