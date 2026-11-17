"""Named boards and invitations. Organizations remains authoritative for group membership."""
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from . import views
from .accounts import Access, account_access
from .catalog import Board, catalog, remember_profile
from .people import address_book, directory, org_members, public_person, selected

router = APIRouter(tags=["Boards and invitations"])
OperationKey = Header(None, alias="Idempotency-Key", min_length=1, max_length=200)


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def manager(context, board_id):
    board = selected(context, board_id)
    if not board.can_manage:
        raise HTTPException(403, "Only the board owner or a group administrator can manage this board.")
    return board


class BoardIn(BaseModel):
    scope_id: str = Field(max_length=254)
    name: str = Field(min_length=1, max_length=100)


class InvitationIn(BaseModel):
    recipient: str = Field(min_length=1, max_length=254)


@router.post("/api/boards", status_code=201, response_model=Board)
def create_board(data: BoardIn, context: Access = Depends(account_access), key: str | None = OperationKey):
    parent = manager(context, data.scope_id)
    name = data.name.strip()
    if parent.id != parent.scope or not name:
        raise HTTPException(422, "Choose a personal, organization or team scope and give the board a name.")
    with catalog(write=True) as conn:
        old = conn.execute("SELECT * FROM boards WHERE creator=? AND operation=?", (context.account.uuid, key)).fetchone()
        if old:
            if old["scope"] != parent.id or old["name"] != name:
                raise HTTPException(409, "This operation key was used for another board.")
            board_id = old["id"]
        else:
            if name.casefold() == parent.name.casefold() or conn.execute(
                "SELECT 1 FROM boards WHERE scope=? AND name=?", (parent.id, name)
            ).fetchone():
                raise HTTPException(409, "This scope already has a board with that name.")
            board_id = "board:" + str(uuid4())
            conn.execute("INSERT INTO boards VALUES (?,?,?,?,?,?)",
                         (board_id, parent.id, name, context.account.uuid, timestamp(), key))
    return Board(board_id, name, parent.kind, parent.organization, True, parent.id)


def invite_record(conn, invite_id):
    return conn.execute("""SELECT i.*, b.name AS board_name,b.scope FROM invitations i
                           JOIN boards b ON b.id=i.board WHERE i.id=?""", (invite_id,)).fetchone()


def invitations_view(conn, rows, saved):
    items = []
    for row in rows:
        item = {k: row[k] for k in ("id", "board", "board_name", "state", "created_at")}
        for column in ("sender", "recipient"):
            uid = row[column]
            profile = conn.execute("SELECT * FROM profiles WHERE uuid=?", (uid,)).fetchone()
            item[column] = saved.get(uid) or public_person(dict(profile))
        items.append(item)
    return items


@router.get("/api/invitations", response_model=views.Invitations, response_model_exclude_unset=True)
def incoming(context: Access = Depends(account_access)):
    saved = address_book(context)
    scopes = {b.scope for b in context.boards}
    with catalog() as conn:
        rows = conn.execute("""SELECT i.*, b.name AS board_name,b.scope FROM invitations i
                               JOIN boards b ON b.id=i.board WHERE recipient=? AND state IN ('pending','accepted')
                               ORDER BY i.created_at DESC""", (context.account.uuid,)).fetchall()
        visible = [r for r in rows if r["scope"].startswith("personal:") or r["scope"] in scopes]
        return {"items": invitations_view(conn, visible, saved)}


@router.get("/api/invitations/count")
def pending_invitations(context: Access = Depends(account_access)) -> dict[str, int]:
    scopes = [b.id for b in context.boards if not b.scope_id]
    with catalog() as conn:
        row = conn.execute("""SELECT COUNT(*) FROM invitations i JOIN boards b ON b.id=i.board
                              WHERE i.recipient=? AND i.state='pending' AND
                              (b.scope LIKE 'personal:%' OR b.scope IN (""" +
                           ",".join("?" for _ in scopes) + "))", (context.account.uuid, *scopes)).fetchone()
    return {"pending": row[0]}


