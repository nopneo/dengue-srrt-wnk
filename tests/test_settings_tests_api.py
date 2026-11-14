"""ปุ่มทดสอบ HOSxP / MOPH / ส่งข้อความทดสอบ + ด่านทดสอบ DB ฝั่งเซิร์ฟเวอร์ตอนบันทึก (Task 5).

ทุกเทสต์ออฟไลน์: connect / tables_source / tcp_check / sender เป็นของปลอม (ฉีดผ่าน app.config["CHECK_DEPS"])
— ไม่แตะ HOSxP จริงหรือ MOPH จริง. ตัวตรวจความลับ: ค่า/ข้อความไดรเวอร์ที่ไม่ควรออกมาต้องไม่โผล่ใน response / log / audit / repr(app.config)
"""
import json
import logging
import socket
import threading

import pymysql
import pytest

import moph_notify
import settings_checks
import settings_store
import settings_views
import setup_check
import wiring
from app import create_app
from config import load_settings
from fakes import FakeSender, FakeSource
from hosxp import HosxpError
from moph_notify import NotifyError

HDR = {"X-Requested-With": "dengue"}
PUBLIC = {"X-Forwarded-For": "203.0.113.9"}
PW = "correct-horse-pw-1"
SAVE = "/api/settings/save"
TEST = "/api/settings/test"
SEND = "/api/settings/send-test"

KEY_A = "UNIQUE-CLIENT-KEY-abcdef01"
KEY_B = "UNIQUE-SECRET-KEY-abcdef02"
DB_SECRET = "UNIQUE-DB-PASSWORD-zz9<>'\""
FIXED_TEXT = "ทดสอบการส่งจากระบบแจ้งเตือนไข้เลือดออก SRRT (ไม่มีข้อมูลผู้ป่วย)"


# ---------------------------------------------------------------- ของปลอม

class FakeCon:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class ConnectSpy:
    """แทน wiring.make_connect: บันทึกอาร์กิวเมนต์ที่ใช้ประกอบ; fail = exception ที่ factory ต้องยก"""

    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []
        self.kwargs = []   # คีย์เวิร์ดเพิ่มเติม (เช่น timeout ของเส้นทางทดสอบ)
        self.con = FakeCon()
        self.during = []   # ค่าที่ hook อยากเก็บตอน factory ถูกเรียก (เช่น สถานะล็อก)

    def __call__(self, host, port, database, user, password, **kw):
        self.kwargs.append(kw)
        self.calls.append({"host": host, "port": port, "database": database,
                           "user": user, "password": password})

        def factory():
            self.during.append(settings_views._WRITE_LOCK.locked())
            if self.fail is not None:
                raise self.fail
            return self.con
        return factory


def all_tables(con):
    return {t: {c: "text" for c in cols} for t, cols in setup_check.REQUIRED_COLUMNS.items()}


class TcpSpy:
    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []

    def __call__(self, host, port):
        self.calls.append((host, port))
        if self.fail is not None:
            raise self.fail


class SenderSpy:
    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []

    def __call__(self, messages, base_url, client_key, secret_key):
        self.calls.append({"messages": messages, "base_url": base_url,
                           "client_key": client_key, "secret_key": secret_key})
        if self.fail is not None:
            raise self.fail
        return {"message_code": 200}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def denied():
    return pymysql.err.OperationalError(
        1045, "Access denied for user 'dbuser-xyz'@'10.77.66.55' (using password: YES)")


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def connect():
    return ConnectSpy()


@pytest.fixture
def tcp():
    return TcpSpy()


@pytest.fixture
def sender():
    return SenderSpy()


@pytest.fixture
def app(tmp_path, store, connect, tcp, sender):
    settings = load_settings({"SECRET_KEY": "test-secret-key-0123456789",
                              "DENGUE_DB_PATH": str(tmp_path / "d.db")})
    app = create_app(settings=settings, store=store, source=FakeSource(), sender=FakeSender())
    app.config["RESTART_HOOK"] = lambda: None
    app.config["RESTART_DELAY"] = 0
    app.config["CHECK_DEPS"] = {"connect": connect, "tables_source": all_tables,
                                "tcp_check": tcp, "sender": sender}
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


def db_test(client, values=None, **kw):
    return post(client, TEST, {"target": "db", "values": values or {}}, **kw)


def moph_test(client, values=None, **kw):
    return post(client, TEST, {"target": "moph", "values": values or {}}, **kw)


def step(resp_json, name):
    found = [s for s in resp_json["steps"] if s["name"] == name]
    assert len(found) == 1, (name, resp_json["steps"])
    return found[0]


DB_VALUES = {"DB_HOST": "10.1.2.3", "DB_USER": "reader", "DB_PASS": "pw-123"}


# ---------------------------------------------------------------- POST /api/settings/test target=db

def test_db_test_ok_steps(admin, connect):
    r = db_test(admin, {**DB_VALUES, "DB_PORT": "3307", "DB_NAME": "hosdb"})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] is True
    for s in j["steps"]:
        assert set(s) == {"name", "status", "detail"} and s["status"] in ("ok", "warn", "fail")
        assert isinstance(s["detail"], str) and s["detail"]
    assert [s["name"] for s in j["steps"]] == [
        "ตั้งค่าครบ", "เชื่อมต่อฐานข้อมูล", "เซสชันอ่านอย่างเดียว", "ตารางที่ใช้"]
    assert all(s["status"] == "ok" for s in j["steps"])
    assert connect.calls == [{"host": "10.1.2.3", "port": 3307, "database": "hosdb",
                              "user": "reader", "password": "pw-123"}]
    assert connect.con.closed == 1


def test_db_test_missing_settings_is_fail_without_connecting(admin, connect):
    j = db_test(admin).get_json()
    assert j["ok"] is False
    s = step(j, "ตั้งค่าครบ")
    assert s["status"] == "fail" and "DB_HOST" in s["detail"] and "DB_USER" in s["detail"]
    assert connect.calls == []


def test_db_test_access_denied_maps_to_code_without_driver_text(admin, connect, caplog):
    caplog.set_level(logging.DEBUG)
    connect.fail = denied()
    r = db_test(admin, {**DB_VALUES, "DB_PASS": DB_SECRET})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] is False
    s = step(j, "เชื่อมต่อฐานข้อมูล")
    assert s["status"] == "fail"
    assert s["detail"] == "รหัสผ่านหรือสิทธิ์ไม่ถูกต้อง (Access denied 1045)"
    body = r.get_data(as_text=True)
    for leak in ("dbuser-xyz", "10.77.66.55", "using password", DB_SECRET, "Access denied for user"):
        assert leak not in body and leak not in caplog.text
    assert connect.con.closed == 0   # ไม่มีการเชื่อมต่อให้ปิด


