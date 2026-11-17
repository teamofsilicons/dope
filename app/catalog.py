"""Board ownership and invitations are separate from the contents of each board."""
from dataclasses import dataclass, replace

from .storage import db, storage_dir


def catalog(*, write=False):
    return db(storage_dir() / "catalog.sqlite3", write=write)


def init_catalog():
    with catalog() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS boards (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, name TEXT NOT NULL COLLATE NOCASE,
                creator TEXT NOT NULL, created_at TEXT NOT NULL, operation TEXT,
                UNIQUE(creator, operation)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS board_names ON boards(scope,name) WHERE id != scope;
            CREATE TABLE IF NOT EXISTS invitations (
                id TEXT PRIMARY KEY, board TEXT NOT NULL REFERENCES boards(id),
                sender TEXT NOT NULL, recipient TEXT NOT NULL, state TEXT NOT NULL
                CHECK(state IN ('pending','accepted','declined','revoked')),
                created_at TEXT NOT NULL, operation TEXT, UNIQUE(sender,operation)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS active_board_invite ON invitations(board,recipient)
                WHERE state IN ('pending','accepted');
            CREATE TABLE IF NOT EXISTS invitation_receipts (
                sender TEXT NOT NULL, operation TEXT NOT NULL, invitation TEXT NOT NULL REFERENCES invitations(id),
                PRIMARY KEY(sender,operation)
            );
            CREATE INDEX IF NOT EXISTS invitations_recipient ON invitations(recipient,created_at);
            CREATE INDEX IF NOT EXISTS invitations_sender ON invitations(sender,created_at);
            CREATE TABLE IF NOT EXISTS profiles (
                uuid TEXT PRIMARY KEY, handle TEXT NOT NULL, display_name TEXT NOT NULL, picture TEXT
            );
            CREATE TABLE IF NOT EXISTS contacts_connections (owner TEXT PRIMARY KEY, generation TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS contacts_proofs (
                owner TEXT NOT NULL REFERENCES contacts_connections(owner) ON DELETE CASCADE,
                session TEXT NOT NULL, scope TEXT NOT NULL, proof_id TEXT NOT NULL,
                proof TEXT NOT NULL, expires INTEGER NOT NULL, PRIMARY KEY(owner,session,scope)
            );
            CREATE TABLE IF NOT EXISTS board_files (
                id TEXT PRIMARY KEY, board TEXT NOT NULL, uploader TEXT NOT NULL, name TEXT NOT NULL,
                created_at TEXT NOT NULL, size INTEGER NOT NULL, digest TEXT NOT NULL, content BLOB NOT NULL,
                operation TEXT, UNIQUE(uploader,operation)
            );
            CREATE INDEX IF NOT EXISTS board_files_board ON board_files(board,created_at);
        """)


@dataclass(frozen=True)
class Board:
    id: str
    name: str
    kind: str
    organization: str | None
    can_manage: bool
    scope_id: str = ""
    shared_by: str | None = None

    @property
    def scope(self):
        return self.scope_id or self.id


def available_boards(account_uuid, roots):
    scopes = {b.id: b for b in roots}
    params = [account_uuid, *scopes]
    placeholders = ",".join("?" for _ in scopes)
    with catalog() as conn:
        rows = conn.execute(f"""
            SELECT b.*, i.sender FROM boards b LEFT JOIN invitations i
              ON i.board=b.id AND i.recipient=? AND i.state='accepted'
            WHERE b.scope IN ({placeholders}) OR (i.id IS NOT NULL AND b.scope LIKE 'personal:%')
            ORDER BY b.name COLLATE NOCASE,b.id
            """, params).fetchall()
    by_id = dict(scopes)
    for row in rows:
        parent = scopes.get(row["scope"])
        if parent:
            board = by_id.get(row["id"]) or Board(row["id"], row["name"], parent.kind,
                                                  parent.organization, parent.can_manage, parent.id)
            by_id[row["id"]] = replace(board, shared_by=row["sender"])
        else:
            by_id[row["id"]] = Board(row["id"], row["name"], "personal", None, False,
                                      row["scope"], row["sender"])
    return list(by_id.values())


def remember_profile(conn, person):
    # Only directory identities belong in shared history. Address-book aliases stay private.
    conn.execute("""INSERT INTO profiles VALUES (?,?,?,?) ON CONFLICT(uuid) DO UPDATE SET
                    handle=excluded.handle, display_name=excluded.display_name, picture=COALESCE(excluded.picture,profiles.picture)""",
                 (person["uuid"], person["id"], person.get("display_name") or person["id"], person.get("pfp_url")))
