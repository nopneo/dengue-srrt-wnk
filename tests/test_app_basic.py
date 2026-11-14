"""เว็บพื้นฐาน: auth, รายการเคส, healthz, PrefixMiddleware (Task 6)."""
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from werkzeug.security import generate_password_hash

from app import PrefixMiddleware, create_app, sanitize_error, set_cycle_error
from auth import (LoginThrottle, api_write_guard, login_required, role_required,
                  safe_next)
from config import load_settings
from webfmt import area_text, icd_dot, sex_text, thai_date


def make_settings(tmp_path, **env):
    base = {"SECRET_KEY": "test-secret-key-0123456789", "DENGUE_DB_PATH": str(tmp_path / "d.db")}
    base.update(env)
    return load_settings(base)


@pytest.fixture(scope="session")
def pw_hashes():
    """แฮชรหัสผ่านทดสอบ คำนวณครั้งเดียวต่อรอบเทสต์ (pbkdf2 ช้า ~0.5 วินาที)."""
    return {pw: generate_password_hash(pw, method="pbkdf2:sha256")
            for pw in ("pw-nurse", "pw-view")}


def seed_users(store, pw_hashes):
    with store._connect() as con:
        for name, pw, role in (("nurse", "pw-nurse", "srrt"),
                               ("viewer1", "pw-view", "viewer")):
            con.execute('INSERT INTO "user" (username, password_hash, role) VALUES (?,?,?)',
                        (name, pw_hashes[pw], role))


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def app(settings, store, pw_hashes):
    seed_users(store, pw_hashes)
    return create_app(settings=settings, store=store)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def fast_auth(store, monkeypatch):
    """แทน check_user ด้วยตัวเทียบเร็ว (ไม่ใช้ pbkdf2) สำหรับเทสต์ throttle/next ที่ไม่เกี่ยวกับแฮช."""
    users = {"nurse": ("pw-nurse", "srrt"), "viewer1": ("pw-view", "viewer")}

    def fake(username, password):
        pw, role = users.get(username, (None, None))
        return role if pw is not None and pw == password else None

    monkeypatch.setattr(store, "check_user", fake)


def login(client, user="nurse", pw="pw-nurse", **extra):
    return client.post("/login", data={"username": user, "password": pw, **extra})


def add(store, key, hn="HN-SECRET-1", village="40010103", pin=None, in_area=True,
        days_ago=1, icd="A91", sex="1", age=30, visit="VN"):
    cid = store.add_case(f"{visit}:{key}", visit, hn, icd,
                         datetime.now() - timedelta(days=days_ago), village, "12",
                         sex, age, in_area)
    if pin:
        store.set_pin(cid, 14.0, 101.0, pin, "test", "nurse")
    return cid


# ---------- auth ----------

def test_cases_requires_login(client):
    r = client.get("/")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_login_ok_and_wrong_password(client):
    bad = login(client, pw="wrong")
    assert bad.status_code == 200
    assert "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง" in bad.get_data(as_text=True)
    assert client.get("/").status_code == 302
    # ผู้ใช้ที่ไม่มีอยู่ ต้องได้ข้อความเดียวกัน (ไม่เปิดเผยว่ามีผู้ใช้นี้หรือไม่)
    nouser = login(client, user="ghost", pw="x")
    assert "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง" in nouser.get_data(as_text=True)
    ok = login(client)
    assert ok.status_code == 302
    assert client.get("/").status_code == 200
    with client.session_transaction() as s:
        assert s["user"] == "nurse" and s["role"] == "srrt"


def test_login_throttle_429_then_expires_and_success_clears(client, app, fast_auth):
    t = [1000.0]
    app.extensions["login_throttle"].clock = lambda: t[0]
    for _ in range(4):
        assert login(client, pw="bad").status_code == 200
    assert login(client).status_code == 302  # สำเร็จ -> ล้างตัวนับ
    client.post("/logout")
    for _ in range(4):
        assert login(client, pw="bad").status_code == 200
    # ถ้า clear() ไม่ทำงาน จะสะสมเป็น 8 ครั้งและโดนล็อกแล้ว
    assert login(client).status_code == 302
    client.post("/logout")
    for _ in range(5):
        login(client, pw="bad")
    blocked = login(client)  # รหัสถูกก็ยังโดนบล็อก
    assert blocked.status_code == 429
    assert "ลองใหม่" in blocked.get_data(as_text=True)
    # ผู้ใช้อื่นไม่ถูกกระทบ
    assert login(client, user="viewer1", pw="pw-view").status_code == 302
    client.post("/logout")
    t[0] += 601
    assert login(client).status_code == 302


