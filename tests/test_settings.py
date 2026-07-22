from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient


def load_main(tmp_path, monkeypatch):
    monkeypatch.setenv("DOPE_DB_PATH", str(tmp_path / "dope.db"))
    import app.main as main

    return importlib.reload(main)


def signup(client, username, display_name):
    response = client.post(
        "/api/auth/signup",
        json={"username": username, "password": "password", "display_name": display_name},
    )
    assert response.status_code == 200


def login(client, username):
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "password"},
    )
    assert response.status_code == 200


def test_only_saket_can_view_and_change_reset_settings(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)
    fixed_now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(main, "utc_now", lambda: fixed_now)

    with TestClient(main.app) as client:
        signup(client, "shubham", "Shubham")
        signup(client, "saket", "Saket")
        login(client, "shubham")
        assert client.get("/api/settings/dope-day").status_code == 403
        assert client.put("/api/settings/dope-day", json={"reset_time": "18:00"}).status_code == 403

        login(client, "saket")
        default = client.get("/api/settings/dope-day")
        assert default.status_code == 200
        assert default.json()["reset_time"] == "09:00"
        assert default.json()["timezone"] == "IST"
        assert default.json()["history_window_hours"] == 16
        assert default.json()["remaining_seconds"] == 15 * 60 * 60 + 30 * 60

        updated = client.put("/api/settings/dope-day", json={"reset_time": "18:00"})
        assert updated.status_code == 200
        payload = updated.json()
        assert payload["reset_time"] == "18:00"
        assert payload["changed_at"] == fixed_now.isoformat(timespec="seconds")
        assert payload["retroactive_from"] == (fixed_now - timedelta(hours=16)).isoformat(timespec="seconds")
        assert payload["remaining_seconds"] == 30 * 60


def test_reset_change_rebuckets_only_previous_16_hours(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)
    fixed_now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(main, "utc_now", lambda: fixed_now)

    with TestClient(main.app) as client:
        signup(client, "saket", "Saket")
        login(client, "saket")
        with main.db() as conn:
            user_id = conn.execute("SELECT id FROM users WHERE username = 'saket'").fetchone()[0]
            old_completion = datetime(2026, 8, 9, 4, 30, tzinfo=timezone.utc)
            recent_completion = datetime(2026, 8, 10, 4, 30, tzinfo=timezone.utc)
            conn.executemany(
                """
                INSERT INTO dopes
                  (title, description_html, time_minutes, created_by, created_at, completed_by, completed_at)
                VALUES (?, '<p>Done</p>', ?, ?, ?, ?, ?)
                """,
                [
                    ("Older than window", 30, user_id, old_completion.isoformat(), user_id, old_completion.isoformat()),
                    ("Inside window", 45, user_id, recent_completion.isoformat(), user_id, recent_completion.isoformat()),
                ],
            )

        assert client.put("/api/settings/dope-day", json={"reset_time": "18:00"}).status_code == 200
        progress = client.get("/api/stats/progress?days=7")
        assert progress.status_code == 200
        payload = progress.json()

    august_8 = next(day for day in payload if day["date"] == "2026-08-08")
    august_9 = next(day for day in payload if day["date"] == "2026-08-09")
    assert august_8["total_minutes"] == 0
    assert august_9["total_minutes"] == 75
    assert august_9["stacks"][0]["minutes"] == 75
    assert august_9["stacks"][0]["count"] == 2
