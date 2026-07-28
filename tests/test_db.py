from __future__ import annotations

import importlib


def test_db_context_manager_closes_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("DOPE_DB_PATH", str(tmp_path / "dope.db"))
    import app.main as main

    main = importlib.reload(main)

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
    monkeypatch.setattr(main.sqlite3, "connect", lambda path: fake)

    with main.db() as conn:
        assert conn is fake
        assert fake.closed is False

    assert fake.exited is True
    assert fake.closed is True