@pytest.mark.parametrize("exc,expected", [
    (pymysql.err.OperationalError(1049, "Unknown database 'secretdb'"), "ไม่พบฐานข้อมูล (1049)"),
    (pymysql.err.OperationalError(2003, "Can't connect to MySQL server on '10.9.8.7'"),
     "ต่อไม่ได้ (2003)"),
    (pymysql.err.OperationalError(2013, "Lost connection to server"), "หมดเวลา"),
    (pymysql.err.OperationalError(2006, "MySQL server has gone away"), "หมดเวลา"),
    (socket.timeout("timed out connecting 10.9.8.7"), "หมดเวลา"),
    (TimeoutError("slow host 10.9.8.7"), "หมดเวลา"),
    (pymysql.err.OperationalError(1129, "Host '10.9.8.7' is blocked"), "ข้อผิดพลาดฐานข้อมูล (1129)"),
    (ValueError("boom 10.9.8.7 secretdb"), "ValueError"),
])
def test_db_test_timeout_and_unreachable(admin, connect, exc, expected):
    connect.fail = exc
    r = db_test(admin, DB_VALUES)
    j = r.get_json()
    s = step(j, "เชื่อมต่อฐานข้อมูล")
    assert j["ok"] is False and s["status"] == "fail" and s["detail"] == expected
    assert "10.9.8.7" not in r.get_data(as_text=True) and "secretdb" not in r.get_data(as_text=True)


def _tables_without(app, *absent):
    present = {t: {} for t in setup_check.REQUIRED_COLUMNS if t not in absent}
    app.config["CHECK_DEPS"]["tables_source"] = lambda con: present


def test_db_test_reports_missing_tables_as_count(admin, app):
    _tables_without(app, "ovst", "patient")   # ตารางที่ใช้จริง 2 ตาราง
    j = db_test(admin, DB_VALUES).get_json()
    s = step(j, "ตารางที่ใช้")
    assert s["status"] == "warn" and s["detail"] == "ตารางไม่ครบ 2 ตาราง"
    assert j["ok"] is True   # warn ไม่ทำให้ ok เป็นเท็จ


def test_db_test_optional_tables_are_not_counted_as_required(admin, app):
    assert "thaiaddress" in setup_check.OPTIONAL_TABLES
    _tables_without(app, "thaiaddress")
    s = step(db_test(admin, DB_VALUES).get_json(), "ตารางที่ใช้")
    assert s["status"] == "warn"
    assert s["detail"] == "ไม่พบตารางเสริม 1 ตาราง (ชื่อพื้นที่จะแสดงเป็นรหัสแทน)"
    _tables_without(app, "ovst", "thaiaddress")
    s = step(db_test(admin, DB_VALUES).get_json(), "ตารางที่ใช้")
    assert s["status"] == "warn" and s["detail"] == "ตารางไม่ครบ 1 ตาราง · ไม่พบตารางเสริม 1 ตาราง"


def test_db_test_table_listing_error_is_warn_with_safe_detail(admin, app):
    def boom(con):
        raise HosxpError("OperationalError: (1142, \"SELECT command denied to user 'zed'@'h'\")")
    app.config["CHECK_DEPS"]["tables_source"] = boom
    r = db_test(admin, DB_VALUES)
    s = step(r.get_json(), "ตารางที่ใช้")
    assert s["status"] == "warn" and "zed" not in r.get_data(as_text=True)
    assert s["detail"] == "ตรวจรายการตารางไม่สำเร็จ (HosxpError)"


def test_db_test_forces_read_only_session(admin, app, monkeypatch):
    """ไม่ฉีด connect: ใช้ wiring.make_connect จริง (pymysql ปลอม) — ต้องตั้ง SESSION READ ONLY ก่อนอย่างอื่น"""
    executed = []
    made = []

    class Cur:
        def execute(self, sql, *a):
            executed.append(sql)

        def close(self):
            pass

    class Con:
        closed = 0

        def cursor(self):
            return Cur()

        def close(self):
            Con.closed += 1

    def fake_connect(**kw):
        made.append(kw)
        return Con()
    monkeypatch.setattr(wiring.pymysql, "connect", fake_connect)
    del app.config["CHECK_DEPS"]["connect"]   # default จริง
    j = db_test(admin, DB_VALUES).get_json()
    assert j["ok"] is True
    assert executed[0] == "SET SESSION TRANSACTION READ ONLY"
    assert made[0]["host"] == "10.1.2.3" and made[0]["password"] == "pw-123"
    # เส้นทางทดสอบใช้ timeout สั้น (5/10) ไม่ใช่ 10/60 ของการใช้งานจริง
    assert (made[0]["connect_timeout"], made[0]["read_timeout"]) == (5, 10) and Con.closed == 1
    assert step(j, "เซสชันอ่านอย่างเดียว")["status"] == "ok"


def test_db_test_fails_when_read_only_cannot_be_set(admin, app, monkeypatch):
    class Cur:
        def execute(self, sql, *a):
            raise RuntimeError("driver text 10.5.5.5 secret-detail")

        def close(self):
            pass

    class Con:
        closed = 0

        def cursor(self):
            return Cur()

        def close(self):
            Con.closed += 1
    monkeypatch.setattr(wiring.pymysql, "connect", lambda **kw: Con())
    del app.config["CHECK_DEPS"]["connect"]
    r = db_test(admin, DB_VALUES)
    j = r.get_json()
    assert j["ok"] is False
    assert step(j, "เซสชันอ่านอย่างเดียว")["status"] == "fail"
    assert "secret-detail" not in r.get_data(as_text=True) and Con.closed == 1


STORED = {"DB_HOST": "10.1.2.3", "DB_USER": "reader", "DB_PASS": DB_SECRET, "DB_PORT": "3306"}
PASSWORD_REQUIRED = {"error": "password_required",
                     "message": "กรอกรหัสผ่านฐานข้อมูลใหม่เมื่อเปลี่ยนโฮสต์ พอร์ต หรือผู้ใช้"}


def test_db_test_blank_secret_uses_stored_value(admin, connect, path):
    settings_store.write_overlay(path, STORED, "seed")
    # DB_PASS ว่าง (หรือไม่ส่ง) และปลายทางเดิม = ใช้ค่าที่เก็บไว้; เปลี่ยนแค่ชื่อฐาน (DB_NAME) ก็ใช้รหัสเดิมได้
    j = db_test(admin, {"DB_PASS": "", "DB_NAME": "otherdb"}).get_json()
    assert j["ok"] is True
    assert connect.calls[0]["password"] == DB_SECRET and connect.calls[0]["host"] == "10.1.2.3"
    assert connect.calls[0]["user"] == "reader" and connect.calls[0]["database"] == "otherdb"


@pytest.mark.parametrize("values", [
    {"DB_HOST": "10.8.8.8"}, {"DB_PORT": "3307"}, {"DB_USER": "other"},
    {"DB_HOST": "10.8.8.8", "DB_PASS": ""}, {"DB_HOST": "10.8.8.8", "DB_PORT": "3307", "DB_USER": "x"},
])
def test_db_test_changing_endpoint_without_new_password_is_400_and_never_connects(
        admin, connect, path, values):
    """R8: รหัสที่เก็บไว้ห้ามถูกส่งไปปลายทางใหม่ที่ผู้ใช้พิมพ์ (ผู้ฟังปลอมเก็บรหัสได้)"""
    settings_store.write_overlay(path, STORED, "seed")
    r = db_test(admin, values)
    assert r.status_code == 400 and r.get_json() == PASSWORD_REQUIRED
    assert connect.calls == []
    assert DB_SECRET not in r.get_data(as_text=True)


