from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def load_main(tmp_path, monkeypatch):
    monkeypatch.setenv("DOPE_DB_PATH", str(tmp_path / "dope.db"))
    import app.main as main

    return importlib.reload(main)


def signup_and_login(client):
    signup = client.post(
        "/api/auth/signup",
        json={"username": "shubham", "password": "password", "display_name": "Shubham"},
    )
    assert signup.status_code == 200
    login = client.post(
        "/api/auth/login",
        json={"username": "shubham", "password": "password"},
    )
    assert login.status_code == 200


def create_dope(client, title, time_text):
    response = client.post(
        "/api/dopes",
        json={
            "title": title,
            "description_html": "<p>Body</p>",
            "time_text": time_text,
            "dependency_ids": [],
        },
    )
    assert response.status_code == 200
    return response.json()


def test_save_for_later_has_its_own_queue_and_is_excluded_from_analytics(
    tmp_path,
    monkeypatch,
):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        signup_and_login(client)
        saved = create_dope(client, "Set this aside", "2hr")
        active = create_dope(client, "Keep this active", "30min")
        assert client.post(f"/api/dopes/{saved['id']}/assign").status_code == 200

        response = client.post(f"/api/dopes/{saved['id']}/save-for-later")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "saved"
        assert payload["saved_at"] is not None
        assert payload["saved_by"]["username"] == "shubham"
        assert payload["assigned_to"]["username"] == "shubham"

        active_ids = {item["id"] for item in client.get("/api/dopes?status=active").json()}
        saved_items = client.get("/api/dopes?status=saved").json()
        assert active_ids == {active["id"]}
        assert [item["id"] for item in saved_items] == [saved["id"]]
        assert next(
            item for item in client.get("/api/dopes?status=all").json() if item["id"] == saved["id"]
        )["status"] == "saved"

        diagnostics = client.get("/api/diagnostics").json()
        assert diagnostics["totals"]["total"] == 1
        assert diagnostics["totals"]["active"] == 1
        assert diagnostics["totals"]["active_minutes"] == 30
        person = next(
            item for item in diagnostics["per_person"] if item["user"]["username"] == "shubham"
        )
        assert person["created_count"] == 1
        assert person["in_progress_count"] == 0
        assert saved["id"] not in {event["dope_id"] for event in diagnostics["activity"]}

        assert client.post(f"/api/dopes/{saved['id']}/assign").status_code == 404
        blocked_completion = client.post(
            f"/api/dopes/{saved['id']}/complete",
            json={"completion_text": "done https://github.com/team/repo/commit/abc"},
        )
        assert blocked_completion.status_code == 404

        restored = client.post(f"/api/dopes/{saved['id']}/move-to-active")
        assert restored.status_code == 200
        assert restored.json()["status"] == "active"
        assert restored.json()["saved_at"] is None
        assert restored.json()["saved_by"] is None

        active_ids = {item["id"] for item in client.get("/api/dopes?status=active").json()}
        assert active_ids == {active["id"], saved["id"]}
        assert client.get("/api/dopes?status=saved").json() == []
        diagnostics = client.get("/api/diagnostics").json()
        assert diagnostics["totals"]["total"] == 2
        assert diagnostics["totals"]["active_minutes"] == 150
        assert diagnostics["totals"]["in_progress"] == 1


def test_save_for_later_requires_an_unsaved_active_dope(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        signup_and_login(client)
        dope = create_dope(client, "Only save once", "1hr")

        assert client.post(f"/api/dopes/{dope['id']}/move-to-active").status_code == 404
        assert client.post(f"/api/dopes/{dope['id']}/save-for-later").status_code == 200
        assert client.post(f"/api/dopes/{dope['id']}/save-for-later").status_code == 404
