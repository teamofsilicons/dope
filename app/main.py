from __future__ import annotations

import html
import hashlib
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from html.parser import HTMLParser
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any

import markdown
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, HTMLResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import accounts, files, people, sharing, views
from .catalog import init_catalog
from .storage import board_path, db


ROOT = Path(__file__).resolve().parent
IST_OFFSET = timedelta(hours=5, minutes=30)
DOPE_DAY_RESET = datetime_time(hour=9)
DEFAULT_DOPE_DAY_RESET_MINUTES = 9 * 60
RESET_RETROACTIVE_MINUTES = 10
COMBINED_DOPE_DAYS = {date(2026, 7, 20): date(2026, 7, 21)}

@asynccontextmanager
async def lifespan(app: FastAPI):
    accounts.init_sessions()
    init_catalog()
    yield


app = FastAPI(title="Silicon Dopes", version="1.0", lifespan=lifespan,
              description="Boards, dopes, invitations, contacts and files. See the [interface guide](/interface-api).")
app.include_router(accounts.router)
app.include_router(sharing.router)
app.include_router(people.router)
app.include_router(files.router)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def private_responses(request: Request, call_next):
    origin = request.headers.get("origin")
    foreign = origin and origin != accounts.origin(request)
    bearer = request.headers.get("authorization", "").lower().startswith("bearer ")
    external_api = request.url.path.startswith("/api/") and not request.url.path.startswith("/api/auth/")
    cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE",
            "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Dope-Board, X-Dope-Account, Idempotency-Key",
            "Access-Control-Expose-Headers": "Content-Disposition, Retry-After", "Access-Control-Max-Age": "600"}
    if foreign and external_api and request.method == "OPTIONS":
        return Response(status_code=204, headers=cors)
    if foreign and request.method not in {"HEAD", "GET", "OPTIONS"} and not (bearer and external_api):
        return JSONResponse({"detail": "Cross-origin writes require an app access token."}, status_code=403)
    response = await call_next(request)
    if foreign and bearer and external_api:
        response.headers.update(cors)
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utc_now().isoformat(timespec="seconds")


def now_iso_precise() -> str:
    # Comments and read markers compare timestamps with >, so they need
    # sub-second precision to keep rapid back-and-forth unread-accurate.
    return utc_now().isoformat(timespec="microseconds")


def parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def dope_day_for(value: str | datetime, reset_time: datetime_time = DOPE_DAY_RESET) -> date:
    parsed = parse_iso_datetime(value) if isinstance(value, str) else value.astimezone(timezone.utc)
    local = parsed + IST_OFFSET
    day = local.date()
    if local.time() < reset_time:
        day -= timedelta(days=1)
    return COMBINED_DOPE_DAYS.get(day, day)


def current_dope_day(reset_time: datetime_time = DOPE_DAY_RESET) -> date:
    return dope_day_for(utc_now(), reset_time)


