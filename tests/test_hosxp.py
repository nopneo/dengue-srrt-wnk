"""HosxpSource กับ connection ปลอม: ตรวจ SQL แบบพารามิเตอร์ การแปลงแถว และการยก HosxpError."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from hosxp import DxRow, HosxpError, HosxpSource

ICDS = ("A90", "A91")


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def execute(self, sql, params=()):
        self.conn.executed.append((sql, tuple(params)))
        if self.conn.error is not None:
            raise self.conn.error
        self.rows = self.conn.results.pop(0) if self.conn.results else []

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class FakeConn:
    def __init__(self, results=(), error=None):
        self.results = [list(r) for r in results]
        self.error = error
        self.executed = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def source(conn):
    return HosxpSource(lambda: conn)


# (vn, hn, icd10, vstdate, vsttime, chw, amp, tmb, moo, addrpart, sex, birthday)
OPD_ROW = ("6911080001", "000123", "A911", date(2026, 11, 8), timedelta(hours=9, minutes=30),
           "40", "01", "01", "3", " 12/4 ", "2", date(1996, 11, 9))
# (an, hn, icd10, regdate, regtime, chw, amp, tmb, moo, addrpart, sex, birthday)
IPD_ROW = ("691100007", "000456", "A90", date(2026, 11, 7), time(14, 5, 0),
           "40", "02", "05", "11", "7", "1", None)


def test_new_dx_uses_parameters_not_literals():
    conn = FakeConn([[], []])
    source(conn).new_dx(datetime(2026, 11, 1), ICDS)
    assert len(conn.executed) == 2
    for sql, params in conn.executed:
        assert "A9" not in sql
        assert sql.count("%s") == len(params)
        assert "icd10 LIKE %s" in sql
        assert params[0] == date(2026, 11, 1)
        assert params[1:] == ("A90%", "A91%")
    assert "ovstdiag" in conn.executed[0][0] and "iptdiag" in conn.executed[1][0]
    assert conn.closed


def test_new_dx_normalises_dotted_prefix():
    conn = FakeConn([[], []])
    source(conn).new_dx(datetime(2026, 11, 1), ("A91.1",))
    assert conn.executed[0][1][1:] == ("A911%",)


def test_new_dx_without_icds_queries_nothing():
    conn = FakeConn()
    assert source(conn).new_dx(datetime(2026, 11, 1), ()) == []
    assert conn.executed == []


def test_new_dx_maps_opd_and_ipd_rows():
    conn = FakeConn([[OPD_ROW], [IPD_ROW]])
    rows = source(conn).new_dx(datetime(2026, 11, 1), ICDS)
    by_key = {r.visit_key: r for r in rows}
    opd, ipd = by_key["VN:6911080001"], by_key["AN:691100007"]
    assert opd == DxRow(
        visit_key="VN:6911080001", visit_type="OPD", hn="000123", icd10="A911",
        dx_at=datetime(2026, 11, 8, 9, 30), chw="40", amp="01", tmb="01", moo="3",
        house_no="12/4", sex="2", age_years=29)  # เกิด 9 พ.ย. 2539 ยังไม่ถึงวันเกิดในปีนั้น
    assert ipd.visit_key == "AN:691100007" and ipd.visit_type == "IPD"
    assert ipd.dx_at == datetime(2026, 11, 7, 14, 5)
    assert ipd.sex == "1" and ipd.age_years is None and ipd.amp == "02"


def test_new_dx_tolerates_null_time_and_blank_parts():
    row = ("1", "000001", "A90", date(2026, 11, 8), None, None, "", " ", None, "", None, None)
    (dx,) = source(FakeConn([[row], []])).new_dx(datetime(2026, 11, 1), ICDS)
    assert dx.dx_at == datetime(2026, 11, 8, 0, 0)
    assert dx.chw is None and dx.amp is None and dx.tmb is None
    assert dx.moo is None and dx.house_no is None and dx.sex is None


def test_driver_exception_becomes_hosxp_error():
    for call in (
        lambda s: s.new_dx(datetime(2026, 11, 1), ICDS),
        lambda s: s.dx_present("VN:1", ICDS),
        lambda s: s.dx_present_many(["VN:1", "AN:2"], ICDS),
        lambda s: s.contact("000001"),
        lambda s: s.village_centroid("40010103"),
        lambda s: s.default_area_amphoe(),
    ):
        with pytest.raises(HosxpError):
            call(source(FakeConn(error=RuntimeError("boom"))))


def test_connect_failure_becomes_hosxp_error():
    def bad():
        raise OSError("no route")

    with pytest.raises(HosxpError):
        HosxpSource(bad).dx_present("VN:1", ICDS)


def test_dx_present_false_only_on_empty_result():
    conn = FakeConn([[]])
    assert source(conn).dx_present("VN:6911080001", ICDS) is False
    sql, params = conn.executed[0]
    assert "ovstdiag" in sql and "A9" not in sql and sql.count("%s") == len(params)
    assert "vn IN (%s)" in sql
    assert params == ("6911080001", "A90%", "A91%")
    assert source(FakeConn([[("1",)]])).dx_present("VN:1", ICDS) is True


def test_dx_present_ipd_uses_iptdiag():
    conn = FakeConn([[("691100007",)]])
    assert source(conn).dx_present("AN:691100007", ICDS) is True
    assert "iptdiag" in conn.executed[0][0]
    assert conn.executed[0][1][0] == "691100007"


def test_dx_present_rejects_unknown_key():
    with pytest.raises(ValueError):
        source(FakeConn()).dx_present("XX:1", ICDS)


def test_village_centroid():
    conn = FakeConn([[(Decimal("14.5"), Decimal("101.8"))]])
    assert source(conn).village_centroid("40010103") == (14.5, 101.8)
    assert conn.executed[0][1] == ("40010103",)
    assert source(FakeConn([[]])).village_centroid("40010103") is None
    assert source(FakeConn([[(None, 101.8)]])).village_centroid("40010103") is None


def test_default_area_amphoe():
    conn = FakeConn([[("400101",), ("400102",), ("40010305",), (None,), ("12",)]])
    assert source(conn).default_area_amphoe() == ("4001",)


def test_contact_with_thai_address_names():
    patient = [("นาย", "สมชาย", "ใจดี", None, "044-111222", "081", "12/4", "3", "01", "01", "40")]
    names = [("40", "ขอนแก่น"), ("4001", "เมืองขอนแก่น"), ("400101", "ในเมือง")]
    c = source(FakeConn([patient, names])).contact("000123")
    assert c["name"] == "นายสมชาย ใจดี"
    assert c["phone"] == "044-111222"
    assert c["address"] == "บ้านเลขที่ 12/4 หมู่ 3 ต.ในเมือง อ.เมืองขอนแก่น จ.ขอนแก่น"


def test_contact_falls_back_to_codes_when_lookup_fails_or_missing():
    patient = [(None, "สมหญิง", None, "", "", "", "", "0", "01", "01", "40")]

    class LookupFails(FakeConn):
        def cursor(self):
            cur = super().cursor()
            original = cur.execute

            def execute(sql, params=()):
                if "thaiaddress" in sql:
                    raise RuntimeError("no table")
                original(sql, params)

            cur.execute = execute
            return cur

    c = source(LookupFails([patient])).contact("1")
    assert c["name"] == "สมหญิง" and c["phone"] == ""
    assert c["address"] == "ต.01 อ.01 จ.40"


def test_contact_unknown_hn_gives_empty_fields():
    assert source(FakeConn([[]])).contact("nope") == {"name": "", "phone": "", "address": ""}


def test_dx_present_many_returns_subset_for_mixed_keys():
    conn = FakeConn([[("1",), ("3",)], [("77",)]])
    present = source(conn).dx_present_many(
        ["VN:1", "VN:2", "VN:3", "AN:77", "AN:78"], ICDS)
    assert present == {"VN:1", "VN:3", "AN:77"}
    assert len(conn.executed) == 2 and conn.closed
    (opd_sql, opd_params), (ipd_sql, ipd_params) = conn.executed
    assert "ovstdiag" in opd_sql and "vn IN (%s,%s,%s)" in opd_sql
    assert opd_params == ("1", "2", "3", "A90%", "A91%")
    assert "iptdiag" in ipd_sql and "an IN (%s,%s)" in ipd_sql
    assert ipd_params == ("77", "78", "A90%", "A91%")
    for sql, params in conn.executed:
        assert "A9" not in sql and sql.count("%s") == len(params)


def test_dx_present_many_chunks_at_200_keys_in_one_connection():
    keys = [f"VN:{i}" for i in range(450)]
    conn = FakeConn([[("0",)], [("200",)], [("400",)]])
    opened = []
    src = HosxpSource(lambda: opened.append(1) or conn)
    present = src.dx_present_many(keys, ICDS)
    assert present == {"VN:0", "VN:200", "VN:400"}
    assert len(opened) == 1
    sizes = [len(params) - 2 for _, params in conn.executed]
    assert sizes == [200, 200, 50]


def test_dx_present_many_empty_keys_does_not_touch_db():
    def boom():
        raise AssertionError("must not connect")

    assert HosxpSource(boom).dx_present_many([], ICDS) == set()


@pytest.mark.parametrize("icds", [(), ("",), ("  ", ".")])
def test_dx_present_without_disease_codes_raises(icds):
    conn = FakeConn([[("1",)]])
    with pytest.raises(HosxpError, match="no disease codes configured"):
        source(conn).dx_present("VN:1", icds)
    with pytest.raises(HosxpError, match="no disease codes configured"):
        source(conn).dx_present_many(["VN:1"], icds)
    assert conn.executed == []


def test_dx_present_many_rejects_unknown_key():
    with pytest.raises(ValueError):
        source(FakeConn()).dx_present_many(["XX:1"], ICDS)


# ---- area_label (ชื่อตำบล + หมู่ จากรหัสหมู่บ้าน 8 หลัก) ----
def test_area_label_builds_tambon_and_moo_and_caches():
    conn = FakeConn([[("ในเมือง",)]])
    src = source(conn)
    assert src.area_label("40010103") == "ต.ในเมือง ม.3"
    assert src.area_label("40010103") == "ต.ในเมือง ม.3"  # รอบสองมาจากแคช
    assert len(conn.executed) == 1
    sql, params = conn.executed[0]
    assert "thaiaddress" in sql and "%s" in sql and params == ("400101",)


def test_area_label_moo_is_int_not_zero_padded():
    assert source(FakeConn([[("วังน้ำเขียว",)]])).area_label("40010112") == "ต.วังน้ำเขียว ม.12"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_area_label_error_gives_none_and_is_not_cached_forever():
    conn = FakeConn(error=RuntimeError("no table"))
    clock = Clock()
    src = HosxpSource(lambda: conn, clock=clock)
    assert src.area_label("40010103") is None
    conn.error = None
    conn.results = [[("ในเมือง",)]]
    clock.t += 61
    assert src.area_label("40010103") == "ต.ในเมือง ม.3"  # error ไม่ถูกแคชถาวร


def test_area_label_backs_off_60s_after_error_for_every_code():
    conn = FakeConn(error=RuntimeError("down"))
    clock = Clock()
    src = HosxpSource(lambda: conn, clock=clock)
    assert src.area_label("40010103") is None
    assert len(conn.executed) == 1
    clock.t += 59
    assert src.area_label("40010103") is None
    assert src.area_label("40010204") is None  # รหัสอื่นก็ไม่ต่อใหม่ระหว่างงดลอง
    assert len(conn.executed) == 1
    conn.error = None
    conn.results = [[("ในเมือง",)]]
    clock.t += 2  # ครบ 61 วินาที
    assert src.area_label("40010204") == "ต.ในเมือง ม.4"
    assert len(conn.executed) == 2


def test_area_label_missing_row_or_bad_code_gives_none():
    conn = FakeConn([[]])
    src = source(conn)
    assert src.area_label("40010103") is None
    assert src.area_label("40010103") is None  # ไม่พบก็แคชไว้ (ข้อมูลคงที่)
    assert len(conn.executed) == 1
    for bad in ("", "4001", "4001010x", None, "400101033"):
        assert src.area_label(bad) is None
    assert len(conn.executed) == 1  # รหัสผิดรูปแบบไม่ยิง query


# ---- ข้อมูลย้อนหลังสำหรับหน้าเปรียบเทียบปี พ.ศ. ----
from trend import VisitRow  # noqa: E402

WRITE_WORDS = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE", "REPLACE")


def test_episode_rows_queries_are_read_only_and_parameterized():
    conn = FakeConn([[], [], []])  # ชุดแรกเป็นของ SET SESSION max_statement_time
    assert source(conn).episode_rows(ICDS, date(2025, 10, 1), date(2026, 9, 30)) == []
    queries = [e for e in conn.executed if not e[0].startswith("SET SESSION")]
    assert len(queries) == 2
    for sql, params in queries:
        assert not any(w in sql.upper().split() for w in WRITE_WORDS)
        assert "A90" not in sql and "2025" not in sql  # ค่าไปทางพารามิเตอร์เท่านั้น
        assert sql.count("%s") == len(params)
        assert params[-2:] == ("A90%", "A91%")
        assert "COALESCE(" not in sql.split("WHERE", 1)[1]     # ตัวกรองวันที่ต้องใช้ดัชนีได้ (sargable)
    d1, d2 = date(2025, 10, 1), date(2026, 9, 30)
    assert queries[0][1][:4] == (d1, d2, d1, d2)    # vstdate หรือ (vstdate เป็น NULL แล้วใช้ ovst.vstdate)
    assert queries[1][1][:2] == (d1, d2)
    assert "ovstdiag" in queries[0][0] and "iptdiag" in queries[1][0]


def test_episode_rows_maps_rows_and_skips_bad():
    opd = [("000123", date(2025, 11, 8), "40", "01", "01", "3"),
           ("", date(2025, 11, 9), "40", "01", "01", "3"),           # hn ว่าง -> ข้าม
           ("000124", None, "40", "01", "01", "3")]                   # ไม่มีวันที่ -> ข้าม
    ipd = [("000456", datetime(2025, 12, 1, 0, 0), None, None, None, 11)]
    rows = source(FakeConn([[], opd, ipd])).episode_rows(ICDS, date(2025, 10, 1), date(2026, 9, 30))
    assert rows == [VisitRow("000123", date(2025, 11, 8), "40", "01", "01", "3"),
                    VisitRow("000456", date(2025, 12, 1), None, None, None, "11")]


def test_episode_rows_empty_icds_returns_empty_without_query():
    def boom():
        raise AssertionError("must not connect")

    assert HosxpSource(boom).episode_rows((), date(2025, 10, 1), date(2026, 9, 30)) == []
    assert HosxpSource(boom).episode_rows(("",), date(2025, 10, 1), date(2026, 9, 30)) == []


def test_episode_rows_driver_error_becomes_hosxp_error():
    with pytest.raises(HosxpError):
        source(FakeConn(error=RuntimeError("boom"))).episode_rows(
            ICDS, date(2025, 10, 1), date(2026, 9, 30))


def test_address_names_merges_tambon_and_village_names():
    tambon = [("363001", "วังน้ำเขียว"), ("363002", " ")]
    village = [("36300103", "บ้านทดสอบ")]
    conn = FakeConn([tambon, village])
    names = source(conn).address_names(["363001", "363002", "36300103", "bad", "36300103"])
    assert names == {"363001": "วังน้ำเขียว", "36300103": "บ้านทดสอบ"}
    assert "thaiaddress" in conn.executed[0][0] and "village" in conn.executed[1][0]
    assert conn.executed[0][1] == ("363001", "363002")      # ตัดรหัสผิดรูปและซ้ำ
    assert conn.executed[1][1] == ("36300103",)


def test_address_names_failure_returns_empty():
    assert source(FakeConn(error=RuntimeError("down"))).address_names(["363001"]) == {}
    assert source(FakeConn()).address_names([]) == {}


def test_address_names_chunks_large_lists():
    codes = [f"36{i:04d}" for i in range(450)]
    conn = FakeConn([[], [], []])
    source(conn).address_names(codes)
    assert [len(p) for _, p in conn.executed] == [200, 200, 50]


def test_visits_in_ranges_sums_opd_and_ipd_per_range_in_one_connection():
    r1, r2 = (date(2025, 10, 1), date(2026, 9, 30)), (date(2024, 10, 1), date(2025, 9, 30))
    conn = FakeConn([[], [(7,)], [(2,)], [(0,)], [(4,)]])
    opened = []
    src = HosxpSource(lambda: opened.append(1) or conn)
    assert src.visits_in_ranges(ICDS, [r1, r2]) == [9, 4]
    assert len(opened) == 1
    queries = [e for e in conn.executed if not e[0].startswith("SET SESSION")]
    assert [p[:2] for _, p in queries] == [r1, r1, r2, r2]
    assert all("COALESCE(" not in sql.split("WHERE", 1)[1] for sql, _ in queries)
    assert all("COALESCE(" not in sql.split("WHERE", 1)[1] for sql, _ in queries)
    for sql, params in queries:
        assert sql.count("%s") == len(params) and "COUNT(" in sql.upper()
    assert source(FakeConn([[], [(None,)], [(None,)]])).visits_in_ranges(ICDS, [r1]) == [0]
    assert source(FakeConn()).visits_in_ranges((), [r1]) == [0]
    assert source(FakeConn()).visits_in_ranges(ICDS, []) == []


# ---- จุดกลางหมู่บ้าน (เลเยอร์ปีก่อนบนแผนที่) ----
def test_village_centroids_parameterized_read_only():
    conn = FakeConn([[("30250103", Decimal("14.3"), "101.8")]])
    got = source(conn).village_centroids(["30250103"])
    assert got == {"30250103": (14.3, 101.8)}
    (sql, params), = conn.executed
    assert "village_gis_location" in sql and sql.count("%s") == len(params) == 1
    assert not any(w in sql.upper().split() for w in WRITE_WORDS)
    assert "30250103" not in sql


def test_village_centroids_filters_bad_codes_and_coordinates():
    codes = ["abc", "1234567", "", "30250103", "30250103", "30250104", "30250105", "30250106",
             "30250107", "30250108", "30250109", "٣٠٢٥٠١٠٣"]
    rows = [("30250103", "14.3", "101.8"), ("30250104", None, 101.8), ("30250105", "", "101.8"),
            ("30250106", "0", "0"), ("30250107", "95", "101.8"), ("30250108", "x", "y"),
            ("30250109", float("nan"), 101.8), ("bad", 14.3, 101.8)]
    conn = FakeConn([rows])
    got = source(conn).village_centroids(codes)
    assert got == {"30250103": (14.3, 101.8)}
    assert conn.executed[0][1] == ("30250103", "30250104", "30250105", "30250106",
                                   "30250107", "30250108", "30250109")   # เฉพาะรหัส 8 หลักไม่ซ้ำ


def test_village_centroids_chunks_at_200_in_one_connection():
    codes = [f"3025{i:04d}" for i in range(450)]
    conn = FakeConn([[], [], []])
    opened = []
    HosxpSource(lambda: opened.append(1) or conn).village_centroids(codes)
    assert [len(p) for _, p in conn.executed] == [200, 200, 50] and len(opened) == 1


def test_village_centroids_error_raises_so_callers_can_tell_failure_from_no_data():
    with pytest.raises(HosxpError):
        source(FakeConn(error=RuntimeError("down"))).village_centroids(["30250103"])


def test_village_centroids_no_valid_codes_does_not_connect():
    def boom():
        raise AssertionError("must not connect")

    assert HosxpSource(boom).village_centroids(["abc", "", "123"]) == {}
    assert HosxpSource(boom).village_centroids([]) == {}


# ---- รายชื่อ รพ./รพ.สต. ของอำเภอ (หมุดสถานพยาบาลบนแผนที่) ----
RPST_NAME = "โรงพยาบาลส่งเสริมสุขภาพตำบล"


def test_facilities_query_parameterized_read_only():
    conn = FakeConn([[("02840", RPST_NAME + "ศาลเจ้าพ่อ", "30", "25", "01")]])
    got = source(conn).facilities(("3025",))
    assert got == [{"hospcode": "02840", "name": RPST_NAME + "ศาลเจ้าพ่อ", "kind": "rpst", "tmb": "302501"}]
    (sql, params), = conn.executed
    assert "hospcode" in sql and "CONCAT(" in sql.replace(" ", "") .replace("CONCAT(", "CONCAT(")
    assert sql.count("%s") == len(params) and params[0] == "3025"
    assert not any(w in sql.upper().split() for w in WRITE_WORDS) and "3025" not in sql


def test_facilities_filters_to_hospital_and_rpst_five_digit_codes():
    rows = [("00246", "สำนักงานสาธารณสุขอำเภอวังน้ำเขียว", "30", "25", "01"),
            ("02840", RPST_NAME + "ศาลเจ้าพ่อ", "30", "25", "01"),
            ("10894", "โรงพยาบาลวังน้ำเขียว", "30", "25", "05"),
            ("GA0002840", RPST_NAME + "ศาลเจ้าพ่อ", "30", "25", "01"),
            ("EA0010894", "โรงพยาบาลวังน้ำเขียว", "30", "25", "05"),
            ("32218", "คลินิกนายแพทย์วิชาญ", "30", "25", "01"),
            ("02840", RPST_NAME + "ศาลเจ้าพ่อ", "30", "25", "01"),           # ซ้ำ
            ("02850", RPST_NAME + "ไม่มีตำบล", "30", "25", None),            # ตำบลประกอบไม่ได้
            ("02851", RPST_NAME + "ตำบลผิดรูป", "30", "25", "x1")]
    got = source(FakeConn([rows])).facilities(("3025",))
    assert [(g["hospcode"], g["kind"], g["tmb"]) for g in got] == [("02840", "rpst", "302501"),
                                                                    ("10894", "hospital", "302505")]


def test_facilities_empty_area_does_not_connect():
    def boom():
        raise AssertionError("must not connect")

    assert HosxpSource(boom).facilities(()) == []
    assert HosxpSource(boom).facilities(("abc", "30", "")) == []


def test_facilities_error_raises_hosxp_error():
    with pytest.raises(HosxpError):
        source(FakeConn(error=RuntimeError("down"))).facilities(("3025",))
