from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from silicon import ORG, TEAM, load_main, login


def scope(board, key=None):
    return {"X-Dope-Board": board, **({"Idempotency-Key": key} if key else {})}


def test_board_creation_and_team_authority(tmp_path, monkeypatch):
    app = load_main(tmp_path, monkeypatch).app
    p = app.state.provider
    p.team_admins.add("jo")
    with TestClient(app) as admin, TestClient(app) as lead, TestClient(app) as member:
        login(admin, "saket"); login(lead, "jo"); login(member, "bea")
        def create(client, parent, name):
            return client.post("/api/boards", json={"scope_id": parent, "name": name}, headers={"Idempotency-Key": name})
        org = create(admin, ORG, "Company work").json()["id"]
        team = create(lead, TEAM, "Design backlog").json()["id"]
        assert create(lead, TEAM, "Design backlog").json()["id"] == team
        assert create(lead, ORG, "Denied").status_code == 403
        assert create(member, TEAM, "Denied").status_code == 403
        assert create(admin, team, "Nested").status_code == 422
        assert member.get("/api/workspace", headers=scope(team)).json()["board"]["can_manage"] is False
        p.groups["personal"] = []
        path = "/api/boards/" + team + "/invitations"
        assert lead.post(path, json={"recipient":"c:personal"}).status_code == 403
        assert member.post(path, json={"recipient":"c:jo"}).status_code == 403
        invite = lead.post(path, json={"recipient":"c:bea"}).json()
        assert member.post("/api/invitations/" + invite["id"] + "/accept").status_code == 200
        p.groups["bea"] = [ORG]
        assert member.get("/api/invitations").json()["items"] == []
        assert member.get("/api/dopes", headers=scope(team)).status_code == 403
        assert member.get("/api/workspace", headers=scope(org)).status_code == 200


