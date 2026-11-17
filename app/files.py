"""Files belong to boards and follow the same live access rules as all other board data."""
import hashlib
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from . import views
from .accounts import Access, access
from .catalog import catalog, remember_profile
from .people import address_book, public_person, selected
from .sharing import OperationKey, timestamp

router = APIRouter(tags=["Board files"])
MAX_FILE = 10 * 1024 * 1024
FIELDS = "f.id,f.board,f.name,f.size,f.created_at,f.uploader"


def file_info(row, profiles, boards):
    result = dict(row)
    result["uploader"] = profiles[row["uploader"]]
    result["board_name"] = boards[row["board"]].name
    result["download_url"] = "/api/files/" + row["id"]
    return result


@router.get("/api/files", response_model=views.Files, response_model_exclude_unset=True)
def files(board_id: str | None = None, shared_with_me: bool = False, contacts: str = Query("all", pattern="^(all|saved|unsaved)$"),
          min_trust: int = Query(0, ge=0, le=255), sender: UUID | None = None, q: str = Query("", max_length=254),
          limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), context: Access = Depends(access)):
    if context.board_key and (contacts != "all" or min_trust):
        raise HTTPException(403, "Contacts filters require account access.")
    boards = {b.id: b for b in ([selected(context, board_id)] if board_id else context.boards)}
    saved = address_book(context) if not context.board_key else {}
    params = list(boards)
    conditions = ["f.board IN (" + ",".join("?" for _ in boards) + ")"]
    if shared_with_me:
        conditions.append("f.uploader!=?")
        params.append(context.account.uuid)
    if sender:
        conditions.append("f.uploader=?")
        params.append(str(sender))
    if q:
        conditions.append("instr(lower(f.name),lower(?))>0")
        params.append(q)
    if contacts != "all" or min_trust:
        eligible = [uid for uid, p in saved.items() if not p["blocked"] and p["trust"] >= min_trust]
        if contacts == "unsaved" and min_trust:
            return {"items": [], "total": 0}
        if contacts == "unsaved" or eligible:
            conditions.append("f.uploader " + ("NOT IN" if contacts == "unsaved" else "IN") +
                              "(" + ",".join("?" for _ in eligible) + ")")
            params.extend(eligible)
        else:
            return {"items": [], "total": 0}
    clause = " FROM board_files f WHERE " + " AND ".join(conditions)
    with catalog() as conn:
        total = conn.execute("SELECT COUNT(*)" + clause, params).fetchone()[0]
        rows = conn.execute("SELECT " + FIELDS + clause + " ORDER BY f.created_at DESC,f.id LIMIT ? OFFSET ?",
                            [*params, limit, offset]).fetchall()
        profiles = {}
        for uid in {r["uploader"] for r in rows}:
            profiles[uid] = saved.get(uid) or public_person(dict(conn.execute("SELECT * FROM profiles WHERE uuid=?", (uid,)).fetchone()))
    return {"items": [file_info(r, profiles, boards) for r in rows], "total": total}


def store_file(context, board, name, data, key):
    actor = context.account
    checksum = hashlib.sha256(data).hexdigest()
    with catalog(write=True) as conn:
        profile = {"uuid": actor.uuid, "id": actor.handle, "display_name": actor.display_name, "pfp_url": actor.pfp_url}
        remember_profile(conn, profile)
        old = conn.execute("SELECT id,board,name,digest FROM board_files WHERE uploader=? AND operation=?", (actor.uuid, key)).fetchone()
        if old and (old["board"], old["name"], old["digest"]) != (board.id, name, checksum):
            raise HTTPException(409, "This operation key was used for a different upload.")
        file_id = old["id"] if old else str(uuid4())
        if not old:
            conn.execute("INSERT INTO board_files VALUES (?,?,?,?,?,?,?,?,?)",
                         (file_id, board.id, actor.uuid, name, timestamp(), len(data), checksum, data, key))
        row = conn.execute("SELECT " + FIELDS + " FROM board_files f WHERE f.id=?", (file_id,)).fetchone()
        return file_info(row, {actor.uuid: public_person(profile)}, {board.id: board})


@router.post("/api/files", status_code=201, response_model=views.File, response_model_exclude_unset=True, openapi_extra={"requestBody": {"required": True, "content": {
    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}})
async def upload(request: Request, name: str = Query(min_length=1, max_length=240),
                 board_id: str = Header(alias="X-Dope-Board"), expected: str | None = Header(None, alias="X-Dope-Account"),
                 key: str | None = OperationKey, context: Access = Depends(access)):
    if expected is not None and expected != context.account.uuid:
        raise HTTPException(409, "The signed-in account changed.")
    board = selected(context, board_id)
    if not name.strip() or any(ord(c) < 32 or c in '/\\' for c in name):
        raise HTTPException(422, "Use a file name without path components.")
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_FILE:
        raise HTTPException(413, "Files can be up to 10 MiB.")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_FILE:
            raise HTTPException(413, "Files can be up to 10 MiB.")
        data.extend(chunk)
    return await run_in_threadpool(store_file, context, board, name.strip(), bytes(data), key)


@router.get("/api/files/{file_id}", response_class=Response, responses={200: {"content": {"application/octet-stream": {}}}})
def download(file_id: UUID, context: Access = Depends(access)):
    with catalog() as conn:
        meta = conn.execute("SELECT board,name FROM board_files WHERE id=?", (str(file_id),)).fetchone()
        if not meta or not any(b.id == meta["board"] for b in context.boards):
            raise HTTPException(404, "File unavailable.")
        data = conn.execute("SELECT content FROM board_files WHERE id=?", (str(file_id),)).fetchone()[0]
    return Response(data, media_type="application/octet-stream", headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(meta["name"], safe=""), "X-Content-Type-Options": "nosniff"})
