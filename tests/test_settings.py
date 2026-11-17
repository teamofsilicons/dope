from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from silicon import load_main, login, org_db


def test_only_a_board_manager_can_view_and_change_reset_settings(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)
    fixed_now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(main, "utc_now", lambda: fixed_now)

    with TestClient(main.app) as client:
        login(client, "shubham", "Shubham")
        login(client, "saket", "Saket")
        login(client, "shubham")
        assert client.get("/api/settings/dope-day").status_code == 403
        assert client.put("/api/settings/dope-day", json={"reset_time": "18:00"}).status_code == 403

        login(client, "saket")
        default = client.get("/api/settings/dope-day")
        assert default.status_code == 200
        assert default.json()["reset_time"] == "09:00"
        assert default.json()["timezone"] == "IST"
        assert default.json()["history_window_minutes"] == 10
        assert default.json()["remaining_seconds"] == 15 * 60 * 60 + 30 * 60

        updated = client.put("/api/settings/dope-day", json={"reset_time": "18:00"})
        assert updated.status_code == 200
        payload = updated.json()
        assert payload["reset_time"] == "18:00"
        assert payload["changed_at"] == fixed_now.isoformat(timespec="seconds")
        assert payload["retroactive_from"] == (fixed_now - timedelta(minutes=10)).isoformat(timespec="seconds")
        assert payload["remaining_seconds"] == 30 * 60


def test_reset_change_rebuckets_only_previous_10_minutes(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)
    fixed_now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(main, "utc_now", lambda: fixed_now)

    with TestClient(main.app) as client:
        login(client, "saket", "Saket")
        with org_db() as conn:
            user_id = conn.execute("SELECT id FROM users WHERE account_handle = 'c:saket'").fetchone()[0]
            old_completion = fixed_now - timedelta(minutes=11)
            recent_completion = fixed_now - timedelta(minutes=5)
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
        with org_db() as conn:
            reset_changes = main.load_reset_changes(conn)
        assert main.reset_minutes_for_completion(old_completion, reset_changes) == 9 * 60
        assert main.reset_minutes_for_completion(recent_completion, reset_changes) == 18 * 60

        progress = client.get("/api/stats/progress?days=7")
        assert progress.status_code == 200
        payload = progress.json()

    august_9 = next(day for day in payload if day["date"] == "2026-08-09")
    assert august_9["total_minutes"] == 45
    assert august_9["stacks"][0]["minutes"] == 45
    assert august_9["stacks"][0]["count"] == 1