@pytest.mark.parametrize("values", [
    {"DB_HOST": "10.8.8.8", "DB_PASS": "fresh-pw"},
    {"DB_PORT": "3307", "DB_PASS": "fresh-pw"},
    {"DB_USER": "other", "DB_PASS": "fresh-pw"},
])
def test_db_test_changing_endpoint_with_new_password_uses_only_new_password(
        admin, connect, path, values):
    settings_store.write_overlay(path, STORED, "seed")
    r = db_test(admin, values)
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert [c["password"] for c in connect.calls] == ["fresh-pw"]
    assert DB_SECRET not in repr(connect.calls)


@pytest.mark.parametrize("values", [
    {"DB_HOST": "10.1.2.3"}, {"DB_HOST": " 10.1.2.3 ", "DB_PORT": "3306"},
    {"DB_USER": "reader", "DB_PASS": ""}, {"DB_NAME": "x"}, {},
])
def test_db_test_unchanged_endpoint_keeps_stored_password(admin, connect, path, values):
    settings_store.write_overlay(path, STORED, "seed")
    assert db_test(admin, values).status_code == 200
    assert connect.calls[0]["password"] == DB_SECRET


def test_db_test_default_port_counts_as_3306(admin, app, connect):
    app.config["ENV_PROVIDER"] = lambda: {"DB_HOST": "h1", "DB_USER": "u", "DB_PASS": DB_SECRET,
                                          "DB_PORT": ""}
    assert db_test(admin, {"DB_PORT": "3306"}).status_code == 200
    assert connect.calls[0]["password"] == DB_SECRET
    assert db_test(admin, {"DB_PORT": "3307"}).status_code == 400


def test_db_test_new_secret_overrides_stored_value(admin, connect, path):
    settings_store.write_overlay(path, {"DB_PASS": "old-pass", "DB_HOST": "10.1.2.3",
                                        "DB_USER": "reader"}, "seed")
    db_test(admin, {"DB_PASS": "new-pass"})
    assert connect.calls[0]["password"] == "new-pass"


def test_db_test_uses_env_provider_not_masked_base_env(admin, app, connect):
    app.config["ENV_PROVIDER"] = lambda: {"DB_HOST": "10.4.4.4", "DB_USER": "u1", "DB_PASS": "p1",
                                          "DB_NAME": "", "DB_PORT": ""}
    app.config["SETTINGS_BASE_ENV"] = {"DB_HOST": "10.0.0.0", "DB_PASS": "*"}
    assert db_test(admin).get_json()["ok"] is True
    assert connect.calls[0] == {"host": "10.4.4.4", "port": 3306, "database": "hos",
                                "user": "u1", "password": "p1"}


@pytest.mark.parametrize("values", [
    {"DB_PORT": "99999"}, {"DB_HOST": "bad host!"}, {"DB_USER": ""}, {"NOPE": "x"},
    {"DB_NAME": "a b"},
])
def test_db_test_invalid_values_400(admin, connect, values):
    r = db_test(admin, values)
    j = r.get_json()
    assert r.status_code == 400 and j["error"] == "invalid" and j["message"]
    assert connect.calls == []
    for v in values.values():
        if len(v) > 3:
            assert v not in r.get_data(as_text=True)


@pytest.mark.parametrize("body", [
    [], "x", {}, {"target": "other"}, {"target": 5}, {"target": "db", "values": []},
    {"target": "db", "values": {"DB_HOST": 5}}, {"target": "db", "values": {"DB_HOST": None}},
    {"target": "db", "values": {str(i): "x" for i in range(100)}},
])
def test_test_endpoint_bad_shapes_400(admin, connect, body):
    r = post(admin, TEST, body)
    assert r.status_code == 400 and r.get_json() == {"error": "bad_request"}
    assert connect.calls == []


def test_test_endpoint_does_not_need_password_or_write_anything(admin, path, store):
    assert db_test(admin, DB_VALUES).status_code == 200
    assert not path.exists() and store.audit() == []


# ---------------------------------------------------------------- target=moph

def test_moph_test_format_and_network_only_never_sends_keys(admin, app, tcp, monkeypatch):
    import requests

    def never(*a, **k):
        raise AssertionError("ห้ามส่งกุญแจ/ข้อความจากปุ่มทดสอบ")
    monkeypatch.setattr(moph_notify, "send_messages", never)
    monkeypatch.setattr(requests, "post", never)
    monkeypatch.setattr(requests, "request", never)
    app.config["CHECK_DEPS"]["sender"] = never
    r = moph_test(admin, {"MOPH_CLIENT_KEY": KEY_A, "MOPH_SECRET_KEY": KEY_B})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] is True
    assert [s["name"] for s in j["steps"]] == ["กุญแจครบ", "รูปแบบกุญแจ", "เครือข่ายถึง MOPH"]
    assert all(s["status"] == "ok" for s in j["steps"])
    assert tcp.calls == [("morpromt2f.moph.go.th", 443)]
    body = r.get_data(as_text=True)
    assert KEY_A not in body and KEY_B not in body
    assert KEY_A not in repr(tcp.calls) and KEY_B not in repr(tcp.calls)


def test_moph_test_keys_missing_is_warn(admin, tcp):
    j = moph_test(admin).get_json()
    assert j["ok"] is True
    assert step(j, "กุญแจครบ") == {"name": "กุญแจครบ", "status": "warn", "detail": "ยังไม่ได้ตั้งกุญแจ"}
    assert step(j, "เครือข่ายถึง MOPH")["status"] == "ok"


def test_moph_test_blank_secret_uses_stored_keys(admin, path, tcp):
    settings_store.write_overlay(path, {"MOPH_CLIENT_KEY": KEY_A, "MOPH_SECRET_KEY": KEY_B}, "seed")
    j = moph_test(admin, {"MOPH_CLIENT_KEY": "", "MOPH_SECRET_KEY": ""}).get_json()
    assert step(j, "กุญแจครบ")["status"] == "ok" and step(j, "รูปแบบกุญแจ")["status"] == "ok"


def test_moph_test_bad_key_format_is_400_from_validate(admin, tcp):
    r = moph_test(admin, {"MOPH_CLIENT_KEY": "short key with spaces " + KEY_A})
    assert r.status_code == 400 and r.get_json()["key"] == "MOPH_CLIENT_KEY"
    assert KEY_A not in r.get_data(as_text=True) and tcp.calls == []


def test_moph_test_stored_bad_format_key_is_fail_step(admin, app, tcp):
    """คีย์ที่ค่าเดิม (จาก env) รูปแบบผิด -> ขั้น 'รูปแบบกุญแจ' เป็น fail (ไม่มีตัวค่า)"""
    app.config["ENV_PROVIDER"] = lambda: {"MOPH_CLIENT_KEY": "<Client ID ของกลุ่ม>",
                                          "MOPH_SECRET_KEY": KEY_B}
    r = moph_test(admin)
    j = r.get_json()
    s = step(j, "รูปแบบกุญแจ")
    assert j["ok"] is False and s["status"] == "fail" and KEY_B not in r.get_data(as_text=True)


def test_moph_test_network_failure_is_fail_with_type_only(admin, tcp):
    tcp.fail = OSError(111, "Connection refused by 10.0.0.9")
    r = moph_test(admin, {"MOPH_CLIENT_KEY": KEY_A, "MOPH_SECRET_KEY": KEY_B})
    j = r.get_json()
    s = step(j, "เครือข่ายถึง MOPH")
    assert j["ok"] is False and s["status"] == "fail"
    assert "10.0.0.9" not in r.get_data(as_text=True) and "refused" not in r.get_data(as_text=True)


