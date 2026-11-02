# University Connector

Text your AI agent (Meta Muse, OpenAI dots/ChatGPT, Grok Bot, Cursor, Claude) to see what's due,
download assignment files, and submit finished work to your university's LMS, without opening
the LMS website.

It runs on your own computer, signs in through your school's real SSO + MFA page in a browser
window, and exposes an [MCP](https://modelcontextprotocol.io) server your agent calls.

| University | LMS | Status |
| --- | --- | --- |
| Georgia State University (`us_ga_gastate`) | Brightspace (D2L) | Supported |
| Georgia Institute of Technology (`us_ga_gatech`) | Canvas | Implemented, **untested** with a real account |
| Your school? | Brightspace / Canvas | [Add it](CONTRIBUTING.md) |

Any Brightspace or Canvas school can be added with a single JSON file.

> **Canvas is untested as of now.** The Canvas engine is complete and covered by mock-server
> tests built from the Canvas API docs, and the silent-refresh check reaches Georgia Tech's real
> SSO page correctly. It has not yet been run against a signed-in Canvas account. If you try
> it, please report what works and what breaks.

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run playwright install chromium
uv run university-connector serve
```

`serve` prints a portal link and opens it. Pick your university (e.g. **Georgia State
University**), click **Connect**, and sign in with your school account + Duo in the window that
pops up. When Duo offers
**"Remember me / trust this browser"**, accept it: that's what lets the connector stay signed in
silently. The window closes itself once you reach your LMS home page.

```
University Connector
  Portal:        http://127.0.0.1:8787/?key=...
  MCP endpoint:  http://127.0.0.1:8787/mcp
  Access key:    ...
```

## How staying signed in works

The LMS's own session cookie dies after a period of inactivity (about an hour on D2L), but
the layers behind it last much longer:

```
LMS session cookie      ~1 hour idle      kept alive by a background touch every 10 minutes
School SSO session      hours             replayed from the encrypted cookie jar
Duo device trust        ~30 days          kept in the connector's own browser profile
```

1. **Keepalive.** While `serve` runs, every connected school gets a lightweight `whoami` call
   every 10 minutes, so the LMS inactivity timer never fires.
2. **Silent refresh.** If the LMS still rejects the session, the connector opens a *headless*
   browser with the same profile and cookies and goes straight to the school's SSO link. If
   the IdP session and Duo trust are still valid, fresh cookies come back in a few seconds and
   the original request is retried. You never see it.
3. **Reconnect.** If the SSO page asks for a password or MFA again, the school is marked
   `expired` and every tool returns `{"error": "reauth_required", "reconnect_url": ...}`. Your
   agent sends you that link; opening it pops the sign-in window again.

The sign-in window always opens on the computer running the connector. Tapping a reconnect
link on your phone starts it there, so that computer needs to be awake.

You can test each step yourself:

```bash
uv run university-connector login us_ga_gastate     # sign in from the terminal
uv run university-connector refresh us_ga_gastate   # force a silent refresh
uv run university-connector status
```

## Connecting your agent

Cloud agents (Muse, dots, Grok Bot) can't reach `127.0.0.1`, so expose the server through a
tunnel and tell the connector its public URL. The public URL goes into reconnect and download
links.

```bash
cloudflared tunnel --url http://127.0.0.1:8787     # prints https://<random>.trycloudflare.com
uv run university-connector serve --public-url https://<random>.trycloudflare.com
```

ngrok works too (`ngrok http 8787`). `*.trycloudflare.com` and `*.ngrok-*` hosts are allowed
automatically. For a custom domain, set `UC_ALLOWED_HOSTS=my.domain.com`. Quick-tunnel URLs
change on every restart, so update the agent when they do, or use a named tunnel.

Every request must carry the access key (`university-connector key`; rotate it with
`university-connector key --rotate`). Agents can send it either way:

- Header: `Authorization: Bearer <key>`
- URL: `https://<tunnel>/mcp?key=<key>`, for agents that only accept a URL

| Agent | How to add it |
| --- | --- |
| **Meta Muse** | Ask Muse to *"add a custom connector for the MCP server at `https://<tunnel>/mcp`, sending my API key as a Bearer token"*. Enter the key in Muse's secure credential form, never in chat. |
| **OpenAI dots / ChatGPT** | ChatGPT, Plugins, **+**, Add custom MCP server. URL: `https://<tunnel>/mcp?key=<key>`, authentication: none. Then select the plugin in your dot's Customize, Plugins. |
| **Grok Bot** | In a Bot chat: *"Add this MCP server: `https://<tunnel>/mcp?key=<key>`"*. Grok Bot custom connectors don't accept secret headers, so the key goes in the URL. |
| **Cursor / Claude Desktop** | Local stdio: `{"command": "uv", "args": ["run", "--directory", "/path/to/connector", "university-connector", "stdio"]}`, or HTTP with `url` + `headers: {"Authorization": "Bearer <key>"}`. |

Treat a URL that contains `?key=` like a password.

## What your agent can do

| Tool | What it does |
| --- | --- |
| `list_universities`, `get_auth_status` | Show which schools are connected |
| `get_reconnect_url`, `start_login` | Get a sign-in link, or open the sign-in window now |
| `list_courses` | Current courses with their `course_id` |
| `get_due_assignments` | What's due in the next N days, soonest first, hiding work you've already submitted |
| `get_assignment_details` | Full instructions, attachments, and submission status |
| `download_assignment_files` | Instructor attachments as a link you can open on your phone, or inline base64 |
| `submit_assignment` | Upload a file (local path, base64, or URL). Returns a preview first; only submits when called again with `confirm=true` |

Example conversation:

> **You:** what's due this week?
> **Muse:** CSC 4350 Homework 3, Friday 11:59 PM (one PDF attached)...
> **You:** send me the PDF
> **You:** *(later, with your answers attached)* submit this to Homework 3
> **Muse:** About to submit `answers.pdf` to CSC 4350, Homework 3, due Fri 11:59 PM. Go ahead?
> **You:** yes
> **Muse:** Submitted. Submission #777 at 9:42 PM.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `UC_HOME` | `~/.university-connector` | Browser profiles, encrypted sessions, downloads, submission log |
| `UC_HOST` / `UC_PORT` | `127.0.0.1` / `8787` | Bind address |
| `UC_PUBLIC_URL` | unset | Public tunnel URL (same as `--public-url`) |
| `UC_ALLOWED_HOSTS` | unset | Extra comma-separated Host header patterns for MCP |
| `UC_KEEPALIVE_MINUTES` | `10` | How often connected sessions are touched |
| `UC_BROWSER_CHANNEL` | bundled Chromium | Set to `chrome` to sign in with your installed Google Chrome |
| `UC_USE_KEYRING` | `1` | Store the encryption key in the OS keychain (falls back to a `0600` file) |
| `UC_UNIVERSITIES_DIR` | unset | Extra directory of university presets |

## Security and academic integrity

- Your password never passes through the connector or the agent. You type it into your
  school's real sign-in page. The connector only keeps the resulting cookies, encrypted with a
  key stored in your OS keychain.
- Everything stays on your computer under `~/.university-connector` (permissions `0700`).
- All HTTP endpoints require the access key. Download links are individually signed.
- Submissions always need an explicit confirmation step, and each one is appended to
  `~/.university-connector/submissions.jsonl`.
- This automates your own account, doing what you could do in the browser. You are
  responsible for following your school's honor code and acceptable-use policies. It doesn't
  access anyone else's data.

## Known limitations (v0.1)

- **Live-tested so far:** for both GSU and Georgia Tech, the headless refresh reaches the
  school's real SSO page and detects when a human is needed. **Not yet live-tested:** a full
  signed-in session (courses, downloads, submission) at either school. Those paths are covered
  by mock-server tests built from the D2L and Canvas API docs.
- **Brightspace submission** uses D2L's documented `multipart/mixed` route plus the browser's
  XSRF token. If GSU rejects it, please open an issue with the error message.
- **Canvas is untested.** Submission uses Canvas's three-step upload (request a slot, upload
  to storage, submit the file id) with the `_csrf_token` cookie as the CSRF header.
- The sign-in window opens on the host computer, not on your phone.
- Your computer must be on and running `serve` for the agent to reach it. Hosted deployment
  (e.g. FastMCP Cloud) is planned.
- Only file-upload assignments can be submitted. Text-entry, quizzes, and discussions aren't
  supported.
- On Canvas, instructor attachments are found by scanning the assignment description for file
  links. Files shared only through Modules or the Files tab aren't listed.

## Development

```bash
uv sync
uv run --group dev pytest
```

Layout:

```
universities/                 university presets (one JSON per school) + templates
src/university_connector/
  auth/                       session store, Playwright login/refresh, auth manager, web portal
  lms/                        LMS engines: brightspace.py, canvas.py (untested)
  mcp/server.py               MCP tools
  runtime.py                  wiring, keepalive loop, downloads, submission log
  cli.py                      `university-connector` command
```