def test_login_throttle_keyed_by_forwarded_for_last_entry(client, fast_auth):
    h = {"X-Forwarded-For": "1.1.1.1, 9.9.9.9"}
    for _ in range(5):
        client.post("/login", data={"username": "Nurse", "password": "bad"}, headers=h)
    # username เทียบแบบ lowercase และ key = ท้ายสุดของ XFF
    r = client.post("/login", data={"username": "nurse", "password": "pw-nurse"},
                    headers={"X-Forwarded-For": "5.5.5.5, 9.9.9.9"})
    assert r.status_code == 429
    r = client.post("/login", data={"username": "nurse", "password": "pw-nurse"},
                    headers={"X-Forwarded-For": "9.9.9.8"})
    assert r.status_code == 302


def test_throttle_unit_expiry_and_purge():
    t = [0.0]
    th = LoginThrottle(clock=lambda: t[0])
    for _ in range(5):
        th.fail("k")
    assert th.retry_after("k") > 0
    t[0] += 601
    assert th.retry_after("k") == 0


def test_overlong_username_is_wrong_credentials_and_counted(client, fast_auth):
    long_name = "a" * 65
    r = login(client, user=long_name)
    assert r.status_code == 200
    assert "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง" in r.get_data(as_text=True)
    for _ in range(4):
        login(client, user=long_name)
    assert login(client, user=long_name).status_code == 429


@pytest.mark.parametrize("nxt,expected", [
    ("/case/3", "/case/3"),
    ("//evil.com", "/"),
    ("https://evil.com/x", "/"),
    ("/\\evil.com", "/"),
    ("javascript:alert(1)", "/"),
    ("", "/"),
])
def test_login_next_only_relative(client, fast_auth, nxt, expected):
    r = login(client, next=nxt)
    assert r.status_code == 302
    assert r.headers["Location"] == expected


@pytest.mark.parametrize("target,ok", [
    ("/x", True), ("//x", False), ("http://x", False), ("/a\nb", False), (None, False),
    ("x", False),
])
def test_safe_next_unit(target, ok):
    assert (safe_next(target) is not None) is ok


def test_login_required_redirect_carries_next(client, fast_auth):
    r = client.get("/?status=all")
    assert r.status_code == 302
    assert r.headers["Location"].startswith("/login?next=")
    r2 = login(client, next="/?status=all")
    assert r2.headers["Location"] == "/?status=all"


def test_login_next_keeps_script_prefix(client, fast_auth):
    hdr = {"X-Forwarded-Prefix": "/dengue"}
    r = client.get("/?status=all", headers=hdr)
    loc = urlparse(r.headers["Location"])
    assert loc.path == "/dengue/login"
    nxt = parse_qs(loc.query)["next"][0]
    assert nxt == "/dengue/?status=all"
    r2 = client.post("/login", data={"username": "nurse", "password": "pw-nurse",
                                     "next": nxt}, headers=hdr)
    assert r2.headers["Location"] == "/dengue/?status=all"


def test_logout_is_post_only(client, login_as):
    login_as(client)
    assert client.get("/logout").status_code == 405
    assert client.get("/").status_code == 200
    r = client.post("/logout")
    assert r.status_code == 302
    assert client.get("/").status_code == 302


def test_non_get_without_session_gets_401_not_redirect(app, client):
    @app.post("/do-something")
    @login_required
    def do_something():
        return "ok"

    r = client.post("/do-something")
    assert r.status_code == 401
    assert "Location" not in r.headers
    assert "เข้าสู่ระบบ" in r.get_data(as_text=True)


def test_api_paths_get_json_401_and_403(app, client, login_as):
    @app.get("/api/ping")
    @login_required
    def ping():
        return {"ok": True}

    @app.get("/api/srrt-only")
    @role_required("srrt")
    def srrt_only():
        return {"ok": True}

    @app.get("/srrt-page")
    @role_required("srrt")
    def srrt_page():
        return "ok"

    r = client.get("/api/ping")
    assert r.status_code == 401 and r.get_json()["error"]
    assert client.get("/api/srrt-only").status_code == 401
    login_as(client, "viewer1", "viewer")
    r = client.get("/api/srrt-only")
    assert r.status_code == 403 and r.is_json
    r = client.get("/srrt-page")
    assert r.status_code == 403 and "ไม่มีสิทธิ์" in r.get_data(as_text=True)
    login_as(client, "nurse", "srrt")
    assert client.get("/srrt-page").status_code == 200


def test_api_write_guard(app, client, login_as):
    @app.post("/api/test-write")
    @api_write_guard
    def test_write():
        return {"ok": True}

    ok_headers = {"X-Requested-With": "dengue"}
    # 1) ไม่ login -> 401 JSON
    r = client.post("/api/test-write", json={}, headers=ok_headers)
    assert r.status_code == 401 and r.is_json
    login_as(client)
    # 2) ไม่ใช่ JSON -> 415 JSON
    r = client.post("/api/test-write", data={"a": "1"}, headers=ok_headers)
    assert r.status_code == 415 and r.is_json
    # 3) ไม่มีหัว csrf / ค่าผิด -> 403 {"error": "csrf"}
    r = client.post("/api/test-write", json={})
    assert r.status_code == 403 and r.get_json() == {"error": "csrf"}
    r = client.post("/api/test-write", json={}, headers={"X-Requested-With": "other"})
    assert r.status_code == 403 and r.get_json() == {"error": "csrf"}
    # ผ่าน
    r = client.post("/api/test-write", json={"a": 1}, headers=ok_headers)
    assert r.status_code == 200 and r.get_json() == {"ok": True}