@lru_cache(maxsize=512)
def init_db(path: Path) -> None:
    with db(path) as conn:
        conn.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT NOT NULL UNIQUE COLLATE NOCASE,
              password_hash TEXT NOT NULL,
              display_name TEXT NOT NULL,
              color TEXT NOT NULL DEFAULT '#1a1a1a',
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS create_receipts (
              user_id INTEGER NOT NULL REFERENCES users(id), operation TEXT NOT NULL, kind TEXT NOT NULL,
              fingerprint TEXT NOT NULL, item_id INTEGER NOT NULL, PRIMARY KEY (user_id, operation)
            );
            CREATE TABLE IF NOT EXISTS dopes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              description_html TEXT NOT NULL,
              time_minutes INTEGER NOT NULL,
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              assigned_to INTEGER REFERENCES users(id),
              assigned_at TEXT,
              completed_by INTEGER REFERENCES users(id),
              completed_at TEXT,
              completion_description TEXT,
              review_requested_by INTEGER REFERENCES users(id),
              review_requested_at TEXT,
              review_note TEXT,
              review_branch_url TEXT,
              review_approved_by INTEGER REFERENCES users(id),
              review_approved_at TEXT,
              review_rejected_by INTEGER REFERENCES users(id),
              review_rejected_at TEXT,
              review_rejection_note TEXT,
              review_followup_id INTEGER REFERENCES dopes(id),
              review_parent_id INTEGER REFERENCES dopes(id),
              review_priority INTEGER NOT NULL DEFAULT 0,
              saved_by INTEGER REFERENCES users(id),
              saved_at TEXT,
              archived_by INTEGER REFERENCES users(id),
              archived_at TEXT
            );

            CREATE TABLE IF NOT EXISTS assignment_history (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              dope_id INTEGER NOT NULL REFERENCES dopes(id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL REFERENCES users(id),
              display_name TEXT NOT NULL,
              assigned_at TEXT NOT NULL,
              unassigned_at TEXT,
              unassign_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS commit_links (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              dope_id INTEGER NOT NULL REFERENCES dopes(id) ON DELETE CASCADE,
              url TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS dope_versions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              dope_id INTEGER NOT NULL REFERENCES dopes(id) ON DELETE CASCADE,
              version_number INTEGER NOT NULL,
              title TEXT NOT NULL,
              description_html TEXT NOT NULL,
              edited_by INTEGER NOT NULL REFERENCES users(id),
              edited_at TEXT NOT NULL,
              UNIQUE(dope_id, version_number)
            );

            CREATE TABLE IF NOT EXISTS dope_dependencies (
              dope_id INTEGER NOT NULL REFERENCES dopes(id) ON DELETE CASCADE,
              depends_on_id INTEGER NOT NULL REFERENCES dopes(id) ON DELETE CASCADE,
              created_at TEXT NOT NULL,
              PRIMARY KEY (dope_id, depends_on_id),
              CHECK (dope_id != depends_on_id)
            );

            CREATE TABLE IF NOT EXISTS api_keys (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              key_hash TEXT NOT NULL UNIQUE,
              prefix TEXT NOT NULL,
              created_at TEXT NOT NULL,
              last_used_at TEXT,
              revoked_at TEXT
            );

            CREATE TABLE IF NOT EXISTS categories (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE COLLATE NOCASE,
              color TEXT NOT NULL,
              position INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS comments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              dope_id INTEGER NOT NULL REFERENCES dopes(id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL REFERENCES users(id),
              body TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS comment_mentions (
              comment_id INTEGER NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL REFERENCES users(id),
              PRIMARY KEY (comment_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS comment_reads (
              dope_id INTEGER NOT NULL REFERENCES dopes(id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL REFERENCES users(id),
              last_read_at TEXT NOT NULL,
              PRIMARY KEY (dope_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS dope_day_reset_changes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              reset_minutes INTEGER NOT NULL CHECK (reset_minutes >= 0 AND reset_minutes < 1440),
              changed_by INTEGER NOT NULL REFERENCES users(id),
              changed_at TEXT NOT NULL,
              retroactive_from TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO dope_versions (dope_id, version_number, title, description_html, edited_by, edited_at)
            SELECT d.id, 1, d.title, d.description_html, d.created_by, d.created_at
            FROM dopes d
            WHERE NOT EXISTS (
              SELECT 1 FROM dope_versions v WHERE v.dope_id = d.id
            )
            """
        )
        assignment_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(assignment_history)").fetchall()
        }
        if "unassign_reason" not in assignment_columns:
            conn.execute("ALTER TABLE assignment_history ADD COLUMN unassign_reason TEXT")
        user_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        if "color" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN color TEXT NOT NULL DEFAULT '#1a1a1a'")
        if "last_seen_at" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN last_seen_at TEXT")
        for name in ("account_uuid", "account_handle", "accounts_name"):
            if name not in user_columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_account_uuid ON users(account_uuid)")
        dope_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(dopes)").fetchall()
        }
        if "category_id" not in dope_columns:
            conn.execute("ALTER TABLE dopes ADD COLUMN category_id INTEGER REFERENCES categories(id)")
        review_columns = {
            "review_requested_by": "INTEGER REFERENCES users(id)",
            "review_requested_at": "TEXT",
            "review_note": "TEXT",
            "review_branch_url": "TEXT",
            "review_approved_by": "INTEGER REFERENCES users(id)",
            "review_approved_at": "TEXT",
            "review_rejected_by": "INTEGER REFERENCES users(id)",
            "review_rejected_at": "TEXT",
            "review_rejection_note": "TEXT",
            "review_followup_id": "INTEGER REFERENCES dopes(id)",
            "review_parent_id": "INTEGER REFERENCES dopes(id)",
            "review_priority": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in review_columns.items():
            if name not in dope_columns:
                conn.execute(f"ALTER TABLE dopes ADD COLUMN {name} {definition}")
        saved_columns = {
            "saved_by": "INTEGER REFERENCES users(id)",
            "saved_at": "TEXT",
        }
        for name, definition in saved_columns.items():
            if name not in dope_columns:
                conn.execute(f"ALTER TABLE dopes ADD COLUMN {name} {definition}")
        if not conn.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
            seed_categories = [
                ("Silicon Centered", "#2e6f8e"),
                ("Silicon Supporting", "#5a9a6b"),
                ("Client Side", "#c56f2d"),
                ("Team", "#7a4d8e"),
            ]
            seeded_at = now_iso()
            conn.executemany(
                "INSERT INTO categories (name, color, position, created_at) VALUES (?, ?, ?, ?)",
                [(name, color, index, seeded_at) for index, (name, color) in enumerate(seed_categories)],
            )


@dataclass(frozen=True)
class BoardMember:
    path: Path
    board: accounts.Board
    user: sqlite3.Row
    browser_session: str | None


def board_member(
    board_id: str | None = Header(default=None, alias="X-Dope-Board"),
    expected_account: str | None = Header(default=None, alias="X-Dope-Account"),
    context: accounts.Access = Depends(accounts.access),
) -> BoardMember:
    if expected_account is not None and expected_account != context.account.uuid:
        raise HTTPException(409, "The signed-in account changed. Reload your boards.")
    selected = next((b for b in context.boards if b.id == board_id), None) if board_id is not None else context.boards[0]
    if selected is None:
        raise HTTPException(403, "You do not have access to this board.")
    path = board_path(selected.id)
    init_db(path)
    account = context.account
    with db(path) as conn:
        user = conn.execute("SELECT * FROM users WHERE account_uuid=?", (account.uuid,)).fetchone()
        if (not user or user["account_handle"] != account.handle or user["accounts_name"] != account.display_name
                or not is_online(user["last_seen_at"], timedelta(minutes=1))):
            # The legacy username is unique and may belong to an unverified password account.
            # Only the immutable Accounts UUID can ever link a sign-in to a board member.
            now = now_iso()
            conn.execute("""
                INSERT INTO users (username, password_hash, display_name, created_at, last_seen_at,
                                   account_uuid, account_handle, accounts_name)
                VALUES (?, '', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_uuid) DO UPDATE SET
                    display_name=CASE WHEN users.display_name=users.accounts_name THEN excluded.display_name
                                      ELSE users.display_name END,
                    accounts_name=excluded.accounts_name, account_handle=excluded.account_handle,
                    last_seen_at=excluded.last_seen_at
                """, ("accounts/" + secrets.token_hex(16), account.display_name, now, now,
                      account.uuid, account.handle, account.display_name))
            user = conn.execute("SELECT * FROM users WHERE account_uuid=?", (account.uuid,)).fetchone()
    return BoardMember(path, selected, user, context.browser_session)


def require_manager(member: BoardMember) -> None:
    if not member.board.can_manage:
        raise HTTPException(403, "This action requires a board administrator.")


class DopeIn(BaseModel):
    description: str | None = Field(default=None, max_length=20_000)
    title: str | None = Field(default=None, max_length=180)
    description_html: str | None = Field(default=None, max_length=250_000)
    time_text: str | None = Field(default=None, max_length=40)
    dependency_ids: list[int] = Field(default_factory=list, max_length=50)
    category_id: int | None = None

    def content(self) -> tuple[str, str]:
        if self.description is not None:
            text = self.description.strip()
            if not text:
                raise HTTPException(400, "Write a description for the dope.")
            return (self.title or "").strip() or text.splitlines()[0][:180], html.escape(text).replace("\n", "<br>")
        if not (self.description_html or "").strip():
            raise HTTPException(400, "Write a description for the dope.")
        title = (self.title or "").strip() or plain_description(self.description_html)[:180]
        if not title:
            raise HTTPException(400, "Add a title or a text description.")
        return title, self.description_html


class UnassignIn(BaseModel):
    reason: str = Field(default="", max_length=500)


class CompleteIn(BaseModel):
    description: str = Field(default="", max_length=20_000)
    completion_text: str | None = Field(default=None, max_length=30_000)
    commit_links: list[str] = Field(default_factory=list, max_length=50)
    completion_description: str = Field(default="", max_length=20_000)
    completed_for_user_id: int | None = Field(default=None, gt=0)


class DescriptionText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag, attrs) -> None:
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag) -> None:
        if tag in ("p", "div", "li"):
            self.parts.append("\n")


def plain_description(value: str) -> str:
    parser = DescriptionText()
    try:
        parser.feed(value)
        parser.close()
    except (AssertionError, ValueError):
        # Old rich descriptions can contain malformed markup; they remain readable as text.
        return value.strip()
    return "".join(parser.parts).strip()


class ReviewRequestIn(BaseModel):
    note: str = Field(min_length=1, max_length=30_000)
    branch_url: str = Field(min_length=1, max_length=2000)


class ReviewRejectIn(BaseModel):
    note: str = Field(min_length=1, max_length=30_000)
    time_text: str = Field(min_length=1, max_length=40)


class ProfileIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")


class ApiKeyIn(BaseModel):
    name: str = Field(default="API key", min_length=1, max_length=80)


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")


class DopeCategoryIn(BaseModel):
    category_id: int | None = Field(default=None)


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=10_000)


class DopeDaySettingsIn(BaseModel):
    reset_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def parse_time_to_minutes(value: str) -> int:
    text = value.strip().lower()
    token_re = re.compile(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)?")
    matches = list(token_re.finditer(text))
    consumed = "".join(match.group(0) for match in matches)
    if not matches or re.sub(r"\s+", "", consumed) != re.sub(r"\s+", "", text):
        raise HTTPException(status_code=400, detail="Use time like 30min, 2hr, 0.5hr, or 2hr 30min")
    minutes = 0.0
    for match in matches:
        amount = float(match.group(1))
        unit = match.group(2) or "hr"
        minutes += amount * 60 if unit.startswith("h") else amount
    if minutes <= 0 or minutes > 60 * 24 * 365:
        raise HTTPException(status_code=400, detail="Time must be greater than zero")
    return max(1, round(minutes))


def extract_http_links(value: str) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"https?://[^\s<>)\"']+", value):
        url = match.group(0).rstrip(".,;:]}")
        if url not in seen:
            seen.add(url)
            links.append(url)
    return links


def validate_http_url(value: str, label: str) -> str:
    url = value.strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(status_code=400, detail=f"{label} must start with http:// or https://")
    return url


def status_for(row: sqlite3.Row) -> str:
    if row["archived_at"]:
        return "archived"
    if row["saved_at"]:
        return "saved"
    if row["completed_at"] and row["review_requested_at"] and not row["review_approved_at"] and not row["review_rejected_at"]:
        return "review"
    if row["completed_at"]:
        return "completed"
    return "active"


def user_public(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {"id": row["id"], "username": row["account_handle"] or row["username"], "display_name": row["display_name"], "color": row["color"]}


def reset_time_from_minutes(minutes: int) -> datetime_time:
    return datetime_time(hour=minutes // 60, minute=minutes % 60)


def reset_time_text(minutes: int) -> str:
    value = reset_time_from_minutes(minutes)
    return f"{value.hour:02d}:{value.minute:02d}"


def parse_reset_time(value: str) -> int:
    text = value.strip()
    match = re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text)
    if not match:
        raise HTTPException(status_code=400, detail="Reset time must use HH:MM")
    hour, minute = (int(part) for part in text.split(":"))
    return hour * 60 + minute


def load_reset_changes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, reset_minutes, changed_at, retroactive_from FROM dope_day_reset_changes ORDER BY changed_at, id"
    ).fetchall()
    return [
        {
            "id": row["id"],
            "reset_minutes": int(row["reset_minutes"]),
            "changed_at": row["changed_at"],
            "retroactive_from": parse_iso_datetime(row["retroactive_from"]),
        }
        for row in rows
    ]


def reset_minutes_for_completion(value: str | datetime, changes: list[dict[str, Any]]) -> int:
    completed_at = parse_iso_datetime(value) if isinstance(value, str) else value.astimezone(timezone.utc)
    reset_minutes = DEFAULT_DOPE_DAY_RESET_MINUTES
    for change in changes:
        if completed_at >= change["retroactive_from"]:
            reset_minutes = int(change["reset_minutes"])
    return reset_minutes


def next_reset_at(reset_minutes: int, now: datetime | None = None) -> datetime:
    current = (now or utc_now()).astimezone(timezone.utc)
    local_now = current + IST_OFFSET
    reset_time = reset_time_from_minutes(reset_minutes)
    next_local = datetime.combine(local_now.date(), reset_time, tzinfo=timezone.utc)
    if next_local <= local_now:
        next_local += timedelta(days=1)
    return next_local - IST_OFFSET


def dope_day_settings_payload(row: sqlite3.Row | None, now: datetime | None = None) -> dict[str, Any]:
    current = (now or utc_now()).astimezone(timezone.utc)
    reset_minutes = int(row["reset_minutes"]) if row else DEFAULT_DOPE_DAY_RESET_MINUTES
    next_reset = next_reset_at(reset_minutes, current)
    return {
        "reset_time": reset_time_text(reset_minutes),
        "timezone": "IST",
        "next_reset_at": next_reset.isoformat(timespec="seconds"),
        "remaining_seconds": max(0, int((next_reset - current).total_seconds())),
        "history_window_minutes": RESET_RETROACTIVE_MINUTES,
        "changed_at": row["changed_at"] if row else None,
        "retroactive_from": row["retroactive_from"] if row else None,
    }


def normalize_color(value: str | None, fallback: str = "#1a1a1a") -> str:
    if value and re.match(r"^#[0-9a-fA-F]{6}$", value):
        return value.lower()
    return fallback


def category_public(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {"id": row["id"], "name": row["name"], "color": row["color"]}


def validate_category_id(conn: sqlite3.Connection, category_id: int | None) -> int | None:
    if category_id is None:
        return None
    row = conn.execute("SELECT id FROM categories WHERE id = ?", (category_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="Category not found")
    return category_id


def add_dope_version(
    conn: sqlite3.Connection,
    dope_id: int,
    title: str,
    description_html: str,
    edited_by: int,
    edited_at: str | None = None,
) -> None:
    current = conn.execute(
        "SELECT COALESCE(MAX(version_number), 0) FROM dope_versions WHERE dope_id = ?",
        (dope_id,),
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO dope_versions (dope_id, version_number, title, description_html, edited_by, edited_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (dope_id, int(current) + 1, title, description_html, edited_by, edited_at or now_iso()),
    )


def clean_dependency_ids(raw_ids: list[int], dope_id: int) -> list[int]:
    seen: set[int] = set()
    ids: list[int] = []
    for raw_id in raw_ids:
        dep_id = int(raw_id)
        if dep_id == dope_id or dep_id in seen:
            continue
        seen.add(dep_id)
        ids.append(dep_id)
    return ids


def assert_dependencies_allowed(conn: sqlite3.Connection, dope_id: int, dependency_ids: list[int]) -> list[int]:
    ids = clean_dependency_ids(dependency_ids, dope_id)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id FROM dopes WHERE archived_at IS NULL AND id IN ({placeholders})",
        ids,
    ).fetchall()
    found = {row["id"] for row in rows}
    missing = [dep_id for dep_id in ids if dep_id not in found]
    if missing:
        raise HTTPException(status_code=400, detail="Dependency dope not found")

    graph: dict[int, list[int]] = {}
    edges = conn.execute(
        "SELECT dope_id, depends_on_id FROM dope_dependencies WHERE dope_id != ?",
        (dope_id,),
    ).fetchall()
    for edge in edges:
        graph.setdefault(edge["dope_id"], []).append(edge["depends_on_id"])
    graph[dope_id] = ids

    def reaches_source(start: int) -> bool:
        stack = [start]
        visited: set[int] = set()
        while stack:
            current = stack.pop()
            if current == dope_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            stack.extend(graph.get(current, []))
        return False

    if any(reaches_source(dep_id) for dep_id in ids):
        raise HTTPException(status_code=400, detail="Circular dependencies are not allowed")
    return ids


def set_dope_dependencies(conn: sqlite3.Connection, dope_id: int, dependency_ids: list[int]) -> None:
    ids = assert_dependencies_allowed(conn, dope_id, dependency_ids)
    conn.execute("DELETE FROM dope_dependencies WHERE dope_id = ?", (dope_id,))
    conn.executemany(
        "INSERT INTO dope_dependencies (dope_id, depends_on_id, created_at) VALUES (?, ?, ?)",
        [(dope_id, dep_id, now_iso()) for dep_id in ids],
    )


def active_dependent_count(conn: sqlite3.Connection, dope_id: int) -> int:
    return len(active_dependent_rows(conn, dope_id))


def active_dependent_rows(conn: sqlite3.Connection, dope_id: int) -> list[dict[str, Any]]:
    edges = conn.execute(
        """
        SELECT dd.dope_id, dd.depends_on_id
        FROM dope_dependencies dd
        JOIN dopes child ON child.id = dd.dope_id
        WHERE child.archived_at IS NULL
          AND child.completed_at IS NULL
          AND child.saved_at IS NULL
        """
    ).fetchall()
    reverse_graph: dict[int, list[int]] = {}
    for edge in edges:
        reverse_graph.setdefault(edge["depends_on_id"], []).append(edge["dope_id"])

    dependents: set[int] = set()
    depths: dict[int, int] = {}
    stack = [(child_id, 1) for child_id in reverse_graph.get(dope_id, [])]
    while stack:
        current, depth = stack.pop()
        if current in dependents:
            continue
        dependents.add(current)
        depths[current] = depth
        stack.extend((child_id, depth + 1) for child_id in reverse_graph.get(current, []))
    if not dependents:
        return []

    placeholders = ",".join("?" for _ in dependents)
    rows = conn.execute(
        f"""
        SELECT id, title, time_minutes, assigned_to, completed_at, archived_at
        FROM dopes
        WHERE id IN ({placeholders})
        """,
        tuple(dependents),
    ).fetchall()
    return sorted(
        [
            {
                "id": row["id"],
                "title": row["title"],
                "time_minutes": row["time_minutes"],
                "status": "archived" if row["archived_at"] else "completed" if row["completed_at"] else "active",
                "depth": depths.get(row["id"], 1),
            }
            for row in rows
        ],
        key=lambda item: (item["depth"], item["title"].lower(), item["id"]),
    )


def incomplete_dependency_rows(conn: sqlite3.Connection, dope_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT d.* FROM dope_dependencies dd
        JOIN dopes d ON d.id = dd.depends_on_id
        WHERE dd.dope_id = ? AND d.completed_at IS NULL
        ORDER BY d.id DESC
        """,
        (dope_id,),
    ).fetchall()


ONLINE_WINDOW = timedelta(minutes=5)


def is_online(last_seen_at: str | None, window: timedelta = ONLINE_WINDOW) -> bool:
    if not last_seen_at:
        return False
    try:
        return datetime.now(timezone.utc) - parse_iso_datetime(last_seen_at) <= window
    except ValueError:
        return False


def extract_mention_user_ids(conn: sqlite3.Connection, body: str) -> list[int]:
    text = body.lower()
    if "@" not in text:
        return []
    ids: list[int] = []
    for row in conn.execute("SELECT id, COALESCE(account_handle, username) AS username, display_name FROM users").fetchall():
        tokens = {row["username"].strip().lower(), row["display_name"].strip().lower()}
        for token in tokens:
            if not token:
                continue
            needle = f"@{token}"
            start = 0
            matched = False
            while (idx := text.find(needle, start)) != -1:
                end = idx + len(needle)
                if end >= len(text) or not (text[end].isalnum() or text[end] == "_"):
                    matched = True
                    break
                start = idx + 1
            if matched:
                ids.append(row["id"])
                break
    return ids


def comment_payload(row: sqlite3.Row, conn: sqlite3.Connection) -> dict[str, Any]:
    user = conn.execute("SELECT * FROM users WHERE id = ?", (row["user_id"],)).fetchone()
    mentions = conn.execute(
        """
        SELECT u.* FROM comment_mentions cm
        JOIN users u ON u.id = cm.user_id
        WHERE cm.comment_id = ?
        ORDER BY u.display_name COLLATE NOCASE
        """,
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "dope_id": row["dope_id"],
        "body": row["body"],
        "created_at": row["created_at"],
        "user": user_public(user),
        "mentions": [user_public(m) for m in mentions],
    }


def comment_meta(conn: sqlite3.Connection, dope_id: int, viewer_id: int | None) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               MAX(c.created_at) AS latest,
               SUM(CASE WHEN c.user_id != ? AND c.created_at > COALESCE(r.last_read_at, '') THEN 1 ELSE 0 END) AS unread,
               SUM(CASE WHEN c.user_id != ? AND c.created_at > COALESCE(r.last_read_at, '') AND m.user_id IS NOT NULL THEN 1 ELSE 0 END) AS unread_mentions
        FROM comments c
        LEFT JOIN comment_reads r ON r.dope_id = c.dope_id AND r.user_id = ?
        LEFT JOIN comment_mentions m ON m.comment_id = c.id AND m.user_id = ?
        WHERE c.dope_id = ?
        """,
        (viewer_id, viewer_id, viewer_id, viewer_id, dope_id),
    ).fetchone()
    return {
        "comment_count": int(row["total"] or 0),
        "latest_comment_at": row["latest"],
        "unread_comments": int(row["unread"] or 0) if viewer_id is not None else 0,
        "unread_mentions": int(row["unread_mentions"] or 0) if viewer_id is not None else 0,
    }


def mark_comments_read(conn: sqlite3.Connection, dope_id: int, user_id: int, read_at: str) -> None:
    conn.execute(
        """
        INSERT INTO comment_reads (dope_id, user_id, last_read_at) VALUES (?, ?, ?)
        ON CONFLICT(dope_id, user_id) DO UPDATE SET last_read_at = excluded.last_read_at
        WHERE excluded.last_read_at > comment_reads.last_read_at
        """,
        (dope_id, user_id, read_at),
    )


def dope_payload(row: sqlite3.Row, conn: sqlite3.Connection, viewer_id: int | None = None) -> dict[str, Any]:
    user_ids = sorted(
        {
            row[field]
            for field in [
                "created_by",
                "assigned_to",
                "completed_by",
                "saved_by",
                "archived_by",
                "review_requested_by",
                "review_approved_by",
                "review_rejected_by",
            ]
            if row[field] is not None
        }
    )
    placeholders = ", ".join("?" for _ in user_ids)
    users = {
        u["id"]: u
        for u in conn.execute(f"SELECT * FROM users WHERE id IN ({placeholders})", user_ids).fetchall()
    } if user_ids else {}
    history = conn.execute(
        "SELECT * FROM assignment_history WHERE dope_id = ? ORDER BY assigned_at DESC, id DESC",
        (row["id"],),
    ).fetchall()
    links = conn.execute("SELECT url FROM commit_links WHERE dope_id = ? ORDER BY id", (row["id"],)).fetchall()
    versions = conn.execute(
        """
        SELECT v.id, v.version_number, v.title, v.description_html, v.edited_by, v.edited_at,
               COALESCE(u.account_handle, u.username) AS editor_username, u.display_name AS editor_display_name
        FROM dope_versions v
        JOIN users u ON u.id = v.edited_by
        WHERE v.dope_id = ?
        ORDER BY v.version_number DESC
        """,
        (row["id"],),
    ).fetchall()
    dependencies = conn.execute(
        """
        SELECT d.id, d.title, d.time_minutes, d.completed_at, d.archived_at
        FROM dope_dependencies dd
        JOIN dopes d ON d.id = dd.depends_on_id
        WHERE dd.dope_id = ?
        ORDER BY d.completed_at IS NULL DESC, d.id DESC
        """,
        (row["id"],),
    ).fetchall()
    dependents = active_dependent_rows(conn, row["id"])
    dependent_count = len(dependents)
    blocked_dependencies = [dep for dep in dependencies if dep["completed_at"] is None]
    category = category_public(
        conn.execute(
            "SELECT id, name, color FROM categories WHERE id = ?", (row["category_id"],)
        ).fetchone()
    ) if row["category_id"] else None
    return {
        "id": row["id"],
        "title": row["title"],
        "category": category,
        **comment_meta(conn, row["id"], viewer_id),
        "description_html": row["description_html"],
        "description": plain_description(row["description_html"]),
        "time_minutes": row["time_minutes"],
        "created_at": row["created_at"],
        "assigned_at": row["assigned_at"],
        "completed_at": row["completed_at"],
        "completion_description": row["completion_description"] or "",
        "saved_at": row["saved_at"],
        "archived_at": row["archived_at"],
        "status": status_for(row),
        "review": {
            "requested_by": user_public(users.get(row["review_requested_by"])),
            "requested_at": row["review_requested_at"],
            "note": row["review_note"] or "",
            "branch_url": row["review_branch_url"] or "",
            "approved_by": user_public(users.get(row["review_approved_by"])),
            "approved_at": row["review_approved_at"],
            "rejected_by": user_public(users.get(row["review_rejected_by"])),
            "rejected_at": row["review_rejected_at"],
            "rejection_note": row["review_rejection_note"] or "",
            "followup_id": row["review_followup_id"],
            "parent_id": row["review_parent_id"],
            "priority": int(row["review_priority"] or 0),
        },
        "created_by": user_public(users.get(row["created_by"])),
        "assigned_to": user_public(users.get(row["assigned_to"])),
        "completed_by": user_public(users.get(row["completed_by"])),
        "saved_by": user_public(users.get(row["saved_by"])),
        "archived_by": user_public(users.get(row["archived_by"])),
        "assignment_history": [dict(h) for h in history],
        "commit_links": [l["url"] for l in links],
        "dependencies": [
            {
                "id": dep["id"],
                "title": dep["title"],
                "time_minutes": dep["time_minutes"],
                "status": "archived" if dep["archived_at"] else "completed" if dep["completed_at"] else "active",
                "completed_at": dep["completed_at"],
            }
            for dep in dependencies
        ],
        "blocked_dependencies": [
            {
                "id": dep["id"],
                "title": dep["title"],
                "time_minutes": dep["time_minutes"],
                "status": "archived" if dep["archived_at"] else "active",
            }
            for dep in blocked_dependencies
        ],
        "dependent_count": dependent_count,
        "dependents": dependents,
        "versions": [
            {
                "id": v["id"],
                "version_number": v["version_number"],
                "title": v["title"],
                "description_html": v["description_html"],
                "edited_at": v["edited_at"],
                "edited_by": {
                    "id": v["edited_by"],
                    "username": v["editor_username"],
                    "display_name": v["editor_display_name"],
                    "color": "#1a1a1a",
                },
            }
            for v in versions
        ],
    }


@app.head("/")
@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "templates" / "index.html")


@app.get("/interface-api", response_class=HTMLResponse, include_in_schema=False)
@lru_cache(maxsize=1)
def interface_guide():
    guide = (ROOT.parent / "docs/interface-api.md").read_text()
    body = markdown.markdown(guide.replace("](openapi.json)", "](/openapi.json)"), extensions=["fenced_code", "tables"])
    return HTMLResponse('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                        '<title>Silicon Dopes interface API</title><link rel="stylesheet" href="/static/vendor/fonts.css">'
                        '<style>body{max-width:84ch;margin:2rem auto;padding:0 1rem;background:#ede8e0;color:#1a1a1a;'
                        'font:1rem/1.55 Inter,sans-serif}pre,table{display:block;overflow:auto}code{overflow-wrap:anywhere}'
                        'th,td{padding:.4rem;text-align:left;border-bottom:1px solid #d4cfc7}a{color:#244f65}</style>'
                        '<nav><a href="/docs">Interactive reference</a> · <a href="/openapi.json">OpenAPI JSON</a></nav><main>'
                        + body + '</main></html>')


@app.get("/api/me", response_model=views.BoardUser)
def me(
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    return user_public(member.user)  # type: ignore[return-value]


@app.patch("/api/me")
def update_me(
    data: ProfileIn,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    user = member.user
    display_name = data.display_name.strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="Display name is required")
    color = normalize_color(data.color)
    with db(member.path, write=True) as conn:
        conn.execute(
            "UPDATE users SET display_name = ?, color = ? WHERE id = ?",
            (display_name, color, user["id"]),
        )
        updated = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    return user_public(updated)  # type: ignore[return-value]


def browser_member(member: BoardMember = Depends(board_member)) -> BoardMember:
    if not member.browser_session:
        raise HTTPException(403, "Sign in through the browser to manage API keys.")
    return member


@app.get("/api/me/keys")
def list_api_keys(member: BoardMember = Depends(browser_member)) -> list[dict]:
    with accounts.session_db() as conn:
        rows = conn.execute("SELECT id, name, prefix, created_at, last_used_at FROM board_keys "
                            "WHERE session_hash=? AND board_id=? ORDER BY id DESC",
                            (member.browser_session, member.board.id)).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/me/keys")
def create_api_key(data: ApiKeyIn, member: BoardMember = Depends(browser_member)) -> dict:
    key = "dope_" + secrets.token_urlsafe(32)
    name, created_at = data.name.strip() or "API key", now_iso()
    with accounts.session_db() as conn:
        row = conn.execute("INSERT INTO board_keys (session_hash, board_id, key_hash, prefix, name, created_at) "
                           "VALUES (?, ?, ?, ?, ?, ?)",
                           (member.browser_session, member.board.id, accounts.digest(key), key[:14], name, created_at))
    return {"id": row.lastrowid, "name": name, "key": key, "prefix": key[:14], "created_at": created_at}


@app.delete("/api/me/keys/{key_id}")
def revoke_api_key(key_id: int, member: BoardMember = Depends(browser_member)) -> dict:
    with accounts.session_db() as conn:
        changed = conn.execute("DELETE FROM board_keys WHERE id=? AND board_id=? AND session_hash=?",
                               (key_id, member.board.id, member.browser_session))
        if not changed.rowcount:
            raise HTTPException(404, "API key not found.")
    return {"ok": True}


@app.get("/api/categories", response_model=list[views.Category])
def list_categories(
    member: BoardMember = Depends(board_member),
) -> list[dict[str, Any]]:
    with db(member.path) as conn:
        rows = conn.execute(
            "SELECT id, name, color FROM categories ORDER BY position, id"
        ).fetchall()
    return [category_public(row) for row in rows]  # type: ignore[misc]


@app.post("/api/categories")
def create_category(
    data: CategoryIn,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Category name is required")
    color = normalize_color(data.color)
    with db(member.path, write=True) as conn:
        position = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM categories").fetchone()[0]
        try:
            cur = conn.execute(
                "INSERT INTO categories (name, color, position, created_at) VALUES (?, ?, ?, ?)",
                (name, color, position, now_iso()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Category name already exists") from None
        row = conn.execute("SELECT id, name, color FROM categories WHERE id = ?", (cur.lastrowid,)).fetchone()
    return category_public(row)  # type: ignore[return-value]


@app.patch("/api/categories/{category_id}")
def update_category(
    category_id: int,
    data: CategoryIn,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Category name is required")
    color = normalize_color(data.color)
    with db(member.path, write=True) as conn:
        if not conn.execute("SELECT 1 FROM categories WHERE id = ?", (category_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Category not found")
        try:
            conn.execute(
                "UPDATE categories SET name = ?, color = ? WHERE id = ?",
                (name, color, category_id),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Category name already exists") from None
        row = conn.execute("SELECT id, name, color FROM categories WHERE id = ?", (category_id,)).fetchone()
    return category_public(row)  # type: ignore[return-value]


@app.delete("/api/categories/{category_id}")
def delete_category(
    category_id: int,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    with db(member.path, write=True) as conn:
        if not conn.execute("SELECT 1 FROM categories WHERE id = ?", (category_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Category not found")
        conn.execute("UPDATE dopes SET category_id = NULL WHERE category_id = ?", (category_id,))
        conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    return {"ok": True}


@app.get("/api/board")
def board_overview(member: BoardMember = Depends(board_member)) -> dict:
    # One query for the list. Detailed history, rich text and threads stay on the dope API.
    with db(member.path) as conn:
        rows = conn.execute("""
            SELECT d.id, d.title, d.description_html, d.created_at, d.completed_at, d.saved_at,
                   d.archived_at, d.completion_description, d.review_requested_at, d.review_approved_at,
                   d.review_rejected_at, d.assigned_to, creator.display_name AS creator,
                   assignee.display_name AS assignee, finisher.display_name AS finisher,
                   EXISTS (SELECT 1 FROM dope_dependencies deps JOIN dopes parent ON parent.id=deps.depends_on_id
                           WHERE deps.dope_id=d.id AND parent.completed_at IS NULL) AS blocked
            FROM dopes d JOIN users creator ON d.created_by=creator.id
            LEFT JOIN users assignee ON d.assigned_to=assignee.id
            LEFT JOIN users finisher ON d.completed_by=finisher.id
            ORDER BY COALESCE(d.completed_at, d.created_at) DESC, d.id DESC
        """).fetchall()
    items = [{
        "id": r["id"], "title": r["title"], "description": plain_description(r["description_html"]),
        "status": status_for(r), "creator": r["creator"], "created_at": r["created_at"],
        "assignee": r["assignee"], "assigned_to": r["assigned_to"], "finisher": r["finisher"],
        "completed_at": r["completed_at"], "note": r["completion_description"], "blocked": bool(r["blocked"]),
    } for r in rows]
    return {"board": member.board, "viewer_id": member.user["id"], "dopes": items}


@app.get("/api/dopes", response_model=list[views.Dope])
def list_dopes(
    status: str = "active",
    member: BoardMember = Depends(board_member),
) -> list[dict[str, Any]]:
    user = member.user
    where = {
        "active": "archived_at IS NULL AND completed_at IS NULL AND saved_at IS NULL",
        "saved": "archived_at IS NULL AND completed_at IS NULL AND saved_at IS NOT NULL",
        "completed": "archived_at IS NULL AND completed_at IS NOT NULL AND saved_at IS NULL",
        "review": "archived_at IS NULL AND completed_at IS NOT NULL AND saved_at IS NULL AND review_requested_at IS NOT NULL AND review_approved_at IS NULL AND review_rejected_at IS NULL",
        "archived": "archived_at IS NOT NULL",
        "all": "1 = 1",
    }.get(status)
    if not where:
        raise HTTPException(status_code=400, detail="Bad status")
    if status == "review":
        require_manager(member)
    if status == "active":
        order = """
        CASE
          WHEN review_priority > 0 THEN -1
          WHEN (
            SELECT COUNT(*)
            FROM dope_dependencies dd
            JOIN dopes child ON child.id = dd.dope_id
            WHERE dd.depends_on_id = dopes.id
              AND child.archived_at IS NULL
              AND child.completed_at IS NULL
              AND child.saved_at IS NULL
          ) > 0 THEN 0
          WHEN (
            SELECT COUNT(*)
            FROM dope_dependencies dd
            JOIN dopes parent ON parent.id = dd.depends_on_id
            WHERE dd.dope_id = dopes.id
              AND parent.archived_at IS NULL
          ) = 0 THEN 1
          ELSE 2
        END,
        CASE
          WHEN review_priority > 0 THEN -review_priority
          WHEN (
            SELECT COUNT(*)
            FROM dope_dependencies dd
            JOIN dopes child ON child.id = dd.dope_id
            WHERE dd.depends_on_id = dopes.id
              AND child.archived_at IS NULL
              AND child.completed_at IS NULL
              AND child.saved_at IS NULL
          ) > 0 THEN -(
            SELECT COUNT(*)
            FROM dope_dependencies dd
            JOIN dopes child ON child.id = dd.dope_id
            WHERE dd.depends_on_id = dopes.id
              AND child.archived_at IS NULL
              AND child.completed_at IS NULL
              AND child.saved_at IS NULL
          )
          WHEN (
            SELECT COUNT(*)
            FROM dope_dependencies dd
            JOIN dopes parent ON parent.id = dd.depends_on_id
            WHERE dd.dope_id = dopes.id
              AND parent.archived_at IS NULL
          ) > 0 THEN (
            SELECT COUNT(*)
            FROM dope_dependencies dd
            JOIN dopes parent ON parent.id = dd.depends_on_id
            WHERE dd.dope_id = dopes.id
              AND parent.archived_at IS NULL
          )
          ELSE 0
        END,
        title COLLATE NOCASE ASC,
        id DESC
        """
    elif status == "saved":
        order = "saved_at DESC, id DESC"
    else:
        order = """
        CASE
          WHEN completed_at IS NOT NULL
           AND review_requested_at IS NOT NULL
           AND review_approved_at IS NULL
           AND review_rejected_at IS NULL THEN 0
          ELSE 1
        END,
        completed_at DESC,
        id DESC
        """ if status in {"completed", "review"} else "id DESC"
    with db(member.path) as conn:
        rows = conn.execute(f"SELECT * FROM dopes WHERE {where} ORDER BY {order}").fetchall()
        payloads = [dope_payload(row, conn, user["id"]) for row in rows]
    if status == "active":
        def active_sort_key(item: dict[str, Any]) -> tuple[int, int, str, int]:
            review_priority = int((item.get("review") or {}).get("priority") or 0)
            if review_priority:
                return (-1, -review_priority, item["title"].lower(), -int(item["id"]))
            dependency_count = len([dep for dep in item["dependencies"] if dep["status"] != "archived"])
            if item["dependent_count"]:
                return (0, -int(item["dependent_count"]), item["title"].lower(), -int(item["id"]))
            if dependency_count == 0:
                return (1, 0, item["title"].lower(), -int(item["id"]))
            return (2, dependency_count, item["title"].lower(), -int(item["id"]))

        payloads.sort(key=active_sort_key)
    return payloads


@app.get("/api/settings/dope-day")
def get_dope_day_settings(
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    require_manager(member)
    with db(member.path) as conn:
        row = conn.execute(
            "SELECT * FROM dope_day_reset_changes ORDER BY changed_at DESC, id DESC LIMIT 1"
        ).fetchone()
    return dope_day_settings_payload(row)


@app.put("/api/settings/dope-day")
def update_dope_day_settings(
    data: DopeDaySettingsIn,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    user = member.user
    require_manager(member)
    reset_minutes = parse_reset_time(data.reset_time)
    changed_at = utc_now().astimezone(timezone.utc)
    retroactive_from = changed_at - timedelta(minutes=RESET_RETROACTIVE_MINUTES)
    with db(member.path, write=True) as conn:
        row = conn.execute(
            "SELECT * FROM dope_day_reset_changes ORDER BY changed_at DESC, id DESC LIMIT 1"
        ).fetchone()
        current_minutes = int(row["reset_minutes"]) if row else DEFAULT_DOPE_DAY_RESET_MINUTES
        if reset_minutes != current_minutes:
            conn.execute(
                """
                INSERT INTO dope_day_reset_changes
                  (reset_minutes, changed_by, changed_at, retroactive_from)
                VALUES (?, ?, ?, ?)
                """,
                (
                    reset_minutes,
                    user["id"],
                    changed_at.isoformat(timespec="seconds"),
                    retroactive_from.isoformat(timespec="seconds"),
                ),
            )
            row = conn.execute(
                "SELECT * FROM dope_day_reset_changes ORDER BY changed_at DESC, id DESC LIMIT 1"
            ).fetchone()
    return dope_day_settings_payload(row, changed_at)


@app.get("/api/stats/progress")
def progress_stats(
    days: int = 7,
    member: BoardMember = Depends(board_member),
) -> list[dict[str, Any]]:
    if days not in {7, 14, 30}:
        raise HTTPException(status_code=400, detail="Progress range must be 7, 14, or 30 days")

    with db(member.path) as conn:
        reset_changes = load_reset_changes(conn)
        current_reset_minutes = (
            int(reset_changes[-1]["reset_minutes"])
            if reset_changes
            else DEFAULT_DOPE_DAY_RESET_MINUTES
        )
        today = current_dope_day(reset_time_from_minutes(current_reset_minutes))
        first_day = today - timedelta(days=days - 1)
        buckets: dict[date, dict[int, dict[str, Any]]] = {
            first_day + timedelta(days=i): {} for i in range(days)
        }
        # Read one extra local calendar day so an older reset rule near the
        # chart boundary cannot drop a completion before it is re-bucketed.
        start_utc = (
            datetime.combine(first_day - timedelta(days=1), datetime_time.min, tzinfo=timezone.utc)
            - IST_OFFSET
        )
        rows = conn.execute(
            """
            SELECT d.time_minutes, d.completed_at, u.id AS user_id, u.display_name, u.color
            FROM dopes d
            JOIN users u ON u.id = d.completed_by
            WHERE d.archived_at IS NULL
              AND d.saved_at IS NULL
              AND d.completed_at IS NOT NULL
              AND d.completed_at >= ?
            ORDER BY d.completed_at ASC
            """,
            (start_utc.isoformat(timespec="seconds"),),
        ).fetchall()

    for row in rows:
        reset_minutes = reset_minutes_for_completion(row["completed_at"], reset_changes)
        day = dope_day_for(row["completed_at"], reset_time_from_minutes(reset_minutes))
        if day not in buckets:
            continue
        stack = buckets[day].setdefault(
            row["user_id"],
            {
                "user_id": row["user_id"],
                "display_name": row["display_name"],
                "color": normalize_color(row["color"]),
                "minutes": 0,
                "count": 0,
            },
        )
        stack["minutes"] += int(row["time_minutes"])
        stack["count"] += 1

    payload = []
    for day in sorted(buckets):
        stacks = sorted(buckets[day].values(), key=lambda item: (-item["minutes"], item["display_name"].lower()))
        total_minutes = sum(item["minutes"] for item in stacks)
        payload.append(
            {
                "date": day.isoformat(),
                "label": day.strftime("%b %-d") if os.name != "nt" else day.strftime("%b %#d"),
                "total_minutes": total_minutes,
                "stacks": stacks,
            }
        )
    return payload


def previous_create(conn, user_id, key, kind, data):
    fingerprint = hashlib.sha256(data.model_dump_json().encode()).hexdigest()
    row = conn.execute("SELECT * FROM create_receipts WHERE user_id=? AND operation=?", (user_id, key)).fetchone()
    if row and (row["kind"] != kind or row["fingerprint"] != fingerprint):
        raise HTTPException(409, "This operation key was already used for different content.")
    return (row["item_id"] if row else None), fingerprint


@app.post("/api/dopes", response_model=views.Dope)
def create_dope(
    data: DopeIn,
    key: str | None = sharing.OperationKey,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    user = member.user
    title, body = data.content()
    minutes = parse_time_to_minutes(data.time_text) if data.time_text else 0
    with db(member.path, write=True) as conn:
        prior, fingerprint = previous_create(conn, user["id"], key, "dope", data)
        if prior:
            return dope_payload(require_dope(conn, prior), conn, user["id"])
        category_id = validate_category_id(conn, data.category_id)
        cur = conn.execute(
            """
            INSERT INTO dopes (title, description_html, time_minutes, created_by, created_at, category_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (title, body, minutes, user["id"], now_iso(), category_id),
        )
        dope_id = int(cur.lastrowid)
        if key:
            conn.execute("INSERT INTO create_receipts VALUES (?,?,?,?,?)", (user["id"], key, "dope", fingerprint, dope_id))
        set_dope_dependencies(conn, dope_id, data.dependency_ids)
        add_dope_version(conn, dope_id, title, body, user["id"], now_iso())
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dope_payload(row, conn, user["id"])


@app.put("/api/dopes/{dope_id}", response_model=views.Dope)
def edit_dope(
    dope_id: int,
    data: DopeIn,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    user = member.user
    title, body = data.content()
    edited_at = now_iso()
    with db(member.path, write=True) as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row or row["archived_at"]:
            raise HTTPException(status_code=404, detail="Editable dope not found")
        minutes = parse_time_to_minutes(data.time_text) if data.time_text else row["time_minutes"]
        category_id = validate_category_id(conn, data.category_id) if "category_id" in data.model_fields_set else row["category_id"]
        if row["title"] != title or row["description_html"] != body or row["time_minutes"] != minutes:
            conn.execute(
                "UPDATE dopes SET title = ?, description_html = ?, time_minutes = ? WHERE id = ?",
                (title, body, minutes, dope_id),
            )
            add_dope_version(conn, dope_id, title, body, user["id"], edited_at)
        if row["category_id"] != category_id:
            conn.execute("UPDATE dopes SET category_id = ? WHERE id = ?", (category_id, dope_id))
        if "dependency_ids" in data.model_fields_set:
            set_dope_dependencies(conn, dope_id, data.dependency_ids)
        return dope_payload(conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(), conn, user["id"])


@app.post("/api/dopes/{dope_id}/assign", response_model=views.Dope)
def assign_dope(
    dope_id: int,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    user = member.user
    assigned_at = now_iso_precise()
    with db(member.path, write=True) as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row or row["archived_at"] or row["completed_at"] or row["saved_at"]:
            raise HTTPException(status_code=404, detail="Active dope not found")
        if row["assigned_to"] == user["id"]:
            return dope_payload(row, conn, user["id"])
        if row["assigned_to"] is not None:
            raise HTTPException(409, "A teammate has already taken this dope.")
        if incomplete_dependency_rows(conn, dope_id):
            raise HTTPException(status_code=400, detail="Dependencies Undoped")
        conn.execute(
            "UPDATE dopes SET assigned_to = ?, assigned_at = ? WHERE id = ?",
            (user["id"], assigned_at, dope_id),
        )
        conn.execute(
            "INSERT INTO assignment_history (dope_id, user_id, display_name, assigned_at) VALUES (?, ?, ?, ?)",
            (dope_id, user["id"], user["display_name"], assigned_at),
        )
        return dope_payload(conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(), conn, user["id"])


@app.post("/api/dopes/{dope_id}/unassign", response_model=views.Dope)
def unassign_dope(
    dope_id: int,
    data: UnassignIn = UnassignIn(),
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    user = member.user
    unassigned_at = now_iso_precise()
    reason = data.reason.strip() or "Released"
    with db(member.path, write=True) as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row or not row["assigned_to"] or row["archived_at"] or row["completed_at"] or row["saved_at"]:
            raise HTTPException(status_code=400, detail="Dope is not assigned")
        if row["assigned_to"] != user["id"] and not member.board.can_manage:
            raise HTTPException(403, "Only the assignee or a board administrator can release this dope.")
        conn.execute("UPDATE dopes SET assigned_to = NULL, assigned_at = NULL WHERE id = ?", (dope_id,))
        conn.execute(
            """
            UPDATE assignment_history SET unassigned_at = ?, unassign_reason = ?
            WHERE id = (
              SELECT id FROM assignment_history
              WHERE dope_id = ? AND unassigned_at IS NULL
              ORDER BY id DESC LIMIT 1
            )
            """,
            (unassigned_at, reason, dope_id),
        )
        return dope_payload(conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(), conn, user["id"])


@app.post("/api/dopes/{dope_id}/complete", response_model=views.Dope)
def complete_dope(
    dope_id: int,
    data: CompleteIn = CompleteIn(),
    member: BoardMember = Depends(board_member),
    context: accounts.Access = Depends(accounts.access),
) -> dict[str, Any]:
    user = member.user
    completion_text = (data.completion_text or data.completion_description or data.description).strip()
    raw_links = extract_http_links(completion_text) if data.completion_text is not None else data.commit_links
    clean_links = []
    for link in raw_links:
        clean_links.append(validate_http_url(link, "Commit links"))
    completed_at = now_iso_precise()
    eligible = None
    if data.completed_for_user_id is not None:
        require_manager(member)
        eligible = people.participants(context, member.board)
    with db(member.path, write=True) as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row or row["archived_at"] or row["saved_at"]:
            raise HTTPException(status_code=404, detail="Active dope not found")
        if row["completed_at"] and status_for(row) == "completed":
            return dope_payload(row, conn, user["id"])
        if row["completed_at"]:
            raise HTTPException(409, "This dope is awaiting review.")
        if incomplete_dependency_rows(conn, dope_id):
            raise HTTPException(status_code=400, detail="Dependencies Undoped")
        completed_by_id = user["id"]
        if data.completed_for_user_id is not None:
            completion_user = conn.execute(
                "SELECT id,account_uuid FROM users WHERE id = ?",
                (data.completed_for_user_id,),
            ).fetchone()
            if not completion_user or completion_user["account_uuid"] not in eligible:
                raise HTTPException(status_code=400, detail="Completion user not found")
            completed_by_id = completion_user["id"]
        conn.execute(
            """
            UPDATE dopes
            SET completed_by = ?, completed_at = ?, completion_description = ?, assigned_to = NULL, assigned_at = NULL
            WHERE id = ?
            """,
            (completed_by_id, completed_at, completion_text, dope_id),
        )
        conn.executemany(
            "INSERT INTO commit_links (dope_id, url, created_at) VALUES (?, ?, ?)",
            [(dope_id, link, completed_at) for link in clean_links],
        )
        conn.execute(
            """
            UPDATE assignment_history SET unassigned_at = COALESCE(unassigned_at, ?)
            WHERE dope_id = ? AND unassigned_at IS NULL
            """,
            (completed_at, dope_id),
        )
        return dope_payload(conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(), conn, user["id"])


@app.post("/api/dopes/{dope_id}/review", response_model=views.Dope)
def send_dope_for_review(
    dope_id: int,
    data: ReviewRequestIn,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    user = member.user
    review_note = data.note.strip()
    if not review_note:
        raise HTTPException(status_code=400, detail="Reviewer note is required")
    branch_url = validate_http_url(data.branch_url, "Review branch link")
    completed_at = now_iso()
    with db(member.path, write=True) as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row or row["archived_at"] or row["completed_at"] or row["saved_at"]:
            raise HTTPException(status_code=404, detail="Active dope not found")
        if incomplete_dependency_rows(conn, dope_id):
            raise HTTPException(status_code=400, detail="Dependencies Undoped")
        conn.execute(
            """
            UPDATE dopes
            SET completed_by = ?,
                completed_at = ?,
                completion_description = ?,
                assigned_to = NULL,
                assigned_at = NULL,
                review_requested_by = ?,
                review_requested_at = ?,
                review_note = ?,
                review_branch_url = ?,
                review_approved_by = NULL,
                review_approved_at = NULL,
                review_rejected_by = NULL,
                review_rejected_at = NULL,
                review_rejection_note = NULL,
                review_followup_id = NULL,
                review_priority = 0
            WHERE id = ?
            """,
            (user["id"], completed_at, review_note, user["id"], completed_at, review_note, branch_url, dope_id),
        )
        conn.execute(
            """
            UPDATE assignment_history SET unassigned_at = COALESCE(unassigned_at, ?)
            WHERE dope_id = ? AND unassigned_at IS NULL
            """,
            (completed_at, dope_id),
        )
        return dope_payload(conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(), conn, user["id"])


@app.post("/api/dopes/{dope_id}/review/approve", response_model=views.Dope)
def approve_dope_review(
    dope_id: int,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    user = member.user
    require_manager(member)
    approved_at = now_iso()
    with db(member.path, write=True) as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row or status_for(row) != "review":
            raise HTTPException(status_code=404, detail="Pending review dope not found")
        conn.execute(
            "UPDATE dopes SET review_approved_by = ?, review_approved_at = ? WHERE id = ?",
            (user["id"], approved_at, dope_id),
        )
        return dope_payload(conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(), conn, user["id"])


@app.post("/api/dopes/{dope_id}/review/reject", response_model=views.Dope)
def reject_dope_review(
    dope_id: int,
    data: ReviewRejectIn,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    user = member.user
    require_manager(member)
    rejection_note = data.note.strip()
    if not rejection_note:
        raise HTTPException(status_code=400, detail="Reviewer notes are required")
    minutes = parse_time_to_minutes(data.time_text)
    rejected_at = now_iso()
    with db(member.path, write=True) as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row or status_for(row) != "review":
            raise HTTPException(status_code=404, detail="Pending review dope not found")
        owner_id = row["completed_by"]
        if owner_id is None:
            last_assignment = conn.execute(
                "SELECT user_id FROM assignment_history WHERE dope_id = ? ORDER BY id DESC LIMIT 1",
                (dope_id,),
            ).fetchone()
            owner_id = last_assignment["user_id"] if last_assignment else row["created_by"]
        owner = conn.execute("SELECT * FROM users WHERE id = ?", (owner_id,)).fetchone()
        if not owner:
            raise HTTPException(status_code=400, detail="Review owner not found")
        conn.execute(
            "INSERT INTO assignment_history (dope_id, user_id, display_name, assigned_at) VALUES (?, ?, ?, ?)",
            (dope_id, owner_id, owner["display_name"], rejected_at),
        )
        conn.execute(
            """
            UPDATE dopes
            SET completed_by = NULL,
                completed_at = NULL,
                completion_description = NULL,
                assigned_to = ?,
                assigned_at = ?,
                time_minutes = ?,
                review_rejected_by = ?,
                review_rejected_at = ?,
                review_rejection_note = ?,
                review_followup_id = NULL,
                review_priority = 1
            WHERE id = ?
            """,
            (owner_id, rejected_at, minutes, user["id"], rejected_at, rejection_note, dope_id),
        )
        return dope_payload(conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(), conn, user["id"])


@app.post("/api/dopes/{dope_id}/uncomplete", response_model=views.Dope)
def uncomplete_dope(
    dope_id: int,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    user = member.user
    with db(member.path, write=True) as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row or row["archived_at"] or not row["completed_at"]:
            raise HTTPException(status_code=404, detail="Completed dope not found")
        if row["review_followup_id"]:
            raise HTTPException(status_code=400, detail="Review has follow-up changes")
        reopened_assignment = conn.execute(
            """
            SELECT id, user_id, assigned_at
            FROM assignment_history
            WHERE dope_id = ? AND unassigned_at = ? AND unassign_reason IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (dope_id, row["completed_at"]),
        ).fetchone()
        conn.execute(
            """
            UPDATE dopes
            SET completed_by = NULL,
                completed_at = NULL,
                completion_description = NULL,
                review_requested_by = NULL,
                review_requested_at = NULL,
                review_note = NULL,
                review_branch_url = NULL,
                review_approved_by = NULL,
                review_approved_at = NULL,
                review_rejected_by = NULL,
                review_rejected_at = NULL,
                review_rejection_note = NULL,
                review_followup_id = NULL,
                assigned_to = ?,
                assigned_at = ?
            WHERE id = ?
            """,
            (
                reopened_assignment["user_id"] if reopened_assignment else None,
                reopened_assignment["assigned_at"] if reopened_assignment else None,
                dope_id,
            ),
        )
        if reopened_assignment:
            conn.execute(
                "UPDATE assignment_history SET unassigned_at = NULL WHERE id = ?",
                (reopened_assignment["id"],),
            )
        conn.execute("DELETE FROM commit_links WHERE dope_id = ?", (dope_id,))
        return dope_payload(conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(), conn, user["id"])


@app.patch("/api/dopes/{dope_id}/category", response_model=views.Dope)
def set_dope_category(
    dope_id: int,
    data: DopeCategoryIn,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    user = member.user
    with db(member.path, write=True) as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Dope not found")
        category_id = validate_category_id(conn, data.category_id)
        conn.execute("UPDATE dopes SET category_id = ? WHERE id = ?", (category_id, dope_id))
        return dope_payload(conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(), conn, user["id"])


@app.post("/api/dopes/{dope_id}/save-for-later", response_model=views.Dope)
def save_dope_for_later(
    dope_id: int,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    user = member.user
    with db(member.path, write=True) as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row or row["archived_at"] or row["completed_at"] or row["saved_at"]:
            raise HTTPException(status_code=404, detail="Active dope not found")
        conn.execute(
            "UPDATE dopes SET saved_by = ?, saved_at = ? WHERE id = ?",
            (user["id"], now_iso(), dope_id),
        )
        return dope_payload(
            conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(),
            conn,
            user["id"],
        )


@app.post("/api/dopes/{dope_id}/move-to-active", response_model=views.Dope)
def move_saved_dope_to_active(
    dope_id: int,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    user = member.user
    with db(member.path, write=True) as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row or row["archived_at"] or row["completed_at"] or not row["saved_at"]:
            raise HTTPException(status_code=404, detail="Saved dope not found")
        conn.execute(
            "UPDATE dopes SET saved_by = NULL, saved_at = NULL WHERE id = ?",
            (dope_id,),
        )
        return dope_payload(
            conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(),
            conn,
            user["id"],
        )


@app.post("/api/dopes/{dope_id}/archive", response_model=views.Dope)
def archive_dope(
    dope_id: int,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    user = member.user
    with db(member.path, write=True) as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Dope not found")
        conn.execute(
            "UPDATE dopes SET archived_by = ?, archived_at = ? WHERE id = ?",
            (user["id"], now_iso(), dope_id),
        )
        return dope_payload(conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(), conn, user["id"])


@app.post("/api/dopes/{dope_id}/restore", response_model=views.Dope)
def restore_dope(
    dope_id: int,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    user = member.user
    with db(member.path, write=True) as conn:
        row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Dope not found")
        conn.execute("UPDATE dopes SET archived_by = NULL, archived_at = NULL WHERE id = ?", (dope_id,))
        return dope_payload(conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone(), conn, user["id"])


@app.get("/api/users")
def list_users(
    member: BoardMember = Depends(board_member),
) -> list[dict[str, Any]]:
    with db(member.path) as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY display_name COLLATE NOCASE, id").fetchall()
    return [
        {
            **user_public(row),  # type: ignore[dict-item]
            "online": is_online(row["last_seen_at"]),
            "last_seen_at": row["last_seen_at"],
        }
        for row in rows
    ]


def require_dope(conn: sqlite3.Connection, dope_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM dopes WHERE id = ?", (dope_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Dope not found")
    return row


@app.get("/api/dopes/{dope_id}", response_model=views.Dope)
def get_dope(dope_id: int, member: BoardMember = Depends(board_member)) -> dict:
    with db(member.path) as conn:
        return dope_payload(require_dope(conn, dope_id), conn, member.user["id"])


@app.get("/api/dopes/{dope_id}/comments")
def list_comments(
    dope_id: int,
    member: BoardMember = Depends(board_member),
) -> list[dict[str, Any]]:
    with db(member.path) as conn:
        require_dope(conn, dope_id)
        rows = conn.execute(
            "SELECT * FROM comments WHERE dope_id = ? ORDER BY created_at, id",
            (dope_id,),
        ).fetchall()
        return [comment_payload(row, conn) for row in rows]


@app.post("/api/dopes/{dope_id}/comments")
def create_comment(
    dope_id: int,
    data: CommentIn,
    key: str | None = sharing.OperationKey,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    user = member.user
    body = data.body.strip()
    if not body:
        raise HTTPException(status_code=400, detail="Comment cannot be empty")
    created_at = now_iso_precise()
    with db(member.path, write=True) as conn:
        require_dope(conn, dope_id)
        kind = f"comments/{dope_id}"
        prior, fingerprint = previous_create(conn, user["id"], key, kind, data)
        if prior:
            row = conn.execute("SELECT * FROM comments WHERE id=?", (prior,)).fetchone()
            if not row:
                raise HTTPException(409, "The original comment has since been removed.")
            return comment_payload(row, conn)
        cur = conn.execute(
            "INSERT INTO comments (dope_id, user_id, body, created_at) VALUES (?, ?, ?, ?)",
            (dope_id, user["id"], body, created_at),
        )
        comment_id = int(cur.lastrowid)
        if key:
            conn.execute("INSERT INTO create_receipts VALUES (?,?,?,?,?)", (user["id"], key, kind, fingerprint, comment_id))
        mention_ids = extract_mention_user_ids(conn, body)
        conn.executemany(
            "INSERT OR IGNORE INTO comment_mentions (comment_id, user_id) VALUES (?, ?)",
            [(comment_id, mention_id) for mention_id in mention_ids],
        )
        mark_comments_read(conn, dope_id, user["id"], created_at)
        row = conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
        return comment_payload(row, conn)


@app.delete("/api/dopes/{dope_id}/comments/{comment_id}")
def delete_comment(
    dope_id: int,
    comment_id: int,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    user = member.user
    with db(member.path, write=True) as conn:
        row = conn.execute(
            "SELECT * FROM comments WHERE id = ? AND dope_id = ?", (comment_id, dope_id)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Comment not found")
        if row["user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="You can only delete your own comments")
        conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    return {"ok": True}


@app.get("/api/diagnostics")
def diagnostics(
    limit: int = 50,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    now = datetime.now(timezone.utc)
    week_ago = (now - timedelta(days=7)).isoformat(timespec="seconds")
    two_weeks_ago = (now - timedelta(days=14)).isoformat(timespec="seconds")
    month_ago = (now - timedelta(days=30)).isoformat(timespec="seconds")
    with db(member.path) as conn:
        users = conn.execute("SELECT * FROM users ORDER BY display_name COLLATE NOCASE, id").fetchall()
        active_rows = conn.execute(
            """
            SELECT d.id, d.time_minutes, d.assigned_to, d.category_id,
              EXISTS(
                SELECT 1 FROM dope_dependencies dd
                JOIN dopes p ON p.id = dd.depends_on_id
                WHERE dd.dope_id = d.id AND p.completed_at IS NULL
              ) AS blocked
            FROM dopes d
            WHERE d.archived_at IS NULL
              AND d.completed_at IS NULL
              AND d.saved_at IS NULL
            """
        ).fetchall()
        completed_rows = conn.execute(
            """
            SELECT completed_by, time_minutes, completed_at
            FROM dopes
            WHERE archived_at IS NULL
              AND completed_at IS NOT NULL
              AND saved_at IS NULL
            """
        ).fetchall()
        archived_count = conn.execute(
            "SELECT COUNT(*) FROM dopes WHERE archived_at IS NOT NULL AND saved_at IS NULL"
        ).fetchone()[0]
        created_counts = dict(
            conn.execute(
                "SELECT created_by, COUNT(*) FROM dopes WHERE saved_at IS NULL GROUP BY created_by"
            ).fetchall()
        )
        comment_counts = dict(
            conn.execute(
                """
                SELECT c.user_id, COUNT(*)
                FROM comments c
                JOIN dopes d ON d.id = c.dope_id
                WHERE d.saved_at IS NULL
                GROUP BY c.user_id
                """
            ).fetchall()
        )
        categories = {row["id"]: row for row in conn.execute("SELECT id, name, color FROM categories").fetchall()}

        totals = {
            "total": len(active_rows) + len(completed_rows) + int(archived_count),
            "active": len(active_rows),
            "completed": len(completed_rows),
            "archived": int(archived_count),
            "active_minutes": sum(r["time_minutes"] for r in active_rows),
            "completed_minutes": sum(r["time_minutes"] for r in completed_rows),
            "blocked": sum(1 for r in active_rows if r["blocked"]),
            "blocked_minutes": sum(r["time_minutes"] for r in active_rows if r["blocked"]),
            "in_progress": sum(1 for r in active_rows if r["assigned_to"]),
            "in_progress_minutes": sum(r["time_minutes"] for r in active_rows if r["assigned_to"]),
            "ready": sum(1 for r in active_rows if not r["blocked"] and not r["assigned_to"]),
            "ready_minutes": sum(r["time_minutes"] for r in active_rows if not r["blocked"] and not r["assigned_to"]),
            "completed_7d": sum(1 for r in completed_rows if r["completed_at"] >= week_ago),
            "completed_7d_minutes": sum(r["time_minutes"] for r in completed_rows if r["completed_at"] >= week_ago),
            "completed_prev7d_minutes": sum(
                r["time_minutes"] for r in completed_rows if two_weeks_ago <= r["completed_at"] < week_ago
            ),
        }

        by_category: dict[int | None, dict[str, Any]] = {}
        for row in active_rows:
            bucket = by_category.setdefault(
                row["category_id"],
                {"category": category_public(categories.get(row["category_id"])), "count": 0, "minutes": 0},
            )
            bucket["count"] += 1
            bucket["minutes"] += row["time_minutes"]
        remaining_by_category = sorted(by_category.values(), key=lambda b: -b["minutes"])

        per_person = []
        for u in users:
            stats = {
                "user": user_public(u),
                "online": is_online(u["last_seen_at"]),
                "last_seen_at": u["last_seen_at"],
                "completed_count": 0,
                "completed_minutes": 0,
                "completed_7d_count": 0,
                "completed_7d_minutes": 0,
                "completed_30d_count": 0,
                "completed_30d_minutes": 0,
                "in_progress_count": 0,
                "in_progress_minutes": 0,
                "created_count": int(created_counts.get(u["id"], 0)),
                "comments_count": int(comment_counts.get(u["id"], 0)),
            }
            per_person.append(stats)
        by_user = {p["user"]["id"]: p for p in per_person}
        for row in completed_rows:
            stats = by_user.get(row["completed_by"])
            if not stats:
                continue
            stats["completed_count"] += 1
            stats["completed_minutes"] += row["time_minutes"]
            if row["completed_at"] >= week_ago:
                stats["completed_7d_count"] += 1
                stats["completed_7d_minutes"] += row["time_minutes"]
            if row["completed_at"] >= month_ago:
                stats["completed_30d_count"] += 1
                stats["completed_30d_minutes"] += row["time_minutes"]
        for row in active_rows:
            stats = by_user.get(row["assigned_to"])
            if stats:
                stats["in_progress_count"] += 1
                stats["in_progress_minutes"] += row["time_minutes"]
        per_person.sort(key=lambda p: (-p["completed_minutes"], p["user"]["display_name"].lower()))

        def event(kind: str, at: str, user_row: sqlite3.Row | dict[str, Any] | None, dope_id: int, title: str, detail: str | None = None) -> dict[str, Any]:
            user = user_row if isinstance(user_row, dict) or user_row is None else user_public(user_row)
            return {"type": kind, "at": at, "user": user, "dope_id": dope_id, "dope_title": title, "detail": detail}

        events: list[dict[str, Any]] = []
        for row in conn.execute(
            "SELECT d.id, d.title, d.created_at AS at, u.id AS uid, COALESCE(u.account_handle, u.username) AS username, u.display_name, u.color "
            "FROM dopes d JOIN users u ON u.id = d.created_by "
            "WHERE d.saved_at IS NULL ORDER BY d.created_at DESC LIMIT ?",
            (limit,),
        ):
            events.append(event("created", row["at"], {"id": row["uid"], "username": row["username"], "display_name": row["display_name"], "color": row["color"]}, row["id"], row["title"]))
        for row in conn.execute(
            "SELECT d.id, d.title, d.completed_at AS at, d.review_requested_at, d.review_branch_url, "
            "u.id AS uid, COALESCE(u.account_handle, u.username) AS username, u.display_name, u.color "
            "FROM dopes d JOIN users u ON u.id = d.completed_by "
            "WHERE d.completed_at IS NOT NULL AND d.saved_at IS NULL "
            "ORDER BY d.completed_at DESC LIMIT ?",
            (limit,),
        ):
            kind = "reviewed" if row["review_requested_at"] else "completed"
            events.append(event(kind, row["at"], {"id": row["uid"], "username": row["username"], "display_name": row["display_name"], "color": row["color"]}, row["id"], row["title"], row["review_branch_url"] if kind == "reviewed" else None))
        for row in conn.execute(
            "SELECT d.id, d.title, d.archived_at AS at, u.id AS uid, COALESCE(u.account_handle, u.username) AS username, u.display_name, u.color "
            "FROM dopes d JOIN users u ON u.id = d.archived_by "
            "WHERE d.archived_at IS NOT NULL AND d.saved_at IS NULL "
            "ORDER BY d.archived_at DESC LIMIT ?",
            (limit,),
        ):
            events.append(event("archived", row["at"], {"id": row["uid"], "username": row["username"], "display_name": row["display_name"], "color": row["color"]}, row["id"], row["title"]))
        for row in conn.execute(
            "SELECT h.dope_id, h.assigned_at, h.unassigned_at, h.unassign_reason, d.title, "
            "u.id AS uid, COALESCE(u.account_handle, u.username) AS username, u.display_name, u.color "
            "FROM assignment_history h JOIN dopes d ON d.id = h.dope_id JOIN users u ON u.id = h.user_id "
            "WHERE d.saved_at IS NULL "
            "ORDER BY h.assigned_at DESC LIMIT ?",
            (limit,),
        ):
            actor = {"id": row["uid"], "username": row["username"], "display_name": row["display_name"], "color": row["color"]}
            events.append(event("assigned", row["assigned_at"], actor, row["dope_id"], row["title"]))
            # Completion auto-closes open history rows without a reason; only
            # explicit unassigns (which require a reason) are real events.
            if row["unassigned_at"] and row["unassign_reason"]:
                events.append(event("unassigned", row["unassigned_at"], actor, row["dope_id"], row["title"], row["unassign_reason"]))
        for row in conn.execute(
            "SELECT c.dope_id, c.body, c.created_at AS at, d.title, u.id AS uid, COALESCE(u.account_handle, u.username) AS username, u.display_name, u.color "
            "FROM comments c JOIN dopes d ON d.id = c.dope_id JOIN users u ON u.id = c.user_id "
            "WHERE d.saved_at IS NULL "
            "ORDER BY c.created_at DESC LIMIT ?",
            (limit,),
        ):
            snippet = row["body"] if len(row["body"]) <= 120 else row["body"][:117] + "..."
            events.append(event("commented", row["at"], {"id": row["uid"], "username": row["username"], "display_name": row["display_name"], "color": row["color"]}, row["dope_id"], row["title"], snippet))
        for row in conn.execute(
            "SELECT v.dope_id, v.edited_at AS at, d.title, u.id AS uid, COALESCE(u.account_handle, u.username) AS username, u.display_name, u.color "
            "FROM dope_versions v JOIN dopes d ON d.id = v.dope_id JOIN users u ON u.id = v.edited_by "
            "WHERE v.version_number > 1 AND d.saved_at IS NULL ORDER BY v.edited_at DESC LIMIT ?",
            (limit,),
        ):
            events.append(event("edited", row["at"], {"id": row["uid"], "username": row["username"], "display_name": row["display_name"], "color": row["color"]}, row["dope_id"], row["title"]))

        events.sort(key=lambda e: e["at"], reverse=True)
        return {
            "generated_at": now_iso(),
            "totals": totals,
            "remaining_by_category": remaining_by_category,
            "per_person": per_person,
            "activity": events[:limit],
        }


@app.post("/api/dopes/{dope_id}/comments/read")
def read_comments(
    dope_id: int,
    member: BoardMember = Depends(board_member),
) -> dict[str, Any]:
    user = member.user
    read_at = now_iso_precise()
    with db(member.path, write=True) as conn:
        require_dope(conn, dope_id)
        mark_comments_read(conn, dope_id, user["id"], read_at)
    return {"ok": True, "last_read_at": read_at}

@app.get("/api/workspace", response_model=views.Workspace)
def workspace(status: str = "active", days: int = 7, member: BoardMember = Depends(board_member)):
    """Load a board, viewer, categories and current list with one membership check."""
    return {"board": member.board, "viewer": me(member), "categories": list_categories(member),
            "dopes": list_dopes(status, member), "progress": progress_stats(days, member) if status == "active" else []}


# Advertise the same authentication accepted by the normal routes to interface generators.
def interface_schema():
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi
    schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
    schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    schemes["SiliconAccess"] = {"type": "http", "scheme": "bearer", "description": "An Accounts access token issued to dope."}
    schemes["BoardKey"] = {"type": "http", "scheme": "bearer", "description": "A session-bound key for one board."}
    schemes["BrowserSession"] = {"type": "apiKey", "in": "cookie", "name": accounts.COOKIE}
    for path, methods in schema["paths"].items():
        if not path.startswith("/api/") or path.startswith("/api/auth/"):
            continue
        account_only = path.startswith(("/api/people", "/api/contacts", "/api/invitations")) or "/invitations" in path
        for method, operation in methods.items():
            browser_only = path.startswith("/api/me/keys")
            auth = [] if browser_only else [{"SiliconAccess": []}]
            if not browser_only and not account_only and not (path == "/api/boards" and method == "post"):
                auth.append({"BoardKey": []})
            operation["security"] = auth + [{"BrowserSession": []}]
            operation["parameters"] = [p for p in operation.get("parameters", []) if p.get("name", "").lower() != "authorization"]
    app.openapi_schema = schema
    return schema


app.openapi = interface_schema