def test_moph_test_uses_configured_base_url_host_and_port(admin, app, tcp):
    app.config["ENV_PROVIDER"] = lambda: {"MOPH_BASE_URL": "https://notify.example.org:8443/x"}
    moph_test(admin)
    assert tcp.calls == [("notify.example.org", 8443)]


# ---------------------------------------------------------------- POST /api/settings/send-test

def test_send_test_requires_confirm(admin, app, sender):
    app.extensions["settings_test_limiter"] = settings_views.SlidingWindowLimiter(limits={"test": 99, "send": 99})
    for body in ({}, {"confirm": False}, {"confirm": "true"}, {"confirm": 1}, {"confirm": None},
                 {"values": {}}):
        r = post(admin, SEND, body)
        assert r.status_code == 400 and r.get_json() == {"error": "bad_request"}, body
    assert post(admin, SEND, []).status_code == 400
    assert sender.calls == []


def test_send_test_sends_fixed_text_with_no_patient_data(admin, path, sender):
    settings_store.write_overlay(path, {"MOPH_CLIENT_KEY": KEY_A, "MOPH_SECRET_KEY": KEY_B}, "seed")
    r = post(admin, SEND, {"confirm": True})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] is True
    assert j["steps"][-1] == {"name": "ส่งข้อความทดสอบ", "status": "ok",
                              "detail": "ส่งแล้ว — ตรวจในกลุ่ม LINE"}
    assert len(sender.calls) == 1
    call = sender.calls[0]
    assert call["messages"] == [{"type": "text", "text": FIXED_TEXT}]
    assert call["client_key"] == KEY_A and call["secret_key"] == KEY_B
    assert call["base_url"] == "https://morpromt2f.moph.go.th"
    body = r.get_data(as_text=True)
    assert KEY_A not in body and KEY_B not in body


def test_send_test_values_override_stored_keys(admin, path, sender):
    settings_store.write_overlay(path, {"MOPH_CLIENT_KEY": "stored-client-key-1",
                                        "MOPH_SECRET_KEY": "stored-secret-key-1"}, "seed")
    r = post(admin, SEND, {"confirm": True, "values": {"MOPH_CLIENT_KEY": KEY_A,
                                                        "MOPH_SECRET_KEY": ""}})
    assert r.get_json()["ok"] is True
    assert sender.calls[0]["client_key"] == KEY_A and sender.calls[0]["secret_key"] == "stored-secret-key-1"


def test_send_test_missing_keys_fails_without_sending(admin, sender):
    r = post(admin, SEND, {"confirm": True})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] is False
    assert any(s["status"] == "fail" for s in j["steps"]) and sender.calls == []


def test_send_test_invalid_values_400_nothing_sent(admin, sender):
    r = post(admin, SEND, {"confirm": True, "values": {"MOPH_CLIENT_KEY": "x y " + KEY_A}})
    assert r.status_code == 400 and KEY_A not in r.get_data(as_text=True) and sender.calls == []


@pytest.mark.parametrize("message,expected", [
    (f"Unauthorized — client-key หรือ secret-key ไม่ถูกต้อง (code 401)",
     "client-key หรือ secret-key ไม่ถูกต้อง (code 401)"),
    (f"{KEY_A} — {KEY_B} — รูปแบบข้อความไม่ถูกต้องตามที่ LINE กำหนด (code 400)",
     "รูปแบบข้อความไม่ถูกต้องตามที่ LINE กำหนด (code 400)"),
    ("เชื่อมต่อ MOPH Notify ไม่ได้ (ConnectionError)", "เชื่อมต่อ MOPH Notify ไม่ได้ (ConnectionError)"),
    (f"ข้อความแปลก ๆ ที่มีกุญแจ {KEY_A}", "NotifyError"),
])
def test_send_test_failure_returns_hint_without_keys(admin, path, sender, caplog, message, expected):
    caplog.set_level(logging.DEBUG)
    settings_store.write_overlay(path, {"MOPH_CLIENT_KEY": KEY_A, "MOPH_SECRET_KEY": KEY_B}, "seed")
    sender.fail = NotifyError(message)
    r = post(admin, SEND, {"confirm": True})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] is False
    last = j["steps"][-1]
    assert last["name"] == "ส่งข้อความทดสอบ" and last["status"] == "fail" and last["detail"] == expected
    body = r.get_data(as_text=True)
    for s in (KEY_A, KEY_B):
        assert s not in body and s not in caplog.text


def test_send_test_unexpected_exception_type_only(admin, path, sender):
    settings_store.write_overlay(path, {"MOPH_CLIENT_KEY": KEY_A, "MOPH_SECRET_KEY": KEY_B}, "seed")
    sender.fail = RuntimeError("boom " + KEY_B)
    r = post(admin, SEND, {"confirm": True})
    j = r.get_json()
    assert j["ok"] is False and j["steps"][-1]["detail"] == "RuntimeError"
    assert KEY_B not in r.get_data(as_text=True)


def test_send_test_is_audited_without_keys(admin, path, store, sender):
    settings_store.write_overlay(path, {"MOPH_CLIENT_KEY": KEY_A, "MOPH_SECRET_KEY": KEY_B}, "seed")
    post(admin, SEND, {"confirm": True})
    rows = store.audit()
    assert [r["action"] for r in rows] == ["settings_send_test"] and rows[0]["user"] == "boss"
    assert KEY_A not in json.dumps(rows) and KEY_B not in json.dumps(rows)


# ---------------------------------------------------------------- ด่านสิทธิ์

@pytest.mark.parametrize("url,body", [
    (TEST, {"target": "db", "values": DB_VALUES}),
    (SEND, {"confirm": True}),
])
def test_test_endpoints_need_admin_lan_csrf(client, store, login_as, connect, sender, url, body):
    # ไม่ login -> 401
    assert post(client, url, body).status_code == 401
    # srrt / viewer -> 403
    for role in ("srrt", "viewer"):
        login_as(client, f"u-{role}", role)
        assert post(client, url, body).status_code == 403
    store.add_user("boss", PW, "admin")
    login_as(client, "boss", "admin")
    # นอก LAN -> 403 (ก่อนตรวจ login)
    assert post(client, url, body, headers={**HDR, **PUBLIC}).status_code == 403
    # ไม่มีหัว CSRF -> 403 csrf
    r = post(client, url, body, headers={})
    assert r.status_code == 403 and r.get_json()["error"] == "csrf"
    # ไม่ใช่ JSON -> 415
    assert client.post(url, data="x=1", headers=HDR).status_code == 415
    assert connect.calls == [] and sender.calls == []
    # admin ใน LAN ผ่าน
    assert post(client, url, body).status_code == 200


def test_non_lan_rejected_before_login_check(client):
    assert post(client, TEST, {"target": "db"}, headers={**HDR, **PUBLIC}).status_code == 403
    assert post(client, SEND, {"confirm": True}, headers={**HDR, **PUBLIC}).status_code == 403


@pytest.mark.parametrize("url", [TEST, SEND])
def test_test_endpoints_body_cap_413(admin, connect, sender, url):
    big = b'{"target":"db","confirm":true,"pad":"' + b"a" * settings_views.MAX_BODY_BYTES + b'"}'
    r = admin.post(url, data=big, content_type="application/json", headers=HDR)
    assert r.status_code == 413 and r.get_json()["error"] == "too_large"
    assert connect.calls == [] and sender.calls == []


