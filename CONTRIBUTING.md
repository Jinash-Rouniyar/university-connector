# Contributing

The best contribution is your own university. Most schools run Brightspace or Canvas, so you
usually only need one JSON file.

## Add a Brightspace (D2L) school

1. Copy [`universities/templates/brightspace.json`](universities/templates/brightspace.json) to
   `universities/<id>.json`. The filename must equal the `id`.
2. Fill it in:

   | Field | How to find it |
   | --- | --- |
   | `id` | `<country>_<state/region>_<short name>`, lowercase, e.g. `us_ga_kennesaw` |
   | `base_url` | Your LMS origin, e.g. `https://kennesaw.view.usg.edu` |
   | `login_url` | The page you land on when signed out, usually `<base_url>/d2l/login` |
   | `sso_login_url` | On the login page, right-click the "Sign in with SSO / CampusID" link and copy its address. It usually contains `/d2l/lp/auth/saml/initiate-login?entityId=...`. This is what makes silent refresh work. Leave it out if your school signs in directly on `login_url`. |
   | `success_url_contains` | A URL fragment present only after sign-in, normally `/d2l/home` |
   | `session_cookies` | Normally `["d2lSessionVal", "d2lSecureSessionVal"]` |
   | `notes` | SSO provider and MFA quirks (e.g. "Microsoft Entra + Authenticator number matching") |

3. Verify it end to end:

   ```bash
   uv run --group dev pytest                         # preset validation
   uv run university-connector login <id>            # sign in once
   uv run university-connector refresh <id>          # should succeed silently
   uv run university-connector serve                 # then ask an agent what's due
   ```

4. Open a PR that adds the preset and a row in the README's university table. Say in the PR
   whether silent refresh worked and roughly how long it stayed signed in.

## Add a Canvas school

The Canvas engine ([`src/university_connector/lms/canvas.py`](src/university_connector/lms/canvas.py))
is implemented but **untested against a real account**. Georgia Tech (`us_ga_gatech`) is the
first preset, so testing it is the most valuable contribution right now.

1. Copy [`universities/templates/canvas.json`](universities/templates/canvas.json) to
   `universities/<id>.json`. Most Canvas schools start SSO at `<base_url>/login/saml`. Check by
   opening `<base_url>/login` while signed out and seeing where it redirects. Keep
   `success_url_contains` as `<base_url>/`: the connector confirms sign-in with
   `/api/v1/users/self`, not by URL.
2. Run the same verification steps as for Brightspace, then ask an agent what's due.
3. Try a real submission on a low-stakes assignment (or a sandbox course) and include the
   result in your PR.

How the engine works, in case something breaks:

- It sends the `canvas_session` cookie, plus `X-CSRF-Token` (the URL-decoded `_csrf_token`
  cookie) on writes, and strips Canvas's `while(1);` JSON prefix.
- Expiry is a `401` whose body status isn't `"unauthorized"`, or a redirect to `/login`.
  Canvas also returns `401 "unauthorized"` for permission errors, which aren't retried.
- Submissions upload the file in three steps (request a slot, upload to storage without
  Canvas cookies, submit the file id).

The auth layer, portal, and MCP tools never need changes for a new LMS. They only talk to
`LMSClient`.

## Ground rules

- Never commit real cookies, keys, or account data. Tests use fake sessions only.
- Keep tools agent-friendly: return structured errors, never raise raw exceptions to the agent.
- Anything that changes a user's coursework must keep the preview-then-`confirm` flow.
