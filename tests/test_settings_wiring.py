"""ค่าจาก settings.json (หน้าตั้งค่า) ซ้อนทับ .env ทั่วระบบ: wiring · run.py · setup_check · /check · /healthz."""
import json
from datetime import date
from pathlib import Path

import pytest

import run
import scheduler
import setup_check
import settings_store as ss
import wiring
from fakes import FakeSource
from wiring import build_app, build_runtime

SECRETS = {"DB_PASS": "ovl-db-pass-ZQ41", "MOPH_CLIENT_KEY": "ovl-client-key-ZQ42",
           "MOPH_SECRET_KEY": "ovl-secret-key-ZQ43"}
ENV_SECRET_KEY = "flask-secret-VALUE-0123"


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    scheduler.release_all_locks()


@pytest.fixture
def env(tmp_path):
    return {"SECRET_KEY": ENV_SECRET_KEY, "DENGUE_DB_PATH": str(tmp_path / "data" / "dengue.db"),
            "DENGUE_SCHEDULER": "0", "DB_HOST": "env.db.test", "DB_USER": "env_reader",
            "DB_PASS": "env-db-pass-VALUE", "MOPH_CLIENT_KEY": "env-client-key-VALUE",
            "MOPH_SECRET_KEY": "env-secret-key-VALUE"}


@pytest.fixture
def settings_file(env):
    return Path(env["DENGUE_DB_PATH"]).parent / "settings.json"


@pytest.fixture
def seen(monkeypatch):
    """จับพารามิเตอร์ที่ส่งให้ pymysql.connect แล้วทำให้การต่อล้ม (ไม่ต่อฐานจริงเด็ดขาด)"""
    calls: list[dict] = []

    def fake_connect(**kw):
        calls.append(kw)
        raise OSError("connect refused for test")

    monkeypatch.setattr(wiring.pymysql, "connect", fake_connect)
    return calls


def put(settings_file, values):
    ss.write_overlay(settings_file, values, "admin")


def put_raw(settings_file, doc):
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")


def connect_args(rt, seen):
    with pytest.raises(OSError):
        rt.source._connect()
    return seen[-1]


# ---------- build_runtime ----------

def test_build_runtime_uses_overlay_values(env, settings_file, seen):
    today = date.today().isoformat()
    put(settings_file, {"DB_HOST": "overlay.db.test", "DB_PORT": "3310", "DB_NAME": "hos2",
                        "DB_USER": "ovl_reader", "DB_PASS": SECRETS["DB_PASS"],
                        "MOPH_CLIENT_KEY": SECRETS["MOPH_CLIENT_KEY"],
                        "MOPH_SECRET_KEY": SECRETS["MOPH_SECRET_KEY"],
                        "DENGUE_START_FROM": today, "DENGUE_BASE_URL": "https://overlay.example/dengue",
                        "DENGUE_DATA_AUDITED": "1"})
    rt = build_runtime(env)
    kw = connect_args(rt, seen)
    assert (kw["host"], kw["port"], kw["database"], kw["user"], kw["password"]) == (
        "overlay.db.test", 3310, "hos2", "ovl_reader", SECRETS["DB_PASS"])
    s = rt.settings
    assert s.moph_client_key == SECRETS["MOPH_CLIENT_KEY"] and s.moph_secret_key == SECRETS["MOPH_SECRET_KEY"]
    assert s.start_from.isoformat() == today and s.base_url == "https://overlay.example/dengue"
    assert s.data_audited is True and rt.sender is not None
    assert rt.overlay.error is None
    assert env["DB_HOST"] == "env.db.test"      # ไม่แก้ mapping ที่ผู้เรียกส่งมา


def test_build_runtime_without_file_unchanged(env, settings_file, seen):
    assert not settings_file.exists()
    rt = build_runtime(env)
    kw = connect_args(rt, seen)
    assert (kw["host"], kw["user"], kw["password"]) == ("env.db.test", "env_reader", "env-db-pass-VALUE")
    assert rt.settings.moph_client_key == "env-client-key-VALUE"
    assert rt.overlay.error is None and rt.overlay.values == {}


def test_overlay_empty_secret_clears_env_value(env, settings_file):
    put(settings_file, {"MOPH_CLIENT_KEY": ""})
    assert build_runtime(env).sender is None


def test_env_mapping_passed_in_is_the_base_not_the_cwd(env, tmp_path, monkeypatch, seen):
    other = tmp_path / "cwd"
    (other / "data").mkdir(parents=True)
    (other / "data" / "settings.json").write_text(
        json.dumps({"version": 1, "values": {"DB_HOST": "cwd.db.test"}}), encoding="utf-8")
    monkeypatch.chdir(other)
    kw = connect_args(build_runtime(env), seen)
    assert kw["host"] == "env.db.test"


