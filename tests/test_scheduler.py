"""ตัวตั้งเวลา (scheduler.py) และ CLI (run.py) — ออฟไลน์ทั้งหมด ไม่ต่อ HOSxP/MOPH จริง."""
import json
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
import requests

import alerts
import detector
import run
import scheduler
from app import create_app
from config import load_settings
from detector import CycleResult
from fakes import FakeSender, FakeSource, HosxpError, make_row
from moph_notify import NotifyError
from store import Store
from wiring import Runtime

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 8, 12, 0)


def settings(db_path, **env):
    base = {"DENGUE_START_FROM": "2026-11-01", "SECRET_KEY": "scheduler-test-secret-key",
            "DENGUE_DB_PATH": str(db_path), "DENGUE_POLL_SECONDS": "1"}
    base.update(env)
    return load_settings(base)


@pytest.fixture(autouse=True)
def _release_locks():
    yield
    scheduler.release_all_locks()


@pytest.fixture
def cfg(tmp_path):
    return settings(tmp_path / "data" / "dengue.db")


# ---------------------------------------------------------------- ล็อกไฟล์

def test_acquire_lock_second_caller_false(tmp_path):
    path = str(tmp_path / "data" / "scheduler.lock")
    assert scheduler.acquire_lock(path) is True
    assert scheduler.acquire_lock(path) is False  # ผู้เรียกที่สองในโปรเซสเดียวกัน
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import scheduler;"
            "print(scheduler.acquire_lock(sys.argv[2]))")
    other = subprocess.run([sys.executable, "-c", code, str(ROOT), path],
                           capture_output=True, text=True, timeout=60)
    assert other.stdout.strip() == "False", other.stderr  # อีกโปรเซสหนึ่ง
    scheduler.release_all_locks()
    again = subprocess.run([sys.executable, "-c", code, str(ROOT), path],
                           capture_output=True, text=True, timeout=60)
    assert again.stdout.strip() == "True", again.stderr  # ปล่อยแล้วโปรเซสอื่นได้ล็อก


# ---------------------------------------------------------------- cycle_once

def test_cycle_once_runs_detect_send_cluster_in_order(store, cfg, monkeypatch):
    calls = []
    marker = CycleResult(new_case_ids=[7])
    lookup = lambda code: "x"  # noqa: E731
    sender = FakeSender()
    source = FakeSource()

    def fake_run_cycle(st, src, s, now):
        calls.append(("detect", st, src, s, now))
        return marker

    def fake_send(st, snd, s, area_lookup=None, now=None):
        calls.append(("send", st, snd, s, area_lookup, now))
        return 0

    def fake_clusters(st, snd, s, area_lookup=None, now=None):
        calls.append(("cluster", st, snd, s, area_lookup, now))
        return []

    monkeypatch.setattr(detector, "run_cycle", fake_run_cycle)
    monkeypatch.setattr(alerts, "send_pending", fake_send)
    monkeypatch.setattr(alerts, "evaluate_clusters", fake_clusters)
    result = scheduler.cycle_once(store, source, sender, cfg, NOW, area_lookup=lookup)
    assert result is marker
    assert [c[0] for c in calls] == ["detect", "send", "cluster"]
    assert calls[0][1:] == (store, source, cfg, NOW)
    assert calls[1][1:] == (store, sender, cfg, lookup, NOW)
    assert calls[2][1:] == (store, sender, cfg, lookup, NOW)


def test_cycle_once_end_to_end_creates_case_and_alerts(store, cfg):
    source = FakeSource([make_row("VN:1")])
    sender = FakeSender()
    result = scheduler.cycle_once(store, source, sender, cfg, NOW)
    assert result.error is None and len(result.new_case_ids) == 1
    assert len(sender.calls) == 1
    assert store.alert_sent("new", case_id=result.new_case_ids[0])


def test_cycle_survives_exception_in_sender(store, cfg):
    source = FakeSource([make_row("VN:1")])
    sender = FakeSender(error=NotifyError("ล่ม"))
    result = scheduler.cycle_once(store, source, sender, cfg, NOW)  # ต้องไม่ยก
    assert result.error is None and len(result.new_case_ids) == 1
    assert sender.calls  # พยายามส่งแล้ว
    assert not store.alert_sent("new", case_id=result.new_case_ids[0])
    # ตัวส่งกลับมาปกติ: รอบถัดไปส่งเคสที่ค้างได้
    good = FakeSender()
    scheduler.cycle_once(store, source, good, cfg, NOW)
    assert store.alert_sent("new", case_id=result.new_case_ids[0])