def test_private_guest_lifecycle_files_contacts_and_ranking(tmp_path, monkeypatch):
    app = load_main(tmp_path, monkeypatch).app
    p = app.state.provider
    with TestClient(app) as a, TestClient(app) as b, TestClient(app) as robot:
        login(a,"mara"); login(b,"jo"); login(robot,"si:scout")
        person = lambda c: c.get("/api/boards").json()["account"]["uuid"]
        alice, bob, bot = person(a), person(b), person(robot)
        private = "personal:" + alice
        sibling = a.post("/api/boards", json={"scope_id":private, "name":"Another list"}).json()["id"]
        board = a.post("/api/boards", json={"scope_id":private, "name":"Design notes"}).json()["id"]
        url = "/api/boards/" + board + "/invitations"
        def invite(c, peer, url=url, key=""):
            return c.post(url, json={"recipient":peer}, headers={"Idempotency-Key":key} if key else {})
        with ThreadPoolExecutor(2) as workers:
            ids = [r.json()["id"] for r in workers.map(lambda _: invite(a,"c:jo",key="same-send"), range(2))]
        assert ids[0] == ids[1]
        assert invite(a, bot, key="same-send").status_code == 409
        assert a.get(url).json()["items"][0]["recipient"]["id"] == "c:jo"
        assert b.get("/api/board", headers=scope(board)).status_code == 403
        inbox = b.get("/api/invitations").json()["items"]
        assert inbox[0]["board_name"] == "Design notes" and inbox[0]["sender"]["uuid"] == alice
        action = "/api/invitations/" + ids[0]
        assert robot.post(action + "/accept").status_code == 404
        assert b.post(action + "/accept").json()["state"] == "accepted"
        assert b.post(action + "/accept").status_code == 200
        assert b.get("/api/board", headers=scope(sibling)).status_code == 403
        assert b.get("/api/board", headers=scope(private)).status_code == 403
        assert b.get("/api/workspace", headers=scope(board)).json()["board"]["shared_by"] == alice
        assert invite(b,bot).status_code == 403
        item = b.post("/api/dopes", headers=scope(board), json={"description":"Review this"}).json()
        assert a.post(f"/api/dopes/{item['id']}/complete", json={}, headers=scope(board)).json()["status"] == "completed"

        def upload(client, name, contents=b"example"):
            return client.post("/api/files", params={"name":name}, headers=scope(board,"upload"), content=contents)
        file = upload(b, "spec résumé.txt").json()
        assert upload(b,"spec résumé.txt").json()["id"] == file["id"]
        assert upload(b,"different").status_code == 409
        assert a.get(file["download_url"]).content == b"example"
        assert robot.get(file["download_url"]).status_code == 404
        assert b.get("/api/files", params={"shared_with_me":True}).json()["total"] == 0
        assert a.get("/api/files", params={"shared_with_me":True,"contacts":"unsaved"}).json()["total"] == 1
        assert a.put("/api/contacts", json={"id":"c:jo","name":"Jo on Design","trust":250}).status_code == 403
        assert a.put("/api/contacts/connection",json={"enabled":True}).status_code == 200
        def save(uid, name, trust):
            return a.put("/api/contacts", json={"id":uid,"name":name,"trust":trust})
        save(bot,"Scout agent",255)
        assert save(bob,"Jo on Design",200).json()["display_name"] == "Jo on Design"
        photo = "bc48f2b4-95c8-439e-aca0-913502cf62eb"
        p.contacts["mara"][bob].update(photo_id=photo, note="Preserve this")
        saved = save(bob,"Design partner",250).json()
        assert saved["pfp_url"] == "/api/contacts/photos/" + photo and a.get(saved["pfp_url"]).status_code == 200
        assert p.contacts["mara"][bob]["note"] == "Preserve this"
        assert a.get("/api/people/resolve",params={"id":"c:jo"}).json()["display_name"] == "Design partner"
        assert [r["uuid"] for r in a.get("/api/contacts").json()["items"]] == [bot,bob]
        assert b.get("/api/contacts").json()["items"] == []
        trust_query = {"shared_with_me":True,"contacts":"saved","min_trust":250}
        data = a.get("/api/files",params=trust_query).json()
        assert data["total"] == 1 and data["items"][0]["uploader"]["display_name"] == "Design partner"
        assert a.get("/api/files",params={**trust_query,"min_trust":251}).json()["total"] == 0
        assert a.get("/api/people",params={"q":"dsgn prtnr"}).json()["items"][0]["uuid"] == bob
        assert a.get("/api/people/resolve",params={"id":"si:scout"}).json()["id"] == "si:scout"
        invite(b,alice,"/api/boards/personal:" + bob + "/invitations")
        invite(robot,alice,"/api/boards/personal:" + bot + "/invitations")
        ranked = a.get("/api/people").json()["items"]
        assert [(r["uuid"],r["sent"],r["received"],r["score"]) for r in ranked] == [(bob,1,1,3),(bot,0,1,1)]
        assert b.get("/api/invitations").json()["items"][0]["sender"]["display_name"] == "Mara"
        key = b.post("/api/me/keys",headers=scope(board),json={"name":"Design"}).json()["key"]
        headers = {"Authorization":"Bearer " + key}
        assert b.get("/api/invitations",headers=headers).status_code == 403
        assert b.get("/api/files",params={"min_trust":1},headers=headers).status_code == 403
        assert a.post(action + "/revoke").status_code == 200
        assert b.get("/api/files",headers=headers).status_code == 403
        assert b.get(file["download_url"]).status_code == 404
        assert b.get("/api/dopes",headers=scope(board)).status_code == 403
        assert b.get("/api/invitations").json()["items"] == []
        assert a.put("/api/contacts/connection",json={"enabled":False}).json()["connected"] is False
        assert not p.proofs and not a.get("/api/contacts").json()["items"]


def test_interface_bearer_and_schema(tmp_path, monkeypatch):
    app = load_main(tmp_path,monkeypatch).app
    p = app.state.provider
    tokens = p.exchange("mara")
    with TestClient(app) as client:
        headers = {"Authorization":"Bearer " + tokens["access_token"], "Origin":"https://interface.example"}
        reply = client.post("/api/dopes", json={"description":"From an Interface"}, headers=headers)
        assert reply.status_code == 200 and reply.headers["access-control-allow-origin"] == "*"
        assert client.get("/api/boards",headers={"Authorization":"Bearer " + tokens["refresh_token"]}).status_code == 401
        assert client.get("/api/me/keys",headers=headers).status_code == 403
        preflight = client.options("/api/dopes",headers={"Origin":headers["Origin"],"Access-Control-Request-Method":"POST"})
        assert preflight.status_code == 204 and "Authorization" in preflight.headers["access-control-allow-headers"]
        schema = client.get("/openapi.json").json()
        assert {"SiliconAccess":[]} in schema["paths"]["/api/dopes"]["post"]["security"]
        assert {"BoardKey":[]} not in schema["paths"]["/api/invitations"]["get"]["security"]
        assert schema["paths"]["/api/files"]["post"]["requestBody"]["content"]["application/octet-stream"]