# ---------- รายการเคส ----------

def test_cases_lists_with_pin_badges(client, store, login_as):
    add(store, "1", pin=None, hn="HN-AAA", icd="A911")
    add(store, "2", pin="estimated", hn="HN-BBB", village="40010105", sex="2")
    add(store, "3", pin="confirmed", hn="HN-CCC", in_area=False)
    cancelled = add(store, "4", hn="HN-DDD")
    store.mark_cancelled(cancelled)
    login_as(client)
    html = client.get("/").get_data(as_text=True)
    assert "รอปักหมุด" in html and "ประมาณ" in html and "ยืนยันแล้ว" in html
    assert "นอกเขต" in html and "ในเขต" in html
    assert "A91.1" in html
    assert "หมู่ 3" in html and "หมู่ 5" in html
    assert "ชาย" in html and "หญิง" in html
    assert "SRRT ไข้เลือดออก" in html
    for hn in ("HN-AAA", "HN-BBB", "HN-CCC"):
        assert hn not in html  # ไม่แสดง HN ในรายการ
    assert f'href="/case/{cancelled}"' not in html  # ยกเลิกแล้วไม่โชว์ใน active
    cancelled_html = client.get("/?status=cancelled").get_data(as_text=True)
    assert f'href="/case/{cancelled}"' in cancelled_html
    assert "HN-DDD" not in cancelled_html


def test_dates_render_in_buddhist_era(client, store, login_as):
    add(store, "1", days_ago=1)
    login_as(client)
    html = client.get("/").get_data(as_text=True)
    be = (datetime.now() - timedelta(days=1)).year + 543
    assert re.search(rf"\d{{1,2}} \S+ {be} \d\d:\d\d", html)


def test_kpis_count_active_only(client, store, login_as):
    # active=8, pending=4, confirmed=2, out=1 (ไม่ซ้ำกันสักช่อง); ยกเลิก 1 ราย ต้องไม่นับ
    for i in range(4):
        add(store, f"p{i}")
    for i in range(2):
        add(store, f"c{i}", pin="confirmed")
    add(store, "e1", pin="estimated")
    add(store, "o1", pin="estimated", in_area=False)
    store.mark_cancelled(add(store, "x", pin="confirmed", in_area=False))
    login_as(client)
    html = client.get("/").get_data(as_text=True)
    expected = {"kpi-active": 8, "kpi-pending": 4, "kpi-confirmed": 2, "kpi-out": 1}
    for testid, n in expected.items():
        m = re.search(rf'data-testid="{testid}"[^>]*>\s*(\d+)\s*<', html)
        assert m, testid
        assert int(m.group(1)) == n, (testid, m.group(1))


def test_same_village_estimated_badge(client, store, login_as):
    add(store, "1", pin="estimated", village="40010103")
    add(store, "2", pin="estimated", village="40010103")
    add(store, "3", pin="estimated", village="40010107")  # หมู่เดียว ไม่ถึง 2
    add(store, "4", pin="confirmed", village="40010107")  # confirmed ไม่นับ
    gone = add(store, "5", pin="estimated", village="40010107")
    store.mark_cancelled(gone)  # ยกเลิกไม่นับ
    login_as(client)
    html = client.get("/").get_data(as_text=True)
    assert "หมู่บ้านเดียวกัน 2 ราย (ยังไม่ปักหมุดครบ)" in html
    assert html.count('class="same-village-bar"') == 1
    assert "หมู่ 3" in html


def test_audit_banner_visible_when_not_audited(client, login_as):
    login_as(client)
    html = client.get("/").get_data(as_text=True)
    assert "ตัวเลขยังไม่ได้ตรวจกับข้อมูลของโรงพยาบาลนี้" in html
    assert 'data-testid="audit-banner"' in html
    assert "no-print" in html and "@media print" in html
    assert "dengue_audited" in html


def test_audit_banner_absent_when_audited(tmp_path, store, login_as):
    app = create_app(settings=make_settings(tmp_path, DENGUE_DATA_AUDITED="1"),
                     store=store)
    c = app.test_client()
    login_as(c)
    assert 'data-testid="audit-banner"' not in c.get("/").get_data(as_text=True)


def test_filters_invalid_values_fall_back(client, store, login_as):
    add(store, "1")
    login_as(client)
    for q in ("since=garbage", "status=zzz", "area=zzz", "since=9999-99-99&status=&area=",
              "since=0001-01-01"):
        assert client.get("/?" + q).status_code == 200