def test_corrupt_overlay_falls_back_to_env_and_app_starts(env, settings_file, seen):
    put_raw(settings_file, "{ this is not json")
    app = build_app(env)
    assert app.config["SETTINGS_OVERLAY_ERROR"]
    assert app.config["SETTINGS"].moph_client_key == "env-client-key-VALUE"
    kw = connect_args(build_runtime(env), seen)
    assert kw["host"] == "env.db.test"


def test_partial_overlay_applies_valid_keys_and_reports_error(env, settings_file, seen):
    put_raw(settings_file, {"version": 1, "values": {"DB_HOST": "overlay.db.test", "DB_PORT": "99999"}})
    app = build_app(env)
    assert app.config["SETTINGS_OVERLAY_ERROR"] and "DB_PORT" in app.config["SETTINGS_OVERLAY_ERROR"]
    rt = build_runtime(env)
    kw = connect_args(rt, seen)
    assert kw["host"] == "overlay.db.test" and kw["port"] == 3306


def test_overlay_error_none_without_file(env):
    assert build_app(env).config["SETTINGS_OVERLAY_ERROR"] is None


def test_overlay_never_overrides_secret_key(env, settings_file):
    put_raw(settings_file, {"version": 1, "values": {
        "SECRET_KEY": "evil-secret-from-overlay-0000", "DENGUE_DB_PATH": "/evil/dengue.db",
        "DENGUE_SCHEDULER": "1", "DENGUE_LAN_PREFIXES": "0.", "DB_HOST": "overlay.db.test"}})
    app = build_app(env)
    assert app.secret_key == ENV_SECRET_KEY
    s = app.config["SETTINGS"]
    assert s.secret_key == ENV_SECRET_KEY and s.db_path == env["DENGUE_DB_PATH"]
    assert "0." not in s.lan_prefixes
    assert app.config["SCHEDULER_EXPECTED"] is False
    eff = ss.effective_env(env)
    assert eff["SECRET_KEY"] == ENV_SECRET_KEY and eff["DB_HOST"] == "overlay.db.test"


# ---------- /healthz ----------

def test_healthz_reports_settings_error_flag_without_text(env, settings_file):
    app = build_app(env)
    j = app.test_client().get("/healthz").get_json()
    assert j["settings_error"] is False and j["ok"] is True

    put_raw(settings_file, {"version": 1, "values": {"DB_HOST": "overlay.db.test", "DB_PORT": "99999"}})
    app = build_app(env)
    r = app.test_client().get("/healthz")
    j = r.get_json()
    assert j["settings_error"] is True and j["ok"] is False
    body = r.get_data(as_text=True)
    assert "DB_PORT" not in body and "ไฟล์ค่าตั้ง" not in body
    assert j["last_error"] is None

    put_raw(settings_file, "{ broken")
    j = build_app(env).test_client().get("/healthz").get_json()
    assert j["settings_error"] is True and j["ok"] is False


# ---------- /check ----------

def _check_client(env, login_as):
    app = build_app(env)
    c = app.test_client()
    login_as(c)
    return c


def test_check_page_shows_source_column_without_secret_values(env, settings_file, seen, login_as):
    put(settings_file, {"DB_HOST": "overlay.db.test", "DB_PASS": SECRETS["DB_PASS"],
                        "MOPH_CLIENT_KEY": SECRETS["MOPH_CLIENT_KEY"],
                        "MOPH_SECRET_KEY": SECRETS["MOPH_SECRET_KEY"],
                        "DENGUE_BASE_URL": "https://overlay.example/dengue"})
    c = _check_client(env, login_as)
    body = c.get("/check").get_data(as_text=True)
    for secret in (*SECRETS.values(), "env-db-pass-VALUE", "env-client-key-VALUE",
                   "env-secret-key-VALUE", ENV_SECRET_KEY):
        assert secret not in body
    table = body.split('id="env-table"')[1].split("</table>")[0]
    assert "แหล่งที่มา" in table
    rows = {}
    for tr in table.split("<tr>")[2:]:
        cells = [t.split("</td>")[0] for t in tr.split("<td")[1:]]
        name = cells[0].split(">", 1)[1]
        rows[name] = tr
    assert "overlay.db.test" in rows["DB_HOST"] and "หน้าตั้งค่า" in rows["DB_HOST"]
    assert "env_reader" in rows["DB_USER"] and ".env" in rows["DB_USER"]
    assert "overlay.example" in rows["DENGUE_BASE_URL"] and "หน้าตั้งค่า" in rows["DENGUE_BASE_URL"]
    assert "ตั้งแล้ว" in rows["DB_PASS"] and "หน้าตั้งค่า" in rows["DB_PASS"]
    assert "ตั้งแล้ว" in rows["MOPH_CLIENT_KEY"] and "หน้าตั้งค่า" in rows["MOPH_CLIENT_KEY"]
    assert "ค่าเริ่มต้น" in rows["DB_NAME"] and "hos" in rows["DB_NAME"]
    assert "ค่าเริ่มต้น" in rows["DENGUE_DATA_AUDITED"]
    assert ".env" in rows["SECRET_KEY"] and "ตั้งแล้ว" in rows["SECRET_KEY"]


