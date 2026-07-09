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


def signup(client, username, display_name):
    response = client.post(
        "/api/auth/signup",
        json={"username": username, "password": "password", "display_name": display_name},
    )
    assert response.status_code == 200


def login(client, username):
    response = client.post("/api/auth/login", json={"username": username, "password": "password"})
    assert response.status_code == 200


def test_completed_dope_can_be_marked_not_completed(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        signup(client, "shubham", "Shubham")
        signup(client, "saket", "Saket")
        signup(client, "brainspoof", "Brainspoof")
        login(client, "shubham")
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


def test_dope_can_be_sent_for_review_and_approved(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        signup(client, "shubham", "Shubham")
        signup(client, "saket", "Saket")
        login(client, "shubham")
        created = client.post(
            "/api/dopes",
            json={
                "title": "Reviewable work",
                "description_html": "<p>Body</p>",
                "time_text": "45min",
                "dependency_ids": [],
            },
        ).json()
        dope_id = created["id"]
        assert client.post(f"/api/dopes/{dope_id}/assign").status_code == 200

        missing_branch = client.post(
            f"/api/dopes/{dope_id}/review",
            json={"note": "Please review", "branch_url": "feature/reviewable-work"},
        )
        assert missing_branch.status_code == 400

        reviewed = client.post(
            f"/api/dopes/{dope_id}/review",
            json={"note": "Please review the edge cases", "branch_url": "https://github.com/team/repo/tree/reviewable-work"},
        )
        assert reviewed.status_code == 200
        payload = reviewed.json()
        assert payload["status"] == "review"
        assert payload["completed_by"]["display_name"] == "Shubham"
        assert payload["review"]["branch_url"] == "https://github.com/team/repo/tree/reviewable-work"
        assert payload["assigned_to"] is None

        completed = client.get("/api/dopes?status=completed").json()
        assert completed[0]["id"] == dope_id
        assert completed[0]["status"] == "review"
        assert client.get("/api/dopes?status=review").status_code == 403

        progress = client.get("/api/stats/progress?days=7").json()
        today = main.current_dope_day().isoformat()
        today_bucket = next(day for day in progress if day["date"] == today)
        assert today_bucket["total_minutes"] == 45
        assert today_bucket["stacks"][0]["display_name"] == "Shubham"

        blocked_approve = client.post(f"/api/dopes/{dope_id}/review/approve")
        assert blocked_approve.status_code == 403

        client.post("/api/auth/logout")
        login(client, "saket")
        review_queue = client.get("/api/dopes?status=review")
        assert review_queue.status_code == 200
        assert review_queue.json()[0]["id"] == dope_id

        approved = client.post(f"/api/dopes/{dope_id}/review/approve")
        assert approved.status_code == 200
        approved_payload = approved.json()
        assert approved_payload["status"] == "completed"
        assert approved_payload["review"]["approved_by"]["display_name"] == "Saket"


def test_rejected_review_creates_top_assigned_followup(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        signup(client, "shubham", "Shubham")
        signup(client, "saket", "Saket")
        signup(client, "brainspoof", "Brainspoof")
        login(client, "shubham")
        ordinary = client.post(
            "/api/dopes",
            json={
                "title": "Ordinary active work",
                "description_html": "<p>Body</p>",
                "time_text": "30min",
                "dependency_ids": [],
            },
        ).json()
        reviewable = client.post(
            "/api/dopes",
            json={
                "title": "Needs reviewer",
                "description_html": "<p>Body</p>",
                "time_text": "1hr",
                "dependency_ids": [],
            },
        ).json()
        dope_id = reviewable["id"]
        assert client.post(f"/api/dopes/{dope_id}/assign").status_code == 200
        assert client.post(
            f"/api/dopes/{dope_id}/review",
            json={"note": "Ready for review", "branch_url": "https://github.com/team/repo/tree/review-me"},
        ).status_code == 200
        blocked_reject = client.post(
            f"/api/dopes/{dope_id}/review/reject",
            json={"note": "Not enough", "time_text": "20min"},
        )
        assert blocked_reject.status_code == 403

        client.post("/api/auth/logout")
        login(client, "brainspoof")
        brainspoof_queue = client.get("/api/dopes?status=review")
        assert brainspoof_queue.status_code == 200
        assert brainspoof_queue.json()[0]["id"] == dope_id

        client.post("/api/auth/logout")
        login(client, "saket")
        rejected = client.post(
            f"/api/dopes/{dope_id}/review/reject",
            json={"note": "Tighten empty state and add tests", "time_text": "40min"},
        )
        assert rejected.status_code == 200
        body = rejected.json()
        followup = body["followup"]
        assert followup["status"] == "active"
        assert followup["time_minutes"] == 40
        assert followup["assigned_to"]["display_name"] == "Shubham"
        assert followup["review"]["parent_id"] == dope_id
        assert followup["review"]["priority"] == 1
        assert followup["dependencies"][0]["id"] == dope_id

        reviewed = body["reviewed"]
        assert reviewed["status"] == "completed"
        assert reviewed["review"]["rejected_by"]["display_name"] == "Saket"
        assert reviewed["review"]["rejection_note"] == "Tighten empty state and add tests"
        assert reviewed["review"]["followup_id"] == followup["id"]

        active = client.get("/api/dopes?status=active").json()
        assert active[0]["id"] == followup["id"]
        assert ordinary["id"] in {item["id"] for item in active}