def test_filters_area_and_since(client, store, login_as):
    add(store, "in", in_area=True, days_ago=1)
    out = add(store, "out", in_area=False, days_ago=1)
    old = add(store, "old", days_ago=30)
    login_as(client)
    html = client.get("/?area=out").get_data(as_text=True)
    assert f'href="/case/{out}"' in html and 'href="/case/1"' not in html
    assert f'href="/case/{old}"' not in client.get("/").get_data(as_text=True)
    since = (datetime.now() - timedelta(days=60)).date().isoformat()
    assert f'href="/case/{old}"' in client.get(f"/?since={since}").get_data(as_text=True)


def test_no_external_resources_in_page(client, login_as):
    login_as(client)
    html = client.get("/").get_data(as_text=True)
    assert "http://" not in html and "https://" not in html
    assert '<html lang="th"' in html


# ---------- healthz / app ----------

def test_healthz_public_has_no_patient_fields(client, app, store):
    add(store, "1", hn="HN-SECRET-9")
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "hn" not in r.get_json() and "HN-SECRET-9" not in body
    j = r.get_json()
    assert re.fullmatch(r"[0-9a-f]{16}", j.pop("boot_id"))   # รหัสสุ่มต่อโปรเซส ไม่ใช่ข้อมูลผู้ป่วย
    assert j == {"ok": True, "last_cycle_at": None, "last_error": None,
                 "scheduler_expected": False, "alerts_failing": 0,
                 "alerts_given_up": 0, "settings_error": False}
    assert "cases_active" not in r.get_json()   # จำนวนเคสไม่เปิดสาธารณะ
    app.config["CYCLE_STATE"]["last_error"] = "boom"
    app.config["CYCLE_STATE"]["last_cycle_at"] = "2026-11-08T10:00:00"
    j = client.get("/healthz").get_json()
    assert j["ok"] is False and j["last_error"] == "boom"
    assert j["last_cycle_at"] == "2026-11-08T10:00:00"


def test_set_cycle_error_sanitizes(app, client):
    class Boom(Exception):
        pass

    msg = "ต่อฐานไม่ได้ HN 1234567 cid 1234567890123\nบรรทัดสอง " + "x" * 300
    set_cycle_error(app, Boom(msg))
    err = client.get("/healthz").get_json()["last_error"]
    assert err.startswith("Boom: ")
    assert "1234567" not in err and "…" in err
    assert "\n" not in err
    assert len(err) <= len("Boom: ") + 120
    set_cycle_error(app, "ok 123 and 99999")
    assert app.config["CYCLE_STATE"]["last_error"] == "ok 123 and …"
    set_cycle_error(app, ValueError())
    assert app.config["CYCLE_STATE"]["last_error"] == "ValueError"
    assert sanitize_error(Boom("a\r\nb")) == "Boom: a b"
    set_cycle_error(app, None)
    j = client.get("/healthz").get_json()
    assert j["last_error"] is None and j["ok"] is True


DRIVER_MESSAGES = [
    "OperationalError: (1045, \"Access denied for user 'dbuser-xyz'@'10.77.66.55' (using password: YES)\")",
    "OperationalError: (2003, \"Can't connect to MySQL server on '10.20.30.40' ([Errno 111] Connection refused)\")",
    "OperationalError: (1044, \"Access denied for user 'reader'@'192.168.1.9' to database 'hos'\")",
    "OperationalError: (1130, \"Host '192.168.70.5' is not allowed to connect to this MariaDB server\")",
    "InternalError: (1049, \"Unknown database 'hos_secret'\")",
]


def _no_leak(text):
    for leak in ("dbuser-xyz", "reader", "10.77", "10.20", "192.168", "'@'", "Access denied", "Connection refused",
                 "hos_secret", "MariaDB", "password", "Can't connect"):
        assert leak not in text, (leak, text)


def test_sanitize_error_driver_errors_keep_only_type_and_mysql_code():
    from hosxp import HosxpError
    expected = ["OperationalError (1045)", "OperationalError (2003)", "OperationalError (1044)",
                "OperationalError (1130)", "InternalError (1049)"]
    for msg, want in zip(DRIVER_MESSAGES, expected):
        # ข้อความที่ detector เก็บ (str ของ HosxpError ไม่มีชื่อชนิดนอก) และตัว exception เอง (ชื่อชนิดนอก + ใน)
        assert sanitize_error(msg) == want, msg
        assert sanitize_error(HosxpError(msg)) == f"HosxpError: {want}", msg
        _no_leak(sanitize_error(msg))
        _no_leak(sanitize_error(HosxpError(msg)))


def test_sanitize_error_driver_text_without_code_or_type_keeps_nothing_sensitive():
    for msg in ("Access denied for user 'bob'@'10.1.2.3'", "cannot reach 10.1.2.3 as 'bob'@'host'"):
        out = sanitize_error(msg)
        _no_leak(out)
        assert "bob" not in out and "10.1.2.3" not in out
    from hosxp import HosxpError
    out = sanitize_error(HosxpError("failed for 'bob'@'10.1.2.3'"))
    assert out == "HosxpError" and "bob" not in out