def test_check_page_source_for_default_base_url_and_unset_secret(env, settings_file, seen, login_as):
    env = {k: v for k, v in env.items() if k not in ("DB_PASS", "DB_HOST")}
    body = _check_client(env, login_as).get("/check").get_data(as_text=True)
    table = body.split('id="env-table"')[1].split("</table>")[0]
    base_row = [t for t in table.split("<tr>") if "DENGUE_BASE_URL" in t][0]
    assert "wnkhos.go.th" in base_row and "ค่าเริ่มต้น" in base_row
    pass_row = [t for t in table.split("<tr>") if ">DB_PASS<" in t][0]
    assert "ยังไม่ได้ตั้ง" in pass_row and "ค่าเริ่มต้น" in pass_row


def test_check_results_include_overlay_check(env, settings_file, seen, login_as):
    put_raw(settings_file, "{ broken")
    body = _check_client(env, login_as).get("/check").get_data(as_text=True)
    assert "ไฟล์ตั้งค่าจากหน้าเว็บ" in body and "ไฟล์ค่าตั้งเสีย" in body


# ---------- setup_check ----------

def _lines(capsys):
    return capsys.readouterr().out.splitlines()


def _overlay_line(lines):
    found = [ln for ln in lines if "ไฟล์ตั้งค่าจากหน้าเว็บ" in ln]
    assert len(found) == 1, lines
    return found[0]


def test_setup_check_overlay_check_warns_on_corrupt_file(env, settings_file, seen, capsys):
    put_raw(settings_file, "{ broken")
    setup_check.main([], build=lambda: build_runtime(env))
    line = _overlay_line(_lines(capsys))
    assert line.startswith("⚠") and "ไฟล์ค่าตั้งเสีย" in line


def test_setup_check_overlay_check_ok_absent_and_valid(env, settings_file, seen, capsys):
    setup_check.main([], build=lambda: build_runtime(env))
    line = _overlay_line(_lines(capsys))
    assert line.startswith("✔") and "ไม่มี" in line
    put(settings_file, {"DB_HOST": "overlay.db.test", "DB_PASS": SECRETS["DB_PASS"]})
    setup_check.main([], build=lambda: build_runtime(env))
    out = capsys.readouterr().out
    line = _overlay_line(out.splitlines())
    assert line.startswith("✔") and "ใช้ค่าจากหน้าตั้งค่า 2 ค่า" in line
    assert SECRETS["DB_PASS"] not in out and "overlay.db.test" not in out


def test_setup_check_other_checks_see_overlay_values(env, settings_file, seen, capsys):
    put(settings_file, {"DB_HOST": "overlay.db.test"})
    setup_check.main([], build=lambda: build_runtime(env))
    assert seen and seen[0]["host"] == "overlay.db.test"


def test_run_checks_without_overlay_argument_says_none():
    from config import load_settings
    results = setup_check.run_checks(load_settings({"SECRET_KEY": ENV_SECRET_KEY}), FakeSource(), True)
    status, _name, detail = [r for r in results if "ไฟล์ตั้งค่าจากหน้าเว็บ" in r[1]][0]
    assert status == "ok" and "ไม่มี" in detail


# ---------- run.py ----------

def test_run_cli_uses_overlay(env, settings_file, seen, capsys):
    put(settings_file, {"DB_HOST": "overlay.db.test", "DB_USER": "ovl_reader"})
    code = run.main(["--once", "--dry-run"], env=env)
    assert code == 1                      # ต่อฐานไม่ได้ (ของปลอมล้ม) แต่ต้องลองต่อด้วยค่าซ้อนทับ
    assert seen and seen[0]["host"] == "overlay.db.test" and seen[0]["user"] == "ovl_reader"
    assert not Path(env["DENGUE_DB_PATH"]).exists()   # dry-run ยังใช้ฐานชั่วคราว


