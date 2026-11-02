from __future__ import annotations

import asyncio

import pytest

from conftest import make_session
from university_connector.auth import ReauthRequired
from university_connector.runtime import Runtime

UID = "us_ga_gastate"


class FakeBrowser:
    def __init__(self, refresh_ok: bool = True, login_delay: float = 0):
        self.refresh_ok = refresh_ok
        self.login_delay = login_delay
        self.store = None

    async def login_interactive(self, university):
        await asyncio.sleep(self.login_delay)
        session = make_session("fresh-login")
        self.store.save(university.id, session)
        return session

    async def silent_refresh(self, university):
        if not self.refresh_ok:
            raise ReauthRequired(university.id, "SSO asked for credentials again")
        session = make_session("refreshed")
        self.store.save(university.id, session)
        return session


@pytest.fixture
def runtime(settings, registry):
    return Runtime(settings, registry)


def use_browser(runtime: Runtime, browser: FakeBrowser) -> FakeBrowser:
    browser.store = runtime.store
    runtime.auth.browser = browser
    return browser


async def test_states_through_login_lifecycle(runtime):
    use_browser(runtime, FakeBrowser(login_delay=0.05))
    assert runtime.auth.state(UID) == "not_connected"

    runtime.auth.start_login(UID)
    assert runtime.auth.state(UID) == "connecting"
    await asyncio.sleep(0.1)
    assert runtime.auth.state(UID) == "connected"
    assert runtime.auth.status(UID)["login_attempt"]["status"] == "succeeded"


async def test_failed_silent_refresh_marks_expired(runtime):
    use_browser(runtime, FakeBrowser(refresh_ok=False))
    runtime.store.save(UID, make_session())
    with pytest.raises(ReauthRequired):
        await runtime.auth.refresh(UID)
    assert runtime.auth.state(UID) == "expired"
    with pytest.raises(ReauthRequired):
        runtime.auth.get_session(UID)


async def test_successful_refresh_keeps_connected(runtime):
    use_browser(runtime, FakeBrowser(refresh_ok=True))
    runtime.store.save(UID, make_session("old"))
    session = await runtime.auth.refresh(UID)
    assert session.cookies_for_host("gastate.view.usg.edu")["d2lSessionVal"] == "refreshed"
    assert runtime.auth.state(UID) == "connected"


async def test_refresh_during_interactive_login_does_not_block(runtime):
    use_browser(runtime, FakeBrowser(login_delay=0.5))
    runtime.store.save(UID, make_session())
    runtime.auth.start_login(UID)
    await asyncio.sleep(0.01)
    with pytest.raises(ReauthRequired, match="in progress"):
        await asyncio.wait_for(runtime.auth.refresh(UID), timeout=0.2)


async def test_refresh_without_any_session_requires_login(runtime):
    use_browser(runtime, FakeBrowser())
    with pytest.raises(ReauthRequired, match="Not connected"):
        await runtime.auth.refresh(UID)


async def test_reconnect_after_expiry_clears_flag(runtime):
    use_browser(runtime, FakeBrowser(refresh_ok=False))
    runtime.store.save(UID, make_session())
    with pytest.raises(ReauthRequired):
        await runtime.auth.refresh(UID)
    await runtime.auth.login(UID)
    assert runtime.auth.state(UID) == "connected"
