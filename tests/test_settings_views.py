"""หน้า /settings + POST /api/settings/save|revert (Task 4) — ส่วนที่เขียนความลับ จึงเข้มเรื่องสิทธิ์/ล็อก/ไม่หลุดค่า."""
import json
import logging
import os
import re
import signal
import threading
import time

import pymysql
import pytest

import config
import settings_store
import settings_views
from app import create_app
from config import load_settings
from fakes import FakeSender, FakeSource

HDR = {"X-Requested-With": "dengue"}
PUBLIC = {"X-Forwarded-For": "203.0.113.9"}
GUNICORN = {"SERVER_SOFTWARE": "gunicorn/22.0.0"}
PW = "correct-horse-pw-1"
SAVE = "/api/settings/save"
REVERT = "/api/settings/revert"


class Recorder:
    """RESTART_HOOK ปลอม: ไม่ฆ่าโปรเซส แค่นับครั้งที่ถูกเรียก (Timer เรียกจากเธรดอื่น)"""

    def __init__(self):
        self.calls = 0
        self.event = threading.Event()

    def __call__(self):
        self.calls += 1
        self.event.set()

    def wait(self, timeout=2.0):
        return self.event.wait(timeout)


@pytest.fixture
def hook():
    return Recorder()


class DbSpy:
    """ตัวต่อ HOSxP ปลอมของด่านทดสอบ DB ตอนบันทึก: ไม่แตะเครือข่ายจริง; ค่าเริ่มต้น = ต่อไม่ได้ (2003)"""

    def __init__(self):
        self.ok = False
        self.calls = []

    def __call__(self, host, port, database, user, password, **kw):
        self.calls.append((host, port, database, user))

        def factory():
            if not self.ok:
                raise pymysql.err.OperationalError(2003, "Can't connect to MySQL server on 'h'")

            class Con:
                def close(self):
                    pass
            return Con()
        return factory


@pytest.fixture
def db_spy():
    return DbSpy()


@pytest.fixture
def app(tmp_path, store, hook, db_spy):
    settings = load_settings({"SECRET_KEY": "test-secret-key-0123456789",
                              "DENGUE_DB_PATH": str(tmp_path / "d.db")})
    app = create_app(settings=settings, store=store, source=FakeSource(), sender=FakeSender())
    app.config["RESTART_HOOK"] = hook
    app.config["RESTART_DELAY"] = 0
    app.config["CHECK_DEPS"] = {"connect": db_spy, "tables_source": lambda con: {}}
    return app


@pytest.fixture
def path(app):
    return app.config["SETTINGS_PATH"]


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin(client, store, login_as):
    store.add_user("boss", PW, "admin")
    login_as(client, "boss", "admin")
    return client


def post(client, url, body, headers=HDR, **kw):
    return client.post(url, json=body, headers=headers, **kw)


def save(client, values=None, clear=None, password=PW, **extra):
    body = {"values": values or {}, "clear": clear or [], "password": password}
    body.update(extra)
    return post(client, SAVE, body)


def page_data(client):
    html = client.get("/settings").get_data(as_text=True)
    m = re.search(r'<script type="application/json" id="settings-data">(.*?)</script>', html, re.S)
    assert m, "ไม่พบ settings-data"
    return json.loads(m.group(1)), html


# ---------------------------------------------------------------- GET /settings

def test_settings_page_403_for_srrt_and_viewer(client, store, login_as):
    for role in ("srrt", "viewer"):
        login_as(client, f"u-{role}", role)
        r = client.get("/settings")
        assert r.status_code == 403
        assert "settings-data" not in r.get_data(as_text=True)


def test_settings_page_requires_login_and_lan(client, login_as):
    r = client.get("/settings")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert client.get("/settings", headers=PUBLIC).status_code == 403
    login_as(client, "boss", "admin")
    assert client.get("/settings", headers=PUBLIC).status_code == 403
    assert client.get("/settings", headers={"X-Forwarded-For": "192.168.70.135"}).status_code == 200


def test_settings_page_admin_lan_ok_and_shows_state_not_values(admin, path):
    secret = "UNIQUE-DB-PASSWORD-9f3a"
    settings_store.write_overlay(path, {"DB_HOST": "10.1.2.3", "DB_PASS": secret,
                                        "MOPH_CLIENT_KEY": "UNIQUE-CLIENT-KEY-77"}, "seed")
    settings_store.write_overlay(path, {"DENGUE_BASE_URL": "https://example.org/d"}, "seed")
    r = admin.get("/settings")
    assert r.status_code == 200
    data, html = page_data(admin)
    assert "ตั้งค่า" in html
    assert secret not in html and "UNIQUE-CLIENT-KEY-77" not in html
    items = {i["key"]: i for i in data["items"]}
    assert list(items) == list(settings_store.ALLOWED)
    assert set(items["DB_PASS"]) == {"key", "label", "secret", "state", "source", "value"}
    assert items["DB_PASS"]["secret"] is True and items["DB_PASS"]["value"] is None
    assert items["DB_PASS"]["state"] == "set" and items["DB_PASS"]["source"] == "overlay"
    assert items["MOPH_SECRET_KEY"]["state"] == "unset" and items["MOPH_SECRET_KEY"]["value"] is None
    assert items["DB_HOST"]["value"] == "10.1.2.3" and items["DB_HOST"]["source"] == "overlay"
    assert items["DENGUE_BASE_URL"]["value"] == "https://example.org/d"
    # ตัวที่มีค่าเริ่มต้นใน config แสดงค่าที่แอปใช้จริง
    assert items["DENGUE_DATA_AUDITED"]["value"] == "0"
    # I-2: START_FROM ที่ยังไม่ตั้งแสดงช่องว่าง (value None) + ค่าสำรอง (วันเริ่มระบบ) แยกต่างหาก ไม่ใช่ค่าที่ตั้งไว้
    assert items["DENGUE_START_FROM"]["value"] is None
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", items["DENGUE_START_FROM"]["fallback"])
    assert data["updated_by"] == "seed" and data["updated_at"]
    assert data["has_prev"] is True and data["overlay_error"] is None
    assert data["save_url"].endswith(SAVE) and data["revert_url"].endswith(REVERT)
    # ฟอร์มจริงอยู่ที่ tests/test_settings_page.py; ที่นี่ย้ำว่าค่าลับไม่ถูกใส่ในช่องกรอก
    main = html.split('<main class="content">', 1)[1].split("</main>", 1)[0]
    assert 'id="settings-form"' in main and 'value="10.1.2.3"' in main
    assert "UNIQUE-" not in main


