"""In-process Silicon provider for exercising Dopes' normal hosted sign-in and scope checks."""
import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlsplit
from uuid import NAMESPACE_DNS, uuid5

from fastapi import HTTPException

from app import accounts, main
from app.storage import board_path, db

ORG = "organization:acme"
TEAM = "team:acme/design"
OTHER_TEAM = "team:acme/support"


class Silicon:
    def __init__(self):
        self.groups = {}
        self.admins = {"saket", "brainspoof"}
        self.names = {}
        self.codes = {}
        self.tokens = {}
        self.rotations = []
        self.outage = False
        self.team_name = "Design"

    def account(self, handle):
        uuid = str(uuid5(NAMESPACE_DNS, handle))
        return {"uuid": uuid, "sub": uuid, "membership_id": f"dope:{uuid}",
                "id": f"c:{handle}", "display_name": self.names.get(handle, handle.title())}

    def authorize(self, params, handle):
        code = secrets.token_urlsafe(16)
        self.codes[code] = (handle, params["code_challenge"][0], params["redirect_uri"][0])
        return code

    def exchange(self, handle):
        access, refresh = secrets.token_urlsafe(), secrets.token_urlsafe()
        self.tokens[access] = self.tokens[refresh] = handle
        return {"access_token": access, "refresh_token": refresh, "expires_in": 1800, "account": self.account(handle)}

    def request(self, method, url, **kwargs):
        path = urlsplit(url).path
        if path == "/v1/oauth/token":
            data = kwargs["data"]
            assert data["client_id"] == "dope"
            if data["grant_type"] == "refresh_token":
                self.rotations.append(kwargs["headers"]["Idempotency-Key"])
                return self.exchange(self.tokens.pop(data["refresh_token"]))
            handle, challenge, uri = self.codes.pop(data["code"])
            verifier = hashlib.sha256(data["code_verifier"].encode()).digest()
            assert base64.urlsafe_b64encode(verifier).decode().rstrip("=") == challenge
            assert data["redirect_uri"] == uri
            return self.exchange(handle)
        if path == "/v1/oauth/revoke":
            self.tokens.pop(kwargs["data"]["token"], None)
            return {"revoked": True}
        handle = self.tokens.get(kwargs["headers"]["Authorization"].removeprefix("Bearer "))
        if not handle:
            raise HTTPException(401, "Session ended")
        account = self.account(handle)
        if path == "/v1/userinfo":
            return account
        assert path == "/v1/integrations/dope/me"
        if self.outage:
            raise HTTPException(503, "Organizations unavailable")
        groups = self.groups.get(handle, [ORG, TEAM])
        org = {"org_id": "acme", "name": "Acme", "capabilities": {"admin": handle in self.admins}}
        targets = [{"type": "organization", "org_id": "acme", "id": "acme", "name": "Acme"},
                   {"type": "team", "org_id": "acme", "id": "acme/design", "name": self.team_name},
                   {"type": "team", "org_id": "acme", "id": "acme/support", "name": "Support"}]
        return {"account_uuid": account["uuid"], "groups": groups, "organizations": [org] if groups else [],
                "share_targets": targets if groups else []}


def load_main(tmp_path, monkeypatch):
    monkeypatch.setenv("DOPE_DB_PATH", str(tmp_path / "dope.db"))
    monkeypatch.setenv("DOPE_PUBLIC_URL", "http://testserver")
    monkeypatch.setenv("DOPE_ACCOUNTS_APP_ID", "dope")
    monkeypatch.delenv("DOPE_ACCOUNTS_APP_SECRET", raising=False)
    provider = Silicon()
    monkeypatch.setattr(accounts, "upstream", provider.request)
    main.app.state.provider = provider
    return main


def login(client, handle="shubham", display_name=None):
    provider = client.app.state.provider
    if display_name:
        provider.names[handle] = display_name
    response = client.get("/api/auth/login", follow_redirects=False)
    assert response.status_code == 303
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["prompt"] == ["select_account"]
    assert params["code_challenge_method"] == ["S256"]
    code = provider.authorize(params, handle)
    callback = client.get(accounts.CALLBACK, params={"code": code, "state": params["state"][0]}, follow_redirects=False)
    assert callback.status_code == 303 and callback.headers["location"] == "/"
    me = client.get("/api/me")
    assert me.status_code == 200
    return me.json()


def org_db():
    return db(board_path(ORG))
