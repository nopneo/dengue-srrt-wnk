"""setup_check (run_checks + CLI), เมธอดอ่านโครงสร้างของ HosxpSource, is_lan_client และหน้า /check (Task 10)."""
import json
import re
import shutil
import socket
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import setup_check
from app import create_app
from auth import is_lan_client
from config import load_settings
from fakes import FakeSender, FakeSource, make_row
from hosxp import HosxpError, HosxpSource

FAC_RPST = {"hospcode": "02846", "name": "โรงพยาบาลส่งเสริมสุขภาพตำบลระเริง", "kind": "rpst", "tmb": "302503"}
FAC_HOSP = {"hospcode": "10894", "name": "โรงพยาบาลวังน้ำเขียว", "kind": "hospital", "tmb": "302505"}


def tambon_dir(tmp_path, monkeypatch, codes=("3025",)):
    """โฟลเดอร์เส้นขอบชั่วคราวที่มีไฟล์ตำบลสังเคราะห์ของอำเภอใน `codes` (ชี้ boundary.BOUNDARY_DIR ไปที่นี่)"""
    import boundary
    d = tmp_path / "bounds"
    d.mkdir(exist_ok=True)
    ring = [[101.5, 14.2], [102.0, 14.2], [102.0, 14.6], [101.5, 14.6], [101.5, 14.2]]
    for c in codes:
        doc = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"code": c + "01", "name": "ตำบลหนึ่ง"},
             "geometry": {"type": "Polygon", "coordinates": [ring]}},
            {"type": "Feature", "properties": {"code": c + "02", "name": "ตำบลสอง"},
             "geometry": {"type": "Polygon", "coordinates": [ring]}}]}
        (d / f"{c}.tambon.geojson").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(boundary, "BOUNDARY_DIR", d)
    return d


def placed_store(tmp_path, *items):
    from store import Store
    st = Store(str(tmp_path / "chk.db"))
    for it in items:
        st.facility_upsert(it["hospcode"], it["name"], it["kind"], it["tmb"], 14.4, 101.8, "csv", "t",
                           datetime(2026, 11, 10, 9, 0))
    return st


LAN = ("10.", "172.16.", "192.168.", "127.", "30.25.")
SECRET = "flask-secret-VALUE-9f3"
MOPH_ID, MOPH_SEC = "moph-client-VALUE-77", "moph-secret-VALUE-88"
ROOT = Path(__file__).resolve().parent.parent


def make_settings(tmp_path, **env):
    # สภาพแวดล้อมที่ "ปกติ": ตั้ง DENGUE_START_FROM แล้ว (ยังไม่ตั้ง = ⚠ ดู START_CHECK); ส่ง DENGUE_START_FROM=None เพื่อไม่ตั้งเลย
    base = {"SECRET_KEY": SECRET, "DENGUE_DB_PATH": str(tmp_path / "d.db"),
            "DENGUE_DATA_AUDITED": "1", "DENGUE_START_FROM": date.today().isoformat()}
    base.update(env)
    return load_settings({k: v for k, v in base.items() if v is not None})


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """เทสต์ต้องไม่ออกเครือข่ายจริง: TCP ไป MOPH ถูกแทนด้วยของปลอมที่สำเร็จ."""
    class Sock:
        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

    calls = []

    def fake_connect(addr, timeout=None, **kw):
        calls.append((addr, timeout))
        return Sock()

    monkeypatch.setattr(setup_check.socket, "create_connection", fake_connect)
    return calls


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


def get(results, key):
    found = [r for r in results if key in r[1]]
    assert len(found) == 1, (key, [r[1] for r in results])
    return found[0]


def run(settings, source=None, moph=True, store=None):
    return setup_check.run_checks(settings, source or FakeSource(), moph, store=store)


# ---------- run_checks ----------

def test_run_checks_all_ok_on_healthy_source(tmp_path, monkeypatch):
    # สภาพ "สุขภาพดี": อำเภอที่ตั้งไว้มีไฟล์เส้นขอบ (3025 ส่งมากับโค้ด) + ไฟล์ตำบล + สถานพยาบาลมีพิกัดครบ
    # ไม่งั้นแถวขอบเขตอำเภอ/ตำบล/พิกัดสถานพยาบาลเป็น ⚠ ตั้งใจ
    bounds = tambon_dir(tmp_path, monkeypatch)
    shutil.copy(ROOT / "boundaries" / "3025.geojson", bounds / "3025.geojson")
    results = run(make_settings(tmp_path, DENGUE_AREA_AMPHOE="3025"), FakeSource(facilities=[FAC_RPST]),
                  store=placed_store(tmp_path, FAC_RPST))
    assert all(len(r) == 3 and r[0] in ("ok", "warn", "fail") for r in results)
    assert [r for r in results if r[0] != "ok"] == []


def test_run_checks_fail_when_table_missing(settings):
    src = FakeSource()
    del src.schema["ovstdiag"]
    status, _, detail = get(run(settings, src), "ตารางที่ใช้")
    assert status == "fail" and "ovstdiag" in detail


def test_run_checks_thaiaddress_missing_is_only_warn(settings):
    src = FakeSource()
    del src.schema["thaiaddress"]
    status, _, detail = get(run(settings, src), "ตารางที่ใช้")
    assert status == "warn" and "thaiaddress" in detail


def test_run_checks_warns_when_moph_keys_missing(settings):
    status, _, detail = get(run(settings, moph=False), "กุญแจ MOPH")
    assert status == "warn"
    assert get(run(settings, moph=True), "กุญแจ MOPH")[0] == "ok"


def test_run_checks_one_failure_does_not_stop_others(settings):
    src = FakeSource(failure=HosxpError("boom"), fail_methods={"server_now"})
    results = run(settings, src)
    assert get(results, "เชื่อมต่อ HOSxP")[0] == "fail"
    assert get(results, "ตารางที่ใช้")[0] == "ok"
    assert get(results, "SECRET_KEY")[0] == "ok"
    assert len(results) == len(run(settings))


def test_run_checks_exception_detail_is_type_name_only(settings):
    src = FakeSource(failure=RuntimeError("password=hunter2 host=10.9.8.7 user=nop"))
    results = run(settings, src)
    assert [r for r in results if r[0] == "fail"]
    for status, name, detail in results:
        assert "hunter2" not in detail and "10.9.8.7" not in detail
        assert "user=nop" not in detail
    assert "RuntimeError" in get(results, "เชื่อมต่อ HOSxP")[2]


def test_python_version_check(settings, monkeypatch):
    monkeypatch.setattr(sys, "version_info", (3, 9, 18, "final", 0))
    assert get(run(settings), "Python")[0] == "fail"
    monkeypatch.setattr(sys, "version_info", (3, 10, 0, "final", 0))
    assert get(run(settings), "Python")[0] == "ok"


