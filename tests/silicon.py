"""In-process Silicon provider for exercising Dopes' normal hosted sign-in and scope checks."""
import base64
import hashlib
import secrets
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import NAMESPACE_DNS, uuid5

from fastapi import HTTPException

from app import accounts, main, people
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
        self.team_admins = set()
        self.contacts = {}
        self.proofs = {}
        self.directory_names = {"mara", "jo", "personal", "saket", "bea", "si:scout"}

    def account(self, handle):
        self.directory_names.add(handle)
        uuid = str(uuid5(NAMESPACE_DNS, handle))
        return {"uuid": uuid, "sub": uuid, "membership_id": f"dope:{uuid}",
                "id": handle if ":" in handle else f"c:{handle}", "display_name": self.names.get(handle, handle.title()),
                "pfp_url": f"https://images.example/{uuid}.png"}

    def authorize(self, params, handle):
        code = secrets.token_urlsafe(16)
        self.codes[code] = (handle, params["code_challenge"][0], params["redirect_uri"][0])
        return code

    def exchange(self, handle):
        access, refresh = secrets.token_urlsafe(), "refresh." + secrets.token_urlsafe()
        self.tokens[access] = self.tokens[refresh] = handle
        return {"access_token": access, "refresh_token": refresh, "expires_in": 1800, "account": self.account(handle)}

    def request(self, method, url, **kwargs):
        path = urlsplit(url).path
        if path == "/v1/oauth/token":
            data = kwargs["data"]
            assert data.get("client_id") == "dope" or kwargs.get("auth", ("",))[0] == "dope"
            if data["grant_type"] == "refresh_token":
                self.rotations.append(kwargs["headers"]["Idempotency-Key"])
                return self.exchange(self.tokens.pop(data["refresh_token"]))
            handle, challenge, uri = self.codes.pop(data["code"])
            verifier = hashlib.sha256(data["code_verifier"].encode()).digest()
            assert base64.urlsafe_b64encode(verifier).decode().rstrip("=") == challenge
            assert data["redirect_uri"] == uri
            return self.exchange(handle)
        if path == "/v1/oauth/introspect":
            token = kwargs["data"]["token"]
            if token not in self.tokens or token.startswith("refresh."):
                return {"active": False}
            return {"active": True, "token_type": "access_token", "client_id": "dope", "aud": "dope",
                    "sub": self.account(self.tokens[token])["uuid"]}
        if path == "/v1/oauth/revoke":
            self.tokens.pop(kwargs["data"]["token"], None)
            return {"revoked": True}
        handle = self.tokens.get(kwargs["headers"]["Authorization"].removeprefix("Bearer "))
        if not handle:
            raise HTTPException(401, "Session ended")
        account = self.account(handle)
        if path == "/v1/userinfo":
            return account
        if path == "/v1/integrations/dope/orgs/acme":
            roster = [who for who in sorted(self.directory_names) if ORG in self.groups.get(who, [ORG, TEAM])]
            return {"members": [self.account(who) for who in roster], "teams": [{"id": "design", "members": [
                self.account(who)["uuid"] for who in roster if TEAM in self.groups.get(who, [ORG, TEAM])]}]}
        assert path == "/v1/integrations/dope/me"
        if self.outage:
            raise HTTPException(503, "Organizations unavailable")
        groups = self.groups.get(handle, [ORG, TEAM])
        org = {"org_id": "acme", "name": "Acme", "capabilities": {"admin": handle in self.admins, "team_admins": ["design"] if handle in self.team_admins else []}}
        targets = [{"type": "organization", "org_id": "acme", "id": "acme", "name": "Acme"},
                   {"type": "team", "org_id": "acme", "id": "acme/design", "name": self.team_name},
                   {"type": "team", "org_id": "acme", "id": "acme/support", "name": "Support"}]
        return {"account_uuid": account["uuid"], "groups": groups, "organizations": [org] if groups else [],
                "share_targets": targets if groups else []}

    def directory(self, value):
        for handle in sorted(self.directory_names):
            record = self.account(handle)
            if value in (record["uuid"], record["id"]):
                return record
        raise HTTPException(404, "Account not found")

    def service(self, method, url, **options):
        path = unquote(urlsplit(url).path)
        body = options.get("json", {})
        if path == "/v1/proofs/user-verification":
            assert body["receiving_app"] == "silicon-contacts"
            token = secrets.token_urlsafe()
            self.proofs[token] = (self.tokens[body["subject_token"]], body["scopes"][0])
            return {"proof_token": token, "proof_id": token}
        if path == "/v1/proofs/revoke":
            self.proofs.pop(body["proof_id"], None)
            return {}
        if path.startswith("/v1/accounts/"):
            return self.directory(path.rsplit("/", 1)[1])
        token = options["headers"]["Authorization"].removeprefix("Proof ")
        owner, scope = self.proofs[token]
        book = self.contacts.setdefault(owner, {})
        if method == "GET":
            assert scope == "contacts.read"
            if path == "/v1/me":
                return self.account(owner)
            if path == "/v1/lookup":
                return self.directory(options["params"]["q"])
            if path == "/v1/contacts":
                offset = int(options["params"]["offset"])
                return {"items": list(book.values())[offset:offset + 200], "total": len(book)}
            if path.startswith("/v1/photos/"):
                return bytes.fromhex("89504e470d0a1a0a") + bytes(path, "ascii")
            if path.startswith("/v1/contacts/"):
                key = path.rsplit("/", 1)[1]
                if key not in book:
                    raise HTTPException(404, "Not saved")
                return book[key]
        assert scope == "contacts.write" and method in ("PUT", "POST")
        peer = self.directory(body["id"])
        record = {**body, "account": peer, "uuid": peer["uuid"], "blocked": False}
        book[peer["uuid"]] = record
        return record


def load_main(tmp_path, monkeypatch):
    monkeypatch.setenv("DOPE_DB_PATH", str(tmp_path / "dope.db"))
    monkeypatch.setenv("DOPE_PUBLIC_URL", "http://testserver")
    monkeypatch.setenv("DOPE_ACCOUNTS_APP_ID", "dope")
    monkeypatch.setenv("DOPE_ACCOUNTS_APP_SECRET", "local-provider-only")
    provider = Silicon()
    monkeypatch.setattr(accounts, "upstream", provider.request)
    monkeypatch.setattr(people, "remote", provider.service)
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
