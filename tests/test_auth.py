"""Accounts: the three modes, hashing, sessions, and Cloudflare."""
import pytest

from app import auth, cf_access, db


def test_a_fresh_install_asks_nobody_to_sign_in():
    db.set_setting("auth_mode", "")
    assert auth.mode() == "none"
    assert auth.needs_setup() is False


def test_an_unknown_mode_falls_back_to_off():
    db.set_setting("auth_mode", "nonsense")
    assert auth.mode() == "none"


def test_turning_local_on_with_no_accounts_asks_for_one():
    db.set_setting("auth_mode", "local")
    assert auth.needs_setup() is True
    auth.create_user("someone", "a-good-password")
    assert auth.needs_setup() is False


def test_a_password_is_hashed_not_stored():
    uid = auth.create_user("bob", "a-good-password")
    row = auth._con().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    assert "a-good-password" not in (row["pw_hash"] or "")
    assert row["pw_salt"] and len(row["pw_salt"]) == 32


def test_two_accounts_with_the_same_password_hash_differently():
    auth.create_user("a", "same-password")
    auth.create_user("b", "same-password")
    rows = auth._con().execute("SELECT pw_hash, pw_salt FROM users").fetchall()
    assert rows[0]["pw_salt"] != rows[1]["pw_salt"]
    assert rows[0]["pw_hash"] != rows[1]["pw_hash"]


def test_verify_accepts_the_right_password_and_refuses_the_wrong_one():
    auth.create_user("bob", "a-good-password")
    assert auth.verify("bob", "a-good-password")["username"] == "bob"
    assert auth.verify("bob", "wrong") is None
    assert auth.verify("nobody", "a-good-password") is None


def test_a_username_is_case_insensitive():
    auth.create_user("Bob", "a-good-password")
    assert auth.verify("bOB", "a-good-password")
    with pytest.raises(ValueError):
        auth.create_user("BOB", "another-password")


def test_a_short_password_is_refused():
    with pytest.raises(ValueError, match="8 characters"):
        auth.create_user("bob", "short")


def test_a_session_token_is_stored_hashed():
    uid = auth.create_user("bob", "a-good-password")
    token = auth.create_session(uid)
    stored = auth._con().execute("SELECT token_hash FROM sessions").fetchone()
    assert token not in stored["token_hash"], "a copy of the file grants no logins"
    assert auth.session_user(token)["id"] == uid


def test_an_expired_session_is_not_accepted():
    uid = auth.create_user("bob", "a-good-password")
    token = auth.create_session(uid)
    con = auth._con()
    con.execute("UPDATE sessions SET expires_at = 1")
    con.commit()
    assert auth.session_user(token) is None


def test_signing_out_drops_the_session():
    uid = auth.create_user("bob", "a-good-password")
    token = auth.create_session(uid)
    auth.delete_session(token)
    assert auth.session_user(token) is None


def test_deleting_an_account_takes_its_sessions_and_preferences():
    uid = auth.create_user("bob", "a-good-password")
    token = auth.create_session(uid)
    auth.set_pref(uid, "theme", "light")
    auth.delete_user(uid)
    assert auth.session_user(token) is None
    assert auth.get_pref(uid, "theme") is None


def test_a_cloudflare_identity_gets_an_account_on_first_sight():
    first = auth.user_for_email("someone@example.com")
    assert first["username"] == "someone"
    assert first["role"] == "admin", "the first account is the administrator"
    again = auth.user_for_email("someone@example.com")
    assert again["id"] == first["id"], "and is not created twice"
    second = auth.user_for_email("someone@other.example")
    assert second["username"] == "someone2", "a name clash is resolved"
    assert second["role"] == "user"


def test_a_theme_is_remembered_per_account():
    a = auth.create_user("a", "a-good-password")
    b = auth.create_user("b", "a-good-password")
    auth.set_pref(a, "theme", "light")
    assert auth.get_pref(a, "theme") == "light"
    assert auth.get_pref(b, "theme") is None


def test_cloudflare_refuses_a_token_it_cannot_verify():
    """The plain email header is never trusted on its own."""
    assert cf_access.verify_email("", "team.example", "aud") is None
    assert cf_access.verify_email("not-a-jwt", "team.example", "aud") is None
    assert cf_access.verify_email("x.y.z", "", "aud") is None


def test_the_cloudflare_check_refuses_an_unreachable_team():
    ok, detail = cf_access.check("no-such-team.invalid", "aud")
    assert ok is False and detail