def test_libraries_missing_is_fail(settings, monkeypatch):
    real = setup_check.importlib.import_module

    def fake(name, *a, **k):
        if name == "pymysql":
            raise ImportError("no module")
        return real(name, *a, **k)

    monkeypatch.setattr(setup_check.importlib, "import_module", fake)
    status, _, detail = get(run(settings), "ไลบรารี")
    assert status == "fail" and "pymysql" in detail


def test_timezone_warns_when_db_clock_differs(settings):
    src = FakeSource()
    src.now = datetime.now() - timedelta(hours=7)
    assert get(run(settings, src), "เขตเวลา")[0] == "warn"
    src.now = datetime.now() - timedelta(minutes=2)
    assert get(run(settings, src), "เขตเวลา")[0] == "ok"


def test_read_only_session_required(settings):
    src = FakeSource()
    src.read_only = False
    assert get(run(settings, src), "อ่านอย่างเดียว")[0] == "fail"


def test_write_privileges_warn_names_only(settings):
    src = FakeSource()
    src.write_privs = ["INSERT", "UPDATE"]
    status, _, detail = get(run(settings, src), "สิทธิ์บัญชี")
    assert status == "warn" and "INSERT, UPDATE" in detail
    assert get(run(settings), "สิทธิ์บัญชี")[0] == "ok"


def test_missing_columns_fail_and_listed(settings):
    src = FakeSource()
    del src.schema["patient"]["moopart"]
    status, _, detail = get(run(settings, src), "คอลัมน์ที่ใช้")
    assert status == "fail" and "patient.moopart" in detail


def test_integer_visit_keys_warn(settings):
    src = FakeSource()
    src.schema["ovstdiag"]["vn"] = "bigint"
    status, _, detail = get(run(settings, src), "ชนิด vn/an")
    assert status == "warn" and "ovstdiag.vn" in detail


def test_thai_text_unreadable_is_fail(settings):
    src = FakeSource()
    src.thai = ["????? ???", "???"]
    assert get(run(settings, src), "ภาษาไทย")[0] == "fail"
    src.thai = []
    assert get(run(settings, src), "ภาษาไทย")[0] == "warn"


def test_latest_visit_stale_warns(settings):
    src = FakeSource()
    src.latest_visit = date.today() - timedelta(days=45)
    assert get(run(settings, src), "วินิจฉัยล่าสุด")[0] == "warn"
    src.latest_visit = None
    assert get(run(settings, src), "วินิจฉัยล่าสุด")[0] == "warn"


def test_village_and_area_checks(settings, tmp_path):
    src = FakeSource(area=())
    src.villages = {"total": 0, "with_centroid": 0}
    assert get(run(settings, src), "ตาราง village")[0] == "warn"
    assert get(run(settings, src), "อำเภอในเขต")[0] == "warn"
    configured = make_settings(tmp_path, DENGUE_AREA_AMPHOE="4001")
    assert get(run(configured, src), "อำเภอในเขต")[0] == "ok"


def test_centroid_coverage_warns_with_counts_only(settings):
    src = FakeSource()
    src.villages = {"total": 9, "with_centroid": 3}
    status, _, detail = get(run(settings, src), "จุดกลางหมู่บ้าน")
    assert status == "warn" and "3" in detail and "9" in detail


def test_icd_codes_found_vs_used(settings):
    status, _, detail = get(run(settings), "DENGUE_ICDS")
    assert status == "ok" and "A90" in detail and "A91" in detail
    src = FakeSource()
    src.icd_found = {}
    assert get(run(settings, src), "DENGUE_ICDS")[0] == "warn"


def test_moph_host_unreachable_warns(settings, monkeypatch, no_network):
    assert get(run(settings), "เข้าถึง MOPH")[0] == "ok"
    addr, timeout = no_network[-1]
    assert addr == ("morpromt2f.moph.go.th", 443) and timeout == 5

    def refuse(*a, **k):
        raise socket.timeout("timed out")

    monkeypatch.setattr(setup_check.socket, "create_connection", refuse)
    assert get(run(settings), "เข้าถึง MOPH")[0] == "warn"


def test_secret_key_and_audited_state(tmp_path):
    s = make_settings(tmp_path, SECRET_KEY="", DENGUE_DATA_AUDITED="0")
    results = run(s)
    assert get(results, "SECRET_KEY")[0] == "fail"
    assert get(results, "DENGUE_DATA_AUDITED")[0] == "warn"


def test_results_never_contain_secrets(tmp_path):
    s = make_settings(tmp_path, MOPH_CLIENT_KEY=MOPH_ID, MOPH_SECRET_KEY=MOPH_SEC)
    text = json.dumps(run(s), ensure_ascii=False)
    for secret in (SECRET, MOPH_ID, MOPH_SEC):
        assert secret not in text


# ---------- CLI ----------

def fake_runtime(settings, source, sender=object()):
    return SimpleNamespace(settings=settings, source=source, sender=sender)


def test_cli_exit_code_1_when_fail(settings, capsys):
    src = FakeSource()
    del src.schema["ovst"]
    code = setup_check.main([], build=lambda: fake_runtime(settings, src))
    out = capsys.readouterr().out
    assert code == 1
    assert "✘" in out and "✔" in out and "ovst" in out


def test_cli_exit_code_0_when_only_warnings(settings, capsys):
    code = setup_check.main([], build=lambda: fake_runtime(settings, FakeSource(), None))
    out = capsys.readouterr().out
    assert code == 0 and "⚠" in out
    assert not [line for line in out.splitlines() if line.startswith("✘")]


def test_cli_build_failure_exits_1(capsys):
    def boom():
        raise RuntimeError("password=hunter2")

    assert setup_check.main([], build=boom) == 1
    out = capsys.readouterr()
    assert "hunter2" not in out.out + out.err


# ---------- HosxpSource: เมธอดอ่านโครงสร้าง ----------

class Cur:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        self.conn.executed.append((sql, tuple(params)))
        if sql.startswith("SET SESSION"):
            if self.conn.reject_set:
                raise RuntimeError("unknown system variable")
            self.rows = []
            return
        if self.conn.error is not None:
            raise self.conn.error
        self.rows = self.conn.results.pop(0)

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class Conn:
    def __init__(self, *results, reject_set=False, error=None):
        self.results = [list(r) for r in results]
        self.executed = []
        self.reject_set = reject_set
        self.error = error

    def cursor(self):
        return Cur(self)

    def close(self):
        pass