def test_run_cli_without_overlay_uses_env(env, seen, capsys):
    assert run.main(["--once", "--dry-run"], env=env) == 1
    assert seen[0]["host"] == "env.db.test"


def test_run_rearm_alerts_uses_overlay(env, settings_file, monkeypatch, capsys):
    put(settings_file, {"MOPH_CLIENT_KEY": ""})
    captured = {}
    real = run.build_runtime

    def spy(e=None):
        rt = real(e)
        captured["sender"] = rt.sender
        return rt

    monkeypatch.setattr(run, "build_runtime", spy)
    assert run.main(["rearm-alerts", "--yes"], env=env) == 0
    assert captured["sender"] is None


# ---------- ไม่มีที่อ่านตัวแปรอนุญาตตรงจาก os.environ นอก settings_store ----------

def test_no_direct_environ_reads_of_allowed_keys_outside_settings_store():
    import re
    root = Path(__file__).resolve().parent.parent
    keys = "|".join(ss.ALLOWED)
    offenders = []
    for path in root.glob("*.py"):
        if path.name in ("settings_store.py", "config.py", "envfile.py"):
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "os.environ" in line or "os.getenv" in line:
                offenders.append(f"{path.name}:{n}: {line.strip()}")
            elif re.search(rf"environ\W.*\b({keys})\b", line):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert offenders == []


# ---------- settings_store.resolve / Runtime ไม่เปิดเผยความลับ ----------

def test_resolve_separates_base_from_effective_and_hides_secrets_in_repr(env, settings_file):
    put(settings_file, {"DB_HOST": "overlay.db.test", "DB_PASS": SECRETS["DB_PASS"]})
    r = ss.resolve(env)
    assert r.env["DB_HOST"] == "overlay.db.test" and r.base["DB_HOST"] == "env.db.test"
    assert r.env["DB_PASS"] == SECRETS["DB_PASS"] and r.base["DB_PASS"] == "env-db-pass-VALUE"
    assert r.overlay.values["DB_HOST"] == "overlay.db.test"
    rt = build_runtime(env)
    text = repr(r) + repr(rt) + str(rt)
    for secret in (*SECRETS.values(), "env-db-pass-VALUE", ENV_SECRET_KEY):
        assert secret not in text


def test_build_app_does_not_keep_process_env_or_secrets_in_config(env):
    """app.config ต้องไม่เก็บสำเนา env ทั้งหมด/ความลับ (repr ของ config ใช้ในหน้า debug/ล็อกได้)."""
    env = {**env, "UNRELATED_VAR_QX7": "unrelated-value-QX7", "AWS_SECRET_ACCESS_KEY": "aws-secret-QX8"}
    app = build_app(env)
    secrets = ["env-db-pass-VALUE", "env-client-key-VALUE", "env-secret-key-VALUE",
               "unrelated-value-QX7", "UNRELATED_VAR_QX7", "aws-secret-QX8"]
    # Flask เก็บ SECRET_KEY ไว้ใน config["SECRET_KEY"] เองเพื่อเซ็นคุกกี้ session — ข้ามเฉพาะรายการนั้น
    cfg = {k: v for k, v in app.config.items() if k != "SECRET_KEY"}
    blobs = [repr(cfg)] + [f"{k}={v!r}" for k, v in cfg.items()]
    for blob in blobs:
        for s in secrets + [ENV_SECRET_KEY]:
            assert s not in blob, (s, blob[:80])
    base = app.config["SETTINGS_BASE_ENV"]
    assert "UNRELATED_VAR_QX7" not in base and "AWS_SECRET_ACCESS_KEY" not in base
    # ยังบอกได้ว่า "ตั้งแล้ว" (หน้า /check ใช้ตัดสินแหล่งที่มา) และชื่อที่ไม่ลับยังอยู่
    assert base["DB_PASS"] and base["SECRET_KEY"] and base["DB_HOST"] == "env.db.test"
    assert base["DENGUE_DB_PATH"] == env["DENGUE_DB_PATH"]


def test_check_page_still_reports_sources_with_filtered_base_env(env, login_as):
    app = build_app(env)
    c = app.test_client()
    login_as(c, "u", "viewer")
    body = c.get("/check").get_data(as_text=True)
    assert c.get("/check").status_code == 200 and ".env" in body
    for s in ("env-db-pass-VALUE", "env-client-key-VALUE", "env-secret-key-VALUE", ENV_SECRET_KEY):
        assert s not in body
