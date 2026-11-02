from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from university_connector.auth.session_store import SessionData, SessionStore
from university_connector.config import Settings
from university_connector.universities import University

log = logging.getLogger(__name__)

SessionValidator = Callable[[University, SessionData], Awaitable[bool]]

_COOKIE_FIELDS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")

_READ_XSRF_JS = "() => { try { return localStorage.getItem('XSRF.Token'); } catch (e) { return null; } }"
_HAS_LOGIN_FORM_JS = """() => {
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  return [...document.querySelectorAll('input[type=password]')].some(visible);
}"""


class AuthError(Exception):
    pass


class ReauthRequired(AuthError):
    def __init__(self, university_id: str, reason: str):
        super().__init__(f"{university_id}: {reason}")
        self.university_id = university_id
        self.reason = reason


class LoginCancelled(AuthError):
    pass


class LoginTimedOut(AuthError):
    pass


def _replayable(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: c[k] for k in _COOKIE_FIELDS if k in c} for c in cookies]


class PersistentBrowser:
    """Drives a per-university persistent Chromium profile.

    The profile keeps long-lived IdP/MFA state (e.g. Duo "remember this device"), and the
    encrypted session store keeps the full cookie jar so session-only SSO cookies survive
    browser restarts. Together they let ``silent_refresh`` mint new LMS cookies headlessly.
    """

    def __init__(self, settings: Settings, store: SessionStore, validator: SessionValidator):
        self._settings = settings
        self._store = store
        self._validate = validator

    async def login_interactive(self, university: University) -> SessionData:
        return await self._run(university, interactive=True)

    async def silent_refresh(self, university: University) -> SessionData:
        try:
            return await self._run(university, interactive=False)
        except (LoginTimedOut, LoginCancelled) as exc:
            raise ReauthRequired(university.id, str(exc)) from exc

    async def _run(self, university: University, *, interactive: bool) -> SessionData:
        from playwright.async_api import async_playwright

        previous = self._store.load(university.id)
        timeout = (
            self._settings.login_timeout_seconds
            if interactive
            else self._settings.refresh_timeout_seconds
        )

        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(self._store.profile_dir(university.id)),
                headless=not interactive,
                channel=self._settings.browser_channel,
                # Headless Chromium advertises "HeadlessChrome"; reuse the headed UA so
                # MFA device trust bound to the browser fingerprint keeps matching.
                user_agent=None if interactive else (previous.user_agent if previous else None),
                no_viewport=interactive,
                args=["--disable-blink-features=AutomationControlled"],
            )
            closed = asyncio.Event()
            context.on("close", lambda _: closed.set())
            try:
                if previous and previous.cookies:
                    await context.add_cookies(_replayable(previous.cookies))
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(university.start_url, wait_until="domcontentloaded")
                session = await self._wait_for_session(
                    university, context, closed, timeout, interactive, previous
                )
                self._store.save(university.id, session)
                return session
            finally:
                if not closed.is_set():
                    await context.close()

    async def _wait_for_session(
        self,
        university: University,
        context: Any,
        closed: asyncio.Event,
        timeout: int,
        interactive: bool,
        previous: SessionData | None,
    ) -> SessionData:
        deadline = time.monotonic() + timeout
        while True:
            if closed.is_set() or not context.pages:
                raise LoginCancelled("Login window was closed before sign-in finished")

            for page in list(context.pages):
                if university.success_url_contains not in page.url:
                    continue
                candidate = await self._capture(
                    university, context, page, None if interactive else previous
                )
                if candidate and await self._validate(university, candidate):
                    return candidate

            if not interactive and await self._shows_login_form(context):
                raise ReauthRequired(university.id, "SSO asked for credentials or MFA again")

            if time.monotonic() > deadline:
                raise LoginTimedOut(f"Sign-in did not complete within {timeout} seconds")
            await asyncio.sleep(1)

    async def _capture(
        self, university: University, context: Any, page: Any, previous: SessionData | None
    ) -> SessionData | None:
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass  # long-polling pages never go idle; cookies are already set by now
        cookies = await context.cookies()
        session = SessionData(cookies=cookies)
        if not session.has_cookies(university.host, university.session_cookies):
            return None
        try:
            session.xsrf_token = await page.evaluate(_READ_XSRF_JS)
            session.user_agent = await page.evaluate("() => navigator.userAgent")
        except Exception as exc:
            log.debug("Could not read page state for %s: %s", university.id, exc)
        if previous:
            session.captured_at = previous.captured_at
            session.user_agent = session.user_agent or previous.user_agent
        return session

    async def _shows_login_form(self, context: Any) -> bool:
        for page in list(context.pages):
            try:
                if await page.evaluate(_HAS_LOGIN_FORM_JS):
                    return True
            except Exception:
                continue  # page mid-navigation
        return False