def test_write_privileges_parses_names_only():
    conn = Conn([("hos",)], [
        ("GRANT SELECT, INSERT, UPDATE ON `hos`.* TO `reader`@`10.1.1.1`",),
        ("GRANT DELETE ON `other`.* TO `reader`@`10.1.1.1`",),
        ("GRANT USAGE ON *.* TO `reader`@`10.1.1.1` IDENTIFIED BY PASSWORD 'abc'",),
        ("GRANT DROP ON `hos`.`ovst` TO `reader`@`10.1.1.1`",),
    ])
    privs = HosxpSource(lambda: conn).write_privileges()
    assert privs == ["INSERT", "UPDATE"]
    assert "reader" not in " ".join(privs) and "10.1.1.1" not in " ".join(privs)


def test_write_privileges_all_and_global():
    conn = Conn([("hos",)], [("GRANT ALL PRIVILEGES ON *.* TO `root`@`%`",)])
    assert HosxpSource(lambda: conn).write_privileges() == ["ALL PRIVILEGES"]
    conn = Conn([("hos",)], [("GRANT SELECT ON `hos`.* TO `r`@`%`",)])
    assert HosxpSource(lambda: conn).write_privileges() == []


def test_session_read_only_and_server_now():
    assert HosxpSource(lambda: Conn([(1,)])).session_read_only() is True
    assert HosxpSource(lambda: Conn([(0,)])).session_read_only() is False
    now = datetime(2026, 11, 8, 10, 0, 0)
    assert HosxpSource(lambda: Conn([(now,)])).server_now() == now


def test_table_columns_shape_and_parameters():
    conn = Conn([("ovst", "vn", "varchar"), ("OVST", "hn", "varchar")])
    cols = HosxpSource(lambda: conn).table_columns(("ovst", "ipt"))
    assert cols == {"ovst": {"vn": "varchar", "hn": "varchar"}}
    sql, params = statements(conn)[0]
    assert "information_schema" in sql and "DATABASE()" in sql
    assert params == ("ovst", "ipt") and sql.count("%s") == 2


def statements(conn):
    """คำสั่งที่รันจริง ไม่นับ SET SESSION ของ timeout."""
    return [e for e in conn.executed if not e[0].startswith("SET SESSION")]


def test_icd_counts_and_area_counts_use_parameters_and_return_counts():
    conn = Conn([("A90", 8), ("A911", 2)], [("A90", 1)], [("A911", 1)])
    got = HosxpSource(lambda: conn).icd_counts(("A90", "A91"), date(2025, 11, 8))
    assert got == {"A90": 9, "A911": 3}
    assert len(statements(conn)) == 3
    for sql, params in statements(conn):
        assert "A9" not in sql and sql.count("%s") == len(params)
        assert "GROUP BY" in sql
    conn = Conn([("4001", 9, 8)], [("4001", 1, 1)], [("4002", 1, 0)])
    area = HosxpSource(lambda: conn).area_counts(("A90",), date(2025, 11, 8))
    assert area == {"4001": {"visits": 10, "mapped": 9},
                    "4002": {"visits": 1, "mapped": 0}}
    for sql, params in statements(conn):
        assert "A90" not in sql and sql.count("%s") == len(params)


def test_audit_date_filters_are_sargable():
    """ตัวกรองวันที่เทียบคอลัมน์ตรง ๆ (ใช้ดัชนี vstdate ได้) ห้ามห่อ COALESCE/ฟังก์ชัน;
    ค่า NULL ของ ovstdiag.vstdate ใช้สาขาแยกที่จำกัดด้วย IS NULL (ดัชนี) แล้วค่อยดู ovst.vstdate."""
    since = date(2025, 11, 8)
    for call in (lambda s: s.icd_counts(("A90",), since),
                 lambda s: s.area_counts(("A90",), since)):
        conn = Conn([], [], [])
        call(HosxpSource(lambda: conn))
        sqls = [q for q, _ in statements(conn)]
        assert len(sqls) == 3
        assert not any("COALESCE" in q.upper() for q in sqls)
        main, null_branch, ipd = sqls
        assert "d.vstdate >= %s" in main and "ovst o" not in main
        assert "d.vstdate IS NULL AND o.vstdate >= %s" in null_branch
        assert "i.regdate >= %s" in ipd


def test_latest_visit_uses_plain_max_on_index_column():
    conn = Conn([(date(2026, 11, 7),)])
    HosxpSource(lambda: conn).latest_visit_date()
    assert statements(conn) == [("SELECT MAX(vstdate) FROM ovstdiag", ())]


def test_audit_queries_set_statement_timeout_first():
    conn = Conn([(1,)])
    HosxpSource(lambda: conn).session_read_only()
    assert conn.executed[0][0] == "SET SESSION max_statement_time = 15"
    assert len(conn.executed) == 2


def test_audit_continues_when_timeout_variable_rejected():
    conn = Conn([(date(2026, 11, 7),)], reject_set=True)
    assert HosxpSource(lambda: conn).latest_visit_date() == date(2026, 11, 7)


def test_detector_queries_do_not_set_timeout():
    conn = Conn([], [])
    HosxpSource(lambda: conn).new_dx(datetime(2026, 11, 1), ("A90",))
    assert not [q for q, _ in conn.executed if q.startswith("SET SESSION")]


def test_statement_timeout_supported_probe():
    assert HosxpSource(lambda: Conn()).statement_timeout_supported() is True
    assert HosxpSource(lambda: Conn(reject_set=True)).statement_timeout_supported() is False


def test_statement_timeout_error_becomes_hosxp_timeout():
    from hosxp import HosxpTimeout

    class Killed(Exception):
        pass

    conn = Conn(error=Killed(1969, "Query execution was interrupted (max_statement_time exceeded)"))
    with pytest.raises(HosxpTimeout) as info:
        HosxpSource(lambda: conn).latest_visit_date()
    assert isinstance(info.value, HosxpError)
    other = Conn(error=Killed(1045, "denied"))
    with pytest.raises(HosxpError) as info2:
        HosxpSource(lambda: other).latest_visit_date()
    assert not isinstance(info2.value, HosxpTimeout)


def test_run_checks_timeout_check_warns_when_unsupported_and_names_timeout_type(settings):
    src = FakeSource()
    src.timeout_ok = False
    status, _, detail = get(run(settings, src), "timeout คำสั่ง")
    assert status == "warn" and "ตั้ง timeout คำสั่งไม่ได้" in detail
    from hosxp import HosxpTimeout
    src = FakeSource(failure=HosxpTimeout("x"), fail_methods={"latest_visit_date"})
    status, _, detail = get(run(settings, src), "วินิจฉัยล่าสุด")
    assert status == "fail" and "HosxpTimeout" in detail


class CountingSource(FakeSource):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.calls = {}

    def __getattribute__(self, name):
        attr = super().__getattribute__(name)
        if name.startswith("_") or not callable(attr) or name == "calls":
            return attr
        calls = super().__getattribute__("calls")

        def wrapper(*a, **k):
            calls[name] = calls.get(name, 0) + 1
            return attr(*a, **k)
        return wrapper