def test_sanitize_error_keeps_fixed_thai_messages_unchanged():
    from detector import MASS_CANCEL_NOTICE
    from hosxp import MSG_DB_NOT_CONFIGURED, MSG_READ_ONLY_FAILED
    for text in (MSG_DB_NOT_CONFIGURED, MASS_CANCEL_NOTICE, "ยังไม่ได้ตั้งค่า MOPH Notify", "HosxpError: ต่อไม่ได้",
                 "ค่าบางตัวในไฟล์ค่าตั้งไม่ถูกต้องจึงไม่ถูกใช้: DB_HOST", f"{MSG_READ_ONLY_FAILED} (OperationalError)"):
        assert sanitize_error(text) == text
    from hosxp import HosxpError
    assert sanitize_error(HosxpError(MSG_DB_NOT_CONFIGURED)) == f"HosxpError: {MSG_DB_NOT_CONFIGURED}"


def test_set_cycle_error_driver_text_never_reaches_healthz(app, client):
    from hosxp import HosxpError
    for msg in DRIVER_MESSAGES:
        set_cycle_error(app, msg)                 # ข้อความที่ detector คืน (result.error = str(HosxpError))
        _no_leak(client.get("/healthz").get_data(as_text=True))
        set_cycle_error(app, HosxpError(msg))     # exception ตรง ๆ จากรอบที่ล้ม
        j = client.get("/healthz").get_json()
        _no_leak(j["last_error"])
        assert j["last_error"].startswith("HosxpError: ")


def test_create_app_requires_secret_key(tmp_path, store):
    with pytest.raises(RuntimeError):
        create_app(settings=make_settings(tmp_path, SECRET_KEY=""), store=store)


def test_session_cookie_flags_and_cycle_state(app):
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["CYCLE_STATE"] == {"last_cycle_at": None, "last_error": None}


def test_security_headers(client):
    r = client.get("/healthz")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "same-origin"


def test_static_css_and_fonts_served(client):
    bodies = {}
    for path in ("/static/app.css", "/static/fonts.css",
                 "/static/fonts/Kanit-SemiBold.ttf", "/static/fonts/Sarabun-Regular.ttf"):
        r = client.get(path)
        try:
            assert r.status_code == 200, path
            bodies[path] = r.get_data(as_text=path.endswith(".css"))
        finally:
            r.close()  # ปิดไฟล์ static (กัน ResourceWarning)
    for token in ("--navy:#0b2e8a", "--g-red", "14px"):
        assert token in bodies["/static/app.css"]
    assert "http" not in bodies["/static/fonts.css"]


# ---------- PrefixMiddleware ----------

def run_middleware(**env):
    seen = {}

    def inner(environ, start_response):
        seen.update(environ)
        start_response("200 OK", [])
        return [b""]

    PrefixMiddleware(inner)({"PATH_INFO": "/", "wsgi.url_scheme": "http", **env},
                            lambda *a: None)
    return seen


def test_prefix_middleware_sets_script_name_and_scheme():
    seen = run_middleware(HTTP_X_FORWARDED_PREFIX="/dengue/",
                          HTTP_X_FORWARDED_PROTO="https")
    assert seen["SCRIPT_NAME"] == "/dengue"
    assert seen["wsgi.url_scheme"] == "https"


@pytest.mark.parametrize("prefix", ["dengue", "//evil", "/a//b", "/a/../b", "/..", ""])
def test_prefix_middleware_ignores_bad_prefix(prefix):
    seen = run_middleware(HTTP_X_FORWARDED_PREFIX=prefix)
    assert "SCRIPT_NAME" not in seen


def test_prefix_middleware_proto_first_entry_and_whitelist():
    assert run_middleware(HTTP_X_FORWARDED_PROTO="https, http")["wsgi.url_scheme"] == "https"
    assert run_middleware(HTTP_X_FORWARDED_PROTO="HTTPS")["wsgi.url_scheme"] == "https"
    assert run_middleware(HTTP_X_FORWARDED_PROTO="ftp")["wsgi.url_scheme"] == "http"
    assert run_middleware(HTTP_X_FORWARDED_PROTO="javascript, https")["wsgi.url_scheme"] == "http"


def test_prefix_applies_to_generated_links(client):
    r = client.get("/login", headers={"X-Forwarded-Prefix": "/dengue",
                                      "X-Forwarded-Proto": "https"})
    html = r.get_data(as_text=True)
    assert 'href="/dengue/static/app.css"' in html
    assert 'action="/dengue/login"' in html


# ---------- webfmt ----------

