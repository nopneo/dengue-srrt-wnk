"""wiring.build_runtime / build_app — ไม่ต่อ DB จริง (connect เป็นแบบ lazy)."""
import pytest

import scheduler
import wiring
from hosxp import HosxpError
from wiring import build_app, build_runtime, make_connect

ENV = {
    "SECRET_KEY": "flask-secret-VALUE-0123", "DENGUE_DB_PATH": "",
    "MOPH_CLIENT_KEY": "client-key-VALUE", "MOPH_SECRET_KEY": "secret-key-VALUE",
    "DB_HOST": "db.test", "DB_USER": "reader", "DB_PASS": "db-pass-VALUE",
}


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    scheduler.release_all_locks()


@pytest.fixture
def env(tmp_path):
    return {**ENV, "DENGUE_DB_PATH": str(tmp_path / "data" / "dengue.db")}


@pytest.fixture(autouse=True)
def no_real_connect(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("build ต้องไม่ต่อฐานข้อมูลตอนสร้าง")

    monkeypatch.setattr(wiring.pymysql, "connect", boom)


def test_build_runtime_sender_none_without_keys(env):
    for missing in ("MOPH_CLIENT_KEY", "MOPH_SECRET_KEY"):
        e = {k: v for k, v in env.items() if k != missing}
        assert build_runtime(e).sender is None


def test_build_runtime_has_sender_and_no_secrets_in_repr(env):
    rt = build_runtime(env)
    assert callable(rt.sender) and callable(rt.area_lookup)
    assert rt.area_lookup == rt.source.area_label
    text = repr(rt) + str(rt)
    for secret in ("flask-secret-VALUE", "client-key-VALUE", "secret-key-VALUE",
                   "db-pass-VALUE"):
        assert secret not in text
    assert rt.settings.moph_client_key == "client-key-VALUE"


def test_build_runtime_sender_calls_moph_with_settings(env, monkeypatch):
    calls = []
    monkeypatch.setattr(wiring.moph_notify, "send_messages",
                        lambda *a: calls.append(a))
    rt = build_runtime({**env, "MOPH_BASE_URL": "https://moph.test"})
    rt.sender([{"type": "text"}])
    assert calls == [([{"type": "text"}], "https://moph.test",
                      "client-key-VALUE", "secret-key-VALUE")]


class FakeCon:
    def __init__(self, fail_readonly=False):
        self.fail_readonly = fail_readonly
        self.statements = []
        self.closed = False

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        self.statements.append(sql)
        if self.fail_readonly:
            raise RuntimeError("denied")

    def close(self):
        self.closed = True


def test_make_connect_sets_read_only_and_driver_options(monkeypatch):
    con = FakeCon()
    seen = {}
    monkeypatch.setattr(wiring.pymysql, "connect", lambda **kw: seen.update(kw) or con)
    out = make_connect("db.test", 3307, "hos", "reader", "pw")()
    assert out is con
    assert seen == {"host": "db.test", "port": 3307, "user": "reader", "password": "pw",
                    "database": "hos", "charset": "utf8mb4", "connect_timeout": 10,
                    "read_timeout": 60}
    assert con.statements == ["SET SESSION TRANSACTION READ ONLY"]


def test_make_connect_fails_closed_when_read_only_not_set(monkeypatch):
    con = FakeCon(fail_readonly=True)
    monkeypatch.setattr(wiring.pymysql, "connect", lambda **kw: con)
    with pytest.raises(HosxpError):
        make_connect("db.test", 3306, "hos", "reader", "pw")()
    assert con.closed  # ไม่คืนการเชื่อมต่อที่เขียนได้


def test_make_connect_requires_host_and_user():
    with pytest.raises(HosxpError):
        make_connect("", 3306, "hos", "", "")()


def test_build_runtime_db_defaults(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(wiring.pymysql, "connect",
                        lambda **kw: seen.update(kw) or FakeCon())
    e = {k: v for k, v in env.items() if k != "DB_NAME"}
    build_runtime(e).source._connect()
    assert seen["port"] == 3306 and seen["database"] == "hos"
    assert seen["host"] == "db.test" and seen["password"] == "db-pass-VALUE"


def test_build_app_scheduler_disabled_by_env(env):
    app = build_app({**env, "DENGUE_SCHEDULER": "0"})
    assert app.config["AREA_LOOKUP"] == app.config["SOURCE"].area_label
    assert app.config["CYCLE_STATE"]["last_cycle_at"] is None
    assert "SCHEDULER_STOP" not in app.config and "SCHEDULER_THREAD" not in app.config
    assert app.config["SCHEDULER_EXPECTED"] is False   # ปิดเอง: /healthz ไม่ตำหนิว่าไม่มีรอบ


def test_build_app_starts_scheduler_by_default(env, monkeypatch):
    started = []
    monkeypatch.setattr(wiring.scheduler, "start_scheduler",
                        lambda *a, **k: started.append((a, k)))
    app = build_app(env)
    (args, kwargs), = started
    assert args == (app, app.config["STORE"], app.config["SOURCE"], app.config["SENDER"],
                    app.config["SETTINGS"], app.config["AREA_LOOKUP"])
    assert app.config["SCHEDULER_EXPECTED"] is True


def test_build_app_keeps_scheduler_thread_for_the_restart_hook(env, monkeypatch):
    """M-3: hook รีสตาร์ตของ /settings ต้องรอรอบของตัวตั้งเวลาจบก่อน SIGTERM จึงต้องรู้จักเธรดนั้น"""
    sentinel = object()
    monkeypatch.setattr(wiring.scheduler, "start_scheduler", lambda *a, **k: sentinel)
    app = build_app(env)
    assert app.config["SCHEDULER_THREAD"] is sentinel


def test_wsgi_module_is_not_imported_by_wiring():
    assert "wsgi" not in vars(wiring)


@pytest.mark.parametrize("name", ["MOPH_CLIENT_KEY", "MOPH_SECRET_KEY"])
@pytest.mark.parametrize("bad", ["", "   ", "<Client ID ของกลุ่ม>", "<Secret ของกลุ่ม>"])
def test_placeholder_or_blank_moph_key_counts_as_not_configured(env, name, bad):
    assert build_runtime({**env, name: bad}).sender is None


def test_build_app_requires_strong_secret_key(env):
    for bad in ("", "short", "<สุ่มยาว 32 ตัวอักษรขึ้นไป>"):
        with pytest.raises(RuntimeError):
            build_app({**env, "SECRET_KEY": bad, "DENGUE_SCHEDULER": "0"})
