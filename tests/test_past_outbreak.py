"""past_outbreak — รวมจำนวนรายต่อหมู่บ้านของปี พ.ศ.หนึ่งเป็นข้อมูลวงกลมบนแผนที่ (ฟังก์ชันล้วน)"""
from datetime import datetime

import past_outbreak
from trend import CountRow
from trend_service import YearResult

AT = datetime(2026, 11, 10, 9, 0)


def result(rows, **kw):
    base = dict(year=2569, rows=rows, computed_at=AT, stale=False, error=None, throttled=False)
    base.update(kw)
    return YearResult(**base)


ROWS = [
    CountRow(0, True, "302501", "03", 4), CountRow(5, True, "302501", "03", 1),   # รวม 5 ที่ 30250103
    CountRow(1, True, "302501", "05", 2),                                          # ไม่มีจุดกลาง
    CountRow(2, True, "302502", "01", 7),                                          # มากสุด
    CountRow(3, True, "302501", "00", 3),                                          # ไม่ทราบหมู่
    CountRow(4, True, "", "00", 2),                                                # ไม่ทราบตำบล
    CountRow(4, False, "262501", "01", 9),                                         # นอกอำเภอ ไม่นับ
]
CENTROIDS = {"30250103": (14.31, 101.81), "30250201": (14.4, 101.9)}
NAMES = {"30250201": "บ้านสอง"}


def test_build_past_groups_by_village_code_and_sorts():
    out = past_outbreak.build_past(2569, result(ROWS), CENTROIDS, NAMES)
    assert [(v["code"], v["cases"]) for v in out["villages"]] == [("30250201", 7), ("30250103", 5)]
    first = out["villages"][0]
    assert first == {"code": "30250201", "name": "บ้านสอง", "lat": 14.4, "lng": 101.9, "cases": 7}
    assert out["year"] == 2569 and out["computed_at"] == "2026-11-10T09:00:00"
    assert out["stale"] is False and out["error"] is None


def test_unmapped_buckets_sum_invariant():
    out = past_outbreak.build_past(2569, result(ROWS), CENTROIDS, NAMES)
    assert out["total"] == 4 + 1 + 2 + 7 + 3 + 2 == 19                  # นอกอำเภอไม่รวม
    assert out["unmapped"] == {"villages": 1, "cases": 2 + 3 + 2}        # 30250105 + ไม่ทราบหมู่ + ไม่ทราบตำบล
    assert sum(v["cases"] for v in out["villages"]) + out["unmapped"]["cases"] == out["total"]


def test_name_fallback_uses_moo_number():
    out = past_outbreak.build_past(2569, result(ROWS), CENTROIDS, {})
    assert {v["code"]: v["name"] for v in out["villages"]} == {"30250201": "ม.1", "30250103": "ม.3"}


def test_error_and_busy_pass_through_with_empty_rows():
    out = past_outbreak.build_past(2569, result([], computed_at=None, error="ดึงข้อมูลไม่สำเร็จ"), {}, {})
    assert out["error"] == "ดึงข้อมูลไม่สำเร็จ" and out["total"] == 0 and out["villages"] == []
    assert out["computed_at"] is None
    busy = past_outbreak.build_past(2569, result([], computed_at=None, busy=True), {}, {})
    assert busy["error"] == "กำลังคำนวณอยู่" and busy["total"] == 0
    stale = past_outbreak.build_past(2569, result(ROWS, stale=True, error="ดึงข้อมูลไม่สำเร็จ"), CENTROIDS, {})
    assert stale["stale"] is True and stale["error"] == "ดึงข้อมูลไม่สำเร็จ" and stale["total"] == 19


def test_village_codes_only_in_area_valid_unique():
    assert past_outbreak.village_codes(ROWS) == ["30250103", "30250105", "30250201"]
    assert past_outbreak.village_codes([]) == []


def test_result_has_no_identifier_keys():
    out = past_outbreak.build_past(2569, result(ROWS), CENTROIDS, NAMES)
    assert set(out) == {"year", "total", "villages", "unmapped", "computed_at", "stale", "error",
                        "centroid_error"}
    assert set(out["unmapped"]) == {"villages", "cases"}
    for v in out["villages"]:
        assert set(v) == {"code", "name", "lat", "lng", "cases"}


def test_centroid_failure_sets_error_and_flag_without_hiding_total():
    out = past_outbreak.build_past(2569, result(ROWS), {}, {}, centroid_error=True)
    assert out["centroid_error"] is True and out["error"] == "ดึงพิกัดหมู่บ้านไม่สำเร็จ"
    assert out["total"] == 19 and out["villages"] == []
    # ความผิดพลาดของปี พ.ศ. (คำนวณไม่ได้) มาก่อนข้อความพิกัด
    both = past_outbreak.build_past(2569, result([], computed_at=None, error="ดึงข้อมูลไม่สำเร็จ"),
                                    {}, {}, centroid_error=True)
    assert both["error"] == "ดึงข้อมูลไม่สำเร็จ"
    ok = past_outbreak.build_past(2569, result(ROWS), CENTROIDS, NAMES)
    assert ok["centroid_error"] is False and ok["error"] is None
