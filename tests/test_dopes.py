from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def load_main(tmp_path, monkeypatch):
    monkeypatch.setenv("DOPE_DB_PATH", str(tmp_path / "dope.db"))
    import app.main as main

    return importlib.reload(main)


def signup_and_login(client):
    client.post(
        "/api/auth/signup",
        json={"username": "shubham", "password": "password", "display_name": "Shubham"},
    )
    login = client.post("/api/auth/login", json={"username": "shubham", "password": "password"})
    assert login.status_code == 200


def test_completed_dope_can_be_marked_not_completed(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        signup_and_login(client)
        created = client.post(
            "/api/dopes",
            json={
                "title": "Undo completion",
                "description_html": "<p>Body</p>",
                "time_text": "30min",
                "dependency_ids": [],
            },
        )
        assert created.status_code == 200
        dope_id = created.json()["id"]

        assigned = client.post(f"/api/dopes/{dope_id}/assign")
        assert assigned.status_code == 200
        completed = client.post(
            f"/api/dopes/{dope_id}/complete",
            json={"completion_text": "done https://github.com/teamofsilicons/dope/commit/abc123"},
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "completed"

        reopened = client.post(f"/api/dopes/{dope_id}/uncomplete")
        assert reopened.status_code == 200
        payload = reopened.json()
        assert payload["status"] == "active"
        assert payload["completed_at"] is None
        assert payload["completed_by"] is None
        assert payload["completion_description"] == ""
        assert payload["commit_links"] == []
        assert payload["assigned_to"]["display_name"] == "Shubham"
        assert payload["assignment_history"][0]["unassigned_at"] is None

        active_ids = {item["id"] for item in client.get("/api/dopes?status=active").json()}
        completed_ids = {item["id"] for item in client.get("/api/dopes?status=completed").json()}
        assert dope_id in active_ids
        assert dope_id not in completed_ids