def test_run_report_reads_each_fact_at_most_once_even_when_down(settings):
    for src in (CountingSource(), CountingSource(failure=HosxpError("down"))):
        setup_check.run_report(settings, src, True)
        assert src.calls and max(src.calls.values()) == 1, src.calls


def test_icd_counts_empty_icds_returns_empty():
    conn = Conn()
    assert HosxpSource(lambda: conn).icd_counts((), date(2025, 1, 1)) == {}
    assert HosxpSource(lambda: conn).area_counts((), date(2025, 1, 1)) == {}
    assert conn.executed == []


def test_village_points_filters_bad_coordinates():
    conn = Conn([("30250100", "หมู่ 1", "14.36", "101.80"),
                 ("30250101", "หมู่ 2", None, None),
                 ("30250102", "หมู่ 3", "abc", "101"),
                 ("30250103", "หมู่ 4", "0", "0"),
                 ("30250104", "หมู่ 5", "nan", "101")])
    pts = HosxpSource(lambda: conn).village_points()
    assert pts == [{"code": "30250100", "name": "หมู่ 1", "lat": 14.36, "lng": 101.80}]


def test_village_counts_and_latest_visit():
    assert HosxpSource(lambda: Conn([(9, 3)])).village_counts() == {
        "total": 9, "with_centroid": 3}
    assert HosxpSource(lambda: Conn([(date(2026, 11, 7),)])).latest_visit_date() == date(2026, 11, 7)
    assert HosxpSource(lambda: Conn([(None,)])).latest_visit_date() is None


# ---------- is_lan_client ----------

@pytest.fixture
def plain_app(settings, store):
    return create_app(settings=settings, store=store, source=FakeSource(),
                      sender=FakeSender())


def lan(plain_app, remote, xff=None, prefixes=LAN):
    from flask import request
    headers = {"X-Forwarded-For": xff} if xff is not None else {}
    with plain_app.test_request_context("/", headers=headers,
                                        environ_base={"REMOTE_ADDR": remote}):
        return is_lan_client(request, prefixes)


def test_is_lan_client_public_xff_from_localhost_denied(plain_app):
    assert lan(plain_app, "127.0.0.1", "203.0.113.9") is False


def test_is_lan_client_lan_xff_allowed(plain_app):
    assert lan(plain_app, "127.0.0.1", "192.168.70.135") is True


def test_is_lan_client_no_xff_uses_remote_addr(plain_app):
    assert lan(plain_app, "10.1.2.3") is True
    assert lan(plain_app, "203.0.113.9") is False


def test_is_lan_client_spoofed_first_xff_entry_denied(plain_app):
    assert lan(plain_app, "127.0.0.1", "192.168.1.5, 203.0.113.9") is False
    assert lan(plain_app, "127.0.0.1", "203.0.113.9, 192.168.1.5") is True


def test_is_lan_client_empty_prefixes_denied(plain_app):
    assert lan(plain_app, "127.0.0.1", prefixes=()) is False
    assert lan(plain_app, "10.0.0.1", prefixes=("",)) is False


def test_is_lan_client_rejects_non_ip_and_empty_xff(plain_app):
    assert lan(plain_app, "127.0.0.1", "10.evil.example.com") is False
    assert lan(plain_app, "127.0.0.1", "") is False
    assert lan(plain_app, "127.0.0.1", " , ") is False


def test_is_lan_client_ipv4_mapped_ipv6(plain_app):
    assert lan(plain_app, "::ffff:10.1.2.3") is True
    assert lan(plain_app, "::ffff:203.0.113.9") is False


def test_is_lan_client_prefix_is_not_substring(plain_app):
    assert lan(plain_app, "110.1.2.3") is False
    assert lan(plain_app, "10.1.2.3", prefixes=("172.16.",)) is False


# ---------- หน้า /check ----------

HN, PNAME = "ZX9071", "นายลับ ใจดี"
PUBLIC = {"X-Forwarded-For": "203.0.113.9"}


@pytest.fixture
def source():
    return FakeSource(rows=[make_row("VN:1", hn=HN)],
                      contacts={HN: {"name": PNAME, "phone": "0899990001",
                                     "address": "บ้านเลขที่ 99"}})


@pytest.fixture
def app(tmp_path, store, source):
    return create_app(settings=make_settings(tmp_path, MOPH_CLIENT_KEY=MOPH_ID,
                                             MOPH_SECRET_KEY=MOPH_SEC),
                      store=store, source=source, sender=FakeSender())


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def viewer(client, login_as):
    login_as(client, "viewer1", "viewer")
    return client


def test_check_page_403_from_public_ip(viewer):
    r = viewer.get("/check", headers=PUBLIC)
    assert r.status_code == 403
    body = r.get_data(as_text=True)
    assert "เครือข่ายภายใน" in body
    assert "A90" not in body and "ovstdiag" not in body and "check-data" not in body


def test_check_page_requires_login_and_lan(client, login_as):
    # ยังไม่ login จาก LAN -> ไปหน้า login
    r = client.get("/check")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    # ยังไม่ login จากนอก LAN -> 403 (ไม่ redirect ไปหน้า login)
    assert client.get("/check", headers=PUBLIC).status_code == 403
    # login แล้วแต่อยู่นอก LAN -> 403
    login_as(client)
    assert client.get("/check", headers=PUBLIC).status_code == 403
    # login + LAN -> 200
    assert client.get("/check", headers={"X-Forwarded-For": "192.168.70.135"}).status_code == 200
    assert client.get("/check").status_code == 200


def test_check_page_has_counts_only(viewer):
    body = viewer.get("/check").get_data(as_text=True)
    assert "A90" in body and "ตารางที่ใช้" in body
    assert "ยืนยันว่าตรวจกับข้อมูล รพ. นี้แล้ว" in body and "ล้างการยืนยัน" in body
    for private in (HN, PNAME, "0899990001", "บ้านเลขที่ 99", "VN:1"):
        assert private not in body
    # จำนวนนับของรหัสโรค/อำเภอจาก FakeSource ปรากฏ
    assert "4001" in body and ">8<" in body and ">9<" in body


def test_check_page_never_prints_secrets(viewer):
    body = viewer.get("/check").get_data(as_text=True)
    for secret in (SECRET, MOPH_ID, MOPH_SEC):
        assert secret not in body
    assert "ตั้งแล้ว" in body