def test_webfmt_helpers():
    assert icd_dot("A911") == "A91.1" and icd_dot("A90") == "A90" and icd_dot(None) == "-"
    assert sex_text("1") == "ชาย" and sex_text("2") == "หญิง" and sex_text(None) == "-"
    assert area_text("40010103") == "หมู่ 3"
    assert area_text("40010100") == "-"  # หมู่ 0
    assert area_text("4001") == "-" and area_text(None) == "-"
    assert thai_date(datetime(2026, 11, 8)) == "8 พ.ย. 2569"


@pytest.mark.parametrize("bad", ["", "   ", "short", "0123456789abcde",
                                 "<สุ่มยาว 32 ตัวอักษรขึ้นไป>", "<x>-padding-padding-padding"])
def test_create_app_rejects_weak_or_placeholder_secret_key(tmp_path, store, bad):
    with pytest.raises(RuntimeError) as exc:
        create_app(settings=make_settings(tmp_path, SECRET_KEY=bad), store=store)
    assert bad.strip() == "" or bad not in str(exc.value)  # ไม่พิมพ์ค่า


def test_create_app_accepts_sixteen_char_secret_key(tmp_path, store):
    create_app(settings=make_settings(tmp_path, SECRET_KEY="0123456789abcdef"), store=store)


# ---------- แถบเตือนส่ง LINE ไม่สำเร็จ + healthz (Task 11b) ----------

def fail_alerts(store, case_id, n, kind="new"):
    for _ in range(n):
        store.log_alert(kind, case_id, None, False, 401, "NotifyError: ข้อความลับ HN-SECRET-1")


def banners(client, path="/"):
    html = client.get(path).get_data(as_text=True)
    return (html, 'data-testid="alert-failing-banner"' in html,
            'data-testid="alert-given-up-banner"' in html)


def test_no_alert_banners_when_all_sent(client, store, login_as):
    login_as(client)
    cid = add(store, "1")
    store.log_alert("new", cid, None, True, None, None)
    _, failing, given_up = banners(client)
    assert not failing and not given_up


def test_failing_banner_shows_count_for_every_role(client, store, login_as):
    from alerts import MAX_TOTAL_ATTEMPTS
    a, b = add(store, "1", hn="H1"), add(store, "2", hn="H2")
    fail_alerts(store, a, 2)
    fail_alerts(store, b, MAX_TOTAL_ATTEMPTS - 1)
    for role in ("srrt", "viewer"):
        login_as(client, "u", role)
        html, failing, given_up = banners(client)
        assert failing and not given_up
        assert "ส่ง LINE ไม่สำเร็จ 2 รายการ — ระบบกำลังลองใหม่อัตโนมัติ" in html
        m = re.search(r'<div[^>]*data-testid="alert-failing-banner"[^>]*>', html)
        assert 'role="status"' in m.group(0) and "no-print" in m.group(0)
        assert "HN-SECRET-1" not in html and "ข้อความลับ" not in html


def test_given_up_banner_is_red_alert(client, store, login_as):
    from alerts import MAX_TOTAL_ATTEMPTS
    login_as(client)
    stuck, retrying = add(store, "1", hn="H1"), add(store, "2", hn="H2")
    fail_alerts(store, stuck, MAX_TOTAL_ATTEMPTS)
    fail_alerts(store, retrying, 1)
    html, failing, given_up = banners(client)
    assert given_up
    assert ("หยุดส่งแล้ว 1 รายการ — ตรวจกุญแจ MOPH Notify และกลุ่ม LINE "
            "แล้วแจ้งผู้ดูแลระบบ") in html
    m = re.search(r'<div[^>]*data-testid="alert-given-up-banner"[^>]*>', html)
    assert 'role="alert"' in m.group(0) and "no-print" in m.group(0)
    # รายการที่ยังลองใหม่อยู่ (ไม่รวมที่หยุดส่งแล้ว) แสดงแยก ไม่ให้ข้อความขัดกัน
    assert failing and "ส่ง LINE ไม่สำเร็จ 1 รายการ" in html


def test_only_given_up_items_do_not_claim_retrying(client, store, login_as):
    from alerts import MAX_TOTAL_ATTEMPTS
    login_as(client)
    fail_alerts(store, add(store, "1"), MAX_TOTAL_ATTEMPTS)
    _, failing, given_up = banners(client)
    assert given_up and not failing


def test_alert_banners_on_map_page_and_not_on_login_or_case_page(client, store, login_as):
    login_as(client)
    cid = add(store, "1")
    fail_alerts(store, cid, 1)
    assert banners(client, "/map")[1:] == (True, False)
    assert banners(client, f"/case/{cid}")[1:] == (False, False)


def test_alert_banner_css_has_red_variant():
    css = (Path(__file__).resolve().parent.parent / "static" / "app.css").read_text(
        encoding="utf-8")
    assert ".alert-bar" in css and ".alert-bar-err" in css


