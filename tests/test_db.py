from __future__ import annotations

from app import storage


def test_db_context_manager_closes_connection(tmp_path, monkeypatch):
    class FakeConnection:
        row_factory = None

        def __init__(self):
            self.closed = False
            self.exited = False

        def execute(self, *args, **kwargs):
            return None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.exited = True

        def close(self):
            self.closed = True

    fake = FakeConnection()
    monkeypatch.setattr(storage.sqlite3, "connect", lambda path, **kwargs: fake)

    with storage.db(tmp_path / "dope.db") as conn:
        assert conn is fake
        assert fake.closed is False

    assert fake.exited is True
    assert fake.closed is True