def test_settings_page_reports_overlay_error_text_only(admin, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json UNIQUE-JUNK", encoding="utf-8")
    data, html = page_data(admin)
    assert data["overlay_error"] and "UNIQUE-JUNK" not in html


# ---------------------------------------------------------------- ด่านสิทธิ์ของ POST

@pytest.mark.parametrize("url", [SAVE, REVERT])
def test_save_requires_csrf_header_and_json(client, store, login_as, path, url):
    body = {"values": {"DENGUE_BASE_URL": "https://x.example/d"}, "password": PW}
    # ไม่ login -> 401 JSON
    assert post(client, url, body).status_code == 401
    # srrt / viewer -> 403 (แม้ถูกต้องทุกอย่าง)
    for role in ("srrt", "viewer"):
        login_as(client, f"u-{role}", role)
        assert post(client, url, body).status_code == 403
    store.add_user("boss", PW, "admin")
    login_as(client, "boss", "admin")
    # นอก LAN -> 403
    r = post(client, url, body, headers={**HDR, **PUBLIC})
    assert r.status_code == 403
    # ไม่มีหัว CSRF -> 403 csrf
    r = post(client, url, body, headers={})
    assert r.status_code == 403 and r.get_json()["error"] == "csrf"
    # ไม่ใช่ JSON -> 415
    r = client.post(url, data={"password": PW}, headers=HDR)
    assert r.status_code == 415
    assert not path.exists()


def test_post_non_lan_rejected_before_login_check(client):
    r = post(client, SAVE, {"password": PW}, headers={**HDR, **PUBLIC})
    assert r.status_code == 403  # lan_only ชั้นนอกสุด (ไม่ใช่ 401)


# ---------------------------------------------------------------- รูปแบบคำขอ

@pytest.mark.parametrize("body", [
    [], "x", {"values": []}, {"values": {"DB_HOST": 5}}, {"values": {"DB_HOST": None}},
    {"clear": "MOPH_CLIENT_KEY"}, {"clear": [5]}, {"password": 123}, {"force": "yes"},
    {"values": {str(i): "x" for i in range(100)}},
])
def test_save_bad_request_shapes(admin, path, body):
    if isinstance(body, dict):
        body = {"password": PW, **body}
    r = post(admin, SAVE, body)
    assert r.status_code == 400 and r.get_json() == {"error": "bad_request"}
    assert not path.exists()


def test_save_non_json_body_is_bad_request(admin, path):
    r = admin.post(SAVE, data="{not json", headers=HDR, content_type="application/json")
    assert r.status_code == 400 and r.get_json()["error"] == "bad_request"


# ---------------------------------------------------------------- รหัสผ่าน + ตัวจำกัด

def test_save_wrong_password_writes_nothing_and_counts_throttle(admin, path, store, hook):
    r = save(admin, {"DENGUE_BASE_URL": "https://x.example/d"}, password="wrong-password")
    assert r.status_code == 403 and r.get_json()["error"] == "password"
    r = save(admin, {"DENGUE_BASE_URL": "https://x.example/d"}, password="")
    assert r.status_code == 403 and r.get_json()["error"] == "password"
    r = post(admin, SAVE, {"values": {"DENGUE_BASE_URL": "https://x.example/d"}})  # ไม่มี password
    assert r.status_code == 403 and r.get_json()["error"] == "password"
    assert not path.exists() and store.audit() == [] and hook.calls == 0
    throttle = admin.application.extensions["login_throttle"]
    assert len(throttle._fails[("settings", "boss")]) == 3
    # ใส่ถูกแล้วล้างตัวนับ
    assert save(admin, {"DENGUE_BASE_URL": "https://x.example/d"}).status_code == 200
    assert ("settings", "boss") not in throttle._fails


def test_save_throttled_after_five_wrong_passwords(admin, path, store):
    for _ in range(5):
        assert save(admin, {"DENGUE_BASE_URL": "https://x.example/d"},
                    password="nope").status_code == 403
    # ครั้งที่ 6: แม้รหัสถูกก็ถูกล็อก
    r = save(admin, {"DENGUE_BASE_URL": "https://x.example/d"})
    assert r.status_code == 429
    j = r.get_json()
    assert j["error"] == "throttled" and j["retry_after"] > 0 and "นาที" in j["message"]
    assert not path.exists()
    # revert ใช้ตัวนับเดียวกัน
    assert post(admin, REVERT, {"password": PW}).status_code == 429
    # ยังล็อกอยู่ แม้ใส่รหัสถูก (ซ้ำอีกครั้ง) และไม่มีอะไรถูกเขียน
    assert save(admin, {"DENGUE_BASE_URL": "https://x.example/d"}).status_code == 429
    assert not path.exists()


def test_throttle_lock_is_per_user(admin, store, login_as):
    for _ in range(5):
        assert save(admin, {"DENGUE_BASE_URL": "https://x.example/d"},
                    password="nope").status_code == 403
    assert save(admin, {"DENGUE_BASE_URL": "https://x.example/d"}).status_code == 429
    # ผู้ดูแลอีกคน (ล็อกอินจริงในไคลเอนต์อื่น) บันทึกได้ตามปกติ
    store.add_user("other", PW, "admin")
    other = admin.application.test_client()
    login_as(other, "other", "admin")
    r = save(other, {"DENGUE_BASE_URL": "https://other.example/d"})
    assert r.status_code == 200 and r.get_json()["changed"] == ["DENGUE_BASE_URL"]
    # boss ยังถูกล็อก (แม้รหัสถูก)
    assert save(admin, {"DENGUE_BASE_URL": "https://boss.example/d"}).status_code == 429
    assert settings_store.read_overlay(admin.application.config["SETTINGS_PATH"]).values[
        "DENGUE_BASE_URL"] == "https://other.example/d"


def test_throttle_key_is_lowercased_username(admin):
    save(admin, {"DENGUE_BASE_URL": "https://x.example/d"}, password="nope")
    assert ("settings", "boss") in admin.application.extensions["login_throttle"]._fails


# ---------------------------------------------------------------- บันทึก

def test_save_blank_secret_keeps_existing_value(admin, path):
    settings_store.write_overlay(path, {"DB_PASS": "old-secret-pw", "MOPH_CLIENT_KEY": "old-client-key-1"}, "seed")
    r = save(admin, {"DB_PASS": "", "MOPH_CLIENT_KEY": "   ", "DENGUE_BASE_URL": "https://x.example/d"})
    assert r.status_code == 200 and r.get_json()["changed"] == ["DENGUE_BASE_URL"]
    ov = settings_store.read_overlay(path).values
    assert ov["DB_PASS"] == "old-secret-pw" and ov["MOPH_CLIENT_KEY"] == "old-client-key-1"
    assert ov["DENGUE_BASE_URL"] == "https://x.example/d"


def test_save_clear_flag_clears_value(admin, path):
    settings_store.write_overlay(path, {"MOPH_CLIENT_KEY": "old-client-key-1", "DB_PASS": "old-pw"}, "seed")
    r = save(admin, clear=["MOPH_CLIENT_KEY"])
    assert r.status_code == 200 and r.get_json()["changed"] == ["MOPH_CLIENT_KEY"]
    ov = settings_store.read_overlay(path).values
    assert ov["MOPH_CLIENT_KEY"] == "" and ov["DB_PASS"] == "old-pw"
    # DB_PASS ล้างได้ต้องผ่านด่าน DB เช่นกัน
    assert save(admin, clear=["DB_PASS"]).status_code == 409
    assert save(admin, clear=["DB_PASS"], force=True).status_code == 200
    assert settings_store.read_overlay(path).values["DB_PASS"] == ""


def test_save_clear_conflicting_with_new_value_is_invalid(admin, path):
    r = save(admin, {"MOPH_CLIENT_KEY": "brand-new-key-123"}, clear=["MOPH_CLIENT_KEY"])
    assert r.status_code == 400 and r.get_json()["error"] == "invalid"
    assert not path.exists()


def test_save_rejects_unknown_key_and_does_not_write(admin, path, store):
    r = save(admin, {"DENGUE_BASE_URL": "https://x.example/d", "SECRET_KEY": "x" * 30})
    j = r.get_json()
    assert r.status_code == 400 and j["error"] == "invalid" and j["key"] is None
    assert "SECRET_KEY" not in r.get_data(as_text=True)
    assert not path.exists()
    for clear in (["DB_HOST"], ["SECRET_KEY"], ["DENGUE_BASE_URL"], ["nope"]):
        r = save(admin, clear=clear)
        assert r.status_code == 400 and r.get_json()["error"] == "invalid", clear
    assert not path.exists() and store.audit() == []


def test_save_blank_non_secret_is_400(admin, path):
    for key in ("DENGUE_BASE_URL", "DB_HOST", "DENGUE_START_FROM"):
        r = save(admin, {key: ""}, force=True)
        assert r.status_code == 400 and r.get_json()["key"] == key
        r = save(admin, {key: "   "}, force=True)
        assert r.status_code == 400 and r.get_json()["key"] == key
    assert not path.exists()


def test_save_rejects_buddhist_year_start_from_400(admin, path):
    r = save(admin, {"DENGUE_START_FROM": "2569-11-09"})
    j = r.get_json()
    assert r.status_code == 400 and j["error"] == "invalid" and j["key"] == "DENGUE_START_FROM"
    assert "ค.ศ." in j["message"] and "2569-11-09" not in r.get_data(as_text=True)
    assert not path.exists()


def test_save_one_invalid_value_writes_nothing(admin, path):
    r = save(admin, {"DENGUE_BASE_URL": "https://x.example/d", "DB_PORT": "99999"}, force=True)
    assert r.status_code == 400 and r.get_json()["key"] == "DB_PORT"
    assert not path.exists()


def test_save_db_change_failing_server_side_test_is_409(admin, path, store, hook, db_spy):
    """ด่านฝั่งเซิร์ฟเวอร์ (R1): เปลี่ยน DB_* แล้วทดสอบต่อไม่ผ่าน -> 409 พร้อมขั้นตอน ไม่เขียนอะไร (ดูเทสต์เต็มใน test_settings_tests_api)"""
    for values in ({"DB_HOST": "10.1.2.3", "DB_PASS": "pw-1"}, {"DB_PASS": "some-new-password"},
                   {"DB_PORT": "3307", "DB_PASS": "pw-2"}):
        r = save(admin, values)
        j = r.get_json()
        assert r.status_code == 409 and j["error"] == "untested" and j["steps"]
    # เปลี่ยนโฮสต์/พอร์ตโดยไม่ส่งรหัสใหม่ = 400 (R8: ไม่เอารหัสที่เก็บไว้ไปต่อปลายทางใหม่) ไม่ต่อฐาน
    calls = len(db_spy.calls)
    r = save(admin, {"DB_HOST": "10.1.2.3"})
    assert r.status_code == 400 and r.get_json()["error"] == "password_required"
    assert len(db_spy.calls) == calls
    assert not path.exists() and store.audit() == [] and hook.calls == 0
    # คีย์ที่ไม่ใช่ DB_* ไม่ติดด่านนี้ (และไม่ต่อฐาน)
    calls = len(db_spy.calls)
    assert save(admin, {"DENGUE_DATA_AUDITED": "1"}).status_code == 200
    assert len(db_spy.calls) == calls
    # ธงจากหน้าเว็บไม่ใช่การควบคุม: tested_ok ถูกเมิน
    r = save(admin, {"DB_HOST": "10.1.2.3", "DB_PASS": "pw-1"}, tested_ok=True)
    assert r.status_code == 409
    assert "DB_HOST" not in settings_store.read_overlay(path).values
    # ทดสอบผ่านจริงจึงบันทึกได้ (DB_USER/DB_PASS ต้องมีครบจึงผ่านขั้น "ตั้งค่าครบ")
    db_spy.ok = True
    r = save(admin, {"DB_HOST": "10.1.2.3", "DB_USER": "reader", "DB_PASS": "pw-1"})
    assert r.status_code == 200
    assert settings_store.read_overlay(path).values["DB_HOST"] == "10.1.2.3"


def test_save_with_force_ok(admin, path):
    r = save(admin, {"DB_HOST": "10.9.9.9", "DB_PASS": "p@ss word 1"}, force=True)
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] is True and j["changed"] == ["DB_HOST", "DB_PASS"]
    assert j["restart"] == "manual"
    ov = settings_store.read_overlay(path)
    assert ov.values["DB_HOST"] == "10.9.9.9" and ov.values["DB_PASS"] == "p@ss word 1"
    assert ov.updated_by == "boss"