def test_check_page_unset_secret_states_and_tile_key_hidden(tmp_path, store, login_as):
    s = make_settings(
        tmp_path, DENGUE_TILE_URL="https://tiles.example.com/{z}/{x}/{y}.png?key=TILEKEY123")
    app = create_app(settings=s, store=store, source=FakeSource(), sender=None)
    c = app.test_client()
    login_as(c)
    body = c.get("/check").get_data(as_text=True)
    assert "ยังไม่ได้ตั้ง" in body
    # ค่าตั้งที่แสดงในตาราง env ไม่โชว์ query ของ tile URL (อาจมีคีย์)
    env_part = body.split('id="env-table"')[1].split("</table>")[0]
    assert "TILEKEY123" not in env_part and "tiles.example.com" in env_part


def test_check_page_data_block_is_valid_json_with_villages(viewer):
    body = viewer.get("/check").get_data(as_text=True)
    m = re.search(r'<script type="application/json" id="check-data">(.*?)</script>',
                  body, re.S)
    data = json.loads(m.group(1))
    assert [v["code"] for v in data["villages"]] == ["30250100", "30250111"]
    assert data["tile_url"].startswith("https://")
    assert "leaflet/leaflet.js" in body and "check.js" in body


def test_check_page_escapes_village_names(tmp_path, store, login_as):
    src = FakeSource()
    src.points = [{"code": "30250100", "name": "</script><b>x", "lat": 14.3, "lng": 101.8}]
    app = create_app(settings=make_settings(tmp_path), store=store, source=src,
                     sender=FakeSender())
    c = app.test_client()
    login_as(c)
    body = c.get("/check").get_data(as_text=True)
    assert "</script><b>x" not in body


def test_check_page_survives_hosxp_down(tmp_path, store, login_as):
    src = FakeSource(failure=HosxpError("connect failed: password=hunter2"))
    app = create_app(settings=make_settings(tmp_path), store=store, source=src,
                     sender=FakeSender())
    c = app.test_client()
    login_as(c)
    r = c.get("/check")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert body.count('class="pill pill-red"') >= 10   # แถว ✘ ล้มเหลวจริง ไม่ใช่แค่ป้ายสรุป
    assert 'data-testid="check-results"' in body and "ตรวจไม่สำเร็จ (HosxpError)" in body
    assert "hunter2" not in body


def test_check_page_without_source(tmp_path, store, login_as):
    app = create_app(settings=make_settings(tmp_path), store=store, source=None,
                     sender=None)
    c = app.test_client()
    login_as(c)
    r = c.get("/check")
    assert r.status_code == 200 and "ยังไม่ได้ตั้งค่า" in r.get_data(as_text=True)


def test_nav_link_only_for_lan_clients(viewer):
    assert 'href="/check"' in viewer.get("/").get_data(as_text=True)
    assert "/check" not in viewer.get("/", headers=PUBLIC).get_data(as_text=True)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def cached_app(tmp_path, store):
    src = CountingSource()
    app = create_app(settings=make_settings(tmp_path), store=store, source=src,
                     sender=FakeSender())
    clock = Clock()
    cache = app.extensions["check_cache"]
    cache.clock = clock
    cache.wall = lambda: datetime(2026, 11, 8, 14, 5)
    return app, src, clock


def test_check_page_is_cached_for_300_seconds(tmp_path, store, login_as):
    app, src, clock = cached_app(tmp_path, store)
    c = app.test_client()
    login_as(c)
    body = c.get("/check").get_data(as_text=True)
    assert "ข้อมูลเมื่อ 14:05" in body
    n = src.calls["icd_counts"]
    clock.t += 299
    c.get("/check")
    assert src.calls["icd_counts"] == n
    clock.t += 2
    c.get("/check")
    assert src.calls["icd_counts"] == n + 1


def test_check_refresh_is_rate_limited_to_30_seconds(tmp_path, store, login_as):
    app, src, clock = cached_app(tmp_path, store)
    c = app.test_client()
    login_as(c)
    c.get("/check")
    n = src.calls["icd_counts"]
    clock.t += 10
    body = c.get("/check?refresh=1").get_data(as_text=True)
    assert src.calls["icd_counts"] == n
    assert 'data-testid="check-throttled"' in body and "ข้อมูลเมื่อ 14:05" in body
    clock.t += 21
    body = c.get("/check?refresh=1").get_data(as_text=True)
    assert src.calls["icd_counts"] == n + 1
    assert 'data-testid="check-throttled"' not in body


