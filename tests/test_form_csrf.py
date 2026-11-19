"""ฟอร์ม login / logout ต้องมีโทเคน CSRF — เว็บอื่นพาเบราว์เซอร์มา login ด้วยบัญชีผู้โจมตี หรือสั่ง logout ไม่ได้."""
import re

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from config import load_settings


@pytest.fixture
def app(tmp_path, store):
    settings = load_settings({"SECRET_KEY": "test-secret-key-0123456789", "DENGUE_DB_PATH": str(tmp_path / "d.db")})
    with store._connect() as con:
        con.execute('INSERT INTO "user" (username, password_hash, role) VALUES (?,?,?)',
                    ("nurse", generate_password_hash("pw-nurse", method="pbkdf2:sha256:1000"), "srrt"))
    return create_app(settings=settings, store=store)


@pytest.fixture
def client(app):
    return app.test_client()


def _token(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_login_page_has_token_and_valid_token_logs_in(client):
    tok = _token(client.get("/login").get_data(as_text=True))
    r = client.post("/login", data={"username": "nurse", "password": "pw-nurse", "csrf": tok}, no_csrf=True)
    assert r.status_code == 302
    with client.session_transaction() as s:
        assert s["user"] == "nurse"


def test_login_without_token_rejected(client):
    r = client.post("/login", data={"username": "nurse", "password": "pw-nurse"}, no_csrf=True)
    assert r.status_code == 400
    with client.session_transaction() as s:
        assert "user" not in s


def test_login_with_wrong_token_rejected(client):
    client.get("/login")
    r = client.post("/login", data={"username": "nurse", "password": "pw-nurse", "csrf": "forged"}, no_csrf=True)
    assert r.status_code == 400
    with client.session_transaction() as s:
        assert "user" not in s


def test_csrf_failure_does_not_count_as_password_guess(client, app):
    for _ in range(10):
        client.post("/login", data={"username": "nurse", "password": "x"}, no_csrf=True)
    assert client.post("/login", data={"username": "nurse", "password": "pw-nurse"}).status_code == 302


def test_logout_requires_token(client):
    tok = _token(client.get("/login").get_data(as_text=True))
    client.post("/login", data={"username": "nurse", "password": "pw-nurse", "csrf": tok}, no_csrf=True)
    r = client.post("/logout", no_csrf=True)                      # ฟอร์มจากเว็บอื่น ไม่มีโทเคน
    assert r.status_code == 400
    with client.session_transaction() as s:
        assert s.get("user") == "nurse"
    page = client.get("/").get_data(as_text=True)                  # หน้าในแอปมีปุ่มออกจากระบบพร้อมโทเคน
    r = client.post("/logout", data={"csrf": _token(page)}, no_csrf=True)
    assert r.status_code == 302
    with client.session_transaction() as s:
        assert "user" not in s


def test_logout_without_session_is_harmless(client):
    assert client.post("/logout", no_csrf=True).status_code == 302