def test_healthz_alert_counts_and_ok_flag(client, app, store):
    from alerts import MAX_TOTAL_ATTEMPTS
    a, b = add(store, "1", hn="H1"), add(store, "2", hn="H2")
    fail_alerts(store, a, 3)
    j = client.get("/healthz").get_json()
    assert (j["alerts_failing"], j["alerts_given_up"], j["ok"]) == (1, 0, True)
    fail_alerts(store, b, MAX_TOTAL_ATTEMPTS)
    j = client.get("/healthz").get_json()
    assert (j["alerts_failing"], j["alerts_given_up"], j["ok"]) == (2, 1, False)
    assert j["last_error"] is None
    assert isinstance(j["alerts_failing"], int) and isinstance(j["alerts_given_up"], int)
    body = client.get("/healthz").get_data(as_text=True)
    assert "HN-SECRET-1" not in body and "H1" not in body and "ข้อความลับ" not in body


def test_healthz_ok_false_when_last_error_even_without_alert_failures(client, app):
    app.config["CYCLE_STATE"]["last_error"] = "boom"
    j = client.get("/healthz").get_json()
    assert j["ok"] is False and j["alerts_failing"] == 0 and j["alerts_given_up"] == 0


def test_alert_bars_db_error_logs_type_only_and_page_still_works(client, app, store,
                                                                 login_as, monkeypatch, caplog):
    import logging

    def boom(max_total, **kw):
        raise RuntimeError("secret-detail HN-SECRET-1")

    monkeypatch.setattr(store, "alert_health", boom)
    login_as(client)
    with caplog.at_level(logging.WARNING):
        r = client.get("/")
    assert r.status_code == 200
    assert "alert-failing-banner" not in r.get_data(as_text=True)
    text = " ".join(rec.getMessage() for rec in caplog.records)
    assert "RuntimeError" in text and "secret-detail" not in text and "HN-SECRET-1" not in text


# ---------- /healthz ต้องไม่บอก ok เมื่อตัวตรวจจับไม่ทำงาน (R24/I2) ----------

NOW_H = datetime(2026, 11, 9, 12, 0, 0)


@pytest.fixture
def hz(app, client):
    """healthz แบบคาดว่ามี scheduler: นาฬิกาฉีดได้ (poll 120 วินาที -> เกณฑ์ 360 วินาที)."""
    clock = {"now": NOW_H}
    app.config["CLOCK"] = lambda: clock["now"]
    app.config["STARTED_AT"] = NOW_H
    app.config["SCHEDULER_EXPECTED"] = True

    def get(advance_s=0, last_cycle=None):
        clock["now"] = NOW_H + timedelta(seconds=advance_s)
        app.config["CYCLE_STATE"]["last_cycle_at"] = (
            last_cycle.isoformat(timespec="seconds") if last_cycle else None)
        return client.get("/healthz").get_json()
    return get


def test_healthz_reports_scheduler_expected_flag(app, client):
    assert client.get("/healthz").get_json()["scheduler_expected"] is False
    app.config["SCHEDULER_EXPECTED"] = True
    assert client.get("/healthz").get_json()["scheduler_expected"] is True


def test_healthz_grace_period_after_startup_without_cycle(hz):
    assert hz(0)["ok"] is True
    assert hz(360)["ok"] is True                  # ยังไม่เกิน 3 x poll (120 s)


def test_healthz_not_ok_when_no_cycle_after_grace(hz):
    j = hz(361)
    assert j["ok"] is False and j["last_cycle_at"] is None and j["scheduler_expected"] is True
    assert j["last_error"] is None                # บอกผ่าน ok เท่านั้น ไม่ใส่ข้อความใหม่


def test_healthz_ok_with_recent_cycle_and_not_ok_when_stale(hz):
    last = NOW_H + timedelta(seconds=1000)
    assert hz(1000 + 360, last_cycle=last)["ok"] is True
    assert hz(1000 + 361, last_cycle=last)["ok"] is False


def test_healthz_stale_check_skipped_when_scheduler_not_expected(app, hz):
    app.config["SCHEDULER_EXPECTED"] = False
    assert hz(10_000)["ok"] is True
    assert hz(10_000, last_cycle=NOW_H)["ok"] is True


def test_healthz_stale_threshold_follows_poll_seconds(tmp_path, store, pw_hashes):
    app = create_app(settings=make_settings(tmp_path, DENGUE_POLL_SECONDS="10"), store=store)
    app.config.update(SCHEDULER_EXPECTED=True, STARTED_AT=NOW_H,
                      CLOCK=lambda: NOW_H + timedelta(seconds=31))
    assert app.test_client().get("/healthz").get_json()["ok"] is False   # > 3 x 10


def test_healthz_existing_error_rules_still_apply_when_fresh(app, hz):
    app.config["CYCLE_STATE"]["last_error"] = "boom"
    j = hz(10, last_cycle=NOW_H)
    assert j["ok"] is False and j["last_error"] == "boom"