def test_pending_alerts_still_sent_when_hosxp_down(store, cfg):
    cid = store.add_case("VN:1", "OPD", "000001", "A90", datetime(2026, 11, 8, 9, 0),
                         "40010103", "12", "1", 30, True)
    source = FakeSource(failure=HosxpError("down"))
    sender = FakeSender()
    result = scheduler.cycle_once(store, source, sender, cfg, NOW)
    assert result.error == "down"
    assert len(sender.calls) == 1  # เคสที่สร้างไว้แล้วยังถูกแจ้ง แม้ HOSxP ล่ม
    assert store.alert_sent("new", case_id=cid)


def test_sender_none_skips_alert_functions_and_sets_error(store, cfg, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("ต้องไม่เรียกเมื่อ sender เป็น None")

    monkeypatch.setattr(alerts, "send_pending", boom)
    monkeypatch.setattr(alerts, "evaluate_clusters", boom)
    result = scheduler.cycle_once(store, FakeSource([make_row("VN:1")]), None, cfg, NOW)
    assert len(result.new_case_ids) == 1  # ยังตรวจจับ/สร้างเคสตามปกติ

    app = create_app(cfg, store, FakeSource(), None)
    thread = scheduler.start_scheduler(app, store, FakeSource(), None, cfg)
    try:
        assert thread is not None
        # M-3: hook รีสตาร์ตของ /settings ใช้ทั้งสองตัวนี้สั่งหยุดและรอรอบจบ
        assert app.config["SCHEDULER_THREAD"] is thread and app.config["SCHEDULER_STOP"] is not None
        assert app.config["CYCLE_STATE"]["last_error"] == "ยังไม่ได้ตั้งค่า MOPH Notify"
        _spin_until(lambda: app.config["CYCLE_STATE"]["last_cycle_at"] is not None)
        # รอบที่สำเร็จต้องไม่ล้างข้อความนี้ (การแจ้งเตือนยังปิดอยู่)
        assert app.config["CYCLE_STATE"]["last_error"] == "ยังไม่ได้ตั้งค่า MOPH Notify"
    finally:
        _stop(app, thread)


def _raiser(name):
    def fn(*a, **k):
        raise RuntimeError(f"HN 0001234567 {name}")
    return fn


def test_cycle_runs_alert_steps_even_if_detector_raises(store, cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(detector, "run_cycle", _raiser("detector"))
    monkeypatch.setattr(alerts, "send_pending", lambda *a, **k: calls.append("send") or 0)
    monkeypatch.setattr(alerts, "evaluate_clusters",
                        lambda *a, **k: calls.append("cluster") or [])
    result = scheduler.cycle_once(store, FakeSource(), FakeSender(), cfg, NOW)
    assert calls == ["send", "cluster"]
    assert result.error == "RuntimeError"  # ชื่อชนิดอย่างเดียว ไม่มีข้อความ/เลขผู้ป่วย
    assert result.new_case_ids == []


def test_cycle_runs_cluster_step_even_if_send_pending_raises(store, cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(alerts, "send_pending", _raiser("send"))
    monkeypatch.setattr(alerts, "evaluate_clusters",
                        lambda *a, **k: calls.append("cluster") or [])
    result = scheduler.cycle_once(store, FakeSource(), FakeSender(), cfg, NOW)
    assert calls == ["cluster"]
    assert result.error == "RuntimeError"


def test_first_failure_is_reported_in_result_error(store, cfg, monkeypatch):
    class DetectorError(Exception):
        pass

    class SendError(Exception):
        pass

    class ClusterError(Exception):
        pass

    def boom(exc):
        def fn(*a, **k):
            raise exc("HN 0001234567")
        return fn

    # ขั้นแรกที่ยกเป็นผู้ชนะ
    monkeypatch.setattr(alerts, "send_pending", boom(SendError))
    monkeypatch.setattr(alerts, "evaluate_clusters", boom(ClusterError))
    result = scheduler.cycle_once(store, FakeSource(), FakeSender(), cfg, NOW)
    assert result.error == "SendError"
    # ตัวตรวจจับคืน error เอง (HosxpError) -> เก็บไว้ ไม่ถูกทับด้วยขั้นหลัง
    result = scheduler.cycle_once(
        store, FakeSource(failure=HosxpError("down")), FakeSender(), cfg, NOW)
    assert result.error == "down"
    # ตัวตรวจจับยก exception -> ชนะขั้นหลัง
    monkeypatch.setattr(detector, "run_cycle", boom(DetectorError))
    result = scheduler.cycle_once(store, FakeSource(), FakeSender(), cfg, NOW)
    assert result.error == "DetectorError"


def test_cycle_logs_type_name_only(store, cfg, monkeypatch, caplog):
    monkeypatch.setattr(detector, "run_cycle", _raiser("detector"))
    with caplog.at_level("WARNING", logger="scheduler"):
        scheduler.cycle_once(store, FakeSource(), FakeSender(), cfg, NOW)
    assert "RuntimeError" in caplog.text and "0001234567" not in caplog.text


def test_cycle_warns_when_detector_returns_error(store, cfg, caplog):
    with caplog.at_level("WARNING", logger="scheduler"):
        scheduler.cycle_once(store, FakeSource(failure=HosxpError("down")), None, cfg, NOW)
    assert "down" in caplog.text


# ---------------------------------------------------------------- เธรด

def _stop(app, thread):
    app.config["SCHEDULER_STOP"].set()
    if thread is not None:
        thread.join(timeout=10)
        assert not thread.is_alive()


def test_scheduler_thread_survives_exceptions_and_updates_cycle_state(
        store, cfg, monkeypatch):
    cfg = replace(cfg, poll_seconds=0.01)
    app = create_app(cfg, store, FakeSource(), FakeSender())
    observed = []
    second = threading.Event()
    state = {"n": 0}

    def fake_cycle_once(st, src, snd, s, now, area_lookup=None):
        state["n"] += 1
        observed.append(dict(app.config["CYCLE_STATE"]))
        if state["n"] == 1:
            raise RuntimeError("HN 0001234567 พัง")
        if state["n"] == 2:
            return CycleResult(error="HosxpError: ต่อไม่ได้")
        second.set()
        return CycleResult()

    monkeypatch.setattr(scheduler, "cycle_once", fake_cycle_once)
    thread = scheduler.start_scheduler(app, store, FakeSource(), FakeSender(), cfg)
    try:
        assert thread is not None and thread.daemon
        assert second.wait(10), "เธรดตายหลัง exception"
        _spin_until(lambda: state["n"] >= 3 and app.config["CYCLE_STATE"]["last_error"] is None)
    finally:
        _stop(app, thread)
    # รอบที่ 2 เริ่มเมื่อ error ของรอบแรก (ชื่อชนิดอย่างเดียว ไม่มีเลขผู้ป่วย) ถูกบันทึกแล้ว
    assert observed[0]["last_error"] is None
    assert observed[1]["last_error"] == "RuntimeError: HN … พัง"
    assert observed[1]["last_cycle_at"] is not None
    assert observed[2]["last_error"] == "HosxpError: ต่อไม่ได้"
    assert app.config["CYCLE_STATE"]["last_cycle_at"] is not None


def test_scheduler_thread_survives_set_cycle_error_raising(store, cfg, monkeypatch):
    cfg = replace(cfg, poll_seconds=0.01)
    app = create_app(cfg, store, FakeSource(), FakeSender())
    real = scheduler.set_cycle_error
    state = {"n": 0}

    def flaky(a, err):
        state["n"] += 1
        if state["n"] <= 2:  # ยกซ้ำในเส้นทาง except ด้วย (ของเดิมเธรดตายตรงนี้)
            raise RuntimeError("บันทึกสถานะพัง")
        real(a, err)

    monkeypatch.setattr(scheduler, "set_cycle_error", flaky)
    monkeypatch.setattr(scheduler, "cycle_once", lambda *a, **k: CycleResult())
    thread = scheduler.start_scheduler(app, store, FakeSource(), FakeSender(), cfg)
    try:
        _spin_until(lambda: state["n"] >= 4
                    and app.config["CYCLE_STATE"]["last_cycle_at"] is not None)
        assert thread.is_alive()  # เธรดไม่ตายแม้ set_cycle_error ยก
    finally:
        _stop(app, thread)


def _spin_until(pred, timeout=10):
    ev = threading.Event()
    end = datetime.now().timestamp() + timeout
    while datetime.now().timestamp() < end:
        if pred():
            return
        ev.wait(0.01)
    raise AssertionError("เงื่อนไขไม่เป็นจริงภายในเวลาที่กำหนด")


def test_scheduler_not_started_without_lock(store, cfg, monkeypatch):
    lock_path = os.path.join(os.path.dirname(cfg.db_path), "scheduler.lock")
    assert scheduler.acquire_lock(lock_path)  # จำลองโปรเซส/worker อื่นถือล็อกอยู่
    monkeypatch.setattr(
        scheduler, "cycle_once",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ต้องไม่รัน")))
    app = create_app(cfg, store, FakeSource(), FakeSender())
    assert scheduler.start_scheduler(app, store, FakeSource(), FakeSender(), cfg) is None
    assert app.config["CYCLE_STATE"]["last_cycle_at"] is None


# ---------------------------------------------------------------- CLI: add-user

def _user_count(db) -> int:
    Store(str(db))  # สร้างสคีมาถ้ายังไม่มี (ถูกปฏิเสธก่อนเปิดฐาน)
    with closing(sqlite3.connect(db)) as con:
        return con.execute('SELECT COUNT(*) FROM "user"').fetchone()[0]


def _passwords(monkeypatch, *values):
    it = iter(values)
    monkeypatch.setattr(run.getpass, "getpass", lambda prompt="": next(it))


def test_add_user_command_stores_pbkdf2_hash_and_role(tmp_path, monkeypatch, capsys):
    db = tmp_path / "data" / "dengue.db"
    _passwords(monkeypatch, "S3cret-pass", "S3cret-pass")
    code = run.main(["add-user", "nurse1", "srrt"], env={"DENGUE_DB_PATH": str(db)})
    assert code == 0
    store = Store(str(db))
    assert store.check_user("nurse1", "S3cret-pass") == "srrt"
    assert store.check_user("nurse1", "wrong") is None
    with closing(sqlite3.connect(db)) as con:
        (stored,) = con.execute(
            'SELECT password_hash FROM "user" WHERE username=?', ("nurse1",)).fetchone()
    assert stored.startswith("pbkdf2:sha256")
    out = capsys.readouterr()
    assert "S3cret-pass" not in out.out + out.err


def test_add_user_rejects_unknown_role(tmp_path, monkeypatch, capsys):
    db = tmp_path / "data" / "dengue.db"
    _passwords(monkeypatch, "pw12345678", "pw12345678")
    code = run.main(["add-user", "bob", "root"], env={"DENGUE_DB_PATH": str(db)})
    assert code == 2
    assert "srrt" in capsys.readouterr().err  # บอกบทบาทที่ใช้ได้
    assert _user_count(db) == 0


@pytest.mark.parametrize("first,second", [("", ""), ("abcdefghij1", "abcdefghij2")])
def test_add_user_refuses_empty_or_mismatched_password(tmp_path, monkeypatch, first, second):
    db = tmp_path / "data" / "dengue.db"
    _passwords(monkeypatch, first, second)
    assert run.main(["add-user", "bob", "viewer"], env={"DENGUE_DB_PATH": str(db)}) == 2
    assert _user_count(db) == 0


def test_add_user_strips_username(tmp_path, monkeypatch):
    db = tmp_path / "data" / "dengue.db"
    _passwords(monkeypatch, "pw12345678", "pw12345678")
    assert run.main(["add-user", "  bob  ", "viewer"], env={"DENGUE_DB_PATH": str(db)}) == 0
    with closing(sqlite3.connect(db)) as con:
        assert [r[0] for r in con.execute('SELECT username FROM "user"')] == ["bob"]


@pytest.mark.parametrize("name", ["", "   ", "a b", "a	b", "x" * 65])
def test_add_user_rejects_bad_usernames(tmp_path, monkeypatch, name):
    db = tmp_path / "data" / "dengue.db"
    _passwords(monkeypatch, "pw12345678", "pw12345678")
    assert run.main(["add-user", name, "viewer"], env={"DENGUE_DB_PATH": str(db)}) == 2
    assert _user_count(db) == 0


def test_add_user_duplicate_is_friendly(tmp_path, monkeypatch, capsys):
    db = tmp_path / "data" / "dengue.db"
    Store(str(db))
    with closing(sqlite3.connect(db)) as con, con:
        con.execute('INSERT INTO "user" (username, password_hash, role) VALUES (?,?,?)',
                    ("bob", "x", "viewer"))
    _passwords(monkeypatch, "x1234567890", "x1234567890")
    assert run.main(["add-user", "bob", "viewer"], env={"DENGUE_DB_PATH": str(db)}) == 2
    assert "bob" in capsys.readouterr().err


# ---------------------------------------------------------------- CLI: --once

def _runtime(tmp_path, source, sender=None):
    s = settings(tmp_path / "data" / "dengue.db")
    return Runtime(settings=s, store=Store(s.db_path), source=source, sender=sender,
                   area_lookup=None)


def _json_docs(text):
    dec, i, docs = json.JSONDecoder(), 0, []
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return docs
        doc, i = dec.raw_decode(text, i)
        docs.append(doc)


def test_dry_run_prints_json_and_never_calls_requests(tmp_path, monkeypatch, capsys):
    def no_network(*a, **k):
        raise AssertionError("dry-run ต้องไม่เรียก requests")

    monkeypatch.setattr(requests, "post", no_network)
    monkeypatch.setattr(requests.Session, "request", no_network)
    real_db = tmp_path / "data" / "dengue.db"
    source = FakeSource([make_row("VN:1")])
    captured_env = {}

    def fake_build(env=None):
        captured_env.update(env or {})
        s = settings(captured_env["DENGUE_DB_PATH"])
        # มีกุญแจ MOPH อยู่ก็ยังต้องไม่ส่งจริง
        s = replace(s, moph_client_key="ck", moph_secret_key="sk")
        return Runtime(settings=s, store=Store(s.db_path), source=source,
                       sender=lambda m: requests.post("http://x", json=m),
                       area_lookup=None)

    monkeypatch.setattr(run, "build_runtime", fake_build)
    code = run.main(["--once", "--dry-run"], env={"DENGUE_DB_PATH": str(real_db)})
    out = capsys.readouterr()
    assert code == 0
    docs = _json_docs(out.out)  # stdout เป็น JSON ล้วน
    assert docs and docs[0][0]["type"] == "flex"
    assert "ไข้เลือดออก" in json.dumps(docs, ensure_ascii=False)
    assert "ใช้ฐานชั่วคราว" in out.err
    assert not real_db.exists()  # ไม่แตะฐานจริง
    assert captured_env["DENGUE_DB_PATH"] != str(real_db)
    assert "sk" not in out.out


def test_once_reports_cycle_error_exit_1(tmp_path, monkeypatch, capsys):
    rt = _runtime(tmp_path, FakeSource(failure=HosxpError("ต่อไม่ได้")), FakeSender())
    monkeypatch.setattr(run, "build_runtime", lambda env=None: rt)
    assert run.main(["--once"], env={}) == 1
    assert "ต่อไม่ได้" in capsys.readouterr().err


def test_once_refuses_when_scheduler_lock_held(tmp_path, monkeypatch, capsys):
    sender = FakeSender()
    rt = _runtime(tmp_path, FakeSource([make_row("VN:1")]), sender)
    monkeypatch.setattr(run, "build_runtime", lambda env=None: rt)
    assert scheduler.acquire_lock(os.path.join(os.path.dirname(rt.settings.db_path),
                                               "scheduler.lock"))
    assert run.main(["--once"], env={}) == 1
    assert sender.calls == []
    assert "scheduler" in capsys.readouterr().err


def test_once_real_run_sends_and_records(tmp_path, monkeypatch):
    sender = FakeSender()
    rt = _runtime(tmp_path, FakeSource([make_row("VN:1")]), sender)
    monkeypatch.setattr(run, "build_runtime", lambda env=None: rt)
    assert run.main(["--once"], env={}) == 0
    assert len(sender.calls) == 1


def test_no_arguments_is_usage_error(capsys):
    with pytest.raises(SystemExit) as e:
        run.main([], env={})
    assert e.value.code == 2


# ---------------------------------------------------------------- CLI: rearm-alerts

def _seed_failed_alerts(db, given_up=1, retrying=1):
    store = Store(str(db))
    for i in range(given_up + retrying):
        cid = store.add_case(f"V{i}", "OPD", f"HN{i}", "A90", datetime(2026, 11, 8, 9, 0),
                             "40010103", "1", "1", 30, True)
        for _ in range(alerts.MAX_TOTAL_ATTEMPTS if i < given_up else 2):
            store.log_alert("new", cid, None, False, 401, "NotifyError")
    return store


def _no_prompt(monkeypatch):
    def fail(prompt=""):
        raise AssertionError("ต้องไม่ถามเมื่อใช้ --yes")
    monkeypatch.setattr("builtins.input", fail)


def test_rearm_alerts_requires_yes_confirmation(tmp_path, monkeypatch, capsys):
    db = tmp_path / "data" / "dengue.db"
    store = _seed_failed_alerts(db)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "yes")
    code = run.main(["rearm-alerts"], env={"DENGUE_DB_PATH": str(db)})
    out = capsys.readouterr().out
    assert code == 0 and prompts == ["พิมพ์ yes เพื่อยืนยัน: "]
    assert "2" in out and "1" in out                       # ก่อน: ส่งไม่สำเร็จ 2 · หยุดส่งแล้ว 1
    assert store.alert_health(max_total=alerts.MAX_TOTAL_ATTEMPTS) == {"failing": 0, "given_up": 0}


@pytest.mark.parametrize("answer", ["", "y", "no", "YES!", "ใช่"])
def test_rearm_alerts_other_answers_exit_2_and_change_nothing(tmp_path, monkeypatch, answer):
    db = tmp_path / "data" / "dengue.db"
    store = _seed_failed_alerts(db)
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    assert run.main(["rearm-alerts"], env={"DENGUE_DB_PATH": str(db)}) == 2
    assert store.alert_health(max_total=alerts.MAX_TOTAL_ATTEMPTS) == {"failing": 2, "given_up": 1}


def test_rearm_alerts_eof_on_prompt_exits_2(tmp_path, monkeypatch):
    db = tmp_path / "data" / "dengue.db"
    store = _seed_failed_alerts(db)

    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert run.main(["rearm-alerts"], env={"DENGUE_DB_PATH": str(db)}) == 2
    assert store.alert_health(max_total=alerts.MAX_TOTAL_ATTEMPTS)["failing"] == 2


def test_rearm_alerts_yes_flag_skips_prompt_and_reports_count(tmp_path, monkeypatch, capsys):
    db = tmp_path / "data" / "dengue.db"
    store = _seed_failed_alerts(db, given_up=2, retrying=1)
    _no_prompt(monkeypatch)
    assert run.main(["rearm-alerts", "--yes"], env={"DENGUE_DB_PATH": str(db)}) == 0
    assert "3" in capsys.readouterr().out
    assert store.alert_health(max_total=alerts.MAX_TOTAL_ATTEMPTS) == {"failing": 0, "given_up": 0}
    assert run.main(["rearm-alerts", "--yes"], env={"DENGUE_DB_PATH": str(db)}) == 0


def test_rearm_alerts_does_not_send_or_start_scheduler(tmp_path, monkeypatch):
    db = tmp_path / "data" / "dengue.db"
    _seed_failed_alerts(db)

    def boom(*a, **k):
        raise AssertionError("ต้องไม่ถูกเรียก")

    for mod, name in ((scheduler, "cycle_once"), (scheduler, "start_scheduler"),
                      (scheduler, "acquire_lock"), (alerts, "send_pending"),
                      (alerts, "evaluate_clusters")):
        monkeypatch.setattr(mod, name, boom)
    assert run.main(["rearm-alerts", "--yes"], env={"DENGUE_DB_PATH": str(db)}) == 0
