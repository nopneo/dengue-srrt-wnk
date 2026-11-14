"""บทบาท admin อยู่เหนือ srrt (viewer < srrt < admin) — has_role, role_required, ปุ่มแก้หมุด, run.py."""
import json
import re
from datetime import datetime

import pytest

import auth
import run
from app import create_app
from config import load_settings
from fakes import FakeSender, FakeSource
from store import ROLES, Store
from webfmt import ROLE_LABELS

HDR = {"X-Requested-With": "dengue"}
NOW = datetime(2026, 11, 8, 9, 0)


@pytest.fixture
def app(tmp_path, store):
    settings = load_settings({"SECRET_KEY": "test-secret-key-0123456789",
                              "DENGUE_DB_PATH": str(tmp_path / "d.db")})
    return create_app(settings=settings, store=store, source=FakeSource(), sender=FakeSender())


@pytest.fixture
def client(app):
    return app.test_client()


def add_case(store):
    return store.add_case("VN:1", "OPD", "000001", "A90", NOW, "40010103", "1", "1", 30, True)


def case_data(html):
    m = re.search(r'<script type="application/json" id="case-data">(.*?)</script>', html, re.S)
    assert m
    return json.loads(m.group(1))


def test_roles_include_admin_last():
    assert ROLES == ("srrt", "viewer", "admin")


def test_has_role_ordering():
    assert auth.ROLE_RANK == {"viewer": 0, "srrt": 1, "admin": 2}
    assert auth.has_role("admin", "srrt") and auth.has_role("admin", "viewer")
    assert auth.has_role("admin", "admin")
    assert auth.has_role("srrt", "srrt") and auth.has_role("srrt", "viewer")
    assert not auth.has_role("srrt", "admin")
    assert auth.has_role("viewer", "viewer")
    assert not auth.has_role("viewer", "srrt")
    assert not auth.has_role(None, "viewer")
    assert not auth.has_role("ghost", "viewer")
    assert not auth.has_role("admin", "ghost")


def test_admin_can_pin_via_api(client, login_as, store):
    cid = add_case(store)
    login_as(client, "boss", "admin")
    r = client.post(f"/api/case/{cid}/pin", headers=HDR,
                    json={"lat": 14.35, "lng": 101.9, "state": "confirmed"})
    assert r.status_code == 200
    assert store.get_pin(cid)["lat"] == 14.35


def test_admin_sees_edit_controls_on_case_page(client, login_as, store):
    cid = add_case(store)
    login_as(client, "boss", "admin")
    html = client.get(f"/case/{cid}").get_data(as_text=True)
    assert 'id="btn-confirm"' in html
    assert case_data(html)["can_edit"] is True


def test_viewer_still_cannot_edit(client, login_as, store):
    cid = add_case(store)
    login_as(client, "v", "viewer")
    html = client.get(f"/case/{cid}").get_data(as_text=True)
    assert 'id="btn-confirm"' not in html
    assert case_data(html)["can_edit"] is False
    r = client.post(f"/api/case/{cid}/pin", headers=HDR,
                    json={"lat": 14.35, "lng": 101.9, "state": "confirmed"})
    assert r.status_code == 403


def test_role_required_admin_only_and_srrt_accepts_admin(app, client, login_as):
    @app.get("/_only_admin")
    @auth.role_required("admin")
    def only_admin():
        return "ok"

    @app.get("/_srrt_up")
    @auth.role_required("srrt")
    def srrt_up():
        return "ok"

    for role, admin_ok, srrt_ok in (("admin", 200, 200), ("srrt", 403, 200), ("viewer", 403, 403)):
        login_as(client, f"u_{role}", role)
        assert client.get("/_only_admin").status_code == admin_ok
        assert client.get("/_srrt_up").status_code == srrt_ok


def test_role_downgrade_takes_effect_next_request(app, client, login_as, store):
    @app.get("/_only_admin")
    @auth.role_required("admin")
    def only_admin():
        return "ok"

    login_as(client, "boss", "admin")
    assert client.get("/_only_admin").status_code == 200
    with store._connect() as con:   # ลดสิทธิ์ในฐาน โดยคุกกี้ยังบอก admin
        con.execute('UPDATE "user" SET role=? WHERE username=?', ("viewer", "boss"))
    assert client.get("/_only_admin").status_code == 403


def test_add_user_accepts_admin_role(tmp_path, monkeypatch):
    path = tmp_path / "data" / "dengue.db"
    it = iter(["a-long-password-1", "a-long-password-1"])
    monkeypatch.setattr(run.getpass, "getpass", lambda prompt="": next(it))
    assert run.main(["add-user", "boss", "admin"], env={"DENGUE_DB_PATH": str(path)}) == 0
    assert Store(str(path)).user_role("boss") == "admin"


def test_role_label_for_admin():
    assert ROLE_LABELS["admin"] == "ผู้ดูแล"


def test_admin_role_label_shown_in_page(client, login_as):
    login_as(client, "boss", "admin")
    html = client.get("/").get_data(as_text=True)
    assert "ผู้ดูแล" in html