def test_save_blank_db_pass_does_not_trigger_db_gate(admin, path, db_spy):
    r = save(admin, {"DB_PASS": "", "DENGUE_DATA_AUDITED": "1"})
    assert r.status_code == 200 and r.get_json()["changed"] == ["DENGUE_DATA_AUDITED"]
    assert db_spy.calls == []


def test_save_nothing_changed_does_not_write_or_restart(admin, path, store, hook):
    r = post(admin, SAVE, {"values": {"DB_PASS": ""}, "clear": [], "password": PW,
                           }, environ_overrides=GUNICORN)
    assert r.status_code == 200 and r.get_json() == {"ok": True, "changed": [], "restart": "none"}
    r = save(admin)
    assert r.get_json() == {"ok": True, "changed": [], "restart": "none"}
    time.sleep(0.05)
    assert not path.exists() and store.audit() == [] and hook.calls == 0


def test_save_writes_audit_with_key_names_only(admin, store):
    secret = "UNIQUE-AUDIT-SECRET-5521"
    save(admin, {"DENGUE_BASE_URL": "https://x.example/d", "MOPH_CLIENT_KEY": secret,
                 "DB_PASS": secret + "-db"}, clear=["MOPH_SECRET_KEY"], force=True)
    rows = store.audit()
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "settings_change" and row["user"] == "boss" and row["case_id"] is None
    # JSON ของไคลเอนต์เรียงคีย์ตามตัวอักษร: ค่าที่ตั้งตามลำดับนั้น แล้วตามด้วยคีย์ที่ล้าง
    assert row["detail"] == "DB_PASS,DENGUE_BASE_URL,MOPH_CLIENT_KEY,MOPH_SECRET_KEY"
    assert secret not in json.dumps(rows, ensure_ascii=False)


