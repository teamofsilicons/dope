"""Hosted Silicon Accounts sign-in and live Organizations board discovery."""
from __future__ import annotations

import atexit
import base64
import hashlib
import os
import json
from pathlib import Path
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from pydantic import BaseModel
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from .storage import db, storage_dir
from .catalog import Board, available_boards, catalog, remember_profile

http = httpx.Client(timeout=8, follow_redirects=False)
atexit.register(http.close)
router = APIRouter()
COOKIE = "dope_session"
FLOW_COOKIE = "dope_signin"
CALLBACK = "/api/auth/callback"
SESSION_AGE = 30 * 86400
FLOW_AGE = 600


def app_id() -> str:
    return os.getenv("DOPE_ACCOUNTS_APP_ID", "dope")


def accounts_url() -> str:
    return os.getenv("DOPE_ACCOUNTS_URL", "https://accounts.teamofsilicons.com").rstrip("/")


def origin(request: Request) -> str:
    configured = os.getenv("DOPE_PUBLIC_URL")
    if configured:
        return configured.rstrip("/")
    if request.url.hostname in {"127.0.0.1", "::1", "localhost"}:
        return str(request.base_url).rstrip("/")
    return "https://dope.teamofsilicons.com"


def session_db():
    return db(storage_dir() / "sessions.sqlite3")