@pytest.mark.parametrize("url,body", [
    (TEST, {"target": "db", "values": DB_VALUES}),
    (SEND, {"confirm": True}),
])
def test_test_endpoints_unexpected_error_is_fixed_500(admin, app, caplog, url, body):
    caplog.set_level(logging.DEBUG)

    def boom():
        raise RuntimeError("boom " + DB_SECRET)
    app.config["ENV_PROVIDER"] = boom
    r = post(admin, url, body)
    assert r.status_code == 500 and r.get_json()["error"] == "server"
    assert DB_SECRET not in r.get_data(as_text=True) and DB_SECRET not in caplog.text
    assert "RuntimeError" in caplog.text


# ---------------------------------------------------------------- ตัวจำกัดความถี่

def test_test_rate_limit_10_per_minute_per_user(admin, app, store, login_as, connect):
    clock = Clock()
    app.extensions["settings_test_limiter"] = settings_views.SlidingWindowLimiter(clock=clock)
    for _ in range(10):
        assert db_test(admin, DB_VALUES).status_code == 200
    r = db_test(admin, DB_VALUES)
    assert r.status_code == 429
    j = r.get_json()
    assert j["error"] == "rate_limited" and j["message"]
    assert len(connect.calls) == 10
    # ผู้ใช้อื่นไม่ถูกจำกัดร่วม
    store.add_user("boss2", PW, "admin")
    other = app.test_client()
    login_as(other, "boss2", "admin")
    assert db_test(other, DB_VALUES).status_code == 200
    # ผ่านไป 60 วินาที -> ใช้ได้อีก
    clock.now += 60.5
    assert db_test(admin, DB_VALUES).status_code == 200


def test_send_test_rate_limit_3_per_minute_and_separate_bucket(admin, app, path, sender):
    clock = Clock()
    app.extensions["settings_test_limiter"] = settings_views.SlidingWindowLimiter(clock=clock)
    settings_store.write_overlay(path, {"MOPH_CLIENT_KEY": KEY_A, "MOPH_SECRET_KEY": KEY_B}, "seed")
    for _ in range(3):
        assert post(admin, SEND, {"confirm": True}).status_code == 200
    r = post(admin, SEND, {"confirm": True})
    assert r.status_code == 429 and r.get_json()["error"] == "rate_limited"
    assert len(sender.calls) == 3
    # ที่นั่งของปุ่มทดสอบแยกกัน
    assert db_test(admin, DB_VALUES).status_code == 200
    clock.now += 61
    assert post(admin, SEND, {"confirm": True}).status_code == 200


def test_limiter_sliding_window_unit():
    clock = Clock()
    lim = settings_views.SlidingWindowLimiter(clock=clock)
    assert all(lim.allow("test", "a") for _ in range(10))
    assert not lim.allow("test", "a")
    clock.now += 30
    assert not lim.allow("test", "a")   # หน้าต่างเลื่อน: ยังไม่ครบ 60 วินาทีนับจากครั้งแรก
    clock.now += 31
    assert lim.allow("test", "a")
    assert all(lim.allow("send", "a") for _ in range(3)) and not lim.allow("send", "a")


# ---------------------------------------------------------------- ด่านทดสอบ DB ฝั่งเซิร์ฟเวอร์ตอนบันทึก (R1)

def save(client, values=None, clear=None, password=PW, **extra):
    body = {"values": values or {}, "clear": clear or [], "password": password}
    body.update(extra)
    return post(client, SAVE, body)


def test_save_db_change_runs_server_side_test_and_writes_on_success(admin, path, connect, store):
    r = save(admin, {"DB_HOST": "10.1.2.3", "DB_USER": "reader", "DB_PASS": "pw-123"})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] is True
    assert sorted(j["changed"]) == ["DB_HOST", "DB_PASS", "DB_USER"]
    assert connect.calls == [{"host": "10.1.2.3", "port": 3306, "database": "hos",
                              "user": "reader", "password": "pw-123"}]
    assert settings_store.read_overlay(path).values["DB_HOST"] == "10.1.2.3"
    assert [a["action"] for a in store.audit()] == ["settings_change"]


def test_save_db_change_with_failing_test_is_409_with_steps_and_writes_nothing(
        admin, path, connect, store, caplog):
    caplog.set_level(logging.DEBUG)
    connect.fail = denied()
    r = save(admin, {"DB_HOST": "10.1.2.3", "DB_USER": "reader", "DB_PASS": DB_SECRET})
    j = r.get_json()
    assert r.status_code == 409 and j["error"] == "untested"
    assert step(j, "เชื่อมต่อฐานข้อมูล")["status"] == "fail"
    assert not path.exists() and store.audit() == []
    body = r.get_data(as_text=True)
    for leak in (DB_SECRET, "dbuser-xyz", "10.77.66.55", "using password"):
        assert leak not in body and leak not in caplog.text


def test_save_db_gate_merges_current_values_with_changes(admin, path, connect):
    settings_store.write_overlay(path, {"DB_HOST": "10.1.2.3", "DB_USER": "reader",
                                        "DB_PASS": "stored-pw", "DB_NAME": "hosx"}, "seed")
    r = save(admin, {"DB_NAME": "hosy"})   # เปลี่ยนแค่ชื่อฐาน: ใช้รหัสที่เก็บไว้ได้
    assert r.status_code == 200
    assert connect.calls == [{"host": "10.1.2.3", "port": 3306, "database": "hosy",
                              "user": "reader", "password": "stored-pw"}]


def test_save_db_gate_applies_clears(admin, path, connect):
    settings_store.write_overlay(path, {"DB_HOST": "10.1.2.3", "DB_USER": "reader",
                                        "DB_PASS": "stored-pw"}, "seed")
    r = save(admin, clear=["DB_PASS"])
    j = r.get_json()
    assert r.status_code == 409 and step(j, "ตั้งค่าครบ")["status"] == "fail"
    assert connect.calls == []
    assert settings_store.read_overlay(path).values["DB_PASS"] == "stored-pw"


@pytest.mark.parametrize("values,clear", [
    ({"DB_HOST": "10.8.8.8"}, None), ({"DB_PORT": "3307"}, None), ({"DB_USER": "other"}, None),
    ({"DB_HOST": "10.8.8.8", "DB_PASS": ""}, None), ({"DB_HOST": "10.8.8.8"}, ["DB_PASS"]),
])
def test_save_changing_endpoint_without_new_password_is_400_nothing_written(
        admin, path, connect, store, values, clear):
    """R8 ผ่านด่านบันทึกด้วย: ไม่เชื่อมต่อ ไม่เขียน ไม่ audit"""
    settings_store.write_overlay(path, STORED, "seed")
    before = path.read_bytes()
    r = save(admin, values, clear=clear)
    assert r.status_code == 400 and r.get_json() == PASSWORD_REQUIRED
    assert connect.calls == [] and path.read_bytes() == before and store.audit() == []
    assert DB_SECRET not in r.get_data(as_text=True)


