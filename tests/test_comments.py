from __future__ import annotations

from fastapi.testclient import TestClient

from silicon import load_main, login, org_db


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
        login(client, "shubham", "Shubham")
        dope_id = create_dope(client)

        posted = client.post(f"/api/dopes/{dope_id}/comments", json={"body": "First take on this"})
        assert posted.status_code == 200
        assert posted.json()["user"]["display_name"] == "Shubham"

        # Author sees no unread for their own comment
        mine = next(d for d in client.get("/api/dopes?status=active").json() if d["id"] == dope_id)
        assert mine["comment_count"] == 1
        assert mine["unread_comments"] == 0

        # Second user sees an unread badge
        login(client, "saket", "Saket")
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
        login(client, "shubham")
        back = next(d for d in client.get("/api/dopes?status=active").json() if d["id"] == dope_id)
        assert back["unread_comments"] == 1


def test_mentions_extracted_and_flagged(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        login(client, "saket", "Saket")
        login(client, "shubham", "Shubham S")
        dope_id = create_dope(client)

        posted = client.post(
            f"/api/dopes/{dope_id}/comments",
            json={"body": "hey @saket can you review this? cc @Shubham S"},
        )
        assert posted.status_code == 200
        mentioned = {m["username"] for m in posted.json()["mentions"]}
        assert mentioned == {"c:saket", "c:shubham"}

        # @saketology should not match user saket
        no_match = client.post(f"/api/dopes/{dope_id}/comments", json={"body": "ping @saketology"})
        assert no_match.json()["mentions"] == []

        # Mentioned user sees the mention badge
        login(client, "saket")
        badge = next(d for d in client.get("/api/dopes?status=active").json() if d["id"] == dope_id)
        assert badge["unread_mentions"] == 1
        assert badge["unread_comments"] == 2


def test_only_author_can_delete_comment(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        login(client, "shubham", "Shubham")
        dope_id = create_dope(client)
        comment_id = client.post(f"/api/dopes/{dope_id}/comments", json={"body": "mine"}).json()["id"]

        login(client, "saket", "Saket")
        forbidden = client.delete(f"/api/dopes/{dope_id}/comments/{comment_id}")
        assert forbidden.status_code == 403

        login(client, "shubham")
        assert client.delete(f"/api/dopes/{dope_id}/comments/{comment_id}").status_code == 200
        assert client.get(f"/api/dopes/{dope_id}/comments").json() == []


def test_users_endpoint_reports_presence(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        login(client, "shubham", "Shubham")
        login(client, "idle", "Idle")
        with org_db() as conn:
            conn.execute("UPDATE users SET last_seen_at=NULL WHERE account_handle='c:idle'")
        login(client, "shubham")

        users = client.get("/api/users").json()
        by_username = {u["username"]: u for u in users}
        assert by_username["c:shubham"]["online"] is True
        assert by_username["c:idle"]["online"] is False
        assert set(by_username["c:shubham"]) == {"id", "username", "display_name", "color", "online", "last_seen_at"}


def test_comments_work_over_api_key(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        login(client, "shubham", "Shubham")
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
        assert posted.json()["mentions"][0]["username"] == "c:shubham"

        listed = client.get(f"/api/dopes/{dope_id}/comments", headers=headers)
        assert listed.status_code == 200
        assert len(listed.json()) == 1

        assert client.get("/api/users", headers=headers).status_code == 200
        assert client.post(f"/api/dopes/{dope_id}/comments/read", headers=headers).status_code == 200