def init_sessions() -> None:
    with session_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS signin_flows (
                state_hash TEXT PRIMARY KEY, verifier TEXT NOT NULL,
                redirect_uri TEXT NOT NULL, operation_key TEXT NOT NULL, expires INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_hash TEXT PRIMARY KEY, account_uuid TEXT NOT NULL,
                handle TEXT NOT NULL, display_name TEXT NOT NULL, access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL, access_until INTEGER NOT NULL,
                refresh_operation TEXT NOT NULL, expires INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS board_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_hash TEXT NOT NULL REFERENCES sessions ON DELETE CASCADE,
                board_id TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE, prefix TEXT NOT NULL,
                name TEXT NOT NULL, created_at TEXT NOT NULL, last_used_at TEXT
            );
        """)
        conn.execute("DELETE FROM sessions WHERE expires <= ?", (time.time(),))
        conn.execute("DELETE FROM signin_flows WHERE expires <= ?", (time.time(),))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def upstream(method: str, url: str, **options: Any) -> dict:
    try:
        response = http.request(method, url, **options)
        body = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "Silicon Accounts or Organizations is unavailable. Try again.") from None
    if response.status_code in (401, 403) or (isinstance(body, dict) and body.get("error") == "invalid_grant"):
        raise HTTPException(401, "Sign in again with Silicon Accounts.")
    if not response.is_success or not isinstance(body, dict):
        raise HTTPException(503, "Silicon could not complete this request. Try again.")
    return body


def app_secret():
    value = os.getenv("DOPE_ACCOUNTS_APP_SECRET")
    path = os.getenv("DOPE_REGISTRATION_FILE")
    if not value and path:
        try:
            value = json.loads(Path(path).read_text())["app_secret"]
        except (OSError, ValueError, KeyError):
            raise HTTPException(503, "The Dopes server registration is unavailable.") from None
    return value


def app_credentials():
    secret = app_secret()
    if not secret:
        raise HTTPException(503, "Configure the Dopes server's Accounts app credentials.")
    return app_id(), secret


def token_request(fields: dict, operation: str) -> dict:
    secret = app_secret()
    auth = (app_id(), secret) if secret else None
    if not secret:
        fields = {**fields, "client_id": app_id()}
    return upstream("POST", accounts_url() + "/v1/oauth/token", data=fields, auth=auth,
                    headers={"Idempotency-Key": operation})


@dataclass(frozen=True)
class Account:
    uuid: str
    handle: str
    display_name: str
    pfp_url: str | None = None


def account_from(data: dict) -> Account:
    uuid = data.get("uuid")
    if (not isinstance(uuid, str) or not uuid or data.get("membership_id") != f"{app_id()}:{uuid}"
            or data.get("sub", uuid) != uuid or not isinstance(data.get("id"), str) or not data["id"]):
        raise HTTPException(401, "Use a Silicon Accounts session issued to Dopes.")
    return Account(uuid, data["id"], data.get("display_name") or data["id"], data.get("pfp_url"))


def token_values(tokens: dict) -> tuple[str, str, int]:
    if not all(isinstance(tokens.get(k), str) and tokens[k] for k in ("access_token", "refresh_token")):
        raise HTTPException(503, "Silicon Accounts returned an incomplete session.")
    lifetime = tokens.get("expires_in")
    if not isinstance(lifetime, int) or lifetime < 0:
        raise HTTPException(503, "Silicon Accounts returned an invalid session lifetime.")
    return tokens["access_token"], tokens["refresh_token"], int(time.time()) + lifetime


@router.get("/api/auth/login")
def login(request: Request) -> RedirectResponse:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    redirect_uri = origin(request) + CALLBACK
    with session_db() as conn:
        conn.execute("DELETE FROM signin_flows WHERE expires <= ?", (time.time(),))
        conn.execute("INSERT INTO signin_flows VALUES (?, ?, ?, ?, ?)",
                     (digest(state), verifier, redirect_uri, secrets.token_urlsafe(32), int(time.time()) + FLOW_AGE))
    query = urlencode(dict(app_id=app_id(), redirect_uri=redirect_uri, response_type="code", scope="profile",
                           prompt="select_account", state=state, code_challenge=challenge, code_challenge_method="S256"))
    response = RedirectResponse(f"{accounts_url()}/authorize?{query}", status_code=303)
    response.set_cookie(FLOW_COOKIE, state, max_age=FLOW_AGE, path=CALLBACK, httponly=True,
                        samesite="lax", secure=redirect_uri.startswith("https://"))
    return response


@router.get(CALLBACK)
def callback(request: Request, state: str = "", code: str = "", error: str = "") -> RedirectResponse:
    bound = request.cookies.get(FLOW_COOKIE, "")
    if not bound or not secrets.compare_digest(bound.encode(), state.encode()):
        return RedirectResponse("/?signin=expired", status_code=303)
    with session_db() as conn:
        flow = conn.execute("SELECT * FROM signin_flows WHERE state_hash=? AND expires>?",
                            (digest(state), time.time())).fetchone()
        if not flow:
            return RedirectResponse("/?signin=expired", status_code=303)
        if error or not code:
            conn.execute("DELETE FROM signin_flows WHERE state_hash=?", (digest(state),))
            response = RedirectResponse("/?signin=cancelled", status_code=303)
            response.delete_cookie(FLOW_COOKIE, path=CALLBACK)
            return response
    try:
        tokens = token_request(dict(grant_type="authorization_code", code=code,
                                    redirect_uri=flow["redirect_uri"], code_verifier=flow["verifier"]), flow["operation_key"])
        account = account_from(tokens.get("account") or {})
        values = token_values(tokens)
    except HTTPException:
        return RedirectResponse("/?signin=failed", status_code=303)
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(FLOW_COOKIE, path=CALLBACK)
    cookie = secrets.token_urlsafe(40)
    with session_db() as conn:
        # A concurrent callback can redeem the same code idempotently, but only one installs a session.
        if not conn.execute("DELETE FROM signin_flows WHERE state_hash=? AND expires>?",
                            (digest(state), time.time())).rowcount:
            return RedirectResponse("/?signin=expired", status_code=303)
        conn.execute("DELETE FROM sessions WHERE session_hash=? OR expires<=?",
                     (digest(request.cookies.get(COOKIE, "")), time.time()))
        conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (digest(cookie), account.uuid, account.handle, account.display_name,
                      *values, secrets.token_urlsafe(32), int(time.time()) + SESSION_AGE))
    with catalog(write=True) as conn:
        remember_profile(conn, {"uuid": account.uuid, "id": account.handle, "display_name": account.display_name, "pfp_url": account.pfp_url})
    response.set_cookie(COOKIE, cookie, max_age=SESSION_AGE, httponly=True, samesite="lax",
                        secure=flow["redirect_uri"].startswith("https://"))
    return response


@router.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict:
    with session_db() as conn:
        session_hash = digest(request.cookies.get(COOKIE, ""))
        session = conn.execute("SELECT refresh_token FROM sessions WHERE session_hash=?", (session_hash,)).fetchone()
        conn.execute("DELETE FROM sessions WHERE session_hash=?", (session_hash,))
    response.delete_cookie(COOKIE)
    if session:
        secret = app_secret()
        fields = {"token": session["refresh_token"]}
        if not secret:
            fields["client_id"] = app_id()
        try:
            upstream("POST", accounts_url() + "/v1/oauth/revoke", data=fields, auth=(app_id(), secret) if secret else None)
        except HTTPException:
            # The server-side session and all its keys have already been invalidated locally.
            pass
    return {"ok": True}


def session_row(conn: sqlite3.Connection, key: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM sessions WHERE session_hash=? AND expires>?", (key, time.time())).fetchone()
    if not row:
        raise HTTPException(401, "Sign in with Silicon Accounts.")
    return row


def current_session(key: str) -> sqlite3.Row:
    with session_db() as conn:
        row = session_row(conn, key)
        if row["access_until"] > time.time() + 30:
            return row
    # Each refresh token has its own random recovery key, committed alongside it.
    # If Accounts rotated the token before a timeout or restart, replay that exact operation.
    for _ in range(2):
        with session_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = session_row(conn, key)
            if row["access_until"] > time.time() + 30:
                return row
            tokens = token_request({"grant_type": "refresh_token", "refresh_token": row["refresh_token"]},
                                   row["refresh_operation"])
            account = account_from(tokens.get("account") or {})
            if account.uuid != row["account_uuid"]:
                raise HTTPException(401, "The signed-in account changed. Sign in again.")
            conn.execute("UPDATE sessions SET handle=?, display_name=?, access_token=?, refresh_token=?, "
                         "access_until=?, refresh_operation=? WHERE session_hash=?",
                         (account.handle, account.display_name, *token_values(tokens), secrets.token_urlsafe(32), key))
            updated = session_row(conn, key)
            if updated["access_until"] > time.time():
                return updated
        # A recovered exchange can return an expired access token and a usable successor.
    raise HTTPException(503, "Silicon Accounts is renewing this session. Try again.")


@dataclass(frozen=True)
class Access:
    account: Account
    boards: list[Board]
    browser_session: str | None = None
    token: str = field(default="", repr=False)
    board_key: bool = False


def boards_for(account: Account, token: str) -> list[Board]:
    orgs_url = os.getenv("DOPE_ORGANIZATIONS_URL", "https://organizations.teamofsilicons.com").rstrip("/")
    context = upstream("GET", f"{orgs_url}/v1/integrations/{quote(app_id(), safe='')}/me",
                       headers={"Authorization": f"Bearer {token}"})
    if context.get("account_uuid") != account.uuid:
        raise HTTPException(401, "Organizations could not verify the selected account.")
    if not all(isinstance(context.get(key), list) for key in ("groups", "organizations", "share_targets")):
        raise HTTPException(503, "Organizations could not load your boards.")
    # Admins can see extra share targets. Only groups establish membership.
    groups = set(context["groups"])
    orgs = {org["org_id"]: org for org in context["organizations"]}
    boards = []
    for target in context["share_targets"]:
        kind, target_id, org_id = target["type"], target["id"], target["org_id"]
        board_id = f"{kind}:{target_id}"
        if kind not in ("organization", "team") or board_id not in groups or org_id not in orgs:
            continue
        caps = orgs[org_id].get("capabilities", {})
        manages = caps.get("admin") is True or (
            kind == "team" and target_id.partition("/")[2] in caps.get("team_admins", [])
        )
        boards.append(Board(board_id, target["name"], kind, orgs[org_id]["name"], manages))
    boards.sort(key=lambda board: (board.organization.casefold(), board.kind != "organization", board.name.casefold(), board.id))
    boards.append(Board(f"personal:{account.uuid}", "Personal board", "personal", None, True))
    return boards


def access(request: Request, authorization: str | None = Header(default=None)) -> Access:
    browser_session = None
    key_board = None
    if authorization is not None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(401, "Provide a Dope access token.")
        if token.startswith("dope_"):
            with session_db() as conn:
                key = conn.execute("SELECT * FROM board_keys WHERE key_hash=?", (digest(token),)).fetchone()
                if not key:
                    raise HTTPException(401, "This Dope API key is no longer active.")
                conn.execute("UPDATE board_keys SET last_used_at=CURRENT_TIMESTAMP WHERE id=?", (key["id"],))
            session = current_session(key["session_hash"])
            key_board = key["board_id"]
        else:
            live = upstream("POST", accounts_url() + "/v1/oauth/introspect", auth=app_credentials(), data={"token": token})
            if (live.get("active") is not True or live.get("token_type") != "access_token"
                    or live.get("client_id") != app_id() or live.get("aud") != app_id()):
                raise HTTPException(401, "Use a live access token issued to Dopes.")
            info = upstream("GET", accounts_url() + "/v1/userinfo", headers={"Authorization": authorization})
            account = account_from(info)
            if account.uuid != live.get("sub"):
                raise HTTPException(401, "Accounts could not verify this identity.")
            return Access(account, available_boards(account.uuid, boards_for(account, token)), token=token)
    else:
        cookie = request.cookies.get(COOKIE)
        if not cookie:
            raise HTTPException(401, "Sign in with Silicon Accounts.")
        browser_session = digest(cookie)
        session = current_session(browser_session)
    account = Account(session["account_uuid"], session["handle"], session["display_name"])
    boards = available_boards(account.uuid, boards_for(account, session["access_token"]))
    if key_board:
        boards = [board for board in boards if board.id == key_board]
        if not boards:
            raise HTTPException(403, "You no longer have access to this board.")
    return Access(account, boards, browser_session, session["access_token"], key_board is not None)


def account_access(context: Access = Depends(access), expected: str | None = Header(None, alias="X-Dope-Account")) -> Access:
    if context.board_key:
        raise HTTPException(403, "This operation needs account access.")
    if expected is not None and expected != context.account.uuid:
        raise HTTPException(409, "The signed-in account changed.")
    return context


class BoardDirectory(BaseModel):
    account: Account
    boards: list[Board]


@router.get("/api/boards", response_model=BoardDirectory)
def list_boards(context: Access = Depends(access)) -> dict:
    return {"account": context.account, "boards": context.boards}