@pytest.mark.parametrize("values", [
    {"DB_HOST": "10.8.8.8", "DB_PASS": "fresh-pw"},
    {"DB_PORT": "3307", "DB_PASS": "fresh-pw"},
    {"DB_USER": "other", "DB_PASS": "fresh-pw"},
])
def test_save_changing_endpoint_with_new_password_tests_with_only_new_password(
        admin, path, connect, values):
    settings_store.write_overlay(path, STORED, "seed")
    r = save(admin, values)
    assert r.status_code == 200
    assert [c["password"] for c in connect.calls] == ["fresh-pw"]
    assert DB_SECRET not in repr(connect.calls)
    assert settings_store.read_overlay(path).values["DB_PASS"] == "fresh-pw"


def test_save_unchanged_endpoint_values_keep_stored_password(admin, path, connect):
    settings_store.write_overlay(path, STORED, "seed")
    # ปลายทางเดิม (ส่งค่าเดิมซ้ำ) + เปลี่ยนชื่อฐาน -> ใช้รหัสเดิมได้
    assert save(admin, {"DB_HOST": "10.1.2.3", "DB_NAME": "z"}).status_code == 200
    assert connect.calls[0]["password"] == DB_SECRET


@pytest.mark.parametrize("values,clear", [
    ({"DB_HOST": "10.8.8.8"}, None), ({"DB_PORT": "3307"}, None), ({"DB_USER": "other"}, None),
    ({"DB_HOST": "10.8.8.8", "DB_PASS": ""}, None), ({"DB_HOST": "10.8.8.8"}, ["DB_PASS"]),
])
def test_force_never_skips_password_required(admin, path, connect, store, values, clear):
    """R8: force ข้ามได้เฉพาะการต่อทดสอบ — เขียนโฮสต์ใหม่ทิ้งรหัสเดิมไว้ = ครั้งหน้าแอปส่งรหัสจริงไปที่อยู่นั้น"""
    settings_store.write_overlay(path, STORED, "seed")
    before = path.read_bytes()
    r = save(admin, values, clear=clear, force=True)
    assert r.status_code == 400 and r.get_json() == PASSWORD_REQUIRED
    assert path.read_bytes() == before and connect.calls == [] and store.audit() == []
    assert settings_store.read_overlay(path).values["DB_PASS"] == DB_SECRET


@pytest.mark.parametrize("values", [
    {"DB_HOST": "10.8.8.8", "DB_PASS": "fresh-pw"},
    {"DB_PORT": "3307", "DB_PASS": "fresh-pw"},
    {"DB_USER": "other", "DB_PASS": "fresh-pw"},
])
def test_force_with_new_password_skips_test_and_stores_only_new_password(admin, path, connect, values):
    settings_store.write_overlay(path, STORED, "seed")
    connect.fail = denied()   # ต่อไม่ได้ก็บันทึกได้เพราะ force
    r = save(admin, values, force=True)
    assert r.status_code == 200 and connect.calls == []
    ov = settings_store.read_overlay(path).values
    assert ov["DB_PASS"] == "fresh-pw" and DB_SECRET not in json.dumps(ov)


def test_force_with_unchanged_endpoint_is_still_200_without_connecting(admin, path, connect):
    settings_store.write_overlay(path, STORED, "seed")
    assert save(admin, {"DB_NAME": "z"}, force=True).status_code == 200
    assert save(admin, {"DB_HOST": "10.1.2.3", "DB_USER": "reader", "DB_PORT": "3306"},
                force=True).status_code == 200   # no-op
    assert connect.calls == []


def test_password_required_400_does_not_consume_rate_limit_bucket(admin, app, path, connect):
    app.extensions["settings_test_limiter"] = settings_views.SlidingWindowLimiter(clock=Clock())
    settings_store.write_overlay(path, STORED, "seed")
    for force in (False, True):
        for _ in range(15):
            assert save(admin, {"DB_HOST": "10.8.8.8"}, force=force).status_code == 400
    for _ in range(10):   # ถังยังเต็ม 10 ครั้ง
        assert db_test(admin, {"DB_NAME": "n"}).status_code == 200
    assert db_test(admin, {"DB_NAME": "n"}).status_code == 429


@pytest.mark.parametrize("current_port,new_port,needs_password", [
    ("3_307", "3307", False),     # production อ่าน int("3_307") = 3307 -> ไม่เปลี่ยน
    ("+3307", "3307", False),
    (" 3307 ", "3307", False),
    ("3_307", "3306", True),      # เดิมถูกอ่านเป็น 3306 ผิด ๆ -> ดูเหมือนไม่เปลี่ยน
    ("+3307", "3308", True),
    ("abc", "3306", False),       # ไม่ใช่ตัวเลข = ค่าเริ่มต้น 3306 เหมือน wiring._port
    ("abc", "3307", True),
])
def test_port_normalization_matches_production(admin, app, connect, current_port, new_port, needs_password):
    app.config["ENV_PROVIDER"] = lambda: {"DB_HOST": "h1", "DB_USER": "u", "DB_PASS": DB_SECRET,
                                          "DB_PORT": current_port}
    r = db_test(admin, {"DB_PORT": new_port})
    assert (r.status_code == 400) is needs_password
    assert bool(connect.calls) is (not needs_password)
    r = save(admin, {"DB_PORT": new_port}, force=True)
    assert (r.status_code == 400) is needs_password


def test_save_force_skips_db_test(admin, path, connect):
    connect.fail = denied()
    r = save(admin, {"DB_HOST": "10.1.2.3", "DB_PASS": "p"}, force=True)
    assert r.status_code == 200 and connect.calls == []
    assert settings_store.read_overlay(path).values["DB_HOST"] == "10.1.2.3"


def test_save_without_db_keys_does_not_run_db_test(admin, connect):
    assert save(admin, {"DENGUE_DATA_AUDITED": "1"}).status_code == 200
    assert save(admin, {"MOPH_CLIENT_KEY": KEY_A}).status_code == 200
    assert connect.calls == []


def test_save_ignores_client_tested_ok_flag(admin, path, connect):
    connect.fail = denied()
    values = {"DB_HOST": "10.1.2.3", "DB_USER": "u", "DB_PASS": "p"}
    r = save(admin, values, tested_ok=True)
    assert r.status_code == 409 and r.get_json()["error"] == "untested"
    assert not path.exists()
    # ไม่ใช่ boolean ก็ไม่ 400 (ไม่สนใจฟิลด์นี้)
    r = save(admin, values, tested_ok="whatever")
    assert r.status_code == 409


def test_save_db_test_runs_before_write_lock_is_taken(admin, connect):
    assert save(admin, {"DB_HOST": "10.1.2.3", "DB_USER": "u", "DB_PASS": "p"}).status_code == 200
    assert connect.during == [False]   # ตอนต่อฐาน ยังไม่ได้ถือ _WRITE_LOCK


def test_save_wrong_password_never_runs_db_test(admin, connect):
    r = save(admin, {"DB_HOST": "10.1.2.3", "DB_PASS": "p"}, password="wrong")
    assert r.status_code == 403 and connect.calls == []


GATED = {"DB_HOST": "10.1.2.3", "DB_USER": "u", "DB_PASS": "p"}


