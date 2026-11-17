"""SQLite storage for independently authorized boards and browser sessions."""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path


def legacy_path() -> Path:
    return Path(os.getenv("DOPE_DB_PATH", Path(__file__).resolve().parents[1] / "dope.db"))


def storage_dir() -> Path:
    root = Path(f"{legacy_path()}.boards")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def board_path(board_id: str) -> Path:
    return storage_dir() / (hashlib.sha256(board_id.encode()).hexdigest() + ".sqlite3")


@contextmanager
def db(path: Path, *, write: bool = False) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(path, timeout=15)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn


def import_legacy(board_id: str) -> None:
    """Copy the former global board once, only into an explicitly chosen empty destination."""
    if not board_id.startswith(("organization:", "team:")) or not board_id.split(":", 1)[1]:
        raise ValueError("Use the exact organization or team board ID from /api/boards.")
    source = legacy_path().resolve()
    target = board_path(board_id)
    if not source.is_file() or target.exists():
        raise ValueError("Import requires an existing legacy database and a board that has never been opened.")
    with tempfile.NamedTemporaryFile(dir=target.parent) as scratch:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as old, db(Path(scratch.name)) as new:
            old.execute("SELECT id, created_by FROM dopes LIMIT 1")
            old.backup(new)
            new.execute("UPDATE users SET password_hash=''")
            if new.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='api_keys'").fetchone():
                new.execute("DELETE FROM api_keys")
        # Publish only the complete copy; never overwrite a board opened during import.
        os.link(scratch.name, target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import the former shared Dope database before opening its destination board.")
    parser.add_argument("board_id", help="organization:<org> or team:<org>/<team>")
    try:
        import_legacy(parser.parse_args().board_id)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Import failed: {exc}\n")
