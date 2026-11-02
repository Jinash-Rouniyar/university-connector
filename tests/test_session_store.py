from __future__ import annotations

import stat

from conftest import make_session
from university_connector.auth import SessionStore

UID = "us_ga_gastate"


def test_round_trip_is_encrypted_at_rest(settings):
    store = SessionStore(settings)
    store.save(UID, make_session("secret-cookie"))

    session_file = settings.universities_home / UID / "session.enc"
    assert b"secret-cookie" not in session_file.read_bytes()
    assert stat.S_IMODE(session_file.stat().st_mode) == 0o600

    loaded = store.load(UID)
    assert loaded.cookies_for_host("gastate.view.usg.edu")["d2lSessionVal"] == "secret-cookie"
    assert loaded.xsrf_token == "xsrf-123"


def test_cookies_are_scoped_by_host(settings):
    session = make_session()
    lms = session.cookies_for_host("gastate.view.usg.edu")
    assert "shib_idp_session" not in lms
    assert session.cookies_for_host("idp.gsu.edu") == {"shib_idp_session": "idp"}
    assert session.has_cookies("gastate.view.usg.edu", ["d2lSessionVal", "d2lSecureSessionVal"])


def test_mark_needs_reauth_and_clear(settings):
    store = SessionStore(settings)
    assert store.load(UID) is None
    store.save(UID, make_session())
    store.mark_needs_reauth(UID)
    assert store.load(UID).needs_reauth
    store.clear(UID)
    assert store.load(UID) is None


def test_unreadable_session_is_treated_as_signed_out(settings):
    store = SessionStore(settings)
    store.save(UID, make_session())
    (settings.home / ".encryption-key").write_bytes(b"0" * 43 + b"=")
    assert SessionStore(settings).load(UID) is None


def test_update_host_cookies_only_touches_matching_host():
    session = make_session()
    changed = session.update_host_cookies(
        "gastate.view.usg.edu", {"d2lSessionVal": "new", "shib_idp_session": "hijack"}
    )
    assert changed
    assert session.cookies_for_host("gastate.view.usg.edu")["d2lSessionVal"] == "new"
    assert session.cookies_for_host("idp.gsu.edu")["shib_idp_session"] == "idp"
