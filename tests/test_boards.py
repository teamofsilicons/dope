from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import accounts
from app.storage import board_path, db, import_legacy, legacy_path
from silicon import ORG, OTHER_TEAM, TEAM, load_main, login


def scoped(board):
    return {"X-Dope-Board": board}


def test_org_team_personal_and_removed_membership(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)
    provider = main.app.state.provider
    provider.groups["bea"] = [ORG]
    provider.groups["personal"] = []
    with TestClient(main.app) as alice, TestClient(main.app) as bea, TestClient(main.app) as personal:
        login(alice, "saket")
        login(bea, "bea")
        login(personal, "personal")
        boards = alice.get("/api/boards").json()["boards"]
        assert [(b["kind"], b["name"]) for b in boards] == [
            ("organization", "Acme"), ("team", "Design"), ("personal", "Personal board")]
        # Even an org admin's visible share targets include teams they haven't joined.
        assert alice.get("/api/board", headers=scoped(OTHER_TEAM)).status_code == 403
        private_id = boards[-1]["id"]
        assert [b["kind"] for b in personal.get("/api/boards").json()["boards"]] == ["personal"]
        assert personal.get("/api/board", headers=scoped(private_id)).status_code == 403
        for board, text in ((ORG, "Company dope"), (TEAM, "Team dope"), (private_id, "Private dope")):
            item = alice.post("/api/dopes", headers=scoped(board), json={"description": text}).json()
            assert item["description"] == text
            assert item["time_minutes"] == 0
        assert [d["title"] for d in bea.get("/api/board").json()["dopes"]] == ["Company dope"]
        assert personal.get("/api/board").json()["dopes"] == []
        for path in ("/api/board", "/api/dopes", "/api/dopes/1", "/api/dopes/1/comments", "/api/users",
                     "/api/categories", "/api/stats/progress", "/api/diagnostics"):
            assert bea.get(path, headers=scoped(TEAM)).status_code == 403
        for action, payload in (("assign", None), ("complete", {}), ("comments", {"body": "hello"})):
            assert bea.post("/api/dopes/1/" + action, headers=scoped(TEAM), json=payload).status_code == 403
        key = alice.post("/api/me/keys", headers=scoped(TEAM), json={"name": "Team key"}).json()["key"]
        key_headers = {"Authorization": "Bearer " + key}
        assert alice.get("/api/board", headers=key_headers).json()["board"]["id"] == TEAM
        assert alice.get("/api/board", headers={**key_headers, **scoped(ORG)}).status_code == 403
        provider.team_name = "Product Design"
        assert alice.get("/api/board", headers=scoped(TEAM)).json()["board"]["name"] == "Product Design"
        assert alice.get("/api/board", headers=scoped(TEAM)).json()["dopes"][0]["title"] == "Team dope"
        provider.groups["saket"] = [ORG]
        assert alice.get("/api/board", headers=key_headers).status_code == 403
        assert alice.post("/api/dopes/1/complete", headers=scoped(TEAM)).status_code == 403
        provider.outage = True
        assert bea.post("/api/dopes", json={"description": "Try later"}).status_code == 503


def test_teammates_claim_release_complete_and_reopen(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)
    with TestClient(main.app) as alice, TestClient(main.app) as bea:
        login(alice, "alice")
        login(bea, "bea")
        text = "Draft <launch> & review\nInvite the team"
        created = alice.post("/api/dopes", headers=scoped(TEAM), json={"description": text}).json()
        assert created["title"] == "Draft <launch> & review"
        assert created["description"] == text
        assert alice.post("/api/dopes", headers=scoped(TEAM), json={"description": "   "}).status_code == 400
        base = "/api/dopes/" + str(created["id"])
        barrier = Barrier(2)

        def take(client):
            barrier.wait()
            return client.post(base + "/assign", headers=scoped(TEAM))

        with ThreadPoolExecutor(2) as pool:
            first, second = list(pool.map(take, [alice, bea]))
        assert sorted([first.status_code, second.status_code]) == [200, 409]
        winner, other = (alice, bea) if first.status_code == 200 else (bea, alice)
        assert len(winner.post(base + "/assign", headers=scoped(TEAM)).json()["assignment_history"]) == 1
        assert other.post(base + "/unassign", headers=scoped(TEAM)).status_code == 403
        assert winner.post(base + "/unassign", headers=scoped(TEAM)).json()["assigned_to"] is None
        assert other.post(base + "/assign", headers=scoped(TEAM)).status_code == 200
        done = winner.post(base + "/complete", headers=scoped(TEAM), json={"description": "Ready for launch"}).json()
        assert (done["status"], done["completion_description"], done["commit_links"]) == ("completed", "Ready for launch", [])
        assert other.post(base + "/complete", headers=scoped(TEAM)).json()["completed_by"] == done["completed_by"]
        assert other.post(base + "/uncomplete", headers=scoped(TEAM)).json()["status"] == "active"
        assert other.post(base + "/complete", headers=scoped(TEAM)).json()["status"] == "completed"