def test_save_db_gate_shares_the_test_rate_limit_bucket(admin, app, path, connect, store):
    clock = Clock()
    app.extensions["settings_test_limiter"] = settings_views.SlidingWindowLimiter(clock=clock)
    connect.fail = denied()
    for _ in range(10):
        assert save(admin, GATED).status_code == 409
    r = save(admin, GATED)
    assert r.status_code == 429 and r.get_json()["error"] == "rate_limited" and r.get_json()["message"]
    assert len(connect.calls) == 10 and not path.exists() and store.audit() == []
    # ปุ่มทดสอบใช้ถังเดียวกัน (หมดแล้ว)
    assert db_test(admin, DB_VALUES).status_code == 429
    # force ไม่ทดสอบจึงไม่กินโควตา/ไม่ถูกจำกัด
    assert save(admin, GATED, force=True).status_code == 200
    clock.now += 61
    connect.fail = None
    assert save(admin, {"DB_NAME": "n"}).status_code == 200


def test_test_button_and_gate_draw_from_one_bucket(admin, app, connect):
    app.extensions["settings_test_limiter"] = settings_views.SlidingWindowLimiter(clock=Clock())
    for _ in range(6):
        assert db_test(admin, DB_VALUES).status_code == 200
    for i in range(4):
        assert save(admin, {**GATED, "DB_NAME": f"n{i}"}).status_code == 200
    assert save(admin, {"DB_NAME": "x"}).status_code == 429
    assert len(connect.calls) == 10


def test_save_password_required_and_no_test_saves_do_not_consume_gate_quota(admin, app, path, connect):
    app.extensions["settings_test_limiter"] = settings_views.SlidingWindowLimiter(clock=Clock())
    settings_store.write_overlay(path, STORED, "seed")
    for _ in range(15):
        assert save(admin, {"DB_HOST": "10.8.8.8"}).status_code == 400   # ไม่ถึงขั้นทดสอบ
        assert save(admin, {"DENGUE_DATA_AUDITED": "1"}).status_code == 200
    assert connect.calls == []


def test_save_with_warn_only_db_test_goes_through(admin, app, path, connect):
    app.config["CHECK_DEPS"]["tables_source"] = lambda con: {}   # ตารางหายหมด = warn
    r = save(admin, GATED)
    assert r.status_code == 200 and connect.calls
    assert settings_store.read_overlay(path).values["DB_HOST"] == "10.1.2.3"


def test_corrupt_settings_file_does_not_break_test_or_save(admin, path, connect, sender):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json UNIQUE-JUNK", encoding="utf-8")
    assert db_test(admin, DB_VALUES).get_json()["ok"] is True
    assert moph_test(admin, {"MOPH_CLIENT_KEY": KEY_A}).status_code == 200
    assert post(admin, SEND, {"confirm": True, "values": {"MOPH_CLIENT_KEY": KEY_A,
                                                           "MOPH_SECRET_KEY": KEY_B}}).status_code == 200
    r = save(admin, GATED)
    assert r.status_code == 200
    assert settings_store.read_overlay(path).values["DB_HOST"] == "10.1.2.3"


# ---------------------------------------------------------------- timeout สั้นของเส้นทางทดสอบ

def test_run_db_test_passes_short_timeouts_to_connect():
    spy = ConnectSpy()
    settings_checks.run_db_test({"DB_HOST": "h", "DB_USER": "u", "DB_PASS": "p"},
                                connect=spy, tables_source=all_tables)
    assert spy.kwargs == [{"connect_timeout": settings_checks.TEST_CONNECT_TIMEOUT_S,
                           "read_timeout": settings_checks.TEST_READ_TIMEOUT_S}]
    assert (settings_checks.TEST_CONNECT_TIMEOUT_S, settings_checks.TEST_READ_TIMEOUT_S) == (5, 10)


class _NullCur:
    def execute(self, sql, *a):
        pass

    def close(self):
        pass


class _NullCon:
    def cursor(self):
        return _NullCur()

    def close(self):
        pass


def test_make_connect_timeouts_default_unchanged_and_overridable(monkeypatch):
    made = []
    monkeypatch.setattr(wiring.pymysql, "connect", lambda **kw: made.append(kw) or _NullCon())
    wiring.make_connect("h", 3306, "hos", "u", "p")()
    wiring.make_connect("h", 3306, "hos", "u", "p", connect_timeout=5, read_timeout=10)()
    assert (made[0]["connect_timeout"], made[0]["read_timeout"]) == (10, 60)
    assert (made[1]["connect_timeout"], made[1]["read_timeout"]) == (5, 10)


def test_production_runtime_connection_still_uses_10_and_60(monkeypatch, tmp_path):
    made = []
    monkeypatch.setattr(wiring.pymysql, "connect", lambda **kw: made.append(kw) or _NullCon())
    rt = wiring.build_runtime({"SECRET_KEY": "test-secret-key-0123456789",
                               "DENGUE_DB_PATH": str(tmp_path / "d.db"), "DB_HOST": "h",
                               "DB_USER": "u", "DB_PASS": "p"})
    rt.source._connect().close()
    assert (made[0]["connect_timeout"], made[0]["read_timeout"]) == (10, 60)


# ---------------------------------------------------------------- ENV_PROVIDER (R7)

def test_default_env_provider_returns_current_unmasked_effective_env(app, path):
    settings_store.write_overlay(path, {"DB_PASS": DB_SECRET, "MOPH_CLIENT_KEY": KEY_A}, "seed")
    env = app.config["ENV_PROVIDER"]()
    assert env["DB_PASS"] == DB_SECRET and env["MOPH_CLIENT_KEY"] == KEY_A
    # อ่านไฟล์ปัจจุบันทุกครั้ง: ค่าที่แก้ภายหลังมีผลทันที
    settings_store.write_overlay(path, {"DB_PASS": "changed-pw-1"}, "seed")
    assert app.config["ENV_PROVIDER"]()["DB_PASS"] == "changed-pw-1"


def test_env_provider_does_not_leak_into_app_config_repr(tmp_path):
    env = {"SECRET_KEY": "test-secret-key-0123456789", "DENGUE_DB_PATH": str(tmp_path / "x" / "d.db"),
           "DENGUE_SCHEDULER": "0", "DB_HOST": "10.1.2.3", "DB_PASS": DB_SECRET,
           "MOPH_CLIENT_KEY": KEY_A, "MOPH_SECRET_KEY": KEY_B}
    app = wiring.build_app(env)
    provider = app.config["ENV_PROVIDER"]
    assert callable(provider)
    cfg = repr(app.config)
    for s in (DB_SECRET, KEY_A, KEY_B):   # (SECRET_KEY ของ Flask อยู่ใน config อยู่แล้ว ไม่เกี่ยวกับงานนี้)
        assert s not in cfg
    cur = provider()
    assert cur["DB_PASS"] == DB_SECRET and cur["MOPH_CLIENT_KEY"] == KEY_A and cur["DB_HOST"] == "10.1.2.3"
    # ซ้อนด้วยไฟล์ค่าตั้งปัจจุบัน
    settings_store.write_overlay(app.config["SETTINGS_PATH"], {"DB_HOST": "10.9.9.9"}, "seed")
    assert provider()["DB_HOST"] == "10.9.9.9"
    # ไม่เก็บสำเนา env ทั้งหมด: เฉพาะตัวแปรที่เกี่ยวข้อง (ตัวแปรอื่นเช่น DENGUE_SCHEDULER ไม่อยู่ใน closure)
    assert "DENGUE_SCHEDULER" not in provider() and "SECRET_KEY" not in provider()


