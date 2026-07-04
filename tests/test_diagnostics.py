from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def load_main(tmp_path, monkeypatch):
    monkeypatch.setenv("DOPE_DB_PATH", str(tmp_path / "dope.db"))
    import app.main as main

    return importlib.reload(main)


def signup_and_login(client, username, display_name):
    client.post(
        "/api/auth/signup",
        json={"username": username, "password": "password", "display_name": display_name},
    )
    login = client.post("/api/auth/login", json={"username": username, "password": "password"})
    assert login.status_code == 200


def create_dope(client, title, time_text="1hr", dependency_ids=None):
    response = client.post(
        "/api/dopes",
        json={
            "title": title,
            "description_html": "<p>Body</p>",
            "time_text": time_text,
            "dependency_ids": dependency_ids or [],
        },
    )
    assert response.status_code == 200
    return response.json()["id"]


def test_diagnostics_totals_per_person_and_activity(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        signup_and_login(client, "shubham", "Shubham")
        signup_and_login(client, "saket", "Saket")

        # saket creates three dopes: one gets completed, one blocks on another
        done_id = create_dope(client, "Ship it", "2hr")
        dep_id = create_dope(client, "Foundation", "30min")
        blocked_id = create_dope(client, "Tower", "1hr", dependency_ids=[dep_id])

        client.post(f"/api/dopes/{done_id}/assign")
        client.post(
            f"/api/dopes/{done_id}/complete",
            json={"completion_text": "done https://github.com/x/y/commit/abc"},
        )
        client.post(f"/api/dopes/{dep_id}/assign")
        client.post(f"/api/dopes/{blocked_id}/comments", json={"body": "waiting on @shubham"})

        d = client.get("/api/diagnostics").json()

        t = d["totals"]
        assert t["total"] == 3
        assert t["completed"] == 1
        assert t["completed_minutes"] == 120
        assert t["active"] == 2
        assert t["active_minutes"] == 90
        assert t["blocked"] == 1
        assert t["blocked_minutes"] == 60
        assert t["in_progress"] == 1
        assert t["in_progress_minutes"] == 30
        assert t["ready"] == 0

        saket = next(p for p in d["per_person"] if p["user"]["username"] == "saket")
        assert saket["completed_count"] == 1
        assert saket["completed_minutes"] == 120
        assert saket["completed_7d_count"] == 1
        assert saket["in_progress_count"] == 1
        assert saket["in_progress_minutes"] == 30
        assert saket["created_count"] == 3
        assert saket["comments_count"] == 1
        assert saket["online"] is True

        shubham = next(p for p in d["per_person"] if p["user"]["username"] == "shubham")
        assert shubham["completed_count"] == 0

        # newest first, all event types present
        stamps = [e["at"] for e in d["activity"]]
        assert stamps == sorted(stamps, reverse=True)
        kinds = {e["type"] for e in d["activity"]}
        assert {"created", "assigned", "completed", "commented"} <= kinds
        completed_event = next(e for e in d["activity"] if e["type"] == "completed")
        assert completed_event["dope_title"] == "Ship it"
        assert completed_event["user"]["username"] == "saket"

        uncategorized = d["remaining_by_category"][0]
        assert uncategorized["category"] is None
        assert uncategorized["count"] == 2
        assert uncategorized["minutes"] == 90


def test_diagnostics_unassign_reason_in_activity(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        signup_and_login(client, "saket", "Saket")
        dope_id = create_dope(client, "Flaky one")
        client.post(f"/api/dopes/{dope_id}/assign")
        client.post(f"/api/dopes/{dope_id}/unassign", json={"reason": "blocked on infra"})

        activity = client.get("/api/diagnostics").json()["activity"]
        unassigned = next(e for e in activity if e["type"] == "unassigned")
        assert unassigned["detail"] == "blocked on infra"

        # completion should not double as an unassign event
        client.post(f"/api/dopes/{dope_id}/assign")
        client.post(
            f"/api/dopes/{dope_id}/complete",
            json={"completion_text": "ok https://github.com/x/y/commit/abc"},
        )
        activity = client.get("/api/diagnostics").json()["activity"]
        assert sum(1 for e in activity if e["type"] == "unassigned") == 1


def test_diagnostics_over_api_key_and_limit(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        signup_and_login(client, "saket", "Saket")
        for i in range(5):
            create_dope(client, f"Dope {i}")
        key = client.post("/api/me/keys", json={"name": "Agent"}).json()["key"]
        client.cookies.clear()
        headers = {"Authorization": f"Bearer {key}"}

        assert client.get("/api/diagnostics").status_code == 401
        d = client.get("/api/diagnostics?limit=3", headers=headers)
        assert d.status_code == 200
        assert len(d.json()["activity"]) == 3