def test_report_cache_runs_compute_once_for_simultaneous_requests():
    import threading
    import time as _t
    from check_views import ReportCache
    runs = []

    def slow():
        runs.append(1)
        _t.sleep(0.2)
        return ([], {})

    cache = ReportCache(slow)
    threads = [threading.Thread(target=cache.get) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(runs) == 1


def test_lan_prefixes_normalised_with_trailing_dot():
    s = load_settings({"SECRET_KEY": "k", "DENGUE_LAN_PREFIXES": "10, 172.16 ,192.168."})
    assert s.lan_prefixes == ("10.", "172.16.", "192.168.")
    assert load_settings({"SECRET_KEY": "k"}).lan_prefixes[0] == "10."


def test_is_lan_client_normalised_prefix_does_not_match_longer_octet(plain_app):
    prefixes = load_settings({"SECRET_KEY": "k", "DENGUE_LAN_PREFIXES": "10"}).lan_prefixes
    assert lan(plain_app, "100.1.1.1", prefixes=prefixes) is False
    assert lan(plain_app, "10.1.1.1", prefixes=prefixes) is True
    host = load_settings({"SECRET_KEY": "k", "DENGUE_LAN_PREFIXES": "10.0.0.1"}).lan_prefixes
    assert lan(plain_app, "10.0.0.1", prefixes=host) is True
    assert lan(plain_app, "10.0.0.10", prefixes=host) is False


def test_client_key_shares_forwarded_parsing(plain_app):
    from auth import client_key
    for headers, remote, want in (
            ({"X-Forwarded-For": "1.1.1.1, 2.2.2.2"}, "127.0.0.1", "2.2.2.2"),
            ({"X-Forwarded-For": " , "}, "127.0.0.1", "127.0.0.1"),
            ({}, "10.1.1.1", "10.1.1.1")):
        with plain_app.test_request_context("/", headers=headers,
                                            environ_base={"REMOTE_ADDR": remote}):
            assert client_key() == want


def test_check_js_uses_expected_storage_key():
    js = (ROOT / "static" / "check.js").read_text(encoding="utf-8")
    assert "dengue_audited" in js and "textContent" in js
    assert "innerHTML" not in js and 'referrerPolicy: "origin"' in js


@pytest.mark.parametrize("bad", ["", "short-key", "<สุ่มยาว 32 ตัวอักษรขึ้นไป>"])
def test_secret_key_fails_when_weak_or_placeholder_without_printing_value(tmp_path, bad):
    s = make_settings(tmp_path, SECRET_KEY=bad)
    status, _, detail = get(run(s), "SECRET_KEY")
    assert status == "fail"
    assert "SECRET_KEY" in detail
    assert not bad.strip() or bad not in detail


def test_secret_key_sixteen_chars_is_ok(tmp_path):
    assert get(run(make_settings(tmp_path, SECRET_KEY="0123456789abcdef")), "SECRET_KEY")[0] == "ok"


@pytest.mark.parametrize("env", [{"MOPH_CLIENT_KEY": "<Client ID ของกลุ่ม>", "MOPH_SECRET_KEY": MOPH_SEC},
                                 {"MOPH_CLIENT_KEY": MOPH_ID, "MOPH_SECRET_KEY": "<Secret ของกลุ่ม>"}])
def test_moph_placeholder_keys_warn_even_if_runtime_says_configured(tmp_path, env):
    status, _, detail = get(run(make_settings(tmp_path, **env), moph=True), "กุญแจ MOPH")
    assert status == "warn" and "<" in detail and "ตัวอย่าง" in detail
    assert MOPH_ID not in detail and MOPH_SEC not in detail


# ---------- DENGUE_START_FROM ----------

START_CHECK = "วันเริ่มแจ้งเตือน"


def test_start_from_check_ok_for_normal_values(tmp_path):
    for env in ({"DENGUE_START_FROM": "2026-10-01"},
                {"DENGUE_START_FROM": date.today().isoformat()}):
        status, name, detail = get(run(make_settings(tmp_path, **env)), START_CHECK)
        assert status == "ok" and "DENGUE_START_FROM" in name
        assert "2569" not in detail


def test_start_from_check_warns_when_unset_because_it_slides_forward_on_every_restart(tmp_path):
    import config
    for env in ({"DENGUE_START_FROM": None}, {"DENGUE_START_FROM": ""}, {"DENGUE_START_FROM": "   "}):
        status, name, detail = get(run(make_settings(tmp_path, **env)), START_CHECK)
        assert status == "warn" and "DENGUE_START_FROM" in name
        assert detail == config.MSG_START_FROM_UNSET
        assert "ยังไม่ได้ตั้ง DENGUE_START_FROM" in detail and "restart" in detail
        assert "ลงรหัสย้อนหลังหลัง restart จะไม่ถูกตรวจ" in detail and "ตั้งเป็นวัน go-live" in detail


def test_start_from_check_ok_when_explicitly_set_to_today(tmp_path):
    status, _, detail = get(run(make_settings(tmp_path, DENGUE_START_FROM=date.today().isoformat())), START_CHECK)
    assert status == "ok" and "ยังไม่ได้ตั้ง" not in detail


def test_start_from_check_warns_on_invalid_without_raw_text(tmp_path):
    raw = "15/11/secret-typo"
    status, _, detail = get(run(make_settings(tmp_path, DENGUE_START_FROM=raw)), START_CHECK)
    assert status == "warn"
    assert "อ่านเป็นวันที่ไม่ได้" in detail and date.today().isoformat() in detail
    assert raw not in detail and "secret-typo" not in detail


def test_start_from_check_warns_on_future_date(tmp_path):
    status, _, detail = get(
        run(make_settings(tmp_path, DENGUE_START_FROM="2569-11-15")), START_CHECK)
    assert status == "warn"
    assert "2569-11-15" in detail and "ค.ศ." in detail and "อนาคต" in detail


# ---------- env_rows (หน้า /check) ----------

def _secret_states(settings):
    from check_views import env_rows
    return {r["name"]: r["state"] for r in env_rows(settings)[1]}


def test_env_rows_realert_label_describes_real_behavior(tmp_path):
    from check_views import env_rows
    note = {r["name"]: r["note"] for r in env_rows(make_settings(tmp_path))[0]}[
        "DENGUE_REALERT_DAYS"]
    assert "HN เดิม" in note and "ไม่สร้างเคสใหม่" in note and "ไม่แจ้งซ้ำ" in note
    assert "cluster" not in note


def test_env_rows_secret_states(tmp_path):
    ok = _secret_states(make_settings(tmp_path, MOPH_CLIENT_KEY=MOPH_ID,
                                      MOPH_SECRET_KEY=MOPH_SEC))
    assert ok["SECRET_KEY"] == ok["MOPH_CLIENT_KEY"] == ok["MOPH_SECRET_KEY"] == "set"
    empty = _secret_states(make_settings(tmp_path, SECRET_KEY="  "))
    assert empty["SECRET_KEY"] == empty["MOPH_CLIENT_KEY"] == "unset"
    bad = _secret_states(make_settings(
        tmp_path, SECRET_KEY="<สุ่มยาว 32 ตัวอักษร>", MOPH_CLIENT_KEY="<Client ID ของกลุ่ม>",
        MOPH_SECRET_KEY="<Secret ของกลุ่ม>"))
    assert bad["SECRET_KEY"] == bad["MOPH_CLIENT_KEY"] == bad["MOPH_SECRET_KEY"] == "invalid"
    assert _secret_states(make_settings(tmp_path, SECRET_KEY="short"))["SECRET_KEY"] == "invalid"


def test_check_page_marks_placeholder_moph_keys_without_printing_values(
        tmp_path, store, login_as):
    s = make_settings(tmp_path, MOPH_CLIENT_KEY="<CLIENT-PLACEHOLDER-VALUE>")
    c = create_app(settings=s, store=store, source=FakeSource(), sender=None).test_client()
    login_as(c)
    env_part = c.get("/check").get_data(as_text=True).split('id="env-table"')[1].split(
        "</table>")[0]
    assert "ยังเป็นค่าตัวอย่าง/ไม่ถูกต้อง" in env_part
    assert "ยังไม่ได้ตั้ง" in env_part        # MOPH_SECRET_KEY ว่าง
    assert "ตั้งแล้ว" in env_part             # SECRET_KEY ถูกต้อง
    assert "CLIENT-PLACEHOLDER-VALUE" not in env_part and SECRET not in env_part


# ---------- DENGUE_BASE_URL (R24 minor) ----------

BASE_CHECK = "DENGUE_BASE_URL"


@pytest.mark.parametrize("good", ["https://wnkhos.go.th/dengue", "http://10.1.2.3:6800",
                                  "https://example.org/x/"])
def test_base_url_check_ok_for_http_urls(tmp_path, good):
    status, _, detail = get(run(make_settings(tmp_path, DENGUE_BASE_URL=good)), BASE_CHECK)
    assert status == "ok"


@pytest.mark.parametrize("bad", ["<https://โดเมนของ รพ./dengue>", "ftp://files.example.org/x",
                                 "wnkhos.go.th/dengue", "javascript:alert(1)", "https://",
                                 "https://bad host/x", "   "])
def test_base_url_check_fails_for_placeholder_or_non_http(tmp_path, bad):
    status, name, detail = get(run(make_settings(tmp_path, DENGUE_BASE_URL=bad)), BASE_CHECK)
    assert status == "fail" and "DENGUE_BASE_URL" in detail
    assert bad.strip() not in detail or not bad.strip()     # ไม่พิมพ์ค่าทั้งก้อนกลับ


def test_base_url_check_never_prints_more_than_scheme_and_host(tmp_path):
    bad = "gopher://user:pw-SECRET@files.example.org/private/path?token=ABC123"
    status, _, detail = get(run(make_settings(tmp_path, DENGUE_BASE_URL=bad)), BASE_CHECK)
    assert status == "fail"
    for leak in ("pw-SECRET", "user:", "private", "token", "ABC123"):
        assert leak not in detail


def test_check_page_env_row_marks_invalid_base_url(tmp_path, store, login_as):
    s = make_settings(tmp_path, DENGUE_BASE_URL="<https://โดเมนของ รพ./dengue>")
    c = create_app(settings=s, store=store, source=FakeSource(), sender=None).test_client()
    login_as(c)
    env_part = c.get("/check").get_data(as_text=True).split('id="env-table"')[1].split(
        "</table>")[0]
    assert "ค่าไม่ถูกต้อง" in env_part and "โดเมนของ รพ." not in env_part


def test_check_page_env_row_shows_valid_base_url_and_cancel_check_days(tmp_path, store,
                                                                        login_as):
    s = make_settings(tmp_path, DENGUE_BASE_URL="https://x.example.org/dengue",
                      DENGUE_CANCEL_CHECK_DAYS="45")
    c = create_app(settings=s, store=store, source=FakeSource(), sender=None).test_client()
    login_as(c)
    env_part = c.get("/check").get_data(as_text=True).split('id="env-table"')[1].split(
        "</table>")[0]
    assert "https://x.example.org/dengue" in env_part and "ค่าไม่ถูกต้อง" not in env_part
    assert "DENGUE_CANCEL_CHECK_DAYS" in env_part and "45" in env_part


# ---------- จำนวน visit ตามปี พ.ศ. (หน้า /trend) ----------

def test_check_year_visits_ok_and_warns_on_empty_prior_year(settings):
    from trend import year_of
    status, _, detail = get(run(settings), "ปี พ.ศ.")
    assert status == "ok" and str(year_of(date.today())) in detail
    src = FakeSource()
    empty = year_of(date.today()) - 2
    src.visits_by_year = {empty: 0}
    status, _, detail = get(run(settings, src), "ปี พ.ศ.")
    assert status == "warn" and str(empty) in detail and "/trend" in detail


def test_check_year_visits_failure_is_warning_not_fail(settings):
    src = FakeSource(failure=HosxpError("x"), fail_methods=["visits_in_ranges"])
    status, _, detail = get(run(settings, src), "ปี พ.ศ.")
    assert status == "warn" and "ไม่สำเร็จ" in detail


def test_audit_reports_visits_per_year(settings):
    from trend import year_of
    src = FakeSource()
    cur = year_of(date.today())
    src.visits_by_year = {cur: 3, cur - 1: 0, cur - 2: 7, cur - 3: 9}
    _, audit = setup_check.run_report(settings, src, True)
    rows = audit["years"]["rows"]
    assert [(r["year"], r["visits"], r["current"]) for r in rows] == [
        (cur, 3, True), (cur - 1, 0, False), (cur - 2, 7, False), (cur - 3, 9, False)]
    assert audit["years"]["error"] is None
    bad = FakeSource(failure=HosxpError("x"), fail_methods=["visits_in_ranges"])
    _, audit = setup_check.run_report(settings, bad, True)
    assert audit["years"]["error"] and audit["years"]["rows"] == []


def test_check_page_shows_year_counts_without_identifiers(viewer, source):
    from trend import year_of
    cur = year_of(date.today())
    source.visits_by_year = {cur - 1: 0, cur - 2: 12}
    body = viewer.get("/check").get_data(as_text=True)
    assert 'data-testid="years-table"' in body and str(cur - 2) in body and ">12<" in body
    for private in (HN, PNAME, "0899990001", "VN:1"):
        assert private not in body


# ---------- ขอบเขตอำเภอบนแผนที่ (v1.3.0) ----------

def test_check_boundary_ok_for_3025(tmp_path):
    status, _, detail = get(run(make_settings(tmp_path, DENGUE_AREA_AMPHOE="3025")), "ขอบเขตอำเภอบนแผนที่")
    assert status == "ok" and "3025" in detail


def test_check_boundary_warns_when_file_missing_with_fetch_command(tmp_path):
    status, _, detail = get(run(make_settings(tmp_path, DENGUE_AREA_AMPHOE="3025,9999")), "ขอบเขตอำเภอบนแผนที่")
    assert status == "warn" and "9999" in detail and "3025" not in detail.split("fetch_boundary.py")[0]
    assert "fetch_boundary.py" in detail and "พิกัดตั้งต้น" in detail


def test_check_boundary_warns_when_area_unknown(tmp_path):
    src = FakeSource(area=())
    status, _, detail = get(run(make_settings(tmp_path), src), "ขอบเขตอำเภอบนแผนที่")
    assert status == "warn" and "ยังไม่ทราบอำเภอ" in detail


def test_check_boundary_never_raises_and_does_not_use_network(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("ห้ามออกเครือข่าย")

    monkeypatch.setattr(setup_check.socket, "create_connection", boom)   # แถว MOPH จะล้มของมันเอง ไม่เกี่ยวกับเรา
    monkeypatch.setattr("requests.get", boom)
    monkeypatch.setattr("requests.post", boom)
    down = FakeSource(failure=HosxpError("down"), fail_methods=["default_area_amphoe"])
    status, _, detail = get(run(make_settings(tmp_path), down), "ขอบเขตอำเภอบนแผนที่")
    assert status == "warn" and "ไม่สำเร็จ" in detail


def test_audit_reports_boundary_rows(tmp_path):
    _, audit = setup_check.run_report(make_settings(tmp_path, DENGUE_AREA_AMPHOE="3025,9999"), FakeSource(), True)
    assert audit["boundary"]["rows"] == [{"code": "3025", "ok": True}, {"code": "9999", "ok": False}]
    assert audit["boundary"]["error"] is None
    down = FakeSource(failure=HosxpError("x"), fail_methods=["default_area_amphoe"])
    _, audit = setup_check.run_report(make_settings(tmp_path), down, True)
    assert audit["boundary"]["rows"] == [] and audit["boundary"]["error"]


def test_check_page_shows_boundary_table_without_identifiers(tmp_path, store, login_as, source):
    app = create_app(settings=make_settings(tmp_path, DENGUE_AREA_AMPHOE="3025,9999"), store=store,
                     source=source, sender=FakeSender())
    c = app.test_client()
    login_as(c, "viewer1", "viewer")
    body = c.get("/check").get_data(as_text=True)
    assert 'data-testid="boundary-table"' in body and "3025" in body and "9999" in body
    for private in (HN, PNAME, "0899990001", "VN:1"):
        assert private not in body


# ---------- ขอบเขตตำบลและพิกัดสถานพยาบาล (v1.4.0) ----------

def test_check_tambon_ok_with_synthetic_file(tmp_path, monkeypatch):
    tambon_dir(tmp_path, monkeypatch)
    status, _, detail = get(run(make_settings(tmp_path, DENGUE_AREA_AMPHOE="3025")), "ขอบเขตตำบลบนแผนที่")
    assert status == "ok" and "3025" in detail and "2 ตำบล" in detail


def test_check_tambon_warns_when_missing_with_command(tmp_path, monkeypatch):
    tambon_dir(tmp_path, monkeypatch, codes=("3025",))
    status, _, detail = get(run(make_settings(tmp_path, DENGUE_AREA_AMPHOE="3025,9999")), "ขอบเขตตำบลบนแผนที่")
    assert status == "warn" and "9999" in detail and "make_tambon_boundary.py" in detail and "ชั้นตำบล" in detail


def test_check_tambon_warns_when_area_unknown(tmp_path, monkeypatch):
    tambon_dir(tmp_path, monkeypatch)
    status, _, detail = get(run(make_settings(tmp_path), FakeSource(area=())), "ขอบเขตตำบลบนแผนที่")
    assert status == "warn" and "ยังไม่ทราบอำเภอ" in detail


def test_check_facilities_ok_when_all_placed(tmp_path):
    st = placed_store(tmp_path, FAC_RPST, FAC_HOSP)
    status, _, detail = get(run(make_settings(tmp_path, DENGUE_AREA_AMPHOE="3025"),
                                FakeSource(facilities=[FAC_RPST, FAC_HOSP]), store=st), "พิกัดสถานพยาบาล")
    assert status == "ok" and "2 แห่ง" in detail


def test_check_facilities_warns_with_names_when_unplaced(tmp_path):
    st = placed_store(tmp_path, FAC_RPST)
    status, _, detail = get(run(make_settings(tmp_path, DENGUE_AREA_AMPHOE="3025"),
                                FakeSource(facilities=[FAC_RPST, FAC_HOSP]), store=st), "พิกัดสถานพยาบาล")
    assert status == "warn" and "โรงพยาบาลวังน้ำเขียว" in detail and "1 แห่ง" in detail
    assert "import-facilities" in detail and "จัดตำแหน่งสถานพยาบาล" in detail


def test_check_facilities_warns_without_store_and_when_hosxp_down(tmp_path):
    s = make_settings(tmp_path, DENGUE_AREA_AMPHOE="3025")
    status, _, detail = get(run(s, FakeSource(facilities=[FAC_RPST])), "พิกัดสถานพยาบาล")
    assert status == "warn" and "ไม่มีฐานข้อมูลแอป" in detail
    down = FakeSource(facilities=[FAC_RPST], failure=HosxpError("down 10.1.2.3"), fail_methods=["facilities"])
    status, _, detail = get(run(s, down, store=placed_store(tmp_path)), "พิกัดสถานพยาบาล")
    assert status == "warn" and "ไม่สำเร็จ" in detail and "10.1.2.3" not in detail
    status, _, detail = get(run(s, FakeSource(), store=placed_store(tmp_path)), "พิกัดสถานพยาบาล")
    assert status == "warn" and "ไม่พบ" in detail


def test_new_checks_never_use_network(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("ห้ามออกเครือข่าย")

    monkeypatch.setattr(setup_check.socket, "create_connection", boom)    # แถว MOPH ล้มของมันเอง ไม่เกี่ยวกับเรา
    monkeypatch.setattr("requests.get", boom)
    monkeypatch.setattr("requests.post", boom)
    results = run(make_settings(tmp_path, DENGUE_AREA_AMPHOE="3025"), FakeSource(facilities=[FAC_RPST]),
                  store=placed_store(tmp_path))
    assert get(results, "ขอบเขตตำบลบนแผนที่") and get(results, "พิกัดสถานพยาบาล")


def test_audit_has_tambon_and_facility_blocks(tmp_path, monkeypatch):
    tambon_dir(tmp_path, monkeypatch)
    st = placed_store(tmp_path, FAC_RPST)
    _, audit = setup_check.run_report(make_settings(tmp_path, DENGUE_AREA_AMPHOE="3025,9999"),
                                      FakeSource(facilities=[FAC_RPST, FAC_HOSP]), True, store=st)
    assert audit["tambon"]["rows"] == [{"code": "3025", "ok": True, "names": ["ตำบลหนึ่ง", "ตำบลสอง"]},
                                       {"code": "9999", "ok": False, "names": []}]
    assert audit["tambon"]["error"] is None
    assert audit["facilities"]["placed"] == 1 and audit["facilities"]["error"] is None
    assert audit["facilities"]["unplaced"] == [{"code": "10894", "name": "โรงพยาบาลวังน้ำเขียว", "kind": "hospital"}]
    down = FakeSource(failure=HosxpError("x"), fail_methods=["facilities", "default_area_amphoe"])
    _, audit = setup_check.run_report(make_settings(tmp_path), down, True, store=st)
    assert audit["facilities"]["error"] and audit["tambon"]["error"]


def test_check_page_shows_tambon_and_facility_tables_without_identifiers(tmp_path, store, login_as, monkeypatch):
    tambon_dir(tmp_path, monkeypatch)
    src = FakeSource(facilities=[FAC_RPST, FAC_HOSP], rows=[make_row("VN:1", hn=HN)],
                     contacts={HN: {"name": PNAME, "phone": "0899990001", "address": "บ้านเลขที่ 99"}})
    store.facility_upsert("02846", FAC_RPST["name"], "rpst", "302503", 14.4, 101.8, "csv", "t", datetime(2026, 11, 10))
    app = create_app(settings=make_settings(tmp_path, DENGUE_AREA_AMPHOE="3025"), store=store, source=src,
                     sender=FakeSender())
    c = app.test_client()
    login_as(c, "viewer1", "viewer")
    body = c.get("/check").get_data(as_text=True)
    assert 'data-testid="tambon-table"' in body and "ตำบลหนึ่ง" in body
    assert 'data-testid="facility-table"' in body and "โรงพยาบาลวังน้ำเขียว" in body
    for private in (HN, PNAME, "0899990001", "VN:1"):
        assert private not in body