def test_retryable_content_and_guest_members(tmp_path, monkeypatch):
    app = load_main(tmp_path, monkeypatch).app
    with TestClient(app) as owner, TestClient(app) as guest:
        login(owner, "mara"); login(guest, "jo")
        uid = owner.get("/api/boards").json()["account"]["uuid"]
        board = "personal:" + uid
        headers = scope(board, "description-once")
        create = lambda data: owner.post("/api/dopes", headers=headers, json={"description":data})
        with ThreadPoolExecutor(2) as workers:
            first, second = workers.map(create, ["Check", "Check"])
        assert first.json()["id"] == second.json()["id"]
        assert create("Different").status_code == 409
        invitation = owner.post("/api/boards/" + board + "/invitations",json={"recipient":"c:jo"}).json()["id"]
        guest.post("/api/invitations/" + invitation + "/accept")
        members = owner.get("/api/people",params={"board_id":board,"q":"jo","members_only":True}).json()["items"]
        assert len(members) == 1 and members[0]["local_id"] and members[0]["pfp_url"]
        dope = "/api/dopes/" + str(first.json()["id"])
        h = scope(board,"comment-once")
        send = lambda: owner.post(dope + "/comments",headers=h,json={"body":"Ready @c:jo"})
        comment = send().json()
        assert comment["id"] == send().json()["id"] and comment["mentions"][0]["id"] == members[0]["local_id"]
        assert owner.delete(dope + "/comments/" + str(comment["id"]),headers=scope(board)).status_code == 200
        assert send().status_code == 409
        owner.post("/api/invitations/" + invitation + "/revoke")
        assert owner.get("/api/people",params={"board_id":board,"q":"jo","members_only":True}).json()["items"] == []
        assert owner.post(dope + "/complete",headers=scope(board),json={"completed_for_user_id":members[0]["local_id"]}).status_code == 400


def test_suggestion_tiebreaks_and_fuzzy_filter(tmp_path, monkeypatch):
    import itertools
    from app import sharing
    app = load_main(tmp_path,monkeypatch).app
    provider = app.state.provider
    provider.directory_names.update({"ash","becca","cory","si:robot"})
    sequence = itertools.count()
    monkeypatch.setattr(sharing,"timestamp",lambda: f"2026-01-01T00:00:{next(sequence):02d}+00:00")
    with TestClient(app) as owner:
        login(owner,"mara")
        board = "personal:" + owner.get("/api/boards").json()["account"]["uuid"]
        owner.put("/api/contacts/connection",json={"enabled":True})
        for handle,name,trust in (("c:ash","Ash",250),("c:becca","Becca",1),("si:robot","Robot",200),("c:cory","Cory",100)):
            assert owner.put("/api/contacts",json={"id":handle,"name":name,"trust":trust}).status_code == 200
        for handle in ("c:ash","c:becca"):
            assert owner.post("/api/boards/" + board + "/invitations",json={"recipient":handle}).status_code == 201
        items = owner.get("/api/people").json()["items"]
        assert [p["id"] for p in items] == ["c:becca","c:ash","si:robot","c:cory"]
        assert owner.get("/api/people",params={"q":"rbt"}).json()["items"][0]["id"] == "si:robot"


def test_published_schema_matches_runtime(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    app = load_main(tmp_path,monkeypatch).app
    with TestClient(app) as client:
        assert client.get("/openapi.json").json() == json.loads((Path(__file__).resolve().parents[1]/"docs/openapi.json").read_text())
        assert client.get("/interface-api").status_code == 200
