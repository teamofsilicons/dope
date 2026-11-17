"""Run with python tests/browser_boards.py from the project root."""
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
from fastapi.responses import HTMLResponse
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

    @mock.api_route("/v1/{path:path}", methods=["GET", "POST"])
    async def upstream(request: Request, path: str):
        raw = (await request.body()).decode()
        fields = {k: v[0] for k, v in parse_qs(raw).items()}
        return provider.request(request.method, str(request.url), data=fields,
                                headers={"Authorization": request.headers.get("authorization", ""),
                                         "Idempotency-Key": request.headers.get("idempotency-key", "")})
    return mock


def run():
    provider = Silicon()
    provider.names.update(mara="Mara Chen", jo="Jo Ortiz", personal="Mara Chen")
    provider.groups["personal"] = []
    with tempfile.TemporaryDirectory() as temp, serve(provider_app(provider)) as silicon:
        os.environ.update(DOPE_DB_PATH=str(Path(temp) / "dope.db"), DOPE_ACCOUNTS_URL=silicon,
                          DOPE_ORGANIZATIONS_URL=silicon, DOPE_ACCOUNTS_APP_ID="dope")
        os.environ.pop("DOPE_ACCOUNTS_APP_SECRET", None)
        os.environ.pop("DOPE_PUBLIC_URL", None)
        with serve(app) as url, sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=os.getenv("CHROMIUM_PATH"), headless=True)
            errors = []
            desktop = browser.new_context(viewport={"width": 1280, "height": 800})
            mobile = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            a, b = desktop.new_page(), mobile.new_page()
            for page, handle in ((a, "mara"), (b, "jo")):
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url)
                page.get_by_role("link", name="Sign in with Silicon Accounts").click()
                page.get_by_role("link", name=handle, exact=True).click()
                expect(page.get_by_role("heading", name="Acme")).to_be_visible()
            a.get_by_role("button", name="Design", exact=True).click()
            b.get_by_label("Board", exact=True).select_option(TEAM)
            task = "Review the onboarding copy"
            a.get_by_label("Dope description").fill(task)
            a.get_by_role("button", name="Add dope").click()
            expect(a.get_by_role("button", name=task, exact=True)).to_be_visible()
            b.reload()
            b.get_by_role("button", name="Take", exact=True).click()
            expect(b.get_by_role("button", name="Taken by Jo Ortiz, view dope")).to_be_visible()
            a.reload()
            a.get_by_role("button", name=task, exact=True).click()
            a.get_by_text("Comments", exact=True).click()
            a.get_by_role("textbox", name="Comment", exact=True).fill("Looks ready to ship.")
            a.get_by_role("button", name="Post comment").click()
            expect(a.get_by_text("Looks ready to ship.", exact=True)).to_be_visible()
            a.get_by_label("Completion note (optional)").fill("Reviewed with the team.")
            a.get_by_role("button", name="Mark done", exact=True).click()
            a.get_by_role("button", name="Done 1").click()
            a.get_by_role("button", name=task, exact=True).click()
            expect(a.get_by_text("Reviewed with the team.")).to_be_visible()
            a.get_by_role("button", name="Close", exact=True).click()
            b.reload()
            b.get_by_role("button", name="Done 1").click()
            expect(b.get_by_text("Completed by Mara Chen", exact=False)).to_be_visible()
            b.get_by_role("button", name="Reopen " + task).click()
            b.get_by_role("button", name="Open 1").click()
            b.get_by_role("button", name="Mark done: " + task).click()
            expect(b.get_by_role("button", name="Done 1")).to_be_visible()
            b.get_by_label("Board", exact=True).select_option(ORG)
            expect(b.get_by_text("No open dopes. Add one above.")).to_be_visible()
            assert b.evaluate("document.documentElement.scrollWidth <= innerWidth")
            # A draft stays on its own board, including across same-tab account switches.
            a.get_by_label("Dope description").fill("A team-only draft")
            a.get_by_role("button", name="Personal board", exact=True).click()
            expect(a.get_by_label("Dope description")).to_have_value("")
            a.get_by_role("button", name="Design", exact=True).click()
            expect(a.get_by_label("Dope description")).to_have_value("A team-only draft")
            a.locator("#account-menu summary").click()
            a.get_by_role("link", name="Switch account").click()
            a.get_by_role("link", name="personal", exact=True).click()
            expect(a.get_by_role("heading", name="Personal board", exact=True)).to_be_visible()
            expect(a.get_by_label("Dope description")).to_have_value("")
            expect(a.get_by_role("button", name="Design", exact=True)).to_have_count(0)
            a.locator("#account-menu summary").click()
            a.get_by_role("button", name="Sign out").click()
            expect(a.get_by_role("link", name="Sign in with Silicon Accounts")).to_be_visible()
            assert not errors, errors
            browser.close()
    print("Desktop and mobile board workflows passed.")


if __name__ == "__main__":
    run()