# ---------------------------------------------------------------- ไม่มีความลับหลุดจากทุก response/log/audit

def test_no_secret_in_any_test_response_log_or_audit(admin, app, path, store, connect, tcp, sender, caplog):
    caplog.set_level(logging.DEBUG)
    bodies = []

    def grab(r):
        bodies.append(r.get_data(as_text=True))
        return r

    exc_text = f"Access denied for user 'u-{DB_SECRET}'@'host-{KEY_A}' (using password: {KEY_B})"
    connect.fail = pymysql.err.OperationalError(1045, exc_text)
    grab(db_test(admin, {**DB_VALUES, "DB_PASS": DB_SECRET}))
    connect.fail = RuntimeError(exc_text)
    grab(db_test(admin, {**DB_VALUES, "DB_PASS": DB_SECRET}))
    connect.fail = HosxpError(exc_text)
    grab(db_test(admin, {**DB_VALUES, "DB_PASS": DB_SECRET}))
    connect.fail = denied()
    grab(save(admin, {**DB_VALUES, "DB_PASS": DB_SECRET}))                        # 409 + steps
    tcp.fail = OSError(exc_text)
    grab(moph_test(admin, {"MOPH_CLIENT_KEY": KEY_A, "MOPH_SECRET_KEY": KEY_B}))
    sender.fail = NotifyError(f"{KEY_A} {KEY_B}")
    grab(post(admin, SEND, {"confirm": True, "values": {"MOPH_CLIENT_KEY": KEY_A,
                                                         "MOPH_SECRET_KEY": KEY_B}}))
    sender.fail = RuntimeError(exc_text)
    grab(post(admin, SEND, {"confirm": True, "values": {"MOPH_CLIENT_KEY": KEY_A,
                                                         "MOPH_SECRET_KEY": KEY_B}}))
    grab(post(admin, TEST, {"target": "db", "values": {"DB_PASS": DB_SECRET, "DB_PORT": "x"}}))  # 400

    secrets = (DB_SECRET, KEY_A, KEY_B, PW)
    for body in bodies:
        for s in secrets:
            assert s not in body
    for s in secrets:
        assert s not in caplog.text
        assert s not in json.dumps(store.audit(), ensure_ascii=False)
        assert s not in repr(app.config)


# ---------------------------------------------------------------- หน่วย: settings_checks (ไม่ผ่าน Flask)

def test_describe_error_never_uses_str_of_exception():
    d = settings_checks.describe_error
    assert d(denied()) == "รหัสผ่านหรือสิทธิ์ไม่ถูกต้อง (Access denied 1045)"
    assert d(pymysql.err.OperationalError(1049, "x")) == "ไม่พบฐานข้อมูล (1049)"
    assert d(pymysql.err.OperationalError(2003, "x")) == "ต่อไม่ได้ (2003)"
    assert d(pymysql.err.OperationalError(2013, "x")) == "หมดเวลา"
    assert d(pymysql.err.OperationalError(2006, "x")) == "หมดเวลา"
    assert d(pymysql.err.ProgrammingError(1146, "table secret_t")) == "ข้อผิดพลาดฐานข้อมูล (1146)"
    assert d(socket.timeout("a")) == "หมดเวลา" and d(TimeoutError("a")) == "หมดเวลา"
    assert d(KeyError("hunter2")) == "KeyError"
    # HosxpError: ข้อความคงที่ของแอปผ่านได้ ที่เหลือเป็นชื่อชนิดหรือรหัสของสาเหตุ
    assert d(HosxpError("ยังไม่ได้ตั้งค่า DB_HOST / DB_USER")) == "ยังไม่ได้ตั้งค่า DB_HOST / DB_USER"
    assert d(HosxpError("OperationalError: leaks 10.0.0.1")) == "HosxpError"
    try:
        try:
            raise denied()
        except Exception as inner:
            raise HosxpError(f"OperationalError: {inner}") from inner
    except HosxpError as wrapped:
        assert d(wrapped) == "รหัสผ่านหรือสิทธิ์ไม่ถูกต้อง (Access denied 1045)"


def test_run_db_test_is_injectable_and_closes_connection_on_error():
    spy = ConnectSpy()

    def boom(con):
        raise RuntimeError("x")
    steps = settings_checks.run_db_test(
        {"DB_HOST": "h", "DB_USER": "u", "DB_PASS": "p"}, connect=spy, tables_source=boom)
    assert spy.con.closed == 1
    assert [s["name"] for s in steps][:3] == ["ตั้งค่าครบ", "เชื่อมต่อฐานข้อมูล", "เซสชันอ่านอย่างเดียว"]


def test_run_db_test_whitespace_only_db_pass_counts_as_set():
    spy = ConnectSpy()
    steps = settings_checks.run_db_test(
        {"DB_HOST": "h", "DB_USER": "u", "DB_PASS": "   "}, connect=spy, tables_source=all_tables)
    assert steps[0]["status"] == "ok" and spy.calls[0]["password"] == "   "


def test_run_moph_test_unreadable_base_url_is_fail():
    tcp = TcpSpy()
    steps = settings_checks.run_moph_test({"MOPH_BASE_URL": "not a url"}, tcp_check=tcp)
    assert steps[-1]["status"] == "fail" and tcp.calls == []


def test_send_test_message_unit_keys_missing_no_send():
    sender = SenderSpy()
    steps = settings_checks.send_test_message({"MOPH_CLIENT_KEY": KEY_A}, sender=sender)
    assert sender.calls == [] and any(s["status"] == "fail" for s in steps)
    steps = settings_checks.send_test_message({"MOPH_CLIENT_KEY": "<Client ID ของกลุ่ม>",
                                               "MOPH_SECRET_KEY": KEY_B}, sender=sender)
    assert sender.calls == [] and any(s["status"] == "fail" for s in steps)


def test_default_tcp_check_is_the_setup_check_helper():
    assert settings_checks.default_tcp_check is setup_check.tcp_reachable


def test_default_tables_source_uses_hosxp_table_columns_on_the_open_connection():
    """ค่าเริ่มต้น: HosxpSource.table_columns บนการเชื่อมที่เปิดแล้ว (ไม่เปิดใหม่ ไม่ปิดก่อนเวลา)"""
    queries = []

    class Cur:
        def execute(self, sql, params=()):
            queries.append((sql, params))

        def fetchall(self):
            return [("ovst", "vn", "varchar")]

        def close(self):
            pass

    class Con:
        closed = 0

        def cursor(self):
            return Cur()

        def close(self):
            Con.closed += 1
    con = Con()
    cols = settings_checks.default_tables_source(con)
    assert cols == {"ovst": {"vn": "varchar"}} and Con.closed == 0
    # ถามแต่ information_schema (โครงสร้าง) — ไม่อ่านแถวของตารางผู้ป่วย
    assert all("information_schema" in q[0] or "max_statement_time" in q[0].lower() for q in queries)


# ---------------------------------------------------------------- ThreadSafety ของตัวจำกัด (เบา ๆ)

def test_limiter_is_thread_safe_enough():
    lim = settings_views.SlidingWindowLimiter(clock=Clock())
    results = []

    def worker():
        results.append(lim.allow("test", "u"))
    threads = [threading.Thread(target=worker) for _ in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 10
