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


def create_dope(client, title="Comment target"):
    response = client.post(
        "/api/dopes",
        json={"title": title, "description_html": "<p>Body</p>", "time_text": "30min", "dependency_ids": []},
    )
    assert response.status_code == 200
    return response.json()["id"]


def test_comment_back_and_forth_with_unread_badges(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        signup_and_login(client, "shubham", "Shubham")
        dope_id = create_dope(client)

        posted = client.post(f"/api/dopes/{dope_id}/comments", json={"body": "First take on this"})
        assert posted.status_code == 200
        assert posted.json()["user"]["display_name"] == "Shubham"

        # Author sees no unread for their own comment
        mine = next(d for d in client.get("/api/dopes?status=active").json() if d["id"] == dope_id)
        assert mine["comment_count"] == 1
        assert mine["unread_comments"] == 0

        # Second user sees an unread badge
        signup_and_login(client, "saket", "Saket")
        theirs = next(d for d in client.get("/api/dopes?status=active").json() if d["id"] == dope_id)
        assert theirs["comment_count"] == 1
        assert theirs["unread_comments"] == 1

        # Reading clears the badge
        assert client.post(f"/api/dopes/{dope_id}/comments/read").status_code == 200
        cleared = next(d for d in client.get("/api/dopes?status=active").json() if d["id"] == dope_id)
        assert cleared["unread_comments"] == 0

        # Replying keeps the thread ordered
        client.post(f"/api/dopes/{dope_id}/comments", json={"body": "Replying back"})
        thread = client.get(f"/api/dopes/{dope_id}/comments").json()
        assert [c["body"] for c in thread] == ["First take on this", "Replying back"]

        # And the first user now sees the reply as unread
        client.post("/api/auth/login", json={"username": "shubham", "password": "password"})
        back = next(d for d in client.get("/api/dopes?status=active").json() if d["id"] == dope_id)
        assert back["unread_comments"] == 1


def test_mentions_extracted_and_flagged(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        signup_and_login(client, "saket", "Saket")
        signup_and_login(client, "shubham", "Shubham S")
        dope_id = create_dope(client)

        posted = client.post(
            f"/api/dopes/{dope_id}/comments",
            json={"body": "hey @saket can you review this? cc @Shubham S"},
        )
        assert posted.status_code == 200
        mentioned = {m["username"] for m in posted.json()["mentions"]}
        assert mentioned == {"saket", "shubham"}

        # @saketology should not match user saket
        no_match = client.post(f"/api/dopes/{dope_id}/comments", json={"body": "ping @saketology"})
        assert no_match.json()["mentions"] == []

        # Mentioned user sees the mention badge
        client.post("/api/auth/login", json={"username": "saket", "password": "password"})
        badge = next(d for d in client.get("/api/dopes?status=active").json() if d["id"] == dope_id)
        assert badge["unread_mentions"] == 1
        assert badge["unread_comments"] == 2


def test_only_author_can_delete_comment(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        signup_and_login(client, "shubham", "Shubham")
        dope_id = create_dope(client)
        comment_id = client.post(f"/api/dopes/{dope_id}/comments", json={"body": "mine"}).json()["id"]

        signup_and_login(client, "saket", "Saket")
        forbidden = client.delete(f"/api/dopes/{dope_id}/comments/{comment_id}")
        assert forbidden.status_code == 403

        client.post("/api/auth/login", json={"username": "shubham", "password": "password"})
        assert client.delete(f"/api/dopes/{dope_id}/comments/{comment_id}").status_code == 200
        assert client.get(f"/api/dopes/{dope_id}/comments").json() == []


def test_users_endpoint_reports_presence(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        signup_and_login(client, "shubham", "Shubham")
        client.post("/api/auth/signup", json={"username": "idle", "password": "password", "display_name": "Idle"})

        users = client.get("/api/users").json()
        by_username = {u["username"]: u for u in users}
        assert by_username["shubham"]["online"] is True
        assert by_username["idle"]["online"] is False
        assert set(by_username["shubham"]) == {"id", "username", "display_name", "color", "online", "last_seen_at"}


def test_comments_work_over_api_key(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        signup_and_login(client, "shubham", "Shubham")
        dope_id = create_dope(client)
        key = client.post("/api/me/keys", json={"name": "Agent"}).json()["key"]
        headers = {"Authorization": f"Bearer {key}"}
        client.cookies.clear()

        posted = client.post(
            f"/api/dopes/{dope_id}/comments",
            headers=headers,
            json={"body": "Posted through the API @shubham"},
        )
        assert posted.status_code == 200
        assert posted.json()["mentions"][0]["username"] == "shubham"

        listed = client.get(f"/api/dopes/{dope_id}/comments", headers=headers)
        assert listed.status_code == 200
        assert len(listed.json()) == 1

        assert client.get("/api/users", headers=headers).status_code == 200
        assert client.post(f"/api/dopes/{dope_id}/comments/read", headers=headers).status_code == 200
