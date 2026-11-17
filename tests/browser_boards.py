"""Run with python tests/browser_boards.py from the project root."""
import base64
import os
import socket
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlencode

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import app
from silicon import ORG, TEAM, Silicon


@contextmanager
def serve(application):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(application, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(.05)
            assert server.started
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(10)


def provider_app(provider):
    mock = FastAPI()

    @mock.get("/authorize", response_class=HTMLResponse)
    def authorize(request: Request):
        params = parse_qs(request.url.query)
        links = []
        for name in ("mara", "jo", "personal"):
            code = provider.authorize(params, name)
            query = urlencode({"state": params["state"][0], "code": code})
            url = escape(params["redirect_uri"][0] + "?" + query)
            links.append(f'<p><a href="{url}">{name}</a></p>')
        return "".join(links)

    @mock.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT"])
    async def upstream(request: Request, path: str):
        raw = (await request.body()).decode()
        fields = {k: v[0] for k, v in parse_qs(raw).items()}
        if path.startswith(("accounts/", "proofs/", "contacts", "lookup", "photos/", "me")):
            result = provider.service(request.method, str(request.url), headers={"Authorization": request.headers.get("authorization", "")},
                                      json=await request.json() if raw else {}, params=dict(request.query_params))
            return Response(result,media_type="image/png") if isinstance(result, bytes) else result
        return provider.request(request.method, str(request.url), data=fields, auth=("dope","fixture"),
                                headers={"Authorization": request.headers.get("authorization", ""),
                                         "Idempotency-Key": request.headers.get("idempotency-key", "")})
    return mock


def run():
    provider = Silicon()
    provider.names.update(mara="Mara Chen", jo="Jo Ortiz", personal="Mara Chen")
    provider.groups["personal"] = []
    provider.team_admins.add("mara")
    with tempfile.TemporaryDirectory() as temp, serve(provider_app(provider)) as silicon:
        os.environ.update(DOPE_DB_PATH=str(Path(temp) / "dope.db"), DOPE_ACCOUNTS_URL=silicon,
                          DOPE_ORGANIZATIONS_URL=silicon, DOPE_CONTACTS_URL=silicon, DOPE_ACCOUNTS_APP_ID="dope",
                          DOPE_ACCOUNTS_APP_SECRET="local-provider-only")
        os.environ.pop("DOPE_PUBLIC_URL", None)
        with serve(app) as url, sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=os.getenv("CHROMIUM_PATH"), headless=True)
            errors = []
            desktop = browser.new_context(viewport={"width": 1280, "height": 800})
            mobile = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            a, b = desktop.new_page(), mobile.new_page()
            for page, handle in ((a, "mara"), (b, "jo")):
                page.set_default_timeout(7000)
                avatar = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/j1sAAAAASUVORK5CYII=")
                page.route("https://images.example/**", lambda route: route.fulfill(content_type="image/png", body=avatar))
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url)
                page.get_by_role("link", name="Sign in with Silicon Accounts").click()
                page.get_by_role("link", name=handle, exact=True).click()
                expect(page.get_by_role("heading", name="Active Dopes", exact=True)).to_be_visible()
            a.get_by_role("button", name="Design", exact=True).click()
            b.get_by_label("Board", exact=True).select_option(TEAM)
            expect(a.locator("#board-invite")).to_be_visible()
            expect(b.locator("#board-invite")).to_be_hidden()
            task = "Review the onboarding copy"
            a.get_by_role("button", name="New Dope").click()
            a.get_by_role("textbox", name="Dope description").fill(task)
            delayed = []
            def hold(route):
                if route.request.method == "POST":
                    delayed.append(route)
                else:
                    route.continue_()
            a.route("**/api/dopes", hold)
            a.get_by_role("button", name="Create Dope").click()
            pending = a.locator(".dope-card").filter(has_text=task)
            expect(pending).to_be_visible()
            expect(pending).to_be_disabled()
            assert delayed
            delayed.pop().continue_()
            expect(pending).to_be_enabled()
            a.unroute("**/api/dopes", hold)
            b.reload()
            card = b.locator(".dope-card").filter(has_text=task)
            card.click()
            b.get_by_role("button",name="I'll take it").click()
            expect(b.locator("#active-assigned")).to_contain_text("Jo Ortiz")
            a.reload()
            a.locator(".dope-card").filter(has_text=task).click()
            a.get_by_role("textbox",name="Comment",exact=True).fill("Looks ready @jo")
            suggestion = a.locator(".mention-item").first
            expect(suggestion).to_contain_text("Jo Ortiz")
            suggestion.click()
            expect(a.locator("#mention-selected img")).to_have_count(1)
            a.locator("#comment-send").click()
            expect(a.locator(".comment-text")).to_contain_text("@c:jo")
            a.get_by_role("button",name="Completion note…").click()
            a.get_by_label("Completion note (optional)").fill("Reviewed with the team.")
            a.locator("#confirm-complete").click()
            expect(a.locator(".dope-card").filter(has_text=task)).to_have_count(0)
            a.locator('[data-route="completed"]').click()
            a.locator(".dope-card").filter(has_text=task).click()
            expect(a.locator(".completion-text")).to_have_text("Reviewed with the team.")
            a.keyboard.press("Escape")
            b.reload()
            b.locator('[data-route="completed"]').click()
            b.locator(".dope-card").filter(has_text=task).click()
            b.once("dialog", lambda dialog: dialog.accept())
            b.locator("#uncomplete").click()
            b.locator('[data-route="active"]').click()
            b.locator(".dope-card").filter(has_text=task).click()
            b.locator("#complete").click()
            expect(b.locator(".dope-card").filter(has_text=task)).to_have_count(0)
            b.get_by_label("Board", exact=True).select_option(ORG)
            expect(b.locator("#empty")).to_be_visible()
            assert b.evaluate("document.documentElement.scrollWidth <= innerWidth")

            a.locator('[data-route="active"]').click()
            a.locator("#new-dope").click()
            a.get_by_role("textbox",name="Dope description").fill("A team-only draft")
            a.keyboard.press("Escape")
            a.get_by_role("button",name="Personal board",exact=True).click()
            a.locator("#new-dope").click()
            expect(a.get_by_role("textbox",name="Dope description")).to_have_text("")
            # An explicitly rejected optimistic create returns the list to its previous state.
            a.get_by_role("textbox",name="Dope description").fill("Retry this draft")
            a.route("**/api/dopes", hold)
            a.locator("#create-dope").click()
            expect(a.locator(".dope-card").filter(has_text="Retry this draft")).to_be_visible()
            delayed.pop().fulfill(status=409,content_type="application/json",body='{"detail":"Please retry"}')
            expect(a.locator(".dope-card").filter(has_text="Retry this draft")).to_have_count(0)
            a.unroute("**/api/dopes", hold)
            a.locator("#new-dope").click()
            expect(a.get_by_role("textbox",name="Dope description")).to_have_text("Retry this draft")
            a.keyboard.press("Escape")
            a.locator("#board-create").click()
            a.get_by_label("Board name").fill("Private launch")
            a.locator("#workspace-dialog").get_by_role("button",name="Create board").click()
            expect(a.locator('#boards-list [aria-current="true"]')).to_have_text("Private launch")
            a.locator("#board-contacts").click()
            a.get_by_role("button",name="Connect Silicon Contacts").click()
            a.locator('input[name="peer"]').fill("c:jo")
            expect(a.locator(".person-preview")).to_contain_text("Jo Ortiz")
            a.get_by_label("Saved name").fill("Design Jo")
            a.get_by_label("Trust (1–255)").fill("230")
            a.get_by_role("button",name="Save contact").click()
            expect(a.locator(".work-list")).to_contain_text("Design Jo")
            a.keyboard.press("Escape")
            a.locator("#board-invite").click()
            a.locator('input[name="peer"]').fill("dsgn j")
            a.get_by_role("option",name="Design Jo",exact=False).click()
            expect(a.locator(".person-preview img")).to_be_visible()
            a.wait_for_function('document.querySelector(".person-preview img")?.naturalWidth > 0')
            a.get_by_role("button",name="Send invitation").click()
            expect(a.locator(".work-list")).to_contain_text("pending")
            a.keyboard.press("Escape")
            b.locator("#board-inbox").click()
            expect(b.locator(".work-list")).to_contain_text("Private launch")
            b.get_by_role("button",name="Accept",exact=True).click()
            b.get_by_role("button",name="Open board").click()
            expect(b.locator("#board-invite")).to_be_hidden()
            b.locator("#board-files").click()
            b.locator("#upload-file").set_input_files({"name":"brief.txt","mimeType":"text/plain","buffer":b"Shared notes"})
            expect(b.locator(".file-link")).to_have_text("brief.txt")
            b.keyboard.press("Escape")
            a.locator("#board-files").click()
            a.locator("#file-scope").select_option("shared")
            a.locator("#file-contacts").select_option("saved")
            a.locator("#file-trust").fill("230")
            a.locator("#file-trust").press("Tab")
            expect(a.locator(".work-list")).to_contain_text("brief.txt")
            expect(a.locator(".work-list")).to_contain_text("Design Jo")
            a.locator("#workspace-close").click()
            a.get_by_role("button",name="Design",exact=True).click()
            a.locator("#new-dope").click()
            expect(a.get_by_role("textbox",name="Dope description")).to_have_text("A team-only draft")
            a.keyboard.press("Escape")
            a.locator("#profile-open").click()
            a.get_by_role("link",name="Switch account").click()
            a.get_by_role("link",name="personal",exact=True).click()
            expect(a.locator('#boards-list [aria-current="true"]')).to_have_text("Personal board")
            expect(a.get_by_role("button",name="Design",exact=True)).to_have_count(0)
            a.locator("#new-dope").click()
            expect(a.get_by_role("textbox",name="Dope description")).to_have_text("")
            a.keyboard.press("Escape")
            a.locator("#logout").click()
            expect(a.get_by_role("link",name="Sign in with Silicon Accounts")).to_be_visible()
            assert not errors, errors
            browser.close()
    print("Desktop and mobile board workflows passed.")


if __name__ == "__main__":
    run()