def test_save_audit_failure_does_not_lose_restart(admin, app, store, hook, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("audit down UNIQUE-AUD")
    monkeypatch.setattr(type(store), "add_audit", boom)
    caplog.set_level(logging.DEBUG)
    r = post(admin, SAVE, {"values": {"DENGUE_DATA_AUDITED": "1"}, "password": PW},
             environ_overrides=GUNICORN)
    assert r.status_code == 200 and r.get_json()["restart"] == "scheduled"
    assert hook.wait()
    assert "UNIQUE-AUD" not in caplog.text and "RuntimeError" in caplog.text
    # log ต้องบอกว่า audit ของ action ไหนล้ม (ชื่อ action + ชนิด exception เท่านั้น)
    assert "settings_change" in caplog.text


# ---------------------------------------------------------------- restart

def test_save_schedules_restart_once_under_gunicorn_only(admin, hook):
    # ไม่ใช่ gunicorn (เช่น python run.py / dev server) -> manual ไม่มี timer
    r = save(admin, {"DENGUE_DATA_AUDITED": "1"})
    assert r.get_json()["restart"] == "manual"
    time.sleep(0.1)
    assert hook.calls == 0
    # os.environ ไม่มีผล (ตรวจจาก WSGI environ)
    os.environ["SERVER_SOFTWARE"] = "gunicorn/22"
    try:
        r = save(admin, {"DENGUE_DATA_AUDITED": "0"})
        assert r.get_json()["restart"] == "manual"
    finally:
        del os.environ["SERVER_SOFTWARE"]
    time.sleep(0.1)
    assert hook.calls == 0
    # gunicorn (WSGI environ) -> scheduled และเรียก hook ครั้งเดียว
    r = post(admin, SAVE, {"values": {"DENGUE_DATA_AUDITED": "1"}, "password": PW},
             environ_overrides=GUNICORN)
    assert r.get_json() == {"ok": True, "changed": ["DENGUE_DATA_AUDITED"], "restart": "scheduled"}
    assert hook.wait()
    time.sleep(0.15)
    assert hook.calls == 1


def test_failed_requests_never_schedule_restart(admin, hook):
    post(admin, SAVE, {"values": {"DENGUE_START_FROM": "2569-01-01"}, "password": PW},
         environ_overrides=GUNICORN)
    post(admin, SAVE, {"values": {"DENGUE_DATA_AUDITED": "1"}, "password": "bad"},
         environ_overrides=GUNICORN)
    post(admin, SAVE, {"values": {"DB_HOST": "10.0.0.1"}, "password": PW}, environ_overrides=GUNICORN)
    post(admin, REVERT, {"password": PW}, environ_overrides=GUNICORN)  # ไม่มี prev
    time.sleep(0.15)
    assert hook.calls == 0


def test_restart_uses_configured_delay(admin, app, hook):
    app.config["RESTART_DELAY"] = 0.4
    post(admin, SAVE, {"values": {"DENGUE_DATA_AUDITED": "1"}, "password": PW},
         environ_overrides=GUNICORN)
    time.sleep(0.1)
    assert hook.calls == 0
    assert hook.wait(2.0) and hook.calls == 1


def test_hook_exception_is_swallowed_and_logged_by_type(admin, app, caplog):
    done = threading.Event()

    def bad():
        done.set()
        raise RuntimeError("hook failed UNIQUE-HOOK")
    app.config["RESTART_HOOK"] = bad
    caplog.set_level(logging.DEBUG)
    r = post(admin, SAVE, {"values": {"DENGUE_DATA_AUDITED": "1"}, "password": PW},
             environ_overrides=GUNICORN)
    assert r.status_code == 200 and done.wait(2.0)
    time.sleep(0.1)
    assert "UNIQUE-HOOK" not in caplog.text and "RuntimeError" in caplog.text


def test_default_restart_hook_sends_sigterm_to_self(monkeypatch):
    sent = []
    monkeypatch.setattr(settings_views.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    settings_views.default_restart_hook()
    assert sent == [(os.getpid(), signal.SIGTERM)]


def test_create_app_defaults(tmp_path, store):
    settings = load_settings({"SECRET_KEY": "test-secret-key-0123456789",
                              "DENGUE_DB_PATH": str(tmp_path / "data" / "d.db")})
    app = create_app(settings=settings, store=store)
    assert app.config["SETTINGS_PATH"] == tmp_path / "data" / "settings.json"
    assert app.config["RESTART_HOOK"] is settings_views.default_restart_hook
    assert app.config["RESTART_DELAY"] == 1.5


# ---------------------------------------------------------------- revert

def test_revert_restores_prev_and_requires_password(admin, path, store, hook):
    # ยังไม่มี prev -> 409 no_prev
    r = post(admin, REVERT, {"password": PW})
    assert r.status_code == 409 and r.get_json() == {"error": "no_prev"}
    assert save(admin, {"DENGUE_BASE_URL": "https://a.example/d"}).status_code == 200
    assert save(admin, {"DENGUE_BASE_URL": "https://b.example/d"}).status_code == 200
    # รหัสผิด -> 403 ไม่เปลี่ยน
    r = post(admin, REVERT, {"password": "wrong"})
    assert r.status_code == 403 and r.get_json()["error"] == "password"
    assert settings_store.read_overlay(path).values["DENGUE_BASE_URL"] == "https://b.example/d"
    # รหัสถูก -> ย้อน
    r = post(admin, REVERT, {"password": PW})
    assert r.status_code == 200 and r.get_json() == {"ok": True, "restart": "manual"}
    assert settings_store.read_overlay(path).values["DENGUE_BASE_URL"] == "https://a.example/d"
    actions = [(a["action"], a["detail"]) for a in store.audit()]
    assert actions[-1] == ("settings_revert", "ย้อนกลับค่าก่อนหน้า")
    assert [a for a, _ in actions].count("settings_change") == 2


def test_revert_schedules_restart_once_under_gunicorn(admin, hook):
    save(admin, {"DENGUE_BASE_URL": "https://a.example/d"})
    save(admin, {"DENGUE_BASE_URL": "https://b.example/d"})
    r = post(admin, REVERT, {"password": PW}, environ_overrides=GUNICORN)
    assert r.get_json() == {"ok": True, "restart": "scheduled"}
    assert hook.wait()
    time.sleep(0.15)
    assert hook.calls == 1


def test_revert_bad_body(admin):
    assert post(admin, REVERT, {"password": 5}).status_code == 400
    assert post(admin, REVERT, []).status_code == 400


# ---------------------------------------------------------------- ล็อกการเขียน / ข้อผิดพลาด / ความลับ

def test_writes_are_serialized_under_module_lock(admin, monkeypatch):
    seen = []
    real = settings_store.write_overlay

    def spy(*a, **k):
        seen.append(settings_views._WRITE_LOCK.locked())
        return real(*a, **k)
    monkeypatch.setattr(settings_store, "write_overlay", spy)
    save(admin, {"DENGUE_DATA_AUDITED": "1"})
    assert seen == [True] and not settings_views._WRITE_LOCK.locked()

    seen.clear()
    real_revert = settings_store.revert_overlay

    def spy_revert(*a, **k):
        seen.append(settings_views._WRITE_LOCK.locked())
        return real_revert(*a, **k)
    monkeypatch.setattr(settings_store, "revert_overlay", spy_revert)
    save(admin, {"DENGUE_DATA_AUDITED": "0"})
    seen.clear()  # สนใจเฉพาะ revert (save ข้างบนก็เรียก write_overlay ที่ถูกสอดอยู่)
    post(admin, REVERT, {"password": PW})
    assert seen == [True] and not settings_views._WRITE_LOCK.locked()


def test_clean_username():
    f = settings_views.clean_username
    assert f("boss") == "boss"
    assert f("a\ud800b\x00c\n\x7fd") == "abcd"
    assert len(f("x" * 200)) == 64
    assert f("\ud800") == "unknown"
    f("\ud800x").encode("utf-8")


def test_unexpected_exception_is_fixed_500_without_leak(admin, path, monkeypatch, caplog):
    secret = "UNIQUE-500-SECRET-abc"

    def boom(*a, **k):
        raise RuntimeError("boom " + secret)
    monkeypatch.setattr(settings_store, "write_overlay", boom)
    caplog.set_level(logging.DEBUG)
    r = save(admin, {"DENGUE_DATA_AUDITED": "1", "MOPH_CLIENT_KEY": secret})
    j = r.get_json()
    assert r.status_code == 500 and j["error"] == "server" and j["message"]
    assert "boom" not in r.get_data(as_text=True) and secret not in r.get_data(as_text=True)
    assert secret not in caplog.text and "RuntimeError" in caplog.text
    monkeypatch.undo()
    # revert ก็เช่นกัน
    monkeypatch.setattr(settings_store, "revert_overlay", boom)
    r = post(admin, REVERT, {"password": PW})
    assert r.status_code == 500 and r.get_json()["error"] == "server"


def test_errors_never_echo_submitted_values(admin, path):
    u = "UNIQUE-ECHO-7781"
    cases = [
        {"DENGUE_BASE_URL": f"javascript:alert('{u}')"},
        {"DB_HOST": f"bad host {u}!"},
        {"DB_PORT": f"port-{u}"},
        {"DB_NAME": f"na me {u}"},
        {"DENGUE_START_FROM": f"date-{u}"},
        {"DENGUE_DATA_AUDITED": f"yes-{u}"},
        {"MOPH_CLIENT_KEY": f"short {u}"},
        {u: "x"},                       # คีย์ที่ไม่รู้จัก
    ]
    for values in cases:
        r = save(admin, values, force=True)
        assert r.status_code == 400, values
        assert u not in r.get_data(as_text=True), values
    r = save(admin, {"DENGUE_BASE_URL": "https://x.example/d"}, clear=[u])
    assert r.status_code == 400 and u not in r.get_data(as_text=True)
    r = save(admin, password=u)
    assert r.status_code == 403 and u not in r.get_data(as_text=True)
    assert not path.exists()


def test_no_secret_in_any_response_log_or_audit(admin, app, path, store, caplog):
    caplog.set_level(logging.DEBUG)
    s_db = "UNIQUE-DB-PW-<script>'\"-3141"
    s_ck = "UNIQUE-CLIENT-KEY-2718"
    s_sk = "UNIQUE-SECRET-KEY-1618"
    bodies = []

    def grab(r):
        bodies.append(r.get_data(as_text=True))
        return r

    # สำเร็จ
    r = grab(save(admin, {"DB_PASS": s_db, "MOPH_CLIENT_KEY": s_ck, "MOPH_SECRET_KEY": s_sk,
                          "DB_HOST": "10.1.1.1"}, force=True))
    assert r.status_code == 200
    # ล้มเหลวทุกแบบพร้อมแนบความลับไปในคำขอ
    grab(save(admin, {"DB_PASS": s_db, "MOPH_CLIENT_KEY": s_ck}, password="wrong"))          # 403
    grab(save(admin, {"DB_PASS": s_db, "DB_PORT": "x"}, force=True))                         # 400 invalid
    grab(save(admin, {"DB_PASS": s_db, "NOPE": s_ck}, force=True))                           # 400 unknown
    grab(save(admin, {"DB_PASS": s_db}))                                                     # 409 untested (+ steps)
    grab(post(admin, SAVE, {"values": {"DB_PASS": 5, "MOPH_CLIENT_KEY": s_ck}, "password": PW}))  # 400 shape
    grab(post(admin, SAVE, {"values": {"DB_PASS": s_db}, "password": PW}, headers={}))       # 403 csrf
    grab(admin.post(SAVE, data={"values": s_db}, headers=HDR))                               # 415
    for _ in range(5):
        grab(save(admin, {"DB_PASS": s_db}, password="wrong"))
    grab(save(admin, {"DB_PASS": s_db, "MOPH_SECRET_KEY": s_sk}))                            # 429
    # หน้า + revert
    admin.application.extensions["login_throttle"].clear(("settings", "boss"))
    grab(admin.get("/settings"))
    grab(post(admin, REVERT, {"password": PW}))
    grab(post(admin, REVERT, {"password": "wrong-pw-" + s_db}))

    secrets = (s_db, s_ck, s_sk, PW, "wrong-pw-" + s_db)
    for body in bodies:
        for s in secrets:
            assert s not in body
    for s in secrets:
        assert s not in caplog.text
        assert s not in json.dumps(store.audit(), ensure_ascii=False)
    # repr ของ config ที่ตอบสนองได้ (แอปไม่เก็บค่าที่เพิ่งบันทึกไว้ในหน่วยความจำ)
    cfg = repr(app.config)
    for s in secrets:
        assert s not in cfg
    # แต่ไฟล์เองมีค่าจริง (ผ่าน settings_store)
    assert settings_store.read_overlay(path).values["DB_PASS"] == s_db


# ---------------------------------------------------------------- Fix round 1: ขนาดคำขอ

def padded_body(size, password=PW):
    """JSON ที่ยาว `size` ไบต์พอดี (เติมคีย์ pad ที่เซิร์ฟเวอร์ไม่สนใจ)"""
    doc = {"password": password, "pad": ""}
    base = len(json.dumps(doc, separators=(",", ":")).encode())
    doc["pad"] = "a" * (size - base)
    raw = json.dumps(doc, separators=(",", ":")).encode()
    assert len(raw) == size
    return raw


def raw_post(client, url, raw, **kw):
    return client.post(url, data=raw, content_type="application/json", headers=HDR, **kw)


@pytest.mark.parametrize("url", [SAVE, REVERT])
def test_oversized_body_is_413_before_anything_happens(admin, path, store, hook, monkeypatch, url):
    calls = []
    monkeypatch.setattr(store, "check_user", lambda *a, **k: calls.append(a) or "admin")
    # รหัสผิดและใหญ่เกิน: ต้อง 413 โดยไม่ตรวจรหัส ไม่นับตัวจำกัด ไม่เขียน ไม่ audit
    r = raw_post(admin, url, padded_body(settings_views.MAX_BODY_BYTES + 1, password="wrong"))
    assert r.status_code == 413
    j = r.get_json()
    assert j["error"] == "too_large" and j["message"]
    assert calls == []
    assert ("settings", "boss") not in admin.application.extensions["login_throttle"]._fails
    assert not path.exists() and store.audit() == [] and hook.calls == 0
    # ใหญ่มาก
    assert raw_post(admin, url, b"{" + b" " * 300_000 + b"}").status_code == 413


def test_body_exactly_at_limit_is_accepted(admin, path):
    raw = padded_body(settings_views.MAX_BODY_BYTES)
    r = raw_post(admin, SAVE, raw)
    assert r.status_code == 200 and r.get_json() == {"ok": True, "changed": [], "restart": "none"}
    # ที่ขีดจำกัดพอดีของ revert: ผ่านด่านขนาดและตรวจรหัส (ไม่มี prev -> 409)
    r = raw_post(admin, REVERT, raw)
    assert r.status_code == 409 and r.get_json() == {"error": "no_prev"}


def test_chunked_body_without_content_length_is_413(admin, path, monkeypatch, store):
    import io
    calls = []
    monkeypatch.setattr(store, "check_user", lambda *a, **k: calls.append(a) or "admin")
    for size in (200, settings_views.MAX_BODY_BYTES + 1):
        r = admin.post(SAVE, data=io.BytesIO(padded_body(size)), content_type="application/json",
                       headers={**HDR, "Transfer-Encoding": "chunked"})
        assert r.status_code == 413, size
    assert calls == [] and not path.exists()


def test_stream_longer_than_declared_is_cut_off_by_limit(admin, path, monkeypatch, store):
    """ไคลเอนต์แจ้ง Content-Length เล็กแต่ stream ยาวกว่า MAX: อ่านได้ไม่เกินขีดจำกัด"""
    calls = []
    monkeypatch.setattr(store, "check_user", lambda *a, **k: calls.append(a) or "admin")
    raw = padded_body(settings_views.MAX_BODY_BYTES + 5000)
    r = admin.post(SAVE, data=raw, content_type="application/json", headers=HDR,
                   environ_overrides={"CONTENT_LENGTH": "100"})
    assert r.status_code in (400, 413) and calls == []
    assert not path.exists()


# ---------------------------------------------------------------- Fix round 1: R6 a-e

@pytest.mark.parametrize("url", [SAVE, REVERT])
def test_unencodable_password_is_wrong_password_not_500(admin, path, url):
    throttle = admin.application.extensions["login_throttle"]
    r = post(admin, url, {"values": {"DENGUE_DATA_AUDITED": "1"}, "password": "pw\ud800x"})
    assert r.status_code == 403 and r.get_json()["error"] == "password"
    assert len(throttle._fails[("settings", "boss")]) == 1
    assert not path.exists()


def test_throttle_key_cannot_collide_with_login_keys(client, store, login_as, app):
    """/login ใช้คีย์ `user|ip`; ผู้ดูแลชื่อ `x|203.0.113.9` ต้องไม่ถูกล็อกจากการพยายามล็อกอินชื่อ `settings:x`"""
    name = "x|203.0.113.9"
    store.add_user(name, PW, "admin")
    login_as(client, name, "admin")
    other = app.test_client()
    for _ in range(6):
        r = other.post("/login", data={"username": "settings:x", "password": "bad"},
                       headers={"X-Forwarded-For": "203.0.113.9"})
        assert r.status_code in (200, 429)
    throttle = app.extensions["login_throttle"]
    assert throttle.retry_after("settings:x|203.0.113.9") > 0  # /login ถูกล็อกจริง
    # ... แต่การเขียนค่าตั้งของผู้ดูแลชื่อนั้นไม่ถูกล็อก
    r = save(client, {"DENGUE_DATA_AUDITED": "1"})
    assert r.status_code == 200 and r.get_json()["changed"] == ["DENGUE_DATA_AUDITED"]


def test_second_save_while_restart_pending_does_not_arm_another_timer(admin, hook):
    r1 = post(admin, SAVE, {"values": {"DENGUE_DATA_AUDITED": "1"}, "password": PW},
              environ_overrides=GUNICORN)
    assert r1.get_json()["restart"] == "scheduled"
    assert hook.wait()
    r2 = post(admin, SAVE, {"values": {"DENGUE_DATA_AUDITED": "0"}, "password": PW},
              environ_overrides=GUNICORN)
    assert r2.get_json() == {"ok": True, "changed": ["DENGUE_DATA_AUDITED"], "restart": "scheduled"}
    # revert ระหว่างรอก็เช่นกัน
    r3 = post(admin, REVERT, {"password": PW}, environ_overrides=GUNICORN)
    assert r3.get_json() == {"ok": True, "restart": "scheduled"}
    time.sleep(0.2)
    assert hook.calls == 1  # ตั้ง Timer ครั้งเดียว
    # การเขียนยังเกิดขึ้นจริง: หลัง revert กลับเป็นค่าก่อนหน้า ("1")
    assert settings_store.read_overlay(admin.application.config["SETTINGS_PATH"]).values[
        "DENGUE_DATA_AUDITED"] == "1"


def test_pending_flag_resets_only_when_hook_fails(admin, app):
    calls = []
    events = [threading.Event(), threading.Event()]

    def failing():
        calls.append(1)
        events[len(calls) - 1].set()
        raise RuntimeError("hook failed")
    app.config["RESTART_HOOK"] = failing

    def go(val):
        return post(admin, SAVE, {"values": {"DENGUE_DATA_AUDITED": val}, "password": PW},
                    environ_overrides=GUNICORN)
    assert go("1").get_json()["restart"] == "scheduled"
    assert events[0].wait(2.0)
    time.sleep(0.15)  # ให้เธรด Timer ล้างธงหลัง hook ล้มเหลว
    assert go("0").get_json()["restart"] == "scheduled"
    assert events[1].wait(2.0)  # ตั้ง Timer ใหม่ได้เพราะธงถูกล้าง
    assert len(calls) == 2


def test_timer_start_failure_reports_manual_and_leaves_flag_clear(admin, monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("cannot start thread")
    monkeypatch.setattr(settings_views.threading, "Timer", Boom)
    r = post(admin, SAVE, {"values": {"DENGUE_DATA_AUDITED": "1"}, "password": PW},
             environ_overrides=GUNICORN)
    assert r.get_json()["restart"] == "manual"
    assert admin.application.extensions["settings_restart"]["pending"] is False


def test_settings_page_unexpected_error_is_fixed_500(admin, monkeypatch, caplog):
    secret = "UNIQUE-PAGE-SECRET-42"

    def boom(*a, **k):
        raise RuntimeError("boom " + secret)
    monkeypatch.setattr(settings_store, "describe", boom)
    caplog.set_level(logging.DEBUG)
    r = admin.get("/settings")
    body = r.get_data(as_text=True)
    assert r.status_code == 500 and "ข้อผิดพลาด" in body
    assert "boom" not in body and secret not in body and "settings-data" not in body
    assert secret not in caplog.text and "RuntimeError" in caplog.text


def test_resending_current_values_changes_nothing(admin, path, store, hook):
    # ค่าเดิมจาก overlay
    settings_store.write_overlay(path, {"DB_HOST": "10.1.2.3", "DENGUE_DATA_AUDITED": "1"}, "seed")
    before = path.read_bytes()
    r = post(admin, SAVE, {"values": {"DB_HOST": "10.1.2.3", "DENGUE_DATA_AUDITED": "1"},
                           "password": PW}, environ_overrides=GUNICORN)
    assert r.status_code == 200 and r.get_json() == {"ok": True, "changed": [], "restart": "none"}
    time.sleep(0.1)
    assert path.read_bytes() == before and store.audit() == [] and hook.calls == 0
    # I-2: ค่าเริ่มต้นของ config (source=default) ไม่ใช่ "ค่าที่ตั้งไว้" — ส่งซ้ำ = ตั้งค่าจริง (เขียนลงไฟล์)
    path.unlink()
    cfg = admin.application.config["SETTINGS"]
    r = save(admin, {"DENGUE_DATA_AUDITED": "0", "DENGUE_BASE_URL": cfg.base_url,
                     "DENGUE_START_FROM": cfg.start_from.isoformat()})
    assert sorted(r.get_json()["changed"]) == ["DENGUE_BASE_URL", "DENGUE_DATA_AUDITED", "DENGUE_START_FROM"]
    assert settings_store.read_overlay(path).values == {
        "DENGUE_DATA_AUDITED": "0", "DENGUE_BASE_URL": cfg.base_url,
        "DENGUE_START_FROM": cfg.start_from.isoformat()}


def test_changing_one_of_resent_values_writes_only_that_key(admin, path, hook):
    settings_store.write_overlay(path, {"DB_HOST": "10.1.2.3", "DENGUE_DATA_AUDITED": "1"}, "seed")
    r = post(admin, SAVE, {"values": {"DB_HOST": "10.1.2.3", "DENGUE_DATA_AUDITED": "0"},
                           "password": PW}, environ_overrides=GUNICORN)
    # DB_HOST เท่าเดิมจึงไม่ติดด่าน DB (ไม่ 409) และไม่อยู่ใน changed
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "changed": ["DENGUE_DATA_AUDITED"], "restart": "scheduled"}
    assert hook.wait()
    assert settings_store.read_overlay(path).values["DENGUE_DATA_AUDITED"] == "0"
    # เปลี่ยน DB_HOST จริงยังต้องผ่านด่าน: ไม่มีรหัสใหม่ = 400 (R8) · มีรหัสใหม่แต่ต่อไม่ได้ (ตัวต่อปลอม) = 409
    assert save(admin, {"DB_HOST": "10.9.9.9"}).status_code == 400
    assert save(admin, {"DB_HOST": "10.9.9.9", "DB_PASS": "pw-1"}).status_code == 409


def test_secret_same_value_is_still_written_when_nonblank(admin, path):
    """ความลับเทียบกับค่าเดิมไม่ได้ (write-only): ไม่ว่างหรือสั่งล้าง = เขียนเสมอ"""
    settings_store.write_overlay(path, {"MOPH_CLIENT_KEY": "same-client-key-1"}, "seed")
    r = save(admin, {"MOPH_CLIENT_KEY": "same-client-key-1"})
    assert r.get_json()["changed"] == ["MOPH_CLIENT_KEY"]


# ---------------------------------------------------------------- I-2: ค่าเริ่มต้น (source=default) ตั้งจากหน้าได้

def test_page_start_from_default_is_empty_with_fallback_and_unset_flag(admin, app):
    data, html = page_data(admin)
    item = {i["key"]: i for i in data["items"]}["DENGUE_START_FROM"]
    cfg = app.config["SETTINGS"]
    assert item["source"] == "default" and item["state"] == "unset"
    assert item["value"] is None and item["fallback"] == cfg.start_from.isoformat()
    assert data["start_from_default"] is True
    assert data["start_from_unset_warning"] == config.MSG_START_FROM_UNSET
    assert "ตั้งเป็นวัน go-live" in data["start_from_unset_warning"]
    # ช่องกรอกว่าง + คำเตือนสีส้ม role=status แสดงฝั่งเซิร์ฟเวอร์ (ไม่ต้องรอ JS)
    field = re.search(r'<input[^>]*id="f-DENGUE_START_FROM"[^>]*>', html).group(0)
    assert 'value=""' in field
    warn = re.search(r'<p[^>]*id="start-from-warning"[^>]*>(.*?)</p>', html, re.S)
    assert 'role="status"' in warn.group(0) and config.MSG_START_FROM_UNSET in warn.group(1)
    # คำอธิบายใต้ช่อง: ยังไม่ได้ตั้ง — ใช้วันที่เริ่มระบบ (<ค่าสำรอง>)
    assert f"ยังไม่ได้ตั้ง — ใช้วันที่เริ่มระบบ ({cfg.start_from.isoformat()})" in html
    # ตัวอื่นที่มีค่าสำรองคงการแสดงเดิม
    items = {i["key"]: i for i in data["items"]}
    assert items["DENGUE_BASE_URL"]["value"] == cfg.base_url and items["DENGUE_DATA_AUDITED"]["value"] == "0"


def test_page_start_from_set_in_overlay_or_env_has_no_unset_warning(admin, app, path):
    settings_store.write_overlay(path, {"DENGUE_START_FROM": "2026-10-01"}, "seed")
    data, html = page_data(admin)
    item = {i["key"]: i for i in data["items"]}["DENGUE_START_FROM"]
    assert item["value"] == "2026-10-01" and item["source"] == "overlay"
    assert data["start_from_default"] is False
    after_json = html.split('id="settings-data"', 1)[1].split("</script>", 1)[1]
    assert config.MSG_START_FROM_UNSET not in after_json
    assert "ยังไม่ได้ตั้ง — ใช้วันที่เริ่มระบบ" not in html
    # ตั้งจาก .env (base env) ก็ไม่ใช่ default
    app.config["SETTINGS_BASE_ENV"] = {**app.config["SETTINGS_BASE_ENV"], "DENGUE_START_FROM": "2026-09-01"}
    path.unlink()
    data, _ = page_data(admin)
    item = {i["key"]: i for i in data["items"]}["DENGUE_START_FROM"]
    assert item["source"] == "env" and data["start_from_default"] is False


def test_save_explicit_start_from_equal_to_fallback_is_written(admin, app, path, hook):
    """go-live วันนี้: ค่าที่พิมพ์เท่ากับ "วันเริ่มระบบ" ที่แสดงเป็นค่าสำรอง ต้องถูกบันทึก (ไม่ใช่ no-op)"""
    today = app.config["SETTINGS"].start_from.isoformat()
    r = post(admin, SAVE, {"values": {"DENGUE_START_FROM": today}, "password": PW}, environ_overrides=GUNICORN)
    assert r.status_code == 200 and r.get_json() == {"ok": True, "changed": ["DENGUE_START_FROM"],
                                                      "restart": "scheduled"}
    assert settings_store.read_overlay(path).values == {"DENGUE_START_FROM": today}
    assert hook.wait()
    # เมื่อตั้งแล้ว (source=overlay) ส่งซ้ำค่าเดิม = no-op เหมือนเดิม
    before = path.read_bytes()
    r = save(admin, {"DENGUE_START_FROM": today})
    assert r.get_json() == {"ok": True, "changed": [], "restart": "none"} and path.read_bytes() == before
    data, _ = page_data(admin)
    assert data["start_from_default"] is False


def test_save_data_audited_zero_over_default_is_written_but_overlay_equal_is_skipped(admin, path):
    r = save(admin, {"DENGUE_DATA_AUDITED": "0"})
    assert r.get_json()["changed"] == ["DENGUE_DATA_AUDITED"]
    assert settings_store.read_overlay(path).values == {"DENGUE_DATA_AUDITED": "0"}
    items = {i["key"]: i for i in page_data(admin)[0]["items"]}
    assert items["DENGUE_DATA_AUDITED"]["source"] == "overlay"
    # ตอนนี้อยู่ใน overlay แล้ว: ส่ง "0" ซ้ำ = ไม่เปลี่ยน
    assert save(admin, {"DENGUE_DATA_AUDITED": "0"}).get_json()["changed"] == []


def test_save_env_sourced_equal_value_is_still_skipped(admin, app, path):
    app.config["SETTINGS_BASE_ENV"] = {**app.config["SETTINGS_BASE_ENV"], "DENGUE_BASE_URL": "https://e.example/d"}
    assert save(admin, {"DENGUE_BASE_URL": "https://e.example/d"}).get_json()["changed"] == []
    assert not path.exists()


# ---------------------------------------------------------------- I-1: ส่งความลับผ่าน http ธรรมดา

LAN_IP = "192.168.70.135"


def _alert_bar(html):
    return re.search(r'<div[^>]*id="insecure-transport"[^>]*>(.*?)</div>', html, re.S)


def _page(client, headers=None, **env):
    html = client.get("/settings", headers=headers or {}, environ_overrides=env).get_data(as_text=True)
    data = json.loads(re.search(r'id="settings-data">(.*?)</script>', html, re.S).group(1))
    return data, html


def test_page_warns_when_plain_http_from_a_non_loopback_client(admin):
    data, html = _page(admin, REMOTE_ADDR=LAN_IP)
    m = _alert_bar(html)
    assert m and 'role="alert"' in m.group(0) and "การเชื่อมต่อนี้ไม่เข้ารหัส (http)" in m.group(1)
    assert "SSH tunnel" in m.group(1) and "4.7" in m.group(1)
    assert "ข้อความธรรมดา" in m.group(1)
    assert data["insecure_transport"] is True


def test_page_has_no_insecure_bar_for_loopback_or_https(admin):
    def check(expect, headers=None, **env):
        data, html = _page(admin, headers, **env)
        assert data["insecure_transport"] is expect, (headers, env)
        assert (_alert_bar(html) is not None) is expect, (headers, env)
    check(False)                                                   # ตัวทดสอบ: 127.0.0.1 (เช่น SSH tunnel)
    check(False, REMOTE_ADDR="127.0.0.1")
    check(False, REMOTE_ADDR="127.8.9.10")
    check(False, {"X-Forwarded-Proto": "https"}, REMOTE_ADDR=LAN_IP)      # https (หลัง Apache) ไม่เตือน
    check(False, {"X-Forwarded-Proto": "https", "X-Forwarded-For": LAN_IP}, REMOTE_ADDR="127.0.0.1")
    check(True, REMOTE_ADDR=LAN_IP)
    check(True, REMOTE_ADDR="::ffff:192.168.70.135")
    # proxy บนเครื่องเดียวกันที่ส่งต่อไคลเอนต์ LAN ผ่าน http: remote_addr เป็น loopback แต่ผู้ใช้จริงไม่ใช่
    check(True, {"X-Forwarded-For": LAN_IP}, REMOTE_ADDR="127.0.0.1")
    check(False, {"X-Forwarded-For": "127.0.0.1"}, REMOTE_ADDR="127.0.0.1")


def test_insecure_transport_helper_edge_cases(app):
    """ด่าน LAN ของหน้าตัดสินที่อยู่ ::1 / ที่อ่านไม่ได้ทิ้งก่อนถึงแถบนี้ — ทดสอบตัวตัดสินตรง ๆ"""
    def run(remote, headers=None):
        with app.test_request_context("/settings", headers=headers or {}, environ_overrides={"REMOTE_ADDR": remote}):
            return settings_views._insecure_transport()
    assert run("::1") is False and run("::ffff:127.0.0.1") is False and run("127.0.0.1") is False
    assert run("not-an-ip") is True and run("") is True and run("10.0.0.5") is True
    assert run("::1", {"X-Forwarded-For": "10.0.0.5"}) is True
    assert run("::1", {"X-Forwarded-For": ""}) is True               # มีหัวแต่ไม่มีรายการ = ไม่รู้ตัวตน


# ---------------------------------------------------------------- M-1: ตรวจกติการหัสผ่านซ้ำใน lock

CONFLICT_MSG = "ค่าถูกเปลี่ยนโดยผู้ดูแลคนอื่นระหว่างบันทึก โหลดหน้าใหม่แล้วลองอีกครั้ง"


def test_save_rechecks_endpoint_inside_write_lock_and_conflicts(admin, path, store, hook, monkeypatch, db_spy):
    """แอดมิน B ส่งเปลี่ยนรหัสผ่านอย่างเดียว; ระหว่างด่านทดสอบกับ lock แอดมิน A เปลี่ยนโฮสต์+รหัสแทรกเข้ามา
    -> B ต้องไม่เขียนทับ (รหัสของ B จะไปอยู่กับโฮสต์ที่ B ไม่ได้ทดสอบ) = 409 conflict ไม่เขียนอะไร"""
    import settings_checks
    settings_store.write_overlay(path, {"DB_HOST": "10.1.1.1", "DB_USER": "reader", "DB_PASS": "old-pw"}, "seed")
    real = settings_checks.run_db_test
    db_spy.ok = True

    def racing(env, **kw):
        steps = real(env, **kw)
        settings_store.write_overlay(path, {"DB_HOST": "10.9.9.9", "DB_PASS": "A-pw"}, "adminA")  # แอดมิน A มาแทรก
        return steps
    monkeypatch.setattr(settings_checks, "run_db_test", racing)

    r = save(admin, {"DB_PASS": "B-pw"})
    assert r.status_code == 409 and r.get_json() == {"error": "conflict", "message": CONFLICT_MSG}
    ov = settings_store.read_overlay(path)
    assert ov.values["DB_HOST"] == "10.9.9.9" and ov.values["DB_PASS"] == "A-pw" and ov.updated_by == "adminA"
    assert store.audit() == [] and hook.calls == 0 and not settings_views._WRITE_LOCK.locked()
    assert "B-pw" not in r.get_data(as_text=True)


def test_save_conflict_also_applies_to_force(admin, app, path, store):
    settings_store.write_overlay(path, {"DB_HOST": "10.1.1.1", "DB_USER": "reader", "DB_PASS": "old-pw"}, "seed")
    real = app.config["ENV_PROVIDER"]
    calls = []

    def provider():
        env = dict(real())
        calls.append(1)
        if len(calls) >= 2:      # ครั้งที่สอง (ใน lock) พบว่าปลายทางถูกคนอื่นย้ายไปแล้ว
            env["DB_HOST"] = "10.7.7.7"
        return env
    app.config["ENV_PROVIDER"] = provider
    before = path.read_bytes()
    r = save(admin, {"DB_NAME": "hosz"}, force=True)
    assert r.status_code == 409 and r.get_json()["error"] == "conflict"
    assert len(calls) == 2 and path.read_bytes() == before and store.audit() == []


def test_save_without_concurrent_change_still_succeeds_after_recheck(admin, path, db_spy):
    db_spy.ok = True
    settings_store.write_overlay(path, {"DB_HOST": "10.1.1.1", "DB_USER": "reader", "DB_PASS": "old-pw"}, "seed")
    assert save(admin, {"DB_PASS": "new-pw"}).status_code == 200
    assert save(admin, {"DB_HOST": "10.2.2.2", "DB_PASS": "new-pw-2"}).status_code == 200
    assert settings_store.read_overlay(path).values["DB_HOST"] == "10.2.2.2"


def test_save_without_db_keys_does_not_read_env_provider_again(admin, app):
    calls = []
    real = app.config["ENV_PROVIDER"]
    app.config["ENV_PROVIDER"] = lambda: (calls.append(1), real())[1]
    assert save(admin, {"DENGUE_DATA_AUDITED": "1"}).status_code == 200
    assert calls == []


# ---------------------------------------------------------------- M-3: hook รอรอบของตัวตั้งเวลาก่อน SIGTERM

class FakeSchedulerThread:
    """เธรดตัวตั้งเวลาปลอม: join(timeout) บันทึก timeout และหน่วงตามที่กำหนด (finish_after=None = ไม่จบเลย)"""

    def __init__(self, finish_after=0.0):
        self.finish_after = finish_after
        self.join_timeouts = []
        self.alive = True

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.join_timeouts.append(timeout)
        if self.finish_after is None:
            time.sleep(min(timeout or 0, 0.2))
            return
        time.sleep(self.finish_after)
        self.alive = False


def test_restart_hook_signals_scheduler_stop_waits_then_sigterm_once(app, monkeypatch):
    stop = threading.Event()
    thread = FakeSchedulerThread(finish_after=0.05)
    app.config["SCHEDULER_STOP"], app.config["SCHEDULER_THREAD"] = stop, thread
    sent = []
    monkeypatch.setattr(settings_views.os, "kill",
                        lambda pid, sig: sent.append((pid, sig, stop.is_set(), thread.is_alive())))
    settings_views.default_restart_hook(app, wait=5)
    # SIGTERM ครั้งเดียว หลังสั่งหยุดแล้วและเธรดจบรอบแล้ว
    assert sent == [(os.getpid(), signal.SIGTERM, True, False)]
    assert thread.join_timeouts == [5]


def test_restart_hook_wait_is_bounded_when_scheduler_never_finishes(app, monkeypatch):
    stop = threading.Event()
    thread = FakeSchedulerThread(finish_after=None)
    app.config["SCHEDULER_STOP"], app.config["SCHEDULER_THREAD"] = stop, thread
    sent = []
    monkeypatch.setattr(settings_views.os, "kill", lambda pid, sig: sent.append(sig))
    t0 = time.monotonic()
    settings_views.default_restart_hook(app, wait=0.1)
    assert time.monotonic() - t0 < 2 and thread.join_timeouts == [0.1]
    assert sent == [signal.SIGTERM] and stop.is_set() and thread.is_alive()   # รอไม่ไหวก็ SIGTERM (ไม่ค้างตลอดไป)


def test_restart_hook_default_wait_is_twenty_seconds_and_real_thread_is_joined(app, monkeypatch):
    assert settings_views.SCHEDULER_STOP_WAIT_S == 20
    stop = threading.Event()
    done = threading.Event()

    def cycle():             # ตัวตั้งเวลาจริง: กำลังอยู่ในรอบ (หน่วง) แล้วค่อยเห็นสัญญาณหยุด
        stop.wait(10)
        time.sleep(0.1)
        done.set()
    thread = threading.Thread(target=cycle, daemon=True)
    thread.start()
    app.config["SCHEDULER_STOP"], app.config["SCHEDULER_THREAD"] = stop, thread
    sent = []
    monkeypatch.setattr(settings_views.os, "kill", lambda pid, sig: sent.append(done.is_set()))
    settings_views.default_restart_hook(app, wait=5)
    assert sent == [True]     # ฆ่าหลังรอบจบเท่านั้น


def test_restart_hook_without_scheduler_or_app_just_sends_sigterm(app, monkeypatch):
    sent = []
    monkeypatch.setattr(settings_views.os, "kill", lambda pid, sig: sent.append(sig))
    settings_views.default_restart_hook(app, wait=0.1)      # ไม่มี SCHEDULER_STOP/THREAD (DENGUE_SCHEDULER=0 หรือไม่ได้ล็อก)
    settings_views.default_restart_hook()
    assert sent == [signal.SIGTERM, signal.SIGTERM]


def test_run_hook_passes_app_only_to_hooks_that_want_it(app):
    seen = []

    def plain():
        seen.append("plain")

    def wants(a):
        seen.append(a)
    wants.wants_app = True
    settings_views._run_hook(app, plain)
    settings_views._run_hook(app, wants)
    assert seen == ["plain", app]
    assert settings_views.default_restart_hook.wants_app is True


def test_saving_a_change_rewrites_a_corrupt_settings_file(admin, app, path):
    """INSTALL 11.4: settings_error แก้จากหน้าได้ — บันทึกค่าใหม่เขียนไฟล์สมบูรณ์ทับไฟล์ที่เสีย"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    data, _ = page_data(admin)
    assert data["overlay_error"]
    assert save(admin, {"DENGUE_DATA_AUDITED": "1"}).status_code == 200
    assert settings_store.read_overlay(path).error is None
    assert settings_store.read_overlay(path).values == {"DENGUE_DATA_AUDITED": "1"}
    data, html = page_data(admin)
    assert data["overlay_error"] is None and "settings-overlay-error" not in html