def test_hosted_signin_state_replay_and_stale_browser(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        start = client.get("/api/auth/login", follow_redirects=False)
        params = parse_qs(urlsplit(start.headers["location"]).query)
        assert urlsplit(start.headers["location"]).netloc == "accounts.teamofsilicons.com"
        assert params["scope"] == ["profile"]
        code = main.app.state.provider.authorize(params, "alice")
        def callback(state):
            return client.get(accounts.CALLBACK, params={"code": code, "state": state}, follow_redirects=False)
        assert callback("other").headers["location"] == "/?signin=expired"
        assert client.get("/api/me").status_code == 401
        saved_flow = client.cookies.get(accounts.FLOW_COOKIE)
        ok = callback(params["state"][0])
        assert ok.headers["location"] == "/" and "HttpOnly" in ok.headers["set-cookie"]
        assert ok.headers["cache-control"] == "no-store"
        client.cookies.set(accounts.FLOW_COOKIE, saved_flow, path=accounts.CALLBACK)
        assert callback(params["state"][0]).headers["location"] == "/?signin=expired"
        client.cookies.delete(accounts.FLOW_COOKIE)
        uuid = client.get("/api/boards").json()["account"]["uuid"]
        login(client, "bea")
        assert client.post("/api/dopes", json={"description": "Wrong identity"},
                           headers={**scoped(ORG), "X-Dope-Account": uuid}).status_code == 409
        assert client.post("/api/dopes/1/complete", headers={"Origin": "https://elsewhere.example"}).status_code == 403
        assert client.post("/api/auth/signup", json={"username": "old"}).status_code == 404
        assert client.post("/api/auth/login", json={"username": "old"}).status_code == 405
        assert client.get("/api/board", headers={"Authorization": "Bearer dope_retired"}).status_code == 401
        cookie = client.cookies.get(accounts.COOKIE)
        assert client.post("/api/auth/logout").status_code == 200
        client.cookies.set(accounts.COOKIE, cookie)
        assert client.get("/api/board").status_code == 401


def test_refresh_and_scoped_accounts_bearer(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)
    provider = main.app.state.provider
    with TestClient(main.app) as client:
        login(client, "alice")
        with accounts.session_db() as conn:
            row = conn.execute("SELECT * FROM sessions").fetchone()
            conn.execute("UPDATE sessions SET access_until=0")
        with ThreadPoolExecutor(3) as pool:
            assert list(pool.map(lambda _: client.get("/api/board").status_code, range(3))) == [200, 200, 200]
        assert provider.rotations == [row["refresh_operation"]]
        with accounts.session_db() as conn:
            assert conn.execute("SELECT refresh_operation FROM sessions").fetchone()[0] != row["refresh_operation"]
        client.cookies.clear()
        session = provider.exchange("bea")
        headers = {"Authorization": "Bearer " + session["access_token"]}
        assert client.get("/api/me", headers=headers).json()["username"] == "c:bea"
        source = provider.account
        monkeypatch.setattr(provider, "account", lambda name: {**source(name), "membership_id": "another-app:uuid"})
        assert client.get("/api/board", headers=headers).status_code == 401


def test_refresh_recovers_lost_response_and_expired_successor(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)
    provider = main.app.state.provider
    with TestClient(main.app) as client:
        login(client, "alice")
        with accounts.session_db() as conn:
            conn.execute("UPDATE sessions SET access_until=0")
        receipts, calls = {}, []
        normal = provider.request

        def lose_first_response(method, url, **kwargs):
            if kwargs.get("data", {}).get("grant_type") != "refresh_token":
                return normal(method, url, **kwargs)
            key = kwargs["headers"]["Idempotency-Key"]
            calls.append(key)
            if key in receipts:
                return {**receipts[key], "expires_in": 0}
            result = normal(method, url, **kwargs)
            receipts[key] = result
            if len(calls) == 1:
                raise HTTPException(503, "Response lost after rotation")
            return result

        monkeypatch.setattr(accounts, "upstream", lose_first_response)
        assert client.get("/api/board").status_code == 503
        assert client.get("/api/board").status_code == 200
        assert len(calls) == 3 and calls[0] == calls[1] and calls[2] != calls[0]


def test_legacy_data_requires_an_explicit_destination(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)
    source = legacy_path()
    # A previously deployed database has no Accounts identity fields.
    with db(source) as conn:
        conn.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL, display_name TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE dopes (id INTEGER PRIMARY KEY, title TEXT NOT NULL, description_html TEXT NOT NULL,
                time_minutes INTEGER NOT NULL, created_by INTEGER NOT NULL, created_at TEXT NOT NULL,
                assigned_to INTEGER, assigned_at TEXT, completed_by INTEGER, completed_at TEXT,
                completion_description TEXT, archived_by INTEGER, archived_at TEXT);
            INSERT INTO users VALUES (42, 'c:alice', 'unverified', 'Original Alice', '2025-01-01');
            INSERT INTO dopes (id,title,description_html,time_minutes,created_by,created_at)
                VALUES (43,'Old work','<p>Original</p>',20,42,'2025-01-01');
        """)
    with TestClient(main.app) as client:
        login(client, "alice")
        assert client.get("/api/board").json()["dopes"] == []
        import_legacy(TEAM)
        old = client.get("/api/board", headers=scoped(TEAM)).json()
        assert old["dopes"][0]["creator"] == "Original Alice"
        assert old["dopes"][0]["description"] == "Original"
        assert old["viewer_id"] != 42
        assert client.post("/api/dopes/43/complete", headers=scoped(TEAM)).json()["status"] == "completed"
        with pytest.raises(ValueError):
            import_legacy(TEAM)
        with db(source) as conn:
            assert conn.execute("SELECT completed_at FROM dopes").fetchone()[0] is None
            assert conn.execute("SELECT password_hash FROM users").fetchone()[0] == "unverified"
        with db(board_path(TEAM)) as conn:
            assert conn.execute("SELECT password_hash FROM users WHERE id=42").fetchone()[0] == ""
        assert board_path(TEAM) != board_path(ORG)