# ---------- คุกกี้ session: Secure/Path (R24/I4) ----------

def _session_cookie(resp):
    cookies = [c for c in resp.headers.getlist("Set-Cookie") if c.startswith("dengue_session=")]
    assert len(cookies) == 1, resp.headers.getlist("Set-Cookie")
    return cookies[0]


def test_session_cookie_secure_and_scoped_behind_https_prefix_proxy(client, fast_auth):
    proxy = {"X-Forwarded-Proto": "https", "X-Forwarded-Prefix": "/dengue"}
    r = client.post("/login", data={"username": "nurse", "password": "pw-nurse"}, headers=proxy)
    assert r.status_code == 302
    cookie = _session_cookie(r)
    assert "Secure" in cookie and "Path=/dengue" in cookie
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


def test_session_cookie_not_secure_on_plain_lan_http(client, fast_auth):
    r = client.post("/login", data={"username": "nurse", "password": "pw-nurse"})
    cookie = _session_cookie(r)
    assert "Secure" not in cookie and "Path=/;" in cookie + ";"
    assert client.get("/").status_code == 200        # LAN http ยังใช้งานได้


def test_session_cookie_secure_flag_follows_each_request_scheme(client, fast_auth):
    r = client.post("/login", data={"username": "nurse", "password": "pw-nurse"},
                    headers={"X-Forwarded-Proto": "https"})
    cookie = _session_cookie(r)
    assert "Secure" in cookie and "Path=/;" in cookie + ";"      # ไม่มี prefix = ราก


def test_logout_clears_cookie_with_same_scope(client, fast_auth, login_as):
    proxy = {"X-Forwarded-Proto": "https", "X-Forwarded-Prefix": "/dengue"}
    client.post("/login", data={"username": "nurse", "password": "pw-nurse"}, headers=proxy)
    r = client.post("/logout", headers=proxy)
    cookie = _session_cookie(r)
    assert "Path=/dengue" in cookie and "Secure" in cookie


# ---------- ตรวจ session กับฐานทุกคำขอ (R24/I5) ----------

PIN = {"lat": 14.35, "lng": 101.9, "state": "confirmed"}
JSON_HDR = {"X-Requested-With": "dengue"}


def _set_role(store, username, role):
    with store._connect() as con:
        con.execute('UPDATE "user" SET role=? WHERE username=?', (role, username))


def test_removed_user_session_rejected_on_next_request(client, store, fast_auth):
    cid = add(store, "1")
    assert login(client).status_code == 302
    assert client.get("/").status_code == 200
    store.remove_user("nurse")
    r = client.get("/")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    with client.session_transaction() as s:
        assert "user" not in s                              # session ถูกล้าง
    r = client.get(f"/case/{cid}")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_removed_user_gets_json_401_on_api(client, store, fast_auth):
    cid = add(store, "1")
    login(client)
    store.remove_user("nurse")
    r = client.post(f"/api/case/{cid}/pin", json=PIN, headers=JSON_HDR)
    assert r.status_code == 401 and r.is_json
    assert store.get_pin(cid) is None


def test_demoted_user_loses_write_access_next_request(client, store, fast_auth):
    cid = add(store, "1")
    login(client)
    assert client.post(f"/api/case/{cid}/pin", json=PIN, headers=JSON_HDR).status_code == 200
    _set_role(store, "nurse", "viewer")
    r = client.post(f"/api/case/{cid}/pin", json=PIN, headers=JSON_HDR)
    assert r.status_code == 403
    assert client.get("/").status_code == 200               # ยังอ่านได้ในฐานะ viewer
    with client.session_transaction() as s:
        assert s["role"] == "viewer"                        # role ในคุกกี้ตามฐาน


def test_promoted_user_gains_access_without_relogin(client, store, fast_auth):
    cid = add(store, "1")
    login(client, user="viewer1", pw="pw-view")
    assert client.post(f"/api/case/{cid}/pin", json=PIN, headers=JSON_HDR).status_code == 403
    _set_role(store, "viewer1", "srrt")
    assert client.post(f"/api/case/{cid}/pin", json=PIN, headers=JSON_HDR).status_code == 200


def test_api_write_guard_alone_also_rechecks_user(app, client, store, fast_auth):
    from auth import api_write_guard

    @app.post("/api/_probe")
    @api_write_guard
    def probe():
        return {"ok": True}

    login(client)
    assert client.post("/api/_probe", json={}, headers=JSON_HDR).status_code == 200
    store.remove_user("nurse")
    assert client.post("/api/_probe", json={}, headers=JSON_HDR).status_code == 401


def test_session_recheck_is_one_read_per_request(client, store, fast_auth, monkeypatch):
    login(client)
    calls = []
    real = store.user_role
    monkeypatch.setattr(store, "user_role", lambda u: calls.append(u) or real(u))
    r = client.get("/")
    assert r.status_code == 200 and calls == ["nurse"]