@router.get("/api/boards/{board_id}/invitations", response_model=views.Invitations, response_model_exclude_unset=True)
def outgoing(board_id: str, context: Access = Depends(account_access)):
    manager(context, board_id)
    saved = address_book(context)
    with catalog() as conn:
        rows = conn.execute("""SELECT i.*, b.name AS board_name FROM invitations i JOIN boards b ON b.id=i.board
                               WHERE board=? AND state IN ('pending','accepted') ORDER BY i.created_at DESC""", (board_id,))
        return {"items": invitations_view(conn, rows, saved)}


@router.post("/api/boards/{board_id}/invitations", status_code=201, response_model=views.Invitation, response_model_exclude_unset=True)
def invite(board_id: str, data: InvitationIn, context: Access = Depends(account_access), key: str | None = OperationKey):
    board = manager(context, board_id)
    actor = context.account
    peer = directory(context, data.recipient)
    saved = address_book(context)
    recipient = peer["uuid"]
    if recipient == actor.uuid or saved.get(recipient, {}).get("blocked"):
        raise HTTPException(422, "Choose another eligible account.")
    if board.kind != "personal" and recipient not in org_members(context, board):
        raise HTTPException(403, "Choose a current member of this group.")
    with catalog(write=True) as conn:
        remember_profile(conn, {"uuid": actor.uuid, "id": actor.handle, "display_name": actor.display_name, "pfp_url": actor.pfp_url})
        remember_profile(conn, peer)
        conn.execute("INSERT OR IGNORE INTO boards VALUES (?,?,?,?,?,NULL)",
                     (board.id, board.scope, board.name, actor.uuid, timestamp()))
        previous = conn.execute("""SELECT i.* FROM invitations i JOIN invitation_receipts r ON r.invitation=i.id
                                   WHERE r.sender=? AND r.operation=?""", (actor.uuid, key)).fetchone()
        if previous is None and key:
            previous = conn.execute("SELECT * FROM invitations WHERE sender=? AND operation=?", (actor.uuid, key)).fetchone()
        if previous and (previous["board"] != board.id or previous["recipient"] != recipient):
            raise HTTPException(409, "This operation key was used for another invitation.")
        existing = previous or conn.execute("""SELECT * FROM invitations WHERE board=? AND recipient=?
                                             AND state IN ('pending','accepted')""", (board.id, recipient)).fetchone()
        invitation_id = existing["id"] if existing else str(uuid4())
        if not existing:
            conn.execute("INSERT INTO invitations VALUES (?,?,?,?,'pending',?,?)",
                         (invitation_id, board.id, actor.uuid, recipient, timestamp(), key))
        if key:
            conn.execute("INSERT OR IGNORE INTO invitation_receipts VALUES (?,?,?)", (actor.uuid, key, invitation_id))
        return invitations_view(conn, [invite_record(conn, invitation_id)], saved)[0]


@router.post("/api/invitations/{invitation_id}/{action}", response_model=views.InvitationState, response_model_exclude_unset=True)
def respond(invitation_id: str, action: str, context: Access = Depends(account_access)):
    if action not in ("accept", "decline", "revoke"):
        raise HTTPException(404, "Unknown invitation action.")
    with catalog(write=True) as conn:
        row = invite_record(conn, invitation_id)
        if not row:
            raise HTTPException(404, "Invitation unavailable.")
        if action == "revoke":
            manager(context, row["board"])
        elif context.account.uuid != row["recipient"]:
            raise HTTPException(404, "Invitation unavailable.")
        elif not row["scope"].startswith("personal:") and not any(b.id == row["scope"] for b in context.boards):
            raise HTTPException(403, "Current group membership is required.")
        target = {"accept": "accepted", "decline": "declined", "revoke": "revoked"}[action]
        if row["state"] == target:
            return {"id": invitation_id, "state": target}
        if row["state"] != "pending" and not (action == "revoke" and row["state"] == "accepted"):
            raise HTTPException(409, "This invitation has already been answered.")
        conn.execute("UPDATE invitations SET state=? WHERE id=?", (target, invitation_id))
        return {"id": invitation_id, "state": target}
