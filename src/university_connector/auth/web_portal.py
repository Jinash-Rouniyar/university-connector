from __future__ import annotations

import asyncio
import html
import json
import logging
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from starlette.types import ASGIApp, Receive, Scope, Send

from university_connector.runtime import Runtime
from university_connector.universities.registry import RegistryError

log = logging.getLogger(__name__)

ACCESS_COOKIE = "uc_key"
_PUBLIC_PATHS = ("/healthz", "/files/")


class AccessKeyMiddleware:
    """Require the connector access key on every request except health and signed file links.

    The key can arrive as ``Authorization: Bearer``, ``?key=`` (for agents that only accept a
    URL, and for links tapped on a phone), or the cookie the portal sets after the first visit.
    """

    def __init__(self, app: ASGIApp, runtime: Runtime):
        self.app = app
        self.runtime = runtime

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(_PUBLIC_PATHS):
            await self.app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        query_key = parse_qs(scope.get("query_string", b"").decode()).get("key", [None])[0]
        bearer = headers.get("authorization", "")
        bearer_key = bearer[7:].strip() if bearer.lower().startswith("bearer ") else None
        cookie = SimpleCookie(headers.get("cookie", ""))
        cookie_key = cookie[ACCESS_COOKIE].value if ACCESS_COOKIE in cookie else None

        verify = self.runtime.access_key.verify
        if not any(verify(k) for k in (bearer_key, query_key, cookie_key)):
            if "text/html" in headers.get("accept", ""):
                response = HTMLResponse(_LOCKED_PAGE, status_code=401)
            else:
                response = JSONResponse(
                    {"error": "unauthorized", "message": "Missing or invalid access key"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            await response(scope, receive, send)
            return

        if verify(query_key) and not verify(cookie_key):
            send = _with_access_cookie(send, query_key, secure=scope.get("scheme") == "https")
        await self.app(scope, receive, send)


def _with_access_cookie(send: Send, key: str, secure: bool) -> Send:
    attributes = f"{ACCESS_COOKIE}={key}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000"
    if secure:
        attributes += "; Secure"

    async def wrapped(message: dict) -> None:
        if message["type"] == "http.response.start":
            message.setdefault("headers", [])
            message["headers"] = [*message["headers"], (b"set-cookie", attributes.encode())]
        await send(message)

    return wrapped


class UniversityRequest(BaseModel):
    university_id: str


def create_app(runtime: Runtime) -> FastAPI:
    from university_connector.mcp.server import create_mcp

    mcp = create_mcp(runtime)
    mcp_app = mcp.http_app(
        path="/mcp",
        host_origin_protection=True,
        allowed_hosts=runtime.settings.allowed_hosts,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        keepalive = asyncio.create_task(runtime.keepalive_forever())
        async with mcp_app.lifespan(app):
            try:
                yield
            finally:
                keepalive.cancel()
                await runtime.aclose()

    app = FastAPI(title="University Connector", lifespan=lifespan, docs_url=None, redoc_url=None)

    def lookup(university_id: str) -> dict:
        try:
            return runtime.auth.status(university_id)
        except RegistryError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    async def index(university: str | None = None, autostart: bool = False) -> str:
        return _render_index(runtime, selected=university, autostart=autostart)

    @app.get("/api/universities")
    async def universities() -> list[dict]:
        return [runtime.auth.status(u.id) for u in runtime.registry.all()]

    @app.get("/auth/status")
    async def auth_status(university: str) -> dict:
        return lookup(university)

    @app.post("/auth/start")
    async def auth_start(body: UniversityRequest) -> dict:
        lookup(body.university_id)
        runtime.auth.start_login(body.university_id)
        return lookup(body.university_id)

    @app.get("/auth/reconnect", response_class=HTMLResponse)
    async def auth_reconnect(university: str) -> str:
        lookup(university)
        runtime.auth.start_login(university)
        return _render_index(runtime, selected=university, autostart=False)

    @app.post("/auth/disconnect")
    async def auth_disconnect(body: UniversityRequest) -> dict:
        lookup(body.university_id)
        runtime.auth.disconnect(body.university_id)
        return lookup(body.university_id)

    @app.get("/files/{relative:path}")
    async def download(relative: str, sig: str | None = None) -> FileResponse:
        path = runtime.resolve_download(relative, sig)
        if path is None:
            raise HTTPException(status_code=404, detail="File not found or link invalid")
        return FileResponse(path, filename=path.name)

    app.mount("/", mcp_app)
    app.add_middleware(AccessKeyMiddleware, runtime=runtime)
    return app


_LOCKED_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>University Connector</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto; padding: 0 1rem;">
<h1>University Connector</h1>
<p>This connector is locked. Open the portal link printed by
<code>university-connector serve</code>, or ask your agent for a fresh reconnect link.</p>
</body></html>"""


def _render_index(runtime: Runtime, selected: str | None, autostart: bool) -> str:
    universities = runtime.registry.all()
    selected = selected if selected in runtime.registry else (universities[0].id if universities else "")
    options = "\n".join(
        f'<option value="{html.escape(u.id)}"{" selected" if u.id == selected else ""}>'
        f"{html.escape(u.name)} ({html.escape(u.lms_type.title())})</option>"
        for u in universities
    )
    config = json.dumps({"autostart": autostart, "mcpUrl": runtime.settings.mcp_url})
    return _INDEX_TEMPLATE.replace("{{OPTIONS}}", options).replace("{{CONFIG}}", config)


_INDEX_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>University Connector</title>
<style>
  :root { color-scheme: light dark; --accent: #2457d6; }
  body { font-family: system-ui, -apple-system, sans-serif; max-width: 34rem; margin: 3rem auto; padding: 0 1rem; line-height: 1.5; }
  h1 { font-size: 1.6rem; margin-bottom: .25rem; }
  .sub { opacity: .75; margin-top: 0; }
  label { display: block; font-weight: 600; margin: 1.5rem 0 .4rem; }
  select, button { font: inherit; padding: .65rem .8rem; border-radius: .5rem; width: 100%; box-sizing: border-box; }
  button { margin-top: .9rem; border: 0; background: var(--accent); color: #fff; font-weight: 600; cursor: pointer; }
  button.secondary { background: transparent; color: inherit; border: 1px solid currentColor; opacity: .7; }
  button:disabled { opacity: .5; cursor: wait; }
  .status { margin-top: 1.5rem; padding: .9rem 1rem; border-radius: .5rem; background: rgba(127,127,127,.12); }
  .state { font-weight: 700; }
  .connected { color: #1a8f4c; } .expired, .not_connected { color: #c0392b; } .connecting { color: #b7791f; }
  code { font-size: .85em; word-break: break-all; }
  .hint { font-size: .9rem; opacity: .75; }
</style>
</head>
<body>
<h1>University Connector</h1>
<p class="sub">Let your AI agent check assignments, download files, and submit work on your behalf.</p>

<label for="university">University</label>
<select id="university">
{{OPTIONS}}
</select>
<button id="connect">Connect</button>
<button id="disconnect" class="secondary" hidden>Disconnect</button>

<div class="status" id="status">Loading status...</div>

<p class="hint">Connecting opens your school's sign-in page in a separate browser window on the computer
running the connector. Sign in and approve MFA; if offered, choose "remember this device" so the
connector can stay signed in silently for longer.</p>
<p class="hint">Agent MCP endpoint: <code id="mcp"></code></p>

<script>
const CONFIG = {{CONFIG}};
const $ = id => document.getElementById(id);
$("mcp").textContent = CONFIG.mcpUrl;
let polling = null;

const LABELS = {
  connected: "Connected",
  connecting: "Waiting for you to finish signing in...",
  expired: "Session expired - reconnect to continue",
  not_connected: "Not connected",
};

function fmt(ts) { return ts ? new Date(ts * 1000).toLocaleString() : "-"; }
function esc(text) { const d = document.createElement("div"); d.textContent = text; return d.innerHTML; }

function render(s) {
  const attempt = s.login_attempt;
  let html = `<div class="state ${s.state}">${LABELS[s.state] || s.state}</div>`;
  if (s.state === "connected") {
    html += `<div>Signed in since ${fmt(s.connected_since)}<br>Last refreshed ${fmt(s.last_refreshed_at)}</div>`;
    html += `<div class="hint">You can close this tab and talk to your agent.</div>`;
  }
  if (attempt && attempt.status === "failed") html += `<div class="hint">Last attempt: ${esc(attempt.message)}</div>`;
  $("status").innerHTML = html;
  $("connect").disabled = s.state === "connecting";
  $("connect").textContent = s.state === "connected" ? "Reconnect" : (s.state === "connecting" ? "Signing in..." : "Connect");
  $("disconnect").hidden = s.state !== "connected" && s.state !== "expired";
  if (s.state === "connecting") startPolling(); else stopPolling();
}

async function api(path, options = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...options });
  if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
  return res.json();
}

async function refresh() {
  try { render(await api(`/auth/status?university=${encodeURIComponent($("university").value)}`)); }
  catch (e) { $("status").textContent = e.message; }
}

function post(path) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ university_id: $("university").value }),
  });
}

function startPolling() { if (!polling) polling = setInterval(refresh, 2000); }
function stopPolling() { clearInterval(polling); polling = null; }

$("connect").onclick = async () => { $("connect").disabled = true; render(await post("/auth/start")); };
$("disconnect").onclick = async () => render(await post("/auth/disconnect"));
$("university").onchange = refresh;

refresh().then(() => { if (CONFIG.autostart) $("connect").click(); });
</script>
</body>
</html>
"""
